"""ESP32-S3 LLTF 20 MHz channel responses -> coarse CIR descriptors.

Wire format (CSV, base64 avoids commas in CSI payload):
CSIRAW,1,round_id,esp_us,anchor_id,source_mac,channel,20,LLTF,first_word_invalid,base64_bytes

For Wi-Fi LLTF 20 MHz, 128 signed bytes = 64 (imag,real) pairs.
Espressif-reported order is k=0..31 then k=-32..-1, i.e. FFT bins 0..63.
Do not interpret peak index as absolute ToF. IFFT bin interval is 50 ns (20 MHz).
"""
from __future__ import annotations

import base64
import binascii
import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CIRPacket:
    round_id: int
    esp_us: int
    anchor_id: str
    source_mac: str
    channel: int
    bandwidth_mhz: int
    ltf: str
    first_word_invalid: bool
    data: bytes


def parse_csiraw(line: str) -> CIRPacket | None:
    p = line.strip().split(',')
    if len(p) != 11 or p[0] != 'CSIRAW' or p[1] != '1':
        return None
    try:
        b = base64.b64decode(p[10], validate=True)
        if len(b) != 128 or p[8].upper() != 'LLTF' or int(p[7]) != 20:
            return None  # intentionally reject unsupported CSI packet formats
        aid = str(int(p[4]))
        return CIRPacket(int(p[2]), int(p[3]), aid, p[5].strip().lower(),
                         int(p[6]), int(p[7]), 'LLTF', bool(int(p[9])), b)
    except (ValueError, IndexError, TypeError, binascii.Error):
        return None


def packet_to_cir(packet: CIRPacket) -> np.ndarray:
    """64-point IFFT. Zero nonactive carriers and invalid leading samples.

    This is a relative CIR *shape*, not calibrated impulse arrival time.
    """
    b = np.frombuffer(packet.data, dtype=np.int8).astype(np.float64)
    h = b[1::2] + 1j * b[0::2]  # Imag then Real (confirmed by Espressif)
    if packet.first_word_invalid:
        h[:2] = 0.0  # 4 bytes = two complex coefficients
    # LLTF OFDM occupied tones +/-1..26; 0 DC and +/-27..32 guards unused.
    valid = np.zeros(64, dtype=bool)
    valid[1:27] = True
    valid[64-26:64] = True
    h[~valid] = 0.0
    total = float(np.sum(np.abs(h)**2))
    if total <= 1e-12:
        raise ValueError('All active CSI tones are zero')
    return np.fft.ifft(h, n=64)


def cir_features(packet: CIRPacket) -> dict[str, float]:
    cir = packet_to_cir(packet)
    power = np.abs(cir)**2
    power = power / max(float(power.sum()), 1e-12)
    peak = int(np.argmax(power))
    # Signed circular differences are invariant under an unknown global time shift.
    indices = np.arange(64)
    delays = (indices - peak + 32) % 64 - 32
    center = float(np.sum(delays * power))
    rms_bins = math.sqrt(max(0.0, float(np.sum((delays-center)**2 * power))))
    # 64 bins of a 20 MHz full-band IFFT => bin spacing = 50 ns.
    entropy = -float(np.sum(power * np.log(power + 1e-15))) / math.log(64)
    return {
        'cir_peak_ratio': float(np.max(power)),
        'cir_rms_delay_ns': float(rms_bins * 50.0),
        'cir_offpeak_ratio': float(np.sum(power[np.abs(delays) > 1])),
        'cir_entropy': entropy,
    }


class CIRBuffer:
    def __init__(self, max_packets_per_link: int = 32, tolerance_ms: int = 650):
        self.max_packets = int(max_packets_per_link)
        self.tolerance_us = int(tolerance_ms) * 1000
        self.by_round: dict[int, dict[str, list[tuple[CIRPacket, dict[str, float]]]]] = {}
        self.invalid_packets = 0

    def ingest(self, packet: CIRPacket) -> None:
        try:
            features = cir_features(packet)
        except (ValueError, FloatingPointError):
            self.invalid_packets += 1
            return
        items = self.by_round.setdefault(packet.round_id, {}).setdefault(packet.anchor_id, [])
        items.append((packet, features))
        if len(items) > self.max_packets:
            del items[0:len(items)-self.max_packets]
        for r in sorted(self.by_round)[:-8]:
            self.by_round.pop(r, None)

    def metrics(self, round_id: int, anchor_id: str, m_esp_us: int, bssid: str = '') -> dict[str, float]:
        records = self.by_round.get(round_id, {}).get(anchor_id, [])
        bssid = bssid.strip().lower()
        candidates = [feat for packet, feat in records
                      if abs(packet.esp_us - m_esp_us) <= self.tolerance_us
                      and (not bssid or packet.source_mac == bssid)]
        if not candidates:
            return {'cir_packets': 0.0}
        out = {'cir_packets': float(len(candidates))}
        for k in ('cir_peak_ratio', 'cir_rms_delay_ns', 'cir_offpeak_ratio', 'cir_entropy'):
            out[k] = float(np.median([feat[k] for feat in candidates]))
        return out

    def complete_round(self, round_id: int) -> None:
        self.by_round.pop(round_id, None)


def heuristic_reliability(feat: dict[str, float], cfg: dict) -> float:
    """Uncalibrated demonstration only. Train a model on true ranging errors."""
    n = feat.get('cir_packets', 0.0)
    if not n:
        return 0.0
    def high(x, low, high):
        return float(np.clip((x-low)/(high-low), 0.0, 1.0))
    def low(x, good, bad):
        return 1.0 - high(x, good, bad)
    q_count = float(np.clip(n/max(float(cfg.get('packets_full', 5)), 1.0), 0.0, 1.0))
    q_peak = high(feat['cir_peak_ratio'], 0.19, 0.65)
    q_spread = low(feat['cir_rms_delay_ns'], 80.0, 400.0)
    q_offpeak = low(feat['cir_offpeak_ratio'], 0.2, 0.8)
    q_entropy = low(feat['cir_entropy'], 0.3, 0.85)
    return float(np.clip(0.05 + 0.95 * (
        0.15*q_count + 0.25*q_peak + 0.25*q_spread +
        0.20*q_offpeak + 0.15*q_entropy), 0.05, 1.0))


def model_features(feat: dict[str, float]) -> np.ndarray:
    """Feature order must match calibration JSON; fixed, deterministic."""
    return np.array([
        feat['cir_peak_ratio'],
        feat['cir_rms_delay_ns']/100.0,
        feat['cir_offpeak_ratio'],
        feat['cir_entropy'],
        np.log1p(feat['cir_packets']),
    ], dtype=float)


def sigma_from_model(feat: dict[str, float], model: dict) -> float:
    x = model_features(feat)
    mu = np.asarray(model['mean'], dtype=float)
    scale = np.maximum(np.asarray(model['scale'], dtype=float), 1e-7)
    coef = np.asarray(model['coef'], dtype=float)
    pred = float(model['intercept'] + ((x-mu)/scale) @ coef)
    return float(np.clip(math.exp(float(np.clip(pred, -2, 5))),
                         float(model.get('sigma_min_m', 0.35)),
                         float(model.get('sigma_max_m', 8.0))))
