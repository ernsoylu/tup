#!/bin/sh
set -e

# tup - installation script (MDView style)
# Installs the compiled Go binary

REPO="ernsoylu/tup"
GITHUB_URL="https://api.github.com/repos/$REPO/releases/latest"

echo "Fetching latest release information..."
if command -v curl >/dev/null 2>&1; then
    RELEASE_DATA=$(curl -s $GITHUB_URL)
elif command -v wget >/dev/null 2>&1; then
    RELEASE_DATA=$(wget --https-only --max-redirect=0 -qO- $GITHUB_URL)
else
    echo "Error: curl or wget is required" >&2
    exit 1
fi

VERSION=$(echo "$RELEASE_DATA" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "Error: Could not determine latest version" >&2
    exit 1
fi

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

if [ "$ARCH" = "x86_64" ]; then
    ARCH="amd64"
elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    ARCH="arm64"
fi

if [ "$OS" = "windows" ]; then
    echo "Error: on Windows, download the zip manually from https://github.com/$REPO/releases" >&2
    exit 1
fi

ARCHIVE_NAME="tup_${OS}_${ARCH}.tar.gz"
DOWNLOAD_URL="https://github.com/$REPO/releases/download/$VERSION/$ARCHIVE_NAME"

DEST_DIR="/usr/local/bin"
if [ ! -w "$DEST_DIR" ]; then
    DEST_DIR="$HOME/.local/bin"
    mkdir -p "$DEST_DIR"
    echo "Note: Installing to $DEST_DIR because /usr/local/bin is not writable."
    echo "Make sure $DEST_DIR is in your PATH."
fi

DEST_FILE="$DEST_DIR/tup"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Downloading tup $VERSION for $OS ($ARCH)..."
if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -fL "$DOWNLOAD_URL" -o "$TMP_DIR/$ARCHIVE_NAME"
else
    wget --https-only "$DOWNLOAD_URL" -O "$TMP_DIR/$ARCHIVE_NAME"
fi

tar -xzf "$TMP_DIR/$ARCHIVE_NAME" -C "$TMP_DIR" tup
install -m 755 "$TMP_DIR/tup" "$DEST_FILE"

echo "Successfully installed tup to $DEST_FILE"
echo "Run 'tup login' to get started!"
