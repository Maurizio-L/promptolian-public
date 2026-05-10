#!/usr/bin/env bash
# build.sh — Sync shared source to Chrome (MV3) and Firefox (MV2) extension dirs
# Usage: ./build.sh [chrome|firefox|all]
# Default: all

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/src"
SHARED=(content.js background.js content.css popup.html)

sync_to() {
  local target="$1"
  echo "  → $target"
  for f in "${SHARED[@]}"; do
    cp "$SRC/$f" "$target/$f"
  done
  cp -r "$SRC/icons/"*.png "$target/icons/"
}

TARGET="${1:-all}"

case "$TARGET" in
  chrome)  sync_to "$SCRIPT_DIR/chrome" ;;
  firefox) sync_to "$SCRIPT_DIR/firefox" ;;
  all)
    sync_to "$SCRIPT_DIR/chrome"
    sync_to "$SCRIPT_DIR/firefox"
    ;;
  *)
    echo "Usage: $0 [chrome|firefox|all]" >&2
    exit 1
    ;;
esac

echo "Build complete."