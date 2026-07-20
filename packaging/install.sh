#!/bin/sh
# tup installer (macOS / Linux).
#
# Installs the tup binary into ~/.tup/bin (tup's home directory, next to its
# .env and registry.db) and symlinks it onto your PATH so `tup` works directly
# in the terminal.
#
# Usage:
#   ./install.sh            # installs the `tup` binary found next to this script
#   ./install.sh /path/tup  # installs a specific binary
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BINARY="${1:-$SCRIPT_DIR/tup}"

if [ ! -f "$BINARY" ]; then
    echo "error: tup binary not found at $BINARY" >&2
    echo "Download a release archive for your OS from the project's releases page," >&2
    echo "extract it, and run this script from inside it (or pass the binary path)." >&2
    exit 1
fi

TUP_HOME="${TUP_CONFIG_DIR:-$HOME/.tup}"
INSTALL_DIR="$TUP_HOME/bin"
mkdir -p "$INSTALL_DIR"
cp "$BINARY" "$INSTALL_DIR/tup"
chmod +x "$INSTALL_DIR/tup"

# macOS quarantines downloaded binaries; clear it so the first run isn't blocked.
if [ "$(uname)" = "Darwin" ] && command -v xattr >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$INSTALL_DIR/tup" 2>/dev/null || true
fi

# Symlink onto the PATH: /usr/local/bin when writable, else ~/.local/bin.
LINK_DIR="/usr/local/bin"
if [ ! -w "$LINK_DIR" ]; then
    LINK_DIR="$HOME/.local/bin"
    mkdir -p "$LINK_DIR"
fi
ln -sf "$INSTALL_DIR/tup" "$LINK_DIR/tup"

echo "✅ Installed $INSTALL_DIR/tup"
echo "✅ Symlinked $LINK_DIR/tup -> $INSTALL_DIR/tup"

case ":$PATH:" in
    *":$LINK_DIR:"*) ;;
    *)
        echo ""
        echo "⚠ $LINK_DIR is not on your PATH. Add this to your shell profile:"
        echo "    export PATH=\"$LINK_DIR:\$PATH\""
        ;;
esac

echo ""
echo "Run 'tup' to get started (first launch opens the setup wizard),"
echo "or 'tup gui' for the graphical explorer."
