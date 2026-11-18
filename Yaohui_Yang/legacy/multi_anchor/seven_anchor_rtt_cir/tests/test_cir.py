import base64
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from cir_features import CIRBuffer, cir_features, parse_csiraw
from localizer_core import StrictEpochLocalizer, parse_mfused


class CITests(unittest.TestCase):
    @staticmethod
    def make_packet(round_id=1, aid=1, mac='aa:bb:cc:dd:ee:01', us=1000000):
        vals=np.zeros(128, dtype=np.int8)
        vals[1::2]=40
        return parse_csiraw('CSIRAW,1,%d,%d,%d,%s,1,20,LLTF,0,%s' % (
            round_id,us,aid,mac,base64.b64encode(vals.tobytes()).decode('ascii')))

    def test_parse_and_ifft_features(self):
        pkt=self.make_packet()
        self.assertIsNotNone(pkt)
        feat=cir_features(pkt)
        self.assertTrue(0 <= feat['cir_peak_ratio'] <= 1)
        self.assertTrue(0 <= feat['cir_entropy'] <= 1)
        self.assertGreaterEqual(feat['cir_rms_delay_ns'],0)

    def test_reject_corrupted_serial_data(self):
        self.assertIsNone(parse_csiraw('CSIRAW,1,1,1,1,aa,1,20,LLTF,0,***'))
        self.assertIsNone(parse_csiraw('CSIRAW,1,1,1,1,aa,1,40,LLTF,0,***'))

    def test_match_round_mac_time(self):
        buf=CIRBuffer(tolerance_ms=100)
        buf.ingest(self.make_packet())
        self.assertEqual(buf.metrics(1,'1',1000000,'aa:bb:cc:dd:ee:01')['cir_packets'],1)
        self.assertEqual(buf.metrics(2,'1',1000000,'aa:bb:cc:dd:ee:01')['cir_packets'],0)
        self.assertEqual(buf.metrics(1,'1',1000000,'aa:bb:cc:dd:ee:02')['cir_packets'],0)
        self.assertEqual(buf.metrics(1,'1',2000000,'aa:bb:cc:dd:ee:01')['cir_packets'],0)

    def test_cir_mode_does_not_use_csi_fallback(self):
        root=Path(__file__).resolve().parents[1]
        config=json.loads((root/'anchors_7.json').read_text(encoding='utf8'))
        config['cir_weighting']['mode']='cir'
        with tempfile.TemporaryDirectory() as temp:
            loc=StrictEpochLocalizer(config,Path(temp))
            try:
                loc.start_epoch(1,100000)
                for aid in [1,2,3,4]:
                    line=f'MFUSED,1,1,100000,{aid},0,100,500,500,500,-55,8,6,-52,1,50,2,0.3,0,5,5,0,aa:bb:cc:dd:ee:{aid:02x},-50'
                    m=parse_mfused(line)
                    self.assertIsNotNone(m)
                    loc.ingest(m)
                self.assertIsNone(loc.finalize(1,100000,200000,100000,4))
            finally:
                loc.close()


if __name__ == '__main__':
    unittest.main()
