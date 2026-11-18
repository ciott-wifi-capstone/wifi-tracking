#!/usr/bin/env python3
"""Summarize LOS/NLOS ranging errors from all trials under recordings/ (stdlib only)."""
import argparse, csv, math, statistics
from pathlib import Path


def percentile(xs, p):
    if not xs: return math.nan
    xs=sorted(xs)
    index=(len(xs)-1)*p/100
    lo=int(math.floor(index)); hi=int(math.ceil(index))
    return xs[lo]+(xs[hi]-xs[lo])*(index-lo)


def stats_one(folder):
    import json
    meta=json.loads((folder/'metadata.json').read_text(encoding='utf8'))
    with (folder/'ftm.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    errors=[]
    for row in rows:
        if int(row['status'])!=0: continue
        e=float(row['signed_error_m'])
        if math.isfinite(e): errors.append(e)
    with (folder/'csi.csv').open(encoding='utf-8-sig', newline='') as f:
        packets=sum(1 for _ in csv.DictReader(f))
    a=[abs(e) for e in errors]
    return {'trial':meta['trial'],'condition':meta['condition'],
            'blocker':meta.get('blocker',''), 'true_distance_m':meta['true_distance_m'],
            'ftm_total':len(rows),'ftm_success':len(errors),
            'success_rate':len(errors)/len(rows) if rows else math.nan,
            'csi_packets':packets,
            'bias_m':statistics.mean(errors) if errors else math.nan,
            'mae_m':statistics.mean(a) if a else math.nan,
            'rmse_m':math.sqrt(statistics.mean([e*e for e in errors])) if errors else math.nan,
            'median_ae_m':statistics.median(a) if a else math.nan,
            'p90_ae_m':percentile(a,90)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root', default='recordings')
    p.add_argument('--output',default='results/basic_error_summary.csv')
    args=p.parse_args()
    folders=sorted(f.parent for f in Path(args.root).rglob('metadata.json'))
    stats=[stats_one(f) for f in folders if (f/'ftm.csv').exists() and (f/'csi.csv').exists()]
    if not stats: raise SystemExit('No completed trial folders found')
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(stats[0])); w.writeheader(); w.writerows(stats)
    for s in stats:
        print(f"{s['trial']:24s} {s['condition']:4s} true={s['true_distance_m']:5.2f}m "
              f"OK={s['ftm_success']}/{s['ftm_total']} CSI={s['csi_packets']} "
              f"MAE={s['mae_m']:.3f}m P90={s['p90_ae_m']:.3f}m")
    print('Saved',output)

if __name__=='__main__':main()
