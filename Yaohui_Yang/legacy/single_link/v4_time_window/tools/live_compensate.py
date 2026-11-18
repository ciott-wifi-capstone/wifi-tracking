#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live FTM range correction, using prior calibrated model.

Uses ESP32-S3 Tag USB COM12, no firmware changes. Ground-truth distance is
never supplied at prediction time. Works with QAR Tag v2 (FTM, FTM_ENTRY,
CSI), and does not require synchronized CSI when using raw_linear model.

  python tools/live_compensate.py --port COM12 --model models/ftm_linear.json

Models only learned on past measured placements. May fail when antenna,
layout, or radio environment changes. 'IN_RAW_RANGE' is NOT proof of accuracy.
"""
import argparse,csv,json,math,sys,datetime
from pathlib import Path
from collections import defaultdict
import numpy as np
from compensate_ftm import predict, FEATS, channel_features

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port',default='COM12');p.add_argument('--baud',type=int,default=115200)
    p.add_argument('--model',default='models/ftm_linear.json')
    p.add_argument('--out',default='live_ftm_corrected.csv')
    p.add_argument('--print-raw',action='store_true',help='Print all non-measurement boot lines')
    p.add_argument('--replay',help='Optional serial_raw.log file for offline test instead of COM port')
    args=p.parse_args()
    model=json.loads(Path(args.model).read_text(encoding='utf-8'))
    if model['mode'] not in ['raw_linear','hybrid','offset']:
        raise ValueError('Unknown model mode')
    feat_cache=defaultdict(list);frame_cache=defaultdict(list)
    rows=0
    with open(args.out,'w',newline='',encoding='utf-8-sig') as f:
        fieldnames=['time_utc','round_id','ftm_raw_m','ftm_calibrated_m','status','n_frames','rssi_dbm','csi_count','note']
        writer=csv.DictWriter(f,fieldnames);writer.writeheader()
        print(f'Input={args.replay or args.port}, model={args.model}. Ctrl+C to exit.')
        if args.replay:
            dev=open(args.replay,'rb')
        else:
            import serial
            dev=serial.Serial(args.port,args.baud,timeout=1)
        with dev:
            try:
                while True:
                    line=dev.readline().decode('utf-8',errors='replace').strip()
                    if not line:
                        if args.replay:break
                        continue
                    parts=line.split(',')
                    kind=parts[0]
                    try:
                        if kind=='CSI' and len(parts)==13:
                            rid=parts[1]
                            csi={'len_bytes':parts[10],'csi_hex':parts[12],'first_word_invalid':parts[11]}
                            features=channel_features(csi)
                            if features:
                                features.append(float(parts[4]))
                                feat_cache[rid].append(features)
                        elif kind=='FTM_ENTRY' and len(parts)==5:
                            rtt=int(parts[3]);
                            if rtt!=4294967295 and rtt>=0:frame_cache[parts[1]].append(0.000149896229*rtt)
                        elif kind=='FTM' and len(parts)==9:
                            _,rid,_,stat,rawns,estns,distcm,nent,rssi=parts
                            if stat!='0':
                                print(f'Round {rid}: FTM failed, status={stat}')
                                frame_cache.pop(rid,None);feat_cache.pop(rid,None)
                                continue
                            raw=float(distcm)/100.0
                            frames=np.asarray(frame_cache.pop(rid,[]),float)
                            cf=np.asarray(feat_cache.pop(rid,[]),float)
                            # For raw_linear, CSI does not alter the distance; optional hybrid uses it.
                            rec={'raw_m':raw,'frame_p25_m':float(np.percentile(frames,25)) if len(frames) else raw,
                              'frame_mad_m':float(np.median(np.abs(frames-np.median(frames)))) if len(frames) else 0,
                              'ftm_rssi_dbm':float(rssi)}
                            if len(cf):
                                med=np.median(cf,axis=0)
                                rec.update(dict(zip(FEATS[4:],map(float,med))))
                            elif model['mode']=='hybrid':
                                print(f'Round {rid}: no CSI, cannot apply hybrid model')
                                continue
                            pred=float(predict(model,[rec])[0])
                            note=[]
                            if raw<model.get('raw_min_m',float('-inf')) or raw>model.get('raw_max_m',float('inf')):
                                note.append('OUTSIDE_TRAINING_RAW_RANGE')
                            if len(frames)<8:note.append('LOW_FRAME_COUNT')
                            if len(cf)<3:note.append('LOW_CSI_COUNT')
                            if pred<model.get('true_min_m',float('-inf')) or pred>model.get('true_max_m',float('inf')):
                                note.append('ESTIMATE_OUTSIDE_TRAINING_TRUE_RANGE')
                            flag='CAUTION' if note else 'IN_RAW_RANGE'
                            record={'time_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              'round_id':rid,'ftm_raw_m':round(raw,3),'ftm_calibrated_m':round(pred,3),
                              'status':flag,'n_frames':len(frames),'rssi_dbm':rssi,'csi_count':len(cf),
                              'note':';'.join(note)}
                            writer.writerow(record);f.flush();rows+=1
                            print(f'Round {rid:>4} | raw={raw:6.2f} m | calibrated={pred:6.2f} m | {flag} | csi={len(cf)}')
                            # prevent stale accumulations from sudden reconnect / reboot
                            if rows%200==0:
                                frame_cache.clear();feat_cache.clear()
                        elif args.print_raw and kind in ('TARGET_READY','RADIO_TAG','FTM_START','ANCHOR_READY'):
                            print(line)
                    except (ValueError,IndexError,KeyError,OverflowError) as e:
                        print('Skipped malformed line:',str(e),file=sys.stderr)
            except KeyboardInterrupt:print('\nStopped. Output:',str(Path(args.out).resolve()))
if __name__=='__main__':main()
