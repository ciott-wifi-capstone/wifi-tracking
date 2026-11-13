#!/usr/bin/env python3
"""Offline diagnosis for QAR-FTM v5: frame token matches vs clock consistency.

Usage:
  python diagnose_ftm_csi_clocks.py recordings/V5_TIMESTAMP_01

Read-only for measurement data; writes clock_diagnostic_by_round.csv in the trial folder.
No timestamp is deemed absolutely calibrated by this script.
"""
import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def load(path):
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def pct(xs, percentage):
    if not xs:
        return None
    ys = sorted(xs)
    j = (len(ys)-1)*percentage / 100.0
    i = int(math.floor(j))
    k = int(math.ceil(j))
    return ys[i]*(k-j) + ys[k]*(j-i) if i != k else ys[i]


def fmt(x):
    return 'n/a' if x is None else f'{x:,.3f}'


def main():
    p = argparse.ArgumentParser(description='QAR-FTM v5 offline per-round and cross-round clock check')
    p.add_argument('folder', help='One captured trial folder, e.g. recordings/V5_TIMESTAMP_01')
    args = p.parse_args()
    folder = Path(args.folder)
    pairs_path = folder / 'ftm_csi_frame_pairs.csv'
    frames_path = folder / 'csi_ftm_frame.csv'
    if not pairs_path.is_file() or not frames_path.is_file():
        p.error('Missing ftm_csi_frame_pairs.csv or csi_ftm_frame.csv. Run pair_ftm_action33.py first.')
    pairs = load(pairs_path)
    frames = load(frames_path)
    total = len(pairs)
    status = Counter(r.get('pair_status','') for r in pairs)
    cwb = Counter(r.get('cwb','?') for r in frames)
    csi_len = Counter(r.get('csi_len_bytes','?') for r in frames)
    precise = Counter(r.get('hw_timestamp_precise','?') for r in frames)
    print(f'FTM entries: {total}, FTM CSI frames: {len(frames)}')
    print('Pair statuses: ' + '; '.join(f'{k}={v}' for k,v in sorted(status.items())))
    print(f'CSI bandwidth indicator (cwb): {dict(cwb)}; CSI lengths: {dict(csi_len)}')
    print(f'Experimental timestamp flag: {dict(precise)}')
    good = [r for r in pairs if r.get('pair_status','')=='TOKEN_UNIQUE_TIMING_COHERENT_PRELIMINARY']
    print(f'Preliminary per-round pair rate: {len(good)}/{total} ({100*len(good)/total:.2f}%)' if total else 'No FTM entries')
    # Exact integers avoid loss of precision from large absolute timestamps.
    groups = defaultdict(list)
    for r in good:
        if not r.get('t2_ps') or not r.get('csi_hw_ns'):
            continue
        try:
            t2 = int(r['t2_ps']); hw = int(r['csi_hw_ns'])
            groups[int(r['round_id'])].append((t2 - hw*1000, hw, t2))
        except (TypeError, ValueError):
            continue
    report = []
    within = []
    all_offset_ps = []
    for rid, arr in sorted(groups.items()):
        offsets = [a[0] for a in arr]
        med_ps = median(offsets)
        residual_ns = [abs(x-med_ps)/1000 for x in offsets]
        within.extend(residual_ns)
        all_offset_ps.extend(offsets)
        report.append(dict(round_id=rid,pairs=len(arr),median_offset_ns=f'{med_ps/1000:.3f}',
                           within_round_p50_ns=f'{pct(residual_ns,50):.3f}',
                           within_round_p90_ns=f'{pct(residual_ns,90):.3f}',
                           first_hw_ns=min(a[1] for a in arr),last_hw_ns=max(a[1] for a in arr)))
    print('Round-level offset summary:')
    for r in report:
        print(f"  round={r['round_id']:>3} pairs={r['pairs']:>3} "
              f"median_offset_ns={r['median_offset_ns']:>18} "
              f"within_p90_ns={r['within_round_p90_ns']:>9}")
    print('Within-round residual: median=' + fmt(pct(within,50)) + ' ns, P90=' + fmt(pct(within,90)) + ' ns')
    if all_offset_ps:
        global_median_ps = median(all_offset_ps)
        global_resid_ns = [abs(x-global_median_ps)/1000 for x in all_offset_ps]
        print('One constant offset for ALL rounds: median residual='+fmt(pct(global_resid_ns,50)) +
              ' ns, P90='+fmt(pct(global_resid_ns,90))+' ns')
        if len(report)>=2:
            diffs_ns = [(float(r['median_offset_ns'])-float(report[0]['median_offset_ns'])) for r in report]
            print('Round-median offset change from first: min='+fmt(min(diffs_ns))+' ns, max='+fmt(max(diffs_ns))+' ns')
            print('If cross-round offset changes significantly, 5 ns within-round checks cannot validate one shared clock.')
    outfile=folder/'clock_diagnostic_by_round.csv'
    if report:
        with outfile.open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(report[0]));writer.writeheader();writer.writerows(report)
        print('Saved:',outfile)
    print('LIMIT: this script diagnoses relative consistency only, NOT absolute receive start time, CIR bias, or correct distance.')

if __name__ == '__main__':
    main()
