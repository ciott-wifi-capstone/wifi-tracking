#!/usr/bin/env python3
"""Conservative frame-identity pairing of FTM_DETAIL and FTM Action33 CSI.

No false precision: token-unique alone means frame identity, NOT timebase sync.
If and only if timestamp patch is active, compare within-round t2/RX clock
*after* estimating a shared offset, and label preliminary timing coherence.
This does not calculate corrected distance; it validates prerequisites.
"""
import argparse
import csv
import statistics
from collections import Counter,defaultdict
from pathlib import Path

def read_csv(p):
    with p.open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))

def analyze(folder,coherence_ns=50):
    root=Path(folder)
    frames=read_csv(root/'csi_ftm_frame.csv')
    entries=read_csv(root/'ftm_detail.csv')
    by_key=defaultdict(list)
    for f in frames:
        # All records in this file were classified as public Action33 on Tag.
        by_key[(int(f['round_id']),int(f['dialog_token']))].append(f)
    entry_ctr=Counter((int(e['round_id']),int(e['dialog_token'])) for e in entries)
    results=[]
    for e in entries:
        rid,tok=int(e['round_id']),int(e['dialog_token'])
        records=by_key[(rid,tok)]
        status='NO_ACTION33_CSI'
        if len(records)>1 or entry_ctr[(rid,tok)]>1: status='AMBIGUOUS_TOKEN'
        elif len(records)==1: status='FRAME_TOKEN_UNIQUE_CLOCK_UNVALIDATED'
        item=dict(e)
        item.update(pair_status=status,csi_hw_ns='',csi_seq_ctl='',csi_precise='',offset_ps='',time_residual_ns='')
        if len(records)==1 and entry_ctr[(rid,tok)]==1:
            f=records[0]
            item['csi_hw_ns']=f['hw_rx_time_ns']
            item['csi_seq_ctl']=f['mac_sequence_control']
            item['csi_precise']=f['hw_timestamp_precise']
            if int(f['hw_timestamp_precise'])==1:
                item['offset_ps']=int(e['t2_ps'])-int(f['hw_rx_time_ns'])*1000
        results.append(item)
    # Estimate shared offset only for rounds with at least 4 unique token candidates.
    groups=defaultdict(list)
    for x in results:
        if x['offset_ps']!='':groups[int(x['round_id'])].append(x)
    for group in groups.values():
        if len(group)<4:continue
        med=statistics.median(int(x['offset_ps']) for x in group)
        for x in group:
            residual=abs(int(x['offset_ps'])-med)/1000
            x['time_residual_ns']=f'{residual:.3f}'
            if residual<=coherence_ns:
                x['pair_status']='TOKEN_UNIQUE_TIMING_COHERENT_PRELIMINARY'
            else:
                x['pair_status']='TOKEN_MATCH_TIMESTAMP_INCONSISTENT'
    out=root/'ftm_csi_frame_pairs.csv'
    heads=list(results[0]) if results else ['round_id','pair_status']
    with out.open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=heads)
        writer.writeheader();writer.writerows(results)
    stats=Counter(x['pair_status'] for x in results)
    print('FTM entries:',len(entries),'Action33 CSI frames:',len(frames))
    for label,count in sorted(stats.items()):print(' ',label,count)
    print('Output:',out)
    print('CAUTION: preliminary timing coherence is NOT independent validation of absolute ToF or corrected range.')
    if not frames:
        print('No Action33 CSI appeared: driver may not expose CSI for FTM responses. Do not claim same-frame CSI.')
    return stats

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('folder')
    ap.add_argument('--coherence-ns',type=float,default=50.0)
    args=ap.parse_args()
    analyze(args.folder,args.coherence_ns)
if __name__=='__main__':main()
