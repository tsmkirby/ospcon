"""Printable circuit cards built from a trace report."""

import os
import tempfile
import unittest

from osp_connectivity.core.cards import circuit_card_pages, write_circuit_cards
from osp_connectivity.core.trace import trace_selection
from osp_connectivity.tests.test_core import feature


class CircuitCardTests(unittest.TestCase):
    def test_card_lists_the_path_from_the_trace(self):
        cable = feature(
            "cable",
            1,
            "vib.cpr.0008",
            {
                "media": "copper",
                "cable_type": "CAT6 flooded",
                "a_end": "VIB-AP-0004",
                "z_end": "VIB-IDF-0002",
                "z_panel": "RR02.PP01",
                "z_port": "14",
                "length_f": "120",
            },
            layer_id="cables",
        )
        ap = feature(
            "ap",
            4,
            "VIB-AP-0004",
            {"cable_ref": "vib.cpr.0008", "mount": "VIB-PL-0002", "panel": "RR02.PP01", "port": "14"},
            layer_id="aps",
            geometry="Point",
            fields={"cable_ref": "cable_id", "mount": "mount_structure_id", "panel": "patch_panel_id", "port": "patch_panel_port"},
        )
        idf = feature("idf", 2, "VIB-IDF-0002", layer_id="idfs", geometry="Point")
        pole = feature("structure", 9, "VIB-PL-0002", layer_id="poles", geometry="Point")
        routes = [
            {"cable_id": "vib.cpr.0008", "segment_id": "0001", "route_order": 1},
            {"cable_id": "vib.cpr.0008", "segment_id": "0002", "route_order": 2},
        ]
        result = trace_selection([ap], [cable, ap, idf, pole], {"routes": routes, "splices": [], "circuits": []})
        pages = circuit_card_pages(result["reports"])
        text = "\n".join(block["text"] for block in pages[0]["blocks"])
        self.assertIn("OSP CIRCUIT CARD", text)
        self.assertIn("VIB-AP-0004", text)
        self.assertIn("VIB-PL-0002", text)
        self.assertIn("vib.cpr.0008", text)
        self.assertIn("CAT6 flooded", text)
        self.assertIn("120 ft", text)
        self.assertIn("0001 > 0002", text)
        self.assertIn("RR02.PP01 port 14 @ VIB-IDF-0002", text)
        self.assertEqual(len(pages), 1)

        handle, path = tempfile.mkstemp(suffix=".pdf")
        os.close(handle)
        try:
            count = write_circuit_cards(path, result["reports"])
            with open(path, "rb") as handle:
                data = handle.read()
        finally:
            os.remove(path)
        self.assertEqual(count, 1)
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertIn(b"VIB-AP-0004", data)
        self.assertIn(b"%%EOF", data)

    def test_long_strand_list_continues_on_the_next_page(self):
        report = {
            "device_id": "AP-1",
            "cable_id": "FB-1",
            "idf_id": "IDF-1",
            "panel_id": "FD-1",
            "panel_port": "1",
            "strand_lines": [f"FB-1 strand {number} expressed to FB-2 strand {number}" for number in range(1, 80)],
        }
        pages = circuit_card_pages([report])
        self.assertGreater(len(pages), 1)
        continued = "\n".join(block["text"] for block in pages[1]["blocks"])
        self.assertIn("AP-1 (continued)", continued)
        self.assertIn("strand 79", "\n".join(block["text"] for page in pages for block in page["blocks"]))

    def test_empty_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            circuit_card_pages([])


if __name__ == "__main__":
    unittest.main()
