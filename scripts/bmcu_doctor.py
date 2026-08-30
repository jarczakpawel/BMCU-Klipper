#!/usr/bin/env python3

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from bmcu_vendor import ensure_vendor_path
ensure_vendor_path(__file__)

import argparse
import json
import os
import re
import stat
import sys
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apply_detected_devices import ConfigError, parse_detected
from safe_file_ops import SafeFileError, read_regular_text
from safe_printer_cfg_include import (INCLUDES, ManagedBlockError,
                                      find_managed_block, find_save_marker)

MAX_LINK_TARGET = 4096

class Report:
    def __init__(self):
        self.items = []

    def add(self, level, code, message):
        self.items.append({
            'level': str(level).upper(),
            'code': str(code),
            'message': str(message),
        })

    def ok(self, code, message):
        self.add('OK', code, message)

    def warn(self, code, message):
        self.add('WARN', code, message)

    def error(self, code, message):
        self.add('ERROR', code, message)

    @property
    def errors(self):
        return sum(item['level'] == 'ERROR' for item in self.items)

    @property
    def warnings(self):
        return sum(item['level'] == 'WARN' for item in self.items)

def _safe_link_target(path):
    path = Path(path)
    try:
        info = os.lstat(str(path))
    except OSError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        return None
    value = os.readlink(str(path))
    if len(value.encode('utf-8', 'surrogateescape')) > MAX_LINK_TARGET:
        raise ValueError('symlink target is too long: %s' % path)
    return (path.parent / value).resolve()

def check_printer_cfg(report, printer_cfg):
    try:
        text, _info = read_regular_text(printer_cfg)
    except SafeFileError as exc:
        report.error('printer_cfg', exc)
        return
    lines = text.splitlines()
    try:
        start, stop, _generation = find_managed_block(lines)
    except ManagedBlockError as exc:
        report.error('include_block', exc)
        return
    if start < 0:
        report.error(
            'include_block',
            'printer.cfg must contain one complete BMCU managed include block')
        return
    save = find_save_marker(lines)
    if save >= 0 and stop > save:
        report.error('include_order', 'BMCU include block must be before SAVE_CONFIG')
        return
    config_root = Path(printer_cfg).parent
    missing = []
    unsafe = []
    for directive in INCLUDES:
        relative = directive[len('[include '):-1].strip()
        target = config_root / relative
        if os.path.islink(str(target)):
            unsafe.append(str(target))
        elif not target.is_file():
            missing.append(str(target))
    for target in unsafe:
        report.error('include_target', 'managed include target is a symlink: %s' % target)
    for target in missing:
        report.error('include_target', 'managed include target is missing: %s' % target)
    if unsafe or missing:
        return
    report.ok('printer_cfg', 'managed include block and all targets are valid')

def check_klipper_links(report, klipper_dir):
    extras = Path(klipper_dir) / 'klippy' / 'extras'
    expected = {
        extras / 'bmcu.py': ROOT / 'klippy' / 'extras' / 'bmcu.py',
        extras / 'bmcu_core': ROOT / 'klippy' / 'extras' / 'bmcu_core',
        extras / 'bmcu_panel.py': ROOT / 'klippy' / 'extras' / 'bmcu_panel.py',
    }
    for path, target in expected.items():
        try:
            actual = _safe_link_target(path)
        except ValueError as exc:
            report.error('klipper_link', exc)
            continue
        if actual is None:
            report.error('klipper_link', 'missing installer-managed symlink: %s' % path)
        elif actual != target.resolve():
            report.error('klipper_link', 'wrong symlink target: %s -> %s' % (path, actual))
        else:
            report.ok('klipper_link', '%s points to packaged source' % path.name)

def check_bmcu_config(report, config_path):
    try:
        _tool_count, baud, devices = parse_detected(config_path)
    except (ConfigError, OSError) as exc:
        report.error('bmcu_cfg', exc)
        return
    report.ok('bmcu_cfg', 'configuration parses correctly at %d baud' % baud)
    if not devices:
        report.warn('devices', 'no BMCU device is configured yet')
        return
    for record in devices:
        port = record['port']
        if not os.path.exists(port):
            report.warn('port', '%s is currently missing: %s' % (record['name'], port))
        elif not port.startswith('/dev/serial/by-'):
            report.warn('port', '%s uses a volatile serial path: %s' % (record['name'], port))
        else:
            report.ok('port', '%s uses stable serial path' % record['name'])
        if record.get('uid') == '-':
            report.warn('uid', '%s has no pinned hardware UID' % record['name'])
        else:
            report.ok('uid', '%s has pinned UID %s' % (record['name'], record['uid']))

def _realpath(value):
    try:
        return os.path.realpath(os.path.expanduser(str(value)))
    except Exception:
        return str(value)

def _paths_equivalent(left, right):
    return bool(left and right and _realpath(left) == _realpath(right))

def _managed_block(text, begin, end):
    if text.count(begin) != 1 or text.count(end) != 1:
        return None, 'managed block is missing or duplicated'
    start = text.find(begin)
    stop = text.find(end, start + len(begin))
    if start < 0 or stop < 0 or stop <= start:
        return None, 'managed block markers are out of order'
    return text[start + len(begin):stop], ''

def _shell_statements(text):
    pending = ''
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        continued = line.endswith('\\')
        if continued:
            line = line[:-1].rstrip()
        pending = (pending + ' ' + line).strip() if pending else line
        if not continued:
            yield pending
            pending = ''
    if pending:
        yield pending

def _bootstrap_invocations(text):
    invocations = []
    for statement in _shell_statements(text):
        try:
            tokens = shlex.split(statement, comments=False, posix=True)
        except ValueError:
            continue
        for index, token in enumerate(tokens):
            if os.path.basename(token) != 'bmcu_host_bootstrap.py':
                continue
            metadata = ''
            for offset in range(index + 1, len(tokens)):
                value = tokens[offset]
                if value == '--metadata' and offset + 1 < len(tokens):
                    metadata = tokens[offset + 1]
                    break
                if value.startswith('--metadata='):
                    metadata = value.split('=', 1)[1]
                    break
            invocations.append({
                'script': token,
                'metadata': metadata,
                'repair': '--repair' in tokens[index + 1:],
                'tokens': tokens,
            })
    return invocations

def _validate_bootstrap_text(text, expected_script, expected_metadata,
                             begin=None, end=None, reject_hooks=False):
    scope = str(text)
    if begin is not None and end is not None:
        scope, reason = _managed_block(scope, begin, end)
        if scope is None:
            return False, reason
    if reject_hooks and '/etc/hooks/klipper.d' in scope:
        return False, 'managed bootstrap still depends on volatile /etc/hooks'
    invocations = _bootstrap_invocations(scope)
    if len(invocations) != 1:
        return False, 'expected exactly one direct bootstrap invocation'
    invocation = invocations[0]
    if not invocation['repair']:
        return False, 'bootstrap invocation is missing --repair'
    if not _paths_equivalent(invocation['script'], expected_script):
        return False, 'bootstrap script path does not resolve to packaged runtime'
    if not _paths_equivalent(invocation['metadata'], expected_metadata):
        return False, 'bootstrap metadata path does not resolve to packaged runtime'
    return True, ''

def _load_installation_metadata(report):
    metadata_path = ROOT / 'INSTALLATION.json'
    try:
        metadata = json.loads(metadata_path.read_text())
    except FileNotFoundError:
        report.error('installation', 'INSTALLATION.json is missing')
        return None
    except Exception as exc:
        report.error('installation', 'INSTALLATION.json is invalid: %s' % exc)
        return None
    if not isinstance(metadata, dict):
        report.error('installation', 'INSTALLATION.json must contain an object')
        return None
    return metadata

def check_boot_stamp(report, metadata):
    hook_type = str((metadata or {}).get('boot_hook_type') or '')
    if hook_type not in ('snapmaker-inline-bootstrap', 'systemd-dropin'):
        report.ok(
            'boot_stamp',
            'current platform does not require a managed pre-start repair hook')
        return
    stamp = ROOT.parent / 'HOST_BOOT.json'
    try:
        payload = json.loads(stamp.read_text())
    except FileNotFoundError:
        report.error('boot_stamp', 'HOST_BOOT.json is missing; managed pre-start repair has not run')
        return
    except Exception as exc:
        report.error('boot_stamp', 'HOST_BOOT.json is invalid: %s' % exc)
        return
    try:
        current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        current_boot = ''
    recorded = str(payload.get('boot_id') or '')
    if current_boot and recorded == current_boot:
        report.ok('boot_stamp', 'managed module repair ran during the current OS boot')
    elif current_boot:
        report.error('boot_stamp', 'managed module repair did not run during the current OS boot')
    else:
        report.warn('boot_stamp', 'OS boot id is unavailable; stamp freshness cannot be verified')

def check_snapmaker_boot_integration(report, metadata):
    if (metadata or {}).get('platform') != 'snapmaker_u1':
        return
    marker = Path('/oem/.debug')
    if marker.is_file():
        report.ok('snapmaker_persistence', '/oem/.debug enables persistent system changes')
    else:
        report.error('snapmaker_persistence', '/oem/.debug is missing')
    service = Path('/etc/init.d/S60klipper')
    try:
        text = service.read_text(errors='replace')
    except OSError as exc:
        report.error('snapmaker_service', 'cannot read %s: %s' % (service, exc))
        return
    begin = '# BEGIN BMCU-KLIPPER SERVICE-HOOKS'
    end = '# END BMCU-KLIPPER SERVICE-HOOKS'
    call_marker = '# BMCU-KLIPPER PREPARE IMMEDIATELY BEFORE KLIPPER PRIVILEGE DROP'
    call = 'bmcu_prepare_klipper_start || exit 1'
    scope, reason = _managed_block(text, begin, end)
    valid = scope is not None
    if valid and '/etc/hooks/klipper.d' in scope:
        valid, reason = False, 'managed bootstrap still depends on volatile /etc/hooks'
    if valid and scope.count('/oem/bmcu-klipper/run-host-bootstrap.py') != 1:
        valid, reason = False, 'expected one root-owned U1 bootstrap runner invocation'
    if valid and 'bmcu_prepare_klipper_start()' not in scope:
        valid, reason = False, 'managed launch preparation function is missing'
    if valid and 'start|restart|reload)' in scope:
        valid, reason = False, 'serial repair still runs before the U1 hardware power cycle'
    if valid:
        lines = text.splitlines()
        launches = [
            index for index, line in enumerate(lines)
            if re.match(r'^\s*start-stop-daemon\s+-S(?:\s|$)', line)
        ]
        if not launches:
            valid, reason = False, 'no Klipper start-stop-daemon launch was found'
        else:
            for index in launches:
                if index < 2:
                    valid, reason = False, 'managed pre-launch call is missing'
                    break
                indent = re.match(r'^(\s*)', lines[index]).group(1)
                if (lines[index - 2] != indent + call_marker or
                        lines[index - 1] != indent + call):
                    valid, reason = False, 'managed repair is not immediately before every Klipper launch'
                    break
    if valid:
        power = next((
            index for index, line in enumerate(text.splitlines())
            if re.match(r'^\s*["\']?\$LAVA_IO["\']?\s+set\s+.*(?:MAIN_MCU_POWER|HEAD_MCU_POWER)=1', line)
        ), None)
        first_call = next((
            index for index, line in enumerate(text.splitlines())
            if line.strip() == call_marker
        ), None)
        if power is not None and (first_call is None or first_call <= power):
            valid, reason = False, 'managed serial repair runs before U1 hardware power-up'
    if valid:
        report.ok(
            'snapmaker_service',
            'S60klipper repairs CH340 access immediately before each Klipper launch')
    else:
        report.error(
            'snapmaker_service',
            'S60klipper BMCU launch integration is invalid: %s' % reason)
    printer_cfg = Path(str(metadata.get('printer_cfg') or ''))
    try:
        cfg_text = printer_cfg.read_text(errors='replace')
    except OSError as exc:
        report.error('snapmaker_cfg_persistence', 'cannot read printer.cfg: %s' % exc)
    else:
        includes = metadata.get('includes') or []
        if (cfg_text.count(str(metadata.get('include_begin') or '')) == 1 and
                cfg_text.count(str(metadata.get('include_end') or '')) == 1 and
                all(str(item) in cfg_text for item in includes)):
            report.ok('snapmaker_cfg_persistence', 'managed BMCU include block is present')
        else:
            report.error('snapmaker_cfg_persistence', 'managed BMCU include block is missing or incomplete')

def check_systemd_boot_integration(report, metadata):
    if str((metadata or {}).get('boot_hook_type') or '') != 'systemd-dropin':
        return
    hook = Path(str(metadata.get('boot_hook') or ''))
    try:
        text = hook.read_text(errors='replace')
    except OSError as exc:
        report.error('service_bootstrap', 'cannot read managed systemd drop-in %s: %s' % (hook, exc))
        return
    valid, reason = _validate_bootstrap_text(
        text, ROOT / 'scripts' / 'bmcu_host_bootstrap.py',
        ROOT / 'INSTALLATION.json')
    if valid:
        report.ok('service_bootstrap', 'systemd contains one managed BMCU pre-start bootstrap')
    else:
        report.error('service_bootstrap', 'managed systemd bootstrap is invalid: %s' % reason)

def run(args):
    report = Report()
    if sys.version_info < (3, 7):
        report.error('python', 'Python 3.7 or newer is required')
    else:
        report.ok('python', 'Python %d.%d is supported' % sys.version_info[:2])
    try:
        import serial
        report.ok('pyserial', 'pyserial is available')
    except Exception:
        report.error('pyserial', 'pyserial is not installed for this Python interpreter')

    check_printer_cfg(report, Path(args.printer_cfg))
    check_klipper_links(report, Path(args.klipper_dir))
    check_bmcu_config(report, Path(args.config_dir) / 'bmcu' / 'bmcu.cfg')
    metadata = _load_installation_metadata(report)
    if metadata is not None:
        check_boot_stamp(report, metadata)
        check_snapmaker_boot_integration(report, metadata)
        check_systemd_boot_integration(report, metadata)
    return report

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--klipper-dir', default='~/klipper')
    parser.add_argument('--config-dir', default='~/printer_data/config')
    parser.add_argument('--printer-cfg', default='')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--strict', action='store_true',
                        help='Return failure when warnings are present')
    args = parser.parse_args(argv)
    args.klipper_dir = str(Path(args.klipper_dir).expanduser())
    args.config_dir = str(Path(args.config_dir).expanduser())
    args.printer_cfg = str(Path(args.printer_cfg).expanduser()
                           if args.printer_cfg else Path(args.config_dir) / 'printer.cfg')
    report = run(args)
    result = {
        'ok': report.errors == 0 and (not args.strict or report.warnings == 0),
        'errors': report.errors,
        'warnings': report.warnings,
        'checks': report.items,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    else:
        for item in report.items:
            print('[%-5s] %-14s %s' % (item['level'], item['code'], item['message']))
        print('Summary: %d error(s), %d warning(s)' %
              (report.errors, report.warnings))
    return 0 if result['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
