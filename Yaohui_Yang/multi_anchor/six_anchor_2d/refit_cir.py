#!/usr/bin/env python3
"""Optional: refit CIR distance coefficients from >=6 surveyed CALIBRATION locations.

Every fold holds out an entire physical location and all of its anchor links.
TEST labels never enter fitting or model selection. The final models still need
an independent TEST capture; cross-validation here is model-selection data.
"""
import argparse
from collections import defaultdict
from pathlib import Path
import sys
import math
import numpy as np

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'code'))
from gt_data import load_session, json_lines, point_for, true_range
from cir_core import SCHEMA, fit_model, predict, save_model
from acquisition import write_json
from positioning import model_hashes


def physical_groups(points, radius=.25):
    keys=list(points); parent={k:k for k in keys}
    def find(k):
        while parent[k]!=k:
            parent[k]=parent[parent[k]]; k=parent[k]
        return k
    for i,k in enumerate(keys):
        for j in keys[:i]:
            if math.hypot(points[k]['x']-points[j]['x'],points[k]['y']-points[j]['y']) < radius:
                parent[find(k)]=find(j)
    return {k:find(k) for k in keys}


def read_training(sources):
    rows,points,audits,seen=[],{},[],set()
    geometry,profile=None,None
    for source in sources:
        session,ps,audit=load_session(source)
        if session.get('cir_feature_schema')!=SCHEMA: raise ValueError('Unknown/missing CIR feature schema.')
        if audit['serial_raw.log'] in seen: raise ValueError('Duplicate input capture.')
        seen.add(audit['serial_raw.log'])
        if geometry is None: geometry=session['geometry_sha256']
        if geometry!=session['geometry_sha256']: raise ValueError('Different installation/tag height in input sessions.')
        audits.append(dict(source=str(Path(source).resolve()),sha256=audit))
        for row in json_lines(Path(source)/'ranges.jsonl'):
            p=point_for(row,ps,'CALIBRATION')
            if p is None or row['ftm_status']!=0 or not row.get('features'): continue
            if row.get('cir_residual_quality')=='SOURCE_MISMATCH': continue
            if profile is None: profile=row.get('firmware_profile')
            if not profile or profile!=row.get('firmware_profile'): raise ValueError('Missing or mixed firmware profiles.')
            key=audit['serial_raw.log']+':'+p['token']
            points[key]=dict(x=p['x'],y=p['y'],z=p['z'],point_id=p['point_id'],
                             source_capture_sha256=audit['serial_raw.log'],token=p['token'])
            feature=dict(row['features'])
            feature.update(true_distance_m=true_range(p,session['config']['anchors'][row['anchor_id']]),
                           point_key=key,anchor_id=row['anchor_id'],trial=key+':'+row['anchor_id'])
            rows.append(feature)
    groups=physical_groups(points)
    if len(set(groups.values()))<6:
        raise ValueError('Full CIR refit requires >=6 distinct CALIBRATION locations (>=0.25 m apart). '
                         'For three-point board-offset calibration, use calibrate.py instead.')
    for row in rows:
        row['physical_group']=groups[row['point_key']]
        row['trial']=row['physical_group']+':'+row['anchor_id']
    return rows,points,audits,geometry,profile


def selection_score(model, rows):
    by_link=defaultdict(list)
    for r in rows:
        # Evaluate every finite prediction; negative/OOD predictions are not hidden in CV.
        value=predict(model,r)['distance_m']
        by_link[r['trial']].append(abs(value-r['true_distance_m']))
    return float(np.mean([np.mean(e) for e in by_link.values()]))


def train(sources):
    rows,points,audits,geometry,profile=read_training(sources)
    groups=sorted({r['physical_group'] for r in rows})
    models,results={},{}
    for kind in ('ftm_affine','cir_direct','cir_residual'):
        candidates=[0.] if kind=='ftm_affine' else [1.,10.,100.,1000.]
        scores={}
        for alpha in candidates:
            fold_scores=[]
            for heldout in groups:
                train_rows=[r for r in rows if r['physical_group']!=heldout]
                test_rows=[r for r in rows if r['physical_group']==heldout]
                model=fit_model(train_rows,kind=kind,alpha=alpha)
                fold_scores.append(selection_score(model,test_rows))
            scores[str(alpha)]=dict(mean_location_mae_m=float(np.mean(fold_scores)),fold_mae_m=fold_scores)
        best=min(candidates,key=lambda a:scores[str(a)]['mean_location_mae_m'])
        models[kind]=fit_model(rows,kind=kind,alpha=best)
        models[kind]['fit_origin']='SURVEYED_MULTI_ANCHOR_CALIBRATION_POINTS'
        results[kind]=dict(selected_alpha=best,candidates=scores)
    info=dict(schema_version=1,kind='CIR_FEATURE_DISTANCE_REFIT',geometry_sha256=geometry,firmware_profile=profile,
              physical_calibration_locations=len(groups),feature_rows=len(rows),training_points=list(points.values()),
              input_audits=audits,selection=results,
              validation='LOPO scores were used to select ridge alpha. Collect a separate TEST session for reported accuracy.',
              note='Same LLTF/CIR reconstruction as the original model. No absolute CIR peak-to-metre conversion.')
    return models,info


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sessions',type=Path,nargs='+',required=True)
    p.add_argument('--out',type=Path,required=True,help='New frozen model folder.')
    args=p.parse_args()
    if args.out.exists() and any(args.out.iterdir()): p.error('Model folder already contains files. Use a new name.')
    models,info=train(args.sessions)
    args.out.mkdir(parents=True,exist_ok=True)
    for kind,model in models.items(): save_model(args.out/(kind+'.json'),model)
    info['model_hashes']=model_hashes(args.out)
    write_json(args.out/'fit_info.json',info)
    print('Frozen CIR models saved:',args.out.resolve())
    print('Next: calibrate.py --sessions <same calibration capture> --models',args.out,'--out calibration_gt.json --include-ood')
    print('Then collect separate TEST points using BOTH --models and --calibration. No test data were used here.')


if __name__=='__main__':main()
