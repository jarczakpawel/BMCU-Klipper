# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function

import json
import logging
import os
import subprocess
import sys

HELPER_TIMEOUT = 300.0


class BMCUPanel(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.enabled = config.getboolean('enabled', True)
        self.host = str(config.get('host', '0.0.0.0') or '0.0.0.0').strip()
        self.port = config.getint('port', 8291, minval=1024, maxval=65535)
        self.moonraker_url = str(
            config.get('moonraker_url', 'http://127.0.0.1:7125') or
            'http://127.0.0.1:7125').strip()
        self.access_token = str(config.get('access_token', '') or '').strip()
        self._helper = None
        self._helper_deadline = 0.0
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        try:
            self._start_host_helpers()
        except Exception:
            logging.exception('BMCU: could not start the host helper processes')

    def _start_host_helpers(self):
        runtime = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__))))
        metadata_path = os.path.join(runtime, 'INSTALLATION.json')
        bootstrap = os.path.join(runtime, 'scripts', 'bmcu_host_bootstrap.py')
        if not (os.path.isfile(metadata_path) and os.path.isfile(bootstrap)):
            return
        with open(metadata_path, 'r') as stream:
            metadata = json.load(stream)
        if not isinstance(metadata, dict) or metadata.get('platform') == 'snapmaker_u1':
            return
        with open(os.devnull, 'rb') as stdin, open(os.devnull, 'wb') as devnull:
            self._helper = subprocess.Popen(
                [sys.executable, '-I', '-S', bootstrap, '--ensure-processes',
                 '--metadata', metadata_path, '--quiet'],
                stdin=stdin, stdout=devnull, stderr=devnull,
                close_fds=True, start_new_session=True)
        reactor = self.printer.get_reactor()
        self._helper_deadline = reactor.monotonic() + HELPER_TIMEOUT
        reactor.register_timer(self._reap_helper, reactor.monotonic() + 1.0)

    def _reap_helper(self, eventtime):
        helper = self._helper
        if helper is None:
            return self.printer.get_reactor().NEVER
        code = helper.poll()
        if code is None and eventtime < self._helper_deadline:
            return eventtime + 1.0
        if code is None:
            try:
                helper.terminate()
            except OSError:
                pass
            logging.warning('BMCU: host helper start exceeded %.0f seconds', HELPER_TIMEOUT)
        elif code:
            logging.warning('BMCU: host helper start exited with code %s', code)
        self._helper = None
        return self.printer.get_reactor().NEVER

    def get_status(self, eventtime):
        return {
            'external_process': True,
            'enabled': bool(self.enabled),
            'host': self.host,
            'port': int(self.port),
        }

def load_config(config):
    return BMCUPanel(config)
