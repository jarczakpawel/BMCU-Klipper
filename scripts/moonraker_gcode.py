#!/usr/bin/env python3
import argparse
import json
import math
import urllib.error
import urllib.parse
import urllib.request

MAX_MOONRAKER_RESPONSE = 1024 * 1024
MAX_GCODE_BYTES = 64 * 1024

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _reject_json_constant(value):
    raise ValueError('non-standard JSON number: %s' % value)

def validate_base_url(value):
    parsed = urllib.parse.urlsplit(str(value or ''))
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise ValueError('--url must be an HTTP(S) base URL without credentials, query or fragment')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))

def request_gcode(command, base_url, api_key='', timeout=15.0):
    if not isinstance(command, str) or not command.strip():
        raise ValueError('G-code command must not be empty')
    try:
        request_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError('timeout must be finite and within 0.05..300 seconds') from exc
    if not math.isfinite(request_timeout) or not 0.05 <= request_timeout <= 300.0:
        raise ValueError('timeout must be finite and within 0.05..300 seconds')
    payload = json.dumps({'script': command}, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_GCODE_BYTES:
        raise ValueError('G-code request is too large')
    target = validate_base_url(base_url) + '/printer/gcode/script'
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if api_key:
        headers['X-Api-Key'] = str(api_key)
    request = urllib.request.Request(target, data=payload, headers=headers, method='POST')
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=request_timeout) as response:
            raw = response.read(MAX_MOONRAKER_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError('Moonraker redirect is refused') from exc
        raise
    if len(raw) > MAX_MOONRAKER_RESPONSE:
        raise RuntimeError('Moonraker response is too large')
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('invalid Moonraker response') from exc
    if not isinstance(value, dict):
        raise RuntimeError('invalid Moonraker response')
    if 'error' in value:
        raise RuntimeError(str(value['error']))
    return value

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('command')
    ap.add_argument('--url', default='http://127.0.0.1:7125')
    ap.add_argument('--api-key', default='')
    args = ap.parse_args()
    try:
        request_gcode(args.command, args.url, args.api_key)
    except (ValueError, RuntimeError, urllib.error.URLError) as exc:
        raise SystemExit(str(exc))
    print('Moonraker accepted: ' + args.command)

if __name__ == '__main__':
    main()
