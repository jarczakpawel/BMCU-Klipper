#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
from pathlib import Path

def vendor_site_packages(script_file=None):
    source = Path(script_file or __file__).resolve()

    root = source.parent.parent
    return root / "vendor" / "site-packages"

def _usable_pyserial():
    try:
        import serial
    except (ImportError, AttributeError):
        return False
    return hasattr(serial, "Serial") and hasattr(serial, "VERSION")

def ensure_vendor_path(script_file=None):

    path = vendor_site_packages(script_file)
    if _usable_pyserial():
        return path
    if path.is_dir():
        value = os.fspath(path)

        sys.modules.pop("serial", None)
        while value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
    return path
