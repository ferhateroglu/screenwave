"""Central settings, persisted to ~/.config/screenwave/settings.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR_NAME = "screenwave"
DEFAULT_HOTKEYS = {
    "record": "<Ctrl><Alt>r",
    "pause": "<Ctrl><Alt>p",
    "stop": "<Ctrl><Alt>s",
    "zoom": "<Ctrl><Alt>z",
}


def _default_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME


def _default_output_dir() -> Path:
    return Path(os.environ.get("SCREENWAVE_OUT_DIR", Path.home() / "Videos"))


@dataclass
class Config:
    # --- recording ---
    fps: int = 30
    output_dir: Path = field(default_factory=_default_output_dir)
    container: str = "mp4"          # muxer: mp4 | mkv
    encoder: str = "x264"           # x264 | (later: vaapi)
    video_bitrate_kbps: int = 8000
    record_audio: bool = True       # capture desktop audio (default sink monitor)
    record_mic: bool = True         # mix in the default microphone (narration)
    mic_noise_suppression: bool = True  # webrtcdsp on the mic: removes hiss/hum
    monitor_connector: str = ""     # "" = auto (external > primary > first)

    # --- cursor / zoom ---
    zoom: float = 1.5               # 1.0 = no zoom
    follow_cursor: bool = True
    smoothing: float = 0.15         # 0..1, how fast the view follows
    cursor_backend: str = "auto"    # auto | pipewire | evdev | mock | none

    # --- start ---
    countdown_seconds: int = 3      # 0 = start immediately
    capture_timeout_ms: int = 15000  # wait for Mutter's PipeWire stream

    settings_version: int = 4       # bump when defaults change (see load())

    # --- hotkeys ---
    hotkeys: dict = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))

    # --- paths ---
    config_dir: Path = field(default_factory=_default_config_dir)

    # ------------------------------------------------------------------
    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["config_dir"] = str(self.config_dir)
        (self.config_dir / "settings.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        path = _default_config_dir() / "settings.json"
        if not path.exists():
            return cfg
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cfg
        # merge only known fields (ignore unknown/old keys)
        for key, value in data.items():
            if key == "output_dir":
                cfg.output_dir = Path(value)
            elif key == "config_dir":
                pass  # always recompute
            elif key == "hotkeys":
                merged = dict(DEFAULT_HOTKEYS)
                merged.update(value or {})
                cfg.hotkeys = merged
            elif hasattr(cfg, key) and not key.startswith("_"):
                setattr(cfg, key, value)
        # migrate older settings files
        if data.get("settings_version", 1) < 2:
            cfg.record_mic = True       # v2: microphone on by default
            cfg.settings_version = 2
        if data.get("settings_version", 1) < 3:
            cfg.zoom = min(cfg.zoom, 1.5)  # v3: gentler default zoom
            cfg.settings_version = 3
        if data.get("settings_version", 1) < 4:
            cfg.settings_version = 4    # v4: countdown added (default 3 s)
        return cfg