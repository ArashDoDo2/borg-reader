#!/bin/bash
# Checks a deployed copy of Borg: every app file must return HTTP 200 with a sane type,
# and (optionally) index.html must carry the expected app version.
# Usage: scripts/smoke-test.sh https://parsun.com/borg-test/ [expected-version]
set -uo pipefail

BASE="${1:?usage: smoke-test.sh BASE_URL [expected-version]}"
BASE="${BASE%/}/"
EXPECTED="${2:-}"
FAIL=0

FILES=(
  "" index.html offline.html manifest.webmanifest service-worker.js
  assets/css/app.css assets/css/reader-fonts.css assets/js/app.js
  assets/vendor/jszip.min.js assets/vendor/epub.min.js
  assets/fonts/Vazirmatn-Variable.woff2 assets/icon-192.png assets/icon-512.png
)

for f in "${FILES[@]}"; do
  read -r code type < <(curl -sS -o /dev/null -w "%{http_code} %{content_type}\n" --max-time 20 "$BASE$f" || echo "000 -")
  if [ "$code" = "200" ]; then
    printf "OK   %s  %s\n" "${f:-/}" "$type"
  else
    printf "FAIL %s  HTTP %s\n" "${f:-/}" "$code"; FAIL=1
  fi
done

if [ -n "$EXPECTED" ]; then
  got="$(curl -sS --max-time 20 "${BASE}index.html" | sed -n 's/.*name="app-version" content="\([^"]*\)".*/\1/p' | head -1)"
  if [ "$got" = "$EXPECTED" ]; then echo "OK   version $got"; else echo "FAIL version: expected $EXPECTED, got '${got:-none}'"; FAIL=1; fi
fi

# Packages and internal notes must never be public.
for f in borg.zip README_FA.md .git/HEAD .cpanel.yml; do
  code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 20 "$BASE$f" || echo 000)"
  if [ "$code" = "200" ]; then echo "FAIL public file should not exist: $f"; FAIL=1; fi
done

[ "$FAIL" = 0 ] && echo "Smoke test passed" || echo "Smoke test FAILED"
exit "$FAIL"
