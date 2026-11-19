from __future__ import annotations

import base64
import csv
import json
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_DIR = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))
import wipro_live as live  # noqa: E402


def frame(kind: str, fields: list[str]) -> bytes:
    """Encode a frame with the firmware's start, separator and end bytes."""
    body = "\x01".join([kind] + fields).encode()
    return b"\x02" + body + b"\x03"


def ftm_report(t1: int, t2: int, t3: int, t4: int, rssi: int = -55,
               token: int = 7) -> bytes:
    """Encode a 40-byte report with RSSI and four little-endian u64 timestamps."""
    return (bytes([token, rssi & 0xFF, 0, 0]) + bytes(4)
            + struct.pack("<QQQQ", t1, t2, t3, t4))


class FrameTests(unittest.TestCase):
    def test_split_drops_esp_idf_log_lines(self):
        """Extract TLV frames from a stream containing diagnostic text."""
        stream = (b"I (98314) firmware::scan: Scan results:\r\n"
                  + frame("RANGE", ["AA:BB:CC:DD:EE:01", "123", "4.56"])
                  + b"more log text \x02 not a real frame\r\n"
                  + frame("RANGE", ["AA:BB:CC:DD:EE:02", "124", "7.89"]))
        buffer = bytearray(stream)
        got = list(live.iter_frames(buffer))
        self.assertEqual([k for k, _ in got], ["RANGE", "RANGE"])
        self.assertEqual(got[0][1][2], "4.56")
        self.assertEqual(got[1][1][0], "AA:BB:CC:DD:EE:02")

    def test_partial_frame_is_kept_until_the_rest_arrives(self):
        buffer = bytearray()
        whole = frame("RANGE", ["AA:BB:CC:DD:EE:01", "1", "2.5"])
        buffer.extend(whole[:12])
        self.assertEqual(list(live.iter_frames(buffer)), [])
        buffer.extend(whole[12:])
        self.assertEqual(len(list(live.iter_frames(buffer))), 1)

    def test_trailing_garbage_is_discarded(self):
        buffer = bytearray(b"\x01\x02\x03 no start byte at all")
        self.assertEqual(list(live.iter_frames(buffer)), [])
        self.assertEqual(len(buffer), 0)


class FtmTests(unittest.TestCase):
    def test_raw_distance_from_the_four_timestamps(self):
        """Subtract turnaround time before converting picoseconds to metres."""
        distance = 8.0
        rtt_ps = int(round(distance / live.PS_TO_M))
        t1, t2, t3 = 1000, 2_000_000, 3_000_000
        t4 = rtt_ps + t1 + t3 - t2
        payload = base64.b64encode(ftm_report(t1, t2, t3, t4)).decode()
        fields = ["AA:BB:CC:DD:EE:01", "1", payload,
                  "CSI", "6", "1",
                  base64.b64encode(b"\x01\x02").decode(), str(t2 // 1000), "999"]
        peer, reports = live.parse_ftm(fields)
        self.assertEqual(peer, "AA:BB:CC:DD:EE:01")
        self.assertEqual(len(reports), 1)
        self.assertAlmostEqual(reports[0]["raw_m"], distance, delta=1e-3)
        self.assertEqual(reports[0]["rssi"], -55)
        # CSI's microsecond timestamp corresponds to the report's t2.
        self.assertTrue(reports[0]["csi_b64"])

    def test_multiple_reports_in_one_payload(self):
        """Decode every 40-byte report in a burst payload."""
        rtt = int(round(5.0 / live.PS_TO_M))
        blob = b"".join(ftm_report(1000, 10_000, 20_000, rtt + 1000 + 20_000 - 10_000)
                        for _ in range(4))
        payload = base64.b64encode(blob).decode()
        _, reports = live.parse_ftm(["PEER", "4", payload])
        self.assertEqual(len(reports), 4)
        self.assertAlmostEqual(reports[0]["raw_m"], 5.0, delta=1e-3)

    def test_separate_payload_fields_retain_first_firmware_report(self):
        first = base64.b64encode(ftm_report(1000, 10000, 20000, 30000, token=1)).decode()
        second = base64.b64encode(ftm_report(2000, 12000, 23000, 35000, token=2)).decode()
        _, reports = live.parse_ftm(["AA:BB:CC:DD:EE:01", "2", first, second])
        self.assertEqual([row['token'] for row in reports], [1, 2])
        self.assertEqual([row['t2'] for row in reports], [10000, 12000])

    def test_wraparound_is_handled(self):
        mask = 0xFFFFFFFFFFFF
        self.assertEqual(live.diff_48bit(5, mask - 2), 8)
        self.assertEqual(live.diff_48bit(mask - 2, 5), -8)


def csi_fields(tgt_mac: str = "AA:BB:CC:DD:EE:FF", rssi: int = -60,
               n_bytes: int = 128, channel: int = 6, channel2: int = 1,
               seq: int = 7) -> list[str]:
    """Construct synthetic CSI fields with an encoded-character length field."""
    payload = base64.b64encode(bytes(n_bytes)).decode()
    return [f"{0x1f:x}", str(seq), tgt_mac, "35000000000", str(len(payload)),
            payload, str(channel), str(channel2), str(rssi), "0"]


class CsiValidationTests(unittest.TestCase):
    def test_accepts_a_well_formed_frame(self):
        event = live.parse_csi(csi_fields())
        self.assertIsNotNone(event)
        self.assertEqual(event["size"], 172)
        self.assertEqual(len(base64.b64decode(event["csi_b64"])), 128)
        self.assertEqual(event["rssi"], -60)

    def test_rejects_incomplete_mac(self):
        self.assertIsNone(live.parse_csi(csi_fields(tgt_mac="AA:BB")))

    def test_rejects_size_mismatch(self):
        fields = csi_fields()
        fields[4] = "999"
        self.assertIsNone(live.parse_csi(fields))

    def test_rejects_non_base64_payload(self):
        fields = csi_fields()
        fields[5] = "!" * len(fields[5])
        self.assertIsNone(live.parse_csi(fields))

    def test_rejects_odd_byte_count(self):
        """Complex CSI samples require pairs of bytes."""
        payload = base64.b64encode(b"\x01\x02\x03").decode()
        fields = csi_fields()
        fields[4], fields[5] = str(len(payload)), payload
        self.assertIsNone(live.parse_csi(fields))

    def test_rejects_wrong_field_count(self):
        self.assertIsNone(live.parse_csi(csi_fields()[:9]))
        self.assertIsNone(live.parse_csi(csi_fields() + ["extra"]))


class MergeTests(unittest.TestCase):
    def setUp(self):
        # Register directory cleanup before file cleanup so files close first.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def _capture(self) -> live.Capture:
        capture = live.Capture("COM_TEST", self.root, [])
        self.addCleanup(capture.close)
        return capture

    def test_range_and_ftm_merge_on_t2(self):
        """RANGE and FTM observations with the same t2 share one sample."""
        peer = "AA:BB:CC:DD:EE:01"
        capture = self._capture()
        capture._handle("RANGE", [peer, "4242", "9.75"])
        capture._handle("FTM", [peer, "1", "x",
                                base64.b64encode(
                                    ftm_report(100, 4242, 300, 4242 + 200)).decode()])
        rows = capture.snapshot()["series"][peer]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][1], 9.75)
        self.assertIsNotNone(rows[0][2])
        self.assertEqual(rows[0][4], 4242)

    def test_malformed_ftm_is_dropped_not_crashed(self):
        """Reject an incomplete peer address and count the rejected frame."""
        capture = self._capture()
        capture._handle("FTM", ["AA:BB", "1", "x", ""])
        snap = capture.snapshot()
        self.assertEqual(snap["dropped"], {"FTM": 1})
        self.assertEqual(snap["series"], {})

    def test_different_t2_creates_separate_points(self):
        capture = self._capture()
        capture._handle("RANGE", ["AA:BB", "1", "1.0"])
        capture._handle("RANGE", ["AA:BB", "2", "2.0"])
        self.assertEqual(len(capture.snapshot()["series"]["AA:BB"]), 2)

    def test_csv_headers_match_the_controller(self):
        """Retain the controller-compatible CSV field order."""
        capture = self._capture()
        capture._handle("RANGE", ["AA:BB", "1", "3.5"])
        capture._handle("CSI", csi_fields())
        capture.close()
        self.assertEqual((self.root / "range.csv").read_text().splitlines()[0],
                         "t_ms,own_mac,tgt_mac,timestamp,range")
        self.assertEqual((self.root / "csi.csv").read_text().splitlines()[0],
                         "t_ms,timestamp,own_mac,tgt_mac,seq,payload_b64,"
                         "mac_timestamp,rssi,sig_mode")

    def test_csi_frame_field_mapping(self):
        event = live.parse_csi(csi_fields(rssi=-77, seq=9))
        self.assertEqual(event["mac_timestamp"], 0x1f)
        self.assertEqual(event["seq"], 9)
        self.assertEqual(event["tgt_mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(event["size"], 172)
        self.assertEqual(event["channel"], 6)
        self.assertEqual(event["rssi"], -77)
        self.assertIsNone(live.parse_csi(["too", "few"]))


def radio_stream_fixture() -> bytes:
    """Build 31 synthetic CSI frames, with diagnostic text corrupting frame 15."""
    stream = bytearray(b"I (100) radio: capture started\r\n")
    for sequence in range(31):
        fields = csi_fields(seq=sequence, rssi=-50 - sequence // 2)
        if sequence == 15:
            fields[5] = fields[5][:70] + "I (200) radio: status\r\n" + fields[5][70:]
        stream.extend(frame("CSI", fields))
        stream.extend(b"I (300) radio: receiving\r\n")
    return bytes(stream)


class RadioStreamTests(unittest.TestCase):
    """Exercise stream recovery and field decoding with an explicit synthetic fixture."""

    def test_corrupted_frame_does_not_drop_neighboring_frames(self):
        raw = radio_stream_fixture()
        with tempfile.TemporaryDirectory() as directory:
            capture = live.Capture("REPLAY", pathlib.Path(directory), [])
            try:
                buffer = bytearray()
                for offset in range(0, len(raw), 71):
                    buffer.extend(raw[offset:offset + 71])
                    for kind, fields in live.iter_frames(buffer):
                        capture._handle(kind, fields)
                snap = capture.snapshot()
            finally:
                capture.close()
            total = sum(snap["counts"].values())
            dropped = sum(snap["dropped"].values())
            self.assertEqual(total, 31)
            self.assertEqual(dropped, 1)
            self.assertEqual(snap["counts"]["CSI"] - snap["dropped"]["CSI"], 30)
            with (pathlib.Path(directory) / "csi.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 30)
            self.assertEqual([int(row["seq"]) for row in rows],
                             [sequence for sequence in range(31) if sequence != 15])

    def test_decoded_frames_preserve_radio_fields(self):
        raw = radio_stream_fixture()
        events = [live.parse_csi(f) for k, f in live.iter_frames(bytearray(raw))
                  if k == "CSI"]
        good = [e for e in events if e]
        self.assertTrue(good)
        for event in good:
            self.assertEqual(event["channel"], 6)
            self.assertEqual(event["channel2"], 1)
            self.assertLess(event["rssi"], 0)
            self.assertGreater(event["rssi"], -100)
            self.assertEqual(event["sig_mode"], 0)
            # The synthetic legacy payload has 64 complex samples.
            self.assertEqual(event["size"], 172)
            self.assertEqual(len(base64.b64decode(event["csi_b64"])), 128)


class MacFrameTests(unittest.TestCase):
    """Identify the local radio from MAC frames and registered full addresses."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_mac_frame_sets_own_mac(self):
        capture = live.Capture("REPLAY", pathlib.Path(self._tmp.name), [])
        self.addCleanup(capture.close)
        stream = frame("MAC", ["24:EC:4A:04:39:04"])
        for kind, fields in live.iter_frames(bytearray(stream)):
            capture._handle(kind, fields)
        self.assertEqual(capture.snapshot()["own_mac"], "24:EC:4A:04:39:04")

    def test_mac_frame_is_a_known_kind(self):
        """Accept the MAC response to the firmware's id command."""
        self.assertIn("MAC", live.KNOWN_KINDS)
        got = list(live.iter_frames(bytearray(frame("MAC", ["AA:BB:CC:DD:EE:FF"]))))
        self.assertEqual(got, [("MAC", ["AA:BB:CC:DD:EE:FF"])])

    def test_malformed_mac_frame_is_ignored(self):
        capture = live.Capture("REPLAY", pathlib.Path(self._tmp.name), [])
        self.addCleanup(capture.close)
        for kind, fields in live.iter_frames(bytearray(frame("MAC", ["nonsense"]))):
            capture._handle(kind, fields)
        self.assertEqual(capture.snapshot()["own_mac"], "UNKNOWN")

    def test_node_label_applies_to_own_mac(self):
        registry = pathlib.Path(__file__).parents[1] / "examples" / "nodes.example.json"
        nodes = json.loads(registry.read_text(encoding="utf-8"))["nodes"]
        with patch.object(live.node_labels, "DEFAULT_PATH", registry):
            capture = live.Capture("REPLAY", pathlib.Path(self._tmp.name), [])
        self.addCleanup(capture.close)
        for address in (nodes["A0"]["mac"], nodes["A0"]["ap_mac"]):
            with self.subTest(address=address):
                capture._handle("MAC", [address.upper()])
                self.assertEqual(capture.snapshot()["own_label"], "A0")
        capture._handle("MAC", ["02:00:00:00:00:10"])
        self.assertNotEqual(capture.snapshot()["own_label"], "A0")


if __name__ == "__main__":
    unittest.main()
