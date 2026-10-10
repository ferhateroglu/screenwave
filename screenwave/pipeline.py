"""One native GStreamer pipeline: monitor capture + audio -> mp4.

  video : pipewiresrc(Mutter node) -> I420 -> videorate -> videocrop -> videoscale
          -> x264enc -> h264parse -> mp4mux
  audio : pulsesrc(s) -> audiomixer -> voaacenc -> aacparse -> mp4mux
          (the microphone branch goes through webrtcdsp noise suppression)

Zoom is done by updating the ``videocrop`` margins from the main loop
(see renderer.ZoomFollow); output size stays constant. The new margins are
applied from a probe on the crop's sink pad, i.e. in the streaming thread
right before a frame, so a frame never sees a half-updated rectangle. Pause keeps
the capture running (pipewiresrc cannot re-negotiate a Mutter stream after a
PAUSED round-trip) and instead drops buffers at the encoder inputs, shifting
later timestamps back by the paused time, so the span is cut out of both A/V. The mp4 is written fragment-wise and finalized into a regular file on EOS, so a crash still
leaves a playable file.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst

log = logging.getLogger("screenwave.pipeline")

STOP_TIMEOUT_S = 8


class PipelineError(RuntimeError):
    pass


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def default_audio_monitor() -> str | None:
    """Desktop audio: default sink's monitor source."""
    sink = _pactl("get-default-sink")
    return f"{sink}.monitor" if sink else None


def has_element(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None


def default_microphone() -> str | None:
    src = _pactl("get-default-source")
    if not src or src.endswith(".monitor"):
        return None
    return src


class RecorderPipeline:
    def __init__(
        self,
        node_id: int | None,
        out_path: Path,
        width: int,
        height: int,
        fps: int = 30,
        bitrate_kbps: int = 8000,
        audio_sources: list[str] | None = None,
        mic_source: str | None = None,
        denoise_mic: bool = True,
        fake: bool = False,
    ):
        self.node_id = node_id
        self.out_path = Path(out_path)
        self.src_w, self.src_h = width, height
        self.w = width & ~1
        self.h = height & ~1
        self.fps = fps
        self.bitrate = bitrate_kbps
        self.audio_sources = audio_sources or []
        self.mic_source = mic_source
        self.denoise_mic = denoise_mic
        self.fake = fake
        self.pipeline: Gst.Pipeline | None = None
        self._crop = None
        self._last_crop: tuple[int, int, int, int] | None = None     # main loop
        self._applied_crop: tuple[int, int, int, int] | None = None  # streaming thread
        self._stop_timer = 0
        self._done = False
        self._paused = False
        self._paused_at = 0.0
        self._offset_ns = 0
        self.on_eos: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    def _description(self) -> str:
        if self.fake:
            src = "videotestsrc is-live=true ! video/x-raw,width=1280,height=720 ! "
        else:
            src = (
                f"pipewiresrc path={self.node_id} do-timestamp=true "
                f"keepalive-time={max(1000 // self.fps, 1)} ! "
            )
        video = (
            f"{src}videoconvert ! "
            f"video/x-raw,format=I420,width={self.src_w},height={self.src_h} ! "
            f"videorate ! video/x-raw,framerate={self.fps}/1 ! "
            "queue max-size-buffers=4 leaky=downstream ! "
            "videocrop name=crop ! videoscale method=bilinear ! "
            f"video/x-raw,format=I420,width={self.w},height={self.h},"
            f"pixel-aspect-ratio=1/1,framerate={self.fps}/1 ! "
            f"x264enc name=venc speed-preset=veryfast bitrate={self.bitrate} "
            f"key-int-max={self.fps * 2} ! h264parse ! queue ! mux. "
        )
        audio = ""
        # pulsesrc must not provide the clock: pipewiresrc only behaves with its
        # own clock (with a pulse clock frames stall and EOS never arrives)
        pulse = "pulsesrc provide-clock=false device="
        srcs = ["audiotestsrc is-live=true wave=silence"] if self.fake else [
            f"{pulse}{dev}" for dev in self.audio_sources
        ]
        branches = [
            f"{s} ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=48000,channels=2 ! queue ! amix. "
            for s in srcs
        ]
        if self.mic_source and not self.fake:
            denoise = ""
            if self.denoise_mic and has_element("webrtcdsp"):
                # mono 48k in, hiss/hum out; no echo probe, so AEC stays off
                denoise = (
                    "audio/x-raw,format=S16LE,rate=48000,channels=1 ! "
                    "webrtcdsp echo-cancel=false gain-control=false "
                    "noise-suppression-level=high high-pass-filter=true ! "
                    "audioconvert ! "
                )
            branches.append(
                f"{pulse}{self.mic_source} ! audioconvert ! audioresample ! "
                f"{denoise}audio/x-raw,rate=48000,channels=2 ! queue ! amix. "
            )
        if branches:
            audio = "audiomixer name=amix ! audio/x-raw,rate=48000,channels=2 ! audioconvert ! voaacenc name=aenc bitrate=160000 ! aacparse ! queue ! mux. "
            audio += " ".join(branches)
        mux = (
            "mp4mux name=mux fragment-duration=2000 fragment-mode=first-moov-then-finalise ! "
            f'filesink location="{self.out_path}" '
        )
        return mux + video + audio

    def build(self) -> None:
        desc = self._description()
        log.info("pipeline: %s", desc)
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            raise PipelineError(f"bad pipeline: {exc.message}") from exc
        self._crop = self.pipeline.get_by_name("crop")
        self._crop.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._apply_crop
        )
        for name in ("venc", "aenc"):
            enc = self.pipeline.get_by_name(name)
            if enc is not None:
                enc.get_static_pad("sink").add_probe(
                    Gst.PadProbeType.BUFFER, self._time_shift
                )
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos_msg)
        bus.connect("message::error", self._on_error_msg)

    def start(self) -> None:
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise PipelineError("pipeline failed to start")

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused:
            self._offset_ns += int((time.monotonic() - self._paused_at) * Gst.SECOND)
            self._paused = False

    def _time_shift(self, _pad, info):
        if self._paused:
            return Gst.PadProbeReturn.DROP
        if self._offset_ns:
            buf = info.get_buffer()  # sole owner here (no tee), safe to edit in place
            if buf.pts != Gst.CLOCK_TIME_NONE:
                buf.pts = max(buf.pts - self._offset_ns, 0)
            if buf.dts != Gst.CLOCK_TIME_NONE:
                buf.dts = max(buf.dts - self._offset_ns, 0)
        return Gst.PadProbeReturn.OK

    def set_crop(self, left: int, top: int, right: int, bottom: int) -> None:
        self._last_crop = (left, top, right, bottom)  # picked up by _apply_crop

    def _apply_crop(self, _pad, _info):
        key = self._last_crop
        # skip when unchanged: a size change renegotiates caps downstream
        if key is not None and key != self._applied_crop:
            self._applied_crop = key
            for name, value in zip(("left", "top", "right", "bottom"), key):
                self._crop.set_property(name, value)
        return Gst.PadProbeReturn.OK

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """EOS through the whole pipeline; on_eos fires once the file is closed."""
        if self.pipeline is None or self._done:
            return
        self.pipeline.send_event(Gst.Event.new_eos())
        self._stop_timer = GLib.timeout_add_seconds(STOP_TIMEOUT_S, self._force_stop)

    def _force_stop(self) -> bool:
        log.warning("EOS timeout, forcing shutdown")
        self._stop_timer = 0
        self._finish()
        return False

    def _on_eos_msg(self, _bus, _msg) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        if self._stop_timer:
            GLib.source_remove(self._stop_timer)
            self._stop_timer = 0
        if self.pipeline is not None:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
        if self.on_eos:
            self.on_eos()

    def _on_error_msg(self, _bus, msg) -> None:
        err, debug = msg.parse_error()
        log.error("pipeline error: %s (%s)", err.message, debug)
        if self.on_error:
            self.on_error(err.message)
