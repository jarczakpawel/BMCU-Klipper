#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from safe_file_ops import (SafeFileError, atomic_write_text, backup_file,
                           read_regular_text)

INCLUDES = [
    "[include bmcu/bmcu.cfg]",
    "[include bmcu/bmcu_macros.cfg]",
    "[include bmcu/bmcu_panel.cfg]",
]
LEGACY_PANEL_MACROS_INCLUDE = "[include bmcu/bmcu_panel_macros.cfg]"
LEGACY_INCLUDES = INCLUDES + [LEGACY_PANEL_MACROS_INCLUDE]
KNOWN_INCLUDES = set(LEGACY_INCLUDES)
MARKER_PREFIX = "#*# <---------------------- SAVE_CONFIG"
BEGIN = "# BEGIN BMCU-KLIPPER AUTO-INCLUDE"
END = "# END BMCU-KLIPPER AUTO-INCLUDE"
BLOCK = [
    "", "################################", "# BMCU-Klipper", BEGIN,
    "################################", *INCLUDES, END, "",
]

def is_normal_line_after_save(line):
    value = line.strip()
    return bool(value) and not value.startswith("#*#")

def find_save_marker(lines):
    for index, line in enumerate(lines):
        if line.strip().startswith(MARKER_PREFIX):
            return index
    return -1

class ManagedBlockError(ValueError):
    pass

def find_managed_block(lines):
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if not begins and not ends:
        return -1, -1, None
    if len(begins) != 1 or len(ends) != 1:
        raise ManagedBlockError('printer.cfg must contain exactly one BEGIN and one END BMCU marker')
    begin, end = begins[0], ends[0]
    if begin >= end:
        raise ManagedBlockError('BMCU managed block markers are out of order')
    active = [line.strip() for line in lines[begin + 1:end]
              if line.strip() and not line.lstrip().startswith('#')]
    if active == INCLUDES:
        generation = 'current'
    elif active == LEGACY_INCLUDES:
        generation = 'legacy-panel-macros'
    else:
        raise ManagedBlockError('BMCU managed block contains unexpected active lines; automatic repair is disabled')
    outside = [i + 1 for i, line in enumerate(lines)
               if line.strip() in KNOWN_INCLUDES and not (begin < i < end)]
    if outside:
        raise ManagedBlockError('BMCU include lines also exist outside the managed block')
    start = begin
    while start > 0 and lines[start - 1].strip() in ('# BMCU-Klipper', '################################', ''):
        start -= 1
    stop = end + 1
    while stop < len(lines) and lines[stop].strip() == '':
        stop += 1
    return start, stop, generation

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--printer-cfg', required=True)
    parser.add_argument('--backup-dir', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    path = Path(args.printer_cfg)
    try:
        original_text, original_info = read_regular_text(path)
    except SafeFileError as exc:
        raise SystemExit(str(exc))
    lines = original_text.splitlines()
    save_index = find_save_marker(lines)
    if save_index >= 0:
        bad = [(save_index + 2 + offset, line)
               for offset, line in enumerate(lines[save_index + 1:])
               if is_normal_line_after_save(line)]
        if bad:
            print('ERROR: printer.cfg contains normal config lines after SAVE_CONFIG block.', file=sys.stderr)
            print('Move those lines above SAVE_CONFIG manually, then rerun the installer.', file=sys.stderr)
            for number, line in bad[:20]:
                print('  line %d: %s' % (number, line), file=sys.stderr)
            raise SystemExit(2)
    try:
        block_start, block_stop, generation = find_managed_block(lines)
    except ManagedBlockError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(4)
    if block_start >= 0 and generation == 'current':
        print('BMCU include block is current. No printer.cfg change needed.')
        return 0
    if block_start >= 0:
        new_lines = lines[:block_start] + BLOCK + lines[block_stop:]
        text = '\n'.join(new_lines).rstrip() + '\n'
        if args.dry_run:
            print('DRY-RUN: would update legacy BMCU include block in %s' % path)
            return 0
        backup = backup_file(path, Path(args.backup_dir), 'bmcu_backup', expected_info=original_info)
        atomic_write_text(path, text, expected_info=original_info)
        print('Updated legacy BMCU include block: %s' % path)
        print('Backup saved: %s' % backup)
        return 0
    outside = [(i + 1, line.strip()) for i, line in enumerate(lines) if line.strip() in KNOWN_INCLUDES]
    if outside:
        active = [line for _number, line in outside]
        if len(active) == len(INCLUDES) and set(active) == set(INCLUDES):
            print('Complete manual BMCU include set detected. printer.cfg was not changed.')
            return 0
        print('ERROR: incomplete or duplicated manual BMCU include set.', file=sys.stderr)
        print('The installer will not rewrite custom printer.cfg layout.', file=sys.stderr)
        for number, line in outside:
            print('  line %d: %s' % (number, line), file=sys.stderr)
        raise SystemExit(3)
    insert_index = save_index if save_index >= 0 else len(lines)
    prefix = lines[:insert_index]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    new_lines = prefix + BLOCK + lines[insert_index:]
    text = '\n'.join(new_lines).rstrip() + '\n'
    if args.dry_run:
        print('DRY-RUN: would insert BMCU include block into %s' % path)
        print('DRY-RUN: insert location: before SAVE_CONFIG block' if save_index >= 0 else 'DRY-RUN: insert location: end of file')
        return 0
    backup = backup_file(path, Path(args.backup_dir), 'bmcu_backup', expected_info=original_info)
    atomic_write_text(path, text, expected_info=original_info)
    print('Updated printer.cfg safely: %s' % path)
    print('Backup saved: %s' % backup)
    print('BMCU include block inserted before SAVE_CONFIG.' if save_index >= 0 else 'BMCU include block appended.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
