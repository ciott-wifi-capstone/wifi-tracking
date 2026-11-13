"""V5 ZIP/folder readers and a shared offline/live serial adapter."""
import csv
import hashlib
import io
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from cir_core import make_round_features
from record_serial_2min import (parse_measurement, FTM_HEAD, CSI_HEAD, DETAIL_HEAD, FRAME_HEAD)


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip('\ufeff')))) if text else []


def read_trials(inputs):
    names = ['metadata.json', 'ftm.csv', 'ftm_detail.csv', 'csi.csv', 'csi_ftm_frame.csv']
    seen = set()
    for source in inputs:
        path = Path(source)
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                groups = defaultdict(dict)
                for member in archive.infolist():
                    normalized = PurePosixPath(member.filename.replace('\\', '/'))
                    if not member.is_dir() and normalized.name in names:
                        if normalized.name in groups[str(normalized.parent)]:
                            raise ValueError('Duplicate normalized ZIP entry.')
                        groups[str(normalized.parent)][normalized.name] = archive.read(member).decode('utf-8-sig')
                for directory, files in sorted(groups.items()):
                    if 'metadata.json' not in files:
                        continue
                    fingerprint = hashlib.sha256(''.join(files.get(n, '') for n in names[1:]).encode()).hexdigest()
                    if fingerprint in seen:
                        raise ValueError('Duplicate capture data in inputs: ' + str(source))
                    seen.add(fingerprint)
                    yield {'source': str(path) + ':' + directory, 'files': files,
                           'fingerprint': fingerprint}
        elif path.is_dir():
            metas = sorted(path.rglob('metadata.json'))
            if not metas:
                archives = sorted(path.rglob('*.zip'))
                if not archives:
                    raise ValueError('No V5 captures found in ' + str(path))
                yield from read_trials(archives)
                continue
            for metadata in metas:
                files = {n: (metadata.parent / n).read_text(encoding='utf-8-sig')
                         for n in names if (metadata.parent / n).exists()}
                fingerprint = hashlib.sha256(''.join(files.get(n, '') for n in names[1:]).encode()).hexdigest()
                if fingerprint in seen:
                    raise ValueError('Duplicate capture data: ' + str(metadata.parent))
                seen.add(fingerprint)
                yield {'source': str(metadata.parent), 'files': files, 'fingerprint': fingerprint}
        else:
            raise ValueError('Expected a V5 recording directory or ZIP: ' + str(path))


def extract_trial(trial):
    files = trial['files']
    for name in ['metadata.json', 'ftm.csv', 'ftm_detail.csv', 'csi.csv', 'csi_ftm_frame.csv']:
        if name not in files:
            raise ValueError('Missing ' + name + ' in ' + trial['source'])
    metadata = json.loads(files['metadata.json'])
    sessions = csv_rows(files['ftm.csv'])
    details = defaultdict(list)
    frames = defaultdict(list)
    csi = defaultdict(dict)
    for row in csv_rows(files['ftm_detail.csv']):
        details[int(row['round_id'])].append(row)
    for row in csv_rows(files['csi_ftm_frame.csv']):
        frames[int(row['round_id'])].append(row)
    for row in csv_rows(files['csi.csv']):
        csi[int(row['round_id'])][int(row['t_us'])] = row
    counts = Counter(int(r['round_id']) for r in sessions)
    if any(v > 1 for v in counts.values()):
        raise ValueError('Repeated round IDs: capture includes a reboot; replay its serial_raw.log instead.')
    result = []
    skipped = []
    for session in sessions:
        round_id = int(session['round_id'])
        try:
            features = make_round_features(session, details[round_id], frames[round_id], csi[round_id])
        except (ValueError, KeyError) as exc:
            skipped.append({'round_id': round_id, 'reason': str(exc)})
            continue
        features.update(trial=metadata['trial'], round_id=round_id,
                        true_distance_m=float(metadata['true_distance_m']),
                        condition=metadata.get('condition', 'UNKNOWN'),
                        capture_fingerprint=trial['fingerprint'])
        result.append(features)
    return result, {'trial': metadata['trial'], 'source': trial['source'],
                    'capture_fingerprint': trial['fingerprint'], 'sessions': len(sessions),
                    'usable_sessions': len(result), 'skipped': skipped,
                    'true_distance_m': float(metadata['true_distance_m']),
                    'paired_cir_frames': sum(r['paired_cir_frames'] for r in result)}


class SerialRoundAdapter:
    """Use exactly the same round feature builder as historical CSV training.

    Does not accept a true distance. Handles repeated IDs after reboots by
    resetting the round at each ROUND_START and adds an epoch counter.
    """
    def __init__(self):
        self.epoch = 0
        self.previous_id = None
        self.round_id = None
        self.ftm = None
        self.details = []
        self.frames = []
        self.csi = {}
        self.last_error = None
        self.last_raw_ftm = None

    def _reset(self, round_id):
        if self.previous_id is not None and round_id <= self.previous_id:
            self.epoch += 1
        self.previous_id = round_id
        self.round_id = round_id
        self.ftm = None
        self.details = []
        self.frames = []
        self.csi = {}
        self.last_error = None
        self.last_raw_ftm = None

    def finish(self):
        if self.ftm is None:
            return None
        self.last_raw_ftm = int(self.ftm['dist_est_cm']) / 100.0 if int(self.ftm['status']) == 0 else None
        try:
            row = make_round_features(self.ftm, self.details, self.frames, self.csi)
            row.update(round_id=self.round_id, epoch=self.epoch)
            self.last_error = None
            return row
        except (ValueError, KeyError) as exc:
            self.last_error = str(exc)
            return None
        finally:
            self.ftm = None

    def feed(self, line):
        parts = line.strip().split(',')
        if parts[0] == 'ROUND_START' and len(parts) >= 2:
            self._reset(int(parts[1]))
            return None
        if parts[0] == 'ROUND_END':
            return self.finish()
        try:
            kind, values = parse_measurement(line, float('nan'))
        except (ValueError, IndexError):
            return None
        headers = {'FTM': FTM_HEAD, 'CSI': CSI_HEAD, 'FTM_DETAIL': DETAIL_HEAD,
                   'CSI_FTM_FRAME': FRAME_HEAD}
        if kind not in headers:
            return None
        row = dict(zip(headers[kind], values))
        if self.round_id is None:
            self._reset(int(row['round_id']))
        if int(row['round_id']) != self.round_id:
            return None
        if kind == 'FTM':
            self.ftm = row
        elif kind == 'CSI':
            self.csi[int(row['t_us'])] = row
        elif kind == 'FTM_DETAIL':
            self.details.append(row)
        elif kind == 'CSI_FTM_FRAME':
            self.frames.append(row)
        return None
