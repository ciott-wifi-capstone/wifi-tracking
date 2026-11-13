#!/usr/bin/env python3
"""DIAGNOSTIC ONLY: FTM t2 vs CSI MAC RX hardware timestamp.

Never calls a microsecond-only association an exact packet match.
Without the validated patched PHY receive timestamp, precision is insufficient
for the Wi-PRO time-offset correction.
"""
import argparse,csv
from pathlib import Path

U32=2**32

def wrapped_diff_ns(a,b):
    # FTM t2 and rx timestamp can use different hardware wrap widths;
    # modulo 32-bit microseconds only for nearest-window preliminary checks.
    a_us=a/1000.0
    b_us=b/1000.0
    return (((a_us-b_us+U32/2)%U32)-U32/2)*1000.0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('trial_folder',type=Path)
    ap.add_argument('--output',type=Path)
    args=ap.parse_args()
    folder=args.trial_folder
    with (folder/'ftm_detail.csv').open(encoding='utf-8-sig',newline='') as f:
        ftms=list(csv.DictReader(f))
    with (folder/'csi_ftm_hw.csv').open(encoding='utf-8-sig',newline='') as f:
        csis=list(csv.DictReader(f))
    by_round={}
    for c in csis: by_round.setdefault(int(c['round_id']),[]).append(c)
    out=[]
    for x in ftms:
        rid=int(x['round_id']); stamp=int(x['t2_ps']) / 1000.0
        found=sorted([(abs(wrapped_diff_ns(stamp,int(c['hw_rx_time_ns']))),wrapped_diff_ns(stamp,int(c['hw_rx_time_ns'])),c)
                      for c in by_round.get(rid,[])],key=lambda z:z[0])
        if not found: status='NO_CSI_IN_FTM_WINDOW'; delta=''; candidate=''; prec=''
        else:
            _,delta,c=found[0]; candidate=c['hw_rx_time_ns'];prec=c['hw_timestamp_precise']
            # Association must be unique in ±2 ns and precision field must be high.
            eligible=[y for y in found if y[0]<=2.0]
            if prec=='1' and len(eligible)==1 and abs(delta)<=2.0:
                status='HARDWARE_TIME_CANDIDATE_CHECK_FRAME_ID'
            elif prec=='1': status='NO_UNIQUE_2NS_MATCH'
            else: status='COARSE_US_ONLY_NOT_EXACT'
        out.append(dict(round_id=rid,ftm_entry_idx=x['entry_idx'],ftm_t2_ps=x['t2_ps'],
                        nearest_csi_hw_ns=candidate,delta_ns=delta,csi_ns_precision_flag=prec,pair_status=status))
    target=args.output or folder/'pair_candidates.csv'
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(out[0]) if out else ['round_id','pair_status'])
        w.writeheader();w.writerows(out)
    from collections import Counter
    print('Analyzed',len(out),'FTM entries;',len(csis),'CSI records')
    for k,v in Counter(row['pair_status'] for row in out).items():print(' ',k,v)
    print('Saved',target)
    print('IMPORTANT: hardware-time candidate is NOT validated as the same Wi-Fi frame without frame ID cross-check.')

if __name__=='__main__':main()
