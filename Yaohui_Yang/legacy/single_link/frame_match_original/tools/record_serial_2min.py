#!/usr/bin/env python3
"""Collect ESP32-S3 Target serial log and label one static LOS/NLOS trial.

Usage:
  python tools/record_serial.py --port COM5 --condition LOS --distance-m 2.0 --trial LOS_2m_01 --rounds 100
"""
import argparse
import csv
import json
import math
import time
from pathlib import Path
from datetime import datetime, timezone

FTM_HEAD = ['round_id','t_us','status','rtt_raw_ns','rtt_est_ns','dist_est_cm',
            'ftm_report_entries','ftm_mean_rssi_dbm','true_distance_m',
            'estimated_distance_m','signed_error_m','absolute_error_m']
CSI_HEAD = ['round_id','t_us','src_mac','rssi_dbm','noise_floor_dbm',
            'channel','secondary_channel','sig_mode','cwb','len_bytes',
            'first_word_invalid','csi_hex']
ENTRY_HEAD = ['round_id','entry_idx','rtt_ps','rssi_dbm']
PROBE_HEAD = ['round_id','probes_sent','echo_received','csi_rows','queue_dropped']
FTMCSI_HW_HEAD = ['round_id','callback_cpu_time_us','hw_rx_time_ns','hw_timestamp_precise','src_mac','rssi_dbm','cwb','csi_len_bytes']
DETAIL_HEAD = ['round_id','entry_idx','dialog_token','rtt_ps','rssi_dbm','t1_ps','t2_ps','t3_ps','t4_ps']
FRAME_HEAD = ['round_id','callback_cpu_time_us','hw_rx_time_ns','hw_timestamp_precise','src_mac','dialog_token','followup_token','mac_sequence_control','rx_seq','cwb','rssi_dbm','csi_len_bytes','csi_hex']


def parse_measurement(line, distance):
    parts = line.strip().split(',')
    if not parts:
        return None, None
    kind = parts[0]
    if kind == 'FTM' and len(parts) == 9:
        _, rid, t_us, status, raw, est, distcm, nent, rssi = parts
        status = int(status)
        # Wi-Fi FTM SUCCESS enum=0; failed sessions still saved with missing distance.
        value = int(distcm) / 100 if status == 0 else math.nan
        return kind, [int(rid),int(t_us),status,int(raw),int(est),int(distcm),int(nent),
                      float(rssi),distance,value,
                      value - distance if math.isfinite(value) else math.nan,
                      abs(value - distance) if math.isfinite(value) else math.nan]
    if kind == 'CSI' and len(parts) == 13:
        vals = parts[1:]
        return kind, [int(vals[0]), int(vals[1]), vals[2],int(vals[3]),int(vals[4]),
                      int(vals[5]),int(vals[6]),int(vals[7]),int(vals[8]),
                      int(vals[9]),int(vals[10]),vals[11]]
    if kind == 'CSI_FTM_HW' and len(parts) == 9:
        return kind, [int(parts[1]),int(parts[2]),int(parts[3]),int(parts[4]),parts[5],int(parts[6]),int(parts[7]),int(parts[8])]
    if kind == 'CSI_FTM_FRAME' and len(parts) == 14:
        return kind, [int(parts[1]),int(parts[2]),int(parts[3]),int(parts[4]),parts[5],*map(int,parts[6:13]),parts[13]]
    if kind == 'FTM_ENTRY' and len(parts) == 5:
        return kind, [int(x) for x in parts[1:]]
    if kind == 'FTM_DETAIL' and len(parts) == 10:
        return kind, [int(x) for x in parts[1:]]
    if kind == 'PROBE_SUMMARY' and len(parts) == 6:
        return kind, [int(x) for x in parts[1:]]
    return None, None


def run_capture(args):
    import serial
    root = Path(args.output) / args.trial
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f'Output already exists and is nonempty: {root} (choose new --trial)')
    root.mkdir(parents=True, exist_ok=True)
    meta = {'trial': args.trial, 'condition': args.condition.upper(),
            'true_distance_m': args.distance_m,
            'blocker': args.blocker, 'notes': args.notes,
            'baud': args.baud, 'port': args.port,
            'requested_duration_seconds': args.seconds,
            'started_utc': datetime.now(timezone.utc).isoformat(),
            'protocol': 'qar_ftm_v5_action33_identity_strict' ,
            'measurement_note': 'FTM Action33 CSI frames are exported only when driver exposes header/payload. Token and clock checks are done offline; no correction unless validated.'}
    (root / 'metadata.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Writing trial to: {root.resolve()}')
    with (root/'serial_raw.log').open('w',encoding='utf-8') as raw, \
         (root/'ftm.csv').open('w',newline='',encoding='utf-8-sig') as ftm, \
         (root/'csi.csv').open('w',newline='',encoding='utf-8-sig') as csi, \
         (root/'ftm_entries.csv').open('w',newline='',encoding='utf-8-sig') as entries, \
         (root/'probes.csv').open('w',newline='',encoding='utf-8-sig') as probes, \
         (root/'ftm_detail.csv').open('w',newline='',encoding='utf-8-sig') as detail, \
         (root/'csi_ftm_hw.csv').open('w',newline='',encoding='utf-8-sig') as hw, \
         (root/'csi_ftm_frame.csv').open('w',newline='',encoding='utf-8-sig') as frames:
        writers = { 'FTM': csv.writer(ftm), 'CSI':csv.writer(csi),
                   'FTM_ENTRY':csv.writer(entries), 'PROBE_SUMMARY':csv.writer(probes), 'FTM_DETAIL':csv.writer(detail),
                   'CSI_FTM_HW':csv.writer(hw), 'CSI_FTM_FRAME':csv.writer(frames)}
        for typ, head in [('FTM',FTM_HEAD),('CSI',CSI_HEAD),('FTM_ENTRY',ENTRY_HEAD),
                          ('PROBE_SUMMARY',PROBE_HEAD),('FTM_DETAIL',DETAIL_HEAD),('CSI_FTM_HW',FTMCSI_HW_HEAD),
                          ('CSI_FTM_FRAME',FRAME_HEAD)]:
            writers[typ].writerow(head)
        # Opening serial may reset some ESP32-S3 setups. Retains boot logs in raw.
        rounds = set()
        good = 0
        total_csi = 0
        with serial.Serial(args.port, args.baud, timeout=1) as device:
            started = time.monotonic()
            print(f'Collecting on {args.port}: {args.seconds:g}s (Ctrl+C to stop early).', flush=True)
            try:
                while True:
                    if args.seconds > 0 and time.monotonic() - started >= args.seconds:
                        print('Duration reached; stopping capture.', flush=True)
                        break
                    line = device.readline().decode('utf-8',errors='replace').strip()
                    if not line:
                        continue
                    raw.write(line+'\n')
                    raw.flush()
                    if line.startswith('TARGET_READY'):
                        print(line)
                    if line.startswith('ROUND_END,'):
                        try:
                            rounds.add(int(line.split(',')[1]))
                        except (ValueError, IndexError):
                            pass
                        if len(rounds) % 10 == 0 and len(rounds):
                            print(f'rounds={len(rounds)}  valid_ftm={good}  csi_packets={total_csi}', flush=True)
                        if args.rounds > 0 and len(rounds) >= args.rounds:
                            break
                    try:
                        typ, row = parse_measurement(line, args.distance_m)
                    except (ValueError, IndexError):
                        continue
                    if typ is not None:
                        writers[typ].writerow(row)
                        if typ == 'FTM' and row[2] == 0:
                            good += 1
                        if typ == 'CSI':
                            total_csi += 1
            except KeyboardInterrupt:
                print('Stopped by user.')
        for f in (ftm,csi,entries,probes,detail,hw,frames): f.flush()
    print(f'Finished: rounds={len(rounds)} valid_ftm={good} csi_packets={total_csi} elapsed_s={time.monotonic()-started:.1f}')


def main():
    p = argparse.ArgumentParser(description='ESP32-S3 single-link LOS/NLOS serial logger')
    p.add_argument('--port', required=True, help='E.g. COM5')
    p.add_argument('--baud',type=int,default=115200)
    p.add_argument('--condition',choices=['LOS','NLOS'],required=True)
    p.add_argument('--distance-m',type=float,required=True,help='Measured antenna-to-antenna distance in meters')
    p.add_argument('--trial',required=True,help='Unique trial ID, e.g. LOS_2m_01')
    p.add_argument('--rounds',type=int,default=0,help='Optional maximum complete rounds; 0=no round limit')
    p.add_argument('--seconds',type=float,default=120,help='Recording duration in seconds; 0=until Ctrl+C')
    p.add_argument('--blocker',default='none',help='none/person/wall/furniture/other')
    p.add_argument('--notes',default='',help='Antenna height, angle, etc.')
    p.add_argument('--output',default='recordings')
    args=p.parse_args()
    if not args.distance_m > 0: p.error('--distance-m must be positive')
    if args.seconds < 0 or args.rounds < 0: p.error('--seconds and --rounds must be nonnegative')
    run_capture(args)

if __name__=='__main__':
    main()
