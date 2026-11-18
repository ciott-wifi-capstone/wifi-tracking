#!/usr/bin/env python3
"""Fit frozen per-anchor range offsets using CALIBRATION points only, equally weighted by point."""
import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'code'))
from acquisition import write_json
from gt_data import load_session, json_lines, point_for, true_range
from positioning import METHODS, model_hashes, model_training_points
from serial_engine import models_from
from cir_core import predict, SCHEMA


def fit(sources, include_ood=False, require_method='cir_residual', require_anchor_count=4, models_dir=None):
    geometry, hashes, config = None, None, None
    profiles, seen, audits, training = set(), set(), [], {}
    grouped, export = {}, []
    excluded = dict(nonpositive_or_failed=0, ood=0, incomplete_or_noncalibration=0)
    recompute = models_from(models_dir) if models_dir else None
    model_points = model_training_points(models_dir) if recompute else []
    for source in sources:
        session, points, audit = load_session(source)
        if audit['serial_raw.log'] in seen: raise ValueError('Duplicate raw capture included twice.')
        seen.add(audit['serial_raw.log'])
        if geometry is None:
            geometry, hashes, config = session['geometry_sha256'], model_hashes(models_dir) if recompute else session['model_hashes'], session['config']
        if geometry != session['geometry_sha256'] or (not recompute and hashes != session['model_hashes']):
            raise ValueError('Cannot mix different anchor installations, tag heights or frozen models.')
        if recompute and session.get('cir_feature_schema') != SCHEMA:
            raise ValueError('Cannot recompute models from an unknown CIR feature schema.')
        audits.append(dict(source=str(Path(source).resolve()), sha256=audit))
        if not recompute:
            for mp in session.get('model_training_points', []):
                if mp not in model_points: model_points.append(mp)
        for row in json_lines(Path(source)/'ranges.jsonl'):
            p = point_for(row, points, 'CALIBRATION')
            if p is None:
                excluded['incomplete_or_noncalibration'] += 1; continue
            if row.get('cir_residual_quality') == 'SOURCE_MISMATCH':
                excluded['nonpositive_or_failed'] += 1; continue
            aid = row['anchor_id']
            if aid not in config['anchors']: continue
            point_key = audit['serial_raw.log']+':'+p['token']
            profiles.add(row.get('firmware_profile'))
            training[point_key] = dict(point_id=p['point_id'], x=p['x'], y=p['y'], z=p['z'],
                                       source_capture_sha256=audit['serial_raw.log'], token=p['token'])
            actual = true_range(p, config['anchors'][aid])
            if recompute:
                for name, model in recompute.items():
                    value = predict(model, row['features']) if row.get('features') else {'distance_m':None,'quality':'CIR_UNAVAILABLE'}
                    row[name+'_m'], row[name+'_quality'] = value['distance_m'], value['quality']
            for method in METHODS:
                value = row.get(method+'_m')
                quality = 'RAW_FTM' if method == 'raw_ftm' else row.get(method+'_quality')
                if row.get('ftm_status') != 0 or value is None or not math.isfinite(value) or value <= 0:
                    excluded['nonpositive_or_failed'] += 1; continue
                if quality == 'OUT_OF_DOMAIN' and not include_ood:
                    excluded['ood'] += 1; continue
                if quality not in ('RAW_FTM', 'EXPERIMENTAL', 'OUT_OF_DOMAIN'): continue
                entry = dict(point_key=point_key, point_id=p['point_id'], anchor_id=aid, method=method,
                             bssid=row['bssid'], true_distance_m=actual, predicted_m=value,
                             bias_sample_m=value-actual, quality=quality, round_id=row['round_id'])
                grouped.setdefault((method, aid), []).append(entry); export.append(entry)
    if len(profiles) != 1 or None in profiles: raise ValueError('Missing or mixed PROFILE records. Use one firmware profile per calibration.')
    unique_xy = {(p['x'], p['y']) for p in training.values()}
    if len(unique_xy) < 3: raise ValueError('Need at least three distinct surveyed CALIBRATION locations; TEST points are never used.')
    methods, skipped = {}, {}
    for method in METHODS:
        methods[method] = {}
        for aid in config['anchors']:
            rows = grouped.get((method, aid), [])
            by_point = {}
            for r in rows: by_point.setdefault(r['point_key'], []).append(r)
            unique = {(training[k]['x'], training[k]['y']) for k in by_point}
            bssids = {r['bssid'] for r in rows}
            if len(unique) < 3 or len(bssids) != 1 or any(not b or b == '00:00:00:00:00:00' for b in bssids):
                skipped[method+'/'+aid] = 'Need >=3 distinct successful locations and one stable real BSSID.'; continue
            point_biases = np.array([np.median([r['bias_sample_m'] for r in rs]) for rs in by_point.values()])
            bias = float(np.median(point_biases))
            # Equal influence per point. This training residual is a heuristic noise scale, not validation accuracy.
            absolute_residual = [np.median([abs(r['bias_sample_m']-bias) for r in rs]) for rs in by_point.values()]
            sigma = max(.75, float(1.4826*np.median(absolute_residual)))
            span = [min(r['true_distance_m'] for r in rows), max(r['true_distance_m'] for r in rows)]
            methods[method][aid] = dict(bias_m=bias, sigma_m=sigma, bssid=next(iter(bssids)),
                                        calibration_points=len(by_point), calibration_rows=len(rows),
                                        true_range_span_m=span, out_of_domain_rows=sum(r['quality']=='OUT_OF_DOMAIN' for r in rows),
                                        limited_distance_span=span[1]-span[0] < 3.)
    missing = set(config['anchors'])-set(methods[require_method])
    if len(methods[require_method]) < require_anchor_count:
        raise ValueError(f'{require_method}: insufficient support for {sorted(missing)}. Collect more calibration locations/valid CIR pairs, '
                         'or explicitly use --include-ood for experimental transfer. No test labels were used.')
    data = dict(schema_version=1, kind='PER_ANCHOR_MEDIAN_BIAS', created_utc=datetime.now(timezone.utc).isoformat(),
                formula='corrected_range_m = frozen_model_prediction_m - bias_m',
                geometry_sha256=geometry, model_hashes=hashes, firmware_profile=next(iter(profiles)),
                include_ood=include_ood, methods=methods, skipped=skipped, excluded_counts=excluded,
                training_points=list(training.values()), input_audits=audits,
                validation='NOT_YET_VALIDATED: use separate TEST points/capture; no training MAE advertised as test accuracy.',
                sigma_note='Heuristic training residual scale with 0.75 m floor; not a confidence bound.',
                limitation='Offset corrects a stable board bias, not distance-dependent multipath or NLOS bias.')
    data['uncalibrated_anchors'] = {m: sorted(set(config['anchors'])-set(v)) for m,v in methods.items()}
    data['prediction_source'] = 'RECOMPUTED_SAVED_CIR_FEATURES' if recompute else 'CAPTURED_FROZEN_MODEL_PREDICTIONS'
    data['model_training_points'] = model_points
    return data, export


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sessions', type=Path, nargs='+', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--include-ood', action='store_true', help='Include positive OOD predictions, retain OOD flags and conservative live weight.')
    p.add_argument('--require-method', choices=METHODS, default='cir_residual')
    p.add_argument('--require-anchor-count', type=int, choices=[4,5,6], default=4)
    p.add_argument('--models', type=Path, help='Recompute predictions from stored features using newly refitted models; no recollection needed.')
    args = p.parse_args()
    if args.out.exists(): p.error('Output already exists; choose a new calibration filename.')
    data, rows = fit(args.sessions, args.include_ood, args.require_method, args.require_anchor_count, args.models)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.out, data)
    csv_path = args.out.with_suffix('.training_ranges.csv')
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print('Saved:', args.out.resolve())
    for method, anchors in data['methods'].items():
        print(method, '|', ', '.join(f'{aid}: bias {v["bias_m"]:+.3f} m' for aid, v in anchors.items()))
        if data['uncalibrated_anchors'][method]: print('  Uncalibrated / excluded:', data['uncalibrated_anchors'][method])
    print('Calibration fitted. Next collect independent TEST points with this frozen calibration.')


if __name__ == '__main__':
    main()
