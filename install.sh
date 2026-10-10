#!/usr/bin/env bash
# Screenwave installer — per user, into ~/.local. No root needed, except for
# missing system packages (see --install-deps).
#
#   ./install.sh                 install / update
#   ./install.sh --install-deps  also apt-install missing packages (sudo)
#   ./install.sh --dev           symlink the app and extension to this checkout
#
# Uninstall with ./uninstall.sh
set -euo pipefail

APP_ID="io.github.ferhateroglu.Screenwave"
UUID="screenwave@ferhateroglu.github.io"
LEGACY_UUIDS=("screenwave@ferhat")  # pre-release ids, removed on install

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/screenwave"
BIN="$HOME/.local/bin/screenwave"
EXT_DIR="$DATA_HOME/gnome-shell/extensions"

DEV=0
INSTALL_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------- checks
if [[ "${XDG_CURRENT_DESKTOP:-}" != *GNOME* ]]; then
  warn "Screenwave targets GNOME (Mutter); this session is '${XDG_CURRENT_DESKTOP:-unknown}'."
fi
if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
  warn "Screenwave is built for Wayland; this session is '${XDG_SESSION_TYPE:-unknown}'."
fi

# Debian/Ubuntu package names
APT_PKGS=(
  python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly gstreamer1.0-pipewire gstreamer1.0-pulseaudio
  libglib2.0-bin gcc pkg-config libpipewire-0.3-dev
)

missing_deps() {
  local missing=()
  python3 - <<'EOF' 2>/dev/null || missing+=("python GTK4/libadwaita/GStreamer bindings")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gst, Gtk
EOF
  for el in pipewiresrc pulsesrc videocrop x264enc voaacenc mp4mux audiomixer; do
    gst-inspect-1.0 "$el" >/dev/null 2>&1 || missing+=("GStreamer element $el")
  done
  command -v gdbus >/dev/null || missing+=("gdbus")
  if ((${#missing[@]})); then printf '%s\n' "${missing[@]}"; fi
}

say "Checking dependencies"
mapfile -t MISSING < <(missing_deps)
if ((${#MISSING[@]})); then
  printf '  missing: %s\n' "${MISSING[@]}"
  if ((INSTALL_DEPS)) && command -v apt-get >/dev/null; then
    say "Installing system packages (sudo)"
    sudo apt-get install -y "${APT_PKGS[@]}"
    mapfile -t MISSING < <(missing_deps)
  fi
  if ((${#MISSING[@]})); then
    echo
    echo "Install them first. On Debian/Ubuntu:"
    echo "  ./install.sh --install-deps"
    echo "or:"
    echo "  sudo apt install ${APT_PKGS[*]}"
    exit 1
  fi
fi
gst-inspect-1.0 webrtcdsp >/dev/null 2>&1 \
  || warn "webrtcdsp missing (gstreamer1.0-plugins-bad): no mic noise suppression"

# ---------------------------------------------------------------- app
say "Installing the app to $APP_DIR"
mkdir -p "$APP_DIR/bin" "$(dirname "$BIN")"
rm -rf "$APP_DIR/screenwave"
if ((DEV)); then
  ln -s "$ROOT/screenwave" "$APP_DIR/screenwave"
else
  cp -r "$ROOT/screenwave" "$APP_DIR/screenwave"
  find "$APP_DIR/screenwave" -name __pycache__ -prune -exec rm -rf {} +
fi

if command -v gcc >/dev/null && pkg-config --exists libpipewire-0.3 2>/dev/null; then
  # shellcheck disable=SC2046
  gcc -O2 -Wall -o "$APP_DIR/bin/swcursor" "$ROOT/helper/swcursor.c" \
    $(pkg-config --cflags --libs libpipewire-0.3)
else
  warn "gcc or libpipewire-0.3-dev missing: cursor following falls back to a fixed center"
  rm -f "$APP_DIR/bin/swcursor"
fi

cat > "$BIN" <<EOF
#!/bin/sh
PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m screenwave "\$@"
EOF
chmod +x "$BIN"

say "Registering with the desktop (launcher, icon, D-Bus activation)"
mkdir -p "$DATA_HOME/applications" "$DATA_HOME/dbus-1/services" \
         "$DATA_HOME/icons/hicolor/scalable/apps"
sed "s|@BIN@|$BIN|" "$ROOT/data/$APP_ID.desktop.in" > "$DATA_HOME/applications/$APP_ID.desktop"
sed "s|@BIN@|$BIN|" "$ROOT/data/$APP_ID.service.in" > "$DATA_HOME/dbus-1/services/$APP_ID.service"
cp "$ROOT/data/icons/$APP_ID.svg" "$DATA_HOME/icons/hicolor/scalable/apps/"
update-desktop-database -q "$DATA_HOME/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
# let the running bus pick up the new service file
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true

# ---------------------------------------------------------------- extension
say "Installing the top-bar extension ($UUID)"
enabled_list() { gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []"; }
set_enabled() {  # $1 = uuid, $2 = 1 (add) / 0 (remove)
  python3 - "$1" "$2" "$(enabled_list)" <<'EOF' | xargs -0 gsettings set org.gnome.shell enabled-extensions
import ast, sys
uuid, add, raw = sys.argv[1], sys.argv[2] == "1", sys.argv[3].removeprefix("@as").strip()
items = [u for u in ast.literal_eval(raw or "[]") if u != uuid] + ([uuid] if add else [])
print("[" + ", ".join(repr(u) for u in items) + "]", end="")
EOF
}
for legacy in "${LEGACY_UUIDS[@]}"; do
  if [[ -e "$EXT_DIR/$legacy" || -L "$EXT_DIR/$legacy" ]]; then
    gnome-extensions disable "$legacy" 2>/dev/null || true
    rm -rf "${EXT_DIR:?}/$legacy"
  fi
  set_enabled "$legacy" 0
done

mkdir -p "$EXT_DIR"
rm -rf "${EXT_DIR:?}/$UUID"
if ((DEV)); then
  ln -s "$ROOT/gnome-extension/$UUID" "$EXT_DIR/$UUID"
else
  cp -r "$ROOT/gnome-extension/$UUID" "$EXT_DIR/$UUID"
fi
RELOGIN=0
if gnome-extensions info "$UUID" >/dev/null 2>&1; then
  gnome-extensions enable "$UUID"
else
  set_enabled "$UUID" 1  # GNOME Shell loads it at next login
  RELOGIN=1
fi

# ---------------------------------------------------------------- hotkeys
say "Registering hotkeys with GNOME"
"$BIN" --apply-hotkeys

echo
say "Done"
echo "  Start it from the app grid (\"Screenwave\"), the top-bar icon, or a hotkey."
echo "  Default hotkeys: Ctrl+Alt+R start/stop, Ctrl+Alt+P pause/resume, Ctrl+Alt+Z zoom, Ctrl+Alt+S stop"
if ((RELOGIN)); then
  echo
  printf '\033[1mLog out and back in once\033[0m so GNOME Shell loads the top-bar extension.\n'
elif ! ((DEV)); then
  echo "  Extension updates take effect after the next login."
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo "  (Optional) add ~/.local/bin to PATH to run 'screenwave' from a terminal." ;;
esac
