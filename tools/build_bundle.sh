#!/usr/bin/env bash
# Rebuild bundle/divar_hunter.zip from the tracked files.
#
# The archive is committed so a permanent direct link exists:
#   https://github.com/fa833618/divar-hunter/raw/arena/01a0d935-divar-hunter/bundle/divar_hunter.zip
#
# Note: the bundle lives in bundle/, NOT dist/ — the dev workspace does not
# persist directories named dist/build/out, so a bundle stored there disappears.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p bundle

git ls-files -z | grep -zv '^bundle/' | tar --null -T - -cf - | (cd "$STAGE" && tar -xf -)

# A dotless twin of the env template: .env.example is invisible in Windows
# Explorer and some file managers refuse to extract dotfiles.
cp .env.example "$STAGE/env.example"

rm -f bundle/divar_hunter.zip
(cd "$STAGE" && zip -qrX "$ROOT/bundle/divar_hunter.zip" .)

unzip -t bundle/divar_hunter.zip >/dev/null
echo "bundle/divar_hunter.zip"
unzip -l bundle/divar_hunter.zip | tail -2 | head -1
sha256sum bundle/divar_hunter.zip
