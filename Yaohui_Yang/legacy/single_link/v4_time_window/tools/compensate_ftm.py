#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QAR-FTM v3: ESP32-S3 FTM+CSI bias correction using previously recorded measurements.

FTM timestamp offsets cannot be recovered unconditionally from normal downlink
CSI. This tool performs TRAINED statistical correction; it is NOT Wi-PRO TOF
reconstruction. Cross-validation is by entire trial, never random packet split.

Commands:
 python tools/compensate_ftm.py evaluate --input recordings2.zip --input recordings3.zip --out evaluation
 python tools/compensate_ftm.py train --input recordings2.zip --input recordings3.zip --out model.json
 python tools/compensate_ftm.py apply --model model.json --input NEW_RECORDINGS.zip --out corrected

No true distance is used by the model at prediction time. ``true_distance_m``
is used ONLY to train, evaluate, and print errors. Do not claim that performance
on training trials reflects generalization to new environments.
"""
import argparse, csv, io, json, math, zipfile, collections
from pathlib import Path
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge

C=0.000149896229 # meters/ps, TWO-way RTT
# Keep model small: 12 independent placements does NOT support dozens of features
FEATS=['raw_m','frame_p25_m','frame_mad_m','ftm_rssi_dbm','csi_rssi_dbm','amp_cv','freq_ripple','cir_entropy']
# Leave distance features in physical scale, standardized later.

def floatval(v, default=np.nan):
    try:return float(v)
    except (TypeError,ValueError):return default

def records(s):
    return list(csv.DictReader(io.StringIO(s))) if s else []

def trial_files(paths):
    for src in paths:
        p=Path(src)
        if p.is_file() and p.suffix.lower()=='.zip':
            with zipfile.ZipFile(p) as z:
                gs=collections.defaultdict(dict)
                for entry in z.namelist():
                    base=Path(entry).name
                    if base in ('metadata.json','ftm.csv','ftm_entries.csv','ftm_detail.csv','csi.csv'):
                        group=Path(entry).parent.name
                        gs[group][base]=z.read(entry).decode('utf-8-sig',errors='replace')
                for t,d in sorted(gs.items()):
                    if 'ftm.csv' in d and 'metadata.json' in d:yield f'{p.stem}/{t}',d
        elif p.is_dir():
            for d in sorted(p.iterdir()):
                if d.is_dir() and (d/'ftm.csv').exists() and (d/'metadata.json').exists():
                    yield f'{p.name}/{d.name}', {n:(d/n).read_text(encoding='utf-8-sig') for n in ('ftm.csv','ftm_entries.csv','ftm_detail.csv','csi.csv','metadata.json') if (d/n).exists()}
        else:raise FileNotFoundError(src)

def channel_features(row):
    # Only decode 128-byte LLTF for now; 384-byte HT40 needs different indexing.
    try:
        if int(row.get('len_bytes',0))!=128:return None
        buf=bytes.fromhex(row['csi_hex'])
        if len(buf)!=128:return None
        z=np.frombuffer(buf,dtype=np.int8).astype(float)
        h=z[1::2]+1j*z[0::2]  # Espressif CSI: imag first, real next
        if int(row.get('first_word_invalid',0)):h[:2]=0
        amp=np.abs(h)
        valid=amp[amp>0]
        if len(valid)<32:return None
        cv=np.std(valid)/(np.mean(valid)+1e-9)
        ripple=np.median(np.abs(np.diff(valid)))/(np.mean(valid)+1e-9)
        cir=np.fft.ifft(h)
        p=np.abs(cir)**2
        q=p/(p.sum()+1e-12)
        entropy=-np.sum(q*np.log(q+1e-12))/np.log(len(q))
        return [cv,ripple,entropy]
    except (ValueError,KeyError):return None

def trial_rows(name, files):
    meta=json.loads(files['metadata.json']); truth=floatval(meta.get('true_distance_m'))
    fr=collections.defaultdict(list)
    for e in records(files.get('ftm_entries.csv')):
        v=floatval(e.get('rtt_ps'))
        if math.isfinite(v) and 0<=v<4294967295:fr[str(e.get('round_id'))].append(v*C)
    cr=collections.defaultdict(list)
    for e in records(files.get('csi.csv')):
        f=channel_features(e)
        if f:
            f.append(floatval(e.get('rssi_dbm')))
            cr[str(e.get('round_id'))].append(f)
    result=[]
    for e in records(files.get('ftm.csv')):
        if str(e.get('status'))!='0':continue
        raw=floatval(e.get('estimated_distance_m'))
        if not np.isfinite(raw) or not 0<=raw<=100:continue
        roundid=str(e.get('round_id'))
        frame=np.asarray(fr.get(roundid,[]),dtype=float)
        channel=np.asarray(cr.get(roundid,[]),dtype=float)
        if len(frame)<4 or len(channel)<3:continue
        med=np.median(frame)
        p25=np.percentile(frame,25)
        mad=np.median(np.abs(frame-med))
        cf=np.median(channel,axis=0)
        x=[raw,p25,mad,floatval(e.get('ftm_mean_rssi_dbm')),*cf]
        if not np.all(np.isfinite(x)):continue
        result.append({'trial':name,'round_id':roundid,'truth_m':truth,**dict(zip(FEATS,map(float,x)))})
    return result

def load(paths):
    rs=[]
    for name,fs in trial_files(paths):rs.extend(trial_rows(name,fs))
    if not rs:raise ValueError('No matched FTM + CSI round samples found. Check paths/format.')
    return rs

def getX(rows):return np.asarray([[r[k] for k in FEATS] for r in rows],float)
def gety(rows):return np.asarray([r['truth_m'] for r in rows],float)

def fit(rows,mode='hybrid',alpha=20):
    """Train residual FTM bias, equal weight per placement, shrink correction."""
    x=getX(rows); truth=gety(rows)
    if mode=='offset':
        groups=collections.defaultdict(list)
        for r in rows:groups[r['trial']].append(r['raw_m']-r['truth_m'])
        b=float(np.mean([np.median(v) for v in groups.values()]))
        return {'mode':'offset','bias_m':b,'n_trials':len(groups)}
    if mode=='raw_linear':use=[0]
    elif mode=='hybrid':use=list(range(len(FEATS)))
    else:raise ValueError(mode)
    x=x[:,use]; raw=getX(rows)[:,0]
    # predict calibrated distance, but penalty favors correction of raw reference
    residual=truth-raw
    groups=collections.Counter(r['trial'] for r in rows)
    weights=np.asarray([1.0/groups[r['trial']] for r in rows])
    scaler=StandardScaler().fit(x,sample_weight=weights)
    xx=scaler.transform(x)
    model=Ridge(alpha=alpha,fit_intercept=True).fit(xx,residual,sample_weight=weights*len(groups))
    return {'mode':mode,'features':[FEATS[i] for i in use],'center':scaler.mean_.tolist(),
      'scale':scaler.scale_.tolist(),'coef':model.coef_.tolist(),'intercept':float(model.intercept_),
      'n_trials':len(groups),'alpha':alpha,'bias_limit_m':12.0,
      'raw_min_m':float(np.quantile(raw,0.01)),
      'raw_max_m':float(np.quantile(raw,0.99)),
      'true_min_m':float(np.min(truth)), 'true_max_m':float(np.max(truth))}

def predict(model, rows):
    raw=np.asarray([r['raw_m'] for r in rows],float)
    if model['mode']=='offset':delta=-float(model['bias_m'])*np.ones(len(rows))
    else:
        keys=model['features']
        x=np.asarray([[r[k] for k in keys] for r in rows],float)
        xx=(x-np.asarray(model['center']))/np.asarray(model['scale'])
        delta=np.asarray(model['coef'])@xx.T + float(model['intercept'])
        delta=np.clip(delta,-float(model.get('bias_limit_m',12)),float(model.get('bias_limit_m',12)))
    return np.maximum(0,raw+delta)

def grouped_cv(rows,mode,alpha):
    trials=sorted(set(r['trial'] for r in rows)); pred=[];truth=[];groups=[];raws=[]
    for group in trials:
        train=[r for r in rows if r['trial']!=group]
        test=[r for r in rows if r['trial']==group]
        model=fit(train,mode,alpha)
        yhat=predict(model,test)
        pred.extend(yhat);truth.extend(gety(test));raws.extend(r['raw_m'] for r in test);groups.extend([group]*len(test))
    return np.asarray(pred),np.asarray(truth),np.asarray(raws),groups

def summarize(p,t,groups=None):
    errors=np.abs(p-t)
    if groups is None:
        return {'MAE':round(float(np.mean(errors)),3),'P90':round(float(np.percentile(errors,90)),3)}
    # evaluate median error of each placement, avoids one trial dominating
    data=collections.defaultdict(list)
    for yhat,y,g in zip(p,t,groups):data[g].append(abs(yhat-y))
    per=[np.mean(vals) for vals in data.values()]
    return {'trial_macro_MAE':round(float(np.mean(per)),3),
            'trial_macro_P90':round(float(np.percentile(per,90)),3),
            'session_MAE':round(float(np.mean(errors)),3),
            'sessions':len(p),'independent_placements':len(data)}

def writecsv(dest, rows):
    if not rows:return
    with Path(dest).open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,list(rows[0]));writer.writeheader();writer.writerows(rows)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='command',required=True)
    for name in ['evaluate','train','apply']:
        p=sub.add_parser(name);p.add_argument('--input',required=True,action='append',help='ZIP or recordings directory (repeat as needed)');p.add_argument('--out',required=True)
        if name=='apply':p.add_argument('--model',required=True)
        if name=='train':p.add_argument('--mode',choices=['raw_linear','hybrid','offset'],default='raw_linear')
    args=ap.parse_args();rs=load(args.input)
    if args.command=='evaluate':
        out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
        # Fixed beforehand; no selecting the best based on CV for a final scientific claim
        methods=[('offset',0),('raw_linear',20),('hybrid',20)]
        results=[];outs=[]
        for mode,alpha in methods:
            p,y,raw,g=grouped_cv(rs,mode,alpha)
            report=summarize(p,y,g);results.append({'method':mode,**report})
            for v,truth,base,group in zip(p,y,raw,g):outs.append({'method':mode,'trial':group,'true_distance_m':truth,'raw_m':base,'predicted_m':round(float(v),4),'error_m':round(float(v-truth),4)})
        truth=gety(rs);raw=getX(rs)[:,0];report=summarize(raw,truth,[r['trial'] for r in rs]);results.insert(0,{'method':'raw_FTM',**report})
        writecsv(out/'cv_summary.csv',results);writecsv(out/'cv_predictions.csv',outs)
        print(json.dumps(results,indent=2,ensure_ascii=False))
        print('All corrections are out-of-trial predictions, no random session leakage.')
    elif args.command=='train':
        model=fit(rs,args.mode,20)
        model['training_sources']=args.input
        model['warning']='Calibrated only on seen data; not hardware/propagation de-embedding. Absolute accuracy not guaranteed.'
        Path(args.out).write_text(json.dumps(model,indent=2,ensure_ascii=False),encoding='utf-8')
        print('Model written:',args.out,'mode=',args.mode)
    elif args.command=='apply':
        model=json.loads(Path(args.model).read_text(encoding='utf-8'))
        if model['mode'] not in ('offset','raw_linear','hybrid'):raise ValueError('Invalid model mode')
        x=predict(model,rs);out=[]
        for r,pred in zip(rs,x):
            rec={k:r[k] for k in ['trial','round_id','raw_m']};rec['corrected_m']=round(float(pred),4)
            # Ground truth not used by prediction. Keep it for optional offline checks only.
            out.append(rec)
        writecsv(args.out,out);print('Predictions written:',args.out)
        print('CAUTION: This does NOT suppress propagation delays like Wi-PRO; it is a regression-based bias correction.')

if __name__=='__main__':main()
