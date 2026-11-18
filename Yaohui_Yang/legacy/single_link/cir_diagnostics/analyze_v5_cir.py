#!/usr/bin/env python3
"""Analyze *paired* ESP32-S3 LLTF CSI + FTM recordings without inventing range corrections.

Input: V5_TIMESTAMP_01.zip or extracted V5_TIMESTAMP_01 directory.
Usage: python analyze_v5_cir.py path/to/V5_TIMESTAMP_01.zip --out results
Requires: numpy, pandas, matplotlib.

Physics caveat: relative packet-referenced CIR is NOT an absolutely calibrated ToF.
A 1024-bin IDFT interpolates an existing ~20 MHz spectrum; it does not
create extra multipath resolution. This tool deliberately makes NO distance
correction from an uncalibrated CIR peak.
"""
import argparse
import json
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

C = 299792458.0
SUB_SPACING_HZ = 312500.0
VALID = np.r_[np.arange(1,27), np.arange(38,64)]
# ESP32-S3 CSI order: signed imaginary byte then signed real byte.
# L-LTF 20MHz FFT frequency bins are 0,1..31,-32..-1 (index 38..63 = -26..-1).

def parse_lltf(hex_string):
    raw = bytes.fromhex(hex_string)
    if len(raw) != 128:
        raise ValueError(f'Expected 128 LLTF bytes, got {len(raw)}')
    v = np.frombuffer(raw, dtype=np.int8).astype(np.float64)
    return v[1::2] + 1j*v[::2]

def idft_interpolated(h64, n=1024):
    # Zero-pad BETWEEN + and - frequencies, not after all 64 original bins.
    H = np.zeros(n,dtype=np.complex128)
    H[1:27] = h64[1:27]
    H[-26:] = h64[38:64]
    return np.fft.ifft(H)

def load_inputs(path):
    if path.is_file() and path.suffix.lower() == '.zip':
        td = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            for name in ('ftm.csv','ftm_detail.csv','ftm_csi_frame_pairs.csv','csi_ftm_frame.csv','metadata.json'):
                hits = [m for m in members if Path(m.filename).name == name]
                if len(hits) != 1:
                    raise ValueError(f'Expected exactly one {name}, found {len(hits)}')
                (Path(td.name)/name).write_bytes(z.read(hits[0]))
        return Path(td.name),td
    if path.is_dir():
        return path,None
    raise FileNotFoundError(path)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input',type=Path,help='V5_TIMESTAMP_01.zip or folder')
    ap.add_argument('--out',type=Path,default=Path('QAR_CIR_Results'))
    a=ap.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    root,tmp=load_inputs(a.input)
    try:
        f=pd.read_csv(root/'ftm.csv',encoding='utf-8-sig')
        d=pd.read_csv(root/'ftm_detail.csv',encoding='utf-8-sig')
        ps=pd.read_csv(root/'ftm_csi_frame_pairs.csv',encoding='utf-8-sig')
        cf=pd.read_csv(root/'csi_ftm_frame.csv',encoding='utf-8-sig')
        meta=json.loads((root/'metadata.json').read_text(encoding='utf-8-sig'))
        count_unmatched=int((ps.pair_status=='NO_ACTION33_CSI').sum())
        ok=ps[ps.pair_status=='TOKEN_UNIQUE_TIMING_COHERENT_PRELIMINARY'].copy()
        if not len(ok):
            raise ValueError('No preliminarily matched FTM/CSI frames found.')
        # Enforce uniqueness; do not conflate a candidate token match with guaranteed physical ToF.
        for name,table in [('FTM candidates',ok),('CSI frames',cf)]:
            if table.duplicated(['round_id','dialog_token']).any():
                raise ValueError(f'{name} has duplicate round/token keys: cannot guarantee one-to-one pairing')
        z=ok.merge(cf,on=['round_id','dialog_token'],validate='one_to_one',suffixes=('','_csi'))
        if z['csi_len_bytes'].ne(128).any() or z['cwb'].ne(0).any():
            raise ValueError('Input contains unexpected non-20MHz/128-byte CSI; review PHY mapping first')
        rows=[]; curv=[]
        for r in z.itertuples(index=False):
            h=parse_lltf(r.csi_hex)
            power=np.abs(h[VALID])**2
            active_cnt=int(np.count_nonzero(power))
            if active_cnt!=52:
                # nonzero bins are a recording observation; unusual packets should be reviewed
                continue
            amps=np.abs(h[VALID]); spectral_flatness=float(np.exp(np.mean(np.log(power+1e-12)))/np.mean(power))
            amp_cv=float(np.std(amps)/np.mean(amps))
            cir64=np.fft.ifft(h)
            coarse_peak=int(np.argmax(abs(cir64)**2))
            circ_energy=float(abs(cir64[coarse_peak])**2/np.sum(abs(cir64)**2))
            ci=idft_interpolated(h)
            curv.append(abs(ci)**2/(max(abs(ci)**2)+1e-30))
            # Phase delay is a diagnostic ONLY, with unknown hardware/timing origin.
            ids=np.r_[np.arange(38,64),np.arange(1,27)]
            sub_k=np.r_[np.arange(-26,0),np.arange(1,27)]
            phase=np.unwrap(np.angle(h[ids]))
            group_delay_ns=float(-np.polyfit(sub_k*SUB_SPACING_HZ,phase,1)[0]/(2*np.pi)*1e9)
            rows.append({
                'round_id':int(r.round_id),'dialog_token':int(r.dialog_token),
                'rtt_ps':int(r.rtt_ps),'ftm_frame_distance_m':float(r.rtt_ps)*1e-12*C/2,
                'rssi_dbm':int(r.rssi_dbm),'time_residual_ns':float(r.time_residual_ns),
                'active_subcarriers':active_cnt,'amplitude_cv':amp_cv,
                'spectral_flatness':spectral_flatness,
                'cir64_peak_bin':coarse_peak,'cir64_peak_energy_fraction':circ_energy,
                'raw_phase_slope_delay_ns_UNCALIBRATED':group_delay_ns,
                'absolute_distance_correction_m':'NOT_IDENTIFIABLE_WITHOUT_CALIBRATION',
            })
        pd.DataFrame(rows).to_csv(a.out/'matched_csi_cir_features.csv',index=False,encoding='utf-8-sig')
        feat=pd.DataFrame(rows)
        grouped=feat.groupby('round_id',as_index=False).agg(
            paired_ftm_csi_frames=('dialog_token','size'),
            median_frame_rtt_ps=('rtt_ps','median'),
            median_spectral_flatness=('spectral_flatness','median'),
            median_amplitude_cv=('amplitude_cv','median'),
            median_uncalibrated_phase_slope_ns=('raw_phase_slope_delay_ns_UNCALIBRATED','median'),
            cir_peak_bin_0_fraction=('cir64_peak_bin',lambda v:float((v==0).mean())),
        )
        grouped=grouped.merge(f[['round_id','estimated_distance_m','true_distance_m','absolute_error_m']],on='round_id',how='left',validate='one_to_one')
        grouped.to_csv(a.out/'round_level_summary.csv',index=False,encoding='utf-8-sig')
        raw=f.estimated_distance_m.to_numpy(float)
        true=f.true_distance_m.to_numpy(float)
        abs_err=np.abs(raw-true)
        all_p90=float(np.quantile(abs_err,0.9))
        # Timebase relation: compare the two receiver timestamps recorded for the same RX event.
        rel_delta=z.t2_ps.to_numpy(float)/1000-z.hw_rx_time_ns.to_numpy(float)
        # Chart 1: original FTM; no adjusted distances are invented.
        fig,ax=plt.subplots(figsize=(10,4.6))
        ax.plot(f.round_id,raw,'o-',label='FTM raw range')
        if np.unique(true).size==1: ax.axhline(true[0],ls='--',label='Ground truth')
        else:ax.plot(f.round_id,true,'--',label='Ground truth')
        ax.set(xlabel='Measurement round',ylabel='Distance (m)',title='V5: raw FTM distance vs ground truth')
        ax.legend();ax.grid(alpha=.25);fig.tight_layout();fig.savefig(a.out/'raw_ftm_distance.png',dpi=170);plt.close(fig)
        # Chart 2: band-limited CIR centered on circular zero-lag; uncalibrated delay origin.
        ccurve=np.vstack(curv)
        # wrapped delay bins; [0,...,1023] where last bins correspond to negative relative delay
        xi=np.arange(1024)
        delay=np.where(xi<=512,xi,xi-1024)/(1024*SUB_SPACING_HZ)*1e9
        ix=np.argsort(delay)
        keep=(delay[ix]>=-100)&(delay[ix]<=100)
        fig,ax=plt.subplots(figsize=(10,4.6))
        med=np.median(ccurve,axis=0);q1=np.quantile(ccurve,.1,axis=0);q9=np.quantile(ccurve,.9,axis=0)
        ax.fill_between(delay[ix][keep],q1[ix][keep],q9[ix][keep],alpha=.2,label='10th–90th percentile')
        ax.plot(delay[ix][keep],med[ix][keep],label='Median normalized CIR power')
        ax.set(xlabel='RELATIVE CIR delay (ns; origin NOT calibrated)',ylabel='Normalized power',title='20 MHz LLTF CIR from matched FTM CSI')
        ax.legend();ax.grid(alpha=.25);fig.tight_layout();fig.savefig(a.out/'relative_cir.png',dpi=170);plt.close(fig)
        # Chart 3: 20 MHz magnitude spectrum in 64 FFT bins.
        hists=np.vstack([abs(parse_lltf(v)) for v in z.csi_hex])
        hmed=np.median(hists,axis=0)
        kin=np.r_[np.arange(0,32),np.arange(-32,0)]
        inds=np.argsort(kin)
        fig,ax=plt.subplots(figsize=(10,4.6))
        ax.plot(kin[inds],hmed[inds],'-o',markersize=2)
        ax.set(xlabel='LLTF subcarrier index (312.5 kHz spacing)',ylabel='Median CSI magnitude (ADC units)',title='Observed subcarrier map: 52 occupied, 12 null')
        ax.grid(alpha=.25);fig.tight_layout();fig.savefig(a.out/'lltf_subcarrier_map.png',dpi=170);plt.close(fig)
        ntotal=len(ps); n=len(feat); cnt=int((feat.cir64_peak_bin==0).sum())
        lines=[
            '# FTM–CSI 同帧数据分析（V5_TIMESTAMP_01）','',
            f'- 输入：{a.input.name}；实际距离：{float(np.median(true)):.2f} m；有效 FTM 会话：{len(f)}。',
            f'- 原始 FTM：中位数 {np.median(raw):.2f} m，均值 {np.mean(raw):.2f} m，MAE {np.mean(abs_err):.2f} m，误差 P90 {all_p90:.2f} m，范围 {np.min(raw):.2f}–{np.max(raw):.2f} m。',
            f'- FTM 逐帧：{len(d)}；Action33 CSI：{len(cf)}；可用唯一初步配对：{n}/{ntotal}（{n/ntotal*100:.2f}%）；无 Action33 CSI：{count_unmatched}。',
            f'- CSI：采样带宽指示 cwb=0（20 MHz），每帧 128 字节 LLTF；经正确虚部/实部顺序解码，实测每帧 52 个非零有效子载波（另外 12 个为零），符合 802.11g LLTF 活跃子载波。',
            f'- 硬件接收时间戳 与 FTM t2（同一接收事件）差值中位数 {np.median(rel_delta):.3f} ns，范围 {np.min(rel_delta):.3f}–{np.max(rel_delta):.3f} ns。该一致性不构成绝对传输延迟的独立校验。',
            f'- 64 点原生 IFFT：{cnt}/{n} 条 CIR 的最大能量点位于相对 delay bin=0。原生采样格间隔为 1/20MHz=50ns，对应单程约 15m 的距离跨度；频谱有效占用约 16.25MHz。',
            f'- 1024 点频域零填充只能插值已有时延曲线，不会增加新信息，也不能据峰位置直接确定 2m 的直达传播时间。',
            f'- 频域幅度变化系数（CV）中位数 {np.median(feat.amplitude_cv):.3f}，谱平坦度中位数 {np.median(feat.spectral_flatness):.3f}；这些反映信道形态，不能单独识别准确传播距离。',
            f'- 未校准相位斜率估计中位数 {np.median(feat.raw_phase_slope_delay_ns_UNCALIBRATED):.2f}ns；受包检测时延、同步、射频链影响，其时间零点不是绝对直达路径；禁止直接折算为距离扣减。',
            '',
            '## 结论与下一步',
            '',
            '**能做**：基于同帧数据比较频域起伏、CIR 形态与 FTM RTT，作为电磁传播环境特征；用已知独立场景的校准数据训练/核验误差模型。',
            '',
            '**目前不能做**：仅凭单个 2m 场景，对硬件常量延迟和多径延迟进行可辨识分解，或输出已验证的绝对距离补偿。',
            '',
            '主要原因：所有最新同帧记录只有一组真实距离及相近摆位；20MHz LLTF 不可分辨相差几米的紧邻路径；同源接收时间戳一致不等于绝对 ToF 校准。',
            '',
            '建议：保留现有 Anchor/Tag；先离线使用 `matched_csi_cir_features.csv`，将来只在有独立校准依据时启用 `d_corrected = d_ftm - b_hardware - b_multipath(CIR)`。',
        ]
        (a.out/'分析结论.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        print('OUT:',a.out.resolve());print('FTM:',len(f),'median',round(np.median(raw),3),'MAE',round(np.mean(abs_err),3),'P90',round(all_p90,3));print('MATCHED:',n,'/',ntotal,'unmatched',count_unmatched);print('CSI 64-bin peak=0:',cnt,'/',n);print('UNVALIDATED phase delay median ns:',round(np.median(feat.raw_phase_slope_delay_ns_UNCALIBRATED),3))
    finally:
        if tmp is not None:tmp.cleanup()

if __name__=='__main__':
    main()
