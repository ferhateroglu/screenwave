"""Cursor position sources.

Reading the global cursor on Wayland is restricted, so we use pluggable
backends, all with the same interface ``read() -> (x, y) | None``:

  * ``pipewire`` — exact position from Mutter's cursor metadata, read by the
    small ``bin/swcursor`` helper from a second, metadata-only ScreenCast
    stream. The default whenever the helper is built.
  * ``evdev`` — fallback: reads /dev/input/event* directly (needs the
    ``input`` group). Mice report relative (dx,dy) moves; we accumulate them
    and realign to the screen center when recording starts, so it drifts.
  * ``mock``  — development: cursor orbits the screen center.
  * ``none``  — no source (zoom stays centered).

Note: not thread-safe; read only from the GStreamer streaming thread.
"""
from __future__ import annotations

import fcntl
import math
import os
import select
import struct
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

# ---- Linux evdev constants / ioctls ----
_IOC_READ = 2
_TYPE_CHAR = 0x45  # 'E'


def _ioc(dir_: int, type_: int, nr: int, size: int) -> int:
    return (dir_ << 30) | (size << 16) | (type_ << 8) | nr


def EVIOCGBIT(ev: int, length: int) -> int:
    return _ioc(_IOC_READ, _TYPE_CHAR, 0x20 + ev, length)


def EVIOCGABS(abs_: int) -> int:
    return _ioc(_IOC_READ, _TYPE_CHAR, 0x40 + abs_, 24)


EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
REL_X, REL_Y = 0, 1
ABS_X, ABS_Y = 0, 1
BTN_LEFT = 0x110

_ABSINFO = struct.Struct("<iiiiii")   # value min max fuzz flat resolution
_EVENT = struct.Struct("<qqHHi")      # timeval(s×2) + type + code + value


class CursorBackend(ABC):
    """Single interface: returns (x, y) in screen pixels per frame."""

    name: str = "abstract"

    @abstractmethod
    def read(self) -> tuple[float, float] | None: ...

    def reset(self, width: int, height: int) -> None:
        self._w, self._h = width, height

    def describe(self) -> str:
        return self.name

    def close(self) -> None:
        pass


class NoneCursor(CursorBackend):
    name = "none"

    def read(self) -> tuple[float, float] | None:
        return None


class MockCursor(CursorBackend):
    """Demo: cursor orbits the screen center."""

    name = "mock"

    def __init__(self, radius_ratio: float = 0.30) -> None:
        self._radius_ratio = radius_ratio
        self._w, self._h = 1920, 1080
        self._t0 = time.monotonic()

    def reset(self, width: int, height: int) -> None:
        self._w, self._h = width, height

    def read(self) -> tuple[float, float]:
        t = time.monotonic() - self._t0
        r = min(self._w, self._h) * self._radius_ratio
        return (
            self._w / 2 + r * math.cos(t * 0.8),
            self._h / 2 + r * math.sin(t * 1.1),
        )


# next to the package: <checkout>/bin or ~/.local/share/screenwave/bin
HELPER = Path(__file__).absolute().parent.parent / "bin" / "swcursor"


class PipewireCursor(CursorBackend):
    """Exact pointer position from Mutter's cursor metadata (via bin/swcursor).

    ``node_id`` must be a ScreenCast stream created with cursor-mode=Metadata.
    Positions are in monitor pixels, so no accumulation drift.
    """

    name = "pipewire"

    def __init__(self, node_id: int) -> None:
        self._pos: tuple[float, float] | None = None
        self._proc = subprocess.Popen(
            [str(HELPER), str(node_id)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(target=self._pump, name="swcursor", daemon=True).start()

    @staticmethod
    def available() -> bool:
        return HELPER.exists() and os.access(HELPER, os.X_OK)

    def _pump(self) -> None:
        for line in self._proc.stdout:
            try:
                x, y = line.split()
                self._pos = (float(x), float(y))
            except ValueError:
                continue

    def read(self) -> tuple[float, float] | None:
        return self._pos

    def describe(self) -> str:
        return "pipewire (real pointer)"

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class EvdevCursor(CursorBackend):
    """Pure-Python evdev reader — cursor tracking via /dev/input/event*.

    Mice give relative motion: the position starts at the screen center when
    recording starts and accumulates (small drift is acceptable for tracking).
    Absolute devices (touchpad/tablet) are applied directly.
    """

    name = "evdev"

    def __init__(self) -> None:
        self._fds: list[int] = []
        self._abs: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {}
        self._w, self._h = 1920, 1080
        self._x, self._y = self._w / 2, self._h / 2
        self._open_devices()

    def _open_devices(self) -> None:
        if not Path("/dev/input").is_dir():
            return
        for dev in sorted(Path("/dev/input").glob("event*")):
            fd = self._try_open(dev)
            if fd is None:
                continue
            try:
                rel = self._bitmask(fd, EV_REL, 16)
                keys = self._bitmask(fd, EV_KEY, (BTN_LEFT // 8) + 1)
                abs_x = self._absinfo(fd, ABS_X)
                abs_y = self._absinfo(fd, ABS_Y) if abs_x else None

                is_pointer = (keys >> BTN_LEFT) & 1 or bool(abs_x)
                if not is_pointer:
                    os.close(fd)
                    continue
                if (rel >> REL_X) & 1 and (rel >> REL_Y) & 1:
                    self._fds.append(fd)  # relative mouse
                elif abs_x and abs_y:
                    self._fds.append(fd)
                    self._abs[fd] = (abs_x, abs_y)  # mutlak cihaz
                else:
                    os.close(fd)
            except OSError:
                os.close(fd)

    def _try_open(self, dev: Path) -> int | None:
        try:
            return os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None

    def _bitmask(self, fd: int, ev: int, nbytes: int) -> int:
        buf = bytearray(nbytes)
        fcntl.ioctl(fd, EVIOCGBIT(ev, nbytes), buf)
        return int.from_bytes(bytes(buf), "little")

    def _absinfo(self, fd: int, axis: int) -> tuple[int, int] | None:
        buf = bytearray(_ABSINFO.size)
        try:
            fcntl.ioctl(fd, EVIOCGABS(axis), buf)
        except OSError:
            return None
        _v, _min, _max, _f, _fl, _res = _ABSINFO.unpack(bytes(buf))
        if _max <= _min:
            return None
        return _min, _max

    def reset(self, width: int, height: int) -> None:
        self._w, self._h = width, height
        self._x, self._y = width / 2, height / 2

    def _scale(self, value: int, lo: int, hi: int, size: int) -> float:
        if hi <= lo:
            return size / 2
        return (value - lo) / (hi - lo) * (size - 1)

    def read(self) -> tuple[float, float] | None:
        if not self._fds:
            return None
        ready, _, _ = select.select(self._fds, [], [], 0)
        for fd in ready:
            while True:
                try:
                    data = os.read(fd, _EVENT.size)
                except (BlockingIOError, OSError):
                    break
                if len(data) < _EVENT.size:
                    break
                _, _, etype, code, value = _EVENT.unpack(data)
                if etype == EV_REL:
                    if code == REL_X:
                        self._x += value
                    elif code == REL_Y:
                        self._y += value
                elif etype == EV_ABS and fd in self._abs:
                    (ax_lo, ax_hi), (ay_lo, ay_hi) = self._abs[fd]
                    if code == ABS_X:
                        self._x = self._scale(value, ax_lo, ax_hi, self._w)
                    elif code == ABS_Y:
                        self._y = self._scale(value, ay_lo, ay_hi, self._h)
        self._x = min(max(self._x, 0), self._w)
        self._y = min(max(self._y, 0), self._h)
        return self._x, self._y

    def describe(self) -> str:
        if not self._fds:
            return "evdev (no pointer device readable — input group?)"
        return f"evdev ({len(self._fds)} cihaz)"

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()


def create_backend(kind: str = "auto", node_id: int | None = None) -> CursorBackend:
    """'auto' | 'pipewire' | 'evdev' | 'mock' | 'none' -> backend.

    ``node_id`` is a cursor-metadata ScreenCast node (needed for pipewire).
    """
    if kind in ("auto", "pipewire") and node_id is not None and PipewireCursor.available():
        return PipewireCursor(node_id)
    if kind == "none":
        return NoneCursor()
    if kind == "mock":
        return MockCursor()
    if kind == "evdev":
        return EvdevCursor()
    # auto without the helper: evdev if readable, else keep the zoom centered
    backend = EvdevCursor()
    if backend._fds:
        return backend
    backend.close()
    return NoneCursor()