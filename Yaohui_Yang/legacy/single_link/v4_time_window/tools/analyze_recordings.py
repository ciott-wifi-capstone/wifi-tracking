#!/usr/bin/env python3
"""Analyze earlier/new QAR-FTM capture ZIP or recordings folder.
No model training; the current 10 single-placement trials are insufficient
for a trustworthy held-out calibration claim.

Usage:
  python tools/analyze_recordings.py --input "recordings(2).zip" --out results
  python tools/analyze_recordings.py --input recordings --out results_new
Requires numpy (pip install numpy).
"""
import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path
import numpy as np


def read_trials(src):
    path = Path(src)
    if path.is_file() and path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z:
            groups = defaultdict(dict)
            for name in z.namelist():
                if '/' not in name or name.endswith('/'):
                    continue
                fname = Path(name).name
                if fname in ('ftm.csv', 'ftm_entries.csv', 'csi.csv', 'metadata.json'):
                    group = Path(name).parent.name
                    groups[group][fname] = z.read(name).decode('utf-8-sig', errors='replace')
            yield from sorted(groups.items())
    elif path.is_dir():
        for d in sorted(path.iterdir()):
            if d.is_dir() and (d/'metadata.json').exists():
                yield d.name, {p.name: p.read_text(encoding='utf-8-sig') for p in d.iterdir()
                               if p.name in ('ftm.csv', 'ftm_entries.csv', 'csi.csv', 'metadata.json')}
    else:
        raise ValueError('Provide a .zip or a recordings directory')


def csvrows(raw):
    return list(csv.DictReader(io.StringIO(raw))) if raw else []


def safe_float(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return math.nan


def selectivity_features(row):
    """20 MHz LLTF-only, 128-byte captures. Non-FTM CSI, no absolute TOF.
    IEEE 802.11 IFFT time origin includes unknown receiver timing offset.
    """
    if int(row.get('len_bytes', 0)) != 128:
        return None
    try:
        raw = bytes.fromhex(row['csi_hex'])
    except (ValueError, KeyError):
        return None
    if len(raw) != 128:
        return None
    x = np.frombuffer(raw, dtype=np.int8).astype(np.float64)
    # Espressif: imaginary byte then real byte.
    H = x[1::2] + 1j*x[0::2]
    if int(row.get('first_word_invalid',0)):
        H[:2] = 0
    amp = np.abs(H)
    idx = np.flatnonzero(amp > 0)
    if len(idx) < 32:
        return None
    mean = float(np.mean(amp[idx]))
    cv = float(np.std(amp[idx]) / max(mean, 1e-8))
    # Normalized magnitude variation. Not a NLOS label or propagation time.
    ripple = float(np.median(np.abs(np.diff(amp[idx]))) / max(mean, 1e-8))
    # Relative delay-domain profile. Useful for comparative features ONLY.
    cir = np.fft.ifft(H)
    p = np.abs(cir)**2
    entropy = float(-np.sum((p/(p.sum()+1e-10))*np.log((p/(p.sum()+1e-10))+1e-12)) / np.log(len(p)))
    return cv, ripple, entropy


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', default='results')
    args = ap.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    summary = []
    features = []
    for name, files in read_trials(args.input):
        meta = json.loads(files.get('metadata.json', '{}'))
        truth = safe_float(meta.get('true_distance_m'))
        condition = str(meta.get('condition', 'UNKNOWN'))
        rows = csvrows(files.get('ftm.csv'))
        ftm = np.array([safe_float(r.get('estimated_distance_m')) for r in rows
                        if r.get('status') == '0' and math.isfinite(safe_float(r.get('estimated_distance_m')))], dtype=float)
        entries = csvrows(files.get('ftm_entries.csv'))
        frame_dist = defaultdict(list)
        for r in entries:
            rtt = safe_float(r.get('rtt_ps'))
            if math.isfinite(rtt) and 0 <= rtt < 4294967295:
                frame_dist[r['round_id']].append(0.000149896229 * rtt)
        p25_sessions = [np.percentile(d, 25) for d in frame_dist.values() if len(d) >= 4]
        csi_rows = csvrows(files.get('csi.csv'))
        csi_features = [selectivity_features(r) for r in csi_rows]
        csi_features = [x for x in csi_features if x is not None]
        if csi_features:
            vals = np.array(csi_features)
            features.append({'trial':name, 'condition':condition, 'truth_m':truth,
                'csi_20mhz_128bytes_used':len(vals), 'amplitude_cv_median':round(float(np.median(vals[:,0])), 5),
                'magnitude_ripple_median':round(float(np.median(vals[:,1])), 5),
                'relative_cir_entropy_median':round(float(np.median(vals[:,2])), 5)})
        ok = len(ftm)
        if ok:
            signed = ftm - truth
            err = np.abs(signed)
            row = {'trial':name, 'condition':condition, 'truth_m':truth,
                'ftm_sessions_total':len(rows), 'ftm_success_count':ok,
                'median_raw_m':round(float(np.median(ftm)),3),
                'bias_mean_m':round(float(np.mean(signed)),3),
                'std_raw_m':round(float(np.std(ftm,ddof=1)) if ok>1 else 0.0,3),
                'mae_raw_m':round(float(np.mean(err)),3),
                'rmse_raw_m':round(float(np.sqrt(np.mean(signed**2))),3),
                'p90_abs_error_m':round(float(np.percentile(err,90)),3),
                'median_frame_p25_m':round(float(np.median(p25_sessions)),3) if p25_sessions else '',
                'csi_packets_total':len(csi_rows),
                'csi_cwb1_ratio':round(sum(r.get('cwb')=='1' for r in csi_rows)/max(1,len(csi_rows)),3),
                'csi_len128_count':sum(r.get('len_bytes')=='128' for r in csi_rows)}
            summary.append(row)
    for filename, data in [('ranging_summary.csv', summary), ('csi_relative_features.csv', features)]:
        if data:
            with (output/filename).open('w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=list(data[0])); writer.writeheader(); writer.writerows(data)
    print('TRIAL | true | median raw | mean bias | MAE | P90 | CSI packets')
    for r in summary:
        print(f"{r['trial']} | {r['truth_m']:.2f} | {r['median_raw_m']:.2f} | {r['bias_mean_m']:+.2f} | {r['mae_raw_m']:.2f} | {r['p90_abs_error_m']:.2f} | {r['csi_packets_total']}")
    print('Outputs:', output.resolve())
    print('CAUTION: raw FTM and window-associated CSI; no verified CSI-to-FTM ToF correction.')
    print('Do not fit a high-capacity model or claim generalization with one placement per trial.')

if __name__ == '__main__':
    main()
