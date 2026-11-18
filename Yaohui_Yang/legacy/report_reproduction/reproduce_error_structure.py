"""Reproduce signed-error and bias/dispersion diagnostics without model refitting.

Inputs are the existing round prediction CSV and unchanged early raw archive.
The plotted points are recorded model outputs, not simulated trajectories.
"""
from pathlib import Path
import csv,io,json,zipfile
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parent
if ROOT.name=='authoring':ROOT=ROOT.parent
OUT=ROOT/'regenerated_error_structure'
OUT.mkdir(exist_ok=True)

def write(path,rows):
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

predictions=list(csv.DictReader((ROOT/'tables/all_session_predictions.csv').open(encoding='utf-8-sig')))
hybrid=[r for r in predictions if r['model']=='cir_residual']
raw={ (r['split'],r['trial'],r['round_id']):r for r in predictions if r['model']=='raw_ftm' }
assert len(hybrid)==638 and len(raw)==638
rows=[]
distances=[1,2,3,4,6,9,12,15]
for split in ['training_fit','validation_selection']:
    for d in distances:
        rr=[r for r in hybrid if r['split']==split and float(r['true_distance_m'])==d]
        e=np.array([float(r['signed_error_m']) for r in rr])
        er=np.array([float(raw[split,r['trial'],r['round_id']]['signed_error_m']) for r in rr])
        n=len(e);bias=float(e.mean());sd=float(e.std(ddof=1));mse=float(np.mean(e*e))
        assert np.isclose(mse,bias*bias+(n-1)/n*sd*sd,atol=1e-10,rtol=1e-12)
        rows.append(dict(split=split,trial=rr[0]['trial'],true_distance_m=d,n=n,
            raw_bias_m=float(er.mean()),raw_sd_m=float(er.std(ddof=1)),
            hybrid_bias_m=bias,hybrid_sd_m=sd,hybrid_rmse_m=float(np.sqrt(mse)),
            hybrid_mae_m=float(np.mean(abs(e))),hybrid_min_error_m=float(e.min()),
            hybrid_max_error_m=float(e.max()),hybrid_squared_bias_fraction=bias*bias/mse))
assert sum(r['n'] for r in rows if r['split']=='training_fit')==318
assert sum(r['n'] for r in rows if r['split']=='validation_selection')==320
assert sum(int(r['paired_cir_frames']) for r in hybrid)==18625
for p in [ROOT/'tables/capture_error_structure.csv',OUT/'capture_error_structure.csv']:write(p,rows)

early=[]
with zipfile.ZipFile(ROOT/'raw/early_recordings.zip') as z:
    for name in z.namelist():
        if not name.endswith('/ftm.csv'):continue
        metadata=json.loads(z.read(name.replace('ftm.csv','metadata.json')))
        records=list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
        reference=float(metadata['true_distance_m'])
        e=np.array([int(r['dist_est_cm'])/100-reference for r in records if int(r['status'])==0])
        n=len(e);b=float(e.mean());s=float(e.std(ddof=1));mse=float(np.mean(e*e))
        assert np.isclose(mse,b*b+(n-1)/n*s*s,atol=1e-10,rtol=1e-12)
        early.append(dict(trial=metadata['trial'],condition=metadata['condition'],
            true_distance_m=reference,n=n,bias_m=b,sample_sd_m=s,rmse_m=float(np.sqrt(mse)),
            squared_bias_fraction=b*b/mse))
assert len(early)==10 and sum(r['n'] for r in early)==414
for p in [ROOT/'tables/early_bias_variance.csv',OUT/'early_bias_variance.csv']:write(p,early)

plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,
                     'savefig.dpi':300,'font.family':'DejaVu Sans'})
fig,axes=plt.subplots(1,2,figsize=(7.3,3.5))
rng=np.random.default_rng(73)
for ax,split,title,color in zip(axes,['training_fit','validation_selection'],
        ['A: fitting captures','B: selection-validation captures'],['#185F86','#D17721']):
    samples=[np.array([float(r['signed_error_m']) for r in hybrid
                      if r['split']==split and float(r['true_distance_m'])==d]) for d in distances]
    xx=np.arange(len(distances))
    box=ax.boxplot(samples,positions=xx,widths=.52,whis=(0,100),showfliers=False,
                   patch_artist=True,manage_ticks=False,
                   medianprops={'color':'#25333D','linewidth':1.25},
                   whiskerprops={'color':color,'linewidth':.9},
                   capprops={'color':color,'linewidth':.9})
    for patch in box['boxes']:patch.set(facecolor=color,edgecolor=color,alpha=.22)
    for x,values in zip(xx,samples):
        ax.scatter(x+rng.uniform(-.15,.15,len(values)),values,s=8,color=color,alpha=.48,zorder=3,linewidths=0)
    ax.axhline(0,color='#7C858C',ls='--',lw=1,zorder=1)
    lo=min(0,min(float(a.min()) for a in samples));hi=max(0,max(float(a.max()) for a in samples));pad=.09*(hi-lo)
    ax.set(xlim=(-.55,7.55),ylim=(lo-pad,hi+pad),xticks=xx,xticklabels=[str(d) for d in distances],
           title=title,xlabel='Nominal separation (m)',ylabel='Signed hybrid range error (m)')
    ax.grid(axis='y',color='#E4E8EB',lw=.6);ax.set_axisbelow(True)
fig.tight_layout(w_pad=1.5)
for suffix in ['png','pdf']:fig.savefig(OUT/f'f48_capture_error_structure.{suffix}',bbox_inches='tight')
plt.close(fig)

# Redraw the existing CDF with the same record unit used throughout the thesis.
# The archival single_link/ reproduction package remains byte-for-byte intact.
models = ['raw_ftm','constant_training_mean','ftm_affine','cir_direct','cir_residual']
labels = ['Raw FTM','Constant 6.5 m','FTM affine','CIR only','FTM + CIR']
colors = ['#666666','#999999','#D97706','#167D8D','#7654A3']
fig,ax=plt.subplots(figsize=(7.3,4.4),layout='constrained')
for kind,label,color in zip(models,labels,colors):
    error=np.sort([float(r['absolute_error_m']) for r in predictions
                   if r['split']=='validation_selection' and r['model']==kind])
    assert len(error)==320
    ax.step(np.r_[0.,error],np.r_[0.,np.arange(1,len(error)+1)/len(error)],
            where='post',color=color,lw=1.8,label=label,
            linestyle='--' if kind=='constant_training_mean' else '-')
ax.set(xlabel='Absolute ranging error (m)',ylabel='Empirical CDF',
       title='B selection-validation: all 320 usable rounds',ylim=(0,1.02),xlim=(0,None))
ax.grid(color='#E4E8EB',lw=.6);ax.set_axisbelow(True)
ax.legend(fontsize=9,loc='lower right')
for suffix in ['png','pdf']:fig.savefig(OUT/f'f45_error_cdf.{suffix}',bbox_inches='tight')
plt.close(fig)
print('Verified 638 hybrid predictions and 414 early reports; reproduced 16 capture diagnostics and two figures.')
