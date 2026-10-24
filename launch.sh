#!/usr/bin/env sh
# Launches OpenPad from anywhere. Only Python 3 + tkinter required.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/openpad.py" "$@"
