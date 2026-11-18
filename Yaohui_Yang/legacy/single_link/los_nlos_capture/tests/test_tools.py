"""Unit tests need numpy, do not touch physical board or serial port."""
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
from record_serial import parse_measurement
from extract_cir import features_from_hex
from analyze_basic import percentile

class Tests(unittest.TestCase):
    def test_ftm(self):
        typ,row=parse_measurement('FTM,17,12345678,0,50,35,525,12,-60.5',4.0)
        self.assertEqual(typ,'FTM')
        self.assertAlmostEqual(row[9],5.25)
        self.assertAlmostEqual(row[-2],1.25)
        self.assertAlmostEqual(row[-1],1.25)
    def test_csi(self):
        raw='FE01'*64
        typ,row=parse_measurement(f'CSI,1,100,11:22:33:44:55:66,-60,-97,6,0,1,0,128,0,{raw}',1.0)
        self.assertEqual(typ,'CSI')
        self.assertEqual(row[9],128)
        self.assertEqual(row[-1],raw)
    def test_ftm_entry(self):
        typ,row=parse_measurement('FTM_ENTRY,4,1,12345,-55',1.0)
        self.assertEqual(row,[4,1,12345,-55])
    def test_cir_constant_H_impulse(self):
        # Imag=0 real=1 for every subcarrier => delta impulse at tap 0
        raw='0001'*64
        feat=features_from_hex(raw,0)
        self.assertEqual(feat['peak_bin'],0)
        self.assertAlmostEqual(feat['peak_ratio'],1.0)
        self.assertAlmostEqual(feat['rms_delay_ns_relative'],0.0)
    def test_cir_invalid_length(self):
        self.assertIsNone(features_from_hex('0001'*8,0))
    def test_p90(self):
        self.assertAlmostEqual(percentile([1,2,3,4,5],90),4.6)

if __name__=='__main__':unittest.main()
