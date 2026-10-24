#!/usr/bin/env sh
# Registers OpenPad as the user's preferred text/plain opener (usually .txt).
# Does not uninstall Kate or change permissions or system-wide defaults.
set -eu
DESKTOP_FILE="$HOME/.local/share/applications/openpad.desktop"
if [ ! -f "$DESKTOP_FILE" ]; then
    printf 'OpenPad must be installed first: run ./install.sh\n' >&2
    exit 1
fi
if ! command -v xdg-mime >/dev/null 2>&1; then
    printf 'Cannot set default: install the xdg-utils package.\n' >&2
    exit 1
fi
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}"
xdg-mime default openpad.desktop text/plain
RESULT=$(xdg-mime query default text/plain || true)
printf 'Default text editor (text/plain): %s\n' "$RESULT"
if [ "$RESULT" != openpad.desktop ]; then
    printf 'Your desktop may have overridden the default. You can also right-click a .txt file > Open With > OpenPad > Always.\n' >&2
    exit 1
fi
printf 'Finished. .txt files should open in OpenPad after the file manager refreshes.\n'
