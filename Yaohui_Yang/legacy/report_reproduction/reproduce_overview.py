"""Reproduce descriptive early-protocol, reported-GRU and geometry figures.

Run from the unpacked supplementary directory. This script does not retrain GRU.
The separate single_link/reproduce_results.py verifies the audited CIR models.
"""
from pathlib import Path
import csv,io,json,zipfile
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'regenerated_overview'
OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,
                     'savefig.dpi':300,'axes.prop_cycle':plt.cycler(color=['#185F86','#D17721'])})
def write(name,rows):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def save(fig,name):
    fig.savefig(OUT/(name+'.png'),bbox_inches='tight');fig.savefig(OUT/(name+'.pdf'),bbox_inches='tight');plt.close(fig)

early=[]
with zipfile.ZipFile(ROOT/'raw/early_recordings.zip') as z:
    for name in z.namelist():
        if not name.endswith('/ftm.csv'):continue
        rows=list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
        meta=json.loads(z.read(name.replace('ftm.csv','metadata.json')))
        good=[r for r in rows if int(r['status'])==0]
        d=np.asarray([int(r['dist_est_cm'])/100 for r in good]);e=d-float(meta['true_distance_m'])
        early.append(dict(trial=meta['trial'],condition=meta['condition'],true_distance_m=meta['true_distance_m'],logged_reports=len(rows),successful_reports=len(good),median_m=float(np.median(d)),mae_m=float(np.mean(abs(e))),rmse_m=float(np.sqrt(np.mean(e*e))),standard_deviation_m=float(np.std(d,ddof=1))))
assert len(early)==10 and sum(r['logged_reports'] for r in early)==414
write('early_protocol_metrics.csv',early)
fig,ax=plt.subplots(figsize=(7.1,3.4));xx=np.arange(3);w=.32
for i,c in enumerate(['LOS','NLOS']):
    y=[next(r['mae_m'] for r in early if r['condition']==c and r['true_distance_m']==d) for d in [3,6,9]]
    bars=ax.bar(xx+(i-.5)*w,y,w,label=c);ax.bar_label(bars,fmt='%.2f',padding=3)
ax.set(xticks=xx,xticklabels=['3 m','6 m','9 m'],xlabel='Actual antenna separation',ylabel='Mean absolute range error (m)',ylim=(0,13.4));ax.legend(frameon=False);save(fig,'early_los_nlos')

gru=list(csv.DictReader((ROOT/'tables/reported_gru_v2_metrics.csv').open(encoding='utf-8-sig')))
fig,ax=plt.subplots(figsize=(7.1,3.25));xx=np.arange(2)
for i,(field,label) in enumerate([('raw_mae_m','Raw FTM'),('gru_mae_m','GRU V2 corrected')]):
    b=ax.bar(xx+(i-.5)*w,[float(r[field]) for r in gru],w,label=label);ax.bar_label(b,fmt='%.3f',padding=3)
ax.set(xticks=xx,xticklabels=[r['split'] for r in gru],ylabel='Reported range MAE (m)',ylim=(0,10.5));ax.legend(loc='upper right',frameon=False);save(fig,'reported_gru_comparison')

anchors=list(csv.DictReader((ROOT/'tables/later_anchor_coordinates.csv').open(encoding='utf-8-sig')))
xy=np.array([[float(a['x_m']),float(a['y_m'])] for a in anchors]);geo=[]
for x in np.linspace(21.58,47.22,400):
    delta=np.array([x,6.415])-xy;j=delta/np.linalg.norm(delta,axis=1)[:,None];info=j.T@j;c=np.linalg.inv(info)
    geo.append(dict(x_m=float(x),y_m=6.415,sigma_x_per_unit_range_sigma=float(np.sqrt(c[0,0])),sigma_y_per_unit_range_sigma=float(np.sqrt(c[1,1])),information_condition=float(np.linalg.cond(info))))
write('planar_geometry_diagnostic.csv',geo)
fig,ax=plt.subplots(figsize=(7.1,3.1))
for field,label in [('sigma_x_per_unit_range_sigma','X direction'),('sigma_y_per_unit_range_sigma','Y direction')]:ax.plot([r['x_m'] for r in geo],[r[field] for r in geo],label=label)
ax.set(xlabel='Position along reference route X (m)',ylabel='First-order error amplification',ylim=(0,3.6));ax.legend(frameon=False);save(fig,'planar_geometry_diagnostic')
print('Reproduced early measurement metrics and descriptive overview figures.')
print('GRU values are reported aggregates; geometry is an idealised planar calculation.')
