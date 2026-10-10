#!/usr/bin/env bash
# Removes what install.sh put into ~/.local. Settings in ~/.config/screenwave
# and recordings are kept unless --purge is given (recordings always are).
set -euo pipefail

APP_ID="io.github.ferhateroglu.Screenwave"
UUID="screenwave@ferhateroglu.github.io"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/screenwave"
BIN="$HOME/.local/bin/screenwave"

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# only if it's running: a plain call would D-Bus-activate it just to quit
if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
     --method org.freedesktop.DBus.NameHasOwner "$APP_ID" 2>/dev/null | grep -q true; then
  say "Stopping Screenwave (a running recording is finalized first)"
  gdbus call --session --dest "$APP_ID" --object-path /io/github/ferhateroglu/Screenwave \
    --method org.freedesktop.Application.ActivateAction quit '[]' '{}' >/dev/null 2>&1 || true
fi

say "Removing hotkeys"
if [[ -x "$BIN" ]]; then
  "$BIN" --remove-hotkeys || true
fi

say "Removing the top-bar extension"
gnome-extensions disable "$UUID" 2>/dev/null || true
current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '@as []')"
python3 - "$UUID" "$current" <<'PY' | xargs -0 gsettings set org.gnome.shell enabled-extensions || true
import ast, sys
uuid, raw = sys.argv[1], sys.argv[2].removeprefix("@as").strip()
items = [u for u in ast.literal_eval(raw or "[]") if u != uuid]
print("[" + ", ".join(repr(u) for u in items) + "]", end="")
PY
rm -rf "${DATA_HOME:?}/gnome-shell/extensions/$UUID"

say "Removing app files"
rm -rf "$APP_DIR"
rm -f "$BIN" \
  "$DATA_HOME/applications/$APP_ID.desktop" \
  "$DATA_HOME/dbus-1/services/$APP_ID.service" \
  "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
update-desktop-database -q "$DATA_HOME/applications" 2>/dev/null || true

if ((PURGE)); then
  say "Removing settings and logs"
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/screenwave" "${XDG_CACHE_HOME:-$HOME/.cache}/screenwave"
fi

say "Done. The top-bar icon disappears after the next login."
