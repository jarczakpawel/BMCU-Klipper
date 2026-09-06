import os
import re

_VERSION_RE = re.compile(
    r'^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$')
_MAX_VERSION_BYTES = 4096


def parse_release_versions(text):
    result = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, value = line.split('=', 1)
        elif ':' in line:
            key, value = line.split(':', 1)
        else:
            continue
        key, value = key.strip().lower(), value.strip().lower()
        if key not in ('package', 'firmware'):
            continue
        if not _VERSION_RE.fullmatch(value):
            raise ValueError('invalid %s version' % key)
        result[key] = value
    if 'package' not in result or 'firmware' not in result:
        raise ValueError('version file is incomplete')
    return result


def load_release_versions(path=None):
    if path is None:
        path = os.path.abspath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)), '..', '..', '..',
            'version'))
    with open(path, 'rb') as stream:
        data = stream.read(_MAX_VERSION_BYTES + 1)
    if len(data) > _MAX_VERSION_BYTES:
        raise ValueError('version file is too large')
    return parse_release_versions(data.decode('utf-8'))


_RELEASE = load_release_versions()
PACKAGE_VERSION = _RELEASE['package']
REQUIRED_FIRMWARE_TEXT = _RELEASE['firmware']
REQUIRED_FIRMWARE = tuple(int(part) for part in REQUIRED_FIRMWARE_TEXT.split('.'))
