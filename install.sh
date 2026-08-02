#!/bin/sh
# tup installer — https://github.com/ernsoylu/tup
#
#   curl -fsSL https://raw.githubusercontent.com/ernsoylu/tup/main/install.sh | sh
#
# Detects the OS (Linux, macOS) and architecture, downloads the latest release,
# and installs it to ~/.tup/bin (and symlinks to ~/.local/bin or /usr/local/bin).
set -eu

REPO="ernsoylu/tup"

say() { printf '%s\n' "$*"; }
die() { printf 'tup installer: %s\n' "$*" >&2; exit 1; }

https_curl() { curl -fsSL --proto '=https' --proto-redir '=https' "$@"; }

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

os=$(uname -s)
case "$os" in
	Linux) os_name=linux ;;
	Darwin) os_name=macos ;;
	*) die "unsupported OS: $os (Windows: download from https://github.com/$REPO/releases)" ;;
esac

arch=$(uname -m)
case "$arch" in
	x86_64 | amd64) arch_name=x86_64 ;;
	aarch64 | arm64) arch_name=arm64 ;;
	*) die "unsupported architecture: $arch" ;;
esac

if [ "$os_name" = "linux" ] && [ "$arch_name" = "arm64" ]; then
    die "no prebuilt binaries for Linux arm64 yet."
fi

target="${os_name}-${arch_name}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

say "Finding the latest release..."
release_json=$(https_curl "https://api.github.com/repos/$REPO/releases/latest") ||
	die "could not reach the release feed"
tag=$(printf '%s' "$release_json" | grep '"tag_name"' | head -1 |
	sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
[ -n "$tag" ] || die "could not determine the latest release"

asset="tup-${tag}-${target}.tar.gz"
base="https://github.com/$REPO/releases/download/$tag"

say "Downloading $asset ($tag)..."
https_curl -o "$tmp/$asset" "$base/$asset" ||
	die "download failed — is there a $target build for $tag?"

tar -xzf "$tmp/$asset" -C "$tmp"
extract_dir="$tmp/tup-${target}"

if [ ! -d "$extract_dir" ]; then
    die "archive did not contain the expected directory tup-${target}"
fi

# We use the packaged install.sh from the tarball to finish the installation.
if [ -f "$extract_dir/install.sh" ]; then
    say "Running installer..."
    sh "$extract_dir/install.sh" "$extract_dir/tup"
else
    die "install.sh not found inside the release archive."
fi

say ""
say "Release notes: https://github.com/$REPO/releases/tag/$tag"
