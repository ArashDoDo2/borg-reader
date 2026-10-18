#!/bin/bash
# Called by .cpanel.yml when "Deploy HEAD Commit" is pressed in cPanel > Git Version Control.
# Branch decides the target:  main -> ~/public_html/borg   staging -> ~/public_html/borg-test
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"

case "$BRANCH" in
  main)    TARGET="$HOME/public_html/borg" ;;
  staging) TARGET="$HOME/public_html/borg-test" ;;
  *) echo "Refusing to deploy branch '$BRANCH' (only main or staging)." >&2; exit 1 ;;
esac

# Safety net: never write anywhere except the two known folders.
case "$TARGET" in
  "$HOME/public_html/borg"|"$HOME/public_html/borg-test") ;;
  *) echo "Unexpected target $TARGET" >&2; exit 1 ;;
esac

mkdir -p "$TARGET"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$REPO_DIR/src/" "$TARGET/"
else
  cp -R "$REPO_DIR/src/." "$TARGET/"
fi

# Web server must be able to read everything (the 403 lesson of v5).
find "$TARGET" -type d -exec chmod 755 {} +
find "$TARGET" -type f -exec chmod 644 {} +

echo "Deployed $(git -C "$REPO_DIR" rev-parse --short HEAD) from '$BRANCH' to $TARGET"
