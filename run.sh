#!/usr/bin/env bash
# Run Screenwave straight from the source tree (development).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 -m screenwave "$@"
