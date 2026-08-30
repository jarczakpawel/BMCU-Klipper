# SPDX-License-Identifier: GPL-3.0-or-later

from .bmcu_core.manager import BMCUManager

def load_config(config):
    return BMCUManager(config)
