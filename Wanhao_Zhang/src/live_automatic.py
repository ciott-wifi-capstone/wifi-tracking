"""Causal, frozen event-local correction; no serial, fitting or localization.

Only ingest complete FTM frames. Outputs are available after both RANGE and
its exact peer/t2 FTM record arrive. Original RANGE timing is retained for age.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
MODEL_SHA256 = '3542bdd9209b66d36221956b814e7c7217c3958d5cfad98f038a3e774fdd10cc'
HELPER_SHA256 = {
    'automatic_branch_core.py': '96c715c85a75db9949fcaae923ec6217b4e58c6ddb68f9aa33bbc89b045b486c',
    'automatic_branch_bench.py': '6a56053793cfdfe134feb7b7dfbdbf2b3e4d194e34ceb0679b971573ee288c6f',
    'condition_feature_bench.py': 'c893a1253b4e191c1247fcb20434009c9cdc58ca9075060d9636a081959bb099',
    'calibration_bench.py': '79c755a0d3c905f5a7704ba7dbe50918a545d7a427091973a355161ae70dacd7',
    'wipro_diagnostic_features.py': '95929ac2008d8d401d4a4d7154f8acda7f07d46f85f4e09d8aea8248b5314ff5',
    'burst_diagnostic_features.py': '6d6d297bce347c9915e09d985aef530e608dbccb31a527ed9e1f5a0a2ee5b0f4',
}
_MAC = re.compile(r'[0-9A-F]{2}(?::[0-9A-F]{2}){5}\Z')


def _finite(value):
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError('Finite numerical input required')
    return float(value)


def _key(peer, timestamp):
    peer = str(peer).strip().upper()
    if not _MAC.fullmatch(peer) or isinstance(timestamp, bool):
        raise ValueError('Full peer MAC and exact integer t2 required')
    number = int(timestamp)
    if number < 0 or number >= 1 << 64 or str(timestamp).strip() != str(number):
        raise ValueError('Exact nonnegative integer t2 required')
    return peer, number


def _load_helpers(directory):
    directory = Path(directory).resolve()
    for name, expected in HELPER_SHA256.items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != expected:
            raise ValueError('Frozen helper hash differs: '+name)
    sys.path.insert(0, str(directory))
    try:
        modules = {name[:-3]: importlib.import_module(name[:-3]) for name in HELPER_SHA256}
    finally:
        sys.path.remove(str(directory))
    # Refuse an earlier import from a different, possibly mutable runtime tree.
    for name, module in modules.items():
        if Path(module.__file__).resolve().parent != directory:
            raise ValueError('Frozen helper already imported from another directory: '+name)
    modules['wipro_diagnostic_features'].load_assets()  # Hash-checked pinned assets.
    return modules


class StreamingAutomatic:
    """Bounded exact-key join and inference using the unchanged compact gate.

    Known duplicate conflicts quarantine a key. A conflict arriving after a
    published output cannot retroactively change history; metadata counts it.
    Missing/invalid CSI uses existing trained imputation, with explicit status.
    Missing FTM cannot produce a correction because raw RTT is unavailable.
    """
    def __init__(self, model_path=None, helper_dir=None, max_pending=4096):
        if isinstance(max_pending, bool) or int(max_pending) != max_pending or max_pending < 1:
            raise ValueError('Positive integer cache limit required')
        self.max_pending = int(max_pending)
        self.helper_dir = Path(helper_dir or HERE/'automatic_branch_runtime').resolve()
        self.model_path = Path(model_path or self.helper_dir/'automatic_model_bundle.json').resolve()
        payload = self.model_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != MODEL_SHA256:
            raise ValueError('Frozen automatic model hash differs; no fitting permitted')
        self.bundle = json.loads(payload)
        self.helpers = _load_helpers(self.helper_dir)
        self.reset()

    def reset(self):
        self.counts = Counter()
        self._ranges, self._ftm = OrderedDict(), OrderedDict()
        self._emitted, self._conflicts = OrderedDict(), OrderedDict()

    def _remember(self, table, key, value, name):
        table[key] = value
        table.move_to_end(key)
        while len(table) > self.max_pending:
            table.popitem(last=False)
            self.counts[name+'_cache_evictions'] += 1

    def _conflict(self, key, source):
        self.counts[source+'_conflicts'] += 1
        if key in self._emitted:
            self.counts['late_conflicts_after_emit'] += 1
        self._remember(self._conflicts, key, True, 'conflict')
        self._ranges.pop(key, None)
        self._ftm.pop(key, None)

    def ingest_range(self, peer, t2, value, unix_ms, monotonic_s):
        key = _key(peer, t2)
        observation = dict(value=_finite(value), unix_ms=_finite(unix_ms), monotonic_s=_finite(monotonic_s))
        self.counts['range_received'] += 1
        if key in self._conflicts:
            self.counts['quarantined_range'] += 1
            return []
        old = self._ranges.get(key)
        if old:
            if old['value'] != observation['value']:
                self._conflict(key, 'range')
            else:
                self.counts['range_duplicates'] += 1
            return []
        self._remember(self._ranges, key, observation, 'range')
        return self._emit([key])

    def ingest_ftm(self, fields, unix_ms, monotonic_s):
        unix_ms, monotonic_s = _finite(unix_ms), _finite(monotonic_s)
        self.counts['ftm_received'] += 1
        try:
            peer, reports, csi_errors = self.helpers['burst_diagnostic_features'].parse_ftm_fields(fields)
        except (ValueError, TypeError, OverflowError):
            self.counts['invalid_ftm_frames'] += 1
            return []
        self.counts['csi_unmatched_or_invalid_groups'] += csi_errors
        ready = []
        for report in reports:
            key = _key(peer, report['t2'])
            if key in self._conflicts:
                self.counts['quarantined_ftm'] += 1
                continue
            signature = tuple(report[name] for name in
                              ('t1', 't2', 't3', 't4', 'rssi', 'csi_b64', 'csi_pair_ambiguous'))
            old = self._ftm.get(key)
            if old:
                if old['signature'] != signature:
                    self._conflict(key, 'ftm')
                else:
                    self.counts['ftm_duplicates'] += 1
                continue
            self._remember(self._ftm, key, dict(report=report, signature=signature,
                unix_ms=unix_ms, monotonic_s=monotonic_s), 'ftm')
            ready.append(key)
        return self._emit(ready)

    def _emit(self, keys):
        ready = [key for key in keys if key in self._ranges and key in self._ftm
                 and key not in self._emitted and key not in self._conflicts]
        if not ready:
            return []
        native = [self._ranges[k]['value'] for k in ready]
        reports = [self._ftm[k]['report'] for k in ready]
        payloads = [r['csi_b64'] for r in reports]
        raw = [r['raw_m'] for r in reports]
        feature_rows = [self.helpers['calibration_bench'].extract_signal_features(d, r, report['rssi'], payload)
                        for d, r, report, payload in zip(native, raw, reports, payloads)]
        diagnostics, audit = self.helpers['wipro_diagnostic_features'].extract_batch_diagnostic_features(
            payloads, raw, native)
        self.counts['native_cir_mismatch'] += audit['native_reconstruction_mismatch_n']
        for feature, diagnostic, d, r in zip(feature_rows, diagnostics, native, raw):
            feature.update(diagnostic)
            feature['relative_correction'] = (r-d)/(abs(r)+1.)
        predicted = self.helpers['automatic_branch_bench'].predict_automatic_bundle(self.bundle, feature_rows)
        output = []
        for index, key in enumerate(ready):
            ranged, ftm = self._ranges[key], self._ftm[key]
            # Both clocks describe the later dependency, selected using monotonic time.
            available = max((ranged, ftm), key=lambda item: item['monotonic_s'])
            flags = feature_rows[index]
            status = ('csi_ambiguous' if reports[index]['csi_pair_ambiguous'] else
                      'csi_missing_or_invalid' if flags['diagnostic_csi_missing'] else
                      'cir_missing_native_mismatch' if flags['diagnostic_cir_missing'] else 'complete')
            p = float(predicted['compact_probability'][index])
            output.append(dict(kind='automatic', peer=key[0], timestamp=key[1],
                value=float(predicted['automatic_compact_hard'][index]), p=p,
                branch='specialized' if p >= .5 else 'los_offset', features_status=status,
                native_value=native[index], raw_m=raw[index],
                unix_ms=available['unix_ms'], monotonic_s=available['monotonic_s'],
                native_unix_ms=ranged['unix_ms'], native_monotonic_s=ranged['monotonic_s'],
                ftm_unix_ms=ftm['unix_ms'], ftm_monotonic_s=ftm['monotonic_s']))
            self._remember(self._emitted, key, True, 'emitted')
            self.counts['emitted'] += 1
            self.counts['branch_'+output[-1]['branch']] += 1
            self.counts['features_'+status] += 1
        return output

    def metadata(self):
        return dict(enabled=True, status='ready', model_sha256=MODEL_SHA256,
            model_path=str(self.model_path), helper_dir=str(self.helper_dir),
            helper_sha256=dict(HELPER_SHA256), mode='automatic_compact_hard',
            los_offset_m=self.bundle['los_offset_m'], low_offset_m=self.bundle['los_offset_m'],
            paired_events_n=self.counts['emitted'], counts=dict(self.counts),
            range_cache_n=len(self._ranges), ftm_cache_n=len(self._ftm), max_cache_entries=self.max_pending,
            causal_policy='Exact full peer+t2; complete FTM and RANGE arrival required; original RANGE age retained',
            duplicate_policy='Known conflicts quarantined in bounded caches; late conflicts recorded without rewriting published history',
            missing_policy='Missing CSI uses frozen trained imputation+flags; no FTM means no automatic output',
            fitting=False, clipping=False, labels_or_coordinates_used=False)
