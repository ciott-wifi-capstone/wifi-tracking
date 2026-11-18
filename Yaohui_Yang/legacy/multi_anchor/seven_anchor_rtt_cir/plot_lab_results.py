#!/usr/bin/env python3
"""Optional plots of actual (not simulated) lab data; pip install matplotlib."""
import argparse
import csv
from pathlib import Path
import numpy as np

def read(path):
    with path.open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--results',type=Path,default=Path(__file__).resolve().parent/'lab_results')
    args=p.parse_args()
    import matplotlib.pyplot as plt
    root=args.results;root.mkdir(parents=True,exist_ok=True)
    ds=read(root/'link_dataset.csv')
    valid=[r for r in ds if r['cir_peak_ratio'] and r['cir_rms_delay_ns']]
    if not valid:
        raise ValueError('No matched CIR features: check bssid, CSI LLTF and time alignment')
    for col, xlabel, out in [
        ('cir_peak_ratio','CIR dominant peak energy ratio','peak_vs_error.png'),
        ('cir_rms_delay_ns','CIR relative RMS delay spread (ns)','spread_vs_error.png'),
        ('cir_entropy','CIR normalized entropy','entropy_vs_error.png'),
        ('cir_offpeak_ratio','CIR energy outside +/-1 peak bins','offpeak_vs_error.png'),
    ]:
        fig,ax=plt.subplots(figsize=(7.5,4.8))
        for condition in sorted(set(r['condition'] for r in valid)):
            sub=[r for r in valid if r['condition']==condition and r['split']=='test']
            if sub:
                ax.scatter([float(r[col]) for r in sub],
                           [float(r['abs_error_m']) for r in sub],s=12,alpha=.35,label=condition)
        ax.set(xlabel=xlabel,ylabel='Absolute FTM ranging error (m)',title='Held-out TEST sessions only')
        ax.grid(alpha=.25);ax.legend()
        fig.tight_layout();fig.savefig(root/out,dpi=160);plt.close(fig)
    ps=read(root/'position_detail.csv')
    for split in ('train','test'):
        fig,ax=plt.subplots(figsize=(7.5,4.8))
        for mode in ('rtt','csi','cir','hybrid'):
            a=sorted(float(r['raw_error_m']) for r in ps if r['split']==split and r['mode']==mode)
            if a:
                ax.plot(a,np.arange(1,len(a)+1)/len(a),label=f'{mode} (n={len(a)})')
        ax.set(xlabel='Position error (m)',ylabel='Empirical CDF',title=f'Localization errors: {split} sessions')
        ax.set_ylim(0,1);ax.legend();ax.grid(alpha=.25)
        fig.tight_layout();fig.savefig(root/f'position_cdf_{split}.png',dpi=160);plt.close(fig)
    print('Plots generated at',root)

if __name__=='__main__':main()
