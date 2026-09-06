#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import dataclasses
import glob
import json
import math
import os
import pathlib
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

SCHEMA = 1
MAX_SCAN_RESULTS = 64
KLIPPY_RE = re.compile(r"(?:^|/)klippy/klippy\.py$")
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")

@dataclasses.dataclass
class Candidate:
    source: str
    confidence: int
    klipper_dir: str
    printer_cfg: str
    config_dir: str
    python: str = ""
    user: str = ""
    pid: int = 0
    service_hint: str = ""
    moonraker_url: str = ""
    moonraker_conf: str = ""
    platform_id: str = "generic"

    def key(self):
        return (real(self.klipper_dir), real(self.printer_cfg))

@dataclasses.dataclass
class ServiceSpec:
    backend: str = "none"
    name: str = ""
    script: str = ""
    service_dir: str = ""
    reason: str = ""

@dataclasses.dataclass
class Plan:
    schema: int
    platform_id: str
    install_user: str
    install_group: str
    install_home: str
    klipper_dir: str
    config_dir: str
    printer_cfg: str
    moonraker_conf: str
    moonraker_url: str
    python: str
    service: ServiceSpec
    source: str
    confidence: int

    def as_dict(self):
        value = dataclasses.asdict(self)
        return value

def real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path

def existing_file(path: str) -> bool:
    try:
        return pathlib.Path(path).is_file()
    except OSError:
        return False

def existing_dir(path: str) -> bool:
    try:
        return pathlib.Path(path).is_dir()
    except OSError:
        return False

def user_from_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return ""

def home_for_user(user: str) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return os.path.expanduser("~")

def group_for_user(user: str) -> str:
    try:
        import grp
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except (KeyError, ImportError):
        return user

def proc_cmdline(pid: int, proc_root: str = "/proc") -> list[str]:
    try:
        raw = pathlib.Path(proc_root, str(pid), "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

def proc_uid(pid: int, proc_root: str = "/proc") -> Optional[int]:
    try:
        for line in pathlib.Path(proc_root, str(pid), "status").read_text(errors="replace").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None

def proc_exe(pid: int, proc_root: str = "/proc") -> str:
    try:
        return os.readlink(os.path.join(proc_root, str(pid), "exe"))
    except OSError:
        return ""

def proc_cwd(pid: int, proc_root: str = "/proc") -> str:
    try:
        return os.readlink(os.path.join(proc_root, str(pid), "cwd"))
    except OSError:
        return ""

def proc_service_hint(pid: int, proc_root: str = "/proc") -> str:
    try:
        text = pathlib.Path(proc_root, str(pid), "cgroup").read_text(errors="replace")
    except OSError:
        return ""
    for match in re.finditer(r"(?:^|/)([A-Za-z0-9_.@-]+\.service)(?:$|/)", text, re.M):
        return match.group(1)
    return ""

def _resolve_process_path(path: str, cwd: str = "") -> str:
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(cwd or os.getcwd(), path))

def parse_klippy_argv(argv: list[str], cwd: str = "") -> Optional[tuple[str, str]]:
    script_index = -1
    script = ""
    for index, token in enumerate(argv):
        if KLIPPY_RE.search(token):
            script_index = index
            script = token
            break
    if script_index < 0:
        return None
    printer_cfg = ""
    skip_next = False
    options_with_value = {
        "-I", "-l", "-a", "-d",
        "--debuginput", "--logfile", "--api-server", "--dictionary",
    }
    for token in argv[script_index + 1:]:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_value:
            skip_next = True
            continue
        if token.startswith("-"):

            continue
        if token.endswith(".cfg"):
            printer_cfg = token
            break
    if not printer_cfg:
        return None
    script_path = _resolve_process_path(script, cwd)
    printer_path = _resolve_process_path(printer_cfg, cwd)
    klipper_dir = str(pathlib.Path(script_path).parent.parent)
    return klipper_dir, printer_path

def validate_moonraker_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("Invalid Moonraker URL: %s" % exc)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise RuntimeError(
            "Moonraker URL must be an HTTP(S) base URL without credentials, query or fragment")
    return raw

def read_moonraker_endpoint(conf_path: str) -> str:
    port = 7125
    route_prefix = ""
    if existing_file(conf_path):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(conf_path, encoding="utf-8")
            if parser.has_option("server", "port"):
                value = parser.getint("server", "port")
                if 1 <= value <= 65535:
                    port = value
            if parser.has_option("server", "route_prefix"):
                route_prefix = parser.get("server", "route_prefix").strip().strip("/")
        except (OSError, ValueError, configparser.Error):
            pass
    suffix = "/" + route_prefix if route_prefix else ""
    return "http://127.0.0.1:%d%s" % (port, suffix)

def find_running_moonraker_conf(config_dir: str, proc_root: str = "/proc") -> str:
    data_root = real(os.path.dirname(config_dir))
    matches: list[tuple[int, str]] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return ""
    for item in entries:
        if not item.isdigit():
            continue
        argv = proc_cmdline(int(item), proc_root)
        if not any("moonraker" in os.path.basename(token).lower() for token in argv):
            continue
        cwd = proc_cwd(int(item), proc_root)
        for index, token in enumerate(argv):
            candidate = ""
            if token in ("-c", "--configfile") and index + 1 < len(argv):
                candidate = argv[index + 1]
            elif token.endswith("moonraker.conf"):
                candidate = token
            if not candidate:
                continue
            candidate = _resolve_process_path(candidate, cwd)
            if not existing_file(candidate):
                continue
            score = 100 if real(candidate).startswith(data_root + os.sep) else 10
            matches.append((score, candidate))
    return sorted(matches, reverse=True)[0][1] if matches else ""

def find_moonraker_for_config(config_dir: str, proc_root: str = "/proc") -> tuple[str, str]:
    running = find_running_moonraker_conf(config_dir, proc_root)
    candidates = [
        running,
        os.path.join(config_dir, "moonraker.conf"),
        os.path.join(os.path.dirname(config_dir), "moonraker.conf"),
        os.path.join(os.path.dirname(config_dir), "config", "moonraker.conf"),
    ]
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if existing_file(path):
            return path, read_moonraker_endpoint(path)
    fallback = os.path.join(config_dir, "moonraker.conf")
    return fallback, "http://127.0.0.1:7125"

def running_candidates(proc_root: str = "/proc") -> list[Candidate]:
    values: list[Candidate] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return values
    for item in entries:
        if not item.isdigit():
            continue
        pid = int(item)
        argv = proc_cmdline(pid, proc_root)
        executable_path = proc_exe(pid, proc_root)
        process_names = [
            os.path.basename(executable_path or '').lower(),
            os.path.basename(argv[0]).lower() if argv else '',
        ]
        if not any(name.startswith('python') or name.startswith('pypy')
                   for name in process_names):

            continue
        parsed = parse_klippy_argv(argv, proc_cwd(pid, proc_root))
        if not parsed:
            continue
        klipper_dir, printer_cfg = parsed
        config_dir = os.path.dirname(printer_cfg)
        uid = proc_uid(pid, proc_root)
        user = user_from_uid(uid) if uid is not None else ""
        python = executable_path
        if not python and argv:
            python = argv[0]
        moonraker_conf, moonraker_url = find_moonraker_for_config(config_dir, proc_root)
        values.append(Candidate(
            source="running-klippy",
            confidence=100,
            klipper_dir=klipper_dir,
            printer_cfg=printer_cfg,
            config_dir=config_dir,
            python=python,
            user=user,
            pid=pid,
            service_hint=proc_service_hint(pid, proc_root),
            moonraker_url=moonraker_url,
            moonraker_conf=moonraker_conf,
            platform_id=detect_platform_id(klipper_dir, config_dir),
        ))
    return values

def detect_platform_id(klipper_dir: str, config_dir: str) -> str:
    kd, cd = real(klipper_dir), real(config_dir)
    if kd == "/home/lava/klipper" or cd == "/home/lava/printer_data/config":
        return "snapmaker_u1"
    if kd == "/usr/share/klipper" and cd == "/usr/data/printer_data/config":
        return "creality_k1_family"
    if kd == "/data/klipper" and cd == "/usr/share/printer_data/config":
        return "flyos_fast"
    if kd.startswith("/home/mks/"):
        return "makerbase_qidi_kingroon"
    if kd.startswith("/home/pi/"):
        return "raspberry_pi_klipper"
    return "generic_klipper_linux"

def static_candidates() -> list[Candidate]:
    layouts = [
        ("snapmaker-u1", 95, "/home/lava/klipper", "/home/lava/printer_data/config/printer.cfg", "lava"),
        ("creality-k1", 92, "/usr/share/klipper", "/usr/data/printer_data/config/printer.cfg", "root"),
        ("flyos-fast", 92, "/data/klipper", "/usr/share/printer_data/config/printer.cfg", "root"),
        ("pi", 80, "/home/pi/klipper", "/home/pi/printer_data/config/printer.cfg", "pi"),
        ("mks", 80, "/home/mks/klipper", "/home/mks/printer_data/config/printer.cfg", "mks"),
        ("root", 72, "/root/klipper", "/root/printer_data/config/printer.cfg", "root"),
    ]
    values: list[Candidate] = []
    for source, confidence, kd, pc, user in layouts:
        if existing_file(os.path.join(kd, "klippy", "klippy.py")) and existing_file(pc):
            config_dir = os.path.dirname(pc)
            moonraker_conf, moonraker_url = find_moonraker_for_config(config_dir)
            values.append(Candidate(
                source="known-layout:%s" % source,
                confidence=confidence,
                klipper_dir=kd,
                printer_cfg=pc,
                config_dir=config_dir,
                python=find_python(kd, config_dir, user),
                user=user,
                moonraker_conf=moonraker_conf,
                moonraker_url=moonraker_url,
                platform_id=detect_platform_id(kd, config_dir),
            ))
    return values

def bounded_glob(patterns: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            result.append(path)
            if len(result) >= MAX_SCAN_RESULTS:
                return result
    return result

def scan_candidates() -> list[Candidate]:
    klippy_files = bounded_glob([
        "/home/*/klipper/klippy/klippy.py",
        "/home/*/*/klippy/klippy.py",
        "/root/klipper/klippy/klippy.py",
        "/opt/*/klippy/klippy.py",
        "/opt/*/klipper/klippy/klippy.py",
        "/usr/share/klipper/klippy/klippy.py",
        "/usr/data/*/klippy/klippy.py",
        "/data/klipper/klippy/klippy.py",
        "/data/*/klippy/klippy.py",
        "/userdata/*/klippy/klippy.py",
    ])
    printer_cfgs = bounded_glob([
        "/home/*/printer_data/config/printer.cfg",
        "/home/*/klipper_config/printer.cfg",
        "/home/*/printer.cfg",
        "/home/*/*/config/printer.cfg",
        "/root/printer_data/config/printer.cfg",
        "/root/klipper_config/printer.cfg",
        "/usr/data/printer_data/config/printer.cfg",
        "/usr/share/printer_data/config/printer.cfg",
        "/data/printer_data/config/printer.cfg",
        "/data/*/config/printer.cfg",
        "/userdata/*/config/printer.cfg",
        "/userdata/*/printer_data/config/printer.cfg",
    ])
    values: list[Candidate] = []
    for klippy in klippy_files:
        kd = str(pathlib.Path(klippy).parent.parent)
        try:
            kowner = user_from_uid(os.stat(klippy).st_uid)
        except OSError:
            kowner = ""
        ranked = []
        for pc in printer_cfgs:
            score = 45
            try:
                powner = user_from_uid(os.stat(pc).st_uid)
            except OSError:
                powner = ""
            if kowner and powner and kowner == powner:
                score += 20
            if kd.startswith("/home/") and pc.startswith(os.path.dirname(kd) + "/"):
                score += 20
            if (kd, os.path.dirname(pc)) in (
                    ("/usr/share/klipper", "/usr/data/printer_data/config"),
                    ("/data/klipper", "/usr/share/printer_data/config")):
                score += 30
            ranked.append((score, pc, powner or kowner))
        for score, pc, user in sorted(ranked, reverse=True)[:2]:
            config_dir = os.path.dirname(pc)
            moonraker_conf, moonraker_url = find_moonraker_for_config(config_dir)
            values.append(Candidate(
                source="bounded-scan",
                confidence=min(score, 89),
                klipper_dir=kd,
                printer_cfg=pc,
                config_dir=config_dir,
                python=find_python(kd, config_dir, user),
                user=user,
                moonraker_conf=moonraker_conf,
                moonraker_url=moonraker_url,
                platform_id=detect_platform_id(kd, config_dir),
            ))
    return values

def find_python(klipper_dir: str, config_dir: str, user: str = "") -> str:
    home = home_for_user(user) if user else ""
    candidates = [
        os.path.join(home, "klippy-env", "bin", "python") if home else "",
        os.path.join(os.path.dirname(klipper_dir), "klippy-env", "bin", "python"),
        os.path.join(os.path.dirname(config_dir), "..", "klippy-env", "bin", "python"),
        "/usr/data/klippy-env/bin/python",
        "/usr/share/klippy-env/bin/python",
        "/usr/bin/python3",
        "/usr/local/bin/python3",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return real(candidate)
    return shutil_which("python3") or "python3"

TRUSTED_SYSTEM_PATH = (
    "/usr/sbin", "/usr/bin", "/sbin", "/bin",
    "/usr/local/sbin", "/usr/local/bin")

def _root_owned_nonwritable(path: str) -> bool:
    resolved = os.path.realpath(path)
    current = resolved
    while True:
        try:
            info = os.stat(current)
        except OSError:
            return False
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent

def shutil_which(name: str) -> str:

    for directory in TRUSTED_SYSTEM_PATH:
        candidate = os.path.join(directory, name)
        if (os.path.isfile(candidate) and os.access(candidate, os.X_OK) and
                _root_owned_nonwritable(candidate)):
            return os.path.realpath(candidate)
    return ""

def dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    best: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        if not existing_file(os.path.join(candidate.klipper_dir, "klippy", "klippy.py")):
            continue
        if not existing_file(candidate.printer_cfg):
            continue
        key = candidate.key()
        old = best.get(key)
        if old is None or candidate.confidence > old.confidence:
            best[key] = candidate
    return sorted(best.values(), key=lambda item: (-item.confidence, item.printer_cfg))

def choose_candidate(candidates: list[Candidate], non_interactive: bool) -> Candidate:
    if not candidates:
        raise RuntimeError(
            "No Klipper installation was found. Pass --klipper-dir and --config-dir/--printer-cfg explicitly.")
    if len(candidates) == 1:
        return candidates[0]
    top = candidates[0]
    second = candidates[1]
    if top.confidence >= 95 and top.confidence - second.confidence >= 10:
        return top
    if non_interactive or not sys.stdin.isatty():
        lines = ["Several Klipper installations are plausible; refusing to guess:"]
        for index, item in enumerate(candidates[:10], 1):
            lines.append("  %d. %s | %s | score=%d" % (
                index, item.klipper_dir, item.printer_cfg, item.confidence))
        lines.append("Pass --klipper-dir and --config-dir/--printer-cfg, then rerun.")
        raise RuntimeError("\n".join(lines))
    print("\nDetected Klipper installations:")
    for index, item in enumerate(candidates[:10], 1):
        print("  %d) %-18s %s" % (index, item.platform_id, item.printer_cfg))
        print("     Klipper: %s (source=%s score=%d)" % (
            item.klipper_dir, item.source, item.confidence))
    while True:
        answer = input("Choose installation [1]: ").strip() or "1"
        try:
            selected = int(answer)
        except ValueError:
            continue
        if 1 <= selected <= min(10, len(candidates)):
            return candidates[selected - 1]

def validate_service_name(value: str) -> str:
    value = value.strip()
    if value.endswith(".service"):
        value = value[:-8]
    if not SERVICE_NAME_RE.fullmatch(value) or value.startswith("-"):
        return ""
    return value

def executable(path: str) -> bool:
    return bool(path and os.path.isfile(path) and os.access(path, os.X_OK))

def detect_service(candidate: Candidate, override_backend: str = "", override_name: str = "") -> ServiceSpec:
    if override_backend:
        raw_name = override_name or "klipper"
        if override_backend in ("sysv", "runit", "s6") and raw_name.startswith("/"):
            name = raw_name
        else:
            name = validate_service_name(raw_name)
            if not name and override_backend not in ("none", "process-only"):
                raise RuntimeError("Invalid service name: %s" % raw_name)
        return service_for_backend(override_backend, name, candidate)

    hinted = validate_service_name(candidate.service_hint)
    systemctl = shutil_which("systemctl")
    if systemctl and os.path.isdir("/run/systemd/system"):
        names = [hinted] if hinted else []
        names += ["klipper", "klipper-1", "klipper_1"]
        try:
            listed = subprocess.run(
                [systemctl, "list-units", "--all", "--type=service", "--plain",
                 "--no-legend", "--no-pager", "klipper*.service"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
            for line in listed.stdout.splitlines():
                unit = line.split(None, 1)[0] if line.strip() else ""
                if unit.endswith(".service"):
                    names.append(unit[:-8])
        except (OSError, subprocess.TimeoutExpired):
            pass
        loaded = []
        seen_names = set()
        for name in names:
            name = validate_service_name(name)
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            try:
                result = subprocess.run(
                    [systemctl, "show", "%s.service" % name,
                     "--property=LoadState", "--property=MainPID"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            fields = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    fields[key.strip()] = value.strip()
            if result.returncode != 0 or fields.get("LoadState") != "loaded":
                continue
            try:
                main_pid = int(fields.get("MainPID", "0"))
            except ValueError:
                main_pid = 0
            if candidate.pid and main_pid == candidate.pid:
                return ServiceSpec(backend="systemd", name=name, reason="systemd MainPID match")
            loaded.append(name)
        if hinted and hinted in loaded:
            return ServiceSpec(backend="systemd", name=hinted, reason="systemd cgroup hint")
        if len(loaded) == 1:
            return ServiceSpec(backend="systemd", name=loaded[0], reason="single systemd Klipper unit")

    init_scripts = []
    for path in glob.glob("/etc/init.d/*"):
        base = os.path.basename(path).lower()
        if "klipper" not in base or not executable(path):
            continue
        text = safe_head(path, 16384)
        score = 10
        if candidate.klipper_dir and candidate.klipper_dir in text:
            score += 100
        if candidate.printer_cfg and candidate.printer_cfg in text:
            score += 120
        if path in ("/etc/init.d/S60klipper", "/etc/init.d/S55klipper_service", "/etc/init.d/klipper"):
            score += 20
        init_scripts.append((score, path, text))
    if init_scripts:
        init_scripts.sort(key=lambda item: (-item[0], item[1]))
        best_score, path, text = init_scripts[0]
        if len(init_scripts) == 1 or best_score > init_scripts[1][0]:
            backend = "openrc" if executable(shutil_which("rc-service")) and "openrc-run" in text else "sysv"
            return ServiceSpec(backend=backend, name=os.path.basename(path), script=path,
                               reason="matched init script")

    supervisorctl = shutil_which("supervisorctl")
    if supervisorctl:
        try:
            result = subprocess.run([supervisorctl, "status"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, timeout=5)
            names = []
            for line in result.stdout.splitlines():
                raw_name = line.split(None, 1)[0] if line.strip() else ""
                name = validate_service_name(raw_name)
                if name and "klipper" in name.lower():
                    names.append(name)
            if candidate.pid:
                for name in names:
                    probe = subprocess.run([supervisorctl, "pid", name], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, timeout=5)
                    if probe.stdout.strip() == str(candidate.pid):
                        return ServiceSpec(backend="supervisor", name=name,
                                           reason="supervisord pid match")
            if len(names) == 1:
                return ServiceSpec(backend="supervisor", name=names[0],
                                   reason="single supervisord Klipper program")

        except (OSError, subprocess.TimeoutExpired):
            pass

    sv = shutil_which("sv")
    if sv:
        entries = []
        for root in ("/etc/service", "/var/service", "/service"):
            if os.path.isdir(root):
                entries.extend(entry for entry in glob.glob(os.path.join(root, "*klipper*"))
                               if os.path.isdir(entry))
        scored = []
        for entry in sorted(set(entries)):
            text = safe_head(os.path.join(entry, "run"), 16384)
            score = int(bool(candidate.klipper_dir and candidate.klipper_dir in text)) * 100
            score += int(bool(candidate.printer_cfg and candidate.printer_cfg in text)) * 120
            scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            entry = scored[0][1]
            return ServiceSpec(backend="runit", name=os.path.basename(entry),
                               service_dir=entry, reason="matched runit service")

    s6svc = shutil_which("s6-svc")
    if s6svc:
        entries = []
        for root in ("/run/service", "/var/run/s6/services", "/service"):
            if os.path.isdir(root):
                entries.extend(entry for entry in glob.glob(os.path.join(root, "*klipper*"))
                               if os.path.isdir(entry))
        scored = []
        for entry in sorted(set(entries)):
            text = safe_head(os.path.join(entry, "run"), 16384)
            score = int(bool(candidate.klipper_dir and candidate.klipper_dir in text)) * 100
            score += int(bool(candidate.printer_cfg and candidate.printer_cfg in text)) * 120
            scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            entry = scored[0][1]
            return ServiceSpec(backend="s6", name=os.path.basename(entry),
                               service_dir=entry, reason="matched s6 service")

    if candidate.pid:
        return ServiceSpec(backend="process-only", name="klipper", reason="Klippy process found but no service manager")
    return ServiceSpec(backend="none", reason="Klipper service not detected")

def safe_head(path: str, maximum: int = 2048) -> str:
    try:
        return pathlib.Path(path).read_text(errors="replace")[:maximum]
    except OSError:
        return ""

def service_for_backend(backend: str, name: str, candidate: Candidate) -> ServiceSpec:
    if backend == "systemd":
        return ServiceSpec(backend=backend, name=name or "klipper", reason="explicit override")
    if backend in ("sysv", "openrc"):
        path = name if name.startswith("/") else "/etc/init.d/%s" % name
        if not os.path.isabs(path) or ".." in pathlib.Path(path).parts:
            raise RuntimeError("Invalid init service path: %s" % path)
        return ServiceSpec(backend=backend, name=os.path.basename(path), script=path,
                           reason="explicit override")
    if backend == "runit":
        if not name.startswith("/"):
            raise RuntimeError(
                "--service-name must be the absolute runit service directory")
        return ServiceSpec(backend=backend,
                           name=os.path.basename(name.rstrip("/")),
                           service_dir=name,
                           reason="explicit override")
    if backend == "s6":
        if not name.startswith("/"):
            raise RuntimeError(
                "--service-name must be the absolute s6 service directory")
        return ServiceSpec(backend=backend,
                           name=os.path.basename(name.rstrip("/")),
                           service_dir=name,
                           reason="explicit override")
    if backend == "supervisor":
        return ServiceSpec(backend=backend, name=name or "klipper", reason="explicit override")
    if backend in ("none", "process-only"):
        return ServiceSpec(backend=backend, name=name, reason="explicit override")
    raise RuntimeError("Unsupported service backend: %s" % backend)

def apply_overrides(candidate: Candidate, args) -> Candidate:
    klipper_dir = args.klipper_dir or candidate.klipper_dir
    config_dir = args.config_dir or candidate.config_dir
    printer_cfg = args.printer_cfg or candidate.printer_cfg
    if config_dir and not printer_cfg:
        printer_cfg = os.path.join(config_dir, "printer.cfg")
    if printer_cfg and not config_dir:
        config_dir = os.path.dirname(printer_cfg)
    user = args.user or candidate.user
    if not user:
        try:
            user = user_from_uid(os.stat(config_dir).st_uid)
        except OSError:
            user = os.environ.get("SUDO_USER") or user_from_uid(os.getuid()) or "root"
    python = args.python or candidate.python or find_python(klipper_dir, config_dir, user)
    moonraker_conf = args.moonraker_conf or candidate.moonraker_conf
    moonraker_url = args.moonraker_url or candidate.moonraker_url
    if not moonraker_conf or not moonraker_url:
        auto_conf, auto_url = find_moonraker_for_config(config_dir)
        moonraker_conf = moonraker_conf or auto_conf
        moonraker_url = moonraker_url or auto_url
    return Candidate(
        source=candidate.source,
        confidence=candidate.confidence,
        klipper_dir=real(klipper_dir),
        printer_cfg=os.path.abspath(printer_cfg),
        config_dir=os.path.abspath(config_dir),
        python=real(python) if os.path.exists(python) else python,
        user=user,
        pid=candidate.pid,
        service_hint=candidate.service_hint,
        moonraker_url=moonraker_url,
        moonraker_conf=real(moonraker_conf),
        platform_id=detect_platform_id(klipper_dir, config_dir),
    )

def explicit_candidate(args) -> Optional[Candidate]:
    kd = args.klipper_dir
    cd = args.config_dir
    pc = args.printer_cfg
    if not (kd or cd or pc):
        return None
    if not kd:
        raise RuntimeError("--klipper-dir is required when using explicit path overrides")
    if not pc and cd:
        pc = os.path.join(cd, "printer.cfg")
    if not cd and pc:
        cd = os.path.dirname(pc)
    if not cd or not pc:
        raise RuntimeError("--config-dir or --printer-cfg is required with --klipper-dir")
    return Candidate("explicit", 1000, kd, pc, cd)

def validate_candidate(candidate: Candidate):
    klippy = os.path.join(candidate.klipper_dir, "klippy", "klippy.py")
    extras = os.path.join(candidate.klipper_dir, "klippy", "extras")
    if not existing_file(klippy):
        raise RuntimeError("Klippy entry point not found: %s" % klippy)
    if not existing_dir(extras):
        raise RuntimeError("Klipper extras directory not found: %s" % extras)
    if not existing_dir(candidate.config_dir):
        raise RuntimeError("Klipper config directory not found: %s" % candidate.config_dir)
    if not existing_file(candidate.printer_cfg):
        raise RuntimeError("printer.cfg not found: %s" % candidate.printer_cfg)
    if not candidate.python or not executable(candidate.python):
        raise RuntimeError("Klipper Python executable not found: %s" % candidate.python)
    try:
        pwd.getpwnam(candidate.user)
    except KeyError:
        raise RuntimeError("Klipper user does not exist: %s" % candidate.user)
    candidate.moonraker_url = validate_moonraker_url(candidate.moonraker_url)

def build_plan(args) -> Plan:
    explicit = explicit_candidate(args)
    if explicit:
        selected = explicit
    else:
        candidates = dedupe(running_candidates(args.proc_root) + static_candidates() + scan_candidates())
        selected = choose_candidate(candidates, args.non_interactive)
    selected = apply_overrides(selected, args)
    validate_candidate(selected)
    service = detect_service(selected, args.service_backend, args.service_name)
    user = selected.user
    home = home_for_user(user)
    return Plan(
        schema=SCHEMA,
        platform_id=selected.platform_id,
        install_user=user,
        install_group=group_for_user(user),
        install_home=home,
        klipper_dir=selected.klipper_dir,
        config_dir=selected.config_dir,
        printer_cfg=selected.printer_cfg,
        moonraker_conf=selected.moonraker_conf,
        moonraker_url=selected.moonraker_url,
        python=selected.python,
        service=service,
        source=selected.source,
        confidence=selected.confidence,
    )

def print_shell(plan: Plan):
    values = {
        "BMCU_PLATFORM_ID": plan.platform_id,
        "BMCU_TARGET_USER": plan.install_user,
        "BMCU_TARGET_GROUP": plan.install_group,
        "BMCU_TARGET_HOME": plan.install_home,
        "KLIPPER_DIR": plan.klipper_dir,
        "CONFIG_DIR": plan.config_dir,
        "PRINTER_CFG": plan.printer_cfg,
        "MOONRAKER_CONF": plan.moonraker_conf,
        "BMCU_MOONRAKER_URL": plan.moonraker_url,
        "BMCU_PYTHON": plan.python,
        "BMCU_KLIPPER_SERVICE_BACKEND": plan.service.backend,
        "BMCU_KLIPPER_SERVICE_NAME": plan.service.name,
        "BMCU_KLIPPER_SERVICE_SCRIPT": plan.service.script,
        "BMCU_KLIPPER_SERVICE_DIR": plan.service.service_dir,
        "BMCU_DISCOVERY_SOURCE": plan.source,
        "BMCU_DISCOVERY_CONFIDENCE": str(plan.confidence),
    }
    for key, value in values.items():
        print("%s=%s" % (key, shlex.quote(str(value))))

def query_printer_idle(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 0.2 <= timeout <= 30.0:
            raise RuntimeError("Moonraker timeout must be within 0.2..30 seconds")
        base = validate_moonraker_url(url)
    except (TypeError, ValueError, RuntimeError) as exc:
        return False, "Moonraker query blocked: %s" % exc
    endpoint = base + "/printer/objects/query?print_stats"
    request = urllib.request.Request(endpoint, headers={"User-Agent": "BMCU-Installer"})
    try:

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Moonraker response is too large")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Moonraker response is not a JSON object")
    except Exception as exc:
        return False, "Moonraker query failed: %s" % exc
    state = (((data.get("result") or {}).get("status") or {}).get("print_stats") or {}).get("state", "")
    normalized = str(state).strip().lower()
    return normalized in {"standby", "complete", "cancelled", "error"}, str(state)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bmcu_platform.py")
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--format", choices=("json", "shell"), default="json")
    discover.add_argument("--non-interactive", action="store_true")
    discover.add_argument("--klipper-dir", default="")
    discover.add_argument("--config-dir", default="")
    discover.add_argument("--printer-cfg", default="")
    discover.add_argument("--moonraker-conf", default="")
    discover.add_argument("--moonraker-url", default="")
    discover.add_argument("--python", default="")
    discover.add_argument("--user", default="")
    discover.add_argument("--service-backend", choices=("", "systemd", "sysv", "openrc", "supervisor", "runit", "s6", "none", "process-only"), default="")
    discover.add_argument("--service-name", default="")
    discover.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)

    idle = sub.add_parser("printer-idle")
    idle.add_argument("--moonraker-url", default="http://127.0.0.1:7125")
    idle.add_argument("--timeout", type=float, default=3.0)
    return parser

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            plan = build_plan(args)
            if args.format == "shell":
                print_shell(plan)
            else:
                print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
            return 0
        if args.command == "printer-idle":
            idle, state = query_printer_idle(args.moonraker_url, args.timeout)
            print(json.dumps({"idle": idle, "state": state}, sort_keys=True))
            return 0 if idle else 1
    except RuntimeError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
