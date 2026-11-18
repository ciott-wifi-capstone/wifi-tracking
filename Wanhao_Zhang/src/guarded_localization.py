"""Causal age/geometry guards and weighted Huber slant-range localization.

No serial, model fitting, route, MARK, map boundary, or position history is used.
This is a separate experimental method; the original baseline is unchanged.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from statistics import median

import numpy as np
from scipy.optimize import least_squares

DEFAULT_CONFIG = dict(age_guard=True, geometry_guard=True, robust=True,
    native_offset_m=1.0881212435575938, default_refresh_s=6.,
    min_stale_cap_s=8., max_stale_cap_s=12., retention_cycles=2.,
    baseline_max_age_s=4., triangle_tolerance_m=1., motion_speed_mps=2., huber_scale_m=1.)


def _number(value):
    if isinstance(value, (str, bytes, bool)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _configuration(config):
    result = dict(DEFAULT_CONFIG)
    if config is not None:
        if not isinstance(config, Mapping) or set(config)-set(result):
            raise ValueError('Unknown guarded configuration')
        result.update(config)
    for key in ('age_guard', 'geometry_guard', 'robust'):
        if not isinstance(result[key], bool):
            raise ValueError('Guard toggles must be boolean')
    for key in set(result)-{'age_guard', 'geometry_guard', 'robust'}:
        value = _number(result[key])
        if value is None or value < 0 or (key not in {'native_offset_m', 'motion_speed_mps'} and value == 0):
            raise ValueError('Invalid guarded numerical parameter: '+key)
        result[key] = value
    if result['min_stale_cap_s'] > result['max_stale_cap_s']:
        raise ValueError('Minimum stale cap exceeds maximum')
    return result


def solve_guarded(anchors, links, height, now, config=None):
    """Return normal position result plus auditable range decisions.

    links[id] has automatic_m, same-batch native_m, original RANGE monotonic_s,
    observed refresh_s and samples_n. Optional available_monotonic_s and
    prediction_ready_monotonic_s must not exceed now. Caller aggregates only
    complete, already available exact-paired batches; no future data permitted.

    Pair triangle tolerances add vmax*absolute sample-time separation, allowing
    motion between asynchronous ranges. This is an assumed speed allowance,
    never an estimated velocity, trajectory constraint or position speed clip.
    """
    try:
        cfg = _configuration(config)
    except ValueError as error:
        return dict(ok=False, x=None, y=None, reason=str(error), anchors_n=0,
                    anchors_used=[], anchors_excluded=[], ranges_used={}, weights={}, link_diagnostics=[])
    result = dict(ok=False, x=None, y=None, residual_rms_m=None, anchors_n=0,
        reason='invalid_input', condition_number=None, anchors_used=[], anchors_excluded=[],
        ranges_used={}, weights={}, link_diagnostics=[], pair_diagnostics=[], config=cfg,
        age_s=None, measurement_span_s=None, robust_objective=None, valid_until_monotonic_s=None,
        method='age_geometry_guarded_huber_v2_native_fallback', route_or_marks_used=False,
        position_history_used=False, map_bounds_used=False, fitting=False,
        limitations=['Refresh period is an observed scheduling estimate, not synchronized RF sampling.',
            'Two metres per second is a declared triangle-tolerance assumption, not a measured speed.',
            'Pair consistency is necessary, not sufficient, for accurate ranges or a unique position.',
            'Fallback offset/native ranges can retain NLOS bias; robust loss cannot guarantee correct localization.',
            'Supplied height is assumed fixed; its uncertainty is not estimated.'])
    z, now = _number(height), _number(now)
    if z is None or z < 0 or now is None or not isinstance(links, Mapping):
        return result
    geometry = {}
    try:
        for anchor in anchors:
            label = anchor['id']
            xyz = tuple(_number(anchor.get(k)) for k in ('x', 'y', 'z'))
            if not isinstance(label, str) or not label.strip() or label in geometry or None in xyz:
                raise ValueError('invalid anchor')
            geometry[label] = xyz
    except (TypeError, ValueError, KeyError, AttributeError):
        result['reason'] = 'invalid_anchor_configuration'
        return result
    def time_eligible(link):
        if not isinstance(link, Mapping):
            return False
        timestamp = _number(link.get('monotonic_s'))
        if timestamp is None or timestamp > now:
            return False
        for name in ('available_monotonic_s', 'prediction_ready_monotonic_s'):
            if name in link:
                value = _number(link[name])
                if value is None or value > now:
                    return False
        return True

    observed = [_number(link.get('refresh_s')) for label, link in links.items()
                if label in geometry and time_eligible(link)]
    observed = [value for value in observed if value is not None and value > 0]
    cycle = median(observed) if observed else cfg['default_refresh_s']
    cap = min(cfg['max_stale_cap_s'], max(cfg['min_stale_cap_s'], cfg['retention_cycles']*cycle))
    if not cfg['age_guard']:
        cap = cfg['baseline_max_age_s']
    result.update(observed_refresh_s=cycle, stale_cap_s=cap)
    active = {}

    def exclude(diag, reason):
        diag['reasons'].append(reason)
        result['anchors_excluded'].append(dict(id=diag['id'], reason=reason))

    for label in sorted(geometry):
        link = links.get(label)
        diag = dict(id=label, automatic_m=None, native_m=None, proposed_m=None,
                    fallback_m=None, used_m=None, age_s=None, age_weight=0.,
                    geometry_weight=1., weight=0., source='excluded', reasons=[], valid=False, stale_cap_s=cap)
        result['link_diagnostics'].append(diag)
        if not isinstance(link, Mapping):
            exclude(diag, 'missing_paired_batch')
            continue
        timestamp = _number(link.get('monotonic_s'))
        unavailable = False
        for name in ('available_monotonic_s', 'prediction_ready_monotonic_s'):
            if name in link:
                available = _number(link[name])
                unavailable |= available is None or available > now
        if unavailable or timestamp is None or timestamp > now:
            exclude(diag, 'not_yet_available_or_invalid_time')
            continue
        age = now-timestamp
        diag['age_s'] = age
        diag['samples_n'] = link.get('samples_n')
        if age > cap or (cfg['age_guard'] and age >= cap):
            exclude(diag, 'stale_paired_batch')
            continue
        age_weight = (1-(age/cap)**2)**2 if cfg['age_guard'] else 1.
        automatic, native = _number(link.get('automatic_m')), _number(link.get('native_m'))
        fallback = native-cfg['native_offset_m'] if native is not None else None
        minimum = abs(z-geometry[label][2])
        diag.update(automatic_m=automatic, native_m=native, proposed_m=automatic,
                    fallback_m=fallback, vertical_minimum_m=minimum, age_weight=age_weight)
        if automatic is None:
            exclude(diag, 'missing_or_nonfinite_automatic_range')
            continue
        used, source = automatic, 'automatic'
        physically_invalid = automatic < 0 or automatic < minimum
        if cfg['geometry_guard'] and physically_invalid:
            if fallback is not None and fallback >= minimum:
                used, source = fallback, 'native_offset_fallback'
                diag['reasons'].append('automatic_below_vertical_minimum_fallback')
            elif native is not None and native >= minimum:
                # Preserve an actual measurement when subtracting the offset
                # conflicts with the supplied (possibly unmeasured) height.
                used, source = native, 'native_unshifted_fallback'
                diag['reasons'].append('automatic_and_offset_invalid_use_original_native')
            else:
                exclude(diag, 'automatic_offset_and_native_physically_invalid')
                continue
        elif automatic < 0:
            exclude(diag, 'negative_automatic_range')
            continue
        diag.update(used_m=used, source=source, weight=age_weight, valid=True)
        active[label] = dict(value=used, fallback=fallback, native=native, time=timestamp, diag=diag)

    def pairs(values):
        diagnostics, objective = [], 0.
        labels = sorted(values)
        for i, left in enumerate(labels):
            for right in labels[i+1:]:
                a, b = values[left], values[right]
                separation = math.dist(geometry[left], geometry[right])
                dt = abs(a['time']-b['time'])
                motion = cfg['motion_speed_mps']*dt
                slack = cfg['triangle_tolerance_m']+motion
                difference = max(0., abs(a['value']-b['value'])-separation-slack)
                summation = max(0., separation-a['value']-b['value']-slack)
                weight = a['diag']['age_weight']*b['diag']['age_weight']
                objective += weight*(difference*difference+summation*summation)
                diagnostics.append(dict(a=left, b=right, anchor_separation_m=separation,
                    sample_separation_s=dt, motion_allowance_m=motion,
                    tolerance_m=slack, difference_violation_m=difference,
                    sum_violation_m=summation, violation_m=max(difference, summation)))
        return diagnostics, objective

    before_pairs, before_objective = pairs(active)
    fallback_steps = []
    if cfg['geometry_guard']:
        # Progress only from automatic to offset/native, then offset to native.
        # Every change reduces global pair violation. No range is fabricated.
        for _ in range(2*len(active)):
            _, current = pairs(active)
            best = None
            for label in sorted(active):
                row = active[label]
                source = row['diag']['source']
                choices = []
                if source == 'automatic':
                    choices.append(('native_offset_fallback', row['fallback']))
                if source in {'automatic', 'native_offset_fallback'}:
                    choices.append(('native_unshifted_fallback', row['native']))
                for candidate_source, fallback in choices:
                    if fallback is None or fallback < row['diag']['vertical_minimum_m']:
                        continue
                    candidate = {k: dict(v) for k, v in active.items()}
                    candidate[label]['value'] = fallback
                    _, cost = pairs(candidate)
                    improvement = current-cost
                    if improvement > 1e-9 and (best is None or improvement > best[0]):
                        best = improvement, label, cost, fallback, candidate_source
            if best is None:
                break
            improvement, label, cost, fallback, source = best
            row = active[label]
            row['value'] = fallback
            row['diag'].update(used_m=row['value'], source=source)
            row['diag']['reasons'].append('fallback_reduces_global_triangle_violation' if source=='native_offset_fallback'
                                         else 'original_native_reduces_global_triangle_violation')
            fallback_steps.append(dict(id=label, objective_before=current, objective_after=cost,
                                       improvement=improvement, source=source, used_m=fallback))
    final_pairs, final_objective = pairs(active)
    result.update(pair_diagnostics_before_guard=before_pairs, pair_diagnostics=final_pairs,
                  pair_objective_before=before_objective, pair_objective_after=final_objective,
                  geometry_fallback_steps=fallback_steps)
    for label, row in active.items():
        violation = max((p['violation_m'] for p in final_pairs if label in (p['a'], p['b'])), default=0.)
        geometry_weight = 1/(1+(violation/cfg['triangle_tolerance_m'])**2) if cfg['geometry_guard'] else 1.
        row['diag'].update(max_triangle_violation_m=violation, geometry_weight=geometry_weight,
                           weight=row['diag']['age_weight']*geometry_weight)
        if cfg['geometry_guard'] and violation > 0:
            row['diag']['reasons'].append('unresolved_triangle_violation_downweighted')
        result['ranges_used'][label] = row['value']
        result['weights'][label] = row['diag']['weight']
    used = sorted(active)
    result.update(anchors_n=len(used), anchors_used=used)
    if used:
        result['valid_until_monotonic_s'] = min(active[k]['time']+cap for k in used)
    if len(used) < 3:
        result['reason'] = 'insufficient_valid_anchors'
        return result
    xyz = np.asarray([geometry[k] for k in used])
    centre = xyz[:, :2].mean(axis=0)
    xy = xyz[:, :2]-centre
    singular = np.linalg.svd(xy, compute_uv=False)
    if singular[0] == 0 or singular[1] <= singular[0]*1e-10:
        result['reason'] = 'rank_deficient_anchor_geometry'
        return result
    result['condition_number'] = float(singular[0]/singular[1])
    measured = np.asarray([active[k]['value'] for k in used])
    weights = np.asarray([result['weights'][k] for k in used])
    dz = z-xyz[:, 2]

    def residual(point):
        return np.sqrt(np.sum((point-xy)**2, axis=1)+dz*dz)-measured

    def jacobian(point):
        difference = point-xy
        length = np.sqrt(np.sum(difference*difference, axis=1)+dz*dz)
        return np.divide(difference, length[:, None], out=np.zeros_like(difference), where=length[:, None] > 0)

    def loss(squared):
        # SciPy supplies r^2/f_scale^2. Weights multiply all three derivatives,
        # keeping the Huber transition at 1m regardless of observation age.
        if not cfg['robust']:
            return np.asarray([weights*squared, weights, np.zeros_like(weights)])
        safe = np.maximum(squared, 1e-30)
        root = np.sqrt(safe)
        return np.asarray([weights*np.where(squared <= 1, squared, 2*root-1),
            weights*np.where(squared <= 1, 1., 1/root),
            weights*np.where(squared <= 1, 0., -.5/(safe*root))])

    squared = np.sum(xy*xy, axis=1)+dz*dz-measured*measured
    linear, *_ = np.linalg.lstsq(2*xy, squared-squared.mean(), rcond=None)
    best = None
    for seed in [np.zeros(2), linear, *xy]:
        try:
            fit = least_squares(residual, seed, jac=jacobian, loss=loss,
                f_scale=cfg['huber_scale_m'], method='trf', ftol=1e-10, xtol=1e-10,
                gtol=1e-10, max_nfev=200)
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            continue
        if fit.success and np.isfinite(fit.x).all() and math.isfinite(float(fit.cost)):
            if best is None or fit.cost < best.cost:
                best = fit
    if best is None:
        result['reason'] = 'optimization_failed'
        return result
    point = best.x+centre
    physical = residual(best.x)
    jac_singular = np.linalg.svd(np.sqrt(weights)[:, None]*jacobian(best.x), compute_uv=False)
    times = [active[k]['time'] for k in used]
    result.update(ok=True, reason='ok', x=float(point[0]), y=float(point[1]),
        residual_rms_m=float(np.sqrt(np.mean(physical*physical))),
        weighted_residual_rms_m=float(np.sqrt(np.sum(weights*physical*physical)/weights.sum())),
        robust_objective=2*float(best.cost), age_s=now-min(times),
        measurement_span_s=max(times)-min(times),
        jacobian_condition_number=float(jac_singular[0]/jac_singular[1]) if jac_singular[1] > 1e-15 else None,
        range_residuals_m={k:float(v) for k,v in zip(used, physical)})
    return result
