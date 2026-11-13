#!/usr/bin/env python3
"""Decode 20MHz LLTF CSI and extract RELATIVE CIR dispersion features (NumPy)."""
import argparse, csv, math
from pathlib import Path
import numpy as np


def features_from_hex(rawhex: str, first_word_invalid: int):
    # IDF stores subcarriers in FFT-bin order: 0..31, -32..-1; each complex int8 = [imag, real].
    b = bytes.fromhex(rawhex)
    if len(b) != 128:
        return None   # strictly 20 MHz LLTF (64 complex carriers)
    arr=np.frombuffer(b,dtype=np.int8).astype(np.float64)
    H=arr[1::2] + 1j*arr[0::2]
    # Invalid first 4 BYTES = first 2 COMPLEX tones. Do not drop/shift bin indices.
    if first_word_invalid: H[:2]=0
    h=np.fft.ifft(H, n=64)
    power=np.abs(h)**2
    total=power.sum()
    if total<=0: return None
    peak=int(np.argmax(power))
    # Shift the circular CIR relative to the strongest tap for descriptive features.
    aligned=np.roll(power, -peak)/total
    bins=np.arange(64, dtype=float)
    mean=float(np.sum(bins*aligned))
    rms_delay_bins=float(np.sqrt(np.sum((bins-mean)**2*aligned)))
    entropy=float(-np.sum(aligned[aligned>0]*np.log(aligned[aligned>0]))/math.log(64))
    return {'peak_bin':peak,'peak_ratio':float(aligned[0]),
            'rms_delay_bins_relative':rms_delay_bins,
            'rms_delay_ns_relative':rms_delay_bins*50.0, # 20 MHz theoretical bin time
            'late_energy_ratio':float(aligned[3:].sum()),
            'energy_entropy_normalized':entropy}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',default='recordings')
    p.add_argument('--output',default='results/cir_features.csv')
    args=p.parse_args()
    out=Path(args.output); out.parent.mkdir(parents=True,exist_ok=True)
    fields=['trial','condition','true_distance_m','round_id','t_us','rssi_dbm',
            'first_word_invalid','peak_bin','peak_ratio',
            'rms_delay_bins_relative','rms_delay_ns_relative',
            'late_energy_ratio','energy_entropy_normalized']
    import json
    kept=0; rejected=0
    with out.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for meta_path in Path(args.root).rglob('metadata.json'):
            folder=meta_path.parent
            meta=json.loads(meta_path.read_text(encoding='utf8'))
            path=folder/'csi.csv'
            if not path.exists(): continue
            with path.open(encoding='utf-8-sig',newline='') as src:
                for row in csv.DictReader(src):
                    feat=features_from_hex(row['csi_hex'],int(row['first_word_invalid']))
                    if feat is None:
                        rejected+=1
                        continue
                    w.writerow({'trial':meta['trial'],'condition':meta['condition'],
                                'true_distance_m':meta['true_distance_m'],
                                'round_id':row['round_id'], 't_us':row['t_us'],
                                'rssi_dbm':row['rssi_dbm'],
                                'first_word_invalid':row['first_word_invalid'],**feat})
                    kept+=1
    print(f'Saved {out}: valid LLTF CSI={kept}; rejected other length={rejected}')
    print('IMPORTANT: relative CIR delay metrics are not calibrated absolute propagation delays.')

if __name__=='__main__':main()
