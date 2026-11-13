"""Numerical/protocol tests only. Fixtures below are synthetic, never measurement results."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'code'))
from positioning import load_config, solve_wls, RangeTracker, select_range, geometry_id, METHODS
from acquisition import Processor, Recorder, write_json
from gt_data import true_range
from calibrate import fit
from evaluate_gt import evaluate
from tools.flash import plan
from refit_cir import train, physical_groups
from cir_core import CIR_FEATURES, SCHEMA, fit_model, save_model
from positioning import model_hashes


def config(): return load_config(ROOT/'anchors.json')

def fixture_models():
    """In-memory model files for software tests; no measured features or weights."""
    rows = []
    for i, d in enumerate((2., 3., 4., 5., 6.)):
        row = dict(true_distance_m=d, ftm_distance_m=d+1, ftm_frame_median_m=d+1,
                   ftm_mad_m=.2, ftm_rssi_dbm=-40, trial=f'synthetic_{i}')
        row.update({name: (j+1)*.01+d*.001 for j,name in enumerate(CIR_FEATURES)})
        rows.append(row)
    return {kind:fit_model(rows,kind=kind) for kind in ('ftm_affine','cir_direct','cir_residual')}


def observation(c, aid, x, y, t=0, sigma=.2):
    return dict(anchor_id=aid, corrected_m=true_range(dict(x=x,y=y,z=c['tag_height_m']),c['anchors'][aid]),
                sigma_m=sigma, rf_time_s=t)


class NumericTests(unittest.TestCase):
    def test_exact_slant_ranges_and_nonzero_height_difference(self):
        c=config(); c['tag_height_m']=1.2
        rows=[observation(c,a,31.2,9.3) for a in c['anchors']]
        p=solve_wls(rows,c)
        self.assertTrue(p['valid']); self.assertLess(math.hypot(p['x']-31.2,p['y']-9.3),1e-5)

    def test_insufficient_anchors(self):
        c=config()
        self.assertFalse(solve_wls([observation(c,'A1',30,9)],c)['valid'])

    def test_asynchronous_moving_tracker_and_timestamp_reset(self):
        c=config(); tracker=RangeTracker(c); last=None
        for k in range(120):
            t=k*.2; x=29+.2*t; y=9+.2*math.sin(t/4)
            last=tracker.update(observation(c,f'A{k%6+1}',x,y,t))
        self.assertIsNotNone(last)
        self.assertLess(math.hypot(last['x']-x,last['y']-y),.4)
        self.assertIsNone(tracker.update(observation(c,'A1',20,8,0)))

    def test_no_fallback_negative_and_ood(self):
        c=config(); row=dict(anchor_id='A1',ftm_status=0,raw_ftm_m=10,cir_residual_m=None,cir_residual_quality='CIR_UNAVAILABLE')
        self.assertFalse(select_range(row,'cir_residual',c)['range_usable'])
        row.update(cir_residual_m=10,cir_residual_quality='OUT_OF_DOMAIN')
        self.assertFalse(select_range(row,'cir_residual',c)['range_usable'])
        c['allow_ood']=True
        self.assertGreaterEqual(select_range(row,'cir_residual',c)['sigma_m'],8)
        row['cir_residual_m']=-1
        self.assertFalse(select_range(row,'cir_residual',c)['range_usable'])


class ProtocolTests(unittest.TestCase):
    def test_full_failed_scan_is_saved_and_midscan_not_claimed(self):
        events=[]; c=config()
        processor=Processor(fixture_models(),c,'cir_residual',None,lambda k,v:events.append((k,v)))
        processor.feed('ROUND_START,1,100',0)
        processor.feed('ROUND_END,1,200',0)
        self.assertEqual(events,[])
        for line in ['SCAN_START,deadbeef,1,1000,point_token','PROFILE,32,2,6,20']:
            processor.feed(line,0)
        for i in range(1,7):
            for line in [f'ROUND_START,{i},{1000*i}',f'LINK_ID,{i},100,A{i},02:00:00:00:00:{i:02x},6',
                         f'FTM,{i},200,-3,0,0,0,0,0.0',f'LINK_TIME,{i},100,200,250',f'ROUND_END,{i},300']:
                processor.feed(line,0)
        processor.feed('SCAN_END,deadbeef,1,1000000,point_token',0)
        self.assertEqual(sum(k=='range' for k,v in events),6)
        scan=next(v for k,v in events if k=='scan')
        self.assertTrue(scan['full_scan']); self.assertFalse(scan['valid'])
        self.assertEqual(scan['token'],'point_token')

    def test_cancelled_point_cannot_become_complete_on_late_ack(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as temp:
            r=Recorder(Namespace(),config(),{},None,temp)
            r.points={'tok':dict(token='tok',point_id='K1',status='ABORTED',requested_scans=1)}
            r.observed_scans={'tok':[['boot',1]]}
            r.emit('protocol',['MARK_ACK','boot','tok','1'])
            r.emit('protocol',['POINT_DONE','boot','tok','1','100'])
            self.assertEqual(r.points['tok']['status'],'ABORTED')

    def test_flash_isolates_anchor_ids_and_tag_profile(self):
        _,_,a=plan(ROOT,'anchor','A1','COM11',115200,2,'IDF','python')
        _,_,b=plan(ROOT,'anchor','A6','COM11',115200,2,'IDF','python')
        self.assertNotEqual(a,b); self.assertIn('-DLAB_ANCHOR_NUMBER=6',b[0])
        _,_,tag=plan(ROOT,'tag',None,'COM11',921600,2,'IDF','python')
        self.assertFalse(any('LAB_ANCHOR_NUMBER' in s for s in tag[0]))


def fixture(directory, purpose, locations, calibration_sha=None):
    """TEST FIXTURE, no radio data. Kept in TemporaryDirectory only."""
    directory.mkdir(); c=config(); points={}; ranges=[]; positions=[]
    for pi,(x,y) in enumerate(locations,1):
        token='fixture_'+str(pi)
        point=dict(token=token,point_id=('K' if purpose=='CALIBRATION' else 'P')+str(pi),x=x,y=y,z=1.7,
                   purpose=purpose,status='COMPLETE',requested_scans=2,observed_scans=[['fixture',pi*10+j] for j in range(2)])
        points[token]=point
        for j in range(2):
            scan=pi*10+j
            for ai,aid in enumerate(c['anchors'],1):
                tr=true_range(point,c['anchors'][aid]); bias=.3*ai
                row=dict(token=token,boot='fixture',scan_id=scan,round_id=scan*6+ai,anchor_id=aid,
                         ftm_status=0,bssid=f'02:00:00:00:00:{ai:02x}',firmware_profile='32:2:6:20')
                for method in METHODS:
                    row[method+'_m']=tr+bias
                    if method!='raw_ftm':row[method+'_quality']='EXPERIMENTAL'
                ranges.append(row)
            positions.append(dict(token=token,boot='fixture',scan_id=scan,kind='WLS_SCAN',valid=True,
                                  reason='OK',x=x+.1,y=y-.1,rf_time_s=float(scan)))
    metadata=dict(source='LIVE_SERIAL',test_fixture=True,state='FINISHED',truth_used_for_inference=False,
                  config=c,geometry_sha256=geometry_id(c),model_hashes={'FIXTURE':'not_a_real_model'},
                  selected_method='cir_residual',calibration_sha256=calibration_sha)
    for name,data in [('session.json',metadata),('points.json',points)]:write_json(directory/name,data)
    for name,rows in [('ranges.jsonl',ranges),('positions.jsonl',positions)]:
        (directory/name).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (directory/'serial_raw.log').write_text('TEST FIXTURE ONLY '+purpose+str(locations))
    return points


class CalibrationTests(unittest.TestCase):
    def test_fit_bias_heldout_evaluation_and_no_test_labels_in_fit(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp); cal=p/'cal'
            fixture(cal,'CALIBRATION',[(19,11),(31,9),(44,7)])
            data,_=fit([cal])
            self.assertAlmostEqual(data['methods']['cir_residual']['A6']['bias_m'],1.8)
            file=p/'calibration.json'; write_json(file,data)
            h=hashlib.sha256(file.read_bytes()).hexdigest()
            test=p/'test'; fixture(test,'TEST',[(20,10),(32,8),(45,6)],h)
            report,_,_,_=evaluate(test,file)
            self.assertEqual(report['wls_coverage'],1.)
            self.assertAlmostEqual(report['wls']['mae_m'],math.sqrt(.02))
            # Adding TEST rows to fitting inputs cannot alter fitted parameters.
            again,_=fit([cal,test]); self.assertEqual(data['methods'],again['methods'])
            fixture(p/'overlap','TEST',[(19,11)],h)
            with self.assertRaisesRegex(ValueError,'0.25 m'): evaluate(p/'overlap',file)
            with self.assertRaises(ValueError): fit([test])

    def test_full_cir_refit_uses_location_folds_and_recomputes_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp); cal=p/'cal'
            fixture(cal,'CALIBRATION',[(19,11),(24,10),(29,9),(34,8),(39,7),(44,6)])
            metadata=json.loads((cal/'session.json').read_text()); metadata['cir_feature_schema']=SCHEMA
            write_json(cal/'session.json',metadata)
            rows=[json.loads(s) for s in (cal/'ranges.jsonl').read_text().splitlines()]
            model=fixture_models()['cir_residual']
            for r in rows:
                f=dict(zip(model['feature_names'],model['center']))
                f['ftm_distance_m']=r['raw_ftm_m']; f['ftm_frame_median_m']=r['raw_ftm_m']
                f['ftm_mad_m']=.2; f['ftm_rssi_dbm']=-40
                # Synthetic features for an algorithm test, never real measured CIR.
                for i,k in enumerate(model['feature_names'][4:]):f[k]+=r['raw_ftm_m']*.001*(i+1)
                r['features']=f
            (cal/'ranges.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            fitted,info=train([cal])
            self.assertEqual(info['physical_calibration_locations'],6)
            self.assertEqual(len(info['selection']['cir_residual']['candidates']['10.0']['fold_mae_m']),6)
            models=p/'models_gt';models.mkdir()
            for k,m in fitted.items():save_model(models/(k+'.json'),m)
            info['model_hashes']=model_hashes(models);write_json(models/'fit_info.json',info)
            bias,_=fit([cal],True,'cir_residual',4,models)
            self.assertEqual(bias['model_hashes'],model_hashes(models))
            self.assertEqual(len(bias['model_training_points']),6)
            self.assertEqual(len(set(physical_groups({'a':dict(x=1,y=1),'b':dict(x=1.1,y=1),'c':dict(x=3,y=3)}).values())),2)


if __name__=='__main__': unittest.main()
