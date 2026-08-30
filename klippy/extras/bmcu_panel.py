# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function

class BMCUPanel(object):
    def __init__(self, config):

        self.enabled = config.getboolean('enabled', True)
        self.host = str(config.get('host', '0.0.0.0') or '0.0.0.0').strip()
        self.port = config.getint('port', 8291, minval=1024, maxval=65535)
        self.moonraker_url = str(
            config.get('moonraker_url', 'http://127.0.0.1:7125') or
            'http://127.0.0.1:7125').strip()
        self.access_token = str(config.get('access_token', '') or '').strip()

    def get_status(self, eventtime):
        return {
            'external_process': True,
            'enabled': bool(self.enabled),
            'host': self.host,
            'port': int(self.port),
        }

def load_config(config):
    return BMCUPanel(config)
