"""Independent closed-form and causal-contract tests for the fixed CV KF.

Run from the student project folder:
    python -m pytest tests/test_cv_kalman.py -q
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import inspect
import json
import unittest

import numpy as np
from numpy.testing import assert_allclose, assert_array_equal

from cv_kalman import CausalCVKalman, FIXED_SPEC, process_matrices


class TestFixedCVKalman(unittest.TestCase):
    def test_declared_parameters_are_frozen(self):
        self.assertEqual(FIXED_SPEC.acceleration_sigma_m_s2, 0.8)
        self.assertEqual(FIXED_SPEC.acceleration_variance_m2_s4, 0.64)
        self.assertEqual(FIXED_SPEC.measurement_variance_m2, 9.0)
        self.assertEqual(FIXED_SPEC.initial_covariance_diagonal, (25,25,4,4))
        self.assertEqual(FIXED_SPEC.publication_max_age_s, 4)
        self.assertEqual(FIXED_SPEC.observation_reset_gap_s, 8)
        self.assertFalse(FIXED_SPEC.innovation_gating)
        with self.assertRaises(FrozenInstanceError):
            FIXED_SPEC.measurement_variance_m2 = 1.0

    def test_exact_cv_transition_and_process_covariance(self):
        # dt=2 gives position-acceleration and velocity-acceleration terms of 2.
        F, G, Q = process_matrices(2.0)
        assert_array_equal(F, [[1,0,2,0],[0,1,0,2],[0,0,1,0],[0,0,0,1]])
        assert_array_equal(G, [[2,0],[0,2],[2,0],[0,2]])
        assert_allclose(Q, [[2.56,0,2.56,0],[0,2.56,0,2.56],
                            [2.56,0,2.56,0],[0,2.56,0,2.56]], rtol=0,atol=0)
        assert_array_equal(F @ [3,-2,1.5,-0.25], [6,-2.5,1.5,-0.25])
        F0, G0, Q0 = process_matrices(0)
        assert_array_equal(F0,np.eye(4))
        assert_array_equal(G0,np.zeros((4,2)))
        assert_array_equal(Q0,np.zeros((4,4)))
        self.assertGreaterEqual(np.linalg.eigvalsh(Q).min(),-1e-14)
        self.assertEqual(np.linalg.matrix_rank(Q),2)

    def test_initialization_and_closed_form_gain_update(self):
        f=CausalCVKalman()
        first=f.step(0,[0,0],True)
        self.assertEqual(first['update_reason'],'initialized')
        assert_array_equal(first['state'],[0,0,0,0])
        assert_array_equal(first['P'],np.diag([25,25,4,4]))
        row=f.step(1,[1,-1],True)
        # Per-axis prior: p_xx=25+4+0.16, p_xv=4+0.32, p_vv=4+0.64.
        expected_K=np.array([[29.16/38.16,0],[0,29.16/38.16],
                             [4.32/38.16,0],[0,4.32/38.16]])
        assert_allclose(row['K'],expected_K,rtol=0,atol=2e-16)
        assert_allclose(row['state'],expected_K @ [1,-1],rtol=0,atol=2e-16)
        predicted=np.array([[29.16,0,4.32,0],[0,29.16,0,4.32],
                            [4.32,0,4.64,0],[0,4.32,0,4.64]])
        # Closed conditional covariance, independent of the implementation's Joseph form.
        cross=predicted[:,:2]
        expected_P=predicted-cross @ cross.T/38.16
        assert_allclose(row['P'],expected_P,rtol=0,atol=6e-15)
        self.assertTrue(row['measurement_used'])
        self.assertEqual(row['measurement_updates'],1)
        self.assertEqual(row['initializations'],1)

    def test_cached_coordinates_never_shrink_covariance_or_refresh_clock(self):
        cached, no_obs, true_obs = (CausalCVKalman() for _ in range(3))
        for f in (cached,no_obs,true_obs):f.step(0,[2,3],True)
        a=cached.step(1,[9000,-4000],False)
        b=no_obs.step(1,None,False)
        c=true_obs.step(1,[2,3],True)
        assert_array_equal(a['state'],b['state'])
        assert_array_equal(a['P'],b['P'])
        self.assertEqual(a['last_observation_time'],0)
        self.assertEqual(a['observation_age_s'],1)
        self.assertFalse(a['measurement_used'])
        self.assertEqual(a['update_reason'],'predict_only_cached')
        self.assertGreater(a['P'][0][0],25)
        self.assertLess(c['P'][0][0],a['P'][0][0])
        # A new independent observation is assimilated even if its coordinates match.
        self.assertEqual(c['last_observation_time'],1)
        self.assertTrue(c['measurement_used'])
        repeated=cached.step(1,[-1e5,1e5],False)
        assert_array_equal(repeated['state'],a['state'])
        assert_array_equal(repeated['P'],a['P'])
        self.assertEqual(repeated['update_reason'],'same_time_no_information_noop')

    def test_publication_expiry_uses_last_real_observation(self):
        f=CausalCVKalman(); f.step(10,[3,4],True)
        at_boundary=f.step(14,[3,4],False)
        self.assertTrue(at_boundary['published'])
        expired=f.step(14.000001,[3,4],False)
        self.assertFalse(expired['published'])
        self.assertIsNone(expired['xy'])
        self.assertEqual(expired['last_observation_time'],10)
        self.assertTrue(np.isfinite(expired['state']).all())
        resumed=f.step(15,[4,4],True)
        self.assertTrue(resumed['published'])
        self.assertFalse(resumed['reset'])
        self.assertEqual(resumed['update_reason'],'measurement_update')

    def test_gap_reset_uses_observation_gap_not_last_prediction(self):
        f=CausalCVKalman(); f.step(0,[1,2],True)
        f.step(9,None,False)
        self.assertEqual(f.last_observation_time,0)
        row=f.step(10,[40,50],True)
        self.assertEqual(row['update_reason'],'gap_reset')
        self.assertTrue(row['reset'])
        assert_array_equal(row['state'],[40,50,0,0])
        assert_array_equal(row['P'],np.diag([25,25,4,4]))
        self.assertEqual(row['last_observation_time'],10)
        self.assertEqual(row['initializations'],2)
        exact=CausalCVKalman();exact.step(0,[1,2],True)
        self.assertFalse(exact.step(8,[40,50],True)['reset'])
        over=CausalCVKalman();over.step(0,[1,2],True)
        self.assertTrue(over.step(8.000001,[40,50],True)['reset'])

    def test_predict_at_is_nonmutating_even_when_querying_future(self):
        queried,control=CausalCVKalman(),CausalCVKalman()
        for f in (queried,control):f.step(0,[1,2],True)
        before=queried.audit
        query=queried.predict_at(100)
        self.assertFalse(query['published'])
        self.assertEqual(queried.audit,before)
        self.assertEqual(queried.state_time,0)
        self.assertEqual(queried.last_observation_time,0)
        self.assertEqual(queried.step(1,[2,3],True),control.step(1,[2,3],True))
        with self.assertRaises(ValueError):queried.predict_at(.5)

    def test_future_changes_and_truncation_cannot_change_prefix(self):
        inputs=[(0,[0,0],True),(.5,[.8,.2],True),(1,[.8,.2],False),
                (1.7,[2.1,.1],True),(3,[3.2,.3],True),(4,None,False),
                (5.4,[4,.9],True),(6,[5,1],True),(7,[5,1],False)]
        def run(rows):
            f=CausalCVKalman()
            return [f.step(*r) for r in rows]
        original=run(inputs)
        changed=deepcopy(inputs)
        changed[5:]=[(4,[100,100],True),(5.4,[-100,50],True),
                     (6,None,False),(7,[1000,-1000],True)]
        self.assertEqual(original[:5],run(changed)[:5])
        self.assertEqual(original[:5],run(inputs[:5]))

    def test_reverse_or_equal_time_real_updates_reject_atomically(self):
        f=CausalCVKalman();f.step(2,[1,2],True)
        before=f.audit
        for bad_t in (1,2):
            with self.assertRaises(ValueError):f.step(bad_t,[8,9],True)
            self.assertEqual(f.audit,before)
            assert_array_equal(f.state,[1,2,0,0])
        self.assertEqual(f.step(2,None,False)['update_reason'],
                         'same_time_no_information_noop')

    def test_invalid_inputs_cannot_mutate_history(self):
        f=CausalCVKalman();f.step(0,[0,0],True); before=f.audit
        cases=[(float('nan'),[1,2],True),(float('inf'),[1,2],True),
               (1,[float('nan'),2],True),(1,[1,2,3],True),
               (1,None,True),(True,[1,2],True),(1,[1,2],1)]
        for args in cases:
            with self.assertRaises((ValueError,TypeError)):f.step(*args)
            self.assertEqual(f.audit,before)
        with self.assertRaises(ValueError):process_matrices(-.1)
        with self.assertRaises(ValueError):process_matrices(1e200)
        self.assertEqual(f.audit,before)

    def test_uninitialized_events_do_not_create_pseudo_observations(self):
        f=CausalCVKalman()
        row=f.step(1,[100,100],False)
        self.assertIsNone(row['state'])
        self.assertIsNone(row['last_observation_time'])
        self.assertFalse(row['published'])
        self.assertEqual(f.predict_at(3)['update_reason'],'query_uninitialized')
        real=f.step(2,[4,5],True)
        assert_array_equal(real['state'],[4,5,0,0])
        self.assertEqual(real['update_reason'],'initialized')

    def test_audit_and_public_properties_are_detached(self):
        f=CausalCVKalman(); row=f.step(0,[1,2],True)
        row['state'][0]=999; row['P'][0][0]=999
        history=f.audit; history[0]['state'][0]=888
        x=f.state; x[0]=777
        P=f.covariance; P[0,0]=777
        self.assertEqual(f.audit[0]['state'],[1,2,0,0])
        self.assertEqual(f.audit[0]['P'][0][0],25)
        assert_array_equal(f.state,[1,2,0,0])
        self.assertEqual(f.covariance[0,0],25)
        json.dumps(f.audit,allow_nan=False)

    def test_irregular_long_run_covariances_finite_symmetric_psd(self):
        rng=np.random.default_rng(4713)
        f=CausalCVKalman();t=0.
        for index in range(600):
            t+=float(rng.uniform(.005,1.5))
            if index % 73 == 0 and index:
                t+=9.0
            is_new=index==0 or bool(rng.uniform()<.6)
            row=f.step(t,rng.normal(0,30,size=2),is_new)
            self.assertTrue(np.isfinite(row['state']).all())
            P=np.asarray(row['P'])
            self.assertTrue(np.isfinite(P).all())
            assert_allclose(P,P.T,rtol=0,atol=0)
            self.assertGreaterEqual(np.linalg.eigvalsh(P).min(),-1e-9)

    def test_axes_are_independent_and_large_innovations_are_not_gated(self):
        a,b=CausalCVKalman(),CausalCVKalman()
        for f in (a,b):f.step(0,[0,0],True)
        ar=a.step(1,[10000,2],True)
        br=b.step(1,[-10000,2],True)
        assert_allclose(np.asarray(ar['state'])[[1,3]],np.asarray(br['state'])[[1,3]],rtol=0,atol=0)
        self.assertTrue(ar['measurement_used'])
        self.assertEqual(ar['update_reason'],'measurement_update')
        self.assertGreater(ar['state'][0],1000)

    def test_no_ground_truth_route_or_parameter_inputs(self):
        self.assertEqual(list(inspect.signature(CausalCVKalman.step).parameters),
                         ['self','t','xy','new_information'])
        f=CausalCVKalman()
        with self.assertRaises(TypeError):f.step(0,[1,2],True,route=[[0,0]])
        with self.assertRaises(TypeError):f.step(0,[1,2],True,ground_truth=[1,2])
        with self.assertRaises(TypeError):CausalCVKalman(R=1)


if __name__=='__main__':
    unittest.main()
