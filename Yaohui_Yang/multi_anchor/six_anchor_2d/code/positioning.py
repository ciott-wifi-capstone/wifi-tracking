"""Range-only 2D inference. This module accepts no ground-truth coordinates.

Ranges are slant distances; tag height is fixed from the installation config.
The EKF handles sequential measurements at their individual RF midpoint times.
"""
import hashlib
import json
import math
from pathlib import Path
import numpy as np

METHODS = ('raw_ftm', 'ftm_affine', 'cir_direct', 'cir_residual')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def geometry_id(config):
    return fingerprint({'anchors': config['anchors'], 'tag_height_m': config['tag_height_m']})


def model_hashes(directory):
    return {k: hashlib.sha256((Path(directory)/(k+'.json')).read_bytes()).hexdigest()
            for k in METHODS if k != 'raw_ftm'}


def model_training_points(directory):
    path = Path(directory)/'fit_info.json'
    if not path.exists(): return []
    info = json.loads(path.read_text(encoding='utf-8-sig'))
    if info['model_hashes'] != model_hashes(directory):
        raise ValueError('Refitted model manifest/hash mismatch.')
    return info.get('training_points', [])


def load_config(path):
    c = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if c.get('coordinate_units') != 'm':
        raise ValueError('Survey coordinates must be in metres; pixel coordinates are not ground truth.')
    if set(c['anchors']) != {f'A{i}' for i in range(1, 7)}:
        raise ValueError('anchors.json must contain A1 through A6 exactly.')
    for a in c['anchors'].values():
        if not all(math.isfinite(float(a[k])) for k in ('x', 'y', 'z')):
            raise ValueError('Anchor coordinates must be finite metres.')
    if not math.isfinite(float(c['tag_height_m'])) or not 3 <= c['min_anchors'] <= 6:
        raise ValueError('Invalid tag height or minimum anchor count.')
    xy = np.array([[a['x'], a['y']] for a in c['anchors'].values()])
    if np.linalg.matrix_rank(xy-xy.mean(axis=0)) < 2:
        raise ValueError('Anchors are collinear.')
    return c


def load_calibration(path, config, hashes, method):
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data['geometry_sha256'] != geometry_id(config):
        raise ValueError('Calibration geometry/tag-height mismatch. Recalibrate after changing installation.')
    if data['model_hashes'] != hashes:
        raise ValueError('Calibration model mismatch.')
    if method not in data['methods']:
        raise ValueError('Selected range method is absent from calibration.')
    return data


def select_range(row, method, config, calibration=None):
    """Explicit selection: missing CIR never silently becomes raw FTM."""
    value = row.get(method+'_m')
    quality = 'RAW_FTM' if method == 'raw_ftm' else row.get(method+'_quality', 'MISSING')
    aid = row['anchor_id']
    out = dict(method=method, measured_m=value, corrected_m=None, sigma_m=None,
               range_quality=quality, range_usable=False, exclusion='')
    if row.get('cir_residual_quality') == 'SOURCE_MISMATCH':
        out['exclusion'] = 'SOURCE_MISMATCH'
        return out
    if row.get('ftm_status') != 0 or value is None or not math.isfinite(float(value)) or value <= 0:
        out['exclusion'] = 'FAILED_OR_NONPOSITIVE'
        return out
    if quality not in ('RAW_FTM', 'EXPERIMENTAL', 'OUT_OF_DOMAIN'):
        out['exclusion'] = quality
        return out
    if quality == 'OUT_OF_DOMAIN' and not config.get('allow_ood', False):
        out['exclusion'] = 'OOD_REJECTED'
        return out
    sigma = config.get('unverified_range_sigma_m', 3.)
    offset = 0.
    if calibration is not None:
        if row.get('firmware_profile') != calibration.get('firmware_profile'):
            out['exclusion'] = 'CALIBRATION_PROFILE_MISMATCH'
            return out
        entry = calibration['methods'].get(method, {}).get(aid)
        if not entry:
            out['exclusion'] = 'ANCHOR_NOT_CALIBRATED'
            return out
        if entry['bssid'] != row.get('bssid', '').lower():
            out['exclusion'] = 'CALIBRATION_BSSID_MISMATCH'
            return out
        offset = entry['bias_m']
        sigma = entry['sigma_m']
        out['calibration_support_m'] = entry['true_range_span_m']
        # Extrapolation is flagged against the estimated corrected range, not truth.
        lo, hi = entry['true_range_span_m']
        if value-offset < lo-1 or value-offset > hi+1:
            out['calibration_extrapolation'] = True
            sigma = max(sigma, config.get('ood_sigma_m', 8.))
    if quality == 'OUT_OF_DOMAIN':
        sigma = max(sigma, config.get('ood_sigma_m', 8.))
    corrected = float(value)-offset
    if corrected <= 0:
        out['exclusion'] = 'CORRECTION_NONPOSITIVE'
        return out
    out.update(corrected_m=corrected, sigma_m=float(max(.3, sigma)), range_usable=True)
    return out


def solve_wls(observations, config):
    """Robust nonlinear slant-range least squares; no map bounds or truth snapping."""
    n = len(observations)
    if n < config['min_anchors']:
        return dict(valid=False, reason='INSUFFICIENT_ANCHORS', anchors=n)
    xyz = np.array([[config['anchors'][o['anchor_id']][k] for k in ('x', 'y', 'z')]
                    for o in observations], float)
    r = np.array([o['corrected_m'] for o in observations], float)
    sig = np.array([o['sigma_m'] for o in observations], float)
    dz2 = (xyz[:, 2]-config['tag_height_m'])**2
    # Subtract squared equations only for the initial estimate.
    A = 2*(xyz[1:, :2]-xyz[0, :2])
    b = np.sum(xyz[1:, :2]**2, axis=1)-np.sum(xyz[0, :2]**2)+dz2[1:]-dz2[0]-(r[1:]**2-r[0]**2)
    seed = np.linalg.lstsq(A, b, rcond=None)[0]
    center = xyz[:, :2].mean(axis=0)
    candidates = []
    for initial in (seed, center, center+np.array([0., 5.]), center-np.array([0., 5.])):
        x = initial.copy()
        damping = 1e-4
        def residual(v):
            return (np.sqrt(np.sum((v-xyz[:, :2])**2, axis=1)+dz2)-r)/sig
        def cost(v):
            e = abs(residual(v)); return float(np.sum(np.where(e <= 2., .5*e**2, 2.*e-2.)))
        for _ in range(60):
            delta = x-xyz[:, :2]
            distances = np.maximum(1e-6, np.sqrt(np.sum(delta**2, axis=1)+dz2))
            e = (distances-r)/sig
            J = delta/distances[:, None]/sig[:, None]
            weights = np.minimum(1., 2./np.maximum(abs(e), 1e-12))
            normal = J.T@(weights[:, None]*J)
            step = np.linalg.solve(normal+np.eye(2)*damping, J.T@(weights*e))
            if cost(x-step) <= cost(x):
                x -= step; damping = max(1e-8, damping/2.)
                if np.linalg.norm(step) < 1e-6: break
            else:
                damping *= 8.
        candidates.append((cost(x), x))
    _, x = min(candidates, key=lambda t: t[0])
    d = np.maximum(1e-6, np.sqrt(np.sum((x-xyz[:, :2])**2, axis=1)+dz2))
    J = (x-xyz[:, :2])/d[:, None]/sig[:, None]
    cond = float(np.linalg.cond(J.T@J))
    residual = d-r
    normalized_rms = float(np.sqrt(np.mean((residual/sig)**2)))
    valid = bool(np.isfinite(x).all() and math.isfinite(cond)
                 and cond <= config['max_geometry_condition'] and normalized_rms < 4.)
    reason = 'OK' if valid else ('POOR_GEOMETRY' if cond > config['max_geometry_condition'] else 'INCONSISTENT_RANGES')
    return dict(valid=valid, reason=reason, anchors=n, x=float(x[0]), y=float(x[1]),
                residual_rmse_m=float(np.sqrt(np.mean(residual**2))), condition=cond if math.isfinite(cond) else None,
                covariance=np.linalg.pinv(J.T@J).tolist())


class RangeTracker:
    """Constant-velocity EKF with asynchronous individual range updates."""
    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        self.state, self.P, self.time_s = None, None, None
        self.history = {}

    def update(self, row):
        t = row['rf_time_s']
        if not math.isfinite(t): return None
        if self.time_s is not None and (t < self.time_s or t-self.time_s > self.config['filter_reset_gap_s']):
            self.reset()
        self.history[row['anchor_id']] = row
        fresh = [o for o in self.history.values() if 0 <= t-o['rf_time_s'] <= self.config['max_seed_age_s']]
        if self.state is not None and len(fresh) < self.config['min_anchors']:
            self.reset()
            self.history[row['anchor_id']] = row
            return None
        if self.state is None:
            recent = [dict(o, sigma_m=math.hypot(o['sigma_m'], max(0., t-o['rf_time_s'])*.8))
                      for o in self.history.values() if 0 <= t-o['rf_time_s'] <= self.config['max_seed_age_s']]
            initial = solve_wls(recent, self.config)
            if not initial['valid']: return None
            self.state = np.array([initial['x'], initial['y'], 0., 0.])
            self.P = np.diag([9., 9., 4., 4.])
            self.P[:2, :2] += np.array(initial['covariance'])
            self.time_s = t
            return self.output('INITIALIZED', True, t)
        dt = t-self.time_s
        F = np.eye(4); F[0, 2] = F[1, 3] = dt
        q = self.config['acceleration_sigma_m_s2']**2
        Q = np.array([[dt**3/3, 0, dt**2/2, 0], [0, dt**3/3, 0, dt**2/2],
                      [dt**2/2, 0, dt, 0], [0, dt**2/2, 0, dt]])*q
        self.state = F@self.state; self.P = F@self.P@F.T+Q
        self.time_s = t
        a = self.config['anchors'][row['anchor_id']]
        delta = self.state[:2]-np.array([a['x'], a['y']])
        expected = max(1e-6, math.sqrt(float(delta@delta)+(a['z']-self.config['tag_height_m'])**2))
        H = np.array([delta[0]/expected, delta[1]/expected, 0., 0.])
        R = row['sigma_m']**2
        S = float(H@self.P@H+R)
        innovation = row['corrected_m']-expected
        accepted = abs(innovation) <= self.config['innovation_gate_sigma']*math.sqrt(S)
        if accepted:
            gain = self.P@H/S
            self.state += gain*innovation
            I_KH = np.eye(4)-np.outer(gain, H)
            self.P = I_KH@self.P@I_KH.T+np.outer(gain, gain)*R
        return self.output('UPDATED' if accepted else 'RANGE_REJECTED', accepted, t)

    def output(self, reason, accepted, t):
        return dict(kind='EKF', valid=True, reason=reason, range_accepted=accepted,
                    rf_time_s=t, x=float(self.state[0]), y=float(self.state[1]),
                    vx=float(self.state[2]), vy=float(self.state[3]),
                    position_sigma_m=float(math.sqrt(max(0., np.trace(self.P[:2, :2])))))
