"""Physical, causal and robust-solve checks; no hardware or model fitting."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys

import pytest

HOST = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(HOST))
from guarded_localization import DEFAULT_CONFIG, solve_guarded


def scene(point=(3., 4.), height=1.85):
    anchors = [dict(id=label, x=x, y=y, z=1.5) for label,x,y in
               [('A1',0.,0.),('A2',20.,0.),('A3',0.,20.),('A4',20.,20.)]]
    links = {}
    for anchor in anchors:
        distance = math.dist((*point,height),(anchor['x'],anchor['y'],anchor['z']))
        links[anchor['id']] = dict(automatic_m=distance,
            native_m=distance+DEFAULT_CONFIG['native_offset_m'], monotonic_s=10.,
            available_monotonic_s=10., prediction_ready_monotonic_s=10.,refresh_s=5.4,samples_n=16)
    return anchors,links,height


def test_exact_physical_position_and_order_invariance_no_truth_leakage():
    anchors,links,height = scene()
    first = solve_guarded(anchors,links,height,10.)
    assert first['ok'] and (first['x'],first['y']) == pytest.approx((3.,4.),abs=1e-7)
    altered = copy.deepcopy(links)
    for row in altered.values():
        row.update(truth_m=999999, mark_coordinates=[-5000,8000], route='invented route')
    second = solve_guarded(anchors[::-1],dict(reversed(list(altered.items()))),height,10.)
    assert (second['x'],second['y']) == pytest.approx((first['x'],first['y']),abs=1e-9)
    assert all(d['source']=='automatic' and d['valid'] for d in first['link_diagnostics'])
    assert first['position_history_used'] is first['route_or_marks_used'] is False
    json.dumps(first,allow_nan=False)


def test_age_guard_covers_full_refresh_and_changes_smoothly_at_old_expiry():
    anchors,links,height = scene()
    for row in links.values():
        row.update(monotonic_s=0.,available_monotonic_s=0.,prediction_ready_monotonic_s=0.)
    before = solve_guarded(anchors,links,height,3.99)
    after = solve_guarded(anchors,links,height,4.01)
    cycle = solve_guarded(anchors,links,height,5.41)
    assert before['ok'] and after['ok'] and cycle['ok']
    assert cycle['anchors_n']==4 and cycle['stale_cap_s']==pytest.approx(10.8)
    assert cycle['valid_until_monotonic_s']==pytest.approx(10.8)
    assert abs(before['weights']['A1']-after['weights']['A1'])<.01
    assert cycle['weights']['A1']>0
    old = solve_guarded(anchors,links,height,4.01,{'age_guard':False})
    assert not old['ok'] and old['anchors_n']==0 and old['stale_cap_s']==4.
    expired = solve_guarded(anchors,links,height,10.8)
    assert expired['anchors_n']==0
    assert all(not d['valid'] and 'stale_paired_batch' in d['reasons'] for d in expired['link_diagnostics'])


def test_gross_overcorrection_falls_back_by_triangle_improvement():
    height,point = 1.85,(1.,0.)
    anchors = [dict(id=label,x=x,y=y,z=1.5) for label,x,y in
               [('A1',0.,0.),('A2',12.,0.),('A3',0.,12.),('A6',33.7,0.)]]
    links = {}
    for a in anchors:
        d=math.dist((*point,height),(a['x'],a['y'],a['z']))
        links[a['id']]=dict(automatic_m=d,native_m=d+DEFAULT_CONFIG['native_offset_m'],
            monotonic_s=10.,refresh_s=5.4,samples_n=16)
    links['A6'].update(automatic_m=9.17,monotonic_s=9.094)
    result=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in result['link_diagnostics'] if d['id']=='A6')
    assert result['ok'] and (result['x'],result['y'])==pytest.approx(point,abs=1e-5)
    assert diag['proposed_m']==9.17 and diag['used_m']>32.
    assert diag['source']=='native_offset_fallback'
    assert 'fallback_reduces_global_triangle_violation' in diag['reasons']
    assert result['pair_objective_after']<result['pair_objective_before']
    assert result['geometry_fallback_steps']
    assert all(s['objective_after']<s['objective_before'] for s in result['geometry_fallback_steps'])
    assert result['valid_until_monotonic_s']==pytest.approx(9.094+10.8)


def test_valid_large_nlos_correction_has_no_magnitude_cap():
    anchors,links,height=scene()
    links['A4']['native_m']=100.
    result=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in result['link_diagnostics'] if d['id']=='A4')
    assert result['ok'] and diag['source']=='automatic'
    assert diag['used_m']==links['A4']['automatic_m']
    assert not result['geometry_fallback_steps']


def test_negative_and_below_vertical_distances_are_not_clipped_into_solver():
    anchors,links,height=scene()
    links['A1']['automatic_m']=-9.
    result=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in result['link_diagnostics'] if d['id']=='A1')
    assert result['ok'] and diag['proposed_m']==-9.
    assert diag['used_m']==pytest.approx(links['A1']['native_m']-DEFAULT_CONFIG['native_offset_m'])
    assert all(v>=0 for v in result['ranges_used'].values())
    links['A1']['automatic_m']=.1
    low=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in low['link_diagnostics'] if d['id']=='A1')
    assert diag['used_m']>diag['vertical_minimum_m']>.1
    links['A1'].update(automatic_m=-9.,native_m=-2.)
    excluded=solve_guarded(anchors,links,height,10.)
    assert 'A1' not in excluded['ranges_used']
    assert any(d['id']=='A1' and not d['valid'] for d in excluded['link_diagnostics'])


@pytest.mark.parametrize('field,value',[('automatic_m',float('nan')),('automatic_m',float('inf')),
    ('monotonic_s',float('nan')),('monotonic_s',11.),
    ('available_monotonic_s',11.),('prediction_ready_monotonic_s',11.)])
def test_invalid_and_future_inputs_excluded_without_future_leakage(field,value):
    anchors,links,height=scene()
    links['A4'][field]=value
    result=solve_guarded(anchors,links,height,10.)
    assert result['ok'] and 'A4' not in result['anchors_used']
    assert (result['x'],result['y'])==pytest.approx((3.,4.),abs=1e-5)
    assert any(d['id']=='A4' and not d['valid'] for d in result['link_diagnostics'])
    json.dumps(result,allow_nan=False)


def test_motion_allowance_avoids_static_triangle_rejection_of_asynchronous_ranges():
    anchors=[dict(id=k,x=x,y=y,z=1.5) for k,x,y in
             [('A1',0.,0.),('A2',10.,0.),('A3',0.,10.),('A4',10.,10.)]]
    links={a['id']:dict(automatic_m=math.dist((9.,0.),(a['x'],a['y'])),
        native_m=None,monotonic_s=4.,refresh_s=5.4) for a in anchors}
    # A1 observed the same target four seconds earlier at (1,0): 2m/s motion.
    links['A1'].update(automatic_m=1.,monotonic_s=0.)
    motion=solve_guarded(anchors,links,1.5,4.)
    static=solve_guarded(anchors,links,1.5,4.,{'motion_speed_mps':0.})
    pair=lambda r:next(p for p in r['pair_diagnostics'] if (p['a'],p['b'])==('A1','A2'))
    assert pair(motion)['sum_violation_m']==0.
    assert pair(static)['sum_violation_m']>6.


def test_huber_resists_large_residual_without_position_smoothing():
    point=(4.,3.);height=1.5
    anchors=[dict(id=k,x=x,y=y,z=height) for k,x,y in
             [('A1',0.,0.),('A2',10.,0.),('A3',10.,10.),('A4',0.,10.),('A5',5.,-5.)]]
    links={a['id']:dict(automatic_m=math.dist(point,(a['x'],a['y'])),
                       native_m=None,monotonic_s=10.,refresh_s=5.4) for a in anchors}
    links['A5']['automatic_m']+=20.
    robust=solve_guarded(anchors,links,height,10.,{'geometry_guard':False,'age_guard':False})
    linear=solve_guarded(anchors,links,height,10.,{'geometry_guard':False,'age_guard':False,'robust':False})
    error=lambda r:math.dist((r['x'],r['y']),point)
    assert robust['ok'] and linear['ok']
    assert error(robust)<1.5 and error(robust)<error(linear)/2
    assert robust['config']['huber_scale_m']==1.


def test_configuration_and_collinear_geometry_fail_explicitly():
    anchors,links,height=scene()
    assert not solve_guarded(anchors,links,height,10.,{'not_a_parameter':True})['ok']
    for a in anchors:a['y']=0.
    result=solve_guarded(anchors,links,height,10.)
    assert not result['ok'] and result['reason']=='rank_deficient_anchor_geometry'
    assert DEFAULT_CONFIG['age_guard'] is True


def test_future_link_refresh_cannot_change_present_geometry_or_weights():
    anchors,links,height=scene()
    links['A4']['available_monotonic_s']=20.
    links['A1']['refresh_s']=3.
    links['A2']['refresh_s']=4.
    links['A3']['refresh_s']=5.
    links['A4']['refresh_s']=.1
    first=solve_guarded(anchors,links,height,10.)
    links['A4']['refresh_s']=99999.
    second=solve_guarded(anchors,links,height,10.)
    assert first['observed_refresh_s']==second['observed_refresh_s']==4.
    assert first['stale_cap_s']==second['stale_cap_s']==8.
    assert first['weights']==second['weights']
    assert (first['x'],first['y'])==pytest.approx((second['x'],second['y']),abs=1e-9)


@pytest.mark.parametrize('native,automatic',[(1.369863,.2817417564424062),(1.37,-9.)])
def test_original_native_survives_offset_below_assumed_vertical_minimum(native,automatic):
    height=1.85
    point=(math.sqrt(native*native-(height-1.5)**2),0.)
    anchors,links,height=scene(point,height)
    links['A1'].update(native_m=native,automatic_m=automatic)
    result=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in result['link_diagnostics'] if d['id']=='A1')
    assert result['ok'] and result['anchors_n']==4
    assert diag['proposed_m']==automatic
    assert diag['fallback_m']<diag['vertical_minimum_m']
    assert diag['used_m']==native and diag['source']=='native_unshifted_fallback'
    assert 'automatic_and_offset_invalid_use_original_native' in diag['reasons']
    assert all(r>=0 for r in result['ranges_used'].values())
    assert (result['x'],result['y'])==pytest.approx(point,abs=1e-5)


def test_original_native_is_distinct_geometry_candidate_with_objective_improvement():
    anchors,links,height=scene((1.,0.),1.5)
    links['A4'].update(automatic_m=5.,native_m=17.5)
    result=solve_guarded(anchors,links,height,10.)
    diag=next(d for d in result['link_diagnostics'] if d['id']=='A4')
    assert diag['source']=='native_unshifted_fallback'
    assert diag['used_m']==17.5 and diag['fallback_m']==pytest.approx(17.5-DEFAULT_CONFIG['native_offset_m'])
    assert 'original_native_reduces_global_triangle_violation' in diag['reasons']
    assert result['pair_objective_after']<result['pair_objective_before']
    assert all(r['objective_after']<r['objective_before'] for r in result['geometry_fallback_steps'])
