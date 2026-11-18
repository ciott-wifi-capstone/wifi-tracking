#!/usr/bin/env python3
"""Evaluate a completed stationary live capture against a tape-measured distance."""
import argparse
import csv
import json
import math
from pathlib import Path
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True, help='Completed measurements/live_... folder')
    parser.add_argument('--truth-m', type=float, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.truth_m) or args.truth_m <= 0:parser.error('--truth-m must be positive.')
    root = Path(args.capture)
    metadata = json.loads((root/'measurement.json').read_text(encoding='utf-8'))
    if metadata['source_mode'] != 'LIVE_SERIAL' or metadata['state'] == 'RUNNING':
        parser.error('Stop the live measurement before evaluating it.')
    if not (root/'live_predictions.csv').exists():
        parser.error('No measurement CSV exists. Check the serial connection error in measurement.json.')
    with (root/'live_predictions.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    summary = []
    print(f'Tape-measured reference: {args.truth_m:g} m | Completed rounds: {len(rows)}')
    print('Reference is used for evaluation only; predictions and models are unchanged.')
    for name in ['raw_ftm', 'ftm_affine', 'cir_direct', 'cir_residual']:
        estimates = [float(r[name+'_m']) for r in rows if r[name+'_m'] != '']
        errors = np.abs(np.asarray(estimates)-args.truth_m)
        record = {'method': name, 'true_distance_m': args.truth_m, 'completed_rounds': len(rows),
                  'predicted_rounds': len(estimates), 'missing_rounds': len(rows)-len(estimates),
                  'negative_predictions': sum(v < 0 for v in estimates),
                  'out_of_domain_predictions': sum(r.get(name+'_quality') == 'OUT_OF_DOMAIN' for r in rows),
                  'mae_m': float(np.mean(errors)) if len(errors) else None,
                  'rmse_m': float(np.sqrt(np.mean(errors**2))) if len(errors) else None,
                  'median_abs_error_m': float(np.median(errors)) if len(errors) else None,
                  'p90_abs_error_m': float(np.percentile(errors,90)) if len(errors) else None}
        summary.append(record)
        value = '--' if record['mae_m'] is None else f"{record['mae_m']:.3f} m"
        print(f"{name:<14} valid_numeric={len(estimates):>3}/{len(rows)}  MAE={value}")
    output = root/f'evaluation_{args.truth_m:g}m.csv'
    with output.open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    (root/f'evaluation_{args.truth_m:g}m.json').write_text(json.dumps({'reference_m':args.truth_m,
        'includes_negative_and_out_of_domain_predictions':True, 'capture_state':metadata['state'],
        'errors':summary},indent=2)+'\n',encoding='utf-8')
    print('Saved:', output.resolve())


if __name__ == '__main__':main()
