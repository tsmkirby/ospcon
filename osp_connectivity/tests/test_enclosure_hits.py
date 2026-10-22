"""Which cables a new fiber enclosure splits or links."""
import unittest

from osp_connectivity.core.enclosures import enclosure_hits
from osp_connectivity.tests.test_core import feature


def cable(fid, name, coords, media='fiber'):
    return feature('cable', fid, name, {'media': media, 'strand_count': 12}, coords=coords, layer_id='cables')


class EnclosureHitTests(unittest.TestCase):
    def setUp(self):
        self.a = cable(1, 'A', [(0, 0), (100, 0)])
        self.b = cable(2, 'B', [(0, 0.05), (100, 0.05)])  # stacked on A
        self.lateral = cable(3, 'L', [(50, 0), (50, 40)])  # ends at the enclosure spot
        self.copper = cable(4, 'CU', [(0, 0), (100, 0)], media='copper')
        self.features = [self.a, self.b, self.lateral, self.copper]

    def names(self, hits):
        return [hit[0]['values']['id'] for hit in hits]

    def test_without_a_selection_every_fiber_cable_at_the_point_is_used(self):
        found = enclosure_hits(self.features, (50, 0.02), 1.0)
        self.assertFalse(found['limited'])
        self.assertEqual(self.names(found['through']), ['A', 'B'])
        self.assertEqual(self.names(found['laterals']), ['L'])

    def test_a_selection_limits_the_split_to_the_selected_cables(self):
        found = enclosure_hits(self.features, (50, 0.02), 1.0, {('cables', 2)})
        self.assertTrue(found['limited'])
        self.assertEqual(self.names(found['through']), ['B'])
        self.assertEqual(found['laterals'], [])
        found = enclosure_hits(self.features, (50, 0.02), 1.0, {('cables', 1), ('cables', 3)})
        self.assertEqual(self.names(found['through']), ['A'])
        self.assertEqual(self.names(found['laterals']), ['L'])

    def test_a_selected_cable_elsewhere_is_reported_not_ignored(self):
        far = cable(5, 'FAR', [(0, 500), (100, 500)])
        found = enclosure_hits(self.features + [far], (50, 0.02), 1.0, {('cables', 5)})
        self.assertTrue(found['limited'])
        self.assertEqual(found['through'], [])
        self.assertEqual(found['selected_fiber'], 1)

    def test_only_copper_selected_is_reported(self):
        found = enclosure_hits(self.features, (50, 0.02), 1.0, {('cables', 4)})
        self.assertTrue(found['limited'])
        self.assertEqual(found['selected_fiber'], 0)

    def test_selecting_a_non_cable_keeps_the_default(self):
        found = enclosure_hits(self.features, (50, 0.02), 1.0, {('enclosures', 9)})
        self.assertFalse(found['limited'])
        self.assertEqual(len(found['through']), 2)

    def test_the_enclosure_snaps_onto_the_nearest_through_cable(self):
        # A click on the exploded drawing lands beside the stored line.
        found = enclosure_hits(self.features, (50.3, 0.3), 1.0, {('cables', 1)})
        self.assertEqual(found['point'], (50.3, 0.0))
        self.assertEqual(tuple(found['through'][0][1]['at']), (50.3, 0.0))
        found = enclosure_hits(self.features, (50, -0.3), 1.0)
        self.assertEqual(found['point'], (50, 0.0))


if __name__ == '__main__':
    unittest.main()
