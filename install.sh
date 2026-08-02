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
    RELEASE_DATA=$(wget -qO- $GITHUB_URL)
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

BINARY_NAME="tup_${OS}_${ARCH}"
if [ "$OS" = "darwin" ]; then
    BINARY_NAME="tup_darwin_${ARCH}"
fi

DOWNLOAD_URL="https://github.com/$REPO/releases/download/$VERSION/$BINARY_NAME"
if [ "$OS" = "windows" ]; then
    DOWNLOAD_URL="${DOWNLOAD_URL}.exe"
fi

DEST_DIR="/usr/local/bin"
if [ ! -w "$DEST_DIR" ]; then
    DEST_DIR="$HOME/.local/bin"
    mkdir -p "$DEST_DIR"
    echo "Note: Installing to $DEST_DIR because /usr/local/bin is not writable."
    echo "Make sure $DEST_DIR is in your PATH."
fi

DEST_FILE="$DEST_DIR/tup"

echo "Downloading tup $VERSION for $OS ($ARCH)..."
if command -v curl >/dev/null 2>&1; then
    curl -L "$DOWNLOAD_URL" -o "$DEST_FILE"
else
    wget "$DOWNLOAD_URL" -O "$DEST_FILE"
fi

chmod +x "$DEST_FILE"

echo "Successfully installed tup to $DEST_FILE"
echo "Run 'tup login' to get started!"
