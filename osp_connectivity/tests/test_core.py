"""Rules that have to hold for both the OSP template and the live NCL project."""

import unittest

from osp_connectivity.core.associate import assignment_edits, port_in_use, propose
from osp_connectivity.core.catalog import build_index, end_convention
from osp_connectivity.core.fields import enclosure_id, media_of, next_id, parse_count, parse_range
from osp_connectivity.core.geometry import chain_segments, ducts_along_cable, split_linestring
from osp_connectivity.core.link import plan_links
from osp_connectivity.core.roles import classify
from osp_connectivity.core.strands import build_splice_rows, compress_splices, validate_edits
from osp_connectivity.core.trace import format_report, trace_selection


def feature(role, fid, identifier, values=None, coords=None, layer_id="L", fields=None, geometry="Line"):
    values = dict(values or {})
    if identifier is not None:
        values.setdefault("id", identifier)
    field_map = {"id": "id"}
    if fields:
        field_map.update(fields)
    return {
        "layer_id": layer_id,
        "layer_name": layer_id,
        "fid": fid,
        "role": role,
        "geometry": geometry,
        "coords": coords,
        "id": identifier,
        "fields": field_map,
        "values": values,
    }


class SchemaTests(unittest.TestCase):
    def test_template_and_ncl_layers_classify(self):
        self.assertEqual(classify("Line", ["cable_id", "media_type", "gis_length_m"]), "cable")
        self.assertEqual(classify("Line", ["segment_id", "duct_config"]), "duct")
        self.assertEqual(classify("Point", ["ap_id", "support_id"]), "ap")
        self.assertEqual(classify("Point", ["cctv_id"]), "cctv")
        self.assertEqual(classify("Polygon", ["mdf_idf_id"]), "idf")
        self.assertEqual(classify("Point", ["pp_id", "mdf_idf_id", "port_count"]), "patch_panel")
        self.assertEqual(classify("Point", ["fdp_id", "position_count"]), "fdp")
        self.assertEqual(classify("Point", ["splice_enc_id", "midspan_access"]), "splice_enc")
        self.assertEqual(classify("Point", ["access_id", "cover_material"]), "handhole")
        self.assertEqual(classify("Point", ["support_id", "support_type"]), "structure")
        self.assertEqual(classify("None", ["cable_id", "strand_no", "tube_no"]), "fiber_strand")
        self.assertEqual(classify("None", ["a_cable_id", "a_strand_no", "b_cable_id", "b_strand_no"]), "fiber_splice")
        self.assertEqual(classify("None", ["cable_id", "segment_id", "route_order"]), "cable_route")

        self.assertEqual(classify("Line", ["cable_id", "a_end_location", "z_end_location", "strand_count"]), "cable")
        self.assertEqual(classify("Line", ["segment_id", "conduit_capacity"]), "duct")
        self.assertEqual(classify("Line", ["segment_id", "cconduit_capacity"]), "duct")
        self.assertEqual(classify("Point", ["ap_id", "patch_pane_id", "mount_structure_id"]), "ap")
        self.assertEqual(classify("Point", ["idf_id", "venue_code"]), "idf")
        self.assertEqual(classify("Point", ["structure_id", "wifi_present"]), "structure")
        self.assertEqual(classify("Point", ["structure_name", "structure_type", "lid_material", "wall_material"]), "handhole")
        self.assertEqual(
            classify("Line", ["id", "fiber_count", "cable_type", "conduit_id"]),
            "cable",
        )

    def test_media_and_counts(self):
        self.assertEqual(media_of({"media": "aerial_fiber"}), "fiber")
        self.assertEqual(media_of({"media": "copper", "cable_type": "CAT6 flooded"}), "copper")
        self.assertEqual(media_of({"media": "fiber", "cable_type": "OS2", "strand_count": "12F"}), "fiber")
        self.assertEqual(media_of({"cable_type": "armored adcc", "strand_count": 24}), "fiber")
        self.assertEqual(parse_count("12F"), 12)
        self.assertEqual(parse_count("72"), 72)
        self.assertEqual(parse_range("1-2"), [1, 2])
        self.assertEqual(parse_range("47-48"), [47, 48])

    def test_ids_follow_the_layer_they_came_from(self):
        self.assertEqual(next_id(["vib.fib.0001", "vib.fib.0004"], "vib.fib.0001"), "vib.fib.0005")
        self.assertEqual(next_id(["VIB-CB-0002"], "VIB-CB-0001"), "VIB-CB-0003")
        self.assertTrue(enclosure_id([], "VIB-CB-0001", True).startswith("VIB-EN-"))
        self.assertTrue(enclosure_id([], "vib.fib.0001", False).startswith("vib.enc."))


class GeometryTests(unittest.TestCase):
    def test_ducts_chain_and_follow_a_cable(self):
        cable = [(0, 0), (10, 0)]
        first = {"fid": 1, "coords": [(0, 0), (5, 0)]}
        second = {"fid": 2, "coords": [(5, 0), (10, 0)]}
        third = {"fid": 3, "coords": [(0, 5), (5, 5)]}
        ordered, clean = chain_segments([second, first], 0.5)
        self.assertTrue(clean)
        self.assertEqual([item["fid"] for item in ordered], [1, 2])
        along = ducts_along_cable(cable, [third, second, first], 0.5)
        self.assertEqual([item["fid"] for item in along], [1, 2])

    def test_split_keeps_the_vertex_on_both_sides(self):
        result = split_linestring([(0, 0), (10, 0)], (4, 0.1), 0.5)
        self.assertEqual(result["kind"], "through")
        self.assertEqual(result["a"][0], (0, 0))
        self.assertAlmostEqual(result["a"][-1][0], 4.0, places=5)
        self.assertAlmostEqual(result["b"][0][0], 4.0, places=5)
        self.assertEqual(result["b"][-1], (10, 0))
        self.assertEqual(split_linestring([(0, 0), (10, 0)], (0, 0), 0.5)["kind"], "endpoint")
        self.assertEqual(split_linestring([(0, 0), (10, 0)], (4, 5), 0.5)["kind"], "miss")


class StrandTests(unittest.TestCase):
    def test_fusion_leaves_the_rest_expressed_on_the_same_numbers(self):
        rows = build_splice_rows(
            144,
            "A",
            "B",
            "ENC",
            fusions=[{"a_from": 47, "a_to": 48, "b_cable_id": "C", "b_start": 1}],
        )
        text = " ".join(compress_splices(rows))
        self.assertIn("A strands 1-46 express to B strands 1-46", text)
        self.assertIn("A strands 47-48 fusion to C strands 1-2", text)
        self.assertIn("A strands 49-144 express to B strands 49-144", text)
        self.assertEqual(len(rows), 144)
        problems = validate_edits(
            144,
            [{"a_from": 47, "a_to": 48, "b_cable_id": "C", "b_start": 1}],
            [{"a_from": 48, "a_to": 49}],
        )
        self.assertTrue(problems)


class LinkTests(unittest.TestCase):
    def test_vibe_copper_fills_a_blank_device_and_does_not_overwrite(self):
        cable = feature(
            "cable",
            1,
            "vib.cpr.0001",
            {
                "media": "copper",
                "cable_type": "CAT6 flooded",
                "a_end": "VIB-AP-0005",
                "z_end": "VIB-IDF-0002",
                "status": "proposed",
            },
            layer_id="cables",
            geometry="Line",
        )
        ap = feature(
            "ap",
            5,
            "VIB-AP-0005",
            {"cable_ref": None, "mount": "VIB-PL-0003", "uplink": "copper"},
            layer_id="aps",
            geometry="Point",
            fields={"cable_ref": "cable_id", "mount": "mount_structure_id", "panel": "patch_panel_id", "port": "patch_panel_port"},
        )
        idf = feature("idf", 2, "VIB-IDF-0002", layer_id="idfs", geometry="Point")
        pole = feature("structure", 3, "VIB-PL-0003", layer_id="poles", geometry="Point")
        plan = plan_links([cable, ap, idf, pole], spatial_routes=False, write_mount_rows=False)
        edits = {(edit.layer_id, edit.fid, edit.canonical): edit.value for edit in plan.edits}
        self.assertEqual(edits[("aps", 5, "cable_ref")], "vib.cpr.0001")
        self.assertTrue(any(insert.role == "circuit" for insert in plan.inserts))

        ap["values"]["cable_ref"] = "vib.cpr.0001"
        again = plan_links([cable, ap, idf, pole], spatial_routes=False, write_mount_rows=False, existing_circuits={("vib-ap-0005", "vib.cpr.0001")}, existing_mounts={("vib-ap-0005", "vib-pl-0003")})
        self.assertFalse(any(edit.canonical == "cable_ref" for edit in again.edits))

    def test_placeholder_cable_ids_are_left_alone(self):
        ap = feature(
            "ap",
            1,
            "VIB-AP-0024",
            {"cable_ref": "unk"},
            layer_id="aps",
            geometry="Point",
            fields={"cable_ref": "cable_id"},
        )
        plan = plan_links([ap], spatial_routes=False, write_mount_rows=False)
        self.assertFalse(plan.edits)
        self.assertTrue(any("unk" in notice.message for notice in plan.notices))

    def test_duplicate_fiber_ids_are_reported(self):
        first = feature("cable", 1, "vib.fib.0001", {"media": "fiber", "a_end": "VIB-IDF-0001", "z_end": "VIB-IDF-0002"}, layer_id="fiber")
        second = feature("cable", 2, "vib.fib.0001", {"media": "fiber", "a_end": "VIB-IDF-0002", "z_end": "VIB-IDF-0001"}, layer_id="copper")
        plan = plan_links([first, second], spatial_routes=False)
        self.assertTrue(any("not merged" in notice.message for notice in plan.notices))
        self.assertFalse(plan.inserts)

    def test_end_convention_matches_vibe_copper(self):
        cable = feature("cable", 1, "vib.cpr.0001", {"a_end": "VIB-AP-0005", "z_end": "VIB-IDF-0002"})
        ap = feature("ap", 5, "VIB-AP-0005", geometry="Point")
        idf = feature("idf", 2, "VIB-IDF-0002", geometry="Point")
        features = [cable, ap, idf]
        self.assertEqual(end_convention([cable], build_index(features)), "a_device")


class AssociateAndTraceTests(unittest.TestCase):
    def _path(self):
        cable = feature(
            "cable",
            1,
            "vib.cpr.0008",
            {"media": "copper", "cable_type": "CAT6 flooded", "length_f": "120"},
            coords=[(0, 0), (10, 0)],
            layer_id="cables",
            fields={"a_end": "a_end_location", "z_end": "z_end_location", "a_panel": "a_end_panel_id", "z_panel": "z_end_panel_id", "a_port": "a_end_port", "z_port": "z_end_port"},
        )
        ap = feature(
            "ap",
            4,
            "VIB-AP-0004",
            {"mount": "VIB-PL-0002"},
            coords=[(0, 0)],
            layer_id="aps",
            geometry="Point",
            fields={"cable_ref": "cable_id", "panel": "patch_panel_id", "port": "patch_panel_port", "mount": "mount_structure_id"},
        )
        idf = feature("idf", 2, "VIB-IDF-0002", coords=[(10, 0)], layer_id="idfs", geometry="Point")
        pole = feature("structure", 9, "VIB-PL-0002", coords=[(0, 0)], layer_id="poles", geometry="Point")
        ducts = [
            feature("duct", 1, "0001", coords=[(0, 0), (5, 0)], layer_id="db", geometry="Line"),
            feature("duct", 2, "0002", coords=[(5, 0), (10, 0)], layer_id="db", geometry="Line"),
        ]
        return [cable, ap, idf, pole, *ducts]

    def test_selection_writes_vibe_ends_device_on_a(self):
        features = self._path()
        proposal = propose(features, features, 0.5)
        self.assertTrue(proposal.ready)
        self.assertEqual(proposal.media, "copper")
        self.assertEqual(proposal.convention, "unknown")
        result = assignment_edits(
            proposal,
            {"a_role": "device", "z_role": "idf", "idf_id": "VIB-IDF-0002", "panel_id": "RR02.PP01", "port": "14"},
        )
        edits = {(edit["fid"], edit["canonical"]): edit["value"] for edit in result["edits"]}
        self.assertEqual(edits[(1, "a_end")], "VIB-AP-0004")
        self.assertEqual(edits[(1, "z_end")], "VIB-IDF-0002")
        self.assertEqual(edits[(1, "z_panel")], "RR02.PP01")
        self.assertEqual(edits[(4, "cable_ref")], "vib.cpr.0008")
        self.assertEqual([route["segment_id"] for route in result["routes"]], ["0001", "0002"])
        self.assertTrue(port_in_use([("RR02.PP01", "14")], "rr02.pp01", "14"))
        self.assertFalse(port_in_use([("RR02.PP01", "1-2")], "RR02.PP01", "3-4"))

    def test_trace_lists_the_route_without_needing_child_tables(self):
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
            {"cable_id": "vib.cpr.0008", "cable_source_layer_id": "cables", "segment_id": "0001", "segment_fid": 1, "route_order": 1},
            {"cable_id": "vib.cpr.0008", "cable_source_layer_id": "cables", "segment_id": "0002", "segment_fid": 2, "route_order": 2},
        ]
        result = trace_selection([ap], [cable, ap, idf, pole], {"routes": routes, "splices": [], "circuits": []})
        text = format_report(result)
        self.assertIn("Device: VIB-AP-0004", text)
        self.assertIn("Pole: VIB-PL-0002", text)
        self.assertIn("Cable: vib.cpr.0008", text)
        self.assertIn("Ducts: 0001 > 0002", text)
        self.assertIn("IDF: VIB-IDF-0002", text)
        self.assertIn("port 14", text)
        self.assertEqual(result["highlights"]["cables"], [("cables", 1)])
        self.assertEqual(result["highlights"]["idfs"], [("idfs", 2)])
        self.assertFalse(any(item[0] == "db" for item in result["highlights"]["cables"]))


if __name__ == "__main__":
    unittest.main()
