import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solidworks_mcp.automation import SolidWorksAutomation
from solidworks_mcp.automation.advanced_features import validate_bbox_contract


class FeatureReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.auto = SolidWorksAutomation()

    def profile(self, status=3, required=True, topology=(0, 0, 1)):
        sketch = SimpleNamespace(GetConstrainedStatus=lambda: status,
                                 CheckFeatureUse=lambda *args: topology)
        self.auto._find_feature = lambda *args: SimpleNamespace(
            GetSpecificFeature2=lambda: sketch)
        self.auto._body_names = lambda doc: []
        self.auto._sw_app = SimpleNamespace(GetUserPreferenceToggle=lambda key: required)
        with patch('solidworks_mcp.automation.advanced_features.typed', lambda x, _: x):
            return self.auto._validate_extrusion_profile(object(), 'Profile', False)

    def test_underdefined_is_clear_failure_when_setting_requires_definition(self):
        result = self.profile(status=2)
        self.assertEqual(result['data']['error']['code'], 'SKETCH_UNDERDEFINED')

    def test_underdefined_is_allowed_when_native_policy_allows(self):
        self.assertIsNone(self.profile(status=2, required=False))

    def test_unknown_cannot_pass_as_zero_dof(self):
        self.assertEqual(self.profile(status=1)['data']['error']['code'],
                         'SKETCH_STATUS_UNAVAILABLE')

    def test_rejected_topology_does_not_treat_zero_counters_as_no_geometry(self):
        self.assertEqual(self.profile(topology=(2, 0, 0))['data']['error']['code'],
                         'SKETCH_INVALID_CONTOUR')

    def test_missing_native_check_is_not_success(self):
        self.assertEqual(self.profile(topology=None)['data']['error']['code'],
                         'SKETCH_TOPOLOGY_UNAVAILABLE')

    def test_fully_defined_closed_profile_passes(self):
        self.assertIsNone(self.profile())

    def test_bbox_exact_match_rejects_smaller_inside_feature(self):
        expect = {'min': [0, 0, 0], 'max': [40, 20, 10], 'mode': 'match', 'tolerance': .001}
        small = {'min': [0, 0, 0], 'max': [1, 1, 1]}
        self.assertIsNotNone(validate_bbox_contract(expect, small))
        self.assertIsNone(validate_bbox_contract({**expect, 'mode': 'contains'}, small))
        self.assertIsNone(validate_bbox_contract(expect, {'min': [0, 0, 0], 'max': [40, 20, 10]}))

    def test_bbox_missing_nan_negative_tolerance_rejected(self):
        expect = {'min': [0, 0, 0], 'max': [1, 1, 1]}
        self.assertIsNotNone(validate_bbox_contract(expect, None))
        self.assertIsNotNone(validate_bbox_contract({**expect, 'tolerance': -1}, check_actual=False))
        self.assertIsNotNone(validate_bbox_contract({**expect, 'min': [float('nan'), 0, 0]}, check_actual=False))

    def test_unreadable_closed_flag_never_counts_as_closed(self):
        mgr = SimpleNamespace(ActiveSketch=SimpleNamespace(
            GetSketchContours=lambda: [SimpleNamespace()]))
        self.assertIsNone(self.auto._count_closed_contours(mgr))

    def test_inverted_native_bbox_cannot_pass_containment(self):
        expect = {'min': [0, 0, 0], 'max': [10, 10, 10]}
        actual = {'min': [8, 8, 8], 'max': [2, 2, 2]}
        self.assertIsNotNone(validate_bbox_contract(expect, actual))

    def test_unavailable_contour_enumeration_is_not_empty(self):
        self.assertIsNone(self.auto._count_closed_contours(SimpleNamespace(
            ActiveSketch=SimpleNamespace())))

    def test_four_way_endpoint_branch_is_not_valid_simple_chain(self):
        segs = [{'type': 'line', 'start': [0, 0, 0], 'end': p}
                for p in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0])]
        bad, _, _ = self.auto._segments_endpoint_check(segs)
        self.assertIn([0.0, 0.0, 0.0], bad)


if __name__ == '__main__':
    unittest.main()
