"""Reproduce the standalone RTT summaries and explanatory development figures.

Run beside the supplementary tables/ directory. The RTT values are transcribed
from meeting 4 notes, not reconstructed from a timestamped packet archive.
The development and CSI workflow drawings are explanatory, not measured data.
"""
from pathlib import Path
import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

ROOT = Path(__file__).resolve().parent
if ROOT.name == 'authoring': ROOT = ROOT.parent
OUT = ROOT / 'regenerated_standalone'
OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.size':10, 'axes.spines.top':False,
                     'axes.spines.right':False, 'savefig.dpi':300,
                     'font.family':'DejaVu Sans'})
BLUE, ORANGE, INK = '#185F86', '#D17721', '#25333D'

with (ROOT/'tables/standalone_rtt_readings.csv').open(encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
groups = []
for label in dict.fromkeys(r['condition_label'] for r in rows):
    rr = [r for r in rows if r['condition_label']==label]
    a = np.asarray([float(r['measured_m']) for r in rr])
    d = float(rr[0]['nominal_distance_m'])
    groups.append(dict(condition_label=label, nominal_distance_m=d,
        offset_state=rr[0]['offset_state'], n=len(a), mean_m=float(a.mean()),
        median_m=float(np.median(a)), min_m=float(a.min()), max_m=float(a.max()),
        standard_deviation_m=float(a.std(ddof=1)), mean_bias_m=float((a-d).mean()),
        mae_m=float(abs(a-d).mean()), rmse_m=float(np.sqrt(np.mean((a-d)**2)))))
assert [g['n'] for g in groups] == [10,10,10,11]
for destination in [ROOT/'tables/standalone_rtt_metrics.csv', OUT/'standalone_rtt_metrics.csv']:
    with destination.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(groups[0]));w.writeheader();w.writerows(groups)

def save(fig,name):
    for ext in ['png','pdf']:fig.savefig(OUT/(name+'.'+ext),bbox_inches='tight')
    plt.close(fig)

fig,axs=plt.subplots(1,2,figsize=(7.3,3.45),gridspec_kw={'width_ratios':[.92,1.08]})
for i,g in enumerate(groups[:2]):
    a=np.array([float(r['measured_m']) for r in rows if r['condition_label']==g['condition_label']])
    axs[0].scatter(i+np.linspace(-.09,.09,len(a)),a,s=27,color=[BLUE,ORANGE][i],alpha=.78,zorder=3)
    axs[0].plot([i-.17,i+.17],[g['mean_m']]*2,color=INK,lw=2,zorder=4)
    axs[0].text(i,g['mean_m']+(.30 if i==0 else .48),f"Mean {g['mean_m']:.3f} m",ha='center',fontsize=9)
axs[0].axhline(0,color='#8B949B',ls='--',lw=1)
axs[0].set(xticks=[0,1],xticklabels=['Before offset\nn = 10','After offset\nn = 10'],
           ylabel='Reported FTM distance (m)',ylim=(-.12,4.38),xlim=(-.52,1.65),
           title='(a) Nominal zero placement')
after=groups[1:]
for g in after:
    a=np.array([float(r['measured_m']) for r in rows if r['condition_label']==g['condition_label']])
    axs[1].scatter(g['nominal_distance_m']+np.linspace(-.035,.035,len(a)),a,
                   s=25,color=BLUE,alpha=.65,zorder=3)
axs[1].plot([0,1.15],[0,1.15],ls='--',color='#818A91',label='Ideal identity',lw=1.2)
axs[1].plot([g['nominal_distance_m'] for g in after],[g['mean_m'] for g in after],
            color=ORANGE,marker='D',markersize=5,label='Mean after offset',lw=1.8,zorder=4)
axs[1].set(xlabel='Nominal antenna separation (m)',ylabel='Reported FTM distance (m)',
           xlim=(-.10,1.15),ylim=(-.04,1.62),xticks=[0,.5,1.0],title='(b) Short-range transfer')
axs[1].legend(loc='lower right',fontsize=8.3,frameon=False)
for ax in axs:ax.grid(axis='y',color='#E4E8EB',lw=.65);ax.set_axisbelow(True)
fig.tight_layout(w_pad=1.2);save(fig,'f41_standalone_rtt_calibration')

fig,ax=plt.subplots(figsize=(7.3,4.25));ax.set(xlim=(0,10),ylim=(.7,6.22));ax.axis('off')
stages=[
 ('1  RTT alone','FTM distance, status and calibration','Local offset improves; transfer still fails'),
 ('2  CSI alone','Sender, receiver and complex subcarrier records','Channel structure becomes observable'),
 ('3  RTT + CSI','One application collects neighbouring observations','Traffic and packet identity need control'),
 ('4  RTT + CIR','Matched full CSI, relative CIR and range correction','Evaluate baselines, transfer and error tails')]
for i,(title,desc,outcome) in enumerate(stages):
    y=5.0-i*1.35
    ax.add_patch(Rectangle((.18,y),9.62,1.02,facecolor='#F2F5F7',edgecolor='#9AAEBB',lw=1.0))
    ax.text(.45,y+.68,title,fontsize=11.3,weight='bold',color=INK,va='center')
    ax.text(3.1,y+.68,desc,fontsize=10.1,color=INK,va='center')
    ax.text(3.1,y+.28,outcome,fontsize=9.7,color=BLUE,va='center')
    if i<3:ax.add_patch(FancyArrowPatch((5,y-.02),(5,y-.31),arrowstyle='-|>',mutation_scale=13,color='#607987',lw=1.3))
save(fig,'f31_development_sequence')

fig,ax=plt.subplots(figsize=(7.3,3.5));ax.set(xlim=(0,10),ylim=(0,4.6));ax.axis('off')
top=[(.1,'Sender','ESP32-S3\ncsi_send\nESP-NOW packets'),
     (2.65,'Radio channel','Received packet\ntraining fields'),
     (5.2,'Receiver','ESP32-S3\ncsi_recv\nCSI callback'),
     (7.75,'UART record','Signed IQ bytes\npacket metadata')]
bottom=[(7.75,'Record checks','Array format\nsource and time'),
        (5.2,'Decode IQ','Imaginary / real\nsubcarrier order'),
        (2.65,'Host plots','Amplitude, phase\nIQ plane'),
        (.1,'Interpretation','Channel features\nneed calibration')]
def node(x,y,title,body):
    ax.add_patch(Rectangle((x,y),2.12,1.25,facecolor='#F2F5F7',edgecolor='#9AAEBB',lw=1))
    ax.text(x+1.06,y+.95,title,ha='center',va='center',fontsize=9.3,weight='bold',color=INK)
    ax.text(x+1.06,y+.43,body,ha='center',va='center',fontsize=8.8,color=INK,linespacing=1.25)
for x,t,b in top:node(x,3.04,t,b)
for x,t,b in bottom:node(x,.70,t,b)
for x in [.1,2.65,5.2]:ax.add_patch(FancyArrowPatch((x+2.16,3.66),(x+2.49,3.66),arrowstyle='-|>',mutation_scale=11,color='#607987'))
for x in [7.75,5.2,2.65]:ax.add_patch(FancyArrowPatch((x-.04,1.32),(x-.39,1.32),arrowstyle='-|>',mutation_scale=11,color='#607987'))
ax.add_patch(FancyArrowPatch((8.81,3.0),(8.81,2.0),arrowstyle='-|>',mutation_scale=12,color='#607987'))
ax.text(4.4,2.38,'Retain packet identity and complete arrays\nbefore interpretation',ha='center',fontsize=9.3,color=BLUE,linespacing=1.25)
save(fig,'f33_standalone_csi_workflow')
print('Reproduced 41 standalone RTT readings, four summaries and three figures.')
