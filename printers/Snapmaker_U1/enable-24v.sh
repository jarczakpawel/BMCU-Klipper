#!/bin/sh
set -eu

PERSIST_DIR=/oem/user-24v
PERSIST_SCRIPT=$PERSIST_DIR/enable-24v.sh
SERVICE=/etc/init.d/S60klipper

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

apply_config() {
python3 - <<'PY'
import os
import re
import stat
import tempfile

cfg_dir = "/home/lava/printer_data/config"
printer_cfg = os.path.join(cfg_dir, "printer.cfg")
power_cfg = os.path.join(cfg_dir, "user_bmcu_power.cfg")
old_power_cfg = os.path.join(cfg_dir, "user_24v_power.cfg")
include = "[include user_bmcu_power.cfg]"
old_include = "[include user_24v_power.cfg]"
power_text = "[output_pin bmcu_24v]\npin: PE15\nvalue: 1\nshutdown_value: 1\n"


def atomic_write(path, data, source_stat=None):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if source_stat is not None:
            os.chmod(temporary, stat.S_IMODE(source_stat.st_mode))
            os.chown(temporary, source_stat.st_uid, source_stat.st_gid)
        else:
            os.chmod(temporary, 0o664)
            try:
                import pwd
                import grp
                os.chown(temporary, pwd.getpwnam("lava").pw_uid, grp.getgrnam("lava").gr_gid)
            except (KeyError, PermissionError):
                pass
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if not os.path.isfile(printer_cfg):
    raise SystemExit("printer.cfg is missing")

wanted = power_text.encode("utf-8")
try:
    with open(power_cfg, "rb") as stream:
        current = stream.read()
except FileNotFoundError:
    current = None
if current != wanted:
    source_stat = os.stat(power_cfg) if os.path.exists(power_cfg) else None
    atomic_write(power_cfg, wanted, source_stat)

with open(printer_cfg, "rb") as stream:
    original = stream.read()
info = os.stat(printer_cfg)
text = original.decode("utf-8")
newline = "\r\n" if "\r\n" in text else "\n"
source_lines = text.replace("\r\n", "\n").split("\n")
lines = []
section = ""

for number, line in enumerate(source_lines, 1):
    stripped = line.strip()
    if stripped in (include, old_include):
        continue
    if stripped.startswith("[") and stripped.endswith("]"):
        section = stripped.lower()
        lines.append(line)
        continue
    match = re.match(r"^\s*(pin|enable_pin|power_enable_pin)\s*:\s*([^#;\s]+)", line, re.IGNORECASE)
    if match and match.group(2).lstrip("!^~").upper() == "PE15":
        if section == "[purifier]" and match.group(1).lower() == "power_enable_pin":
            continue
        raise SystemExit("PE15 is already used in printer.cfg at line %d: %s" % (number, stripped))
    lines.append(line)

insert_at = next(
    (i for i, line in enumerate(lines) if line.startswith("#*# <---------------------- SAVE_CONFIG")),
    len(lines),
)
while insert_at > 0 and lines[insert_at - 1] == "":
    insert_at -= 1
lines[insert_at:insert_at] = ["", include, ""]
updated = newline.join(lines).rstrip("\r\n") + newline
updated_bytes = updated.encode("utf-8")
if updated_bytes != original:
    atomic_write(printer_cfg, updated_bytes, info)

if os.path.exists(old_power_cfg):
    os.unlink(old_power_cfg)
PY
}

patch_service() {
python3 - "$SERVICE" "$PERSIST_SCRIPT" <<'PY'
import os
import re
import stat
import sys
import tempfile

path = sys.argv[1]
script = sys.argv[2]
marker = "# USER-24V"
call = script + " --apply-only"

with open(path, "rb") as stream:
    original = stream.read()
info = os.stat(path)
text = original.decode("utf-8")
newline = "\r\n" if "\r\n" in text else "\n"
lines = text.splitlines()
clean = []
i = 0
while i < len(lines):
    if lines[i].strip() == marker and i + 1 < len(lines) and lines[i + 1].strip() == call:
        i += 2
        continue
    clean.append(lines[i])
    i += 1

launch = re.compile(r"^(?P<indent>[ \t]*)start-stop-daemon[ \t]+-S(?:[ \t]|$)")
patched = []
count = 0
for line in clean:
    match = launch.match(line)
    if match:
        indent = match.group("indent")
        patched.append(indent + marker)
        patched.append(indent + call)
        count += 1
    patched.append(line)
if count == 0:
    raise SystemExit("S60klipper has no start-stop-daemon launch")

updated = (newline.join(patched).rstrip("\r\n") + newline).encode("utf-8")
if updated == original:
    raise SystemExit(0)
parent = os.path.dirname(path)
fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), dir=parent)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, stat.S_IMODE(info.st_mode))
    os.chown(temporary, info.st_uid, info.st_gid)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

if [ "${1:-}" = "--apply-only" ]; then
    apply_config
    exit 0
fi

[ -f "$SERVICE" ] || { echo "S60klipper is missing." >&2; exit 1; }
mkdir -p "$PERSIST_DIR"
if [ ! -f "$PERSIST_DIR/S60klipper.before-24v" ]; then
    cp -a "$SERVICE" "$PERSIST_DIR/S60klipper.before-24v"
fi
if [ ! -f "$PERSIST_DIR/printer.cfg.before-24v" ]; then
    cp -a /home/lava/printer_data/config/printer.cfg "$PERSIST_DIR/printer.cfg.before-24v"
fi
cp "$0" "$PERSIST_SCRIPT"
chmod 700 "$PERSIST_SCRIPT"
apply_config
patch_service
/bin/sh -n "$SERVICE"
"$SERVICE" stop
sleep 2
"$SERVICE" start
echo "Snapmaker U1 upper 6-pin port: 24 V enabled."
