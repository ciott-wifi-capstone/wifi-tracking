"""Small train-fold-only gate for automatic residual-correction branches.

The caller defines labels using training references only. The gate receives
event-local radio features at inference and has no route, identity, reference
distance, condition or ground-truth access. A gate probability describes the
training label; it must not be advertised as a calibrated LOS/NLOS probability.
This module does not load captures, train an experiment, or touch serial ports.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

import calibration_bench as cal


# This list matches the previous dedicated-feature Ridge, without the exactly
# redundant CIR threshold span or any past/completed-burst aggregation.
FULL_GATE_FEATURES = [
    "wipro_m", "raw_minus_wipro_m", "rssi_dbm", "csi_amp_mean",
    "csi_amp_std", "csi_amp_cv", "csi_missing", "rssi_missing",
    "relative_correction", "spectral_roughness", "spectral_deepfade_fraction",
    "spectral_entropy", "spectral_halfband_imbalance", "diagnostic_csi_missing",
    "cir_threshold_low_delta_m", "cir_threshold_high_delta_m",
    "cir_front_peak_gap_m", "cir_front_relative_amplitude",
    "cir_prefront_energy_fraction", "cir_peak_energy_fraction",
    "diagnostic_cir_missing",
]
PHYSICAL_GATE_FEATURES = [
    "relative_correction", "cir_threshold_low_delta_m",
    "cir_threshold_high_delta_m", "cir_front_peak_gap_m",
    "cir_prefront_energy_fraction", "cir_peak_energy_fraction",
    "spectral_roughness", "spectral_deepfade_fraction", "rssi_dbm",
]
DEFAULT_ALPHA = 0.1
DEFAULT_THRESHOLD = 0.5


def _feature_names(feature_names):
    names = list(FULL_GATE_FEATURES if feature_names is None else feature_names)
    if not names or len(names) != len(set(names)):
        raise ValueError("Gate feature names must be nonempty and unique")
    if any(name not in FULL_GATE_FEATURES for name in names):
        raise ValueError("Only predeclared event-local radio features are allowed")
    return names


def fit_gate(feature_rows, target_binary, sample_weights, *, feature_names=None,
             alpha=DEFAULT_ALPHA):
    """Fit weighted logistic regression with an unpenalized intercept.

    Sample weights are supplied by the experiment (normally equal physical
    pair/direction weight) and normalized to sum to one. No hidden class
    rebalancing is performed. All imputation/scaling uses these training rows
    only, through the existing calibration scaler. A single observed class
    produces a deterministic constant gate rather than an infinite intercept.
    """
    feature_rows = list(feature_rows)
    names = _feature_names(feature_names)
    y = np.asarray(target_binary, dtype=float)
    weights = np.asarray(sample_weights, dtype=float)
    n = len(feature_rows)
    if n == 0 or y.shape != (n,) or weights.shape != (n,):
        raise ValueError("Nonempty training features, labels and weights must align")
    if not np.isfinite(y).all() or not np.isin(y, [0., 1.]).all():
        raise ValueError("Gate labels must be binary zero or one")
    if not np.isfinite(weights).all() or (weights < 0).any() or not (weights > 0).any():
        raise ValueError("Sample weights must be finite, nonnegative and have positive sum")
    if not math.isfinite(float(alpha)) or float(alpha) < 0:
        raise ValueError("Regularization must be finite and nonnegative")
    # Scaling weights by their maximum also avoids overflow in the sum.
    weights = weights / weights.max()
    weights = weights / weights.sum()
    # A zero-weight sample cannot affect median imputation or the scaler.
    positive = weights > 0
    feature_rows = [row for row, keep in zip(feature_rows, positive) if keep]
    y, weights = y[positive], weights[positive]
    scaler = cal.fit_scaler(feature_rows, names, weights)
    X = cal.transform(scaler, feature_rows)
    if not np.isfinite(X).all():
        raise ValueError("Scaled gate training features must be finite")
    prior = float(weights @ y)
    common = {"version": "automatic_branch_gate_v1", "scaler": scaler,
              "alpha": float(alpha), "threshold": DEFAULT_THRESHOLD,
              "training_positive_weight_fraction": prior,
              "training_n": len(feature_rows),
              "objective": "weighted_log_loss + alpha * sum(scaled_slopes_squared)",
              "intercept_penalty": False, "class_rebalancing": False}
    if np.all(y == y[0]):
        return {**common, "kind": "constant", "constant_probability": float(y[0]),
                "optimizer_iterations": 0}
    design = np.column_stack([np.ones(len(X)), X])

    def objective(parameters):
        logits = design @ parameters
        # logaddexp and expit remain finite for very large signed logits.
        loss = np.logaddexp(0., logits) - y * logits
        slopes = parameters[1:]
        value = float(weights @ loss + float(alpha) * (slopes @ slopes))
        gradient = design.T @ (weights * (expit(logits) - y))
        gradient[1:] += 2 * float(alpha) * slopes
        return value, gradient

    start = np.zeros(design.shape[1])
    # The weighted fraction can round to exactly one under extreme imbalance,
    # even when both observed classes have positive weight. Bound the optimizer
    # initialization only; do not alter the labels, objective or final outputs.
    initial_prior = min(max(prior, np.finfo(float).eps), 1 - np.finfo(float).eps)
    start[0] = math.log(initial_prior) - math.log1p(-initial_prior)
    result = minimize(objective, start, jac=True, method="L-BFGS-B",
                      options={"maxiter": 400, "ftol": 1e-12, "gtol": 1e-8})
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError(f"Gate optimizer failed: {result.message}")
    return {**common, "kind": "logistic", "coefficients": result.x.tolist(),
            "optimizer_iterations": int(result.nit),
            "training_objective": float(result.fun)}


def predict_probability(model, feature_rows):
    """Return P(training-defined high-bias label) without refitting anything."""
    rows = list(feature_rows)
    _feature_names(model["scaler"]["feature_names"])
    if not rows:
        return np.empty(0, dtype=float)
    if model["kind"] == "constant":
        probability = float(model["constant_probability"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid constant gate probability")
        return np.full(len(rows), probability)
    if model["kind"] != "logistic":
        raise ValueError("Unknown gate kind")
    X = cal.transform(model["scaler"], rows)
    coefficients = np.asarray(model["coefficients"], dtype=float)
    if coefficients.shape != (X.shape[1] + 1,) or not np.isfinite(coefficients).all():
        raise ValueError("Invalid gate coefficients")
    if not np.isfinite(X).all():
        raise ValueError("Scaled gate inference features must be finite")
    logits = coefficients[0] + X @ coefficients[1:]
    probability = expit(logits)
    if not np.isfinite(probability).all():
        raise ValueError("Nonfinite gate probability")
    return probability


def branch_predictions(offset_corrected_m, feature_corrected_m, probability,
                       *, threshold=DEFAULT_THRESHOLD):
    """Apply fixed hard selection and soft mixing to already fitted branches.

    Values are not clipped: negative ranges remain visible for fair ranging
    evaluation. The caller can separately enforce a position solver's validity
    rule. This function has no truth, labels, or model-fitting inputs.
    """
    offset = np.asarray(offset_corrected_m, dtype=float)
    specialized = np.asarray(feature_corrected_m, dtype=float)
    probability = np.asarray(probability, dtype=float)
    if offset.ndim != 1 or offset.shape != specialized.shape or offset.shape != probability.shape:
        raise ValueError("Branch predictions and probabilities must be aligned vectors")
    if not all(np.isfinite(a).all() for a in (offset, specialized, probability)):
        raise ValueError("Branch inputs must be finite")
    if ((probability < 0) | (probability > 1)).any():
        raise ValueError("Gate probabilities must lie in [0, 1]")
    if not math.isfinite(float(threshold)) or not 0 <= float(threshold) <= 1:
        raise ValueError("Gate threshold must lie in [0, 1]")
    use_specialized = probability >= float(threshold)
    return {"hard_prediction_m": np.where(use_specialized, specialized, offset),
            "soft_prediction_m": (1 - probability) * offset + probability * specialized,
            "selected_specialized": use_specialized,
            "probability": probability.copy()}
