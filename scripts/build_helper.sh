#!/usr/bin/env bash
# Builds bin/swcursor (real pointer position from Mutter's cursor metadata).
# Needs: gcc, pkg-config, libpipewire-0.3-dev
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p bin
gcc -O2 -Wall -o bin/swcursor helper/swcursor.c $(pkg-config --cflags --libs libpipewire-0.3)
echo "built bin/swcursor"
