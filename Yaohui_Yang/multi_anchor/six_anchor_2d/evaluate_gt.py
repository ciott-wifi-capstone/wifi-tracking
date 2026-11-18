#!/usr/bin/env python3
"""Evaluate independent static TEST points; export true 2D errors, coverage and figures."""
import argparse
import csv
import math
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'code'))
from acquisition import write_json
from gt_data import load_session, json_file, json_lines, point_for, true_range, digest
from positioning import select_range


def summary(values):
    if not values: return {'count': 0, 'mae_m': None, 'median_m': None, 'p90_m': None, 'rmse_m': None}
    a = np.asarray(values, float)
    return dict(count=len(values), mae_m=float(np.mean(abs(a))), median_m=float(np.median(abs(a))),
                p90_m=float(np.quantile(abs(a), .9)), rmse_m=float(np.sqrt(np.mean(a*a))))


def evaluate(directory, calibration_path, allow_repeat=False):
    session, points, audit = load_session(directory)
    calibration = json_file(calibration_path)
    if session.get('calibration_sha256') != digest(calibration_path):
        raise ValueError('Test session was not acquired with this exact frozen calibration file.')
    if session['geometry_sha256'] != calibration['geometry_sha256'] or session['model_hashes'] != calibration['model_hashes']:
        raise ValueError('Calibration/session geometry or model mismatch.')
    all_training_points = calibration['training_points']+calibration.get('model_training_points', [])
    if audit['serial_raw.log'] in {p['source_capture_sha256'] for p in all_training_points}:
        raise ValueError('This capture was used to fit calibration; collect a separate TEST session.')
    selected_points = {t:p for t,p in points.items() if p['status']=='COMPLETE' and p['purpose']=='TEST'}
    if not selected_points: raise ValueError('No COMPLETE TEST points in this session.')
    overlapping = []
    for p in selected_points.values():
        if min(math.hypot(p['x']-k['x'], p['y']-k['y']) for k in all_training_points) < .25:
            overlapping.append(p['point_id'])
    if overlapping and not allow_repeat:
        raise ValueError('Test points within 0.25 m of calibration locations: '+','.join(overlapping)+
                         '. Use new physical locations, or --allow-repeat-locations for repeatability-only evaluation.')
    config, method = session['config'], session['selected_method']
    errors, ranges, by_point, by_anchor = [], [], {}, {}
    expected_scans = sum(p['requested_scans'] for p in selected_points.values())
    for row in json_lines(Path(directory)/'positions.jsonl'):
        p = point_for(row, selected_points, 'TEST')
        if p is None: continue
        valid = row.get('valid') and all(math.isfinite(row.get(k, float('nan'))) for k in ('x','y'))
        e = math.hypot(row['x']-p['x'], row['y']-p['y']) if valid else None
        out = dict(point_id=p['point_id'], token=p['token'], kind=row['kind'], boot=row['boot'], scan_id=row['scan_id'],
                   gt_x=p['x'], gt_y=p['y'], estimated_x=row.get('x'), estimated_y=row.get('y'), error_2d_m=e,
                   valid=bool(valid), reason=row['reason'], rf_time_s=row['rf_time_s'])
        errors.append(out); by_point.setdefault(p['token'], []).append(out)
    for row in json_lines(Path(directory)/'ranges.jsonl'):
        p = point_for(row, selected_points, 'TEST')
        if p is None: continue
        selected = select_range(row, method, config, calibration)
        actual = true_range(p, config['anchors'][row['anchor_id']])
        predicted = row.get(method+'_m')
        corrected = selected['corrected_m'] if selected['range_usable'] else None
        raw_error = predicted-actual if predicted is not None and predicted > 0 else None
        corrected_error = corrected-actual if corrected is not None else None
        out = dict(point_id=p['point_id'], token=p['token'], anchor_id=row['anchor_id'], round_id=row['round_id'],
                   true_range_m=actual, predicted_m=predicted, corrected_m=corrected, raw_error_m=raw_error,
                   corrected_error_m=corrected_error, quality=selected['range_quality'], usable=selected['range_usable'],
                   exclusion=selected['exclusion'])
        ranges.append(out); by_anchor.setdefault(row['anchor_id'], []).append(out)
    point_summary = []
    for token, p in selected_points.items():
        rs = [r for r in by_point.get(token, []) if r['kind']=='WLS_SCAN' and r['valid']]
        es = [r['error_2d_m'] for r in rs]
        point_summary.append(dict(point_id=p['point_id'], token=token, gt_x=p['x'], gt_y=p['y'],
                                  expected_scans=p['requested_scans'], valid_scans=len(rs),
                                  mean_x=float(np.mean([r['estimated_x'] for r in rs])) if rs else None,
                                  mean_y=float(np.mean([r['estimated_y'] for r in rs])) if rs else None,
                                  **summary(es)))
    wls = [r['error_2d_m'] for r in errors if r['kind']=='WLS_SCAN' and r['valid']]
    ekf = [r['error_2d_m'] for r in errors if r['kind']=='EKF' and r['valid']]
    macro = [p['mae_m'] for p in point_summary if p['mae_m'] is not None]
    report = dict(schema_version=1, task='STATIC_2D_POSITIONING', method=method,
                  test_type='SAME_LOCATION_REPEATABILITY' if overlapping else 'HELD_OUT_LOCATIONS',
                  calibration_sha256=digest(calibration_path), input_audit=audit,
                  expected_scans=expected_scans, valid_wls_scans=len(wls),
                  wls_coverage=len(wls)/expected_scans if expected_scans else 0.,
                  wls=summary(wls), ekf=summary(ekf),
                  point_macro_mae_m=float(np.mean(macro)) if macro else None,
                  points_without_valid_position=sum(p['valid_scans']==0 for p in point_summary),
                  per_point=point_summary,
                  per_anchor={aid:dict(expected_links=expected_scans,
                                      coverage=sum(r['usable'] for r in rs)/expected_scans,
                                      before=summary([r['raw_error_m'] for r in rs if r['raw_error_m'] is not None]),
                                      after=summary([r['corrected_error_m'] for r in rs if r['corrected_error_m'] is not None]))
                              for aid,rs in by_anchor.items()},
                  warning='Static surveyed points only. Walking segments are unlabeled; no dynamic trajectory accuracy is claimed.')
    return report, errors, ranges, config


def save_csv(path, rows):
    if not rows: return
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def figures(destination, report, errors, config):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11, 6))
    for aid, a in config['anchors'].items():
        ax.scatter(a['x'], a['y'], marker='^', c='#146A85', s=95)
        ax.annotate(aid, (a['x'],a['y']), xytext=(5,7), textcoords='offset points')
    first = True
    for p in report['per_point']:
        ax.scatter(p['gt_x'],p['gt_y'],marker='+',s=120,c='black',label='Surveyed test point' if first else None)
        ax.annotate(p['point_id'],(p['gt_x'],p['gt_y']),xytext=(5,-12),textcoords='offset points')
        if p['mean_x'] is not None:
            ax.scatter(p['mean_x'],p['mean_y'],c='#B84A68',s=45,label='Mean WLS estimate' if first else None)
            ax.plot([p['gt_x'],p['mean_x']],[p['gt_y'],p['mean_y']],c='#B84A68',alpha=.55)
        first = False
    ax.set(xlabel='X (m)',ylabel='Y (m)',title='Independent static-point 2D positioning')
    ax.set_aspect('equal',adjustable='datalim'); ax.grid(alpha=.25); ax.legend()
    fig.tight_layout(); fig.savefig(destination/'test_point_map.png',dpi=180); plt.close(fig)
    fig, ax = plt.subplots(figsize=(7, 5))
    for kind, label in [('WLS_SCAN','Static scan WLS'),('EKF','Sequential range EKF')]:
        values=sorted(r['error_2d_m'] for r in errors if r['kind']==kind and r['valid'])
        if values: ax.step(values,np.arange(1,len(values)+1)/len(values),where='post',label=label)
    ax.set(xlabel='2D position error (m)',ylabel='Empirical CDF',ylim=(0,1.02),title='Static test points: all valid estimates')
    ax.grid(alpha=.25)
    if any(r['valid'] for r in errors): ax.legend()
    fig.tight_layout(); fig.savefig(destination/'position_error_cdf.png',dpi=180); plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',type=Path,required=True)
    p.add_argument('--calibration',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--allow-repeat-locations',action='store_true')
    args=p.parse_args()
    if args.out.exists() and any(args.out.iterdir()): p.error('Output folder is not empty; choose a new folder.')
    report,errors,ranges,config=evaluate(args.session,args.calibration,args.allow_repeat_locations)
    args.out.mkdir(parents=True,exist_ok=True)
    write_json(args.out/'summary.json',report)
    save_csv(args.out/'position_errors.csv',errors)
    save_csv(args.out/'range_errors.csv',ranges)
    save_csv(args.out/'point_summary.csv',report['per_point'])
    figures(args.out,report,errors,config)
    print('\nINDEPENDENT STATIC 2D TEST | '+report['test_type'])
    for s in report['per_point']:
        error='--' if s['mae_m'] is None else f"{s['mae_m']:.3f} m"
        print(f"{s['point_id']:10s} GT=({s['gt_x']:.2f},{s['gt_y']:.2f})  MAE={error}  scans={s['valid_scans']}/{s['expected_scans']}")
    print(f"Coverage: {report['valid_wls_scans']}/{report['expected_scans']} = {100*report['wls_coverage']:.1f}%")
    print('WLS:',report['wls'])
    print('Equal-point MAE:',report['point_macro_mae_m'])
    print('Saved:',args.out.resolve())


if __name__=='__main__': main()
