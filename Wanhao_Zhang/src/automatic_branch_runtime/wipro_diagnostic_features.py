"""Event-local diagnostics for the pinned Wi-PRO ESP32 implementation.

No truth, identity, location, LOS label or future sample enters this extractor.
The 25% crossing is checked against the recorded native output *only as an
implementation audit*. Native disagreement makes all CIR diagnostics missing;
it never substitutes a truth-derived estimate. Offline diagnostics are not a
claim of physical multipath excess delay or a calibrated LOS probability.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import time

import numpy as np

ACTIVE_40 = np.asarray(list(range(2, 59)) + list(range(70, 127)), dtype=int)
THRESHOLDS = (0.15, 0.25, 0.35)
SPECTRAL_FEATURES = ["spectral_roughness", "spectral_deepfade_fraction",
                     "spectral_entropy", "spectral_halfband_imbalance"]
CIR_FEATURES = ["cir_threshold_span_m", "cir_threshold_low_delta_m",
                "cir_threshold_high_delta_m", "cir_front_peak_gap_m",
                "cir_front_relative_amplitude", "cir_prefront_energy_fraction",
                "cir_peak_energy_fraction"]
VALIDITY_FEATURES = ["diagnostic_csi_missing", "diagnostic_cir_missing"]
ASSET_DIR = Path(__file__).with_name("wipro_diagnostic_assets")
PINNED_COMMIT = "288311fd40cfdf629780ebc3604bc52671feec69"
ASSET_HASHES = {
    "idft_L.bin": "c4f5adc962ac351f9c53010153aafc4853201d8d7ebdb116184d80e98362de7c",
    "idft_R.bin": "f2b00095b5c504fe796e2378db28d94b09d1db1bcface2818b0a9ad18a7a54ce",
    "idft_drange.bin": "1a346c229eab8f91af1f10e907bb5efc2e6ade368b3bd82fcfe138b0b0374dac",
    "idft_meta.bin": "bee72f9c23088d30c32e50a7182c4c9dc808fdec06c5dbae6788d54b3f00c95d",
    "idft_sinc_rise_comp.bin": "0aa64674577f50ca1bda9b66d5285e66b81184f501cdb114f6293e46da72de60",
}
ASSET_PROVENANCE = {
    "pinned_commit": PINNED_COMMIT,
    "copied_from_actual_flashed_build_tree": "/root/wipro/pure-a1-20261015-248870/firmware/data",
    "assets_regenerated": False,
    "firmware_wipro_rs_sha256": "b1b0ac35a90a1366071e9180da70d340ebd4fd8cb31be4f8663ed9926652cb5c",
    "create_idft_mat_py_sha256": "e6d17c444dd9016d679e92868b5d2a3159d93d9a070c2ab4f12e0d59ea1b9e72",
    "asset_sha256": ASSET_HASHES,
}
FEATURE_SPEC = {
    "version": "wipro_diagnostics_v1",
    "normalization": "Firmware uses all 128 signed int8 HT40 carrier magnitudes before phase correction; spectral features use only 114 observed carriers.",
    "HT40_bytes": "CSI length must be exactly 384; bytes128..383 are signed int8 [imag,real].",
    "halfband_phase_correction": "Multiply carriers64..127 by +j.",
    "interpolation": "Three accepted l1 steps; initial eps0.005; accepted eps*1.1; failed eps*0.5; fail>10 stops; measured114 carriers gradient zeroed.",
    "thresholds_declared_before_benchmark": THRESHOLDS,
    "native_reconstruction_tolerance_m": 0.0001,
    "float_precision": "complex64/f32 channel and assets; numpy FFT evaluated then rounded complex64; native DSP versus FFT/BLAS may differ numerically. Per-event d25 agreement gates CIR features.",
    "spectral_roughness": "mean absolute difference between mean-normalized adjacent amplitudes within each observed halfband; no gap bridged",
    "spectral_deepfade_fraction": "fraction observed amplitudes <0.25 of observed-carrier mean",
    "spectral_entropy": "Shannon entropy of observed amplitude-squared normalized energy, divided by log114",
    "spectral_halfband_imbalance": "absolute difference of mean halfband amplitudes divided by overall observed amplitude mean",
    "cir_threshold_span_m": "d35 minus d15, using same pinned 25%-calibrated sinc compensation for all thresholds; this common term cancels",
    "cir_threshold_low_delta_m": "d25 minus d15",
    "cir_threshold_high_delta_m": "d35 minus d25",
    "cir_front_peak_gap_m": "grid distance at CIR maximum minus distance at first >25% maximum",
    "cir_front_relative_amplitude": "amplitude at first >25% crossing divided by maximum amplitude",
    "cir_prefront_energy_fraction": "energy before first >25% crossing divided by all512 gridpoint energies",
    "cir_peak_energy_fraction": "largest gridpoint energy divided by all512 gridpoint energies",
    "invalid_policy": "Keep event; NaN diagnostic values and explicit missing flags; train-fold-only imputation upstream.",
    "not_physical_claim": "CIR grid is a finite compressed inverse transform; gaps, threshold sensitivity and energy fractions are diagnostics, not resolved physical multipath delay estimates.",
}


def load_assets():
    """Load exact generated binaries from the actual pinned firmware build."""
    for name, expected in ASSET_HASHES.items():
        if hashlib.sha256((ASSET_DIR / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Changed pinned IDFT asset: {name}")
    n, carriers, rank = np.fromfile(ASSET_DIR / "idft_meta.bin", dtype="<u4")
    if (int(n), int(carriers), int(rank)) != (512, 128, 12):
        raise ValueError("Unexpected compressed IDFT dimensions")
    left = np.fromfile(ASSET_DIR / "idft_L.bin", dtype="<c8").reshape(int(n), int(rank))
    right = np.fromfile(ASSET_DIR / "idft_R.bin", dtype="<c8").reshape(int(rank), 128)
    distance = np.fromfile(ASSET_DIR / "idft_drange.bin", dtype="<f4")
    compensation = np.fromfile(ASSET_DIR / "idft_sinc_rise_comp.bin", dtype="<f4")[0]
    return left, right, distance, compensation


def decode_ht40(payload_b64):
    """Strict firmware [imag,real] signed int8 decoding before normalization."""
    data = base64.b64decode(payload_b64 or "", validate=True)
    if len(data) != 384:
        raise ValueError("Expected exact 384-byte HT40 CSI")
    iq = np.frombuffer(data[128:], dtype=np.int8).astype(np.float32)
    h = np.empty(128, dtype=np.complex64)
    h.real, h.imag = iq[1::2], iq[0::2]
    if not np.isfinite(h).all() or not np.any(np.abs(h[ACTIVE_40]) > 0):
        raise ValueError("Empty observed HT40 signal")
    return h


def spectral_features(h):
    amp = np.abs(h[ACTIVE_40]).astype(np.float64)
    mean = float(amp.mean())
    if not np.isfinite(mean) or mean <= 0:
        raise ValueError("Invalid amplitude mean")
    normalized = amp / mean
    adjacent = np.concatenate([np.diff(normalized[:57]), np.diff(normalized[57:])])
    energy = amp * amp
    probability = energy / energy.sum()
    nonzero = probability > 0
    return {
        "spectral_roughness": float(np.abs(adjacent).mean()),
        "spectral_deepfade_fraction": float(np.mean(normalized < 0.25)),
        "spectral_entropy": float(-np.sum(probability[nonzero] * np.log(probability[nonzero])) / np.log(114)),
        "spectral_halfband_imbalance": float(abs(amp[:57].mean() - amp[57:].mean()) / mean),
    }


def interpolate_batch(channels):
    """Faithful f32 state machine, vectorized over independent CSI packets."""
    h = np.asarray(channels, dtype=np.complex64).copy()
    sums = np.cumsum(np.abs(h), axis=1, dtype=np.float32)[:, -1]
    if np.any(sums <= 0):
        raise ValueError("Zero channel magnitude")
    h /= sums[:, None]
    h[:, 64:] *= np.complex64(1j)
    n = len(h)
    eps = np.full(n, 0.005, dtype=np.float32)
    count = np.zeros(n, dtype=int)
    fail = np.zeros(n, dtype=int)
    loss = np.full(n, np.inf, dtype=np.float32)
    while True:
        ix = np.flatnonzero((count < 3) & (fail <= 10))
        if not len(ix):
            break
        current = h[ix]
        inverse = np.fft.ifft(current, axis=1, norm="ortho").astype(np.complex64)
        inverse /= (np.abs(inverse) + np.float32(1e-9)).astype(np.float32)
        gradient = np.fft.fft(inverse, axis=1, norm="ortho").astype(np.complex64)
        gradient[:, ACTIVE_40] = 0
        trial = current - gradient * eps[ix, None]
        trial_ifft = np.fft.ifft(trial, axis=1, norm="ortho").astype(np.complex64)
        trial_loss = np.cumsum(np.abs(trial_ifft), axis=1, dtype=np.float32)[:, -1]
        accepted = trial_loss < loss[ix]
        yes, no = ix[accepted], ix[~accepted]
        h[yes] = trial[accepted]
        loss[yes] = trial_loss[accepted]
        count[yes] += 1
        fail[yes] = 0
        eps[yes] *= np.float32(1.1)
        fail[no] += 1
        eps[no] *= np.float32(0.5)
    return h


def reconstruct_batch(channels, raw_m, assets=None):
    """Return CIR amplitude and exact three crossing distances for each event."""
    left, right, distance, compensation = assets or load_assets()
    h = interpolate_batch(channels)
    # raw_m=(rtt_ps*299792458e-12/2), hence delay_s=raw_m/c exactly.
    frequencies = np.fft.fftfreq(128, d=1 / 40e6)
    phase = -2 * np.pi * np.asarray(raw_m, dtype=np.float64)[:, None] / 299792458 * frequencies[None, :]
    rotation = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)
    h *= rotation
    cir = (h @ right.T) @ left.T
    amp = np.abs(cir).astype(np.float32)
    maxima = amp.max(axis=1)
    relative = amp / maxima[:, None]
    indices = np.column_stack([np.argmax(relative > np.float32(t), axis=1) for t in THRESHOLDS])
    ranges = distance[indices] + compensation
    return amp, indices, ranges


def extract_batch_diagnostic_features(payloads_b64, raw_m, wipro_m=None, *, batch_size=512):
    """Return (feature dictionaries, audit); no reference values accepted.

    Recorded native values, if supplied, gate CIR reconstruction. If omitted,
    CIR diagnostics stay missing because agreement has not been checked.
    """
    started = time.perf_counter()
    if len(payloads_b64) != len(raw_m) or (wipro_m is not None and len(wipro_m) != len(raw_m)):
        raise ValueError("Batch lengths differ")
    empty = {name: float("nan") for name in SPECTRAL_FEATURES + CIR_FEATURES}
    rows = [{**empty, "diagnostic_csi_missing": 1., "diagnostic_cir_missing": 1.} for _ in raw_m]
    channels, indices = [], []
    for i, payload in enumerate(payloads_b64):
        try:
            h = decode_ht40(payload)
            if not np.isfinite(float(raw_m[i])):
                raise ValueError("Nonfinite raw range")
            rows[i].update(spectral_features(h), diagnostic_csi_missing=0.)
            channels.append(h)
            indices.append(i)
        except (TypeError, ValueError):
            continue
    assets = load_assets()
    native_errors, native_mismatch_indices = [], []
    reconstructed = []
    for start in range(0, len(channels), batch_size):
        sample_ix = indices[start:start + batch_size]
        amp, crosses, ranges = reconstruct_batch(np.asarray(channels[start:start + batch_size]),
                                                 [raw_m[i] for i in sample_ix], assets)
        distance = assets[2]
        for j, i in enumerate(sample_ix):
            d15, d25, d35 = map(float, ranges[j])
            reconstructed.append({"event_index": i, "native_reconstruction_m": d25})
            if wipro_m is None:
                continue
            error = abs(d25 - float(wipro_m[i]))
            native_errors.append(error)
            if not np.isfinite(error) or error > FEATURE_SPEC["native_reconstruction_tolerance_m"]:
                native_mismatch_indices.append(i)
                continue
            first = int(crosses[j, 1])
            peak = int(np.argmax(amp[j]))
            e = amp[j].astype(np.float64) ** 2
            total = float(e.sum())
            rows[i].update({
                "cir_threshold_span_m": d35 - d15,
                "cir_threshold_low_delta_m": d25 - d15,
                "cir_threshold_high_delta_m": d35 - d25,
                "cir_front_peak_gap_m": float(distance[peak] - distance[first]),
                "cir_front_relative_amplitude": float(amp[j, first] / amp[j, peak]),
                "cir_prefront_energy_fraction": float(e[:first].sum() / total),
                "cir_peak_energy_fraction": float(e[peak] / total),
                "diagnostic_cir_missing": 0.,
            })
    finite_errors = np.asarray([v for v in native_errors if np.isfinite(v)])
    audit = {
        "events_n": len(rows), "valid_csi_n": len(channels),
        "valid_cir_n": sum(r["diagnostic_cir_missing"] == 0 for r in rows),
        "native_validation_attempted_n": len(native_errors),
        "native_reconstruction_mismatch_n": len(native_mismatch_indices),
        "native_reconstruction_mismatch_event_indices": native_mismatch_indices,
        "native_reconstruction_abs_error_max_m": float(finite_errors.max()) if len(finite_errors) else None,
        "native_reconstruction_abs_error_median_m": float(np.median(finite_errors)) if len(finite_errors) else None,
        "native_reconstruction_tolerance_m": FEATURE_SPEC["native_reconstruction_tolerance_m"],
        "native_reconstruction": reconstructed,
        "runtime_s": time.perf_counter() - started,
        "asset_provenance": ASSET_PROVENANCE,
    }
    return rows, audit


def extract_diagnostic_features(payload_b64, raw_m, wipro_m=None):
    return extract_batch_diagnostic_features([payload_b64], [raw_m],
                                           [wipro_m] if wipro_m is not None else None)[0][0]


if __name__ == "__main__":
    print(json.dumps({"features": FEATURE_SPEC, "assets": ASSET_PROVENANCE}, indent=2))
