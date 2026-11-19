from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from guarded_observations import summarize_completed


def row(t, complete=None, ready=None, automatic=5., native=6.):
    return dict(value=automatic, native_value=native, monotonic_s=t,
                available_monotonic_s=t+.2 if complete is None else complete,
                prediction_ready_monotonic_s=t+.3 if ready is None else ready)


def test_availability_and_original_clock_and_no_future_influence():
    history = {'A1': [row(10.), row(15.4), row(21.)]}
    assert summarize_completed(history, 10.25) == {}
    s = summarize_completed(history, 10.4)['A1']
    assert s['monotonic_s'] == 10.
    assert s['refresh_s'] == 6.
    assert summarize_completed({'A1':history['A1'][:1]},10.4) == {'A1':s}


def test_latest_burst_median_and_cadence_not_per_sample_interval():
    h = [row(t, automatic=a) for t in [10.,15.4,20.8,26.2]
         for a in [3.,4.,100.]]
    s = summarize_completed({'A1':h},27.)['A1']
    assert s['automatic_m'] == 4.
    assert s['native_m'] == 6.
    assert s['samples_n'] == 3
    assert abs(s['refresh_s']-5.4) < 1e-10
    assert s['refresh_gaps_n'] == 3


def test_negative_output_preserved_and_stale_summary_not_rejuvenated():
    s = summarize_completed({'A1':[row(10.,automatic=-.3)]},100.)['A1']
    assert s['automatic_m'] == -.3
    assert s['monotonic_s'] == 10.
