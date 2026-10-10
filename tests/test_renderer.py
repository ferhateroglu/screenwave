"""Zoom-follow camera: dead zone, stable crop size, clean zoom-out."""
import random
import unittest
from unittest import mock

from screenwave import renderer
from screenwave.renderer import ZoomFollow, ZoomSettings

W, H = 1920, 1080


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def size(crop):
    left, top, right, bottom = crop
    return W - left - right, H - top - bottom


class ZoomFollowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(renderer.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.z = ZoomFollow(W, H, ZoomSettings(zoom=1.5))

    def run_frames(self, cursor, frames):
        crops = []
        for i in range(frames):
            self.clock.t += 1 / 60
            crops.append(self.z.tick(cursor(i) if callable(cursor) else cursor))
        return crops

    def zoom_in_at(self, point):
        self.run_frames(point, 10)
        self.z.toggle()
        return self.run_frames(point, 90)

    def test_no_zoom_is_full_frame(self):
        self.assertEqual(self.run_frames((300, 200), 30)[-1], (0, 0, 0, 0))

    def test_zoom_in_centers_on_cursor_with_exact_size(self):
        crop = self.zoom_in_at((900, 500))[-1]
        self.assertEqual(size(crop), (1280, 720))
        left, top, _, _ = crop
        self.assertAlmostEqual(left + 640, 900, delta=2)
        self.assertAlmostEqual(top + 360, 500, delta=2)

    def test_small_movements_inside_dead_zone_do_not_move_camera(self):
        self.zoom_in_at((900, 500))
        rnd = random.Random(1)
        crops = self.run_frames(
            lambda i: (900 + rnd.uniform(-40, 40), 500 + rnd.uniform(-40, 40)), 180)
        self.assertEqual(len(set(crops)), 1)

    def test_pan_is_monotonic_and_keeps_crop_size(self):
        self.zoom_in_at((900, 500))
        crops = self.run_frames((1800, 520), 120)
        lefts = [c[0] for c in crops]
        self.assertTrue(all(b >= a for a, b in zip(lefts, lefts[1:])))
        self.assertGreater(lefts[-1], lefts[0])
        self.assertEqual({size(c) for c in crops}, {(1280, 720)})

    def test_crop_values_are_even(self):
        crops = self.zoom_in_at((901, 503)) + self.run_frames((1500, 900), 60)
        self.assertTrue(all(v % 2 == 0 for c in crops for v in c))

    def test_view_stays_inside_the_frame_at_corners(self):
        self.zoom_in_at((900, 500))
        crop = self.run_frames((1919, 1079), 180)[-1]
        self.assertEqual(crop, (640, 360, 0, 0))

    def test_zoom_out_returns_to_full_frame(self):
        self.zoom_in_at((900, 500))
        self.z.toggle()
        self.assertEqual(self.run_frames((900, 500), 90)[-1], (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
