#!/bin/sh
set -eu

URL=https://github.com/jarczakpawel/BMCU-Klipper/releases/latest/download/BMCU-Klipper.zip
TMP=${TMPDIR:-/tmp}/bmcu-klipper-install.$$
ZIP=$TMP/BMCU-Klipper.zip

cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$TMP"

if command -v curl >/dev/null 2>&1; then
    curl -fL "$URL" -o "$ZIP"
elif command -v wget >/dev/null 2>&1; then
    wget -O "$ZIP" "$URL"
else
    echo "ERROR: curl or wget is required." >&2
    exit 1
fi

PYTHON=
for candidate in /usr/bin/python3 /bin/python3; do
    if [ -x "$candidate" ]; then
        PYTHON=$candidate
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.7 or newer is required." >&2
    exit 1
fi

"$PYTHON" - "$ZIP" "$TMP/release" <<'PY'
import pathlib
import sys
import zipfile

archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.mkdir()
with zipfile.ZipFile(str(archive)) as z:
    for info in z.infolist():
        name = info.filename.replace('\\', '/')
        parts = [part for part in name.split('/') if part]
        if not parts or name.startswith('/') or any(part in ('.', '..') for part in parts):
            raise SystemExit('ERROR: unsafe release archive')
        destination = target.joinpath(*parts)
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as source, open(destination, 'wb') as output:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
PY

COUNT=$(find "$TMP/release" -maxdepth 2 -type f -name install -print | wc -l | tr -d ' ')
if [ "$COUNT" -ne 1 ]; then
    echo "ERROR: release must contain exactly one installer." >&2
    exit 1
fi
INSTALL=$(find "$TMP/release" -maxdepth 2 -type f -name install -print)

sh "$INSTALL" "$@"
