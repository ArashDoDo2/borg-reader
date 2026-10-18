#!/bin/bash
# Fallback when Git deployment is unavailable: builds dist/borg-<version>.zip from src/
# with permissions the web server can read (files 644, folders 755).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/.*name="app-version" content="\([^"]*\)".*/\1/p' "$ROOT/src/index.html" | head -1)"
VERSION="${VERSION:-dev}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp -R "$ROOT/src/." "$TMP/"
find "$TMP" -type d -exec chmod 755 {} +
find "$TMP" -type f -exec chmod 644 {} +
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/borg-$VERSION.zip"
rm -f "$OUT"
(cd "$TMP" && zip -r -q -X "$OUT" .)
echo "Built $OUT"
