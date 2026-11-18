"""Causal summaries of completed frozen-model events for guarded localization.

These summaries keep original RANGE host receipt times. Feature completion is
an availability condition, never a replacement freshness clock. No routes,
marks, fitted positions, or future samples are inputs.
"""
from __future__ import annotations

import math
import statistics


def summarize_completed(observations, now, range_window_s=1.5, default_refresh_s=6.0):
    """Return one latest-burst summary per anchor, without an age cutoff.

    Each row contains value (frozen automatic), native_value (same-event native),
    monotonic_s, available_monotonic_s and prediction_ready_monotonic_s.
    A refresh interval is estimated only after at least three completed gaps.
    Gaps <=0.5 s are packet/chunk fragmentation, not full anchor refreshes.
    Only the most recent seven completed bursts contribute to the cadence.
    """
    summaries = {}
    for identity, history in observations.items():
        rows = [r for r in history
                if r['monotonic_s'] <= now
                and r.get('available_monotonic_s', r['monotonic_s']) <= now
                and r.get('prediction_ready_monotonic_s', r['monotonic_s']) <= now]
        if not rows:
            continue
        latest_time = max(r['monotonic_s'] for r in rows)
        batch = [r for r in rows if r['monotonic_s'] >= latest_time-range_window_s]
        complete_times = sorted(set(r.get('available_monotonic_s', r['monotonic_s']) for r in rows))
        bursts = []
        for t in complete_times:
            if not bursts or t-bursts[-1] > .5:
                bursts.append(t)
        recent = bursts[-7:]
        gaps = [b-a for a, b in zip(recent, recent[1:]) if b-a > .5]
        refresh = statistics.median(gaps) if len(gaps) >= 3 else default_refresh_s
        if not math.isfinite(refresh) or refresh <= 0:
            refresh = default_refresh_s
        summaries[identity] = dict(
            native_m=statistics.median(r['native_value'] for r in batch),
            automatic_m=statistics.median(r['value'] for r in batch),
            monotonic_s=latest_time, refresh_s=refresh,
            samples_n=len(batch), refresh_gaps_n=len(gaps),
            available_monotonic_s=max(r.get('available_monotonic_s', r['monotonic_s']) for r in batch),
            prediction_ready_monotonic_s=max(r.get('prediction_ready_monotonic_s', r['monotonic_s']) for r in batch),
        )
    return summaries
