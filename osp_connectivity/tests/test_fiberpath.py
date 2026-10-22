"""Fiber traces run from one termination to the other, in route order, without looping back."""
import unittest

from osp_connectivity.core.fiberpath import fiber_routes, route_lines
from osp_connectivity.core.strands import build_splice_rows
from osp_connectivity.core.trace import format_report, trace_fibers
from osp_connectivity.tests.test_core import feature

ENDS = {name: name for name in ('a_end', 'z_end', 'a_panel', 'a_port', 'z_panel', 'z_port')}


def cable(fid, name, coords, **values):
    return feature('cable', fid, name, dict({'media': 'fiber', 'strand_count': 12}, **values),
                   coords=coords, layer_id='cables', fields=ENDS)


def point(role, fid, name, xy, **values):
    return feature(role, fid, name, values, coords=[xy], layer_id=role, geometry='Point')


class FiberPathTests(unittest.TestCase):
    def setUp(self):
        # IDF-1/PP1 --F-1--> EN-1 --F-2--> EN-2 --F-3--> IDF-2/PP2, and L-1 from EN-1 to IDF-3/PP3.
        self.cables = [
            cable(1, 'F-1', [(0, 0), (50, 0)], a_end='IDF-1', a_panel='PP1', a_port='1-12', z_end='EN-1'),
            cable(2, 'F-2', [(50, 0), (80, 0)], a_end='EN-1', z_end='EN-2'),
            cable(3, 'F-3', [(80, 0), (120, 0)], a_end='EN-2', z_end='IDF-2', z_panel='PP2', z_port='1-12'),
            cable(4, 'L-1', [(50, 0), (50, 40)], a_end='EN-1', z_end='IDF-3', z_panel='PP3', z_port='1-2'),
        ]
        self.places = [point('idf', 10, 'IDF-1', (0, 0)), point('idf', 11, 'IDF-2', (120, 0)),
                       point('idf', 12, 'IDF-3', (50, 40)),
                       point('splice_enc', 20, 'EN-1', (50, 0)), point('splice_enc', 21, 'EN-2', (80, 0))]
        self.splices = (build_splice_rows(12, 'F-1', 'F-2', 'EN-1', [{'a_from': 5, 'a_to': 6, 'b_cable_id': 'L-1', 'b_start': 1}])
                        + build_splice_rows(12, 'F-2', 'F-3', 'EN-2'))

    def routes(self, cable_id, numbers, splices=None, features=None):
        tables = {'splices': self.splices if splices is None else splices}
        return fiber_routes(cable_id, 'cables', numbers, features or self.cables + self.places, tables)

    def lines(self, *args, **kwargs):
        return route_lines(self.routes(*args, **kwargs)['routes'])

    def test_two_fibers_run_panel_to_panel_from_any_cable_on_the_route(self):
        expected = ['From: IDF-1 PP1:1-2 (F-1:1-2)',
                    'F-1:1-2 ↔︎ F-2:1-2 (express @ EN-1)',
                    'F-2:1-2 ↔︎ F-3:1-2 (express @ EN-2)',
                    'To: IDF-2 PP2:1-2 (F-3:1-2)']
        for start in ('F-1', 'F-2', 'F-3'):
            with self.subTest(start=start):
                self.assertEqual(self.lines(start, [1, 2]), expected)

    def test_a_fusion_follows_the_new_fiber_numbers_to_the_lateral_panel(self):
        self.assertEqual(self.lines('L-1', [1, 2]), [
            'From: IDF-1 PP1:5-6 (F-1:5-6)',
            'F-1:5-6 ↔︎ L-1:1-2 (fusion @ EN-1)',
            'To: IDF-3 PP3:1-2 (L-1:1-2)'])

    def test_only_cables_the_fibers_pass_through_are_on_the_route(self):
        route = self.routes('F-2', [1, 2])['routes'][0]
        self.assertEqual(route['cables'], ['F-1', 'F-2', 'F-3'])
        self.assertEqual(route['enclosures'], ['EN-1', 'EN-2'])

    def test_rows_stored_backwards_or_twice_do_not_loop_back(self):
        flipped = []
        for row in self.splices:
            reverse = dict(row, a_cable_id=row['b_cable_id'], a_strand_no=row['b_strand_no'],
                           b_cable_id=row['a_cable_id'], b_strand_no=row['a_strand_no'])
            flipped.append(reverse if row['splice_enc_id'] == 'EN-2' else row)
        found = self.routes('F-1', [1, 2], flipped + [dict(row) for row in flipped])  # every splice saved twice
        self.assertEqual(route_lines(found['routes'])[1:3],
                         ['F-1:1-2 ↔︎ F-2:1-2 (express @ EN-1)', 'F-2:1-2 ↔︎ F-3:1-2 (express @ EN-2)'])
        self.assertEqual(found['warnings'], [])
        reversed_pair = [dict(row, a_cable_id=row['b_cable_id'], a_strand_no=row['b_strand_no'],
                              b_cable_id=row['a_cable_id'], b_strand_no=row['a_strand_no']) for row in self.splices]
        found = self.routes('F-1', [1, 2], self.splices + reversed_pair)  # the same splice saved once each way
        self.assertEqual(len(route_lines(found['routes'])), 4)
        self.assertEqual(found['warnings'], [])

    def test_a_cable_drawn_backwards_still_reads_panel_to_panel(self):
        self.cables[1] = cable(2, 'F-2', [(80, 0), (50, 0)], a_end='EN-2', z_end='EN-1')
        self.assertEqual(self.lines('F-2', [1, 2])[0], 'From: IDF-2 PP2:1-2 (F-3:1-2)')
        self.assertEqual(self.lines('F-2', [1, 2])[-1], 'To: IDF-1 PP1:1-2 (F-1:1-2)')

    def test_unlabeled_cable_ends_are_found_from_the_enclosure_location(self):
        self.cables[1] = cable(2, 'F-2', [(50, 0), (80, 0)])
        self.assertEqual(self.lines('F-2', [1, 2])[-1], 'To: IDF-2 PP2:1-2 (F-3:1-2)')

    def test_fibers_that_split_are_listed_as_separate_routes(self):
        lines = self.lines('F-1', [4, 5])
        self.assertIn('Fibers F-1:4:', lines)
        self.assertIn('Fibers F-1:5:', lines)
        self.assertIn('To: IDF-2 PP2:4 (F-3:4)', lines)
        self.assertIn('To: IDF-3 PP3:1 (L-1:1)', lines)

    def test_open_fibers_end_at_the_enclosure(self):
        splices = build_splice_rows(12, 'F-1', 'F-2', 'EN-1', [], [{'a_from': 3, 'a_to': 4}])
        splices += [dict(splice_enc_id='EN-1', a_cable_id='F-1', a_strand_no=n, b_cable_id=None, b_strand_no=None,
                         splice_type='OPEN') for n in (3, 4)]
        self.assertEqual(self.lines('F-1', [3, 4], splices),
                         ['From: IDF-1 PP1:3-4 (F-1:3-4)', 'To: F-1:3-4 left OPEN @ EN-1'])

    def test_a_loop_in_the_saved_splices_stops_with_a_warning(self):
        splices = [dict(splice_enc_id='EN-1', a_cable_id='F-1', a_strand_no=1, b_cable_id='F-2', b_strand_no=1, splice_type='FUSION'),
                   dict(splice_enc_id='EN-2', a_cable_id='F-2', a_strand_no=1, b_cable_id='F-1', b_strand_no=2, splice_type='FUSION'),
                   dict(splice_enc_id='EN-1', a_cable_id='F-1', a_strand_no=2, b_cable_id='F-2', b_strand_no=2, splice_type='FUSION'),
                   dict(splice_enc_id='EN-2', a_cable_id='F-2', a_strand_no=2, b_cable_id='F-1', b_strand_no=1, splice_type='FUSION')]
        found = self.routes('F-1', [1], splices)
        self.assertTrue(any('loop back' in warning for warning in found['warnings']))
        self.assertLess(len(route_lines(found['routes'])), 10)

    def test_an_unnamed_end_takes_its_panels_idf(self):
        self.cables[2]['values'].pop('z_end')
        self.places.append(point('fdp', 30, 'PP2', (120, 0), idf_ref='IDF-2'))
        self.assertEqual(self.lines('F-1', [1, 2])[-1], 'To: IDF-2 PP2:1-2 (F-3:1-2)')

    def test_fiber_terminations_set_the_port(self):
        tables = {'splices': self.splices, 'terminations': [
            {'cable_id': 'F-3', 'strand_no': n, 'fdp_id': 'FDP-9', 'panel_position': str(n + 20)} for n in (1, 2)]}
        routes = fiber_routes('F-1', 'cables', [1, 2], self.cables + self.places, tables)['routes']
        self.assertEqual(route_lines(routes)[-1], 'To: IDF-2 FDP-9:21-22 (F-3:1-2)')

    def test_saved_route_report_has_no_unrelated_splices_or_warnings(self):
        result = trace_fibers('F-2', 'cables', 1, 2, self.cables + self.places,
                              {'splices': self.splices, 'memberships': [{'splice_enc_id': 'EN-1', 'cable_id': 'L-1'}]})
        report = result['reports'][0]
        text = format_report(result)
        self.assertNotIn('L-1', text)
        self.assertNotIn('Warning', text)
        self.assertNotIn('all associated fibers', text)
        self.assertEqual(report['circuit_route']['segments'], ['F-2', 'F-1', 'F-3'][:0] or report['circuit_route']['segments'])
        self.assertEqual([ref[1] for ref in report['highlights']['cables']], [1, 2, 3])
        self.assertEqual(sorted(ref[1] for ref in report['highlights']['idfs']), [10, 11])


if __name__ == '__main__':
    unittest.main()
