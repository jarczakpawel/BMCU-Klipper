#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import print_function

import argparse
import fcntl
import glob
import os
import re
import signal
import sys
import time
import traceback
import types

sys.dont_write_bytecode = True

PRODUCT = 'BMCU-Klipper'
VERSION = None
MAX_RELEASE_BYTES = 64 * 1024 * 1024

lifecycle = None
out = print
warn = print


class UninstallError(RuntimeError):
    pass


def load_package_module(name, package):
    path = os.path.join(package, 'scripts', name + '.py')
    with open(path, 'rb') as stream:
        data = stream.read(MAX_RELEASE_BYTES)
    module = types.ModuleType(name)
    module.__file__ = path
    module.__package__ = ''
    sys.modules[name] = module
    exec(compile(data, path, 'exec'), module.__dict__)
    return module


def package_version(package):
    try:
        with open(os.path.join(package, 'version'), 'r') as stream:
            text = stream.read(4096)
    except OSError:
        return 'unknown'
    match = re.search(r'(?m)^\s*package\s*[=:]\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$', text)
    return match.group(1) if match else 'unknown'


def count_package_files(package):
    total = 0
    for _root, dirs, files in os.walk(package):
        dirs[:] = [name for name in dirs if name not in ('.git', '__pycache__', '.pio')]
        total += len([name for name in files if not name.endswith('.pyc')])
    return total


class Target(object):
    def __init__(self):
        self.config_dir = ''
        self.printer_cfg = ''
        self.klipper_dir = ''
        self.service = {}
        self.moonraker = 'http://127.0.0.1:7125'
        self.platform_id = 'generic'
        self.metadata = {}
        self.discovery_note = ''

    @property
    def bmcu_dir(self):
        return os.path.join(self.config_dir, 'bmcu')

    @property
    def data_root(self):
        return os.path.dirname(os.path.realpath(self.config_dir))

    @property
    def extras_dir(self):
        if not self.klipper_dir:
            return ''
        path = os.path.join(self.klipper_dir, 'klippy', 'extras')
        return path if os.path.isdir(path) else ''

    @property
    def is_u1(self):
        return (self.platform_id == 'snapmaker_u1' or
                self.metadata.get('platform') == 'snapmaker_u1')


def _discover(platform, arguments):
    try:
        return platform.build_plan(platform.build_parser().parse_args(
            ['discover', '--format', 'json', '--non-interactive'] + arguments)).as_dict()
    except (RuntimeError, SystemExit) as exc:
        raise UninstallError(str(exc))


def _candidates(platform):
    try:
        return platform.dedupe(platform.running_candidates() +
                               platform.static_candidates() +
                               platform.scan_candidates())
    except Exception:
        return []


def choose_config_dir(args):
    if args.config_dir:
        return os.path.abspath(args.config_dir)
    if args.printer_cfg:
        return os.path.dirname(os.path.abspath(args.printer_cfg))
    installs = lifecycle.find_installations()
    if not installs:
        return ''
    if len(installs) == 1:
        return installs[0][0]
    lines = ['BMCU-Klipper traces were found in several Klipper configurations:']
    for index, (path, traces) in enumerate(installs, 1):
        lines.append('  %d) %s (%s)' % (index, path, ', '.join(traces)))
    if sys.stdin.isatty():
        out('\n'.join(lines))
        while True:
            try:
                answer = input('Remove BMCU-Klipper from which one? [1]: ').strip() or '1'
            except EOFError:
                break
            if answer.isdigit() and 1 <= int(answer) <= len(installs):
                return installs[int(answer) - 1][0]
    lines.append('Run the uninstaller with --config-dir <one of the paths above>.')
    raise UninstallError('\n'.join(lines))


def locate_target(platform, args):
    target = Target()
    config_dir = choose_config_dir(args)
    overrides = []
    for option, value in (('--python', args.python), ('--user', args.user),
                          ('--moonraker-url', args.moonraker_url),
                          ('--moonraker-conf', args.moonraker_conf),
                          ('--service-backend', args.service_backend),
                          ('--service-name', args.service_name)):
        if value:
            overrides.extend([option, value])
    plan = None
    if not config_dir:
        try:
            plan = _discover(platform, overrides)
        except UninstallError:
            return None
        config_dir = plan['config_dir']
    target.config_dir = config_dir
    target.metadata = lifecycle.read_installation_metadata(target.bmcu_dir) or {}
    if plan is None:
        klipper_dir = args.klipper_dir or str(target.metadata.get('klipper_dir') or '')
        if not (klipper_dir and os.path.isfile(os.path.join(klipper_dir, 'klippy', 'klippy.py'))):
            klipper_dir = ''
            for candidate in _candidates(platform):
                if os.path.realpath(candidate.config_dir) == os.path.realpath(config_dir):
                    klipper_dir = candidate.klipper_dir
                    break
        printer_cfg = args.printer_cfg or str(target.metadata.get('printer_cfg') or '')
        if not printer_cfg or os.path.dirname(os.path.realpath(printer_cfg)) != \
                os.path.realpath(config_dir):
            printer_cfg = os.path.join(config_dir, 'printer.cfg')
        if klipper_dir:
            try:
                plan = _discover(platform, overrides + [
                    '--klipper-dir', klipper_dir, '--config-dir', config_dir,
                    '--printer-cfg', printer_cfg])
            except UninstallError as exc:
                target.discovery_note = str(exc)
        target.klipper_dir = klipper_dir
        target.printer_cfg = printer_cfg
    if plan is not None:
        target.klipper_dir = plan['klipper_dir']
        target.printer_cfg = plan['printer_cfg']
        target.service = plan.get('service') or {}
        target.moonraker = plan.get('moonraker_url') or target.moonraker
        target.platform_id = plan.get('platform_id') or target.platform_id
    else:
        recorded = target.metadata.get('service')
        if isinstance(recorded, dict):
            target.service = recorded
        target.moonraker = str(target.metadata.get('moonraker_url') or target.moonraker)
        target.platform_id = str(target.metadata.get('platform') or target.platform_id)
    if (os.path.realpath(target.klipper_dir or '/nonexistent') == '/home/lava/klipper' or
            os.path.realpath(config_dir) == '/home/lava/printer_data/config'):
        target.platform_id = 'snapmaker_u1'
    return target


def has_any_trace(target):
    if lifecycle.installation_traces(target.config_dir):
        return True
    extras = target.extras_dir
    if extras and any(os.path.lexists(os.path.join(extras, name))
                      for name in lifecycle.MODULES):
        return True
    if lifecycle.managed_dropins(target.bmcu_dir):
        return True
    if target.is_u1:
        service = lifecycle.read_optional(lifecycle.U1_KLIPPER_SERVICE)
        if service and any(marker in service for marker in (
                lifecycle.U1_SERVICE_BEGIN.encode(),
                lifecycle.U1_SERVICE_CALL_MARKER.encode(),
                b'bmcu_prepare_klipper_start')):
            return True
        if lifecycle.u1_runner_has_managed_files():
            return True
        if (os.path.lexists(lifecycle.U1_SERIAL_RULE) and
                lifecycle._is_managed(lifecycle.U1_SERIAL_RULE)):
            return True
        for path in (lifecycle.U1_BOOT_HOOK, lifecycle.U1_LEGACY_BOOT_HOOK):
            if os.path.lexists(path) and lifecycle._is_managed(path):
                return True
    return False


def live_handoff(moonraker, force):
    deadline = time.monotonic() + 600.0
    cancelled_update = waiting_announced = False
    while True:
        ok, message = lifecycle.send_gcode(moonraker, 'BMCU_PREPARE_UNINSTALL', timeout=300.0)
        if ok:
            out('The running BMCU handed the printer back to stock control.')
            return True
        lowered = message.lower()
        if 'unknown command' in lowered or 'not ready' in lowered or 'shutdown' in lowered:
            return False
        if 'host update is prepared' in lowered and not cancelled_update:
            cancelled_update = True
            lifecycle.send_gcode(moonraker, 'BMCU_PREPARE_UPDATE ACTION=CANCEL', 30.0)
            continue
        if re.search(r'printer is (printing|paused|pause)\b', lowered):
            raise UninstallError(
                'A print job is active. Finish or cancel it, then run the '
                'uninstaller again. Nothing was changed.')
        busy = any(token in lowered for token in (
            'operation is active', 'is moving', 'host is busy', 'changed while preparing',
            'background preparation is active', 'motion controlled by bmcu'))
        if busy and not force and time.monotonic() < deadline:
            if not waiting_announced:
                waiting_announced = True
                out('Waiting for BMCU to finish its current operation...')
            time.sleep(5.0)
            continue
        out('Live hand-back is not possible (%s); finishing it offline.' %
            message.strip().splitlines()[0][:300] if message.strip() else 'no answer')
        return False


def backup_bmcu_settings(backup, target, state_file):
    bmcu_dir = target.bmcu_dir
    if os.path.isdir(bmcu_dir) and not os.path.islink(bmcu_dir):
        for name in sorted(os.listdir(bmcu_dir)):
            path = os.path.join(bmcu_dir, name)
            if os.path.isfile(path) and not os.path.islink(path) and \
                    name.endswith(('.cfg', '.json', '.txt')):
                backup.save_file(path, 'bmcu-' + name)
        metadata = os.path.join(bmcu_dir, 'runtime', 'INSTALLATION.json')
        if os.path.isfile(metadata):
            backup.save_file(metadata, 'INSTALLATION.json')
        history = os.path.join(bmcu_dir, 'backups')
        if os.path.isdir(history) and not os.path.islink(history) and os.listdir(history):
            backup.save_tree(history, 'bmcu-config-history')
    if state_file and os.path.isfile(state_file):
        backup.save_file(state_file, os.path.basename(state_file))


def strip_includes_everywhere(target, backup):
    changed = []
    data = lifecycle.read_optional(target.printer_cfg)
    if data is not None:
        cleaned, count = lifecycle.strip_bmcu_includes(data)
        if count:
            lifecycle.atomic_write(target.printer_cfg, cleaned)
            changed.append(target.printer_cfg)

    paths = []
    for root, dirs, files in os.walk(target.config_dir):
        dirs[:] = [name for name in dirs
                   if not lifecycle.inside(os.path.join(root, name), target.bmcu_dir) and
                   name not in ('.git', 'snapmaker')]
        for name in files:
            path = os.path.join(root, name)
            if path == target.printer_cfg or re.match(r'^printer-\d{8}_\d{6}\.cfg$', name):
                continue
            if name.lower().endswith('.cfg'):
                paths.append(path)

    for path in paths:
        try:
            data = lifecycle.read_optional(path)
            if data is None:
                continue
            cleaned, count = lifecycle.strip_bmcu_includes(data)
            if not count:
                continue
            backup.save_file(path, os.path.basename(path) + '.before-uninstall')
            lifecycle.atomic_write(path, cleaned)
            changed.append(path)
        except Exception as exc:
            warn('could not remove BMCU include from %s: %s' % (path, exc))
    return changed


def remove_runtime_files(target, state_file):
    removed = []
    if os.path.lexists(target.bmcu_dir):
        lifecycle.remove_path(target.bmcu_dir)
        removed.append(target.bmcu_dir)
    for path in [state_file] + glob.glob(os.path.join(target.config_dir, '.bmcu-state-*')) + \
            glob.glob(os.path.join(target.config_dir, '.bmcu-durable-*')):
        if path and os.path.lexists(path) and not os.path.isdir(path):
            os.unlink(path)
            removed.append(path)
    lifecycle.remove_stale_staging(target.data_root)
    lifecycle.prune_legacy_uninstall_backups(target.data_root)
    return removed


def parser():
    value = argparse.ArgumentParser(
        prog='uninstall', description='Remove BMCU-Klipper completely.')
    value.add_argument('--klipper-dir', default='')
    value.add_argument('--config-dir', default='')
    value.add_argument('--printer-cfg', default='')
    value.add_argument('--python', default='')
    value.add_argument('--user', default='')
    value.add_argument('--moonraker-url', default='')
    value.add_argument('--moonraker-conf', default='')
    value.add_argument('--service-backend', default='')
    value.add_argument('--service-name', default='')
    value.add_argument('--force', action='store_true',
                       help='bypass transient activity, never an active print, firmware upgrade, or unverified running Klipper')
    value.add_argument('--no-restart', action='store_true',
                       help='leave Klipper stopped after removal')
    return value


def main():
    global VERSION, lifecycle, out, warn
    if sys.version_info < (3, 7):
        raise UninstallError('Python 3.7 or newer is required')
    args = parser().parse_args()
    if os.geteuid() != 0:
        raise UninstallError('run the uninstaller as root (sh ./uninstall does this for you)')
    package = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    VERSION = package_version(package)
    lifecycle = load_package_module('bmcu_lifecycle', package)
    out, warn = lifecycle.out, lifecycle.warn
    lifecycle.shield_from_hangup()
    load_package_module('bmcu_planner_process', package)
    platform = load_package_module('bmcu_platform', package)
    out('Package integrity: %d files verified' % max(0, count_package_files(package) - 1))

    target = locate_target(platform, args)
    if target is None or not has_any_trace(target):
        out('BMCU-Klipper is not installed; nothing to remove.')
        return 0

    out('')
    out('=== BMCU-Klipper uninstall ===')
    out('Configuration:  %s' % target.config_dir)
    out('Klipper:        %s' % (target.klipper_dir or 'not found'))
    if target.metadata.get('version'):
        out('Installed:      BMCU-Klipper %s' % target.metadata.get('version'))
    lock = os.open(target.config_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UninstallError('another BMCU install or uninstall is already running')
        return uninstall(target, args)
    finally:
        os.close(lock)


def uninstall(target, args):
    controller = lifecycle.ServiceController(
        target.service, target.klipper_dir or '/nonexistent', target.printer_cfg)
    out('Service:        %s' % (controller.describe() if controller.controllable
                                else 'not detected'))
    state = lifecycle.PrinterSnapshot()
    if target.klipper_dir:
        state = lifecycle.wait_until_safe(
            target.moonraker, target.klipper_dir, target.printer_cfg,
            'sh ./uninstall', force=args.force, is_u1=target.is_u1)
    out('Printer:        %s' % state.describe())

    state_file = lifecycle.bmcu_state_file(target.config_dir, target.bmcu_dir)
    report, problems = [], []

    handed_back = False
    if state.ready:
        handed_back = live_handoff(target.moonraker, args.force)

    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    backup = lifecycle.Backup(target.data_root, 'uninstall')
    backup.save_file(target.printer_cfg, 'printer.cfg.before-uninstall')

    was_running = bool(target.klipper_dir and controller.running_pids())
    stopped = not was_running
    if was_running and controller.controllable:
        out('Stopping Klipper (%s)...' % controller.describe())
        try:
            controller.stop()
        except Exception as exc:
            raise UninstallError(
                'Klipper could not be stopped (%s). Nothing was removed.' % exc)
        stopped = True
    elif was_running:
        warn('the Klipper service manager was not detected; BMCU is removed while '
             'Klipper keeps running and Klipper is restarted through Moonraker')

    try:
        helpers = lifecycle.stop_bmcu_helpers(
            [target.bmcu_dir, lifecycle.U1_RUNNER_DIR] if target.is_u1 else [target.bmcu_dir])
        if helpers:
            report.append('Stopped %d BMCU helper process(es)' % helpers)
    except Exception as exc:
        problems.append('%s; reboot the printer if a BMCU USB adapter is hung' % exc)
    try:
        backup_bmcu_settings(backup, target, state_file)
    except Exception as exc:
        problems.append('settings backup: %s' % exc)

    loaded_heads = []
    if target.is_u1:
        saved_state = lifecycle.read_json(state_file)
        if saved_state is not None and stopped:
            try:
                changes, notes, loaded_heads = lifecycle.u1_offline_restore(
                    target.config_dir, saved_state, backup)
                report.extend(changes)
                for note in notes:
                    warn(note)
            except Exception as exc:
                problems.append('Snapmaker stock settings: %s' % exc)
        elif saved_state is not None:
            warn('Klipper is still running, so the Snapmaker stock feeder settings '
                 'could not be checked; enable auto feed in the printer menu if a '
                 'head does not feed by itself')
        elif not handed_back:
            warn('the saved pre-BMCU Snapmaker feeder state is unavailable; stock '
                 'auto-feed settings were left unchanged')

    try:
        changed = strip_includes_everywhere(target, backup)
        if changed:
            report.append('BMCU include removed from %s' %
                          ', '.join(os.path.basename(path) for path in changed))
    except Exception as exc:
        if was_running and controller.controllable:
            controller.start()
        raise UninstallError('printer.cfg could not be updated; nothing else was removed: %s' % exc)

    if target.extras_dir:
        try:
            removed, foreign = lifecycle.remove_module_links(
                target.extras_dir, backup, target.bmcu_dir)
            if removed:
                report.append('Klipper modules removed: %d' % len(removed))
            for path in foreign:
                warn('%s belongs to another installation and was left alone' % path)
        except Exception as exc:
            problems.append('Klipper modules: %s' % exc)

    if target.is_u1:
        removed_hooks, hook_problems = lifecycle.remove_u1_integration(backup)
        report.extend('Removed %s' % item for item in removed_hooks)
        problems.extend(hook_problems)
    try:
        dropins = lifecycle.remove_systemd_dropins(target.bmcu_dir)
        report.extend('Removed %s' % path for path in dropins)
    except Exception as exc:
        problems.append('systemd drop-in: %s' % exc)

    socket_dir = lifecycle.transport_socket_dir(target.bmcu_dir)
    try:
        for path in remove_runtime_files(target, state_file):
            if path == target.bmcu_dir:
                report.append('Removed %s' % path)
    except Exception as exc:
        problems.append('BMCU files: %s' % exc)
    lifecycle.remove_socket_dir(socket_dir)
    lifecycle.prune_backups(target.data_root, 'uninstall', 1)
    lifecycle.prune_backups(target.data_root, 'install', 0)

    leftovers = lifecycle.config_mentions_bmcu_sections(target.config_dir, target.bmcu_dir)
    for path in leftovers:
        warn('%s still contains a [bmcu...] section; remove it, or Klipper reports '
             'an unknown section' % path)

    ready_text = ''
    if args.no_restart:
        ready_text = 'Klipper was left stopped as requested.' if stopped else ''
    elif controller.controllable and target.klipper_dir:
        controller.daemon_reload()
        out('Starting Klipper...')
        if controller.start():
            after = lifecycle.wait_ready(target.moonraker, target.klipper_dir,
                                         target.printer_cfg,
                                         300.0 if target.is_u1 else 180.0,
                                         is_u1=target.is_u1)
            if after.ready:
                ready_text = 'Klipper is running.'
            else:
                ready_text = ('Klipper started but reports: %s. The previous '
                              'printer.cfg is in the backup below.' % after.describe())
        else:
            problems.append('Klipper did not start; start it from Mainsail/Fluidd '
                            'or reboot the printer')
    elif was_running:
        if lifecycle.request_klipper_restart(target.moonraker):
            ready_text = 'Klipper restart requested.'
        else:
            ready_text = 'Restart Klipper (or reboot the printer) to finish.'

    remaining = []
    if os.path.lexists(target.bmcu_dir):
        remaining.append(target.bmcu_dir)
    cfg = lifecycle.read_optional(target.printer_cfg)
    if cfg is not None and lifecycle.has_bmcu_include(cfg):
        remaining.append('include in %s' % target.printer_cfg)
    if target.extras_dir:
        for name in lifecycle.MODULES:
            path = os.path.join(target.extras_dir, name)
            if (os.path.lexists(path) and lifecycle._looks_like_bmcu_module(path, name) and
                    not lifecycle._owned_by_other_installation(path, target.bmcu_dir)):
                remaining.append(path)
    if target.is_u1:
        service = lifecycle.read_optional(lifecycle.U1_KLIPPER_SERVICE)
        if service and any(marker in service for marker in (
                lifecycle.U1_SERVICE_BEGIN.encode(),
                lifecycle.U1_SERVICE_CALL_MARKER.encode(),
                b'bmcu_prepare_klipper_start')):
            remaining.append('BMCU hook in %s' % lifecycle.U1_KLIPPER_SERVICE)
        if lifecycle.u1_runner_has_managed_files():
            remaining.append(lifecycle.U1_RUNNER_DIR)
        if (os.path.lexists(lifecycle.U1_SERIAL_RULE) and
                lifecycle._is_managed(lifecycle.U1_SERIAL_RULE)):
            remaining.append(lifecycle.U1_SERIAL_RULE)
        for path in (lifecycle.U1_BOOT_HOOK, lifecycle.U1_LEGACY_BOOT_HOOK):
            if os.path.lexists(path) and lifecycle._is_managed(path):
                remaining.append(path)

    out('')
    for line in report:
        out('  - %s' % line)
    for head in loaded_heads:
        warn('Head %d may still hold BMCU filament. Pull it out by hand before '
             'loading filament with the stock feeder.' % (head + 1))
    for problem in problems:
        warn(problem)
    out('Your BMCU settings were saved to: %s' % backup.path)
    if ready_text:
        out(ready_text)
    if remaining:
        raise UninstallError('BMCU-Klipper could not be removed completely: %s. '
                             'Run sh ./uninstall again.' % '; '.join(remaining))
    out('BMCU-Klipper was removed.')
    return 0


def report_unexpected(exc):
    try:
        print('ERROR: unexpected failure: %s' % exc, file=sys.stderr)
        traceback.print_exc()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        try:
            print('\nInterrupted.', file=sys.stderr)
        except Exception:
            pass
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if isinstance(exc, UninstallError) or exc.__class__.__name__ == 'LifecycleError':
            try:
                print('ERROR: %s' % exc, file=sys.stderr)
            except Exception:
                pass
        else:
            report_unexpected(exc)
        raise SystemExit(1)
