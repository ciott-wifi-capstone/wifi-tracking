"""20 MHz LLTF CIR features and portable regularized distance regression.

The frequency-domain timing reference is unknown. All CIR timing features are
relative to the dominant peak; no IFFT index is interpreted as absolute range.
The algorithms here are implemented independently for the existing V5 stream.
"""
import json
from pathlib import Path
import numpy as np

SCHEMA = 'qar_cir20_v1'
PS_TO_M = 0.000149896229
FREQ_INDEX = np.fft.fftfreq(64) * 64
ACTIVE = (np.abs(FREQ_INDEX) >= 1) & (np.abs(FREQ_INDEX) <= 26)
NATIVE_DT_NS = 50.0
OVERSAMPLE = 16
RECON_ITERS = 12
CIR_FEATURES = [
    'cir_peak_fraction', 'cir_early_fraction', 'cir_tail_fraction',
    'cir_relative_rms_ns', 'cir_entropy', 'cir_front_excess_ns',
    'cir_spectral_flatness', 'cir_amplitude_cv',
]
FTM_FEATURES = ['ftm_distance_m', 'ftm_frame_median_m', 'ftm_mad_m', 'ftm_rssi_dbm']


class UnsupportedCSI(ValueError):
    pass


def decode_lltf20(hex_text, *, cwb, sig_mode, invalid_first=0, secondary_channel=0):
    if int(cwb) != 0 or int(sig_mode) != 0 or int(secondary_channel) != 0:
        raise UnsupportedCSI('This model requires non-HT, 20 MHz LLTF CSI.')
    try:
        raw = bytes.fromhex(hex_text)
    except ValueError as exc:
        raise UnsupportedCSI('Invalid CSI hex.') from exc
    if len(raw) != 128:
        raise UnsupportedCSI('Expected exactly 128 CSI bytes.')
    iq = np.frombuffer(raw, dtype=np.int8).astype(float)
    # Espressif: imaginary byte first, real byte second; bins 0..31,-32..-1.
    h = iq[1::2] + 1j * iq[::2]
    known = ACTIVE.copy()
    if int(invalid_first):
        known[:2] = False
    if np.count_nonzero(np.abs(h[known]) > 0) < 40:
        raise UnsupportedCSI('Insufficient nonzero active subcarriers.')
    scale = float(np.sqrt(np.mean(np.abs(h[known]) ** 2)))
    if not np.isfinite(scale) or scale <= 0:
        raise UnsupportedCSI('Invalid CSI power.')
    h /= scale
    h[~known] = 0
    return h, known


def reconstruct_missing(h, known, iterations=RECON_ITERS):
    """Smooth-L1 minimization over unobserved bins, preserving every known bin.

    Backtracking projected gradient reduces a convex smooth approximation to
    sum(abs(IFFT(H))). The missing bins are inferred, not newly measured.
    """
    out = h.copy()
    if not np.any(~known):
        return out
    epsilon = 0.02
    step = 0.2
    for _ in range(iterations):
        response = np.fft.ifft(out, norm='ortho')
        denominator = np.sqrt(np.abs(response) ** 2 + epsilon ** 2)
        loss = float(np.sum(denominator))
        gradient = np.fft.fft(response / denominator, norm='ortho')
        gradient[known] = 0
        accepted = False
        for _ in range(12):
            candidate = out - step * gradient
            candidate[known] = h[known]
            trial_response = np.fft.ifft(candidate, norm='ortho')
            new_loss = float(np.sum(np.sqrt(np.abs(trial_response) ** 2 + epsilon ** 2)))
            if new_loss <= loss - 1e-10:
                out = candidate
                step = min(step * 1.15, 0.5)
                accepted = True
                break
            step *= 0.5
        if not accepted:
            break
    return out


def dense_cir(h, oversample=OVERSAMPLE):
    """Zero-pad between positive/negative frequency halves, not at the end."""
    count = len(h)
    spectrum = np.zeros(count * oversample, dtype=complex)
    spectrum[:count // 2] = h[:count // 2]
    spectrum[-count // 2:] = h[count // 2:]
    return np.fft.ifft(spectrum) * oversample


def relative_front_ns(response, threshold=0.25):
    """Walk backward from the dominant peak to its local amplitude crossing."""
    magnitude = np.abs(response)
    peak = int(np.argmax(magnitude))
    top = float(magnitude[peak])
    if top <= 0:
        raise UnsupportedCSI('Empty CIR.')
    levels = magnitude[(peak - np.arange(len(response) // 2)) % len(response)] / top
    below = np.flatnonzero(levels <= threshold)
    if not len(below):
        raise UnsupportedCSI('No bounded CIR leading edge.')
    j = int(below[0])
    if j == 0:
        return 0.0
    high, low = float(levels[j - 1]), float(levels[j])
    fraction = (high - threshold) / max(high - low, 1e-12)
    step_ns = NATIVE_DT_NS / (len(response) / 64)
    return -(j - 1 + fraction) * step_ns


IDEAL_FRONT_NS = relative_front_ns(dense_cir(np.ones(64, dtype=complex)))


def extract_frame_features(hex_text, *, cwb, sig_mode, invalid_first=0, secondary_channel=0):
    h, known = decode_lltf20(hex_text, cwb=cwb, sig_mode=sig_mode,
                            invalid_first=invalid_first, secondary_channel=secondary_channel)
    amplitude = np.abs(h[known])
    power = amplitude ** 2
    flatness = float(np.exp(np.mean(np.log(np.maximum(power, 1e-12)))) / np.mean(power))
    cv = float(np.std(amplitude) / np.mean(amplitude))
    recovered = reconstruct_missing(h, known)
    response = dense_cir(recovered)
    p = np.abs(response) ** 2
    p /= float(np.sum(p))
    peak = int(np.argmax(p))
    indices = np.arange(len(p))
    relative = ((indices - peak + len(p) // 2) % len(p) - len(p) // 2)
    relative = relative * (NATIVE_DT_NS / OVERSAMPLE)
    center = float(np.sum(relative * p))
    return {
        'cir_peak_fraction': float(np.sum(p[np.abs(relative) <= 25])),
        'cir_early_fraction': float(np.sum(p[np.abs(relative) <= 50])),
        'cir_tail_fraction': float(np.sum(p[np.abs(relative) > 100])),
        'cir_relative_rms_ns': float(np.sqrt(np.sum((relative - center) ** 2 * p))),
        'cir_entropy': float(-np.sum(p * np.log(np.maximum(p, 1e-15))) / np.log(len(p))),
        'cir_front_excess_ns': float(relative_front_ns(response) - IDEAL_FRONT_NS),
        'cir_spectral_flatness': flatness,
        'cir_amplitude_cv': cv,
    }


def make_round_features(ftm, details, frames, csi_metadata, min_pairs=4):
    """One inference unit is one FTM round; labels are never inspected here."""
    if int(ftm['status']) != 0:
        raise ValueError('FTM session failed.')
    valid_rtt = [int(e['rtt_ps']) for e in details if 0 <= int(e['rtt_ps']) < 0xFFFFFFFF]
    if len(valid_rtt) < min_pairs:
        raise ValueError('Insufficient valid RTT entries.')
    by_token = {}
    for frame in frames:
        by_token.setdefault(int(frame['dialog_token']), []).append(frame)
    counts = {}
    for entry in details:
        token = int(entry['dialog_token'])
        counts[token] = counts.get(token, 0) + 1
    values = []
    used = []
    rejected = 0
    for entry in details:
        if not 0 <= int(entry['rtt_ps']) < 0xFFFFFFFF:
            continue
        token = int(entry['dialog_token'])
        candidates = by_token.get(token, [])
        if counts[token] != 1 or len(candidates) != 1:
            continue
        frame = candidates[0]
        metadata = csi_metadata.get(int(frame['callback_cpu_time_us']))
        # CSI_FTM_FRAME lacks sig_mode and first_word_invalid in V5. Use its
        # matching ordinary CSI record; never guess the missing PHY metadata.
        if metadata is None or metadata.get('csi_hex') != frame.get('csi_hex'):
            rejected += 1
            continue
        try:
            features = extract_frame_features(
                frame['csi_hex'], cwb=frame['cwb'], sig_mode=metadata['sig_mode'],
                invalid_first=metadata['first_word_invalid'], secondary_channel=metadata['secondary_channel'])
        except (UnsupportedCSI, ValueError, KeyError):
            rejected += 1
            continue
        values.append(features)
        used.append(entry)
    if len(values) < min_pairs:
        raise ValueError(f'Insufficient valid same-frame CIR pairs: {len(values)}')
    distance = np.asarray(valid_rtt, dtype=float) * PS_TO_M
    median = float(np.median(distance))
    result = {
        'ftm_distance_m': int(ftm['dist_est_cm']) / 100.0,
        'ftm_frame_median_m': median,
        'ftm_mad_m': float(np.median(np.abs(distance - median))),
        'ftm_rssi_dbm': float(ftm['ftm_mean_rssi_dbm']),
        'paired_cir_frames': len(values), 'rejected_csi_frames': rejected,
        'valid_ftm_frames': len(valid_rtt),
    }
    for name in CIR_FEATURES:
        result[name] = float(np.median([r[name] for r in values]))
    return result


def fit_model(rows, *, kind, alpha=10.0):
    names = {'ftm_affine': ['ftm_distance_m'],
             'cir_direct': CIR_FEATURES,
             'cir_residual': FTM_FEATURES + CIR_FEATURES}[kind]
    if not rows or len({float(r['true_distance_m']) for r in rows}) < 2:
        raise ValueError('Training needs at least two distinct labelled distances.')
    x = np.asarray([[float(r[name]) for name in names] for r in rows])
    truth = np.asarray([float(r['true_distance_m']) for r in rows])
    groups = [r['trial'] for r in rows]
    sizes = {g: groups.count(g) for g in set(groups)}
    # Equal total influence per capture, despite unequal session counts.
    weights = np.asarray([len(rows) / (len(sizes) * sizes[g]) for g in groups])
    center = np.average(x, axis=0, weights=weights)
    scale = np.sqrt(np.average((x - center) ** 2, axis=0, weights=weights))
    scale[scale < 1e-9] = 1.0
    z = (x - center) / scale
    y = truth - np.asarray([r['ftm_distance_m'] for r in rows]) if kind == 'cir_residual' else truth
    intercept = float(np.average(y, weights=weights))
    penalty = 0.0 if kind == 'ftm_affine' else alpha
    coefficient = np.linalg.lstsq(z.T @ (weights[:, None] * z) + penalty * np.eye(len(names)),
                                  z.T @ (weights * (y - intercept)), rcond=None)[0]
    return {
        'schema': SCHEMA, 'kind': kind, 'feature_names': names,
        'center': center.tolist(), 'scale': scale.tolist(),
        'coefficient': coefficient.tolist(), 'intercept': intercept,
        'alpha': penalty, 'training_trials': sorted(sizes),
        'training_rows': len(rows), 'training_groups': len(sizes),
        'training_label_mean_m': float(np.average(truth, weights=weights)),
        'training_distance_min_m': float(np.min(truth)),
        'training_distance_max_m': float(np.max(truth)),
        'feature_min': np.min(x, axis=0).tolist(), 'feature_max': np.max(x, axis=0).tolist(),
        'status': 'EXPERIMENTAL_SMALL_DATA',
        'preprocessing': {'bandwidth_mhz': 20, 'ltf': 'nonHT_LLTF', 'csi_bytes': 128,
                          'oversample': OVERSAMPLE, 'smooth_l1_iterations': RECON_ITERS},
    }


def predict(model, row):
    if model.get('schema') != SCHEMA:
        raise ValueError('Model preprocessing schema mismatch.')
    x = np.asarray([float(row[n]) for n in model['feature_names']])
    if not np.all(np.isfinite(x)):
        raise ValueError('Nonfinite inference features.')
    z = (x - np.asarray(model['center'])) / np.asarray(model['scale'])
    fitted = float(model['intercept'] + z @ np.asarray(model['coefficient']))
    if model['kind'] == 'cir_residual':
        fitted += float(row['ftm_distance_m'])
    # Statistical domain check only; it is not a calibrated probability.
    deviation = float(np.max(np.abs(z)))
    outside = deviation > 4.0
    quality = 'OUT_OF_DOMAIN' if outside else 'EXPERIMENTAL'
    if fitted < 0:
        quality = 'INVALID_NEGATIVE_PREDICTION'
    return {'distance_m': fitted, 'quality': quality, 'max_standardized_feature': deviation,
            'distance_for_use_m': fitted if quality == 'EXPERIMENTAL' else None}


def save_model(path, model):
    Path(path).write_text(json.dumps(model, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                          encoding='utf-8')


def load_model(path):
    model = json.loads(Path(path).read_text(encoding='utf-8'))
    if model.get('schema') != SCHEMA:
        raise ValueError('Unsupported model file.')
    return model
