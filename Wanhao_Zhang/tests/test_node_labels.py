from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

MODULE_DIR = pathlib.Path(__file__).parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))
import node_labels as nl  # noqa: E402


class VariantTests(unittest.TestCase):
    """Recognise a registered address and its final-byte increment."""

    def test_expands_to_base_and_next(self):
        self.assertEqual(nl.mac_variants("24:ec:4a:04:76:04"),
                         {"24:EC:4A:04:76:04", "24:EC:4A:04:76:05"})

    def test_last_byte_ff_wraps_without_crashing(self):
        self.assertEqual(nl.mac_variants("24:ec:4a:04:76:ff"),
                         {"24:EC:4A:04:76:FF", "24:EC:4A:04:76:00"})

    def test_junk_is_returned_as_is(self):
        self.assertEqual(nl.mac_variants("garbage"), {"GARBAGE"})

    def test_prefix_of_is_display_only(self):
        self.assertEqual(nl.prefix_of("24:ec:4a:04:76:04"), "24:EC:4A:04:76")


class TableTests(unittest.TestCase):
    def _write(self, payload: dict) -> pathlib.Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(lambda: pathlib.Path(tmp.name).unlink(missing_ok=True))
        return pathlib.Path(tmp.name)

    def test_loads_full_mac_keys(self):
        path = self._write({"nodes": {"A7": {"mac": "24:ec:4a:04:76:04"},
                                      "A1": {"mac": "24:ec:4a:04:73:d0"}}})
        table = nl.load_table(path)
        self.assertEqual(table["24:EC:4A:04:76:04"], "A7")
        self.assertEqual(table["24:EC:4A:04:73:D0"], "A1")

    def test_accepts_plain_string_values(self):
        path = self._write({"A7": "24:ec:4a:04:76:04"})
        self.assertEqual(nl.load_table(path)["24:EC:4A:04:76:04"], "A7")

    def test_undocumented_keys_are_ignored(self):
        path = self._write({"_note": "说明", "_matching": "说明",
                            "nodes": {"A7": "24:ec:4a:04:76:04"}})
        table = nl.load_table(path)
        self.assertNotIn("_NOTE", table)
        self.assertEqual(len(table), 1)

    def test_missing_or_broken_file_yields_empty_table(self):
        self.assertEqual(nl.load_table(pathlib.Path("does-not-exist.json")), {})
        path = self._write({"nodes": {}})
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(nl.load_table(path), {})


class LabelTests(unittest.TestCase):
    TABLE = {"24:EC:4A:04:76:04": "A7", "24:EC:4A:04:39:04": "A0"}

    def test_base_and_sta_map_to_the_same_node(self):
        self.assertEqual(nl.label_for("24:ec:4a:04:76:04", self.TABLE), "A7")
        self.assertEqual(nl.label_for("24:EC:4A:04:76:05", self.TABLE), "A7")

    def test_unregistered_mac_falls_back_to_last_two_bytes(self):
        self.assertEqual(nl.label_for("AA:BB:CC:DD:EE:FF", self.TABLE), "EE:FF")

    def test_resolve_by_name_returns_both_variants(self):
        self.assertEqual(nl.resolve("A7", self.TABLE),
                         ["24:EC:4A:04:76:04", "24:EC:4A:04:76:05"])

    def test_resolve_by_mac_returns_both_variants(self):
        self.assertEqual(nl.resolve("24:ec:4a:04:76:04", self.TABLE),
                         ["24:EC:4A:04:76:04", "24:EC:4A:04:76:05"])

    def test_resolve_passes_through_unknown_tokens(self):
        """Preserve short tokens for legacy interactive matching."""
        self.assertEqual(nl.resolve("EE:FF", self.TABLE), ["EE:FF"])


class NearCollisionTests(unittest.TestCase):
    """Keep addresses with the same first five octets distinct."""

    TABLE = {"24:EC:4A:04:39:04": "A0", "24:EC:4A:04:39:14": "A5"}

    def test_two_boards_differing_only_in_last_byte_stay_distinct(self):
        self.assertEqual(nl.label_for("24:ec:4a:04:39:04", self.TABLE), "A0")
        self.assertEqual(nl.label_for("24:ec:4a:04:39:05", self.TABLE), "A0")
        self.assertEqual(nl.label_for("24:ec:4a:04:39:14", self.TABLE), "A5")
        self.assertEqual(nl.label_for("24:ec:4a:04:39:15", self.TABLE), "A5")

    def test_a0_does_not_match_a5_and_vice_versa(self):
        self.assertFalse(nl.matches("24:ec:4a:04:39:04", "A5", self.TABLE))
        self.assertFalse(nl.matches("24:ec:4a:04:39:14", "A0", self.TABLE))
        self.assertTrue(nl.matches("24:ec:4a:04:39:04", "A0", self.TABLE))
        self.assertTrue(nl.matches("24:ec:4a:04:39:14", "A5", self.TABLE))

    def test_partial_mac_still_works(self):
        self.assertTrue(nl.matches("24:ec:4a:04:39:04", "39:04", self.TABLE))
        self.assertTrue(nl.matches("24:ec:4a:04:39:14", "39:14", self.TABLE))
        self.assertFalse(nl.matches("24:ec:4a:04:39:14", "39:04", self.TABLE))


class ExampleTableTests(unittest.TestCase):
    """Exercise the distributed registry without a private deployment file."""

    EXAMPLE_PATH = MODULE_DIR.parent / "examples" / "nodes.example.json"

    def test_example_table_parses_full_mac_addresses(self):
        document = json.loads(self.EXAMPLE_PATH.read_text(encoding="utf-8"))
        table = nl.load_table(self.EXAMPLE_PATH)
        self.assertEqual(set(table.values()), {f"A{i}" for i in range(7)})
        self.assertEqual(len(table), 7)
        for label, info in document["nodes"].items():
            with self.subTest(label=label):
                base, ap = info["mac"], info["ap_mac"]
                self.assertEqual(table[base], label)
                self.assertEqual(nl.label_for(base, table), label)
                self.assertEqual(nl.label_for(ap, table), label)
                self.assertEqual(set(nl.resolve(label, table)), {base, ap})

    def test_example_boards_are_mutually_distinguishable(self):
        table = nl.load_table(self.EXAMPLE_PATH)
        variants = {address: label for base, label in table.items()
                    for address in nl.mac_variants(base)}
        self.assertEqual(len(variants), 14)
        for address, expected in variants.items():
            for label in table.values():
                with self.subTest(address=address, label=label):
                    self.assertEqual(nl.matches(address, label, table), label == expected)


if __name__ == "__main__":
    unittest.main()
