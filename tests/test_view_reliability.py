"""Offline regression coverage for view/client/DPI coordinate conversion.

These tests do not attach to SOLIDWORKS. Live acceptance is a separate gate.
"""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from solidworks_mcp.automation.view import ViewOperations


class ViewReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.typed = patch("solidworks_mcp.automation.view.typed", return_value=None)
        self.typed.start()
        self.addCleanup(self.typed.stop)
        sleeper = patch("solidworks_mcp.automation.view.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        pump = patch("solidworks_mcp.automation.view.pythoncom.PumpWaitingMessages")
        pump.start()
        self.addCleanup(pump.stop)

    @staticmethod
    def fixture(scale=1.0, screen_origin=(-1920, 173), center=(684, 281.5)):
        """The real failure used stale frame=565x405, client=1127x563."""
        view = SimpleNamespace(
            FrameWidth=565, FrameHeight=405, Scale2=10000.0 * scale,
            GetViewHWndx64=lambda: None, GetViewHWnd=lambda: 1234,
            GetVisibleBox=lambda: (
                screen_origin[0] + 241 * scale, screen_origin[1],
                screen_origin[0] + 1127 * scale,
                screen_origin[1] + 563 * scale),
            Transform=SimpleNamespace(ArrayData=[
                1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0,
                center[0] * scale, center[1] * scale, 0.0,
                10000.0 * scale, 0.0, 0.0, 0.0]))
        automation = ViewOperations()
        automation._view_window_coordinates = lambda _: {
            "client_rect_px": [0, 0, 1127 * scale, 563 * scale],
            "client_origin_screen_px": list(screen_origin),
            "window_dpi": round(96 * scale),
        }
        return automation, SimpleNamespace(ActiveView=view)

    def test_measures_nonzero_visible_origin_and_ignores_stale_frame(self):
        automation, doc = self.fixture()
        result = automation._fit_measurement(
            doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)])
        self.assertTrue(result["verified"])
        self.assertEqual(result["viewport_rect_px"], [241, 0, 1127, 563])
        self.assertEqual(result["frame_px"], [886, 563])
        self.assertEqual(result["center_offset_ratio"], 0)
        self.assertEqual(result["stable_read_count"], 2)

    def test_scaled_dpi_coordinates_preserve_ratios(self):
        results = []
        for scale in (1.0, 1.25, 1.5, 2.0):
            automation, doc = self.fixture(scale=scale)
            results.append(automation._fit_measurement(
                doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)]))
        for result in results:
            self.assertTrue(result["verified"])
            self.assertEqual(result["dominant_fill_ratio"],
                             results[0]["dominant_fill_ratio"])
            self.assertEqual(result["center_offset_ratio"], 0)

    def test_geometry_hidden_under_feature_manager_is_clipped(self):
        automation, doc = self.fixture(center=(400, 281.5))
        result = automation._fit_measurement(
            doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)])
        self.assertFalse(result["verified"])
        self.assertTrue(result["clipped"])
        self.assertEqual(result["screen_bbox_px"][0], 200)

    def test_both_top_and_right_clipping_are_detected(self):
        for center in ((684, 50), (1050, 281.5)):
            automation, doc = self.fixture(center=center)
            result = automation._fit_measurement(
                doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)])
            self.assertFalse(result["verified"])
            self.assertTrue(result["clipped"])

    def test_get_visible_box_is_required_no_frame_fallback(self):
        automation, doc = self.fixture()
        doc.ActiveView.GetVisibleBox = lambda: None
        with self.assertRaisesRegex(RuntimeError, "visible rectangle unavailable"):
            automation._fit_measurement(doc, [(-0.02, 0, 0), (0.02, 0, 0)])

    def test_inconsistent_screen_and_client_coordinates_fail_closed(self):
        automation, doc = self.fixture()
        doc.ActiveView.GetVisibleBox = lambda: (-1920, 173, 5000, 736)
        with self.assertRaisesRegex(RuntimeError, "disagrees with client"):
            automation._fit_measurement(doc, [(-0.02, 0, 0), (0.02, 0, 0)])

    def test_resize_waits_for_matching_transform_and_viewport(self):
        automation, doc = self.fixture()
        stable = automation._view_pixel_snapshot(doc)
        old = copy.deepcopy(stable)
        old["viewport_rect_px"] = [241, 0, 565, 405]
        transitional = copy.deepcopy(stable)
        transitional["transform_data"][9] = 282.5
        with patch.object(automation, "_view_pixel_snapshot", side_effect=[
                old, transitional, stable, stable]):
            result = automation._fit_measurement(
                doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)])
        self.assertTrue(result["verified"])
        self.assertEqual(result["stable_read_count"], 4)

    def test_continuously_changing_readback_is_not_verified(self):
        automation, doc = self.fixture()
        values = []
        for x in range(4):
            value = automation._view_pixel_snapshot(doc)
            value["transform_data"][9] += x
            values.append(value)
        with patch.object(automation, "_view_pixel_snapshot", side_effect=values):
            with self.assertRaisesRegex(RuntimeError, "did not stabilize"):
                automation._fit_measurement(doc, [(0, 0, 0), (0.02, 0, 0)])

    def test_typed_view_is_used_for_native_api_readback(self):
        automation, doc = self.fixture()
        typed_view = doc.ActiveView
        doc.ActiveView = object()
        with patch("solidworks_mcp.automation.view.typed", return_value=typed_view):
            result = automation._fit_measurement(
                doc, [(-0.02, -0.01, 0), (0.02, 0.01, 0)])
        self.assertTrue(result["verified"])

    def test_negative_legacy_handle_is_preserved_as_unsigned(self):
        automation, doc = self.fixture()
        doc.ActiveView.GetViewHWnd = lambda: -2
        snapshot = automation._view_pixel_snapshot(doc)
        self.assertEqual(snapshot["hwnd"], 0xfffffffe)

    def test_nonfinite_transform_and_geometry_fail_closed(self):
        automation, doc = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "Non-finite projected"):
            automation._fit_measurement(doc, [(float("nan"), 0, 0)])
        doc.ActiveView.Transform.ArrayData[9] = float("inf")
        with self.assertRaisesRegex(RuntimeError, "Non-finite view pixel"):
            automation._fit_measurement(doc, [(0, 0, 0)])

    def test_native_conversion_restores_context_on_success_and_failure(self):
        class Function:
            def __init__(self, fn):
                self.fn = fn

            def __call__(self, *args):
                return self.fn(*args)

        calls = []
        native = SimpleNamespace(
            GetWindowDpiAwarenessContext=Function(lambda hwnd: 22),
            SetThreadDpiAwarenessContext=Function(
                lambda value: calls.append(value) or 11),
            GetDpiForWindow=Function(lambda hwnd: 144))
        for fail in (False, True):
            calls.clear()
            with patch("ctypes.WinDLL", return_value=native), \
                    patch("win32gui.GetClientRect", return_value=(0, 0, 1200, 800),
                          side_effect=RuntimeError("native failure") if fail else None), \
                    patch("win32gui.ClientToScreen", return_value=(-1920, 120)):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "native failure"):
                        ViewOperations._view_window_coordinates(1234)
                else:
                    result = ViewOperations._view_window_coordinates(1234)
                    self.assertEqual(result["window_dpi"], 144)
                    self.assertEqual(result["client_origin_screen_px"], [-1920, 120])
            self.assertEqual(calls, [22, 11])

    def test_sketch_view_uses_document_accessor_if_manager_returns_none(self):
        identity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0,
                    0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        sketch = SimpleNamespace(
            ModelToSketchTransform=SimpleNamespace(
                ArrayData=identity, Inverse=SimpleNamespace(ArrayData=identity)),
            GetSketchPoints2=lambda: [SimpleNamespace(X=0.02, Y=0.01, Z=0.0)],
            GetSketchSegments=lambda: [])
        doc = SimpleNamespace(SketchManager=SimpleNamespace(ActiveSketch=None),
                              GetActiveSketch2=lambda: sketch)
        automation = ViewOperations()
        actual, basis = automation._active_sketch_basis(doc)
        self.assertIs(actual, sketch)
        self.assertEqual(basis["normal"], (0.0, 0.0, 1.0))
        self.assertEqual(automation._active_sketch_model_points(doc),
                         [(0.02, 0.01, 0.0)])

    def test_unreadable_edit_state_is_not_reported_as_empty_geometry(self):
        automation = ViewOperations()
        with patch("solidworks_mcp.automation.view.active_sketch_state",
                   return_value=(False, None)):
            with self.assertRaisesRegex(RuntimeError, "state is unreadable"):
                automation._active_sketch_basis(object())
            with self.assertRaisesRegex(RuntimeError, "state is unreadable"):
                automation._active_sketch_model_points(object())


if __name__ == "__main__":
    unittest.main()
