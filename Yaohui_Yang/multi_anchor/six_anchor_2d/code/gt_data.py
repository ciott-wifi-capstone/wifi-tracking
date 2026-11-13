"""Offline surveyed labels. Never imported by the live inference engine."""
import hashlib
import json
import math
from pathlib import Path


def json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def json_lines(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        for line in f:
            if line.strip(): yield json.loads(line)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()


def load_session(directory):
    d = Path(directory)
    session = json_file(d/'session.json')
    if session['source'] != 'LIVE_SERIAL' or session.get('truth_used_for_inference') is not False:
        raise ValueError('Expected real serial data with inference independent of truth.')
    # Do not fit a folder while the live recorder is still writing it.
    if session['state'] == 'RUNNING': raise ValueError('Stop capture and close the live window before fitting/evaluation.')
    points = json_file(d/'points.json')
    audit = {n: digest(d/n) for n in ['session.json', 'points.json', 'serial_raw.log', 'ranges.jsonl', 'positions.jsonl']}
    return session, points, audit


def point_for(row, points, purpose):
    p = points.get(row.get('token'))
    if p is None or p['purpose'] != purpose or p['status'] != 'COMPLETE': return None
    if [row['boot'], row['scan_id']] not in p.get('observed_scans', []): return None
    return p


def true_range(point, anchor):
    return math.sqrt((point['x']-anchor['x'])**2+(point['y']-anchor['y'])**2+(point['z']-anchor['z'])**2)
