#!/usr/bin/env sh
# Install or upgrade OpenPad for this Linux user; no root or Python packages added.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST="$HOME/.local/share/openpad"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"

if ! command -v python3 >/dev/null 2>&1; then
    printf 'OpenPad needs Python 3. Install python3 with your package manager.\n' >&2
    exit 1
fi
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    printf 'OpenPad needs Tkinter. On Ubuntu/Mint: sudo apt install python3-tk\n' >&2
    exit 1
fi
mkdir -p "$DEST" "$BIN" "$APPS"
cp "$HERE/openpad.py" "$DEST/openpad.py"
cp "$HERE/assets/openpad-128.png" "$DEST/openpad.png"
for size in 64 128 256 512; do
    mkdir -p "$ICONS/${size}x${size}/apps"
    cp "$HERE/assets/openpad-${size}.png" "$ICONS/${size}x${size}/apps/openpad.png"
done
# Main executable is a wrapper so Desktop apps can open spaces in filenames safely.
cat > "$BIN/openpad" <<EOF
#!/usr/bin/env sh
exec python3 "$DEST/openpad.py" "\$@"
EOF
chmod 755 "$BIN/openpad"
cat > "$APPS/openpad.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=OpenPad
GenericName=Text Editor
Comment=Minimal, Notepad-inspired text editor
Exec=$BIN/openpad %F
TryExec=$BIN/openpad
Icon=$DEST/openpad.png
Terminal=false
Categories=Utility;TextEditor;
MimeType=text/plain;
StartupNotify=true
Keywords=notepad;notes;text;editor;txt;
EOF
chmod 644 "$APPS/openpad.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -f "$ICONS/index.theme" ]; then
    gtk-update-icon-cache -f "$ICONS" >/dev/null 2>&1 || true
fi
printf '\nOpenPad installed/upgraded successfully.\n'
printf 'Launch: %s/openpad\n' "$BIN"
printf 'To associate .txt files: %s/set-default.sh\n' "$HERE"
printf 'Your existing OpenPad settings and recovered notes were NOT overwritten.\n'
