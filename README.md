# Screenwave

A minimal screen recorder for **GNOME on Wayland**, with smooth
cursor-following zoom (Screen Studio style), a top-bar indicator, a 3-2-1
countdown and global hotkeys.

Python + GStreamer + GTK4/libadwaita, capturing straight from Mutter — no
screen-sharing dialog.

## Features

- **Top-bar indicator** — always there: red ● and elapsed time while
  recording, ⏸ when paused, green 🔍 when zoom is on. Its menu starts/stops,
  pauses, toggles zoom and opens the settings.
- **3-2-1 countdown** — a big countdown on the monitor being recorded, then
  recording starts exactly at zero (configurable, 0–10 s).
- **Global hotkeys** — work from anywhere, even when the app isn't running.
- **Cursor-following zoom** — the view holds still while the pointer is near
  the middle and glides when it nears an edge; no jitter on small movements.
- **Pause / resume** — paused time is cut from the file.
- **Audio** — desktop sound plus microphone, with WebRTC noise suppression on
  the mic.
- **Exact pointer position** from Mutter's cursor metadata; picks the
  external monitor by default.
- **Crash-safe output** — fragmented MP4 (H.264 + AAC) in `~/Videos`.
- Runs in the background only when needed: it starts on demand and quits
  after 10 idle minutes.

## Requirements

- GNOME Shell 45 or newer on **Wayland** (tested on GNOME 48, Debian 13).
  It uses Mutter's ScreenCast API, so other desktops are not supported.
- Python 3.10+, GTK 4, libadwaita, GStreamer 1.22+ with the PipeWire plugin.

## Install

```bash
git clone https://github.com/ferhateroglu/screenwave.git
cd screenwave
./install.sh --install-deps   # Debian/Ubuntu: also apt-installs what's missing
```

Without `--install-deps` the installer only checks dependencies and prints
the packages to install. Everything else goes into `~/.local` (no root).

**Log out and back in once** after the first install, so GNOME Shell loads the
top-bar extension. Then use the top-bar icon, a hotkey, or "Screenwave" in the
app grid.

Update: `git pull && ./install.sh`. Uninstall: `./uninstall.sh` (add
`--purge` to also delete settings).

## Usage

| Hotkey (default) | Action |
| --- | --- |
| `Ctrl+Alt+R` | Start (after the countdown) / stop — during the countdown it cancels |
| `Ctrl+Alt+P` | Pause / resume |
| `Ctrl+Alt+Z` | Cursor zoom on / off |
| `Ctrl+Alt+S` | Stop |

Change the keys in the app: top-bar menu → **Open Screenwave** → menu
(top-right) → **Hotkeys** → "Change…". Wayland doesn't let apps grab global
keys, so Screenwave registers them as GNOME custom shortcuts (they show up in
Settings → Keyboard → Custom Shortcuts too).

The same settings window has the monitor, frame rate, bitrate, audio,
countdown and zoom options. Settings live in
`~/.config/screenwave/settings.json`; recordings go to
`~/Videos/screenwave-<date>_<time>.mp4`.

### Command line / scripting

The app is controlled over D-Bus; calling it starts it if needed:

```bash
gdbus call --session --dest io.github.ferhateroglu.Screenwave \
  --object-path /io/github/ferhateroglu/Screenwave/Status \
  --method io.github.ferhateroglu.Screenwave.Status.Command record-toggle
```

Actions: `record-toggle`, `record`, `stop`, `toggle` (pause/resume), `pause`,
`resume`, `zoom`, `status`. The object also exposes `State`, `Zoom`,
`Elapsed`, `Countdown` and `Monitor` properties.

## Troubleshooting

- **Logs**: `~/.cache/screenwave/screenwave.log`, or
  `journalctl --user -f | grep -i screenwave` while it runs in the background.
- **No top-bar icon**: log out and in once after installing; check
  `gnome-extensions info screenwave@ferhateroglu.github.io`.
- **Hotkeys do nothing**: GNOME's shortcut daemon may have crashed —
  `systemctl --user restart org.gnome.SettingsDaemon.MediaKeys.target`.
  Re-register with `~/.local/bin/screenwave --apply-hotkeys`.
- **Capture test** without the app:
  `python3 scripts/self_test.py` (real capture, ~6 s) or
  `python3 scripts/self_test.py --fake` (test sources, no capture).
- **Zoom doesn't follow the pointer**: the `swcursor` helper wasn't built —
  install `gcc pkg-config libpipewire-0.3-dev` and re-run `./install.sh`.

## Development

```bash
./install.sh --dev                       # symlink app + extension to this checkout
./run.sh                                 # or run straight from the tree
scripts/build_helper.sh                  # builds bin/swcursor for run.sh
python3 -m unittest discover -s tests -t .
```

With `--dev`, Python changes apply on the next app start; extension changes
need a re-login (Wayland can't reload GNOME Shell). To try the extension
without logging out, run a nested shell:
`dbus-run-session -- gnome-shell --nested --wayland`.

```
screenwave/
  main.py            entry: D-Bus activatable Adw.Application, idle exit, CLI flags
  recorder.py        state machine: idle → countdown → recording ⇄ paused
  dbus_status.py     D-Bus interface (state properties + Command)
  pipeline.py        GStreamer: pipewiresrc + pulsesrc → videocrop (zoom) → x264/aac → mp4
  mutter_capture.py  Mutter ScreenCast sessions (monitor stream, cursor metadata stream)
  renderer.py        zoom-follow camera (dead zone + critically damped spring)
  cursor_tracker.py  pointer sources: swcursor helper | evdev | none
  hotkeys.py         GNOME custom-keybinding entries
  config.py          settings.json with migrations
  ui/window.py       libadwaita window + settings
gnome-extension/     top-bar indicator and countdown overlay (GNOME Shell extension)
helper/swcursor.c    reads Mutter's cursor metadata from PipeWire
data/                desktop file, D-Bus service, icon
```

### Design notes

- Zoom changes the `videocrop` margins once per frame, applied from a pad
  probe in the streaming thread so a frame never sees a half-updated
  rectangle; the output size never changes.
- All audio sources run with `provide-clock=false`: `pipewiresrc` only
  behaves with its own clock (with a PulseAudio clock frames stall and EOS
  never arrives).
- Pause keeps capture running and drops frames/audio at the encoders,
  shifting later timestamps back — `pipewiresrc` can't resume a Mutter stream
  after a GStreamer PAUSED round-trip.
- The countdown opens the capture streams while the numbers run, so the
  first frame lands right at zero.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
