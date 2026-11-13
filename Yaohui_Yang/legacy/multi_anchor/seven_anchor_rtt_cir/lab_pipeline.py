#!/usr/bin/env python3
"""Lab recorder, RTT-vs-CIR feature dataset, calibration, and fair A/B comparisons.

Manifest: session_id,split,condition,true_x_m,true_y_m,true_z_m,log_file
Each session represents one STATIONARY tag point with a known position.
Use different physical points/sessions and ideally different days for test split.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from cir_features import CIRBuffer, cir_features, model_features, parse_csiraw
from localizer_core import (StrictEpochLocalizer, parse_mfused,
                            parse_round_meta, parse_round_start)

HERE = Path(__file__).resolve().parent
FEATURE_KEYS = ['cir_peak_ratio','cir_rms_delay_ns','cir_offpeak_ratio','cir_entropy']


def load_manifest(path: Path) -> list[dict]:
    with path.open(newline='',encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    if not rows:
        raise ValueError('Manifest is empty')
    sessions=set()
    for r in rows:
        for k in ('session_id','split','condition','true_x_m','true_y_m','true_z_m','log_file'):
            if not r.get(k):
                raise ValueError(f'Missing {k} in manifest session {r.get("session_id", "?")}')
        if r['session_id'] in sessions:
            raise ValueError('session_id must be unique per measurement file')
        sessions.add(r['session_id'])
        if r['split'] not in ('train', 'test'):
            raise ValueError('split must be train or test, not individual-sample random split')
        for k in ('true_x_m','true_y_m','true_z_m'):
            r[k]=float(r[k])
        p=Path(r['log_file'])
        r['log_file_resolved'] = p if p.is_absolute() else (path.parent/p)
        if not r['log_file_resolved'].is_file():
            raise FileNotFoundError(str(r['log_file_resolved']))
    return rows


def raw_records(log_file: Path) -> list[str]:
    out=[]
    with log_file.open(encoding='utf-8', errors='ignore') as f:
        for line in f:
            line=line.strip()
            if not line: continue
            first, sep, remainder=line.partition(',')
            if sep:
                try:
                    float(first)
                    if remainder.startswith(('MFUSED,','MROUND_START,','MROUND_META,','CSIRAW,')):
                        line=remainder
                except ValueError:
                    pass
            if line.startswith(('MFUSED,','MROUND_START,','MROUND_META,','CSIRAW,')):
                out.append(line)
    return out


def grouped_epochs(raw_lines: list[str]):
    epochs=defaultdict(lambda: {'start':None,'meta':None,'measurements':[],'packets':[]})
    for line in raw_lines:
        if line.startswith('CSIRAW,'):
            p=parse_csiraw(line)
            if p: epochs[p.round_id]['packets'].append(p)
        elif line.startswith('MFUSED,'):
            m=parse_mfused(line)
            if m: epochs[m.round_id]['measurements'].append(m)
        elif line.startswith('MROUND_START,'):
            z=parse_round_start(line)
            if z: epochs[z[0]]['start']=z[1]
        elif line.startswith('MROUND_META,'):
            z=parse_round_meta(line)
            if z: epochs[z[0]]['meta']=z
    # Preserve insertion order: round_id may reset after a device reboot; record separately in that case.
    return epochs


def extract_dataset(manifest: list[dict], config: dict) -> list[dict]:
    output=[]
    anchors=config['anchors']
    for session in manifest:
        cb=CIRBuffer(tolerance_ms=config.get('cir_weighting',{}).get('tolerance_ms',650),
                     max_packets_per_link=config.get('cir_weighting',{}).get('max_packets_per_link',32))
        for rid, rec in grouped_epochs(raw_records(session['log_file_resolved'])).items():
            for p in rec['packets']: cb.ingest(p)
            for m in rec['measurements']:
                if m.status!=0 or m.anchor_id not in anchors or not anchors[m.anchor_id].get('enabled',True):
                    continue
                ax=anchors[m.anchor_id]
                dx=session['true_x_m']-float(ax['x_m'])
                dy=session['true_y_m']-float(ax['y_m'])
                dz=session['true_z_m']-float(ax.get('z_m',0))
                true_d=math.sqrt(dx*dx+dy*dy+dz*dz)
                feat=cb.metrics(rid,m.anchor_id,m.esp_us,m.bssid)
                signed_error=m.corrected_m-true_d
                output.append({
                    'session_id':session['session_id'], 'split':session['split'],
                    'condition':session['condition'], 'round':rid,'anchor':m.anchor_id,
                    'true_distance_m':true_d,'measured_distance_m':m.corrected_m,
                    'signed_error_m':signed_error,'abs_error_m':abs(signed_error),
                    'ftm_rssi':m.ftm_rssi,'entries':m.entries,
                    'cir_packets':int(feat.get('cir_packets',0)),
                    **{k: feat.get(k,'') for k in FEATURE_KEYS}
                })
            cb.complete_round(rid)
    return output


def write_csv(file: Path, rows: list[dict], fields: list[str] | None=None):
    file.parent.mkdir(parents=True,exist_ok=True)
    fields = fields or (list(rows[0].keys()) if rows else [])
    if not fields:
        raise ValueError('No rows to write')
    with file.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        w.writeheader();w.writerows(rows)


def dataset_rows(csvfile: Path) -> list[dict]:
    with csvfile.open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def fit_model(rows: list[dict]) -> dict:
    good=[r for r in rows if r['split']=='train' and int(r['cir_packets'])>=3]
    if len(good)<30 or len(set(r['session_id'] for r in good))<3:
        raise ValueError('Need >=30 matched training link records from >=3 distinct train sessions.')
    X=np.asarray([model_features({k:float(r[k]) for k in FEATURE_KEYS} | {'cir_packets':float(r['cir_packets'])})
                  for r in good])
    y=np.log(np.clip(np.asarray([float(r['abs_error_m']) for r in good]),0.35,8.0))
    mu=X.mean(axis=0);scale=np.maximum(X.std(axis=0),1e-6)
    Z=(X-mu)/scale
    Zb=np.c_[np.ones(len(Z)),Z]
    # Reduce domination by heavily sampled sessions: all sessions get equal total weight.
    counts=defaultdict(int)
    for r in good: counts[r['session_id']]+=1
    weights=np.asarray([1.0/counts[r['session_id']] for r in good],float)
    weights*=len(weights)/weights.sum()
    reg=np.diag([0.0]+[12.0]*Z.shape[1])
    coeff=np.linalg.pinv(Zb.T @ (weights[:,None]*Zb)+reg) @ (Zb.T @ (weights*y))
    return {'version':1,'features':['peak_ratio','rms_delay_100ns','offpeak_ratio','entropy','log1p_packets'],
            'intercept':float(coeff[0]),'coef':coeff[1:].tolist(),
            'mean':mu.tolist(),'scale':scale.tolist(),
            'sigma_min_m':0.35,'sigma_max_m':8.0,
            'trained_sessions':sorted(set(r['session_id'] for r in good)),
            'training_link_samples':len(good),
            'caution':'Predicted absolute ranging error proxy, NOT verified 1-sigma Gaussian std. Tune/validate on held-out sessions.'}


def stats(values: list[float]) -> dict:
    if not values: return {'n':0,'mae_m':'','rmse_m':'','median_m':'','p90_m':''}
    arr=np.asarray(values,float)
    return {'n':len(arr),'mae_m':round(float(arr.mean()),4),
            'rmse_m':round(float(np.sqrt(np.mean(arr**2))),4),
            'median_m':round(float(np.median(arr)),4),
            'p90_m':round(float(np.percentile(arr,90)),4)}


def ranging_summary(rows: list[dict]) -> list[dict]:
    """Actual ranging error by split / scene / anchor, plus overall totals.

    Each link observation remains in link_dataset.csv; this is descriptive only.
    """
    grouped=defaultdict(list)
    for r in rows:
        # Give overall, by-condition, and per-anchor summaries.
        split=str(r['split']); condition=str(r['condition']); aid=str(r['anchor'])
        for key in [(split,'ALL','ALL'),(split,condition,'ALL'),
                    (split,condition,aid),(split,'ALL',aid)]:
            grouped[key].append(r)
    summary=[]
    for (split,condition,aid),group in sorted(grouped.items()):
        errors=[float(r['signed_error_m']) for r in group]
        data=stats([abs(e) for e in errors])
        data.update({'split':split,'condition':condition,'anchor':aid,
                     'signed_bias_m':round(float(np.mean(errors)),4),
                     'cir_match_rate':round(sum(int(r['cir_packets'])>=3 for r in group)/len(group),4)})
        summary.append(data)
    return summary


def common_round_summary(details: list[dict]) -> list[dict]:
    """Compare algorithms on exactly the same session+round data.

    This prevents CIR-only's lower coverage from making its errors look
    artificially smaller by discarding challenging rounds.
    """
    all_modes={'rtt','csi','cir','hybrid'}
    indexed=defaultdict(dict)
    for row in details:
        indexed[(row['session_id'],int(row['round']))][row['mode']]=row
    paired=[]
    for variants in indexed.values():
        if all_modes.issubset(variants):
            paired.extend(variants[mode] for mode in sorted(all_modes))
    summary=[]
    for split in ('train','test'):
        conditions=sorted({r['condition'] for r in paired if r['split']==split})+['ALL']
        for condition in conditions:
            for mode in ('rtt','csi','cir','hybrid'):
                sub=[r for r in paired if r['split']==split and r['mode']==mode
                     and (condition=='ALL' or r['condition']==condition)]
                if not sub: continue
                for key in ('raw_error_m','filtered_error_m'):
                    summary.append({'split':split,'condition':condition,'mode':mode,
                                    'estimate':key.replace('_error_m',''),
                                    **stats([r[key] for r in sub])})
    return summary


def replay(manifest: list[dict], config: dict, output: Path, model_file: Path | None):
    details=[]; coverage=[]
    for sess in manifest:
        ep=grouped_epochs(raw_records(sess['log_file_resolved']))
        for mode in ('rtt','csi','cir','hybrid'):
            cfg=copy.deepcopy(config)
            cfg.setdefault('cir_weighting',{})['mode']=mode
            cfg['cir_weighting']['calibration_file']=str(model_file.resolve()) if model_file else ''
            path=output/'replays'/sess['session_id']/mode
            localizer=StrictEpochLocalizer(cfg,path)
            complete=0;solved=0; matched_links=0;all_links=0
            try:
                for rid, rec in ep.items():
                    if rec['meta'] is None: continue
                    complete+=1
                    if rec['start'] is not None:
                        localizer.start_epoch(rid,rec['start'])
                    for m in rec['measurements']:
                        if m.status==0 and m.anchor_id in localizer.anchors:
                            all_links+=1
                        localizer.ingest(m)
                    for p in rec['packets']:localizer.ingest_cir(p)
                    matched_links+=sum(
                        localizer.cirbuf.metrics(rid,m.anchor_id,m.esp_us,m.bssid).get('cir_packets',0)>=cfg['cir_weighting']['min_packets']
                        for m in rec['measurements'] if m.status==0 and m.anchor_id in localizer.anchors)
                    meta=rec['meta']
                    sol=localizer.finalize(*meta)
                    if sol is None: continue
                    solved+=1
                    rx,ry=sol['raw'];fx,fy=sol['filtered']
                    gt=np.array([sess['true_x_m'],sess['true_y_m']])
                    details.append({'session_id':sess['session_id'],'split':sess['split'],
                        'condition':sess['condition'],'mode':mode,'round':rid,
                        'raw_error_m':round(float(np.linalg.norm(sol['raw']-gt)),5),
                        'filtered_error_m':round(float(np.linalg.norm(sol['filtered']-gt)),5),
                        'raw_x_m':rx,'raw_y_m':ry,'filter_x_m':fx,'filter_y_m':fy,
                        'true_x_m':gt[0],'true_y_m':gt[1],
                        'anchor_count':sol['anchor_count'],'duration_ms':sol['duration_ms']})
            finally:
                localizer.close()
            coverage.append({'session_id':sess['session_id'],'split':sess['split'],'condition':sess['condition'],
                'mode':mode,'complete_rounds':complete,'solved_rounds':solved,
                'solution_rate':round(solved/max(complete,1),4),
                'matched_cir_links':matched_links,'valid_ftm_links':all_links})
    write_csv(output/'position_detail.csv', details)
    write_csv(output/'coverage.csv',coverage)
    summary=[]
    for split in ('train','test'):
        for condition in sorted(set(s['condition'] for s in manifest)) + ['ALL']:
            for mode in ('rtt','csi','cir','hybrid'):
                sub=[r for r in details if r['split']==split and r['mode']==mode
                     and (condition=='ALL' or r['condition']==condition)]
                if not sub:continue
                for key in ('raw_error_m','filtered_error_m'):
                    summary.append({'split':split,'condition':condition,'mode':mode,
                        'estimate':key.replace('_error_m',''),**stats([r[key] for r in sub])})
    write_csv(output/'position_summary.csv', summary)
    common=common_round_summary(details)
    if common:
        write_csv(output/'position_summary_common_rounds.csv',common)
    else:
        (output/'position_summary_common_rounds.csv').write_text(
            'split,condition,mode,estimate,n,mae_m,rmse_m,median_m,p90_m\n',encoding='utf-8-sig')
    return details,coverage,summary


def main():
    parser=argparse.ArgumentParser(description='Collect/calibrate/compare ESP32-S3 RTT+CSI/CIR')
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('capture',help='Save raw serial lines to lab log')
    p.add_argument('--port',required=True); p.add_argument('--baud',type=int,default=115200)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--seconds',type=float,default=0.0)
    p=subs.add_parser('features',help='Convert manifests and CSI to labeled link dataset')
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--config',type=Path,default=HERE/'anchors_7.json')
    p.add_argument('--output',type=Path,default=HERE/'lab_results'/'link_dataset.csv')
    p=subs.add_parser('calibrate',help='Fit error model using TRAIN split only')
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--output',type=Path,default=HERE/'cir_model.json')
    p=subs.add_parser('compare',help='Offline replay CSI/CIR/hybrid on the same lab logs')
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--config',type=Path,default=HERE/'anchors_7.json')
    p.add_argument('--model',type=Path,default=None);p.add_argument('--output',type=Path,default=HERE/'lab_results')
    args=parser.parse_args()
    if args.command=='capture':
        import serial
        args.output.parent.mkdir(parents=True,exist_ok=True)
        n=0;start=time.monotonic()
        try:
            with serial.Serial(args.port,args.baud,timeout=0.3) as ser, args.output.open('w',encoding='utf-8',buffering=1) as out:
                print('Recording. Press Ctrl+C to stop. Close dashboard/IDF monitor first.')
                while not args.seconds or time.monotonic()-start<args.seconds:
                    data=ser.readline()
                    if not data:continue
                    line=data.decode('utf-8',errors='replace').strip()
                    if line:
                        out.write(f'{time.time():.6f},{line}\n')
                        n+=1
        except KeyboardInterrupt:
            pass
        print('Saved',n,'serial lines to',args.output)
    elif args.command=='features':
        config=json.loads(args.config.read_text(encoding='utf-8'))
        dataset=extract_dataset(load_manifest(args.manifest),config)
        write_csv(args.output,dataset)
        ranging_file=args.output.with_name('ranging_summary.csv')
        write_csv(ranging_file,ranging_summary(dataset))
        print('Wrote',len(dataset),'FTM link records:',args.output)
        print('RTT ranging error summary:',ranging_file)
        n=sum(int(r['cir_packets'])>=config.get('cir_weighting',{}).get('min_packets',3) for r in dataset)
        print('Matched CIR >= minimum packet count:',n,'/',len(dataset))
    elif args.command=='calibrate':
        rows=dataset_rows(args.dataset)
        model=fit_model(rows)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(model,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print('Model saved:',args.output)
        if not any(r['split']=='test' for r in rows):
            print('WARNING: no held-out test records in dataset.')
    else:
        config=json.loads(args.config.read_text(encoding='utf-8'))
        details, coverage, summary=replay(load_manifest(args.manifest),config,args.output,args.model)
        print('Position records:',len(details),'summary:',args.output/'position_summary.csv')
        for r in summary:
            if r['split']=='test' and r['condition']=='ALL' and r['estimate']=='raw':
                print(f"TEST {r['mode']}: n={r['n']} MAE={r['mae_m']}m P90={r['p90_m']}m")
        print('Also check position_summary_common_rounds.csv: paired rounds only.')
        print('Report both solution_rate and error; missing CIR epochs are NOT zero-error predictions.')


if __name__=='__main__':
    main()
