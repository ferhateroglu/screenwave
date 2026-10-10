"""Cursor-following zoom: pure math, no pixels.

Each tick() returns the crop rectangle (left, top, right, bottom margins in
source pixels) that the pipeline's ``videocrop`` element should apply.

Camera behaviour (Screen Studio style):
- Dead zone: the camera holds still while the cursor stays inside the central
  part of the view. Only when the cursor leaves it does the camera pick a new
  target, re-centering on the cursor along that axis — one deliberate glide
  instead of chasing every small movement.
- The camera center and zoom factor move with a critically damped spring
  (no overshoot, eases in and out), time-based so the tick rate doesn't matter.
- The crop size is derived from the zoom alone and snapped to even pixels, so
  a pan never changes the crop size (which would make the picture wobble).
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ZoomSettings:
    zoom: float = 1.5            # target factor while zoom is on
    follow_cursor: bool = True
    smoothing: float = 0.15      # 0..1, higher = snappier follow
    dead_zone: float = 0.6       # central share of the view where the camera holds still
    zoom_time: float = 0.25      # seconds, zoom in/out smoothing

    @property
    def follow_time(self) -> float:
        """Spring smoothing time (s) for panning; 0.15 → 0.4 s."""
        return min(max(0.06 / max(self.smoothing, 0.01), 0.1), 1.5)


def _smooth_damp(cur: float, target: float, vel: float, smooth_time: float, dt: float):
    """Critically damped spring step (Game Programming Gems 4, ch. 1.10)."""
    omega = 2.0 / smooth_time
    x = omega * dt
    k = 1.0 / (1.0 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = cur - target
    temp = (vel + omega * change) * dt
    vel = (vel - omega * temp) * k
    return target + (change + temp) * k, vel


def _even(v: float) -> int:
    return int(round(v / 2.0)) * 2


class ZoomFollow:
    def __init__(self, width: int, height: int, settings: ZoomSettings) -> None:
        self.w, self.h = width, height
        self.settings = settings
        self.zoom_on = False
        self._z, self._vz = 1.0, 0.0
        self._cx, self._vx = width / 2, 0.0
        self._cy, self._vy = height / 2, 0.0
        self._tx, self._ty = width / 2, height / 2
        self._cursor: tuple[float, float] | None = None
        self._last = time.monotonic()

    def toggle(self) -> bool:
        self.zoom_on = not self.zoom_on
        if self.zoom_on and self._cursor is not None and self.settings.follow_cursor:
            # zoom in around the pointer instead of panning to it afterwards
            self._tx, self._ty = self._clamp_center(*self._cursor, self._target_zoom())
            self._cx, self._cy = self._tx, self._ty
            self._vx = self._vy = 0.0
        return self.zoom_on

    def _target_zoom(self) -> float:
        return max(self.settings.zoom, 1.0) if self.zoom_on else 1.0

    def _clamp_center(self, cx: float, cy: float, z: float) -> tuple[float, float]:
        hw, hh = self.w / z / 2, self.h / z / 2
        return min(max(cx, hw), self.w - hw), min(max(cy, hh), self.h - hh)

    def _retarget(self, cursor: tuple[float, float], z: float) -> None:
        """Move the target only when the cursor leaves the dead zone."""
        half = self.settings.dead_zone / 2
        x, y = cursor
        if abs(x - self._tx) > self.w / z * half:
            self._tx = x
        if abs(y - self._ty) > self.h / z * half:
            self._ty = y
        self._tx, self._ty = self._clamp_center(self._tx, self._ty, z)

    def tick(self, cursor: tuple[float, float] | None) -> tuple[int, int, int, int]:
        now = time.monotonic()
        dt = min(max(now - self._last, 1e-3), 0.1)
        self._last = now
        s = self.settings

        zt = self._target_zoom()
        self._z, self._vz = _smooth_damp(self._z, zt, self._vz, s.zoom_time, dt)
        if abs(self.w / self._z - self.w / zt) < 3:  # within one even-pixel step
            self._z, self._vz = zt, 0.0
        self._z = max(self._z, 1.0)

        if cursor is not None:
            self._cursor = cursor
        if not s.follow_cursor:
            self._tx, self._ty = self.w / 2, self.h / 2
        elif self._cursor is not None:
            self._retarget(self._cursor, zt)

        self._cx, self._vx = _smooth_damp(self._cx, self._tx, self._vx, s.follow_time, dt)
        self._cy, self._vy = _smooth_damp(self._cy, self._ty, self._vy, s.follow_time, dt)

        # crop size depends on zoom only -> constant while panning
        cw = min(max(_even(self.w / self._z), 2), self.w & ~1)
        ch = min(max(_even(cw * self.h / self.w), 2), self.h & ~1)
        cx, cy = self._clamp_center(self._cx, self._cy, self.w / cw)
        left = min(max(_even(cx - cw / 2), 0), self.w - cw)
        top = min(max(_even(cy - ch / 2), 0), self.h - ch)
        return left, top, max(self.w - left - cw, 0), max(self.h - top - ch, 0)
