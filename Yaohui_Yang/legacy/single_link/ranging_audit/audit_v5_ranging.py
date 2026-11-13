#!/usr/bin/env python3
"""Read-only V5 unit/timestamp audit; Python standard library only.

Usage: python audit_v5_ranging.py recordings
       python audit_v5_ranging.py V5_LOS_2m_B_R1.zip

Reads CSV exports; does not correct distance, train a model, flash firmware,
or independently validate CSI timestamp accuracy. Original files stay intact.
"""
import argparse
import csv
import io
import json
import statistics
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

PS_TO_M = 0.000149896229
INVALID_RTT = 0xFFFFFFFF


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))


def folder_trials(path):
    metadata = sorted(path.rglob('metadata.json'))
    if not metadata:
        raise ValueError(f'No metadata.json found below {path}')
    for meta in metadata:
        root = meta.parent

        def read(name, root=root):
            file = root / name
            return file.read_text(encoding='utf-8-sig') if file.exists() else None

        yield str(root), read


def zip_trials(path):
    with zipfile.ZipFile(path) as archive:
        names = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            normalized = info.filename.replace('\\', '/')
            if normalized in names:
                raise ValueError(f'Duplicate normalized ZIP member: {normalized}')
            names[normalized] = info
        meta_names = sorted(k for k in names if PurePosixPath(k).name == 'metadata.json')
        if not meta_names:
            raise ValueError(f'No metadata.json in {path}')
        for metadata in meta_names:
            root = str(PurePosixPath(metadata).parent)

            def read(name, root=root):
                key = str(PurePosixPath(root) / name)
                return archive.read(names[key]).decode('utf-8-sig') if key in names else None

            yield f'{path}:{root}', read


def audit_trial(source, read):
    meta_text = read('metadata.json')
    ftm_text = read('ftm.csv')
    detail_text = read('ftm_detail.csv')
    if meta_text is None or ftm_text is None or detail_text is None:
        raise ValueError(f'Missing metadata/ftm/ftm_detail: {source}')
    meta = json.loads(meta_text)
    sessions = csv_rows(ftm_text)
    success = [r for r in sessions if int(r['status']) == 0]
    details = csv_rows(detail_text)
    valid = [r for r in details if int(r['rtt_ps']) != INVALID_RTT]
    if not success or not valid:
        raise ValueError(f'No successful sessions or valid frame RTTs: {source}')
    distances = [int(r['dist_est_cm']) / 100 for r in success]
    truth = float(meta['true_distance_m'])
    parse_errors = [abs(float(r['estimated_distance_m']) - int(r['dist_est_cm']) / 100)
                    for r in success]
    # Convert to Python integers BEFORE subtracting large picosecond timestamps.
    errors = [(int(r['t4_ps']) - int(r['t1_ps']))
              - (int(r['t3_ps']) - int(r['t2_ps'])) - int(r['rtt_ps'])
              for r in valid]
    est_errors = [abs(int(r['dist_est_cm']) / 100
                      - int(r['rtt_est_ns']) * PS_TO_M * 1000) for r in success]
    frame_text = read('csi_ftm_frame.csv')
    frames = csv_rows(frame_text) if frame_text else []
    pair_text = read('ftm_csi_frame_pairs.csv')
    pairs = csv_rows(pair_text) if pair_text else []
    return {
        'source': source,
        'trial': meta.get('trial', source),
        'true_distance_m': truth,
        'successful_sessions': len(success),
        'failed_sessions': len(sessions) - len(success),
        'median_distance_m': statistics.median(distances),
        'mean_distance_m': statistics.mean(distances),
        'session_sample_stdev_m': statistics.stdev(distances) if len(distances) > 1 else None,
        'session_mae_m': statistics.mean(abs(d - truth) for d in distances),
        'csv_driver_distance_max_difference_m': max(parse_errors),
        'rtt_est_distance_max_difference_m': max(est_errors),
        'raw_equals_est_sessions': sum(int(r['rtt_raw_ns']) == int(r['rtt_est_ns']) for r in success),
        'valid_frame_rtt_count': len(valid),
        'invalid_frame_rtt_count': len(details) - len(valid),
        'four_timestamp_mismatch_count': sum(e != 0 for e in errors),
        'four_timestamp_max_abs_difference_ps': max(abs(e) for e in errors),
        'action33_csi_count': len(frames),
        'action33_cwb_counts': dict(Counter(r['cwb'] for r in frames)),
        'exported_pair_status_counts': dict(Counter(r['pair_status'] for r in pairs)),
        'limits': 'Internal consistency only. Pair statuses are summarized, not revalidated. '
                  'Agreement does not prove absolute ToF accuracy or identify the bias cause. '
                  'Small rtt_est/distance differences can arise from integer quantization. '
                  'Four-timestamp mismatches require checking rollover/frequency correction '
                  'before concluding a software error.'
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', help='V5 recording folders or ZIP files')
    parser.add_argument('--output', help='Optional new JSON output file; existing file is not overwritten')
    args = parser.parse_args()
    reports = []
    for value in args.inputs:
        path = Path(value)
        if path.is_dir():
            trials = folder_trials(path)
        elif path.is_file() and zipfile.is_zipfile(path):
            trials = zip_trials(path)
        else:
            parser.error(f'Not a recording directory or ZIP: {path}')
        for source, reader in trials:
            reports.append(audit_trial(source, reader))
    result = json.dumps(reports, indent=2, ensure_ascii=False)
    print(result)
    if args.output:
        with Path(args.output).open('x', encoding='utf-8') as file:
            file.write(result + '\n')


if __name__ == '__main__':
    main()
