"""Online-compatible CV Kalman filter with an explicit information clock.

All timestamps are the common times at which solver outputs became available.
The caller is responsible for identifying genuinely new measurement information,
including deduplicating measurement IDs across the entire prefix of a recording.
Repeated coordinates are allowed to be new observations, while a changed cached
coordinate is ignored when ``new_information=False``.

Parameters are fixed by the pre-existing experimental specification. This module
does not accept route coordinates, ground truth, per-arm parameters or gate values.
Only NumPy and the Python standard library are needed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from numbers import Real
from typing import Any

import numpy as np


@dataclass(frozen=True)
class _FixedSpecification:
    state_order: tuple[str, ...] = ("x", "y", "vx", "vy")
    acceleration_sigma_m_s2: float = 0.8
    acceleration_variance_m2_s4: float = 0.64
    measurement_variance_m2: float = 9.0
    initial_covariance_diagonal: tuple[float, ...] = (25.0, 25.0, 4.0, 4.0)
    publication_max_age_s: float = 4.0
    observation_reset_gap_s: float = 8.0
    innovation_gating: bool = False


FIXED_SPEC = _FixedSpecification()
_H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
_R = np.eye(2) * FIXED_SPEC.measurement_variance_m2
_I = np.eye(4)


def _finite_time(value: Any, name: str = "t") -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite real scalar in seconds")
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _coordinate(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (2,) or not np.isfinite(result).all():
        raise ValueError("xy must contain exactly two finite coordinates")
    return result.copy()


def process_matrices(dt: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return new F, G and Q arrays for a nonnegative interval in seconds.

    G has one independent acceleration input per axis. The exact declared model
    is Q = 0.64 G G.T, with dt**2/2 position terms and dt velocity terms.
    """
    interval = _finite_time(dt, "dt")
    if interval < 0.0:
        raise ValueError("dt must be nonnegative")
    try:
        with np.errstate(over="raise", invalid="raise"):
            half_dt_squared = 0.5 * np.float64(interval) ** 2
            F = np.array([[1.0, 0.0, interval, 0.0],
                          [0.0, 1.0, 0.0, interval],
                          [0.0, 0.0, 1.0, 0.0],
                          [0.0, 0.0, 0.0, 1.0]])
            G = np.array([[half_dt_squared, 0.0],
                          [0.0, half_dt_squared],
                          [interval, 0.0], [0.0, interval]])
            Q = FIXED_SPEC.acceleration_variance_m2_s4 * (G @ G.T)
    except FloatingPointError as exc:
        raise ValueError("dt is too large for finite CV process matrices") from exc
    if not all(np.isfinite(a).all() for a in (F, G, Q)):
        raise ValueError("dt is too large for finite CV process matrices")
    return F, G, Q


def _predict(x: np.ndarray, P: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
    F, _, Q = process_matrices(dt)
    try:
        with np.errstate(over="raise", invalid="raise"):
            predicted_x = F @ x
            predicted_P = F @ P @ F.T + Q
    except FloatingPointError as exc:
        raise ValueError("prediction would produce a nonfinite state or covariance") from exc
    predicted_P = 0.5 * (predicted_P + predicted_P.T)
    if not np.isfinite(predicted_x).all() or not np.isfinite(predicted_P).all():
        raise ValueError("prediction would produce a nonfinite state or covariance")
    return predicted_x, predicted_P


class CausalCVKalman:
    """Fixed-parameter [x,y,vx,vy] KF, with causal publication expiry.

    ``step(t, xy, new_information)`` consumes one common solver-log availability
    event. The times must increase. An equal-time event is accepted only when it
    carries no new information, in which case prediction has dt=0 and no update.
    Equal-time new observations and earlier times raise without changing state.

    Every valid step returns a detached JSON-compatible audit record and stores a
    detached copy in ``audit``. ``xy`` is the publishable position or None. ``state``
    and ``P`` retain the internal prediction for auditing even after publication
    expires. Age exactly 4 s is still publishable, and a gap exactly 8 s does not
    reset. A genuinely new observation after a gap greater than 8 s resets to
    [observed_x, observed_y,0,0] and diag(25,25,4,4).

    ``predict_at(t)`` is a non-mutating query for t >= the last step time. Querying
    future times cannot advance the information clock or change a later step.
    """

    def __init__(self) -> None:
        self._x: np.ndarray | None = None
        self._P: np.ndarray | None = None
        self._clock: float | None = None
        self._last_observation_time: float | None = None
        self._measurement_updates = 0
        self._initializations = 0
        self._audit: list[dict[str, Any]] = []

    @property
    def state(self) -> np.ndarray | None:
        return None if self._x is None else self._x.copy()

    @property
    def covariance(self) -> np.ndarray | None:
        return None if self._P is None else self._P.copy()

    @property
    def last_observation_time(self) -> float | None:
        return self._last_observation_time

    @property
    def state_time(self) -> float | None:
        return self._clock

    @property
    def audit(self) -> list[dict[str, Any]]:
        return deepcopy(self._audit)

    @staticmethod
    def specification() -> dict[str, Any]:
        return asdict(FIXED_SPEC)

    @staticmethod
    def _record(*, t: float, x: np.ndarray | None, P: np.ndarray | None,
                last_observation_time: float | None, dt: float,
                new_information: bool, input_xy: np.ndarray | None,
                measurement_used: bool, update_reason: str, reset: bool,
                measurement_updates: int, initializations: int,
                event_index: int, record_kind: str,
                innovation: np.ndarray | None = None,
                gain: np.ndarray | None = None) -> dict[str, Any]:
        age = None if last_observation_time is None else t - last_observation_time
        published = bool(x is not None and age is not None
                         and age <= FIXED_SPEC.publication_max_age_s)
        publication_reason = ("uninitialized" if x is None else
                              "within_observation_age_limit" if published else
                              "expired_without_new_observation")
        return {
            "record_kind": record_kind, "event_index": event_index, "t": t,
            "dt_s": dt, "new_information": new_information,
            "input_xy": None if input_xy is None else input_xy.tolist(),
            "measurement_used": measurement_used, "update_reason": update_reason,
            "reset": reset, "measurement_updates": measurement_updates,
            "initializations": initializations, "published": published,
            "publication_reason": publication_reason,
            "xy": x[:2].tolist() if published else None,
            "state": None if x is None else x.tolist(),
            "P": None if P is None else P.tolist(),
            "last_observation_time": last_observation_time,
            "observation_age_s": age,
            "innovation": None if innovation is None else innovation.tolist(),
            "K": None if gain is None else gain.tolist(),
        }

    def step(self, t: float, xy: Any = None,
             new_information: bool = False) -> dict[str, Any]:
        timestamp = _finite_time(t)
        if not isinstance(new_information, (bool, np.bool_)):
            raise TypeError("new_information must be a bool")
        has_information = bool(new_information)
        observation = _coordinate(xy)
        if has_information and observation is None:
            raise ValueError("new_information=True requires xy")
        if self._clock is not None:
            if timestamp < self._clock:
                raise ValueError("step timestamps must not move backwards")
            if timestamp == self._clock and has_information:
                raise ValueError("equal-time new observations are ambiguous and rejected")
        dt = 0.0 if self._clock is None else timestamp - self._clock
        if not np.isfinite(dt):
            raise ValueError("elapsed time must be finite")
        x = None if self._x is None else self._x.copy()
        P = None if self._P is None else self._P.copy()
        last_observation = self._last_observation_time
        measurement_updates = self._measurement_updates
        initializations = self._initializations
        gain = innovation = None
        reset = False

        # All computation occurs in local copies. Errors leave the filter and
        # prior audit records untouched.
        if has_information and (x is None or
                timestamp - last_observation > FIXED_SPEC.observation_reset_gap_s):
            reset = x is not None
            x = np.array([observation[0], observation[1], 0.0, 0.0])
            P = np.diag(FIXED_SPEC.initial_covariance_diagonal)
            last_observation = timestamp
            initializations += 1
            reason = "gap_reset" if reset else "initialized"
        elif x is not None:
            x, P = _predict(x, P, dt)
            if has_information:
                try:
                    with np.errstate(over="raise", invalid="raise"):
                        innovation = observation - _H @ x
                        S = _H @ P @ _H.T + _R
                        gain = np.linalg.solve(S, _H @ P).T
                        x = x + gain @ innovation
                        residual_operator = _I - gain @ _H
                        # Joseph form preserves covariance symmetry/PSD more
                        # reliably than subtracting two similar arrays.
                        P = (residual_operator @ P @ residual_operator.T
                             + gain @ _R @ gain.T)
                except FloatingPointError as exc:
                    raise ValueError("update would produce nonfinite values") from exc
                P = 0.5 * (P + P.T)
                if not np.isfinite(x).all() or not np.isfinite(P).all():
                    raise ValueError("update would produce a nonfinite state or covariance")
                last_observation = timestamp
                measurement_updates += 1
                reason = "measurement_update"
            elif dt == 0.0:
                reason = "same_time_no_information_noop"
            else:
                reason = ("predict_only_cached" if observation is not None else
                          "predict_only_no_observation")
        else:
            reason = "uninitialized_no_observation"

        row = self._record(t=timestamp, x=x, P=P,
                           last_observation_time=last_observation, dt=dt,
                           new_information=has_information, input_xy=observation,
                           measurement_used=has_information, update_reason=reason,
                           reset=reset, measurement_updates=measurement_updates,
                           initializations=initializations,
                           event_index=len(self._audit) + 1, record_kind="step",
                           innovation=innovation, gain=gain)
        self._x, self._P, self._clock = x, P, timestamp
        self._last_observation_time = last_observation
        self._measurement_updates, self._initializations = measurement_updates, initializations
        self._audit.append(deepcopy(row))
        return row

    def predict_at(self, t: float) -> dict[str, Any]:
        timestamp = _finite_time(t)
        if self._clock is not None and timestamp < self._clock:
            raise ValueError("predict_at cannot query a time before the last step")
        dt = 0.0 if self._clock is None else timestamp - self._clock
        if not np.isfinite(dt):
            raise ValueError("elapsed time must be finite")
        x = None if self._x is None else self._x.copy()
        P = None if self._P is None else self._P.copy()
        if x is not None:
            x, P = _predict(x, P, dt)
        return self._record(t=timestamp, x=x, P=P,
                            last_observation_time=self._last_observation_time, dt=dt,
                            new_information=False, input_xy=None, measurement_used=False,
                            update_reason="query_prediction" if x is not None else
                                          "query_uninitialized", reset=False,
                            measurement_updates=self._measurement_updates,
                            initializations=self._initializations,
                            event_index=len(self._audit), record_kind="query")
