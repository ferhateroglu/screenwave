"""Screenwave UI — GTK4 + libadwaita (Adw), English, self-assignable hotkeys."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GObject, Gtk

from .. import hotkeys as hk_mod
from ..config import Config
from ..recorder import (
    ACTIVE_STATES,
    STATE_COUNTDOWN,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RECORDING,
    RecorderController,
)

_CSS = b"""
.record-btn  { background: #e5484d; color: white; font-weight: bold; }
.record-btn:disabled { background: #7a2b2e; }
.zoom-on     { background: #2ec27e; color: #1b1b1b; font-weight: bold; }
.pause-btn   { background: #f5a524; color: #1b1b1b; font-weight: bold; }
.stop-btn    { background: #6e56cf; color: white; font-weight: bold; }
.status-big  { font-size: 18px; font-weight: bold; }
.status-sub  { font-size: 13px; opacity: 0.75; }
"""

try:
    _CSS_PRIORITY = Gtk.StyleContextProviderPriority.APPLICATION
except Exception:  # GTK >= 4.18 removed the enum
    _CSS_PRIORITY = 600

_ACTION_TITLES = {
    "record": "Start / stop recording",
    "pause": "Pause / resume",
    "stop": "Stop recording",
    "zoom": "Toggle cursor zoom",
}


def _apply_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(display, provider, _CSS_PRIORITY)


# --------------------------------------------------------------------------
class KeyCaptureButton(Gtk.Button):
    """Button that grabs the next key combination pressed."""

    def __init__(self, on_captured, placeholder: str = "Change…"):
        super().__init__(label=placeholder)
        self._on_captured = on_captured
        self.connect("clicked", self._begin)

    def _begin(self, _btn) -> None:
        root = self.get_root()
        win = Gtk.Window(title="Press shortcut")
        win.set_modal(True)
        win.set_transient_for(root)
        win.set_resizable(False)
        win.set_default_size(360, 110)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        label = Gtk.Label(label="Press the key combination you want to use…\n(Esc to cancel)")
        label.set_wrap(True)
        box.append(label)
        win.set_child(box)

        controller = Gtk.EventControllerKey()

        def on_key(_c, keyval: int, _keycode: int, state: int) -> bool:
            mods = state & Gtk.accelerator_get_default_mod_mask()
            if keyval == Gdk.KEY_Escape:
                win.destroy()
                return True
            if keyval in _MODIFIER_KEYS:
                return False  # wait for the real key
            accel = Gtk.accelerator_name(keyval, mods)
            win.destroy()
            self._on_captured(accel)
            return True

        controller.connect("key-pressed", on_key)
        win.add_controller(controller)
        win.present()


_MODIFIER_KEYS = {
    Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock, Gdk.KEY_Scroll_Lock,
}


# --------------------------------------------------------------------------
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, controller: RecorderController, cfg: Config):
        super().__init__(application=app, title="Screenwave")
        self.controller = controller
        self.cfg = cfg
        self.set_default_size(460, 360)
        _apply_css()

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Screenwave", subtitle="Wayland screen recorder"))

        prefs_btn = Gtk.Button(icon_name="open-menu-symbolic")
        prefs_btn.set_tooltip_text("Settings & hotkeys")
        prefs_btn.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        prefs_btn.connect("clicked", self._open_preferences)
        header.pack_end(prefs_btn)
        toolbar.add_top_bar(header)

        toolbar.set_content(self._build_main())

        # the app outlives the window: detach on close so a later window
        # doesn't share the controller with a destroyed one
        self.controller.on_error = self._flash_error
        self.controller.listeners.append(self._on_change)
        self.connect("close-request", self._detach)
        self._shown_state = None
        self._on_change()

    def _detach(self, _win) -> bool:
        if self._on_change in self.controller.listeners:
            self.controller.listeners.remove(self._on_change)
        if self.controller.on_error == self._flash_error:
            self.controller.on_error = None
        return False  # let it close

    # ------------------------------------------------------------------
    def _build_main(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(28)
        box.set_margin_bottom(28)
        box.set_margin_start(28)
        box.set_margin_end(28)
        box.set_valign(Gtk.Align.CENTER)

        self.status_big = Gtk.Label(label="Ready")
        self.status_big.add_css_class("status-big")
        box.append(self.status_big)

        self.status_sub = Gtk.Label(label=self._hint_text())
        self.status_sub.add_css_class("status-sub")
        self.status_sub.set_wrap(True)
        self.status_sub.set_justify(Gtk.Justification.CENTER)
        box.append(self.status_sub)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_halign(Gtk.Align.CENTER)

        self.record_btn = Gtk.Button(label="● Record")
        self.record_btn.add_css_class("record-btn")
        self.record_btn.connect("clicked", self._on_record)

        self.pause_btn = Gtk.Button(label="⏸ Pause")
        self.pause_btn.add_css_class("pause-btn")
        self.pause_btn.set_sensitive(False)
        self.pause_btn.connect("clicked", self._on_pause)

        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.add_css_class("stop-btn")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", self._on_stop)

        self.zoom_btn = Gtk.Button(label="🔍 Zoom")
        self.zoom_btn.set_sensitive(False)
        self.zoom_btn.connect("clicked", self._on_zoom)

        for b in (self.record_btn, self.pause_btn, self.stop_btn, self.zoom_btn):
            b.set_size_request(110, 44)
            b.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            row.append(b)
        box.append(row)

        return box

    def _hint_text(self) -> str:
        def fmt(action: str) -> str:
            accel = self.cfg.hotkeys.get(action, "")
            if accel:
                try:
                    keyval, mods = Gtk.accelerator_parse(accel)
                    label = Gtk.accelerator_get_label(keyval, mods)
                except Exception:
                    label = accel
            else:
                label = "—"
            return f"{_ACTION_TITLES.get(action, action)}: {label}"

        return "\n".join(fmt(a) for a in ("record", "pause", "stop", "zoom"))

    # ------------------------------------------------------------------
    # Preferences (settings + hotkey assignment)
    # ------------------------------------------------------------------
    def _open_preferences(self, _btn) -> None:
        win = Adw.PreferencesWindow(transient_for=self, modal=True)
        win.set_default_size(520, 620)
        page = Adw.PreferencesPage()
        win.add(page)

        # --- recording / monitor / audio ---
        rec_group = Adw.PreferencesGroup(title="Recording")

        try:
            from ..mutter_capture import list_monitors

            monitors = list_monitors()
        except Exception:
            monitors = []
        self._monitor_connectors = [m["connector"] for m in monitors]
        mon_row = Adw.ComboRow(title="Monitor", subtitle="Which display to record")
        model = Gtk.StringList.new(
            [
                f"{m['connector']} — {m['model'] or m['vendor'] or '?'}"
                f" ({m['width']}×{m['height']})"
                + (" [primary]" if m["primary"] else "")
                for m in monitors
            ]
            or ["(no monitors detected)"]
        )
        mon_row.set_model(model)
        # preselect the configured (or auto-picked) monitor
        selected = 0
        if self.cfg.monitor_connector:
            try:
                selected = self._monitor_connectors.index(self.cfg.monitor_connector)
            except ValueError:
                selected = 0
        elif monitors:
            from ..mutter_capture import pick_monitor

            chosen = pick_monitor(monitors)
            if chosen:
                try:
                    selected = self._monitor_connectors.index(chosen["connector"])
                except ValueError:
                    selected = 0
        mon_row.set_selected(selected)

        def on_mon_selected(row, _pspec):
            idx = row.get_selected()
            if 0 <= idx < len(self._monitor_connectors):
                self.cfg.monitor_connector = self._monitor_connectors[idx]
                self.cfg.save()

        mon_row.connect("notify::selected", on_mon_selected)
        rec_group.add(mon_row)

        audio_row = Adw.SwitchRow(
            title="Record system audio",
            subtitle="Captures the default output (desktop sound)",
        )
        audio_row.set_active(self.cfg.record_audio)
        audio_row.connect(
            "notify::active", lambda r, _p: self._set_audio(r.get_active())
        )
        rec_group.add(audio_row)

        mic_row = Adw.SwitchRow(
            title="Record microphone",
            subtitle="Mixes the default microphone into the recording",
        )
        mic_row.set_active(self.cfg.record_mic)
        mic_row.connect("notify::active", lambda r, _p: self._set_mic(r.get_active()))
        rec_group.add(mic_row)

        denoise_row = Adw.SwitchRow(
            title="Mic noise suppression",
            subtitle="Removes background hiss and hum from the microphone",
        )
        denoise_row.set_active(self.cfg.mic_noise_suppression)
        denoise_row.connect(
            "notify::active", lambda r, _p: self._set_denoise(r.get_active())
        )
        rec_group.add(denoise_row)

        fps_row = Adw.SpinRow(
            adjustment=Gtk.Adjustment(value=self.cfg.fps, lower=15, upper=60, step_increment=5),
            title="Frame rate (FPS)",
            subtitle="Capture & encode framerate",
        )
        fps_row.connect("notify::value", lambda r, _p: setattr(self.cfg, "fps", int(r.get_value())))
        rec_group.add(fps_row)

        countdown_row = Adw.SpinRow(
            adjustment=Gtk.Adjustment(
                value=self.cfg.countdown_seconds, lower=0, upper=10, step_increment=1
            ),
            title="Countdown (seconds)",
            subtitle="3-2-1 before recording starts; 0 starts right away",
        )
        countdown_row.connect(
            "notify::value",
            lambda r, _p: self._set_countdown(int(r.get_value())),
        )
        rec_group.add(countdown_row)

        bitrate_row = Adw.SpinRow(
            adjustment=Gtk.Adjustment(
                value=self.cfg.video_bitrate_kbps, lower=1000, upper=40000, step_increment=1000
            ),
            title="Video bitrate (kbps)",
            subtitle="Higher = better quality, bigger files",
        )
        bitrate_row.connect(
            "notify::value",
            lambda r, _p: setattr(self.cfg, "video_bitrate_kbps", int(r.get_value())),
        )
        rec_group.add(bitrate_row)

        out_row = Adw.ActionRow(title="Output folder", subtitle=str(self.cfg.output_dir))
        out_btn = Gtk.Button(label="Choose…")
        out_btn.connect("clicked", self._on_choose_output)
        out_row.add_suffix(out_btn)
        out_row.set_activatable_widget(out_btn)
        rec_group.add(out_row)
        page.add(rec_group)

        # --- hotkeys ---
        hot_group = Adw.PreferencesGroup(
            title="Hotkeys",
            description="Wayland can't give apps global keys, so Screenwave registers "
            "these as GNOME shortcuts. Pick a key combination for each action; "
            "it applies immediately and persists.",
        )
        self._hotkey_rows: dict[str, Adw.ActionRow] = {}
        for action in ("record", "pause", "stop", "zoom"):
            row = Adw.ActionRow(title=_ACTION_TITLES[action])
            row.set_subtitle(self._format_accel(action))
            capture = KeyCaptureButton(lambda accel, a=action: self._on_hotkey(a, accel))
            row.add_suffix(capture)
            row.set_activatable_widget(capture)
            hot_group.add(row)
            self._hotkey_rows[action] = row
        page.add(hot_group)

        # --- cursor / zoom ---
        zoom_group = Adw.PreferencesGroup(
            title="Cursor & Zoom",
            description="The zoom follows the pointer (Screen Studio style): the "
            "view holds still while the pointer is near the middle and glides "
            "when it nears an edge.",
        )
        zoom_row = Adw.SpinRow(
            adjustment=Gtk.Adjustment(value=self.cfg.zoom, lower=1.0, upper=4.0, step_increment=0.25),
            title="Zoom",
            subtitle="1.0 = no zoom; 1.5 = 1.5× follow-zoom",
            digits=2,
        )
        zoom_row.connect(
            "notify::value", lambda r, _p: self.controller.set_zoom(r.get_value())
        )
        zoom_group.add(zoom_row)

        follow_row = Adw.SwitchRow(title="Follow cursor", subtitle="Keep the zoom centered on the pointer")
        follow_row.set_active(self.cfg.follow_cursor)
        follow_row.connect("notify::active", lambda r, _p: self.controller.set_follow(r.get_active()))
        zoom_group.add(follow_row)
        page.add(zoom_group)

        win.present()

    def _format_accel(self, action: str) -> str:
        accel = self.cfg.hotkeys.get(action, "")
        if not accel:
            return "Not set"
        try:
            keyval, mods = Gtk.accelerator_parse(accel)
            return Gtk.accelerator_get_label(keyval, mods)
        except Exception:
            return accel

    def _on_hotkey(self, action: str, accelerator: str) -> None:
        self.cfg.hotkeys[action] = accelerator
        self.cfg.save()
        try:
            hk_mod.apply_hotkeys(self.cfg.hotkeys)
        except Exception:
            pass  # gsettings not available → still saved in config
        self._hotkey_rows[action].set_subtitle(self._format_accel(action))
        self.status_sub.set_text(self._hint_text())

    def _set_output_dir(self, path: str) -> None:
        from pathlib import Path

        self.cfg.output_dir = Path(path)
        self.cfg.save()

    def _set_mic(self, active: bool) -> None:
        self.cfg.record_mic = active
        self.cfg.save()

    def _set_countdown(self, seconds: int) -> None:
        self.cfg.countdown_seconds = seconds
        self.cfg.save()

    def _set_denoise(self, active: bool) -> None:
        self.cfg.mic_noise_suppression = active
        self.cfg.save()

    def _set_audio(self, active: bool) -> None:
        self.cfg.record_audio = active
        self.cfg.save()

    def _on_choose_output(self, _btn) -> None:
        dialog = Gtk.FileChooserNative(
            title="Choose output folder",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Select",
            cancel_label="Cancel",
        )
        dialog.set_current_folder(Gio.File.new_for_path(str(self.cfg.output_dir)))

        def on_response(d, response):
            if response == Gtk.ResponseType.ACCEPT and d.get_file():
                self._set_output_dir(d.get_file().get_path())

        dialog.connect("response", on_response)
        dialog.show()

    # ------------------------------------------------------------------
    # Recording controls
    # ------------------------------------------------------------------
    def _on_record(self, _btn) -> None:
        self.controller.start_recording()  # errors arrive via _flash_error

    def _on_zoom(self, _btn) -> None:
        self.controller.toggle_zoom()  # button updates via _on_change

    def _on_change(self) -> None:
        """Any controller change (hotkey, top bar, this window)."""
        state = self.controller.state
        if state != self._shown_state:
            self._shown_state = state
            self._on_state(state)
        if state == STATE_COUNTDOWN:
            self.status_big.set_text(f"Starting in {self.controller.countdown}…")
        self._sync_zoom()

    def _sync_zoom(self) -> None:
        on = self.controller.state in ACTIVE_STATES and self.controller.zoom_on
        self.zoom_btn.set_label("🔍 Zoom ON" if on else "🔍 Zoom")
        if on:
            self.zoom_btn.add_css_class("zoom-on")
        else:
            self.zoom_btn.remove_css_class("zoom-on")

    def _on_pause(self, _btn) -> None:
        self.controller.toggle_pause()

    def _on_stop(self, _btn) -> None:
        self.controller.stop_recording()

    def _flash_error(self, message: str) -> None:
        self.status_big.set_text("Error")
        self.status_sub.set_text(message)

    # ------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if state == STATE_IDLE:
            self.status_big.set_text("Ready")
            self.status_sub.set_text(self._hint_text())
            self.record_btn.set_sensitive(True)
            self.pause_btn.set_sensitive(False)
            self.stop_btn.set_sensitive(False)
            self.zoom_btn.set_sensitive(False)
            self.zoom_btn.set_label("🔍 Zoom")
            self.zoom_btn.remove_css_class("zoom-on")
        elif state == STATE_COUNTDOWN:
            self.status_sub.set_text("Press ■ Stop or your record hotkey to cancel")
            self.record_btn.set_sensitive(False)
            self.pause_btn.set_sensitive(False)
            self.stop_btn.set_sensitive(True)
            self.zoom_btn.set_sensitive(False)
        elif state == STATE_RECORDING:
            self.status_big.set_text("● Recording")
            self.status_sub.set_text("Pause with ⏸ or your hotkey")
            self.record_btn.set_sensitive(False)
            self.pause_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(True)
            self.zoom_btn.set_sensitive(True)
            self.pause_btn.set_label("⏸ Pause")
        elif state == STATE_PAUSED:
            self.status_big.set_text("⏸ Paused")
            self.status_sub.set_text("Press ▶ or your hotkey to continue")
            self.pause_btn.set_label("▶ Resume")