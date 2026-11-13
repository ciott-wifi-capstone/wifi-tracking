"""Compatible single-round CIR feature extraction; no truth labels enter inference."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

import numpy as np
from cir_core import load_model, predict
from cir_io import SerialRoundAdapter

KINDS = ['ftm_affine', 'cir_direct', 'cir_residual']
NAMES = {'ftm_affine': 'FTM linear', 'cir_direct': 'CIR only', 'cir_residual': 'FTM + CIR'}
FIELDS = ['pc_utc', 'elapsed_s', 'epoch', 'round_id', 'tag_end_us', 'anchor_id', 'bssid', 'channel',
          'ftm_status', 'raw_ftm_m', 'paired_cir_frames', 'failure_reason']
for _kind in KINDS:
    FIELDS += [_kind + '_m', _kind + '_quality', _kind + '_usable_m']


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def models_from(path):
    return {kind: load_model(Path(path) / (kind + '.json')) for kind in KINDS}


def number(value):
    return '--' if value is None else f'{value:.3f}'


class StreamEngine:
    """One result per fully observed round; no labels enter this interface."""
    def __init__(self, models):
        self.models = models
        self.adapter = SerialRoundAdapter()
        self.ready = {'anchor_id': 'UNKNOWN', 'bssid': '', 'channel': ''}
        self.active = False
        self.link = {}
        self.sources = set()
        self.status = None
        self.last_features = None

    def feed(self, line, elapsed_s, pc_utc=None):
        parts = line.strip().split(',')
        kind = parts[0]
        if kind == 'TARGET_READY':
            fields = dict(p.split('=', 1) for p in parts[1:] if '=' in p)
            for key in self.ready:
                if key in fields:self.ready[key] = fields[key].lower() if key == 'bssid' else fields[key]
        if kind == 'ROUND_START':
            self.adapter.feed(line)
            self.active = True
            self.rid = int(parts[1])
            self.link = self.ready.copy()
            self.sources = set()
            self.status = None
            return None
        if not self.active:
            return None  # Opening a port in the middle of a round must not create a full result.
        if kind == 'LINK_ID' and len(parts) == 6 and int(parts[1]) == self.rid:
            self.link = {'anchor_id': parts[3], 'bssid': parts[4].lower(), 'channel': parts[5]}
        if kind == 'FTM' and len(parts) == 9 and int(parts[1]) == self.rid:
            self.status = int(parts[3])
        if kind == 'CSI' and len(parts) == 13 and int(parts[1]) == self.rid:
            self.sources.add(parts[3].lower())
        if kind == 'CSI_FTM_FRAME' and len(parts) == 14 and int(parts[1]) == self.rid:
            self.sources.add(parts[5].lower())
        features = self.adapter.feed(line)
        if kind != 'ROUND_END' or len(parts) < 2 or int(parts[1]) != self.rid:
            return None
        self.active = False
        self.last_features = features
        if not self.link.get('bssid') and len(self.sources) == 1:
            self.link['bssid'] = next(iter(self.sources))
        mismatch = len(self.sources) > 1 or bool(self.sources and self.link.get('bssid')
                       and self.sources != {self.link['bssid']})
        result = dict(pc_utc=pc_utc or utc_now(), elapsed_s=round(elapsed_s, 3), epoch=self.adapter.epoch,
                      round_id=self.rid, tag_end_us=parts[2] if len(parts) > 2 else '',
                      **self.link, ftm_status=self.status, raw_ftm_m=self.adapter.last_raw_ftm,
                      paired_cir_frames=features['paired_cir_frames'] if features else 0, failure_reason='')
        if mismatch:
            failure, reason = 'SOURCE_MISMATCH', 'CSI source differs from the announced AP BSSID.'
        elif self.status != 0:
            failure, reason = 'FTM_FAILED', f'FTM status={self.status}'
        elif features is None:
            failure, reason = 'CIR_UNAVAILABLE', self.adapter.last_error or 'No complete CIR features.'
        else:
            failure, reason = None, ''
        result['failure_reason'] = reason
        for model_kind, model in self.models.items():
            if failure:
                value = {'distance_m': None, 'quality': failure, 'distance_for_use_m': None}
            else:
                value = predict(model, features)
            result[model_kind + '_m'] = value['distance_m']
            result[model_kind + '_quality'] = value['quality']
            result[model_kind + '_usable_m'] = value['distance_for_use_m']
        return result
