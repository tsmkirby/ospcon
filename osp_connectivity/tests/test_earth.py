"""KMZ export keeps WGS84 precision and the KML subset Google Earth on the web imports."""

import os
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from osp_connectivity.core.earth import (
    EDITABLE_FEATURE_LIMIT,
    EDITABLE_VERTEX_LIMIT,
    SIMPLIFY_VERTEX_LIMIT,
    build_kml,
    empty_export_message,
    export_note,
    write_kmz,
)

NS = "{http://www.opengis.net/kml/2.2}"


def _kml(layers, **limits):
    kml, summary = build_kml(layers, **limits)
    return kml, summary, ET.fromstring(kml)


class EarthKmlTests(unittest.TestCase):
    def test_xml_text_removes_forbidden_characters_and_preserves_unicode(self):
        forbidden = "\x00\x08\x0b\x0c\x1f\ud800\udfff\ufffe\uffff"
        valid = "Café & <plant> 🛰\t\n"
        kml, summary, root = _kml([{
            "name": "Layer" + forbidden,
            "features": [{
                "name": "AP" + forbidden,
                "kind": "point", "parts": [(-76, 39)],
                "attributes": {"notes" + forbidden: valid + forbidden + "end"},
            }],
        }])
        self.assertEqual(summary["features"], 1)
        self.assertEqual(root.find(f".//{NS}Folder/{NS}name").text, "Layer")
        self.assertEqual(root.find(f".//{NS}Placemark/{NS}name").text, "AP")
        self.assertEqual(root.find(f".//{NS}description").text, "notes: " + valid + "end")
        kml.encode("utf-8")

    def test_polygon_winding_is_normalized_without_changing_vertices(self):
        outer = [(0, 0), (0, 3), (3, 3), (3, 0), (0, 0)]
        hole = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                _, summary, root = _kml([{"features": [{
                    "kind": "polygon", "parts": [{
                        "outer": outer[::-1] if reverse else outer,
                        "holes": [hole[::-1] if reverse else hole],
                    }],
                }]}])
                for boundary, source, sign in (("outerBoundaryIs", outer, 1), ("innerBoundaryIs", hole, -1)):
                    text = root.find(f".//{NS}{boundary}//{NS}coordinates").text
                    points = [tuple(map(float, token.split(','))) for token in text.split()]
                    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:]))
                    self.assertGreater(sign * area, 0)
                    self.assertEqual(sorted(points), sorted(source))
                    self.assertEqual(points[0], points[-1])
                self.assertEqual(summary["vertices"], 10)

    def test_polygon_winding_handles_antimeridian(self):
        ring = [(179, 0), (179, 1), (-179, 1), (-179, 0), (179, 0)]
        _, _, root = _kml([{"features": [{"kind": "polygon", "parts": [{"outer": ring}]}]}])
        points = root.find(f".//{NS}coordinates").text.split()
        self.assertEqual(points[1], "-179.00000000,0.00000000")

    def test_skipped_geometry_is_reported_for_successful_and_empty_exports(self):
        collected = {"skipped_geometry": {"Cables": 2}}
        for message in (export_note({"features": 1}, collected), empty_export_message(collected, False)):
            self.assertIn("empty or unsupported geometry", message)
            self.assertIn("Cables (2)", message)

    def test_earth_web_limits_match_the_published_import_caps(self):
        self.assertEqual(EDITABLE_FEATURE_LIMIT, 10000)
        self.assertEqual(EDITABLE_VERTEX_LIMIT, 250000)
        self.assertEqual(SIMPLIFY_VERTEX_LIMIT, 10000)

    def test_coordinates_are_longitude_latitude_with_eight_decimal_places(self):
        lon, lat = -76.123456789, 39.987654321
        kml, summary, root = _kml([{
            "name": "Access points",
            "role": "ap",
            "features": [{
                "name": "AP-1",
                "attributes": {"ap_id": "AP-1", "status": "installed", "notes": ""},
                "kind": "point",
                "parts": [(lon, lat)],
            }],
        }])
        token = root.find(f".//{NS}coordinates").text.strip()
        expected = f"{lon:.8f},{lat:.8f}"
        self.assertEqual(token, expected)
        self.assertEqual(len(token.split(",")[0].split(".")[1]), 8)
        self.assertTrue(token.startswith("-76."))
        self.assertNotIn(",", token.split(",", 1)[0])
        self.assertEqual(len(token.split(",")), 2)
        self.assertEqual(summary["features"], 1)
        self.assertEqual(summary["vertices"], 1)
        self.assertTrue(summary["within_editable_limits"])
        self.assertEqual(summary["simplified"], [])
        description = root.find(f".//{NS}description").text
        self.assertIn("ap_id: AP-1", description)
        self.assertIn("status: installed", description)
        self.assertNotIn("notes", description)
        self.assertNotIn("<", description)
        point = root.find(f".//{NS}Point")
        self.assertIsNone(point.find(f"{NS}tessellate"))
        self.assertEqual(point.find(f"{NS}altitudeMode").text, "clampToGround")
        self.assertIn("ffc85a14", kml)
        self.assertNotIn("MultiGeometry", kml)
        self.assertNotIn("gx:", kml)

    def test_polygon_holes_lines_and_folders_stay_in_the_web_subset(self):
        kml, summary, root = _kml([
            {
                "name": "Cables & ducts",
                "role": "cable",
                "features": [{
                    "name": "Pole & <north>",
                    "attributes": {"cable_id": "A & B"},
                    "kind": "line",
                    "parts": [
                        [(-76.1, 39.1), (-76.2, 39.2)],
                        [(-76.3, 39.3), (-76.4, 39.4), (-76.5, 39.5)],
                    ],
                }],
            },
            {
                "name": "Buildings",
                "role": "building",
                "features": [{
                    "name": "BLDG-1",
                    "attributes": {},
                    "kind": "polygon",
                    "parts": [{
                        "outer": [(0, 0), (1, 0), (1, 1), (0, 1)],
                        "holes": [[(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]],
                    }],
                }],
            },
        ])
        folders = list(root.iter(f"{NS}Folder"))
        self.assertEqual([folder.find(f"{NS}name").text for folder in folders], ["Cables & ducts", "Buildings"])
        self.assertIn("Cables &amp; ducts", kml)
        placemarks = list(root.iter(f"{NS}Placemark"))
        self.assertEqual([mark.find(f"{NS}name").text for mark in placemarks], ["Pole & <north>", "Pole & <north> (2)", "BLDG-1"])
        self.assertIn("Pole &amp; &lt;north&gt;", kml)
        self.assertIn("A &amp; B", kml)
        line = root.find(f".//{NS}LineString")
        self.assertEqual(line.find(f"{NS}tessellate").text, "1")
        self.assertEqual(line.find(f"{NS}altitudeMode").text, "clampToGround")
        outer = root.find(f".//{NS}outerBoundaryIs").find(f".//{NS}coordinates").text.split()
        hole = root.find(f".//{NS}innerBoundaryIs").find(f".//{NS}coordinates").text.split()
        self.assertEqual(outer[0], outer[-1])
        self.assertEqual(hole[0], hole[-1])
        self.assertEqual(outer[0], "0.00000000,0.00000000")
        self.assertEqual(summary["features"], 3)
        self.assertEqual(summary["vertices"], 2 + 3 + 5 + 5)
        self.assertIn("ff1478e6", kml)
        self.assertNotIn("MultiGeometry", kml)
        modes = [node.text for node in root.iter(f"{NS}altitudeMode")]
        self.assertTrue(modes)
        self.assertTrue(all(mode == "clampToGround" for mode in modes))

    def test_coordinates_outside_wgs84_are_left_out(self):
        _text, summary, root = _kml([{
            "name": "APs",
            "role": "ap",
            "features": [
                {"name": "AP-1", "attributes": {}, "kind": "point", "parts": [(181, 39)]},
                {"name": "AP-2", "attributes": {}, "kind": "point", "parts": [(-76.1, 39.1)]},
            ],
        }])
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["features"], 1)
        self.assertEqual([node.text for node in root.iter(f"{NS}name") if node.text == "AP-2"], ["AP-2"])
        self.assertNotIn("AP-1", [node.text for node in root.iter(f"{NS}name")])

    def test_a_long_part_is_counted_for_the_simplify_warning(self):
        _text, summary, _root = _kml([{
            "name": "Cables",
            "role": "cable",
            "features": [{
                "name": "CB-1",
                "attributes": {},
                "kind": "line",
                "parts": [[(index / 1000.0, 39.0) for index in range(4)]],
            }],
        }], simplify_limit=3)
        self.assertEqual(summary["vertices"], 4)
        self.assertEqual(summary["simplified"], ["CB-1"])
        self.assertTrue(summary["within_editable_limits"])
        note = export_note(summary, {})
        self.assertIn("CB-1", note)
        self.assertIn("simplify", note.lower())
        self.assertIn("every vertex", note)
        self.assertIn("editable project limits", note)

    def test_note_explains_when_earth_must_use_a_data_layer(self):
        note = export_note(
            {
                "features": 10001,
                "vertices": 250001,
                "within_editable_limits": False,
                "simplified": ["CB-1"],
                "rejected": 2,
            },
            {
                "skipped_crs": ["Sketch"],
                "skipped_transform": ["Old"],
                "empty_selection": ["Handholes"],
                "partial_failures": ["Cables (1)"],
            },
        )
        self.assertIn("10,001", note)
        self.assertIn("250,001", note)
        self.assertIn("data layer", note)
        self.assertIn("CB-1", note)
        self.assertIn("Sketch", note)
        self.assertIn("Old", note)
        self.assertIn("Handholes", note)
        self.assertIn("Cables (1)", note)
        self.assertIn("Left out 2", note)
        self.assertIn("Import file to project", note)
        self.assertIn("WGS84", note)
        self.assertIn("phone", note.lower())

    def test_over_the_feature_cap_is_flagged_from_the_placemarks_that_were_written(self):
        _text, summary, _root = _kml([{
            "name": "APs",
            "role": "ap",
            "features": [
                {"name": "AP-1", "attributes": {}, "kind": "point", "parts": [(-76.1, 39.1)]},
                {"name": "AP-2", "attributes": {}, "kind": "point", "parts": [(-76.2, 39.2)]},
            ],
        }], feature_limit=1)
        self.assertEqual(summary["features"], 2)
        self.assertFalse(summary["within_editable_limits"])

    def test_empty_export_names_the_layers_that_could_not_be_used(self):
        message = empty_export_message(
            {"skipped_crs": ["Sketch"], "empty_selection": ["Cables"], "skipped_transform": [], "partial_failures": []},
            selected_only=True,
        )
        self.assertIn("Nothing with a map location", message)
        self.assertIn("selected on the map", message)
        self.assertIn("Sketch", message)
        self.assertIn("Cables", message)

    def test_write_kmz_stores_doc_kml_and_refuses_an_empty_file(self):
        layers = [{
            "name": "APs",
            "role": "ap",
            "features": [{"name": "AP-1", "attributes": {}, "kind": "point", "parts": [(-76.1, 39.1)]}],
        }]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "plant.kmz")
            summary = write_kmz(path, layers)
            self.assertEqual(summary["features"], 1)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ["doc.kml"])
                stored = archive.read("doc.kml").decode("utf-8")
            self.assertTrue(stored.startswith("<?xml"))
            self.assertIn("AP-1", stored)
            with self.assertRaises(ValueError):
                write_kmz(path, [{
                    "name": "APs",
                    "role": "ap",
                    "features": [{"name": "AP-1", "attributes": {}, "kind": "point", "parts": [(181, 39)]}],
                }])
            with zipfile.ZipFile(path) as archive:
                self.assertIn("AP-1", archive.read("doc.kml").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
