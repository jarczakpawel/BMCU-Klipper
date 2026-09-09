# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import binascii
import copy
import errno
import hashlib
import greenlet
import json
import logging
import math
import os
import re
import socket
import stat
import tempfile
from collections import deque
from contextlib import contextmanager

from . import compat, protocol, presets, transport
from .release import PACKAGE_VERSION
from .device import BMCUDevice
from .endpoints import (create_endpoint, normalize_u1_material_name,
                        u1_tip_profile_defaults,
                        u1_tip_snapmaker_movement_defaults,
                        validate_u1_tip_profile)
from .state import (StateStore, MAX_STATE_DEVICES, MAX_STATE_ENDPOINTS,
                    MAX_U1_TIP_PROFILES, MAX_LIGHTING_PROFILES, DEFAULT_LIGHTING)
from .refill import AutoRefillController
from .persistence import DurableWriteWorker

class BMCUError(RuntimeError):
    pass

_GCODE_REQUIRED = object()
PRINT_PLAN_SCHEMA = 1
U1_NATIVE_TOOL_COUNT = 4
U1_LOGICAL_TOOL_LIMIT = 32
GENERIC_EXTERNAL_TOOL = 0
GENERIC_BMCU_TOOL_MIN = 1
GENERIC_LOGICAL_TOOL_LIMIT = 256
U1_SOURCE_PLAN_SCHEMA = 1

class BMCUManager(object):
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        def finite_config_float(name, default=None, **limits):
            value = config.getfloat(name, default, **limits)
            if not math.isfinite(value):
                raise config.error('%s must be a finite number' % name)
            return value
        self.baud = config.getint('baud', 115200, minval=9600, maxval=2000000)
        self.transport_sidecar = config.getboolean('transport_sidecar', True)
        if not self.transport_sidecar:
            raise config.error(
                'transport_sidecar must be enabled; direct CH340 access from Klipper is unsupported')
        self.transport_socket_dir = os.path.abspath(str(config.get(
            'transport_socket_dir', '/tmp/bmcu-transport') or
            '/tmp/bmcu-transport').strip())
        if (not self.transport_socket_dir.startswith('/') or
                len(self.transport_socket_dir.encode('utf-8')) > 80 or
                any(ord(ch) < 32 for ch in self.transport_socket_dir)):
            raise config.error('invalid transport_socket_dir')

        self.u1_planner_timeout = finite_config_float(
            'u1_planner_timeout', 15.0, minval=1.0, maxval=120.0)
        self.u1_planner_poll_interval = finite_config_float(
            'u1_planner_poll_interval', 0.20, minval=0.05, maxval=1.0)
        self._u1_planner_socket = os.path.join(
            self.transport_socket_dir, 'u1-planner.sock')
        self._u1_planner_result_dir = os.path.join(
            self.transport_socket_dir, 'u1-plans')
        if len(self._u1_planner_socket.encode('utf-8')) >= 100:
            raise config.error('transport_socket_dir is too long for U1 planner')
        self.connect_settle_time = finite_config_float('connect_settle_time', 1.5, minval=0.1, maxval=10.0)
        self.sidecar_status_interval = finite_config_float(
            'sidecar_status_interval', 1.00, minval=0.05, maxval=10.0)
        self.reconnect_interval = finite_config_float('reconnect_interval', 1.0, minval=0.1, maxval=60.0)
        self.reconnect_max = finite_config_float(
            'reconnect_max', 30.0, minval=self.reconnect_interval, maxval=300.0)
        self.connection_timeout = finite_config_float('connection_timeout', 15.0, minval=8.0, maxval=120.0)

        self.sidecar_ipc_resume_grace = finite_config_float(
            'sidecar_ipc_resume_grace', 5.0, minval=1.0, maxval=30.0)

        self.sidecar_ipc_outage_timeout = finite_config_float(
            'sidecar_ipc_outage_timeout', 12.0, minval=3.0, maxval=120.0)
        if self.sidecar_ipc_outage_timeout <= self.sidecar_ipc_resume_grace:
            raise config.error(
                'sidecar_ipc_outage_timeout must exceed sidecar_ipc_resume_grace')
        self.heartbeat_interval = finite_config_float('heartbeat_interval', 5.0, minval=1.0, maxval=10.0)
        self.manager_tick_interval = finite_config_float(
            'manager_tick_interval', 0.50, minval=0.05, maxval=2.0)
        self.manager_idle_interval = finite_config_float(
            'manager_idle_interval', 2.0, minval=0.25, maxval=5.0)
        self.connect_spread = finite_config_float('connect_spread', 0.05, minval=0.0, maxval=1.0)
        self.rx_budget_bytes = config.getint(
            'rx_budget_bytes', 256, minval=128, maxval=16384)
        self.rx_budget_packets = config.getint(
            'rx_budget_packets', 2, minval=1, maxval=64)
        self.rx_budget_ms = finite_config_float(
            'rx_budget_ms', 0.5, minval=0.10, maxval=10.0)
        self.callback_warning_ms = finite_config_float(
            'callback_warning_ms', 10.0, minval=2.0, maxval=100.0)
        self.status_cache_interval = finite_config_float(
            'status_cache_interval', 1.0, minval=0.05, maxval=5.0)
        self.status_cache_idle_interval = finite_config_float(
            'status_cache_idle_interval', 5.0, minval=0.25, maxval=30.0)
        self.reactor_yield_interval = finite_config_float(
            'reactor_yield_interval', 0.005, minval=0.001, maxval=0.05)
        self.manager_work_yield_interval = finite_config_float(
            'manager_work_yield_interval', 0.010, minval=0.001, maxval=0.1)
        self.critical_motion_release_delay = finite_config_float(
            'critical_motion_release_delay', 0.500,
            minval=0.050, maxval=2.0)

        self.critical_transport_interval = finite_config_float(
            'critical_transport_interval', 1.0, minval=0.25, maxval=5.0)
        self.critical_rx_budget_bytes = config.getint(
            'critical_rx_budget_bytes', 128, minval=64, maxval=1024)
        self.critical_rx_budget_packets = config.getint(
            'critical_rx_budget_packets', 1, minval=1, maxval=4)
        self.critical_rx_budget_ms = finite_config_float(
            'critical_rx_budget_ms', 0.15, minval=0.05, maxval=1.0)
        self.critical_reactor_yield_interval = finite_config_float(
            'critical_reactor_yield_interval', 0.020,
            minval=0.005, maxval=0.100)
        self.transport_min_buffer = finite_config_float(
            'transport_min_buffer', 1.50, minval=0.25, maxval=10.0)
        self.transport_retry_interval = finite_config_float(
            'transport_retry_interval', 5.00, minval=0.10, maxval=10.0)
        self.required_runtime_sync_timeout = finite_config_float(
            'required_runtime_sync_timeout', 30.0, minval=2.0, maxval=60.0)
        self.tx_queue_limit = config.getint('tx_queue_limit', 4096, minval=512, maxval=262144)
        if self.heartbeat_interval >= self.connection_timeout:
            raise config.error('heartbeat_interval must be lower than connection_timeout')
        finite_config_float('poll_interval_idle', 8.0, minval=0.5, maxval=30.0)
        finite_config_float('poll_interval_active', 1.0, minval=0.1, maxval=5.0)
        config.getboolean('require_encoder_test', True)
        requested_mode = str(
            config.get('controller_mode', 'standalone') or 'standalone').strip().lower()
        self.requested_controller_mode = requested_mode
        if requested_mode not in ('auto', 'standalone'):
            raise config.error('controller_mode must be standalone')
        self.controller_mode = 'standalone'
        self.controller_block_reason = ''
        self.printer_analysis = compat.analyze_printer(
            self.printer, self.controller_mode)
        self.owns_filament_callbacks = True

        self._auto_channel_autoload_requested = False
        self.auto_channel_autoload = (
            self._auto_channel_autoload_requested and
            self.owns_filament_callbacks)
        self.pause_on_error = config.getboolean('pause_on_error', True)

        self.debug_enabled = config.getboolean('debug', False)
        self.firmware_update_power_pin = str(
            config.get('firmware_update_power_pin', '') or '').strip()
        if (self.firmware_update_power_pin and
                re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',
                             self.firmware_update_power_pin) is None):
            raise config.error(
                'firmware_update_power_pin must be blank or a Klipper output_pin name')
        self.firmware_update_power_settle_time = finite_config_float(
            'firmware_update_power_settle_time', 0.35,
            minval=0.05, maxval=5.0)
        self._firmware_update_power_restore = {}

        self.tool_command_mode = 'printer'
        self._register_t_commands_requested = False
        self.register_t_commands = False
        self._ownership_finalized = False

        self.motion_defaults = {
            'load_pressure_pct': finite_config_float(
                'load_pressure_pct', 82.0, minval=75.0, maxval=95.0),
            'load_speed_mms': finite_config_float('load_speed_mms', 80.0, minval=10.0, maxval=120.0),
            'pull_speed_mms': finite_config_float('pull_speed_mms', 80.0, minval=10.0, maxval=120.0),
            'pull_speed_end_mms': finite_config_float('pull_speed_end_mms', 12.0, minval=4.0, maxval=40.0),
            'jam_timeout_ms': config.getint('jam_timeout_ms', 20000, minval=1000, maxval=120000),
            'before_pullback_target_pct': finite_config_float(
                'before_pullback_target_pct', 40.0, minval=20.0, maxval=60.0),
        }

        self.contact_buffer_pct = config.getint('contact_buffer_pct', 82, minval=60, maxval=98)
        self.contact_timeout = finite_config_float('contact_timeout', 45.0, minval=1.0, maxval=180.0)
        self.max_route_mm = finite_config_float('max_route_mm', 1500.0, minval=50.0, maxval=5000.0)
        self.bite_mm = finite_config_float('bite_mm', 8.0, minval=1.0, maxval=40.0)
        self.bite_feed = config.getint('bite_feed', 180, minval=10, maxval=3000)
        self.bite_encoder_ratio = finite_config_float('bite_encoder_ratio', 0.35, minval=0.05, maxval=1.5)
        self.bite_buffer_delta = finite_config_float('bite_buffer_delta', 2.0, minval=0.0, maxval=30.0)
        self.capture_mm = finite_config_float('capture_mm', 28.0, minval=1.0, maxval=150.0)
        self.capture_feed = config.getint('capture_feed', 300, minval=10, maxval=5000)
        self.capture_encoder_ratio = finite_config_float('capture_encoder_ratio', 0.35, minval=0.05, maxval=1.5)
        self.bite_retries = config.getint('bite_retries', 2, minval=1, maxval=3)
        self.retry_retract_mm = finite_config_float('retry_retract_mm', 2.0, minval=0.0, maxval=15.0)
        self.unload_timeout = finite_config_float('unload_timeout', 60.0, minval=2.0, maxval=300.0)
        self.release_retract_mm = finite_config_float('release_retract_mm', 0.0, minval=0.0, maxval=300.0)
        self.release_retract_feed = config.getint('release_retract_feed', 1200, minval=10, maxval=10000)
        self.encoder_test_mm = finite_config_float('encoder_test_mm', 50.0, minval=10.0, maxval=200.0)

        config_path = self.printer.get_start_args().get('config_file', '')
        default_state_path = os.path.join(os.path.dirname(config_path), 'bmcu_state.json') if config_path else '/tmp/bmcu_state.json'
        self._durable_writer = DurableWriteWorker(self.reactor)
        self.state = StateStore(
            config.get('state_file', default_state_path),
            writer=self._durable_writer)
        self.package_build_id = 'unpackaged'
        try:
            digest_path = os.path.abspath(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), '..', '..', '..',
                'package.sha256'))
            with open(digest_path, 'r') as stream:
                digest = stream.read(256).strip().split()[0]
            if re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                self.package_build_id = digest.lower()[:16]
        except (IOError, OSError, IndexError):
            pass
        logging.info(
            'BMCU-Klipper %s package build %s',
            PACKAGE_VERSION, self.package_build_id)

        self.devices = []
        self.devices_by_name = {}
        self.devices_by_port = {}
        self.devices_by_uid = {}
        self._forget_pending = {}
        self.endpoints = {}
        self.endpoint_locks = set()
        self.path_locks = set()

        self.active_operations = {}

        self._required_transport_users = 0
        self._control_plane_requests = 0

        self.loaded_tools = {}
        self.active_tool = -1
        self.prestaged = {}
        self._uncertain_routes = set()
        self._conflicted_endpoints = set()

        session = self.state.data.get('print_session', {})
        self.print_tools = copy.deepcopy(session.get('tools', {})) if isinstance(session.get('tools', {}), dict) else {}
        self.print_plan_tools = {}
        self.print_plan_required = set()
        self.print_plan_open = bool(session.get('plan_open', False))
        self.print_plan_interrupted = self.print_plan_open
        self.print_plan_schema = PRINT_PLAN_SCHEMA
        self.print_job_id = str(session.get('job_id', '') or '')
        self.print_map_active = bool(session.get('active', False))
        self._u1_map_backup = copy.deepcopy(session.get('u1_map_backup', {})) \
            if isinstance(session.get('u1_map_backup', {}), dict) else {}
        self._u1_used_backup = copy.deepcopy(session.get('u1_used_backup', {})) \
            if isinstance(session.get('u1_used_backup', {}), dict) else {}
        self._u1_end_unload_backup = copy.deepcopy(
            session.get('u1_end_unload_backup', {})) \
            if isinstance(session.get('u1_end_unload_backup', {}), dict) else {}
        self._u1_original = copy.deepcopy(session.get('u1_original', {})) \
            if isinstance(session.get('u1_original', {}), dict) else {}
        self.print_transaction_phase = str(
            session.get('transaction_phase', '') or '').lower()
        self.print_loaded_routes = set(
            str(value) for value in session.get('loaded_routes', [])
            if isinstance(value, str))
        self.print_route_journal_initialized = bool(
            session.get('route_journal_initialized', False))
        self.print_terminal_unload_pending = bool(
            session.get('terminal_unload_pending', False))
        self.print_stock_reset_observed = bool(
            session.get('stock_reset_observed', False))
        self._u1_prepared_heads = set(
            int(value) for value in session.get('u1_prepared_heads', [])
            if isinstance(value, int) and not isinstance(value, bool) and
            value in range(4))
        self.u1_cross_refill_pending = copy.deepcopy(
            session.get('u1_cross_refill_pending', {})) \
            if isinstance(session.get('u1_cross_refill_pending', {}), dict) else {}

        self._u1_preextrude_primed_tools = {}

        self._u1_toolchange_plan = []
        self._u1_toolchange_cursor = -1
        self._u1_toolchange_plan_path = ''
        self._u1_tool_temperature_defaults = {}
        self._u1_planner_requests = 0
        self._u1_planner_failures = 0
        self._u1_planner_max_wait_ms = 0.0
        self._u1_planner_last_wait_ms = 0.0
        self._u1_planner_last_scan_ms = 0.0
        self._u1_pending_temperature_profile = None
        self._u1_background_jobs = {}
        self.last_error = None
        self.last_diagnostic = None
        self._last_poll = {}
        self._registered_tools = set()
        self._autoload_pending = set()
        self._autoload_observation = {}

        self._path_learning_observation = {}
        self._autoload_retry_at = {}
        self._refill_runout_latched = set()

        self._calibration_policy_suspended = set()

        self._last_print_state = None
        self._print_session_seen_active = False
        self._next_print_state_check = 0.0
        self._state_save_pending = False
        self._state_save_due = 0.0
        self._u1_detector_callback_registered = False
        self._u1_projection_repairs = set()
        self._endpoint_projection_clear_pending = set()
        self._endpoint_projection_clear_retries = {}
        self._u1_restore_pending_reported = False

        self._u1_disconnect_hazards = {}

        self._u1_disconnect_hazard_reasons = {}
        self._sidecar_ipc_outages_escalated = 0

        self._u1_devices_reconciled_once = set()
        self._u1_lease_state = {}
        self._u1_persistent_reasserted = set()
        self._u1_lease_dirty = True
        self._next_u1_lease_check = 0.0
        self._u1_lease_callback_scheduled = False

        self._u1_lease_watchdog_fingerprint = None
        self._u1_lease_watchdog_probes = 0
        self._u1_lease_watchdog_changes = 0
        self._u1_lease_watchdog_max_ms = 0.0
        self._u1_lease_reconcile_runs = 0
        self._u1_lease_reconcile_max_ms = 0.0
        self._u1_lease_endpoint_max_ms = {}
        self._klippy_disconnecting = False
        self._status_cache = None
        self._status_cache_at = 0.0
        self._manager_tick_count = 0
        self._manager_tick_max_ms = 0.0
        self._manager_tick_overruns = 0
        self._manager_active_ticks = 0
        self._manager_idle_ticks = 0
        self._status_cache_hits = 0
        self._u1_cancel_original = None
        self._u1_cancel_wrapped = False
        self._u1_replenish_original = None
        self._manual_refill_resume_original = None
        self._manual_refill_resume_wrapped = False
        self._u1_replenish_wrapped = False
        self._u1_cancel_ui_context = False
        self._u1_cancel_requested = False
        self._critical_g28_original = None
        self._critical_g28_wrapped = False
        self._transport_safe_ticks = 0
        self._transport_deferred_ticks = 0
        self._deferred_tasks = deque()
        self._deferred_task_keys = set()
        self._deferred_task_max = 64
        self._deferred_task_timer = self.reactor.register_timer(
            self._drain_deferred_task, self.reactor.NEVER)
        self._deferred_task_runs = 0
        self._deferred_task_max_ms = 0.0
        self._deferred_task_overflows = 0

        self._critical_motion_depth = 0
        self._critical_motion_active = False
        self._critical_motion_pending = False
        self._critical_motion_started_at = 0.0
        self._critical_motion_entries = 0
        self._critical_motion_total_s = 0.0
        self._critical_motion_status_hits = 0
        self._critical_motion_errors = 0
        self._critical_motion_release_timer = self.reactor.register_timer(
            self._release_critical_motion, self.reactor.NEVER)
        self._critical_motion_reasons = []
        self._critical_motion_owners = {}
        self._pending_device_status = {}
        self._status_reconcile_coalesced = 0
        self._background_busy_deferrals = 0
        self._background_retry_interval = 0.250

        self._u1_background_poll_interval = 0.500
        self._background_idle_margin = 0.050
        self._critical_status_fallback = {
            'package_version': PACKAGE_VERSION,
            'package_build_id': self.package_build_id,
            'controller_mode': self.controller_mode,
            'debug': bool(self.debug_enabled),
            'controller_block_reason': self.controller_block_reason,
            'requested_controller_mode': self.requested_controller_mode,
            'tool_command_mode': self.tool_command_mode,
            'owns_filament_callbacks': False,
            'devices': [], 'endpoints': {}, 'u1_tip_profiles': {},
            'preferences': {}, 'tools': {}, 'print_tools': {},
            'print_map_active': False, 'print_plan_open': False,
            'active_tool': -1, 'loaded_tools': {}, 'loaded_tool': -1,
            'active_operations': {}, 'active_operation': None,
            'prestaged': {}, 'last_error': None,
            'printer_analysis': {}, 'performance': {
                'critical_motion_active': True},
            'orca': {}, 'refill': {},
        }

        self._migrate_u1_tip_profiles()
        self._load_endpoints()
        self._parse_devices(config.get('devices', ''))
        self._ensure_channel_tool_assignments()
        self.refill = AutoRefillController(self)
        saved_backups = session.get('backups', {}) if isinstance(session, dict) else {}
        if isinstance(saved_backups, dict):
            for source, items in saved_backups.items():
                try:
                    source_i = int(source)
                except (TypeError, ValueError):
                    continue
                if isinstance(items, list):
                    self.refill.print_backups[source_i] = [
                        dict(item) for item in items if isinstance(item, dict)]
        self._register_commands()
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

        self.printer.register_event_handler('print_stats:start', self._handle_print_started)
        self.printer.register_event_handler('print_stats:stop', self._handle_print_stopped)
        self.printer.register_event_handler(
            'pause_resume:cancel', self._handle_u1_cancel_ui_context)
        self.printer.register_event_handler(
            'homing:homing_move_begin', self._handle_critical_motion_begin)
        self.printer.register_event_handler(
            'homing:homing_move_end', self._handle_critical_motion_end)
        self._timer = self.reactor.register_timer(self._timer_event)

    def _state_key(self, device):
        return device.expected_uid or device.uid or device.name

    @staticmethod
    def _device_uid(device):
        return str(device.expected_uid or device.uid or '').upper()

    def _mapping_device(self, mapping):
        if not isinstance(mapping, dict):
            return None
        uid = str(mapping.get('device_uid', '') or '').upper()
        if uid:
            for device in self.devices:
                if self._device_uid(device) == uid:
                    return device
        return self.devices_by_name.get(mapping.get('device'))

    def _mapping_matches_device(self, mapping, device):
        uid = str(mapping.get('device_uid', '') or '').upper() if isinstance(mapping, dict) else ''
        if uid:
            return uid == self._device_uid(device)
        return isinstance(mapping, dict) and mapping.get('device') == device.name

    def _mapping_payload(self, device, channel):
        mapping = {'device': device.name, 'channel': int(channel)}
        uid = self._device_uid(device)
        if uid:
            mapping['device_uid'] = uid
        return mapping

    def _source_index(self, device, channel):
        try:
            device_index = self.devices.index(device)
        except ValueError:
            return -1
        channel = int(channel)
        if channel < 0 or channel > 3:
            return -1
        return device_index * 4 + channel

    def _snapmaker_platform(self):
        return bool(self.printer_analysis.get('features', {}).get('snapmaker_u1'))

    def _generic_external_endpoint(self):
        if self._snapmaker_platform():
            return None
        endpoint = self.endpoints.get('extruder')
        if endpoint is not None and endpoint.driver != 'snapmaker_u1':
            return endpoint
        generic = [candidate for candidate in self.endpoints.values()
                   if candidate.driver != 'snapmaker_u1']
        return generic[0] if len(generic) == 1 else None

    def _tool_assignment_owner(self, logical_tool, exclude=None):
        try:
            logical_tool = int(logical_tool)
        except (TypeError, ValueError, OverflowError):
            return None
        for candidate in self.devices:
            for candidate_channel in range(4):
                if (exclude is not None and candidate is exclude[0] and
                        candidate_channel == int(exclude[1])):
                    continue
                record = self._channel_record(candidate, candidate_channel)
                try:
                    assigned = int(record.get('logical_tool', -1))
                except (TypeError, ValueError, OverflowError):
                    assigned = -1
                if assigned == logical_tool:
                    return candidate, candidate_channel
        return None

    def _ensure_channel_tool_assignments(self):

        is_u1 = self._snapmaker_platform()
        minimum = U1_NATIVE_TOOL_COUNT if is_u1 else GENERIC_BMCU_TOOL_MIN
        limit = U1_LOGICAL_TOOL_LIMIT if is_u1 else GENERIC_LOGICAL_TOOL_LIMIT
        used = set()
        pending = []
        changed = False
        for device in self.devices:
            for channel in range(4):
                record = self._channel_record(device, channel)
                try:
                    tool = int(record.get('logical_tool', -1))
                except (TypeError, ValueError, OverflowError):
                    tool = -1
                if minimum <= tool < limit and tool not in used:
                    used.add(tool)
                else:
                    pending.append(record)
                    if tool != -1:
                        record['logical_tool'] = -1
                        changed = True
        available = [tool for tool in range(minimum, limit) if tool not in used]
        if len(available) < len(pending):
            mode = 'Snapmaker U1' if is_u1 else 'generic'
            raise self.config.error(
                'not enough %s logical tools for configured BMCU channels' % mode)
        for record, tool in zip(pending, available):
            try:
                current_tool = int(record.get('logical_tool', -1))
            except (TypeError, ValueError, OverflowError):
                current_tool = -1
            if current_tool != tool:
                record['logical_tool'] = tool
                changed = True
        if changed:
            self.state.save()
        return changed

    def _virtual_tool_for_source(self, device, channel):

        try:
            tool = int(self._channel_record(device, channel).get(
                'logical_tool', -1))
        except (TypeError, ValueError, OverflowError):
            return -1
        if self._snapmaker_platform():
            return tool if U1_NATIVE_TOOL_COUNT <= tool < U1_LOGICAL_TOOL_LIMIT else -1
        return tool if GENERIC_BMCU_TOOL_MIN <= tool < GENERIC_LOGICAL_TOOL_LIMIT else -1

    def device_identified(self, device, old_uid=''):

        new_key = str(device.expected_uid or device.uid or '').upper()
        if not new_key:
            return
        if (not re.match(r'^[0-9A-F]{24}$', new_key) or
                new_key in ('0' * 24, 'F' * 24)):
            message = 'invalid BMCU hardware UID %s reported by %s' % (new_key, device.name)
            device.last_error = message
            device.close()
            raise BMCUError(message)
        owner = self.devices_by_uid.get(new_key)
        if owner is not None and owner is not device:
            message = ('duplicate BMCU UID %s reported by %s and %s' %
                       (new_key, owner.name, device.name))
            device.last_error = message
            device.close()
            raise BMCUError(message)
        self.devices_by_uid[new_key] = device
        old_key = str(old_uid or device.name)
        devices = self.state.data.setdefault('devices', {})
        changed = False
        provisional = devices.get(old_key) if old_key != new_key else None
        existing = devices.get(new_key)
        if existing is None and isinstance(provisional, dict):
            devices[new_key] = provisional
            existing = provisional
            changed = True
        elif existing is None:
            existing = self.state.ensure_device(new_key, device.name, device.port)
            changed = True
        elif isinstance(provisional, dict):

            current_channels = existing.setdefault('channels', {})
            for channel in range(4):
                temporary = provisional.get('channels', {}).get(str(channel), {})
                if not isinstance(temporary, dict):
                    continue
                destination = current_channels.setdefault(str(channel), {})
                endpoint = str(temporary.get('endpoint', '') or '').strip()
                if endpoint and destination.get('endpoint') != endpoint:
                    destination['endpoint'] = endpoint
                    changed = True
                try:
                    temporary_tool = int(temporary.get('logical_tool', -1))
                    destination_tool = int(destination.get('logical_tool', -1))
                except (TypeError, ValueError, OverflowError):
                    temporary_tool = destination_tool = -1
                if (1 <= temporary_tool <= 255 and
                        not 1 <= destination_tool <= 255):
                    destination['logical_tool'] = temporary_tool
                    changed = True
        if provisional is not None:
            devices.pop(old_key, None)
            changed = True
        state = self.state.ensure_device(new_key, device.name, device.port)
        if state.get('name') != device.name:
            state['name'] = device.name
            changed = True
        if state.get('port') != device.port:
            state['port'] = device.port
            changed = True

        mapping_sets = [self.state.data.get('print_session', {}).get('tools', {}),
                        getattr(self, 'print_tools', {})]
        for mappings in mapping_sets:
            if not isinstance(mappings, dict):
                continue
            for mapping in mappings.values():
                if not isinstance(mapping, dict):
                    continue
                if (mapping.get('device') == device.name or
                        str(mapping.get('device_uid', '') or '').upper() == new_key):
                    if mapping.get('device') != device.name:
                        mapping['device'] = device.name
                        changed = True
                    if str(mapping.get('device_uid', '') or '').upper() != new_key:
                        mapping['device_uid'] = new_key
                        changed = True
        changed = self._ensure_channel_tool_assignments() or changed
        if changed:
            self._defer_state_save()

    def _device_state(self, device):
        pending = self._forget_pending.get(device.name)
        if isinstance(pending, dict) and isinstance(pending.get('shadow'), dict):
            return pending['shadow']
        key = self._state_key(device)
        return self.state.ensure_device(
            key, device.name, device.port, self.motion_defaults)

    def _device_loading_handoff_pct(self, device, endpoint=None, refill=False):
        motion = self._device_state(device).get('motion_config', {})
        explicit = motion.get('loading_handoff_pct')
        try:
            explicit = float(explicit)
        except (TypeError, ValueError, OverflowError):
            explicit = None
        if explicit is not None and math.isfinite(explicit):
            return max(60, min(98, int(round(explicit))))

        def legacy_value(item):
            if item is None:
                return None
            key = 'refill_contact_buffer_pct' if refill else 'contact_buffer_pct'
            fallback = item.get('contact_buffer_pct', self.contact_buffer_pct)
            try:
                value = float(item.get(key, fallback) or fallback)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(value):
                return None
            return max(60, min(98, int(round(value))))

        value = legacy_value(endpoint)
        if value is not None:
            return value

        legacy_values = set()
        for channel in range(4):
            value = legacy_value(self._endpoint_for_channel(device, channel))
            if value is not None:
                legacy_values.add(value)
        if len(legacy_values) == 1:
            return legacy_values.pop()
        return max(60, min(98, int(round(self.contact_buffer_pct))))

    def _device_loading_handoff_explicit(self, device):
        return 'loading_handoff_pct' in self._device_state(device).get(
            'motion_config', {})

    @staticmethod
    def _route_key(device, channel):
        return '%s:%d' % (device.name, int(channel))

    def _journal_route_key(self, device, channel):

        uid = self._device_uid(device)
        if (re.fullmatch(r'[0-9A-F]{24}', uid or '') is None or
                uid in ('0' * 24, 'F' * 24)):
            raise BMCUError(
                '%s has no stable hardware UID; route journalling is blocked' %
                device.name)
        return 'uid:%s:%d' % (uid, int(channel))

    def _journal_route_aliases(self, device, channel):
        return {self._journal_route_key(device, channel)}

    def _mark_print_route_loaded(self, device, channel, tool):

        try:
            tool = int(tool)
        except (TypeError, ValueError, OverflowError):
            return False
        if not self.print_map_active:
            return False
        mapping = self.print_tools.get(str(tool))
        if not isinstance(mapping, dict) or mapping.get('native'):
            return False
        try:
            mapping_channel = int(mapping.get('channel', -1))
            channel = int(channel)
        except (TypeError, ValueError, OverflowError):
            return False
        if (not self._mapping_matches_device(mapping, device) or
                mapping_channel != channel):
            return False
        route_key = self._journal_route_key(device, channel)
        aliases = self._journal_route_aliases(device, channel)
        changed = aliases.intersection(self.print_loaded_routes) != {route_key}
        self.print_loaded_routes.difference_update(aliases)
        self.print_route_journal_initialized = True
        self.print_loaded_routes.add(route_key)
        if changed:
            self._save_print_session()
        return changed

    def _unmark_print_route(self, device, channel):
        aliases = self._journal_route_aliases(device, channel)
        if not aliases.intersection(self.print_loaded_routes):
            return False
        self.print_loaded_routes.difference_update(aliases)
        if not self.print_loaded_routes:
            self.print_terminal_unload_pending = False
        self._save_print_session()
        return True

    def _channel_record(self, device, channel):
        if int(channel) < 0 or int(channel) > 3:
            raise BMCUError('BMCU channel must be within 0..3')
        return self._device_state(device)['channels'][str(int(channel))]

    def _channel_endpoint_name(self, device, channel):
        return str(self._channel_record(device, channel).get('endpoint', '') or '')

    def _endpoint_for_channel(self, device, channel):
        endpoint_name = self._channel_endpoint_name(device, channel)
        return self.endpoints.get(endpoint_name) if endpoint_name else None

    def _active_operation_owns_route(self, device, channel):

        operation = self.active_operations.get(device.name)
        if not isinstance(operation, dict):
            return False
        try:
            if int(operation.get('channel', -1)) == int(channel):
                return True
        except (TypeError, ValueError, OverflowError):
            pass
        endpoint_name = self._channel_endpoint_name(device, channel)
        if not endpoint_name:
            return False
        if str(operation.get('endpoint', '') or '') == endpoint_name:
            return True
        endpoints = operation.get('endpoints', [])
        return isinstance(endpoints, (list, tuple, set)) and endpoint_name in endpoints

    def _durable_tail_route(self, device, channel):

        channel = int(channel)
        actual_uid = self._device_uid(device)
        ownership = self.state.data.get('u1_ownership', {})
        if isinstance(ownership, dict):
            for endpoint_name, record in ownership.items():
                if not isinstance(record, dict) or not record.get('tail_detached'):
                    continue
                try:
                    record_channel = int(record.get('channel', -1))
                except (TypeError, ValueError, OverflowError):
                    continue
                if record_channel != channel:
                    continue
                expected_uid = str(record.get('device_uid', '') or '').upper()
                device_name = str(record.get('device', '') or '')
                if expected_uid and actual_uid:
                    matches = expected_uid == actual_uid
                else:
                    matches = device_name == device.name
                if not matches:
                    continue
                endpoint_name = str(endpoint_name or '')
                endpoint = self.endpoints.get(endpoint_name)
                assigned_name = self._channel_endpoint_name(device, channel)
                endpoint_compatible = bool(
                    endpoint is not None and
                    endpoint.driver == 'snapmaker_u1')
                try:
                    expected_head = int(record.get('head_index', -1))
                    actual_head = int(
                        endpoint.get('head_index', -1)
                        if endpoint is not None else -1)
                except (TypeError, ValueError, OverflowError):
                    expected_head = actual_head = -1
                if expected_head >= 0:
                    endpoint_compatible = bool(
                        endpoint_compatible and actual_head == expected_head)
                return {
                    'kind': 'u1',
                    'endpoint_name': endpoint_name,
                    'endpoint': endpoint,
                    'endpoint_compatible': endpoint_compatible,
                    'record': record,
                    'routed': bool(endpoint_compatible and
                                   assigned_name == endpoint_name),
                }

        record = self._channel_record(device, channel)
        if record.get('tail_detached'):
            endpoint_name = str(
                record.get('tail_endpoint') or
                self._channel_endpoint_name(device, channel) or '')
            endpoint = self.endpoints.get(endpoint_name)
            assigned_name = self._channel_endpoint_name(device, channel)
            return {
                'kind': 'generic',
                'endpoint_name': endpoint_name,
                'endpoint': endpoint,
                'record': record,
                'routed': bool(endpoint is not None and
                               assigned_name == endpoint_name),
            }
        return None

    @staticmethod
    def _route_states_from_status(status):
        raw = status.get('route_state', []) if isinstance(status, dict) else []
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            return [protocol.ROUTE_UNCERTAIN] * 4
        states = []
        for value in raw:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = protocol.ROUTE_UNCERTAIN
            if value not in (protocol.ROUTE_EMPTY, protocol.ROUTE_LOADED,
                              protocol.ROUTE_UNCERTAIN):
                value = protocol.ROUTE_UNCERTAIN
            states.append(value)
        return states

    def _route_state(self, device, channel, status=None):
        channel = int(channel)
        value = self._route_states_from_status(
            device.status if status is None else status)[channel]
        if (value == protocol.ROUTE_EMPTY and
                self._durable_tail_route(device, channel) is not None):

            return protocol.ROUTE_LOADED
        return value

    def _loaded_channels(self, device, status=None):
        return [channel for channel in range(4)
                if self._route_state(device, channel, status) ==
                protocol.ROUTE_LOADED]

    def _uncertain_channels(self, device, status=None):
        states = self._route_states_from_status(
            device.status if status is None else status)
        return [channel for channel, value in enumerate(states)
                if value == protocol.ROUTE_UNCERTAIN]

    def _routes_for_endpoint(self, endpoint_name, states=None):
        matches = []
        allowed = None if states is None else set(states)
        for device in self.devices:
            for channel in range(4):
                if self._channel_endpoint_name(device, channel) != endpoint_name:
                    continue
                route_state = self._route_state(device, channel)
                if allowed is not None and route_state not in allowed:
                    continue
                matches.append((device, channel, route_state))
        return matches

    @staticmethod
    def _load_pressure_runtime_supported(device):
        try:
            capabilities = int(device.caps.get('capabilities', 0) or 0)
        except (TypeError, ValueError, OverflowError, AttributeError):
            capabilities = 0
        return bool(
            device.firmware_compatible is True and
            capabilities & protocol.CAP_LOAD_PRESSURE_PCT)

    @staticmethod
    def _legacy_load_profile_for_pressure(pressure_pct):

        try:
            pressure = int(round(float(pressure_pct)))
        except (TypeError, ValueError, OverflowError):
            pressure = 82
        if pressure <= 75:
            return 2, 75.0
        if pressure >= 95:
            return 1, 95.0
        return 0, 90.0

    @staticmethod
    def _motion_wire_values(device_state):
        motion = device_state.get('motion_config', {})
        return {
            protocol.CONFIG_LOAD_PRESSURE_PCT: float(
                motion.get('load_pressure_pct', 82.0)),
            protocol.CONFIG_LOAD_SPEED_MMS: float(motion.get('load_speed_mms', 80.0)),
            protocol.CONFIG_PULL_SPEED_MMS: float(motion.get('pull_speed_mms', 80.0)),
            protocol.CONFIG_PULL_SPEED_END_MMS: float(motion.get('pull_speed_end_mms', 12.0)),
            protocol.CONFIG_JAM_TIMEOUT_MS: float(motion.get('jam_timeout_ms', 20000)),
            protocol.CONFIG_BEFORE_PULLBACK_TARGET_PCT: float(
                motion.get('before_pullback_target_pct', 40.0)),
        }

    @staticmethod
    def _channel_retract_lengths_m(device_state):
        channels = device_state.get('channels', {})
        values = []
        for channel in range(4):
            record = channels.get(str(channel), {})
            try:
                millimeters = float(record.get('unload_retract_mm', 200.0))
            except (TypeError, ValueError, OverflowError):
                millimeters = 200.0
            if not math.isfinite(millimeters):
                millimeters = 200.0
            values.append(max(10.0, min(2000.0, millimeters)) / 1000.0)
        return values

    @staticmethod
    def _channel_autoload_lengths_m(device_state):
        channels = device_state.get('channels', {})
        values = []
        for channel in range(4):
            record = channels.get(str(channel), {})
            try:
                millimeters = float(record.get('autoload_mm', 120.0))
            except (TypeError, ValueError, OverflowError):
                millimeters = 120.0
            if not math.isfinite(millimeters):
                millimeters = 120.0
            values.append(max(10.0, min(1000.0, millimeters)) / 1000.0)
        return values

    @staticmethod
    def _channel_autoload_runtime_supported(device):

        return bool(device.firmware_compatible is True)

    @staticmethod
    def _channel_retract_runtime_supported(device):
        try:
            capabilities = int(device.caps.get('capabilities', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            capabilities = 0
        return bool(
            device.firmware_compatible is True and
            capabilities & protocol.CAP_CHANNEL_RETRACT)

    @staticmethod
    def _led_preview_runtime_supported(device):
        try:
            capabilities = int(device.caps.get('capabilities', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            capabilities = 0
        return bool(
            device.firmware_compatible is True and
            capabilities & protocol.CAP_LED_PREVIEW)

    @staticmethod
    def _led_filament_preview_runtime_supported(device):
        try:
            capabilities = int(device.caps.get('capabilities', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            capabilities = 0
        return bool(
            device.firmware_compatible is True and
            capabilities & protocol.CAP_LED_FILAMENT_PREVIEW)

    def _effective_channel_autoload_mm(self, device, channel):
        if not self._channel_autoload_runtime_supported(device):
            return 120.0
        try:
            value = float(self._channel_metadata(device, channel).get(
                'autoload_mm', 120.0) or 120.0)
        except (TypeError, ValueError, OverflowError):
            value = 120.0
        if not math.isfinite(value):
            value = 120.0
        return max(10.0, min(1000.0, value))

    @staticmethod
    def _lighting_config(device_state):
        lighting = device_state.get('lighting')
        return copy.deepcopy(lighting if isinstance(lighting, dict)
                             else DEFAULT_LIGHTING)

    @classmethod
    def _system_led_rgb(cls, device_state):
        color = str(cls._lighting_config(device_state).get(
            'system_color', '#FFFFFF') or '#FFFFFF').upper()
        if not re.match(r'^#[0-9A-F]{6}$', color):
            color = '#FFFFFF'
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))

    def _prime_device_motion_cache(self, device):
        device.motion_config = self._motion_wire_values(self._device_state(device))

    def _device_runtime_policy(self, device):
        standalone = self.controller_mode == 'standalone'
        autonomous = (standalone and
                      device.name not in self._calibration_policy_suspended)
        return {
            'standalone': standalone,
            'autonomous_assist': autonomous,
            'autonomous_unload': autonomous,
        }

    def _send_device_runtime_config(
            self, device, channel_retract_override=None):

        device_state = self._device_state(device)
        logical_values = self._motion_wire_values(device_state)
        wire_values = dict(logical_values)
        if not self._load_pressure_runtime_supported(device):
            legacy_profile, effective_pressure = (
                self._legacy_load_profile_for_pressure(
                    logical_values[protocol.CONFIG_LOAD_PRESSURE_PCT]))
            wire_values[protocol.CONFIG_LOAD_PRESSURE_PCT] = float(
                legacy_profile)
            logical_values[protocol.CONFIG_LOAD_PRESSURE_PCT] = float(
                effective_pressure)
        retract_lengths = self._channel_retract_lengths_m(device_state)
        if channel_retract_override is not None:
            try:
                override_channel, override_mm = channel_retract_override
                override_channel = int(override_channel)
                override_mm = float(override_mm)
            except (TypeError, ValueError, OverflowError):
                raise BMCUError('invalid temporary Channel retract override')
            if override_channel < 0 or override_channel > 3:
                raise BMCUError('temporary Channel retract override is out of range')
            if not math.isfinite(override_mm) or not (10.0 <= override_mm <= 2000.0):
                raise BMCUError(
                    'temporary Channel retract override must be within 10..2000 mm')
            retract_lengths[override_channel] = override_mm / 1000.0
        device.runtime_sync(
            wire_values, self._system_led_rgb(device_state),
            self._device_runtime_policy(device), retract_lengths,
            self._channel_autoload_lengths_m(device_state))

        device.motion_config = dict(logical_values)

    def _sync_device_lighting(self, device):
        try:
            device.set_lighting(
                self._lighting_config(self._device_state(device)))
            device.lighting_runtime_error = ''
            return True
        except Exception as exc:

            device.lighting_runtime_error = str(exc)
            logging.warning('BMCU %s lighting sync deferred: %s',
                            device.name, exc)
            return False

    def _restore_device_runtime_policy(self, eventtime, device):
        self._calibration_policy_suspended.discard(device.name)
        if not device.ready or not device.connected:
            return
        try:
            self._send_device_runtime_config(device)
            self._sync_device_lighting(device)
        except Exception as exc:
            device.last_error = 'runtime policy restore failed: %s' % exc
            logging.exception('BMCU %s runtime policy restore failed', device.name)
            device.close()

    def _sync_device_runtime_config(self, eventtime, device):
        if device.ready and device.runtime_configured:
            device.runtime_config_sync_pending = False
            return
        if not device.connected or not device.hello_validated:
            device.runtime_config_sync_pending = False
            return
        try:
            self._send_device_runtime_config(device)
            device.runtime_configured = True
            device.runtime_config_sync_pending = False
            device.ready = True
            device.last_error = ''
            self._sync_device_lighting(device)

            device.slot_sync_required = True
            self._sync_device_slots(eventtime, device)

            self.device_status_changed(device, dict(device.status), device.status)
            self.device_connection_changed(
                device, True, was_ready=True, was_reconciled=True)
        except Exception as exc:
            device.runtime_configured = False
            device.runtime_config_sync_pending = False
            device.ready = False
            device.last_error = 'runtime config sync failed: %s' % exc
            logging.exception('BMCU %s runtime config sync failed', device.name)
            device.close()

    def _migrate_u1_tip_profiles(self):

        raw = self.state.data.get('u1_tip_profiles')
        raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        cleaned = {}
        endpoint_cleanup_changed = False

        default_profile = raw.get('default')
        if isinstance(default_profile, dict):
            try:
                cleaned['default'] = validate_u1_tip_profile(default_profile)
            except Exception as exc:
                logging.warning(
                    'BMCU discarded incompatible saved default U1 tip profile: %s',
                    exc)
        materials = {}
        saved_materials = raw.get('materials', {})
        if isinstance(saved_materials, dict):
            for name in sorted(saved_materials):
                try:
                    material = normalize_u1_material_name(name)
                    materials[material] = validate_u1_tip_profile(
                        saved_materials[name])
                except Exception as exc:
                    logging.warning(
                        'BMCU discarded incompatible U1 tip profile %s: %s',
                        name, exc)
        if materials:
            cleaned['materials'] = materials

        endpoints = self.state.data.get('endpoints', {})
        legacy_scripts = []
        for name in sorted(endpoints):
            values = endpoints.get(name)
            if not isinstance(values, dict):
                continue
            if str(values.get('driver', '') or '') == 'snapmaker_u1':
                script = str(values.get('u1_unload_gcode', '') or '').strip()
                if script and script not in legacy_scripts:
                    legacy_scripts.append(script)
            for key in ('u1_load_gcode', 'u1_unload_gcode',
                        'u1_wait_gcode', 'u1_parked_temp',
                        'u1_material_profiles'):
                if key in values:
                    values.pop(key, None)
                    endpoint_cleanup_changed = True

        if 'default' not in cleaned and len(legacy_scripts) == 1:
            candidate = u1_tip_profile_defaults()
            try:
                cleaned['default'] = validate_u1_tip_profile(candidate)
                logging.info(
                    'BMCU replaced the former unrestricted U1 unload program with the bounded four-variant default')
            except Exception as exc:
                logging.warning(
                    'BMCU skipped incompatible former U1 unload program: %s', exc)
        elif 'default' not in cleaned and len(legacy_scripts) > 1:
            logging.warning(
                'BMCU found conflicting former per-head U1 unload programs; package default is used')

        if (self.state.data.get('u1_tip_profiles') != cleaned or
                endpoint_cleanup_changed):
            self.state.data['u1_tip_profiles'] = cleaned
            self.state.save()

    def _u1_tip_profile_store(self):
        raw = self.state.data.get('u1_tip_profiles', {})
        raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
        package_default = u1_tip_profile_defaults()
        effective_default = copy.deepcopy(package_default)
        if isinstance(raw.get('default'), dict):
            effective_default = validate_u1_tip_profile(raw['default'])
        materials = {}
        saved_materials = raw.get('materials', {})
        if isinstance(saved_materials, dict):
            for name, profile in saved_materials.items():
                material = normalize_u1_material_name(name)
                materials[material] = validate_u1_tip_profile(profile)
        return {
            'package_default': package_default,
            'default': effective_default,
            'materials': materials,
            'default_is_custom': isinstance(raw.get('default'), dict),
        }

    def _u1_tip_timing_status(self):

        accelerations = []
        velocities = []
        heads = {}
        for name, endpoint in self.endpoints.items():
            if str(getattr(endpoint, 'driver', '') or '') != 'snapmaker_u1':
                continue
            try:
                extruder = endpoint._configured_extruder()
                acceleration = float(getattr(extruder, 'max_e_accel', 0.0) or 0.0)
                velocity = float(getattr(extruder, 'max_e_velocity', 0.0) or 0.0)
                head = int(endpoint._head())
            except Exception:
                continue
            if not math.isfinite(acceleration) or acceleration <= 0.0:
                continue
            if not math.isfinite(velocity) or velocity <= 0.0:
                continue
            accelerations.append(acceleration)
            velocities.append(velocity)
            heads[str(head)] = {
                'max_e_accel_mm_s2': acceleration,
                'max_e_velocity_mm_s': velocity,
            }
        return {
            'max_e_accel_mm_s2': min(accelerations) if accelerations else None,
            'max_e_velocity_mm_s': min(velocities) if velocities else None,
            'heads': heads,
            'estimate_model': 'u1_rest_to_rest_trapezoid',
        }

    def _u1_tip_profiles_status(self):
        error = ''
        try:
            store = self._u1_tip_profile_store()
        except Exception as exc:
            defaults = u1_tip_profile_defaults()
            store = {
                'package_default': defaults, 'default': defaults,
                'materials': {}, 'default_is_custom': False,
            }
            error = str(exc)
        return {
            'package_default': store['package_default'],
            'snapmaker_stock_movements': u1_tip_snapmaker_movement_defaults(),
            'default': store['default'],
            'materials': store['materials'],
            'default_is_custom': bool(store['default_is_custom']),
            'valid': not bool(error),
            'error': error,
            'temperature_mode_labels': {
                'default': 'Snapmaker material default',
                'project': 'Print target from G-code',
                'custom': 'Custom load and tip-forming temperature',
            },
            'timing': self._u1_tip_timing_status(),
        }

    def _u1_tip_profile_for_material(self, material):
        store = self._u1_tip_profile_store()
        try:
            name = normalize_u1_material_name(material)
        except Exception:
            name = ''
        overridden = bool(name and name in store['materials'])
        profile = store['materials'].get(name, store['default'])
        if self.debug_enabled:
            logging.info(
                'BMCU DEBUG U1 tip profile select material=%s source=%s '
                'temperature_mode=%s',
                name or 'UNKNOWN',
                ('material_override' if overridden else 'default'),
                str(profile.get('temperature_mode', 'project') or 'project'))
        return copy.deepcopy(profile)

    def _load_endpoints(self):
        previous = getattr(self, 'endpoints', {})
        changed = False
        configured = self.state.data.setdefault('endpoints', {})
        if not self._snapmaker_platform():
            for extruder_name in self.printer_analysis.get('extruders', ()):
                name = str(extruder_name or '').strip()
                if not name or name in configured or len(configured) >= MAX_STATE_ENDPOINTS:
                    continue
                configured[name] = presets.generic_single_extruder(name)[name]
                changed = True
        endpoints = {}
        for name, endpoint_config in configured.items():
            endpoint = create_endpoint(self, name, endpoint_config)
            old = previous.get(name)
            if old is not None:
                endpoint.runtime_sensor_restore = dict(old.runtime_sensor_restore)
                endpoint.sensor_restore = dict(old.sensor_restore)
            endpoints[name] = endpoint
        self.endpoints = endpoints
        if 'extruder' not in self.endpoints and not self._snapmaker_platform():
            configured['extruder'] = presets.generic_single_extruder('extruder')['extruder']
            self.endpoints['extruder'] = create_endpoint(
                self, 'extruder', configured['extruder'])
            changed = True
        if changed:
            self.state.save()

    def _parse_devices(self, raw):

        records = []
        configured_names = set()
        configured_ports = {}
        configured_uids = {}
        for line_number, line in enumerate(raw.replace(';', '\n').splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if len(records) >= MAX_STATE_DEVICES:
                raise self.config.error(
                    'BMCU device count exceeds the supported state limit of %d' %
                    MAX_STATE_DEVICES)
            parts = [part.strip() for part in line.split(',')]
            if len(parts) not in (2, 3):
                raise self.config.error(
                    'BMCU device line %d must be name,serial_path[,uid]' %
                    line_number)
            if any(not part for part in parts):
                raise self.config.error(
                    'BMCU device line %d contains an empty field; use - for an unknown UID' %
                    line_number)
            name, port = parts[0], parts[1]
            if not re.match(r'^[A-Za-z0-9_.-]+$', name):
                raise self.config.error(
                    'BMCU device name must use only letters, numbers, dot, dash or underscore: %s' % name)
            if name in configured_names:
                raise self.config.error('duplicate BMCU device name %s' % name)
            if any(ord(char) < 32 for char in port):
                raise self.config.error('invalid serial port for BMCU %s' % name)
            canonical_port = os.path.realpath(port) if os.path.exists(port) else port

            uid = ''
            if len(parts) > 2:
                uid_field = parts[2]
                if uid_field != '-':
                    if not re.match(r'^[0-9A-Fa-f]{24}$', uid_field):
                        raise self.config.error(
                            'UID for BMCU %s must be - or exactly 24 hexadecimal characters' % name)
                    uid = uid_field.upper()
                    if uid in ('0' * 24, 'F' * 24):
                        raise self.config.error(
                            'UID for BMCU %s cannot be all-zero or all-FF' % name)
                    previous_uid = configured_uids.get(uid)
                    if previous_uid is not None:
                        raise self.config.error(
                            'duplicate BMCU UID %s configured for %s and %s' %
                            (uid, previous_uid, name))

            previous_port = configured_ports.get(canonical_port)
            if previous_port is not None and (not uid or not previous_port[1]):
                raise self.config.error(
                    'serial hint %s is assigned to more than one BMCU without '
                    'hardware UIDs (%s and %s)' %
                    (port, previous_port[0], name))

            configured_names.add(name)
            configured_ports[canonical_port] = (name, uid)
            if uid:
                configured_uids[uid] = name
            records.append((name, port, uid))

        if (self.printer_analysis.get('features', {}).get('snapmaker_u1') and
                len(records) > (U1_LOGICAL_TOOL_LIMIT - U1_NATIVE_TOOL_COUNT) // 4):
            raise self.config.error(
                'Snapmaker U1 supports at most 7 BMCU devices: T0-T3 are native '
                'heads and T4-T31 provide 28 BMCU Channels')

        for name, port, uid in records:
            device = BMCUDevice(self, name, port, uid)
            if uid:
                self.device_identified(device, old_uid=name)
            self.devices.append(device)
            self.devices_by_name[name] = device
            self.devices_by_port[port] = device
            self._device_state(device)
            self._prime_device_motion_cache(device)

    def _register_commands(self):
        commands = {
            'BMCU_STATUS': self.cmd_STATUS,
            'BMCU_REFRESH': self.cmd_REFRESH,
            'BMCU_STOP': self.cmd_STOP,
            'BMCU_CALIBRATE': self.cmd_CALIBRATE,
            'BMCU_CALIBRATE_CANCEL': self.cmd_CALIBRATE_CANCEL,
            'BMCU_CALIBRATE_POINT': self.cmd_CALIBRATE_POINT,
            'BMCU_CALIBRATE_COMMIT': self.cmd_CALIBRATE_COMMIT,
            'BMCU_CALIBRATION_STATUS': self.cmd_CALIBRATION_STATUS,
            'BMCU_TEST_ENCODER': self.cmd_TEST_ENCODER,
            'BMCU_CHANNEL_AUTOLOAD': self.cmd_CHANNEL_AUTOLOAD,
            'BMCU_CHANNEL_RETRACT': self.cmd_CHANNEL_RETRACT,
            'BMCU_SET_FILAMENT': self.cmd_SET_FILAMENT,
            'BMCU_APPLY_PRESET': self.cmd_APPLY_PRESET,
            'BMCU_SET_ENDPOINT': self.cmd_SET_ENDPOINT,
            'BMCU_SET_PREFERENCES': self.cmd_SET_PREFERENCES,

            'BMCU_TIP_PROFILE': self.cmd_U1_GCODE,
            'BMCU_U1_GCODE': self.cmd_U1_GCODE,
            'BMCU_U1': self.cmd_U1_GCODE,

            'BMCU_APPLY_TIP_TEMP': self.cmd_APPLY_TIP_TEMP,
            'BMCU_APPLY_TIP_FAN': self.cmd_APPLY_TIP_FAN,
            'BMCU_SET_OUTPUT': self.cmd_SET_OUTPUT,
            'BMCU_SET_ROUTE': self.cmd_SET_OUTPUT,
            'BMCU_SNAP_FEEDER': self.cmd_SNAP_FEEDER,
            'BMCU_MAP_TOOL': self.cmd_MAP_TOOL,
            'BMCU_SET_TOOL': self.cmd_SET_TOOL,
            'BMCU_LOAD': self.cmd_LOAD,
            'BMCU_UNLOAD': self.cmd_UNLOAD,
            'BMCU_PRESTAGE': self.cmd_PRESTAGE,
            'BMCU_CLEAR_PRESTAGE': self.cmd_CLEAR_PRESTAGE,
            'BMCU_SAVE_STATE': self.cmd_SAVE_STATE,
            'BMCU_CLEAR_ERROR': self.cmd_CLEAR_ERROR,
            'BMCU_RECONCILE': self.cmd_RECONCILE,
            'BMCU_ROUTE_CONFIRM': self.cmd_ROUTE_CONFIRM,
            'BMCU_ROUTE_RECOVER': self.cmd_ROUTE_RECOVER,
            'BMCU_HEAD_CONFIRM_EMPTY': self.cmd_HEAD_CONFIRM_EMPTY,
            'BMCU_ROUTE_FEED': self.cmd_ROUTE_FEED,
            'BMCU_BUFFER_MODE': self.cmd_BUFFER_MODE,
            'BMCU_SETUP': self.cmd_SETUP,
            'BMCU_ANALYZE_PRINTER': self.cmd_ANALYZE_PRINTER,
            'BMCU_PRINT_BEGIN': self.cmd_PRINT_BEGIN,
            'BMCU_PRINT_REQUIRE': self.cmd_PRINT_REQUIRE,
            'BMCU_PRINT_MAP': self.cmd_PRINT_MAP,
            'BMCU_PRINT_COMMIT': self.cmd_PRINT_COMMIT,
            'BMCU_PRINT_PREEXTRUDE': self.cmd_PRINT_PREEXTRUDE,
            'BMCU_PRINT_END': self.cmd_PRINT_END,
            'BMCU_HANDOFF': self.cmd_HANDOFF,
            'BMCU_PRESSURE': self.cmd_PRESSURE,
            'BMCU_SPEED': self.cmd_SPEED,
            'BMCU_LED': self.cmd_LED,
            'BMCU_LIGHTING': self.cmd_LIGHTING,
            'BMCU_LIGHTING_PROFILE': self.cmd_LIGHTING_PROFILE,
            'BMCU_LED_PREVIEW': self.cmd_LED_PREVIEW,
            'BMCU_TOOL_CHANGE': self.cmd_TOOL_CHANGE,
            'BMCU_REFILL_STATUS': self.cmd_REFILL_STATUS,
            'BMCU_REFILL_NOW': self.cmd_REFILL_NOW,
            'BMCU_PRINT_REFILL': self.cmd_PRINT_REFILL,
            'BMCU_PREPARE_UNINSTALL': self.cmd_PREPARE_UNINSTALL,
            'BMCU_FORGET_DEVICE': self.cmd_FORGET_DEVICE,
            'BMCU_UPDATE_ACCESS': self.cmd_UPDATE_ACCESS,
            'BMCU_SNAP_CHECK': self.cmd_SNAP_CHECK,
            'BMCU_AUTO_FEED': self.cmd_SNAP_AUTO_FEED,
            'BMCU_REFILL_RESUME': self.cmd_SNAP_REFILL_RESUME,
        }
        for name, handler in commands.items():
            self.gcode.register_command(name, handler)

    def _require_standalone_operation(self, operation, gcmd=None):
        mode = getattr(self, 'controller_mode', 'standalone')
        if mode == 'standalone':
            return
        reason = getattr(self, 'controller_block_reason', '')
        message = (
            '%s is unavailable because BMCU motion ownership is blocked%s.' %
            (operation, (': ' + reason) if reason else ''))
        if gcmd is not None:
            raise gcmd.error(message)
        raise BMCUError(message)

    def _manual_refill_resume_needed(self):
        for endpoint in self.endpoints.values():
            if endpoint.driver not in ('generic_single_extruder', 'snapmaker_u1'):
                continue
            if bool(endpoint.get('tail_runout_enabled', True)):
                return True
        return False

    def _install_manual_refill_resume_wrapper(self):
        if self._manual_refill_resume_wrapped:
            return True
        handlers = getattr(self.gcode, 'ready_gcode_handlers', None)
        original = handlers.get('RESUME') if isinstance(handlers, dict) else None
        if original is None:
            self._record_error(
                'MANUAL_REFILL_RESUME_HOOK_UNAVAILABLE',
                details=('stock/custom RESUME handler is unavailable; pending '
                         'same-Channel BMCU refill cannot load before Resume'))
            return False
        removed = self.gcode.register_command('RESUME', None)
        if removed is None:
            self._record_error(
                'MANUAL_REFILL_RESUME_HOOK_UNAVAILABLE',
                details='RESUME could not be detached safely')
            return False
        self._manual_refill_resume_original = removed
        self.gcode.register_command(
            'RESUME', self._cmd_manual_refill_resume_wrapper,
            desc='Resume print after any pending same-Channel BMCU refill')
        self._manual_refill_resume_wrapped = True
        return True

    def _cmd_manual_refill_resume_wrapper(self, gcmd):
        original = self._manual_refill_resume_original
        if original is None:
            raise gcmd.error('printer RESUME handler is unavailable')
        pending = getattr(self.refill, 'manual_pending', None)
        if not isinstance(pending, dict):
            return original(gcmd)
        try:
            self.refill.resume_manual_refill(gcmd=gcmd)
        except Exception as exc:
            self._record_error(
                'MANUAL_REFILL_LOAD_FAILED',
                device=str(pending.get('device', '') or ''),
                channel=int(pending.get('channel', -1)),
                endpoint=str(pending.get('endpoint', '') or ''),
                phase='MANUAL_REFILL_LOAD', details=str(exc))
            raise gcmd.error(
                'BMCU manual refill is not ready: %s' % str(exc))
        return original(gcmd)

    def _handle_u1_cancel_ui_context(self, *args):

        self._u1_cancel_ui_context = True
        self._u1_cancel_requested = True

    def _install_u1_cancel_wrapper(self):
        if self._u1_cancel_wrapped:
            return True
        handlers = getattr(self.gcode, 'ready_gcode_handlers', None)
        original = handlers.get('CANCEL_PRINT') if isinstance(handlers, dict) else None
        if original is None:
            self._record_error(
                'U1_CANCEL_HOOK_UNAVAILABLE',
                details='stock CANCEL_PRINT handler is unavailable; cancel unload is disabled')
            return False
        removed = self.gcode.register_command('CANCEL_PRINT', None)
        if removed is None:
            self._record_error(
                'U1_CANCEL_HOOK_UNAVAILABLE',
                details='stock CANCEL_PRINT could not be detached safely')
            return False
        self._u1_cancel_original = removed
        self.gcode.register_command(
            'CANCEL_PRINT', self._cmd_u1_cancel_wrapper,
            desc='Cancel print with fail-closed BMCU U1 unload')
        self._u1_cancel_wrapped = True
        return True

    def _install_u1_replenish_wrapper(self):
        if self._u1_replenish_wrapped:
            return True
        handlers = getattr(self.gcode, 'ready_gcode_handlers', None)
        original = (handlers.get('INNER_AUTO_REPLENISH_FILAMENT')
                    if isinstance(handlers, dict) else None)
        if original is None:
            self._record_error(
                'U1_REPLENISH_HOOK_UNAVAILABLE',
                details='stock INNER_AUTO_REPLENISH_FILAMENT handler is unavailable')
            return False
        removed = self.gcode.register_command(
            'INNER_AUTO_REPLENISH_FILAMENT', None)
        if removed is None:
            self._record_error(
                'U1_REPLENISH_HOOK_UNAVAILABLE',
                details='stock INNER_AUTO_REPLENISH_FILAMENT could not be detached safely')
            return False
        self._u1_replenish_original = removed
        self.gcode.register_command(
            'INNER_AUTO_REPLENISH_FILAMENT',
            self._cmd_u1_replenish_wrapper)
        self._u1_replenish_wrapped = True
        return True

    def _cmd_u1_replenish_wrapper(self, gcmd):
        original = self._u1_replenish_original
        if original is None:
            raise gcmd.error(
                'stock INNER_AUTO_REPLENISH_FILAMENT handler is unavailable')
        if self.controller_mode != 'standalone':
            return original(gcmd)
        try:
            handled = self.refill.handle_u1_native_runout(gcmd)
        except Exception as exc:
            task, _config = self._u1_task_config()
            if task is not None:
                task.perform_auto_replenish = False
            self._record_error(
                'U1_MIXED_AUTO_REFILL_FAILED', phase='NATIVE_RUNOUT',
                details=str(exc))
            logging.exception('BMCU mixed U1 auto-refill failed')
            return None
        if handled:
            return None
        return original(gcmd)

    def _u1_explicit_cancel_can_unload(self, explicit_ui_cancel=False):
        if not self.print_loaded_routes:
            return False, 'no BMCU route was loaded by this print'
        state = self._print_state()
        if state not in ('printing', 'paused', 'pause'):
            return False, 'printer state %s is not an active cancel state' % (state or 'unknown')
        if self.active_operations:
            return False, 'a BMCU operation is already active'
        stats = self.printer.lookup_object('print_stats', None)
        details = getattr(stats, 'exception_details', {}) if stats is not None else {}
        if isinstance(details, dict) and details:
            return False, 'print has an active firmware exception'
        virtual_sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if virtual_sdcard is not None and not explicit_ui_cancel:
            if bool(getattr(virtual_sdcard, 'cmd_from_sd', False)):
                return False, 'CANCEL_PRINT originated inside the G-code worker'

            if (state == 'printing' and
                    getattr(virtual_sdcard, 'work_timer', None) is None and
                    getattr(virtual_sdcard, 'current_file', None) is not None):
                return False, 'virtual SD read/dispatch failure is being cancelled'
        route_index = {}
        for device in self.devices:
            for channel in range(4):
                for alias in self._journal_route_aliases(device, channel):
                    route_index[alias] = (device, channel)
        for key in self.print_loaded_routes:
            route = route_index.get(key)
            if route is None:
                return False, 'journal route %s is unavailable' % key
            device, channel = route
            if (not device.ready or not device.status_reconciled or
                    self._route_state(device, channel) != protocol.ROUTE_LOADED):
                return False, '%s is not a confirmed online LOADED route' % key
        return True, ('explicit U1 UI cancel with fully reconciled loaded routes'
                      if explicit_ui_cancel else
                      'explicit U1 cancel with fully reconciled loaded routes')

    def _cmd_u1_cancel_wrapper(self, gcmd):
        return self._cmd_u1_cancel_wrapper_quiesced(gcmd)

    def _cmd_u1_cancel_wrapper_quiesced(self, gcmd):
        original = self._u1_cancel_original
        if original is None:
            raise gcmd.error('stock CANCEL_PRINT handler is unavailable')
        explicit_ui_cancel = bool(self._u1_cancel_ui_context)
        self._u1_cancel_ui_context = False
        reconcile_endpoints = set()
        if explicit_ui_cancel:
            for device in self.devices:
                for channel in range(4):
                    if not (self._journal_route_aliases(device, channel) &
                            self.print_loaded_routes):
                        continue
                    endpoint = self._endpoint_for_channel(device, channel)
                    if endpoint is not None and endpoint.driver == 'snapmaker_u1':
                        reconcile_endpoints.add(endpoint.name)
            for job in self._u1_background_jobs.values():
                endpoint = job.get('endpoint') if isinstance(job, dict) else None
                if endpoint is not None and endpoint.driver == 'snapmaker_u1':
                    reconcile_endpoints.add(endpoint.name)
        background_error = None
        if self._u1_background_jobs:
            try:

                self._cancel_u1_background_jobs(
                    ('explicit U1 cancel' if explicit_ui_cancel else
                     'U1 cancel'), wait=True)
            except Exception as exc:
                background_error = exc
                self._record_error(
                    'U1_CANCEL_BACKGROUND_FAILED', phase='CANCEL_BACKGROUND',
                    details=str(exc))
                logging.exception(
                    'BMCU could not stop U1 background motion before stock cancel')
        if background_error is None:
            can_unload, reason = self._u1_explicit_cancel_can_unload(
                explicit_ui_cancel=explicit_ui_cancel)
        else:
            can_unload, reason = False, (
                'background BMCU motion did not stop cleanly: %s' %
                background_error)
        if can_unload:
            try:

                virtual_sdcard = self.printer.lookup_object(
                    'virtual_sdcard', None)
                pause_resume = self.printer.lookup_object(
                    'pause_resume', None)
                if (virtual_sdcard is not None and
                        getattr(virtual_sdcard, 'work_timer', None) is not None):
                    if pause_resume is None or not hasattr(
                            pause_resume, 'send_pause_command'):
                        raise BMCUError(
                            'pause_resume is unavailable before cancel unload')

                    pause_resume.send_pause_command()
                    if getattr(virtual_sdcard, 'work_timer', None) is not None:
                        raise BMCUError(
                            'virtual SD worker did not stop before cancel unload')
                    if not bool(getattr(pause_resume, 'sd_paused', False)):
                        raise BMCUError(
                            'pause_resume did not record the virtual SD pause')
                self._unload_all_loaded_for_print_end()
                self.print_terminal_unload_pending = bool(self.print_loaded_routes)
                self._save_print_session()
            except Exception as exc:

                self.print_terminal_unload_pending = bool(self.print_loaded_routes)
                self._save_print_session()
                self._record_error(
                    'U1_CANCEL_UNLOAD_FAILED', phase='CANCEL_UNLOAD',
                    details=str(exc))
                logging.exception('BMCU U1 cancel unload failed; stock cancel continues')
        else:
            logging.warning('BMCU skipped automatic U1 cancel unload: %s', reason)
        try:
            with self.printer_critical_section('u1_cancel_stock_transaction'):
                return original(gcmd)
        finally:
            try:
                if explicit_ui_cancel:

                    for endpoint_name in sorted(reconcile_endpoints):
                        self._schedule_endpoint_projection_clear(endpoint_name)
            finally:
                self._u1_cancel_requested = False

    def _control_plane_request_enter(self):
        if self._critical_motion_owners.get(greenlet.getcurrent(), 0):
            raise BMCUError(
                'BMCU communication was requested inside printer-critical motion')
        while (self._critical_control_plane_blocked() and
               not self._klippy_disconnecting):
            self.reactor.pause(self.reactor.monotonic() + 0.050)
        if self._klippy_disconnecting:
            return False
        self._control_plane_requests += 1
        return True

    def _control_plane_request_leave(self):
        self._control_plane_requests = max(
            0, self._control_plane_requests - 1)

    def _critical_control_plane_blocked(self):
        return bool(
            self._critical_motion_pending or
            self._critical_motion_active or
            self._critical_motion_depth)

    def _wait_background_control_plane(self, cancel_check=None,
                                       poll_interval=0.050):
        cancel_requested = False
        while self._critical_control_plane_blocked():
            if callable(cancel_check) and cancel_check():
                cancel_requested = True
            if self._klippy_disconnecting:
                raise BMCUError(
                    'Klipper disconnected while waiting for printer motion')
            self.reactor.pause(
                self.reactor.monotonic() + max(0.010, float(poll_interval)))
        if callable(cancel_check) and cancel_check():
            cancel_requested = True
        return cancel_requested

    def _enter_critical_motion(self, reason='printer_motion'):
        reason = str(reason or 'printer_motion')
        while self._critical_motion_pending and not self._klippy_disconnecting:
            self.reactor.pause(self.reactor.monotonic() + 0.010)
        if self._klippy_disconnecting:
            raise BMCUError('Klipper disconnected before printer motion')
        if not self._critical_motion_active:
            self._critical_motion_pending = True
            try:
                while self._control_plane_requests and not self._klippy_disconnecting:
                    self.reactor.pause(self.reactor.monotonic() + 0.010)
                if self._klippy_disconnecting:
                    raise BMCUError('Klipper disconnected before printer motion')
                if isinstance(self._status_cache, dict):
                    self._critical_status_fallback = self._status_cache
                self._critical_motion_active = True
                self._critical_motion_entries += 1
                self._critical_motion_started_at = self.reactor.monotonic()
                self.reactor.update_timer(self._deferred_task_timer, self.reactor.NEVER)
                self.reactor.update_timer(self._timer, self.reactor.NEVER)
                for device in self.devices:
                    try:
                        device.set_reactor_quiesced(True)
                    except Exception:
                        self._critical_motion_errors += 1
                        try:
                            device.close()
                        except Exception:
                            pass
            except Exception:
                self._release_critical_motion(self.reactor.monotonic())
                raise
            finally:
                self._critical_motion_pending = False
        self.reactor.update_timer(self._critical_motion_release_timer, self.reactor.NEVER)
        self._critical_motion_depth += 1
        self._critical_motion_reasons.append(reason)
        owner = greenlet.getcurrent()
        self._critical_motion_owners[owner] = self._critical_motion_owners.get(owner, 0) + 1

    def _leave_critical_motion(self, reason='printer_motion'):
        owner = greenlet.getcurrent()
        depth = self._critical_motion_owners.get(owner, 0)
        if depth > 1:
            self._critical_motion_owners[owner] = depth - 1
        else:
            self._critical_motion_owners.pop(owner, None)
        if self._critical_motion_reasons:
            self._critical_motion_reasons.pop()
        if self._critical_motion_depth > 0:
            self._critical_motion_depth -= 1
        if self._critical_motion_depth:
            return
        now = self.reactor.monotonic()
        try:
            self.reactor.update_timer(
                self._critical_motion_release_timer,
                now + self.critical_motion_release_delay)
        except Exception:
            self._critical_motion_errors += 1

    @contextmanager
    def printer_critical_section(self, reason='printer_motion'):
        self._enter_critical_motion(reason)
        try:
            yield
        finally:
            self._leave_critical_motion(reason)

    def _handle_critical_motion_begin(self, homing_move):
        self._enter_critical_motion('homing_or_probing')

    def _promote_critical_motion_handler(self):

        handlers = getattr(self.printer, 'event_handlers', None)
        callbacks = (handlers.get('homing:homing_move_begin')
                     if isinstance(handlers, dict) else None)
        if not isinstance(callbacks, list):
            return False
        target_func = getattr(self._handle_critical_motion_begin, '__func__', None)
        for index, callback in enumerate(tuple(callbacks)):
            if (getattr(callback, '__self__', None) is self and
                    getattr(callback, '__func__', None) is target_func):
                if index:
                    callbacks.insert(0, callbacks.pop(index))
                return True
        return False

    def _install_critical_g28_wrapper(self):
        if self._critical_g28_wrapped:
            return True
        handlers = getattr(self.gcode, 'ready_gcode_handlers', None)
        original = handlers.get('G28') if isinstance(handlers, dict) else None
        if original is None:
            return False
        removed = self.gcode.register_command('G28', None)
        if removed is None:
            return False
        self._critical_g28_original = removed
        self.gcode.register_command(
            'G28', self._cmd_critical_g28,
            desc='Home with BMCU completely detached from Klipper reactor')
        self._critical_g28_wrapped = True
        return True

    def _cmd_critical_g28(self, gcmd):
        original = self._critical_g28_original
        if original is None:
            raise gcmd.error('stock G28 handler is unavailable')
        with self.printer_critical_section('g28_full_transaction'):
            return original(gcmd)

    def _handle_critical_motion_end(self, homing_move):
        self._leave_critical_motion('homing_or_probing')

    def _release_critical_motion(self, eventtime):
        if self._klippy_disconnecting or self._critical_motion_depth:
            return self.reactor.NEVER
        if not self._critical_motion_active:
            return self.reactor.NEVER
        self._critical_motion_active = False
        self._critical_motion_reasons = []
        if self._critical_motion_started_at:
            self._critical_motion_total_s += max(
                0.0, self.reactor.monotonic() -
                self._critical_motion_started_at)
        self._critical_motion_started_at = 0.0
        now = self.reactor.monotonic()
        for device in self.devices:
            try:
                device.set_reactor_quiesced(False)
            except Exception:
                self._critical_motion_errors += 1
                try:
                    device.close()
                except Exception:
                    pass
        if self._deferred_tasks:
            self._arm_timer_earliest(
                self._deferred_task_timer,
                now + self.manager_work_yield_interval)
        self.reactor.update_timer(
            self._timer, now + self.manager_work_yield_interval)
        return self.reactor.NEVER

    def _printer_motion_busy(self, eventtime=None, margin=0.0):

        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return False
        now = self.reactor.monotonic() if eventtime is None else float(eventtime)
        try:
            if str(getattr(toolhead, 'special_queuing_state', '') or '') == 'Drip':
                return True
            print_time, estimated, lookahead_empty = toolhead.check_busy(now)
            queued = max(0.0, float(print_time) - float(estimated))
            return (not bool(lookahead_empty) or queued > float(margin))
        except Exception:

            return True

    def _printer_background_busy(self, eventtime=None):
        if self._critical_control_plane_blocked() or self._klippy_disconnecting:
            return True

        if self._required_transport_users > 0 or self.active_operations:
            return False
        return self._printer_motion_busy(
            eventtime, margin=self._background_idle_margin)

    def _printer_deferred_busy(self, eventtime=None):

        if self._print_state() in ('printing', 'paused', 'pause'):
            return True
        return self._printer_background_busy(eventtime)

    def _printer_transport_safe(self, eventtime=None):
        if self._critical_motion_active or self._klippy_disconnecting:
            return False

        if self._required_transport_users > 0 or self.active_operations:
            return True
        if self._print_state() in ('printing', 'paused', 'pause'):
            return False
        return not self._printer_motion_busy(eventtime, margin=0.0)

    def queue_device_status_changed(self, device, previous, current):
        key = str(device.name)
        pending = self._pending_device_status.get(key)
        if pending is None:
            self._pending_device_status[key] = (
                device, dict(previous), dict(current))
        else:

            self._pending_device_status[key] = (
                device, pending[1], dict(current))
            self._status_reconcile_coalesced += 1
        task_key = 'device-status-reconcile:%s' % key
        self._queue_deferred_task(
            task_key,
            lambda eventtime, name=key:
                self._process_queued_device_status(eventtime, name))

    def _process_queued_device_status(self, eventtime, device_name):
        pending = self._pending_device_status.pop(str(device_name), None)
        if pending is None:
            return
        device, previous, current = pending
        self.device_status_changed(device, previous, current)

    def _handle_ready(self):
        self._klippy_disconnecting = False
        self.controller_mode = 'standalone'
        self.controller_block_reason = ''
        self.printer_analysis = compat.analyze_printer(
            self.printer, self.controller_mode)
        is_u1 = bool(
            self.printer_analysis.get('features', {}).get('snapmaker_u1'))

        self._load_endpoints()
        if (isinstance(self.last_error, dict) and
                self.last_error.get('code') == 'FILAMENT_OWNER_CONFLICT'):
            self.last_error = None
            self._sync_status_cache_runtime()
        self.owns_filament_callbacks = True
        self.auto_channel_autoload = (
            self._auto_channel_autoload_requested and
            self.owns_filament_callbacks)
        self.register_t_commands = False
        self._ownership_finalized = True
        if is_u1:
            self._install_u1_cancel_wrapper()
            self._install_u1_replenish_wrapper()
        if self._manual_refill_resume_needed():
            self._install_manual_refill_resume_wrapper()
        self._promote_critical_motion_handler()
        self._install_critical_g28_wrapper()
        if self.u1_cross_refill_pending:
            self._record_error(
                'U1_BMCU_CROSS_REFILL_RECOVERY_REQUIRED',
                details=('BMCU cross-head refill stopped in phase %s; '
                         'automatic tool changes remain blocked until recovery') %
                        self.u1_cross_refill_pending.get('phase', 'unknown'))
        if is_u1:
            self._bind_u1_filament_detector()
        for endpoint in self.endpoints.values():
            if endpoint.driver != 'snapmaker_u1':
                continue
            routed = any(
                self._channel_endpoint_name(device, channel) == endpoint.name
                for device in self.devices for channel in range(4))
            if not routed and endpoint.get('u1_native_feeder_takeover', False):
                self._queue_deferred_task(
                    'restore-unrouted:%s' % endpoint.name,
                    lambda eventtime, ep=endpoint:
                    self._restore_unrouted_u1_feeder(eventtime, ep))
        if (self.print_map_active and not is_u1 and
                self._print_state() not in ('printing', 'paused', 'pause')):
            self._clear_print_session(
                'Klipper restart on generic printer',
                preserve_loaded=bool(self.print_loaded_routes))

        if (self._u1_original and self.print_transaction_phase in
                ('applying', 'restoring', 'recovery')):
            self._queue_deferred_task(
                'recover-u1-print-transaction',
                lambda eventtime: self._recover_u1_print_transaction())
        elif self.print_map_active and is_u1:
            self._queue_deferred_task(
                'reapply-u1-print-maps',
                lambda eventtime: self._reapply_u1_print_maps())
        now = self.reactor.monotonic()
        for index, device in enumerate(self.devices):
            device.next_connect_at = max(device.next_connect_at, now + index * self.connect_spread)

        self._u1_lease_dirty = True
        self.reactor.update_timer(self._timer, self.reactor.NOW)

    def _restore_unrouted_u1_feeder(self, eventtime, endpoint):
        if self._endpoint_routed(endpoint.name):
            return
        try:
            record = self._u1_ownership_record(endpoint.name)
            if record.get('persistent_hold'):
                if not self._release_u1_persistent_hold_if_safe(
                        endpoint, 'route detached and path confirmed empty',
                        close_generation=True):
                    return
            else:
                self._handoff_u1_to_native(
                    endpoint, 'route detached and path confirmed empty',
                    save=False, close_generation=True)
            endpoint.config['u1_native_feeder_takeover'] = False
            config = self.state.data.get('endpoints', {}).get(endpoint.name)
            if isinstance(config, dict):
                config['u1_native_feeder_takeover'] = False
            self.state.save()
        except Exception as exc:
            logging.exception(
                'BMCU could not restore unrouted U1 feeder %s', endpoint.name)
            self._record_error(
                'U1_NATIVE_FEEDER_RESTORE_FAILED', '', -1, endpoint.name,
                'READY', str(exc))

    def _bind_u1_filament_detector(self):
        if not self.owns_filament_callbacks:
            return False
        if self._u1_detector_callback_registered:
            return True
        detector = self.printer.lookup_object('filament_detect', None)
        register = getattr(detector, 'register_cb_2_update_filament_info', None)
        if not callable(register):
            return False
        register(self._u1_filament_metadata_updated)
        self._u1_detector_callback_registered = True
        return True

    def _u1_filament_metadata_updated(self, channel, info, is_clear=False):
        if not self.owns_filament_callbacks:
            return
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return
        for endpoint in tuple(self.endpoints.values()):
            if endpoint.driver != 'snapmaker_u1':
                continue
            try:
                if endpoint._head() != channel:
                    continue
                if not endpoint.native_feeder_takeover_enabled():
                    continue
                metadata = endpoint.runtime_filament_metadata()
            except Exception:
                logging.exception('BMCU could not inspect U1 RFID overwrite event')
                continue
            if metadata is None or endpoint.name in self._u1_projection_repairs:
                continue
            if not self._endpoint_has_loaded_or_active_route(endpoint.name):
                continue
            self._u1_projection_repairs.add(endpoint.name)
            self._queue_deferred_task(
                'u1-filament-projection:%s' % endpoint.name,
                lambda eventtime, name=endpoint.name:
                    self._repair_u1_filament_projection(eventtime, name))

    def _repair_u1_filament_projection(self, eventtime, endpoint_name):
        try:
            endpoint = self.endpoints.get(endpoint_name)
            if endpoint is None or endpoint.driver != 'snapmaker_u1':
                return
            if not self._endpoint_has_loaded_or_active_route(endpoint_name):
                return
            metadata = endpoint.runtime_filament_metadata()
            if metadata is None:
                return
            endpoint.sync_active_filament(metadata)
        except Exception as exc:
            logging.exception('BMCU could not repair U1 filament metadata after native RFID update')
            self._record_error(
                'U1_METADATA_REPAIR_FAILED', endpoint=endpoint_name,
                details=str(exc))
            self._safe_pause()
        finally:
            self._u1_projection_repairs.discard(endpoint_name)

    def _queue_deferred_task(self, key, callback, delay=None):

        if self._klippy_disconnecting:
            return False
        key = str(key or '')
        if key and key in self._deferred_task_keys:
            return False
        if len(self._deferred_tasks) >= self._deferred_task_max:
            self._deferred_task_overflows += 1
            logging.error('BMCU deferred task queue overflow at %s', key)
            self._record_error(
                'DEFERRED_TASK_QUEUE_OVERFLOW', details=key)
            self._safe_pause()
            return False
        if key:
            self._deferred_task_keys.add(key)
        self._deferred_tasks.append((key, callback))
        if self._critical_motion_active:

            return True
        now = self.reactor.monotonic()
        wait = (self.manager_work_yield_interval if delay is None
                else max(0.001, float(delay)))
        self._arm_timer_earliest(self._deferred_task_timer, now + wait)
        return True

    def _arm_timer_earliest(self, timer, waketime):

        waketime = float(waketime)
        current = getattr(timer, 'waketime', self.reactor.NEVER)
        try:
            current = float(current)
        except (TypeError, ValueError, OverflowError):
            current = self.reactor.NEVER
        if current <= waketime:
            return False
        self.reactor.update_timer(timer, waketime)
        return True

    def _drain_deferred_task(self, eventtime):
        if self._critical_motion_active:
            return self.reactor.NEVER
        if self._klippy_disconnecting:
            self._deferred_tasks.clear()
            self._deferred_task_keys.clear()
            return self.reactor.NEVER
        if not self._deferred_tasks:
            return self.reactor.NEVER
        if self._printer_deferred_busy(eventtime):
            self._background_busy_deferrals += 1
            return self.reactor.monotonic() + self._background_retry_interval
        key, callback = self._deferred_tasks.popleft()
        if key:
            self._deferred_task_keys.discard(key)
        started = self.reactor.monotonic()
        try:
            callback(eventtime)
        except Exception as exc:
            logging.exception('BMCU deferred task %s failed', key or '<unnamed>')
            self._record_error(
                'DEFERRED_TASK_FAILED', details='%s: %s' %
                (key or '<unnamed>', exc))
            self._safe_pause()
        finally:
            elapsed_ms = max(
                0.0, (self.reactor.monotonic() - started) * 1000.0)
            self._deferred_task_runs += 1
            self._deferred_task_max_ms = max(
                self._deferred_task_max_ms, elapsed_ms)
            if elapsed_ms >= self.callback_warning_ms:
                logging.warning(
                    'BMCU deferred task %s took %.3f ms',
                    key or '<unnamed>', elapsed_ms)
        if self._deferred_tasks:
            return self.reactor.monotonic() + self.manager_work_yield_interval
        return self.reactor.NEVER

    def _handle_disconnect(self):
        self._klippy_disconnecting = True

        try:
            self.reactor.update_timer(self._timer, self.reactor.NEVER)
            self.reactor.update_timer(
                self._critical_motion_release_timer, self.reactor.NEVER)
        except Exception:
            logging.exception('BMCU could not disable manager timer on disconnect')
        self._critical_motion_active = False
        self._critical_motion_depth = 0
        self._critical_motion_pending = False
        self._critical_motion_owners.clear()
        self._control_plane_requests = 0
        self._u1_lease_callback_scheduled = False
        self._status_cache = None
        self._deferred_tasks.clear()
        self._deferred_task_keys.clear()
        self._pending_device_status.clear()
        try:
            self.reactor.update_timer(
                self._deferred_task_timer, self.reactor.NEVER)
        except Exception:
            logging.exception('BMCU could not disable deferred task timer')
        self._reset_u1_lookahead(stop_jobs=False)

        for job in self._u1_background_jobs.values():
            job['cancelled'] = True
            job['cancel_reason'] = 'Klipper disconnect'
        for device in self.devices:
            if device.connected:
                try:
                    device.stop_all()
                except Exception:
                    logging.exception(
                        'BMCU could not stop %s during Klipper disconnect',
                        device.name)
        if self._state_save_pending:
            try:
                self.state.save()
                self._state_save_pending = False
            except Exception:
                logging.exception('BMCU could not flush deferred state on disconnect')
        for device in self.devices:
            device.close()
            shutdown = getattr(device, 'shutdown_reactor_work', None)
            if callable(shutdown):
                shutdown()
        writer = getattr(self, '_durable_writer', None)
        if writer is not None:
            writer.close()

    def _defer_state_save(self, delay=0.5):
        due = self.reactor.monotonic() + max(0.0, float(delay))
        if not self._state_save_pending or due < self._state_save_due:
            self._state_save_due = due
        self._state_save_pending = True

    def _flush_deferred_state(self, eventtime):
        if not self._state_save_pending or eventtime < self._state_save_due:
            return
        try:
            self.state.save()
            self._state_save_pending = False
            self._state_save_due = 0.0
        except Exception:
            logging.exception('BMCU deferred state save failed')
            self._state_save_due = eventtime + 2.0

    def _handle_print_started(self, *args):
        self._u1_cancel_requested = False
        self._print_session_seen_active = True
        self._last_print_state = 'printing'

        for device in self.devices:
            try:
                device.set_transport_paused(True)
            except Exception:
                logging.exception(
                    'BMCU %s could not enter print-silent transport mode',
                    device.name)
        suspended = [device.name for device in self.devices
                     if getattr(device, 'suspended', False)]
        if suspended:
            details = ('print started while firmware access owns: ' +
                       ', '.join(sorted(suspended)))
            self._record_error(
                'PRINT_STARTED_DURING_FIRMWARE_ACCESS',
                phase='firmware_access', details=details)
            self._safe_pause()

    def _handle_print_stopped(self, *args):
        self._u1_cancel_requested = False
        state = self._print_state()
        self._last_print_state = state or 'standby'
        if self._u1_original:

            self.print_stock_reset_observed = True

        if self.print_loaded_routes:
            self.print_terminal_unload_pending = True
        self._status_cache = None
        if self._critical_motion_active:

            self._queue_deferred_task(
                'print-stop-cleanup', self._finish_print_stopped_cleanup)
            return
        self._finish_print_stopped_cleanup(self.reactor.monotonic())

    def _finish_print_stopped_cleanup(self, eventtime):
        keep_loaded = bool(self.print_loaded_routes)
        if keep_loaded:
            self.print_terminal_unload_pending = True
        self._clear_print_session(
            'print_stats:stop event', preserve_loaded=keep_loaded)
        self._status_cache = None

        for device_name in list(self._pending_device_status):
            self._queue_deferred_task(
                'device-status-reconcile:%s' % device_name,
                lambda now, name=device_name:
                    self._process_queued_device_status(now, name))
        try:
            self.reactor.update_timer(
                self._timer, self.reactor.monotonic())
        except Exception:
            pass

    def _manager_needs_fast_tick(self, eventtime):
        if self.active_operations or self._autoload_pending:
            return True
        if any(not job.get('done') and not job.get('cancelled')
               for job in self._u1_background_jobs.values()):
            return True
        for device in self.devices:
            if getattr(device, 'suspended', False):
                continue
            if (device.connected and
                    (not device.ready or bool(device.pending) or
                     bool(device.tx_buffer) or
                     bool(getattr(device, '_decoded_packets', ())))):
                return True
        if (self._state_save_pending and
                self._state_save_due <= eventtime + self.manager_tick_interval):
            return True
        return False

    def _run_u1_lease_reconcile(self, eventtime):
        self._u1_lease_callback_scheduled = False
        if self._klippy_disconnecting:
            return
        if self._print_state() in ('printing', 'paused', 'pause'):

            self._u1_lease_dirty = True
            self._next_u1_lease_check = max(
                self._next_u1_lease_check, eventtime + 1.0)
            return
        started = self.reactor.monotonic()
        try:
            self._reconcile_u1_leases(eventtime)
            self._next_u1_lease_check = 0.0
        except Exception:
            self._u1_lease_dirty = True

            self._next_u1_lease_check = self.reactor.monotonic() + 2.0
            logging.exception(
                'BMCU U1 lease reconciliation failed; retry scheduled')
        finally:
            elapsed_ms = max(
                0.0, (self.reactor.monotonic() - started) * 1000.0)
            self._u1_lease_reconcile_runs += 1
            self._u1_lease_reconcile_max_ms = max(
                self._u1_lease_reconcile_max_ms, elapsed_ms)

    def _check_sidecar_ipc_outages(self, eventtime):

        for device in self.devices:
            if device.ipc_connected:
                continue
            started = float(getattr(device, 'ipc_outage_since', 0.0) or 0.0)
            if not started or getattr(device, 'ipc_outage_escalated', False):
                continue
            age = max(0.0, float(eventtime) - started)
            if age < self.sidecar_ipc_outage_timeout:
                continue
            unsafe_endpoints = []
            for channel in range(4):
                endpoint_name = self._channel_endpoint_name(device, channel)
                if not endpoint_name or endpoint_name in unsafe_endpoints:
                    continue
                endpoint = self.endpoints.get(endpoint_name)
                if (endpoint is not None and
                        endpoint.driver == 'snapmaker_u1' and
                        self._u1_device_endpoint_unsafe(
                            device, endpoint_name)):
                    unsafe_endpoints.append(endpoint_name)
            if not unsafe_endpoints:

                continue
            device.ipc_outage_escalated = True
            self._sidecar_ipc_outages_escalated += 1
            detail = (
                'local BMCU sidecar IPC unavailable for %.1f s; physical USB '
                'state was not declared offline by the sidecar' % age)
            for endpoint_name in unsafe_endpoints:
                self._set_u1_disconnect_hazard(
                    endpoint_name, device.name, 'sidecar_ipc', detail)
                self._set_u1_lease_state(
                    endpoint_name, 'safety_hold',
                    self._u1_disconnect_hazard_reason(endpoint_name),
                    source='sidecar_ipc_timeout')
            self._record_error(
                'SIDECAR_IPC_UNAVAILABLE', device=device.name,
                endpoint=unsafe_endpoints[0], details=detail,
                evidence={'outage_age_s': age,
                          'endpoints': list(unsafe_endpoints)})
            self._safe_pause(defer_to_virtual_sd=True)

    def _timer_event(self, eventtime):
        if self._klippy_disconnecting:
            return self.reactor.NEVER
        if self._critical_motion_active:

            return self.reactor.NEVER
        started = self.reactor.monotonic()
        self._manager_tick_count += 1
        self._check_sidecar_ipc_outages(eventtime)
        printer_busy = self._printer_background_busy(eventtime)
        try:

            if printer_busy:
                transport_safe = self._printer_transport_safe(eventtime)
                for device in self.devices:
                    device.set_transport_paused(not transport_safe)
                    if transport_safe:
                        device.tick(eventtime, transport_only=True)
                if transport_safe:
                    self._transport_safe_ticks += 1
                else:
                    self._transport_deferred_ticks += 1
                return (self.reactor.monotonic() +
                        self.transport_retry_interval)

            self._flush_deferred_state(eventtime)
            for device in self.devices:
                device.set_transport_paused(False)
                device.tick(eventtime)

            if (self._u1_lease_dirty and
                    self._print_state() not in ('printing', 'paused', 'pause') and
                    eventtime >= self._next_u1_lease_check and
                    not self._u1_lease_callback_scheduled):
                self._u1_lease_callback_scheduled = True
                queued = self._queue_deferred_task(
                    'u1-lease-reconcile', self._run_u1_lease_reconcile)
                if (not queued and
                        'u1-lease-reconcile' not in self._deferred_task_keys):

                    self._u1_lease_callback_scheduled = False

            for key in list(self._autoload_pending):
                if eventtime < self._autoload_retry_at.get(key, 0.0):
                    continue
                device = self.devices_by_name.get(key[0])
                if device is not None:
                    self._autoload_retry_at[key] = eventtime + 1.0
                    self._queue_deferred_task(
                        'autoload:%s:%d' % (device.name, int(key[1])),
                        lambda now, d=device, ch=key[1]:
                            self._auto_autoload_channel(now, d, ch))
                break
            if eventtime >= self._next_print_state_check:
                self._next_print_state_check = eventtime + 1.0
                self._check_print_lifecycle()
        finally:
            elapsed_ms = max(
                0.0, (self.reactor.monotonic() - started) * 1000.0)
            if elapsed_ms > self._manager_tick_max_ms:
                self._manager_tick_max_ms = elapsed_ms
            if elapsed_ms >= self.callback_warning_ms:
                self._manager_tick_overruns += 1
                logging.warning(
                    'BMCU manager tick took %.3f ms (warning %.3f ms)',
                    elapsed_ms, self.callback_warning_ms)
        fast = self._manager_needs_fast_tick(eventtime)
        if fast:
            self._manager_active_ticks += 1
            return self.reactor.monotonic() + self.manager_tick_interval
        self._manager_idle_ticks += 1
        return self.reactor.monotonic() + self.manager_idle_interval

    def _notify_refill_runout_once(self, device, channel, source_tool, endpoint):
        if endpoint is not None and endpoint.name in self._conflicted_endpoints:
            return False
        key = (device.name, int(channel))
        if key in self._refill_runout_latched:
            return False
        if endpoint is None or not bool(endpoint.get('tail_runout_enabled', True)):
            return False
        if not self.refill._is_printing():
            return False
        self._refill_runout_latched.add(key)
        self.refill.on_channel_empty(device, int(channel), int(source_tool), endpoint)
        return True

    def _scan_loaded_runouts(self):
        if not self.owns_filament_callbacks:
            return
        for device in self.devices:
            present = device.status.get('present', [0, 0, 0, 0])
            for channel in self._loaded_channels(device):
                key = (device.name, int(channel))
                if self._active_operation_owns_route(device, channel):
                    continue
                if bool(present[channel]):
                    self._refill_runout_latched.discard(key)
                    continue
                tool = self.loaded_tools.get(
                    self._route_key(device, channel),
                    self._tool_for_channel(device, channel))
                endpoint = self._endpoint_for_channel(device, channel)
                if endpoint is not None and tool is not None and int(tool) >= 0:
                    self._notify_refill_runout_once(
                        device, channel, int(tool), endpoint)

    def device_urgent_status_changed(self, device, previous, current):

        key = str(device.name)
        pending = self._pending_device_status.get(key)
        if pending is None:
            self._pending_device_status[key] = (
                device, dict(previous), dict(current))
        else:
            self._pending_device_status[key] = (
                device, pending[1], dict(current))
            self._status_reconcile_coalesced += 1

        previous_present = list(previous.get('present', [0, 0, 0, 0]))
        current_present = list(current.get('present', [0, 0, 0, 0]))
        previous_meters = list(previous.get('meters', [0.0] * 4))
        current_meters = list(current.get('meters', [0.0] * 4))
        previous_routes = self._route_states_from_status(previous)
        current_routes = self._route_states_from_status(current)
        if previous_routes != current_routes:
            self._u1_lease_dirty = True

        for channel in range(4):
            route_key = self._route_key(device, channel)
            operation_owns_route = self._active_operation_owns_route(
                device, channel)
            endpoint = self._endpoint_for_channel(device, channel)
            was_present = bool(previous_present[channel])
            is_present = bool(current_present[channel])
            if (endpoint is not None and
                    endpoint.driver == 'generic_single_extruder' and
                    was_present != is_present):
                if not is_present:
                    record = self._channel_metadata(device, channel)
                    record['path_measure_pending'] = True
                    self._clear_path_learning_observation(device, channel)
                    self._defer_state_save()
                else:
                    start_meters = (previous_meters[channel]
                                    if channel < len(previous_meters)
                                    else current_meters[channel])
                    self._begin_path_learning_observation(
                        device, channel, endpoint, start_meters)
            if is_present:
                self._refill_runout_latched.discard((device.name, channel))
            if (not operation_owns_route and
                    bool(previous_present[channel]) and
                    not bool(current_present[channel]) and
                    self._route_state(device, channel) == protocol.ROUTE_LOADED):
                tool = self.loaded_tools.get(
                    route_key, self._tool_for_channel(device, channel))
                endpoint = self._endpoint_for_channel(device, channel)
                if endpoint is not None and tool is not None and int(tool) >= 0:
                    self._notify_refill_runout_once(
                        device, channel, int(tool), endpoint)
            if (not operation_owns_route and
                    previous_routes[channel] != protocol.ROUTE_UNCERTAIN and
                    current_routes[channel] == protocol.ROUTE_UNCERTAIN and
                    route_key in self.print_loaded_routes):
                self._record_error(
                    'ROUTE_UNCERTAIN', device=device.name, channel=channel,
                    endpoint=self._channel_endpoint_name(device, channel),
                    details='active print route became UNCERTAIN')
                self._safe_pause(defer_to_virtual_sd=True)

        controller_fault_flags = int(current.get(
            'controller_fault_flags',
            int(current.get('error_flags', 0) or 0) & ~0x0002) or 0)
        if (bool(current.get('nvm_fault', False)) or
                int(current.get('nvm_bad_page_mask', 0) or 0) or
                controller_fault_flags):
            self._record_error(
                'DEVICE_FAULT', device=device.name,
                details='BMCU reported a persistent controller/NVM fault during printing',
                evidence={
                    'error_flags': int(current.get('error_flags', 0) or 0),
                    'controller_fault_flags': controller_fault_flags,
                    'route_uncertain_flag': bool(current.get(
                        'route_uncertain_flag', False)),
                    'nvm_fault': bool(current.get('nvm_fault', False)),
                    'nvm_bad_page_mask': int(current.get(
                        'nvm_bad_page_mask', 0) or 0),
                })
            self._safe_pause(defer_to_virtual_sd=True)

    def device_status_changed(self, device, previous, current):
        if self._print_state() not in ('printing', 'paused', 'pause'):
            self._status_cache = None
        lease_previous = (
            int(previous.get('session_id', 0) or 0),
            tuple(self._route_states_from_status(previous)),
            int(previous.get('connected_mask', 0) or 0),
            int(previous.get('encoder_io_mask', 0) or 0))
        lease_current = (
            int(current.get('session_id', 0) or 0),
            tuple(self._route_states_from_status(current)),
            int(current.get('connected_mask', 0) or 0),
            int(current.get('encoder_io_mask', 0) or 0))
        if lease_previous != lease_current:
            self._u1_lease_dirty = True
        if hasattr(device, 'runtime_configured') and not device.runtime_configured:
            if not device.runtime_config_sync_pending:
                device.runtime_config_sync_pending = True
                key = 'runtime-sync:%s' % device.name
                queued = self._queue_deferred_task(
                    key,
                    lambda eventtime, d=device:
                    self._sync_device_runtime_config(eventtime, d))
                if not queued and key not in self._deferred_task_keys:
                    device.runtime_config_sync_pending = False
            return
        force = (not getattr(device, 'status_reconciled', False) or
                 device.session_changed)
        if device.slot_sync_required:
            key = 'slot-sync:%s' % device.name
            self._queue_deferred_task(
                key,
                lambda eventtime, d=device:
                self._sync_device_slots(eventtime, d))

        transition_previous = device._manager_transition_fingerprint(previous)
        transition_current = device._manager_transition_fingerprint(current)
        auto_cal = current.get('auto_calibration', {})
        if not force and transition_previous == transition_current:
            if auto_cal.get('active'):
                operation = self.active_operations.get(device.name)
                if (isinstance(operation, dict) and
                        operation.get('type') == protocol.OP_BUFFER_CALIBRATION):
                    operation.update({
                        'progress': int(auto_cal.get('progress', 0)),
                        'stage': int(auto_cal.get('stage', 0)),
                        'done_mask': int(auto_cal.get('done_mask', 0)),
                        'selected_mask': int(auto_cal.get('selected_mask', 0)),
                    })
            return

        if auto_cal.get('active'):
            self.active_operations[device.name] = {
                'name': 'BUFFER_CALIBRATION', 'type': protocol.OP_BUFFER_CALIBRATION,
                'type_name': 'buffer_calibration', 'device': device.name,
                'channel': int(auto_cal.get('channel', 0xff)),
                'progress': int(auto_cal.get('progress', 0)),
                'stage': int(auto_cal.get('stage', 0)),
                'done_mask': int(auto_cal.get('done_mask', 0)),
                'selected_mask': int(auto_cal.get('selected_mask', 0)),
            }
        elif self.active_operations.get(device.name, {}).get('type') == protocol.OP_BUFFER_CALIBRATION:
            active_op_type = int(current.get('active_op_type', 0))
            active_op_state = int(current.get('active_op_state', protocol.OP_STATE_IDLE))
            if (active_op_type != protocol.OP_BUFFER_CALIBRATION or
                    active_op_state != protocol.OP_STATE_RUNNING):
                self.active_operations.pop(device.name, None)
                if device.name in self._calibration_policy_suspended:
                    self._queue_deferred_task(
                        'restore-runtime-policy:%s' % device.name,
                        lambda eventtime, d=device:
                            self._restore_device_runtime_policy(eventtime, d))

        previous_present = list(previous.get('present', [0, 0, 0, 0]))
        current_present = list(current.get('present', [0, 0, 0, 0]))
        previous_meters = list(previous.get('meters', [0.0] * 4))
        current_meters = list(current.get('meters', [0.0] * 4))
        previous_motion = list(previous.get('motion', [protocol.MOTION_IDLE] * 4))
        previous_encoder_mask = int(previous.get('encoder_io_mask', 0))
        current_encoder_mask = int(current.get('encoder_io_mask', 0))
        current_connected_mask = int(current.get('connected_mask', 0))
        previous_routes = self._route_states_from_status(previous)
        current_routes = self._route_states_from_status(current)
        state = self._device_state(device)
        channels = state['channels']

        touched_endpoints = set()
        for channel in range(4):

            key = (device.name, channel + 1)
            refill_key = (device.name, channel)
            route_key = self._route_key(device, channel)
            endpoint_name = self._channel_endpoint_name(device, channel)
            endpoint = self.endpoints.get(endpoint_name) if endpoint_name else None
            if endpoint_name:
                touched_endpoints.add(endpoint_name)
            was_present = bool(previous_present[channel])
            is_present = bool(current_present[channel])
            observation = self._autoload_observation.get(key)
            presence_changed = was_present != is_present
            encoder_changed = bool((previous_encoder_mask ^ current_encoder_mask) & (1 << channel))

            if force or presence_changed or encoder_changed or observation:
                if is_present:
                    self._refill_runout_latched.discard(refill_key)
                durable_tail = self._durable_tail_route(device, channel)
                tail_endpoint = (durable_tail.get('endpoint')
                                 if durable_tail else None)
                detached_reinsert = bool(
                    is_present and durable_tail is not None and
                    durable_tail.get('kind') == 'u1')
                reinsert_before_boundary = False
                if detached_reinsert:
                    if tail_endpoint is None:

                        reinsert_before_boundary = True
                    else:
                        boundary_cleared = self._u1_tail_sensor_boundary_cleared(
                            tail_endpoint, device, channel)
                        if not boundary_cleared:
                            try:
                                reinsert_before_boundary = (
                                    tail_endpoint.sensor_detected(
                                        'entry_sensor') is not False)
                            except Exception:
                                reinsert_before_boundary = True
                if detached_reinsert and reinsert_before_boundary:
                    self._autoload_observation.pop(key, None)
                    try:
                        device.stop_all()
                    except Exception:
                        logging.exception(
                            'BMCU could not stop premature detached-tail reinsert')
                    self._record_error(
                        'TAIL_REINSERT_CONFLICT', device=device.name,
                        channel=channel, endpoint=(
                            durable_tail.get('endpoint_name', '')
                            if durable_tail else ''),
                        details=(
                            'new filament was inserted before the old detached '
                            'tail cleared the Snapmaker head sensor; remove it '
                            'and wait for the safe follower boundary'))
                    if self._print_state() in ('printing', 'paused', 'pause'):
                        self._safe_pause(defer_to_virtual_sd=True)
                elif is_present and not was_present:

                    path_start = (previous_meters[channel]
                                  if channel < len(previous_meters)
                                  else current_meters[channel])
                    self._begin_path_learning_observation(
                        device, channel, endpoint, path_start)
                    self._autoload_observation[key] = {
                        'start_meters': float(path_start),
                        'started': self.reactor.monotonic(),
                    }
                    observation = self._autoload_observation[key]

                if is_present and observation and (current_encoder_mask & (1 << channel)):
                    moved_mm = abs(float(current_meters[channel]) - observation['start_meters']) * 1000.0
                    expected_mm = self._effective_channel_autoload_mm(
                        device, channel)
                    if moved_mm >= max(10.0, expected_mm * 0.75):
                        channels[str(channel)]['encoder_status'] = 'OK'
                        channels[str(channel)]['encoder_test'] = {
                            'ok': True, 'reason': 'observed_channel_autoload',
                            'measured_mm': moved_mm,
                        }
                        self._autoload_observation.pop(key, None)
                        self._defer_state_save()
                if was_present and not is_present:
                    source_tool = self.loaded_tools.get(
                        route_key, self._tool_for_channel(device, channel))
                    if (self.owns_filament_callbacks and endpoint is not None and
                            source_tool is not None and int(source_tool) >= 0 and
                            previous_routes[channel] == protocol.ROUTE_LOADED and
                            device.name not in self.active_operations and
                            previous_motion[channel] in (
                                protocol.MOTION_BEFORE_ON_USE,
                                protocol.MOTION_ON_USE)):
                        self._notify_refill_runout_once(
                            device, channel, int(source_tool), endpoint)
                if not is_present:
                    self._autoload_observation.pop(key, None)
                    self._clear_path_learning_observation(device, channel)
                    if (endpoint is not None and
                            endpoint.driver == 'generic_single_extruder'):
                        record = channels[str(channel)]
                        if not bool(record.get('path_measure_pending', False)):
                            record['path_measure_pending'] = True
                            self._defer_state_save()
                bit = 1 << channel
                if (current_connected_mask & bit) and not (current_encoder_mask & bit):
                    channels[str(channel)]['encoder_status'] = 'FAULT'
                    channels[str(channel)]['encoder_test'] = {'reason': 'encoder_io'}

            if not (force or previous_routes[channel] != current_routes[channel]):
                continue

            route_state = current_routes[channel]
            uncertain_expected = (
                device.name in self.active_operations or route_key in self.prestaged)
            if route_state == protocol.ROUTE_LOADED:
                self._uncertain_routes.discard(route_key)
                mapped_tool = self._tool_for_channel(device, channel)
                if endpoint is None:
                    self.loaded_tools.pop(route_key, None)
                    self._record_error(
                        'LOADED_CHANNEL_UNROUTED', device=device.name,
                        channel=channel, endpoint=endpoint_name,
                        details='loaded Channel is not assigned to a printer endpoint')
                    self._safe_pause()
                    continue
                if self.owns_filament_callbacks:
                    self._queue_deferred_task(
                        'restore-endpoint-owner:%s' % endpoint.name,
                        lambda eventtime, name=endpoint.name:
                            self._restore_endpoint_runtime_ownership(name))
                    if hasattr(endpoint, 'sync_active_filament'):
                        metadata = dict(self._channel_metadata(device, channel))
                        self._queue_deferred_task(
                            'sync-active-filament:%s' % endpoint.name,
                            lambda eventtime, ep=endpoint, meta=metadata:
                                ep.sync_active_filament(meta))
                if mapped_tool >= 0:
                    self.loaded_tools[route_key] = mapped_tool
                    self._resume_follow_after_reconcile(device, channel)
                    if not is_present and self.owns_filament_callbacks:
                        self._notify_refill_runout_once(
                            device, channel, int(mapped_tool), endpoint)
                else:
                    self.loaded_tools.pop(route_key, None)
                    if not is_present:
                        self._record_error(
                            'LOADED_CHANNEL_UNMAPPED', device=device.name,
                            channel=channel, endpoint=endpoint.name,
                            details='loaded Channel has no logical Tool mapping; assign a Tool before resuming')
                        self._safe_pause()
            elif route_state == protocol.ROUTE_EMPTY:
                durable_tail = self._durable_tail_route(device, channel)
                if durable_tail is not None:

                    if durable_tail.get('routed'):
                        self._uncertain_routes.discard(route_key)
                        mapped_tool = self._tool_for_channel(device, channel)
                        if mapped_tool >= 0:
                            self.loaded_tools[route_key] = mapped_tool
                        else:
                            self.loaded_tools.pop(route_key, None)
                    else:
                        self.loaded_tools.pop(route_key, None)
                        self._uncertain_routes.add(route_key)
                        self._record_error(
                            'TAIL_CHANNEL_UNROUTED', device=device.name,
                            channel=channel,
                            endpoint=durable_tail.get('endpoint_name', ''),
                            details=('detached tail still occupies the downstream '
                                     'route but its original Endpoint is missing '
                                     'or no longer assigned'))
                        self._safe_pause()
                    continue
                self._uncertain_routes.discard(route_key)
                self.loaded_tools.pop(route_key, None)
            else:
                self.loaded_tools.pop(route_key, None)
                if uncertain_expected:
                    self._uncertain_routes.discard(route_key)
                elif route_key not in self._uncertain_routes:
                    self._uncertain_routes.add(route_key)
                    self._record_error(
                        'ROUTE_UNCERTAIN', device=device.name, channel=channel,
                        endpoint=endpoint_name,
                        details='Power was lost or an operation stopped before this Channel route was committed')
                    self._safe_pause()

        self._reconcile_generic_pending_follower_commit()

        for endpoint_name in touched_endpoints:
            if self._halt_endpoint_route_conflict(endpoint_name):
                continue
            occupied = self._routes_for_endpoint(
                endpoint_name,
                (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN))
            if (not occupied and
                    not self._prestaged_devices_for_endpoint(endpoint_name) and
                    not self._u1_endpoint_transition_reserved(endpoint_name)):
                if self.owns_filament_callbacks:
                    self._schedule_endpoint_projection_clear(endpoint_name)

        if device.session_changed:
            was_controlling_filament = (
                device.name in self.active_operations or
                any(value != protocol.ROUTE_EMPTY for value in previous_routes) or
                any(value != protocol.ROUTE_EMPTY for value in current_routes))
            self._drop_prestage_record(device.name, release_sensor=True)
            if was_controlling_filament:
                self._record_error(
                    'DEVICE_RESET', device=device.name,
                    details='BMCU restarted while one or more Channel routes were active; physical states were reconstructed from flash')
                self._safe_pause()
            device.session_changed = False
        device.status_reconciled = True
        for endpoint_name in touched_endpoints:
            self._refresh_u1_disconnect_hazards(endpoint_name)
        if touched_endpoints:
            self._u1_lease_dirty = True

    def _resume_follow_after_reconcile(self, device, channel):
        if self.controller_mode != 'standalone':
            return False
        if device.name in self.active_operations:
            return False
        if self._print_state() not in ('printing', 'paused'):
            return False
        if channel < 0 or channel >= 4:
            return False
        if self._route_state(device, channel) != protocol.ROUTE_LOADED:
            return False
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            return False
        try:
            self._require_exclusive_endpoint_route(device, endpoint, channel)
            self._check_automatic_ready(device, channel)
            self._arm_u1_persistent_hold(
                endpoint, device, channel, 'resume BMCU buffer follow')
        except BMCUError as exc:
            self._record_error(
                'FOLLOW_NOT_RESUMED', device=device.name, channel=channel,
                endpoint=endpoint.name, details=str(exc))
            self._safe_pause()
            return False
        device.set_motion(channel, protocol.MOTION_ON_USE)
        return True

    def _refresh_calibration_after_operation(self, eventtime, device):
        if not device.ready:
            return
        for channel in range(4):
            try:
                device.calibration[channel] = device.calibration_get(channel)
            except Exception as exc:
                logging.info('BMCU calibration refresh failed for %s Channel %d: %s',
                             device.name, channel + 1, exc)

    def operation_finished(self, device, result):
        if result['type'] == protocol.OP_BUFFER_CALIBRATION:
            self.active_operations.pop(device.name, None)
            self.last_diagnostic = {'device': device.name, 'operation': result}
            self._queue_deferred_task(
                'refresh-calibration:%s' % device.name,
                lambda eventtime, d=device:
                    self._refresh_calibration_after_operation(eventtime, d))
            self._queue_deferred_task(
                'restore-runtime-policy:%s' % device.name,
                lambda eventtime, d=device:
                    self._restore_device_runtime_policy(eventtime, d))
            if not result.get('ok') and result.get('reason') != 'aborted':
                self._record_error(
                    'CALIBRATION_FAILED', device=device.name,
                    channel=result.get('channel', 0xff),
                    details=result.get('reason', 'automatic calibration failed'))
            return

        channel = int(result.get('channel', 0xff))
        if channel >= 4:
            self.last_diagnostic = {'device': device.name, 'operation': result}
            return
        state = self._device_state(device)
        channel_state = state['channels'][str(channel)]
        if result['type'] == protocol.OP_ENCODER_TEST:
            channel_state['encoder_status'] = 'OK' if result['ok'] else 'FAULT'
            channel_state['encoder_test'] = dict(result)
            self._defer_state_save()
        elif result['type'] in (
                protocol.OP_CHANNEL_AUTOLOAD,
                protocol.OP_FEED_TO_CONTACT,
                protocol.OP_FEED_DISTANCE):
            try:
                measured_mm = abs(float(result.get('measured_mm', 0.0) or 0.0))
            except (TypeError, ValueError, OverflowError):
                measured_mm = 0.0
            if result['ok'] and measured_mm >= 2.0:
                channel_state['encoder_status'] = 'OK'
                channel_state['encoder_test'] = dict(result)
                self._defer_state_save()
            elif result.get('reason') == 'encoder_io':
                channel_state['encoder_status'] = 'FAULT'
                channel_state['encoder_test'] = dict(result)
                self._defer_state_save()
        self.last_diagnostic = {'device': device.name, 'operation': result}

    @staticmethod
    def _distance_wait_timeout(device, millimeters, minimum_s):
        try:
            speed = float(device.motion_config.get(protocol.CONFIG_LOAD_SPEED_MMS, 80.0) or 80.0)
        except (TypeError, ValueError, AttributeError):
            speed = 80.0
        speed = max(1.0, speed)

        firmware_s = max(float(minimum_s), (float(millimeters) / speed) * 2.0 + 1.5)
        return min(302.0, firmware_s + 2.0)

    def _auto_autoload_channel(self, eventtime, device, channel):
        key = (device.name, channel + 1)
        clear_pending = True
        locked = False
        endpoint = None
        try:
            if key not in self._autoload_pending or not self.auto_channel_autoload:
                return
            if not device.ready or not device.status['present'][channel]:
                return
            if not (device.status.get('calibration_valid_mask', 0) & (1 << channel)):
                return
            if not (device.status.get('encoder_io_mask', 0) & (1 << channel)):
                return
            if int(device.caps.get('hardware_variant', 0)) == 1:
                return
            if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
                clear_pending = False
                return
            if any(int(value) != protocol.MOTION_IDLE for value in device.status.get('motion', [])):
                clear_pending = False
                return
            endpoint = self._endpoint_for_channel(device, channel)
            if endpoint is None:
                return
            if self._loaded_devices_for_endpoint(
                    endpoint.name,
                    excluded_route=self._route_key(device, channel)):
                clear_pending = False
                return
            try:
                self._lock(device, endpoint, 'CHANNEL_AUTOLOAD Channel %d' % (channel + 1), channel=channel)
                locked = True
            except BMCUError:
                clear_pending = False
                return
            if getattr(endpoint, 'driver', '') == 'snapmaker_u1':
                self._arm_u1_persistent_hold(
                    endpoint, device, channel,
                    'automatic channel insertion')
            autoload_mm = float(
                self._channel_metadata(device, channel).get('autoload_mm', 120.0))
            op_id = device.start_distance_operation(
                protocol.MSG_CHANNEL_AUTOLOAD, channel, autoload_mm)
            timeout_s = self._distance_wait_timeout(device, autoload_mm, 6.0)
            result = device.wait_for_op(op_id, timeout=timeout_s)
            if not result.get('ok'):
                self._record_error(
                    'CHANNEL_AUTOLOAD_FAILED', device.name, channel,
                    endpoint.name, 'CHANNEL_AUTOLOAD', result.get('reason', ''),
                    result)
        except Exception as exc:
            logging.exception('BMCU automatic Channel autoload failed')
            self._record_error('CHANNEL_AUTOLOAD_FAILED', device.name, channel,
                               endpoint.name if endpoint else '',
                               'CHANNEL_AUTOLOAD', str(exc))
        finally:
            if locked and endpoint is not None:
                self._unlock(device, endpoint)
            if clear_pending:
                self._autoload_pending.discard(key)
                self._autoload_retry_at.pop(key, None)
            else:
                self._autoload_retry_at[key] = self.reactor.monotonic() + 1.0

    def _performance_status(self):
        totals = {'devices': len(self.devices), 'ready_devices': 0, 'rx_bytes': 0,
                  'tx_bytes': 0, 'rx_packets': 0, 'tx_packets': 0,
                  'connects': 0, 'reconnects': 0, 'decode_errors': 0,
                  'packet_errors': 0, 'unknown_packets': 0, 'stale_packets': 0,
                  'manager_callback_errors': 0, 'pending_requests': 0,
                  'serial_hangups': 0, 'rx_budget_yields': 0,
                  'rx_queue_overflows': 0,
                  'notification_queue_overflows': 0,
                  'status_notifications_coalesced': 0,
                  'status_notifications_suppressed': 0,
                  'operation_notifications_coalesced': 0,
                  'max_rx_callback_ms': 0.0,
                  'max_manager_callback_ms': 0.0}
        per_device = {}
        for device in self.devices:
            stats = device.performance_status()
            per_device[device.name] = stats
            if device.ready:
                totals['ready_devices'] += 1
            for key in ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                        'connects', 'reconnects', 'decode_errors', 'packet_errors',
                        'unknown_packets', 'stale_packets',
                        'manager_callback_errors', 'pending_requests',
                        'serial_hangups', 'rx_budget_yields',
                        'rx_queue_overflows',
                        'notification_queue_overflows',
                        'status_notifications_coalesced',
                        'status_notifications_suppressed',
                        'operation_notifications_coalesced'):
                totals[key] += int(stats.get(key, 0))
            totals['max_rx_callback_ms'] = max(
                totals['max_rx_callback_ms'],
                float(stats.get('max_rx_callback_ms', 0.0)))
            totals['max_manager_callback_ms'] = max(
                totals['max_manager_callback_ms'],
                float(stats.get('max_manager_callback_ms', 0.0)))
        totals.update({
            'manager_tick_count': int(self._manager_tick_count),
            'manager_tick_max_ms': float(self._manager_tick_max_ms),
            'manager_tick_overruns': int(self._manager_tick_overruns),
            'manager_active_ticks': int(self._manager_active_ticks),
            'manager_idle_ticks': int(self._manager_idle_ticks),
            'status_cache_hits': int(self._status_cache_hits),
            'deferred_task_runs': int(self._deferred_task_runs),
            'deferred_task_max_ms': float(self._deferred_task_max_ms),
            'deferred_task_overflows': int(self._deferred_task_overflows),
            'deferred_task_depth': len(self._deferred_tasks),
            'critical_motion_active': bool(self._critical_motion_active),
            'critical_motion_pending': bool(self._critical_motion_pending),
            'critical_motion_depth': int(self._critical_motion_depth),
            'control_plane_requests': int(self._control_plane_requests),
            'critical_motion_entries': int(self._critical_motion_entries),
            'critical_motion_total_s': float(self._critical_motion_total_s),
            'critical_motion_status_hits': int(
                self._critical_motion_status_hits),
            'critical_motion_errors': int(self._critical_motion_errors),
            'critical_motion_release_delay_s': float(
                self.critical_motion_release_delay),
            'critical_motion_reasons': list(self._critical_motion_reasons),
            'transport_safe_ticks': int(self._transport_safe_ticks),
            'transport_deferred_ticks': int(self._transport_deferred_ticks),
            'transport_min_buffer_s': float(self.transport_min_buffer),
            'status_reconcile_coalesced': int(
                self._status_reconcile_coalesced),
            'background_busy_deferrals': int(
                self._background_busy_deferrals),
            'pending_device_status': int(len(self._pending_device_status)),
            'active_tick_interval_s': float(self.manager_tick_interval),
            'idle_tick_interval_s': float(self.manager_idle_interval),
            'rx_budget_bytes': int(self.rx_budget_bytes),
            'rx_budget_packets': int(self.rx_budget_packets),
            'rx_budget_ms': float(self.rx_budget_ms),
            'lookahead_scan_active': False,
            'lookahead_plan_entries': len(self._u1_toolchange_plan),
            'u1_planner_requests': int(self._u1_planner_requests),
            'u1_planner_failures': int(self._u1_planner_failures),
            'u1_planner_last_wait_ms': float(self._u1_planner_last_wait_ms),
            'u1_planner_max_wait_ms': float(self._u1_planner_max_wait_ms),
            'u1_planner_last_scan_ms': float(self._u1_planner_last_scan_ms),
            'u1_lease_watchdog_probes': int(
                self._u1_lease_watchdog_probes),
            'u1_lease_watchdog_changes': int(
                self._u1_lease_watchdog_changes),
            'u1_lease_watchdog_max_ms': float(
                self._u1_lease_watchdog_max_ms),
            'u1_lease_reconcile_runs': int(
                self._u1_lease_reconcile_runs),
            'u1_lease_reconcile_max_ms': float(
                self._u1_lease_reconcile_max_ms),
            'u1_lease_endpoint_max_ms': dict(
                self._u1_lease_endpoint_max_ms),
            'sidecar_ipc_resume_grace_s': float(
                self.sidecar_ipc_resume_grace),
            'sidecar_ipc_outage_timeout_s': float(
                self.sidecar_ipc_outage_timeout),
            'sidecar_ipc_outages_escalated': int(
                self._sidecar_ipc_outages_escalated),
        })
        writer = getattr(self, '_durable_writer', None)
        if writer is not None:
            totals['durable_writer'] = dict(writer.stats)
            totals['durable_writer']['queue_depth'] = writer.queue.qsize()
        totals['per_device'] = per_device
        return totals

    def _channel_state_name(self, device, channel, metadata):
        route_key = self._route_key(device, channel)
        route_state = self._route_state(device, channel)
        staged = self.prestaged.get(route_key)
        if route_state == protocol.ROUTE_UNCERTAIN:
            return 'uncertain'
        if route_state == protocol.ROUTE_LOADED:
            return ('active' if self.loaded_tools.get(route_key) == self.active_tool
                    else 'loaded')
        if staged:
            return 'prestaged'
        if metadata.get('encoder_status') == 'FAULT' or not metadata.get('encoder_io_ok'):
            return 'fault'
        if not metadata.get('present'):
            return 'empty'
        if metadata.get('calibration_valid') and metadata.get('encoder_status') == 'OK':
            return 'ready'
        return 'needs_setup'

    def _orca_status(self, devices):
        systems = []
        effective = self.print_tools if self.print_map_active else {}
        tool_index = {}
        for tool_text, mapping in effective.items():
            try:
                mapped_device = self._mapping_device(mapping)
                device_name = mapped_device.name if mapped_device is not None else str(mapping.get('device'))
                key = (device_name, int(mapping.get('channel', -1)))
                tool_index.setdefault(key, []).append(int(tool_text))
            except (TypeError, ValueError):
                continue
        for values in tool_index.values():
            values.sort()

        for device_info in devices:
            slots = []
            device_name = device_info['name']
            device = self.devices_by_name[device_name]
            for channel in device_info.get('channels', []):
                channel_index = int(channel['channel'])
                tool_ids = tool_index.get((device_name, channel_index), [])
                endpoint_name = str(channel.get('endpoint', '') or '')
                endpoint_cfg = self.state.data.get('endpoints', {}).get(endpoint_name, {})
                endpoint_driver = str(endpoint_cfg.get('driver', '')) if endpoint_name else ''
                endpoint_obj = self.endpoints.get(endpoint_name)
                endpoint_owner = (self._u1_owner_status(endpoint_obj)
                                  if endpoint_obj is not None and
                                  endpoint_obj.driver == 'snapmaker_u1'
                                  else {'owner': 'bmcu', 'reason': ''})
                slots.append({
                    'slot_id': str(channel_index),
                    'channel': channel_index,
                    'source': int(channel.get('source', channel_index)),
                    'virtual_tool': int(channel.get('virtual_tool', -1)),
                    'name': channel.get('name') or 'Channel %d' % (channel_index + 1),
                    'material': channel.get('material', ''),
                    'color': channel.get('color', '#FFFFFF'),
                    'vendor': channel.get('vendor', ''),
                    'profile_id': channel.get('profile_id', ''),
                    'spool_id': channel.get('spool_id'),
                    'temperature_min': int(channel.get('temperature_min', 0) or 0),
                    'temperature_max': int(channel.get('temperature_max', 0) or 0),
                    'subtype': channel.get('subtype', 'generic'),
                    'colors': list(channel.get('colors', [channel.get('color', '#FFFFFF')])),
                    'color_mode': int(channel.get('color_mode', 0) or 0),
                    'exists': bool(channel.get('present')),
                    'ready': bool(channel.get('present') and channel.get('calibration_valid') and
                                  channel.get('encoder_status') == 'OK' and channel.get('encoder_io_ok') and
                                  endpoint_owner.get('owner') == 'bmcu'),
                    'state': self._channel_state_name(device, channel_index, channel),
                    'logical_tools': sorted(tool_ids),
                    'refill_enabled': bool(channel.get('refill_enabled', True)),
                    'refill_group': channel.get('refill_group', ''),
                    'refill_priority': int(channel.get('refill_priority', channel_index)),
                    'endpoint': {
                        'name': endpoint_name,
                        'driver': endpoint_driver,
                        'head_index': int(endpoint_cfg.get('head_index', -1) or -1),
                        'owner': endpoint_owner.get('owner', 'unknown'),
                        'owner_reason': endpoint_owner.get('reason', ''),
                    },
                    'route_state': channel.get('route_state_name', 'EMPTY'),
                    'output_occupied': int(channel.get('route_state', protocol.ROUTE_EMPTY)) != protocol.ROUTE_EMPTY,
                })
            systems.append({
                'system_id': device_info.get('uid') or device_info['name'],
                'device': device_info['name'],
                'name': device_info['name'],
                'type': 'BMCU',
                'slot_count': 4,
                'online': bool(device_info.get('ready')),
                'loaded_channels': list(device_info.get('loaded_channels', [])),
                'uncertain_channels': list(device_info.get('uncertain_channels', [])),
                'slots': slots,
            })
        return {
            'schema_version': PRINT_PLAN_SCHEMA,
            'package_version': PACKAGE_VERSION,
            'package_build_id': self.package_build_id,
            'systems': systems,
            'print_job_id': self.print_job_id,
            'print_map_active': self.print_map_active,
            'print_tools': dict(self.print_tools),
            'print_plan_tools': dict(self.print_plan_tools),
            'print_plan_required': sorted(self.print_plan_required),
            'print_plan_open': bool(self.print_plan_open),
            'print_plan_interrupted': bool(self.print_plan_interrupted),
            'print_transaction_phase': self.print_transaction_phase,
            'print_loaded_routes': sorted(self.print_loaded_routes),
            'print_terminal_unload_pending': bool(
                self.print_terminal_unload_pending),
            'print_stock_reset_observed': bool(
                self.print_stock_reset_observed),
            'u1_cross_refill_pending': copy.deepcopy(
                self.u1_cross_refill_pending),
            'print_refill_backups': {
                str(tool): [dict(item) for item in items]
                for tool, items in getattr(self.refill, 'print_backups', {}).items()
            },
            'capabilities': {
                'live_material_sync': True,
                'per_print_tool_mapping': True,
                'stable_uid_channel_binding': True,
                'stable_virtual_tool_ids': True,
                'configurable_virtual_tool_ids': True,
                'native_u1_tools': ([0, 1, 2, 3]
                                    if self._snapmaker_platform() else []),
                'generic_external_tools': ([] if self._snapmaker_platform()
                                           else [GENERIC_EXTERNAL_TOOL]),
                'bmcu_virtual_tool_base': (U1_NATIVE_TOOL_COUNT
                                           if self._snapmaker_platform()
                                           else GENERIC_BMCU_TOOL_MIN),
                'logical_tool_limit_u1': U1_LOGICAL_TOOL_LIMIT,
                'logical_tool_limit_generic': GENERIC_LOGICAL_TOOL_LIMIT,
                'mapping_expectations': [
                    'endpoint', 'material', 'spool'],
                'per_channel_endpoint_routing': True,
                'independent_channel_outputs': True,
                'background_prestage': True,
                'multiple_devices': True,
                'auto_refill': True,
                'cross_endpoint_refill': True,
                'per_print_refill_mapping': True,
                'tail_handoff': True,
                'sensor_tail_handoff': True,
                'sensorless_learned_tail_handoff': True,
                'per_channel_route_length_learning': True,
                'u1_cross_head_sensor_handoff': True,
                'u1_native_head_fallback': False,
                'u1_runtime_feeder_leases': True,
                'atomic_print_plan_commit': True,
                'persistent_u1_live_reprint_transaction': True,
                'actual_loaded_route_journal': True,
                'single_prime_handshake': True,
                'u1_startup_head_prefill': True,
                'configurable_final_route_policy': True,
                'default_leave_final_filament_loaded': False,
                'crash_safe_route_state': True,
                'volatile_device_metadata': True,
            },
        }

    def _preferences_status(self):
        raw = self.state.data.get('preferences')
        raw = raw if isinstance(raw, dict) else {}
        return {
            'leave_final_filament_loaded': bool(
                raw.get('leave_final_filament_loaded', False)),
        }

    def _leave_final_filament_loaded(self):
        return bool(self._preferences_status()[
            'leave_final_filament_loaded'])

    def _runtime_status_overlay(self):
        return {
            'print_tools': dict(self.print_tools),
            'print_plan_tools': dict(self.print_plan_tools),
            'print_job_id': self.print_job_id,
            'print_map_active': bool(self.print_map_active),
            'print_plan_open': bool(self.print_plan_open),
            'print_plan_interrupted': bool(self.print_plan_interrupted),
            'print_plan_required': sorted(self.print_plan_required),
            'print_loaded_routes': sorted(self.print_loaded_routes),
            'print_terminal_unload_pending': bool(
                self.print_terminal_unload_pending),
            'print_transaction_phase': self.print_transaction_phase,
            'print_stock_reset_observed': bool(
                self.print_stock_reset_observed),
            'u1_cross_refill_pending': copy.deepcopy(
                self.u1_cross_refill_pending),
            'active_tool': self.active_tool,
            'loaded_tools': dict(self.loaded_tools),
            'loaded_tool': (next(iter(self.loaded_tools.values()))
                            if len(self.loaded_tools) == 1 else -1),
            'active_operations': dict(self.active_operations),
            'active_operation': next(iter(self.active_operations.values()), None),
            'last_error': self.last_error,
            'print_state': self._print_state(),
        }

    def _sync_status_cache_runtime(self):

        overlay = self._runtime_status_overlay()
        orca_overlay = {
            'print_job_id': overlay['print_job_id'],
            'print_map_active': overlay['print_map_active'],
            'print_tools': dict(overlay['print_tools']),
            'print_plan_tools': dict(overlay['print_plan_tools']),
            'print_plan_required': list(overlay['print_plan_required']),
            'print_plan_open': overlay['print_plan_open'],
            'print_plan_interrupted': overlay['print_plan_interrupted'],
            'print_transaction_phase': overlay['print_transaction_phase'],
            'print_loaded_routes': list(overlay['print_loaded_routes']),
            'print_terminal_unload_pending': overlay[
                'print_terminal_unload_pending'],
            'print_stock_reset_observed': overlay[
                'print_stock_reset_observed'],
            'u1_cross_refill_pending': copy.deepcopy(
                overlay['u1_cross_refill_pending']),
        }
        for target in (self._status_cache, self._critical_status_fallback):
            if not isinstance(target, dict):
                continue
            target.update(overlay)
            target['refill'] = self.refill.status()

            orca = target.get('orca')
            if isinstance(orca, dict):
                orca.update(orca_overlay)

    def _sync_channel_status_cache(self, device, channel):
        channel = int(channel)
        metadata = copy.deepcopy(self._channel_metadata(device, channel))
        patch = copy.deepcopy(metadata)
        patch.update({
            'virtual_tool': self._virtual_tool_for_source(device, channel),
            'slot': copy.deepcopy(device.slots[channel]),
            'autoload_runtime_supported':
                self._channel_autoload_runtime_supported(device),
            'channel_retract_supported':
                self._channel_retract_runtime_supported(device),
            'autoload_effective_mm':
                self._effective_channel_autoload_mm(device, channel),
        })
        for target in (self._status_cache, self._critical_status_fallback):
            if not isinstance(target, dict):
                continue
            for cached_device in target.get('devices', []):
                if str(cached_device.get('name', '') or '') != device.name:
                    continue
                channels = cached_device.get('channels', [])
                cached_channel = next((
                    value for value in channels
                    if int(value.get('channel', -1)) == channel), None)
                if cached_channel is None:
                    continue
                cached_channel.update(copy.deepcopy(patch))
                cached_channel['state'] = self._channel_state_name(
                    device, channel, cached_channel)

    def get_status(self, eventtime=None):
        print_silent = self._print_state() in ('printing', 'paused', 'pause')
        if self._critical_motion_active or print_silent:

            self._critical_motion_status_hits += 1
            if self._status_cache is not None:
                return self._status_cache
            return self._critical_status_fallback
        now = (self.reactor.monotonic() if eventtime is None
               else float(eventtime))
        cache_interval = (
            self.status_cache_interval if self._manager_needs_fast_tick(now)
            else self.status_cache_idle_interval)
        if (self._status_cache is not None and
                now - self._status_cache_at < cache_interval):
            self._status_cache_hits += 1
            return self._status_cache
        devices = []
        for device_index, device in enumerate(self.devices):
            device_state = self._device_state(device)
            raw_route_states = self._route_states_from_status(device.status)
            route_states = [
                self._route_state(device, channel)
                for channel in range(4)]
            channels = []
            for channel in range(4):
                metadata = dict(device_state['channels'][str(channel)])
                route_key = self._route_key(device, channel)
                durable_tail = self._durable_tail_route(device, channel)
                metadata.update({
                    'channel': channel,
                    'source': device_index * 4 + channel,
                    'virtual_tool': self._virtual_tool_for_source(device, channel),
                    'endpoint': self._channel_endpoint_name(device, channel),
                    'present': bool(device.status['present'][channel]),
                    'buffer_pct': device.status['buffer_pct'][channel],
                    'buffer_raw': device.status['buffer_raw'][channel],
                    'meters': device.status['meters'][channel],
                    'travel_meters': device.status.get(
                        'travel_meters', [0.0] * 4)[channel],
                    'motor_pwm': device.status['motor_pwm'][channel],
                    'motion': device.status['motion'][channel],
                    'calibration_valid': bool(device.status['calibration_valid_mask'] & (1 << channel)),
                    'calibration_capture_mask': device.status['calibration_capture_mask'][channel],
                    'encoder_io_ok': bool(device.status['encoder_io_mask'] & (1 << channel)),
                    'connected': bool(device.status['connected_mask'] & (1 << channel)),
                    'slot': device.slots[channel],
                    'calibration': device.calibration[channel],
                    'route_state': route_states[channel],
                    'raw_route_state': raw_route_states[channel],
                    'route_state_name': protocol.ROUTE_NAMES.get(
                        route_states[channel], 'UNCERTAIN'),
                    'loaded': route_states[channel] == protocol.ROUTE_LOADED,
                    'uncertain': route_states[channel] == protocol.ROUTE_UNCERTAIN,
                    'loaded_tool': self.loaded_tools.get(route_key, -1),
                    'tail_detached': bool(durable_tail),
                    'tail_endpoint': (durable_tail.get('endpoint_name', '')
                                      if durable_tail else
                                      str(metadata.get('tail_endpoint', '') or '')),
                    'tail_unrouted': bool(
                        durable_tail and not durable_tail.get('routed')),
                    'prestaged': dict(self.prestaged.get(route_key, {})),
                    'autoload_runtime_supported':
                        self._channel_autoload_runtime_supported(device),
                    'channel_retract_supported':
                        self._channel_retract_runtime_supported(device),
                    'autoload_effective_mm':
                        self._effective_channel_autoload_mm(device, channel),
                })
                metadata['state'] = self._channel_state_name(device, channel, metadata)
                channels.append(metadata)
            devices.append({
                'name': device.name, 'uid': device.uid, 'port': device.port,
                'transport_socket': device.socket_path,
                'connected': device.connected, 'ready': device.ready,
                'runtime_configured': device.runtime_configured,
                'update_suspended': bool(device.suspended),
                'suspend_reason': device.suspend_reason,
                'firmware': device.caps.get('firmware', ''),
                'firmware_compatible': device.firmware_compatible,
                'firmware_error': device.firmware_error,
                'channel_autoload_runtime_supported':
                    self._channel_autoload_runtime_supported(device),
                'channel_retract_supported':
                    self._channel_retract_runtime_supported(device),
                'load_pressure_runtime_supported':
                    self._load_pressure_runtime_supported(device),
                'session_id': device.status.get('session_id', 0),
                'auto_calibration': dict(device.status.get('auto_calibration', {})),
                'system_led_color': self._lighting_config(device_state).get(
                    'system_color', '#FFFFFF'),
                'lighting': self._lighting_config(device_state),
                'lighting_profile': str(
                    device_state.get('lighting_profile', 'DEFAULT') or 'DEFAULT'),
                'lighting_profiles': copy.deepcopy(
                    device_state.get('lighting_profiles', {}))
                    if isinstance(device_state.get('lighting_profiles'), dict)
                    else {},
                'led_preview_supported': self._led_preview_runtime_supported(device),
                'led_filament_preview_supported':
                    self._led_filament_preview_runtime_supported(device),
                'nvm_fault': bool(device.status.get('nvm_fault', False)),
                'nvm_bad_page_mask': int(device.status.get('nvm_bad_page_mask', 0)),
                'loaded_channels': [index for index, value in enumerate(route_states)
                                    if value == protocol.ROUTE_LOADED],
                'uncertain_channels': [index for index, value in enumerate(route_states)
                                       if value == protocol.ROUTE_UNCERTAIN],
                'route_state': list(route_states),
                'raw_route_state': list(raw_route_states),
                'route_state_name': [protocol.ROUTE_NAMES.get(value, 'UNCERTAIN')
                                     for value in route_states],
                'now_channel': device.status.get('now_channel', 0xff),
                'last_error': device.last_error,
                'lighting_runtime_error': device.lighting_runtime_error,
                'missed_events': device.missed_events,
                'motion_config': {
                    'loading_handoff_pct': self._device_loading_handoff_pct(device),
                    'loading_handoff_explicit': self._device_loading_handoff_explicit(device),
                    'load_pressure_pct': device.motion_config.get(
                        protocol.CONFIG_LOAD_PRESSURE_PCT, 82.0),
                    'load_speed_mms': device.motion_config.get(protocol.CONFIG_LOAD_SPEED_MMS),
                    'pull_speed_mms': device.motion_config.get(protocol.CONFIG_PULL_SPEED_MMS),
                    'pull_speed_end_mms': device.motion_config.get(protocol.CONFIG_PULL_SPEED_END_MMS),
                },
                'channels': channels,
            })
        endpoint_status = {}
        for name, endpoint in self.endpoints.items():
            values = dict(self.state.data.get('endpoints', {}).get(name, {}))
            values['capabilities'] = endpoint.capabilities()
            values['validation'] = endpoint.validate()
            values['assigned_channels'] = [
                {'device': device.name, 'channel': channel}
                for device in self.devices for channel in range(4)
                if self._channel_endpoint_name(device, channel) == name]
            values['occupied_channels'] = [
                {'device': device.name, 'channel': channel, 'state': protocol.ROUTE_NAMES.get(route, 'UNCERTAIN')}
                for device, channel, route in self._routes_for_endpoint(
                    name, (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN))]
            if endpoint.driver == 'snapmaker_u1':
                values['owner'] = self._u1_owner_status(endpoint)
            else:
                values['external_sensor_detected'] = endpoint.sensor_detected(
                    'entry_sensor')
            endpoint_status[name] = values
        u1_tip_profiles = (self._u1_tip_profiles_status()
                           if self.printer_analysis.get(
                               'features', {}).get('snapmaker_u1') else {})
        stable_tools = {}
        if self.printer_analysis.get('features', {}).get('snapmaker_u1'):
            for head in range(U1_NATIVE_TOOL_COUNT):
                stable_tools[str(head)] = {
                    'native': True, 'head': head,
                    'endpoint': 'u1_head%d' % head,
                }
        else:
            external_endpoint = self._generic_external_endpoint()
            stable_tools[str(GENERIC_EXTERNAL_TOOL)] = {
                'external': True,
                'endpoint': (external_endpoint.name
                             if external_endpoint is not None else ''),
            }
        for device in self.devices:
            for channel in range(4):
                tool = self._virtual_tool_for_source(device, channel)
                if tool < 0:
                    continue
                mapping = self._mapping_payload(device, channel)
                mapping['source'] = self._source_index(device, channel)
                mapping['virtual_tool'] = tool
                mapping['endpoint'] = self._channel_endpoint_name(device, channel)
                stable_tools[str(tool)] = mapping
        result = {
            'package_version': PACKAGE_VERSION,
            'package_build_id': self.package_build_id,
            'controller_mode': self.controller_mode,
            'debug': bool(self.debug_enabled),
            'controller_block_reason': self.controller_block_reason,
            'requested_controller_mode': self.requested_controller_mode,
            'tool_command_mode': self.tool_command_mode,
            'owns_filament_callbacks': self.owns_filament_callbacks,
            'firmware_update_power_pin': self.firmware_update_power_pin,
            'firmware_update_power_cut_active': bool(
                self._firmware_update_power_restore),
            'devices': devices,
            'lighting_default': copy.deepcopy(DEFAULT_LIGHTING),
            'endpoints': endpoint_status,
            'u1_tip_profiles': u1_tip_profiles,
            'preferences': self._preferences_status(),

            'tools': stable_tools,
            'print_tools': dict(self.print_tools),
            'print_job_id': self.print_job_id,
            'print_map_active': self.print_map_active,
            'print_plan_open': bool(self.print_plan_open),
            'print_plan_interrupted': bool(self.print_plan_interrupted),
            'print_plan_required': sorted(self.print_plan_required),
            'print_loaded_routes': sorted(self.print_loaded_routes),
            'print_terminal_unload_pending': bool(
                self.print_terminal_unload_pending),
            'print_transaction_phase': self.print_transaction_phase,
            'u1_cross_refill_pending': copy.deepcopy(
                self.u1_cross_refill_pending),
            'u1_prepared_heads': sorted(
                int(value) for value in self._u1_prepared_heads
                if int(value) in range(4)),
            'active_tool': self.active_tool,
            'loaded_tools': dict(self.loaded_tools),
            'loaded_tool': (next(iter(self.loaded_tools.values()))
                            if len(self.loaded_tools) == 1 else -1),
            'active_operations': dict(self.active_operations),
            'active_operation': next(iter(self.active_operations.values()), None),
            'prestaged': dict(self.prestaged),
            'last_error': self.last_error,
            'print_state': self._print_state(),
            'last_diagnostic': self.last_diagnostic,
            'printer_analysis': dict(self.printer_analysis),
            'performance': self._performance_status(),
            'orca': self._orca_status(devices),
            'refill': self.refill.status(),
        }

        self._status_cache = result
        self._status_cache_at = now

        self._critical_status_fallback = result
        return result

    def _require_device(self, gcmd, name=None):
        if name is None:
            name = gcmd.get('DEVICE', None) if gcmd else None
        if name:
            device = self.devices_by_name.get(name)
            if device is None:
                raise BMCUError('unknown BMCU device %s' % name)
            return device
        if len(self.devices) == 1:
            return self.devices[0]
        raise BMCUError('DEVICE is required')

    def _require_channel(self, gcmd):
        channel = gcmd.get_int('CHANNEL', minval=0, maxval=3)
        return channel

    @staticmethod
    def _finite_gcmd_float(gcmd, name, default=_GCODE_REQUIRED, **limits):
        if default is _GCODE_REQUIRED:
            value = gcmd.get_float(name, **limits)
        else:
            value = gcmd.get_float(name, default, **limits)
        if value is not None and not math.isfinite(value):
            raise gcmd.error('%s must be a finite number' % name)
        return value

    def _u1_endpoint_for_head(self, head):
        try:
            head = int(head)
        except (TypeError, ValueError, OverflowError):
            return None
        if head < 0 or head >= U1_NATIVE_TOOL_COUNT:
            return None
        for endpoint in self.endpoints.values():
            if (endpoint.driver == 'snapmaker_u1' and
                    int(endpoint.get('head_index', -1)) == head):
                return endpoint
        return None

    def _activate_u1_logical_tool(self, logical_tool, endpoint):
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            raise BMCUError('Snapmaker U1 endpoint is unavailable')
        logical_tool = int(logical_tool)
        head = int(endpoint.get('head_index', -1))
        if logical_tool < 0 or logical_tool >= U1_LOGICAL_TOOL_LIMIT:
            raise BMCUError(
                'Snapmaker U1 accepts logical tools T0-T31')
        if head < 0 or head >= U1_NATIVE_TOOL_COUNT:
            raise BMCUError('Snapmaker U1 endpoint has an invalid physical head')

        script = ('T%d' % logical_tool if self.print_map_active else
                  'T%d A0' % head)
        with self.printer_critical_section(
                'snapmaker_u1_logical_tool_select'):
            self.gcode.run_script_from_command(script)
            endpoint.verify_selected()
        self.active_tool = logical_tool
        self._save_runtime()
        return True

    def _effective_tool_mapping(self, tool):
        if self.print_map_active:
            mapping = self.print_tools.get(str(tool))
            if mapping:
                return mapping

        try:
            tool = int(tool)
        except (TypeError, ValueError, OverflowError):
            return None
        if self._snapmaker_platform():
            valid = U1_NATIVE_TOOL_COUNT <= tool < U1_LOGICAL_TOOL_LIMIT
        else:
            valid = GENERIC_BMCU_TOOL_MIN <= tool < GENERIC_LOGICAL_TOOL_LIMIT
        if valid:
            owner = self._tool_assignment_owner(tool)
            if owner is not None:
                return self._mapping_payload(owner[0], owner[1])
        return None

    def _resolve_tool(self, tool):
        mapping = self._effective_tool_mapping(tool)
        if not mapping:
            if (self.printer_analysis.get('features', {}).get('snapmaker_u1') and
                    0 <= int(tool) < U1_NATIVE_TOOL_COUNT):
                raise BMCUError(
                    'T%d is a native Snapmaker U1 head, not a BMCU source; '
                    'BMCU sources start at T4' % tool)
            raise BMCUError('T%d is not mapped to a BMCU channel' % tool)
        device = self._mapping_device(mapping)
        if device is None:
            raise BMCUError('mapped BMCU device %s is unavailable' %
                            (mapping.get('device_uid') or mapping.get('device')))
        channel = int(mapping.get('channel', -1))
        if channel < 0 or channel > 3:
            raise BMCUError('invalid channel mapping for T%d' % tool)
        endpoint_name = self._channel_endpoint_name(device, channel)
        if not endpoint_name:
            raise BMCUError(
                '%s Channel %d is not connected to a printer head/extruder' %
                (device.name, channel + 1))
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None:
            raise BMCUError(
                '%s Channel %d endpoint %s does not exist' %
                (device.name, channel + 1, endpoint_name))
        return device, channel, endpoint

    def _channel_metadata(self, device, channel):
        return self._device_state(device)['channels'][str(channel)]

    @staticmethod
    def _path_learning_key(device, channel):
        return (device.name, int(channel))

    def _begin_path_learning_observation(
            self, device, channel, endpoint, start_meters):
        if endpoint is None or endpoint.driver != 'generic_single_extruder':
            return False
        record = self._channel_metadata(device, int(channel))
        if not bool(record.get('path_measure_pending', False)):
            return False
        if not isinstance(getattr(
                self, '_path_learning_observation', None), dict):
            self._path_learning_observation = {}
        self._path_learning_observation[
            self._path_learning_key(device, channel)] = {
                'endpoint': endpoint.name,
                'start_meters': float(start_meters),
                'started': self.reactor.monotonic(),
                'device_uid': self._device_uid(device),
            }
        return True

    def _clear_path_learning_observation(self, device, channel):
        store = getattr(self, '_path_learning_observation', None)
        if not isinstance(store, dict):
            self._path_learning_observation = {}
            return None
        return store.pop(self._path_learning_key(device, channel), None)

    def _effective_channel_path_mm(self, device, channel, endpoint=None):
        channel = int(channel)
        endpoint = endpoint or self._endpoint_for_channel(device, channel)
        record = self._channel_metadata(device, channel)
        endpoint_name = endpoint.name if endpoint is not None else ''
        learned_endpoint = str(record.get('path_length_endpoint', '') or '')
        learned_source = str(record.get('path_length_source', 'none') or 'none')
        learned = float(record.get('path_length_mm', 0.0) or 0.0)
        if (learned > 0.0 and learned_endpoint == endpoint_name and
                learned_source == 'learned'):
            return learned
        return 0.0

    def _measure_path_calibration(self, device, channel, endpoint, arrival=None):

        if endpoint is None or endpoint.driver != 'generic_single_extruder':
            self._clear_path_learning_observation(device, channel)
            return None
        channel = int(channel)
        record = self._channel_metadata(device, channel)
        if not bool(record.get('path_measure_pending', False)):
            self._clear_path_learning_observation(device, channel)
            return None
        observation = self._clear_path_learning_observation(device, channel)
        measured = 0.0
        source = 'controller_contact'
        if (isinstance(observation, dict) and
                observation.get('endpoint') == endpoint.name and
                (not observation.get('device_uid') or
                 observation.get('device_uid') == self._device_uid(device))):
            try:
                measured = abs(
                    float(device.status['meters'][channel]) -
                    float(observation['start_meters'])) * 1000.0
                source = 'input_edge_to_load_complete'
            except (KeyError, TypeError, ValueError, IndexError):
                measured = 0.0

        if not (10.0 <= measured <= 5000.0):
            return None
        return {
            'sample_mm': round(measured, 2),
            'measurement': source,
            'endpoint': endpoint.name,
            'device_uid': self._device_uid(device),
        }

    def _commit_path_calibration(
            self, device, channel, endpoint, measurement,
            reason='successful load committed'):

        if (not isinstance(measurement, dict) or endpoint is None or
                endpoint.driver != 'generic_single_extruder'):
            return None
        if measurement.get('endpoint') != endpoint.name:
            return None
        if (measurement.get('device_uid') and
                measurement.get('device_uid') != self._device_uid(device)):
            return None
        channel = int(channel)
        record = self._channel_metadata(device, channel)
        if not bool(record.get('path_measure_pending', False)):
            return None
        try:
            measured = float(measurement.get('sample_mm', 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return None
        if not (10.0 <= measured <= 5000.0):
            return None
        keys = (
            'path_length_mm', 'path_length_endpoint',
            'path_length_source', 'path_measure_pending')
        snapshot = {key: record.get(key) for key in keys}
        record.update({
            'path_length_mm': round(measured, 2),
            'path_length_endpoint': endpoint.name,
            'path_length_source': 'learned',
            'path_measure_pending': False,
        })
        try:
            self.state.save()
        except Exception:
            record.update(snapshot)
            raise
        self.last_diagnostic = dict(self.last_diagnostic or {})
        self.last_diagnostic['path_calibration'] = {
            'device': device.name, 'channel': channel,
            'endpoint': endpoint.name,
            'path_length_mm': record['path_length_mm'],
            'sample_mm': round(measured, 2),
            'measurement': str(measurement.get('measurement', '') or ''),
            'reason': str(reason or '')[:160],
        }
        return dict(self.last_diagnostic['path_calibration'])

    def _record_path_calibration(
            self, device, channel, endpoint, arrival=None,
            reason='successful endpoint contact'):

        measurement = self._measure_path_calibration(
            device, channel, endpoint, arrival)
        return self._commit_path_calibration(
            device, channel, endpoint, measurement, reason=reason)

    def _generic_tail_detached_matches(self, endpoint, device, channel):
        if (endpoint is None or endpoint.driver == 'snapmaker_u1' or
                getattr(self, 'state', None) is None):
            return False
        try:
            record = self._channel_metadata(device, int(channel))
        except Exception:
            return False
        if not bool(record.get('tail_detached', False)):
            return False
        if str(record.get('tail_endpoint', '') or '') != endpoint.name:
            return False
        return True

    def _mark_generic_tail_detached(
            self, endpoint, device, channel,
            reason='BMCU input empty while downstream tail remains'):
        if endpoint is None or endpoint.driver == 'snapmaker_u1':
            return False
        channel = int(channel)
        record = self._channel_metadata(device, channel)
        changed = not self._generic_tail_detached_matches(
            endpoint, device, channel)
        record['tail_detached'] = True
        record['tail_endpoint'] = endpoint.name
        if changed:
            record['tail_follower_pending'] = False
            record['tail_follower_device'] = ''
            record['tail_follower_uid'] = ''
            record['tail_follower_channel'] = -1
            record['tail_follower_tool'] = -1
        self.state.save()
        logging.warning(
            'BMCU marked %s Channel %d forward-only on %s: %s',
            device.name, channel + 1, endpoint.name, reason)
        return changed

    def _arm_generic_follower_commit(
            self, endpoint, source_device, source_channel,
            follower_device, follower_channel, logical_tool=-1):
        if not self._generic_tail_detached_matches(
                endpoint, source_device, int(source_channel)):
            raise BMCUError(
                'generic detached-tail ownership is missing before follower commit')
        record = self._channel_metadata(source_device, int(source_channel))
        record['tail_follower_pending'] = True
        record['tail_follower_device'] = follower_device.name
        record['tail_follower_uid'] = self._device_uid(follower_device)
        record['tail_follower_channel'] = int(follower_channel)
        try:
            logical_tool = int(logical_tool)
        except (TypeError, ValueError, OverflowError):
            logical_tool = -1
        record['tail_follower_tool'] = logical_tool
        self.state.save()
        return True

    def _clear_generic_tail_detached(
            self, endpoint, device, channel,
            reason='verified follower captured'):
        if not self._generic_tail_detached_matches(
                endpoint, device, int(channel)):
            return False
        record = self._channel_metadata(device, int(channel))
        record.update({
            'tail_detached': False,
            'tail_endpoint': '',
            'tail_path_length_mm': 0.0,
            'tail_follower_pending': False,
            'tail_follower_device': '',
            'tail_follower_uid': '',
            'tail_follower_channel': -1,
            'tail_follower_tool': -1,
        })
        self._refill_runout_latched.discard((device.name, int(channel)))
        self.state.save()
        logging.info(
            'BMCU cleared generic detached tail on %s Channel %d: %s',
            device.name, int(channel) + 1, reason)
        return True

    def _finalize_generic_tail_handoff_after_follower(
            self, source_device, source_channel, endpoint,
            follower_device, follower_channel,
            reason='generic follower captured'):

        source_channel = int(source_channel)
        follower_channel = int(follower_channel)
        if not self._generic_tail_detached_matches(
                endpoint, source_device, source_channel):
            return False
        record = self._channel_metadata(source_device, source_channel)
        journal_uid = str(record.get('tail_follower_uid', '') or '').upper()
        actual_uid = self._device_uid(follower_device)
        try:
            journal_channel = int(record.get('tail_follower_channel', -1))
        except (TypeError, ValueError, OverflowError):
            journal_channel = -1
        if record.get('tail_follower_pending') and (
                journal_channel != follower_channel or
                (journal_uid and journal_uid != actual_uid) or
                (not journal_uid and str(
                    record.get('tail_follower_device', '') or '') !=
                    follower_device.name)):
            raise BMCUError(
                'generic follower does not match the durable journal')
        target_raw = self._route_states_from_status(
            follower_device.status)[follower_channel]
        target_present = follower_device.status.get('present', [False] * 4)
        if (target_raw != protocol.ROUTE_LOADED or
                follower_channel >= len(target_present) or
                not bool(target_present[follower_channel])):
            raise BMCUError(
                'generic follower is not positively LOADED/present')

        same_route = bool(
            source_device.name == follower_device.name and
            source_channel == follower_channel)
        source_raw = self._route_states_from_status(
            source_device.status)[source_channel]
        if same_route:
            if source_raw != protocol.ROUTE_LOADED:
                raise BMCUError(
                    'same-Channel generic follower did not restore LOADED')
        elif source_raw != protocol.ROUTE_EMPTY:
            source_present = source_device.status.get('present', [False] * 4)
            if (source_channel >= len(source_present) or
                    bool(source_present[source_channel])):
                raise BMCUError(
                    'exhausted generic source is not positively empty')
            source_device.mark_unloaded(source_channel)
            source_device.refresh()
            source_raw = self._route_states_from_status(
                source_device.status)[source_channel]
            if source_raw != protocol.ROUTE_EMPTY:
                raise BMCUError(
                    'exhausted generic source did not commit EMPTY')

        source_key = self._route_key(source_device, source_channel)
        follower_key = self._route_key(follower_device, follower_channel)
        try:
            source_logical_tool = int(self.loaded_tools.get(source_key, -1))
        except (TypeError, ValueError, OverflowError):
            source_logical_tool = -1
        try:
            logical_tool = int(record.get('tail_follower_tool', -1))
        except (TypeError, ValueError, OverflowError):
            logical_tool = -1
        if logical_tool < 0:
            logical_tool = source_logical_tool
        if logical_tool < 0:
            projected = []
            for tool_text, mapping in self.print_tools.items():
                if not isinstance(mapping, dict) or mapping.get('native'):
                    continue
                try:
                    tool_value = int(tool_text)
                    mapping_channel = int(mapping.get('channel', -1))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (tool_value >= 0 and mapping_channel == source_channel and
                        self._mapping_matches_device(mapping, source_device)):
                    projected.append(tool_value)
            projected = sorted(set(projected))
            if self.active_tool in projected:
                logical_tool = int(self.active_tool)
            elif len(projected) == 1:
                logical_tool = projected[0]
        if logical_tool < 0:
            raise BMCUError(
                'generic follower journal has no unambiguous logical tool; '
                'detached ownership remains pending')

        snapshot = {
            'record': copy.deepcopy(record),
            'loaded_tools': dict(self.loaded_tools),
            'print_tools': copy.deepcopy(self.print_tools),
            'print_map_active': bool(self.print_map_active),
            'active_tool': self.active_tool,
            'print_loaded_routes': set(self.print_loaded_routes),
            'print_route_journal_initialized': bool(
                self.print_route_journal_initialized),
            'print_terminal_unload_pending': bool(
                self.print_terminal_unload_pending),
            'print_session': copy.deepcopy(
                self.state.data.get('print_session', {})),
        }
        print_active = bool(
            self.print_map_active or
            self._print_state() in ('printing', 'paused', 'pause'))
        try:
            if not same_route:
                self.loaded_tools.pop(source_key, None)
                self.print_loaded_routes.difference_update(
                    self._journal_route_aliases(
                        source_device, source_channel))
            self.loaded_tools[follower_key] = logical_tool
            if print_active:
                self.print_tools[str(logical_tool)] = self._mapping_payload(
                    follower_device, follower_channel)
                self.print_map_active = True
                self.print_route_journal_initialized = True
                self.print_loaded_routes.add(
                    self._journal_route_key(
                        follower_device, follower_channel))
            if (self.active_tool in (logical_tool, source_logical_tool) or
                    same_route):
                self.active_tool = logical_tool
            if not self.print_loaded_routes:
                self.print_terminal_unload_pending = False
            record.update({
                'tail_detached': False,
                'tail_endpoint': '',
                'tail_path_length_mm': 0.0,
                'tail_follower_pending': False,
                'tail_follower_device': '',
                'tail_follower_uid': '',
                'tail_follower_channel': -1,
                'tail_follower_tool': -1,
            })
            if print_active:
                self._save_print_session()
            else:
                self.state.save()
        except Exception:
            record.clear()
            record.update(snapshot['record'])
            self.loaded_tools = snapshot['loaded_tools']
            self.print_tools = snapshot['print_tools']
            self.print_map_active = snapshot['print_map_active']
            self.active_tool = snapshot['active_tool']
            self.print_loaded_routes = snapshot['print_loaded_routes']
            self.print_route_journal_initialized = (
                snapshot['print_route_journal_initialized'])
            self.print_terminal_unload_pending = (
                snapshot['print_terminal_unload_pending'])
            self.state.data['print_session'] = snapshot['print_session']
            raise

        self._refill_runout_latched.discard(
            (source_device.name, source_channel))
        self._save_runtime()
        logging.info(
            'BMCU transferred generic detached tail from %s Channel %d to '
            '%s Channel %d: %s',
            source_device.name, source_channel + 1,
            follower_device.name, follower_channel + 1, reason)
        return True

    def _reconcile_generic_pending_follower_commit(self, endpoint=None):

        changed = False
        for source_device in self.devices:
            for source_channel in range(4):
                record = self._channel_metadata(source_device, source_channel)
                if not (record.get('tail_detached') and
                        record.get('tail_follower_pending')):
                    continue
                endpoint_name = str(record.get('tail_endpoint', '') or '')
                ep = self.endpoints.get(endpoint_name)
                if ep is None or ep.driver == 'snapmaker_u1':
                    continue
                if endpoint is not None and ep.name != endpoint.name:
                    continue
                try:
                    follower_channel = int(record.get(
                        'tail_follower_channel', -1))
                except (TypeError, ValueError, OverflowError):
                    continue
                follower = None
                follower_uid = str(
                    record.get('tail_follower_uid', '') or '').upper()
                for candidate in self.devices:
                    if (follower_uid and self._device_uid(candidate) ==
                            follower_uid) or (
                            not follower_uid and candidate.name ==
                            str(record.get('tail_follower_device', '') or '')):
                        follower = candidate
                        break
                if follower is None or not (0 <= follower_channel <= 3):
                    continue
                if self._channel_endpoint_name(
                        follower, follower_channel) != ep.name:
                    continue
                raw = self._route_states_from_status(
                    follower.status)[follower_channel]
                present = follower.status.get('present', [False] * 4)
                if (raw != protocol.ROUTE_LOADED or
                        follower_channel >= len(present) or
                        not bool(present[follower_channel])):
                    continue
                same = (follower.name == source_device.name and
                        follower_channel == source_channel)
                if not same:
                    source_raw = self._route_states_from_status(
                        source_device.status)[source_channel]
                    source_present = source_device.status.get(
                        'present', [False] * 4)
                    if source_raw == protocol.ROUTE_LOADED:
                        if (source_channel >= len(source_present) or
                                bool(source_present[source_channel])):
                            continue
                        try:
                            source_device.mark_unloaded(source_channel)
                            source_device.refresh()
                        except Exception as exc:
                            logging.warning(
                                'BMCU could not reconcile exhausted generic '
                                'source %s Channel %d: %s',
                                source_device.name, source_channel + 1, exc)
                            continue
                if self._finalize_generic_tail_handoff_after_follower(
                        source_device, source_channel, ep,
                        follower_device=follower,
                        follower_channel=follower_channel,
                        reason=('recovered crash-interrupted generic follower '
                                'commit')):
                    changed = True
        return changed

    def _tool_for_channel(self, device, channel):
        if self.print_map_active:
            for tool_text, mapping in self.print_tools.items():
                if not isinstance(mapping, dict):
                    continue
                try:
                    mapping_channel = int(mapping.get('channel', -1))
                    requested_channel = int(channel)
                    tool = int(tool_text)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (self._mapping_matches_device(mapping, device) and
                        mapping_channel == requested_channel):
                    return tool

        return self._virtual_tool_for_source(device, channel)

    def _source_by_index(self, source):

        source = int(source)
        if source < 0:
            return None, None
        device_index, channel = divmod(source, 4)
        if device_index >= len(self.devices):
            return None, None
        return self.devices[device_index], channel

    def _save_runtime(self):

        return

    def _save_print_session(self):
        self._sync_status_cache_runtime()
        if not hasattr(self, 'state'):
            return
        refill = getattr(self, 'refill', None)
        backups = getattr(refill, 'print_backups', {}) if refill is not None else {}
        self.state.data['print_session'] = {
            'schema': 1,
            'active': bool(getattr(self, 'print_map_active', False)),
            'plan_open': bool(getattr(self, 'print_plan_open', False)),
            'plan_schema': PRINT_PLAN_SCHEMA,
            'job_id': str(getattr(self, 'print_job_id', '') or ''),
            'tools': copy.deepcopy(getattr(self, 'print_tools', {})),
            'backups': {str(tool): copy.deepcopy(items)
                        for tool, items in backups.items()},
            'u1_map_backup': copy.deepcopy(getattr(self, '_u1_map_backup', {})),
            'u1_used_backup': copy.deepcopy(getattr(self, '_u1_used_backup', {})),
            'u1_end_unload_backup': copy.deepcopy(
                getattr(self, '_u1_end_unload_backup', {})),
            'u1_original': copy.deepcopy(
                getattr(self, '_u1_original', {})),
            'transaction_phase': str(getattr(
                self, 'print_transaction_phase', '') or ''),
            'loaded_routes': sorted(getattr(
                self, 'print_loaded_routes', set())),
            'route_journal_initialized': bool(getattr(
                self, 'print_route_journal_initialized', False)),
            'terminal_unload_pending': bool(getattr(
                self, 'print_terminal_unload_pending', False)),
            'stock_reset_observed': bool(getattr(
                self, 'print_stock_reset_observed', False)),
            'u1_prepared_heads': sorted(
                int(value) for value in getattr(
                    self, '_u1_prepared_heads', set())
                if int(value) in range(4)),
            'u1_cross_refill_pending': copy.deepcopy(getattr(
                self, 'u1_cross_refill_pending', {})),
        }
        self.state.save()

    @staticmethod
    def _ensure_u1_runtime_list(config, key, length, default_factory):
        values = config.get(key)
        if not isinstance(values, list):
            values = []
        while len(values) < length:
            values.append(default_factory(len(values)))
        config[key] = values
        return values

    def _u1_task_config(self):
        task = self.printer.lookup_object('print_task_config', None)
        config = getattr(task, 'print_task_config', None) if task is not None else None
        if not isinstance(config, dict):
            return None, None
        return task, config

    @staticmethod
    def _u1_reprint_info(config):
        reprint = config.get('reprint_info')
        if not isinstance(reprint, dict):
            raise BMCUError(
                'Snapmaker U1 reprint_info is unavailable or malformed')
        return reprint

    def _ensure_u1_dual_runtime_list(self, config, key, length,
                                     default_factory):
        live = self._ensure_u1_runtime_list(
            config, key, length, default_factory)
        reprint = self._u1_reprint_info(config)
        saved = self._ensure_u1_runtime_list(
            reprint, key, length, default_factory)
        return live, saved

    def _capture_u1_print_original(self):

        if self._u1_original:
            return self._u1_original
        _task, config = self._u1_task_config()
        if config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        reprint = self._u1_reprint_info(config)
        fields = (
            ('extruder_map_table', 32, 'int', 0, 3),
            ('extruders_used', 4, 'bool', 0, 1),
            ('end_unload_filament', 4, 'bool', 0, 1),
            ('flow_calib_extruders', 4, 'bool', 0, 1),
            ('extruders_replenished', 4, 'int', 0, 3),
        )
        live_snapshot = {}
        reprint_snapshot = {}
        for key, length, kind, minimum, maximum in fields:
            live_value = config.get(key)
            if not isinstance(live_value, list) or len(live_value) != length:
                raise BMCUError(
                    'Snapmaker U1 %s has invalid length; exact rollback is unavailable' %
                    key)
            if kind == 'bool':
                if any(not isinstance(value, bool) for value in live_value):
                    raise BMCUError(
                        'Snapmaker U1 %s is malformed' % key)
            else:
                if any(isinstance(value, bool) or not isinstance(value, int) or
                       value < minimum or value > maximum
                       for value in live_value):
                    raise BMCUError(
                        'Snapmaker U1 %s is malformed' % key)
            live_snapshot[key] = copy.deepcopy(live_value)
            if key != 'extruders_replenished':
                reprint_value = reprint.get(key)
                if (not isinstance(reprint_value, list) or
                        len(reprint_value) != length):
                    raise BMCUError(
                        'Snapmaker U1 reprint_info.%s has invalid length; '
                        'exact rollback is unavailable' % key)
                if kind == 'bool':
                    valid = all(isinstance(value, bool)
                                for value in reprint_value)
                else:
                    valid = all(
                        not isinstance(value, bool) and
                        isinstance(value, int) and
                        minimum <= value <= maximum
                        for value in reprint_value)
                if not valid:
                    raise BMCUError(
                        'Snapmaker U1 reprint_info.%s is malformed' % key)
                reprint_snapshot[key] = copy.deepcopy(reprint_value)
        self._u1_original = {
            'live': live_snapshot,
            'reprint': reprint_snapshot,
        }
        self.print_transaction_phase = 'applying'

        self._save_print_session()
        return self._u1_original

    def _persist_u1_print_task(self, context='BMCU U1 print transaction'):

        task, config = self._u1_task_config()
        path = str(getattr(task, 'config_path', '') or '') if task else ''
        if task is None or config is None or not path:
            raise BMCUError(
                'Snapmaker U1 persistent print-task API is unavailable')
        return self._persist_u1_json(path, config, context)

    def _persist_u1_json(self, path, config, context):

        if not isinstance(config, dict):
            raise BMCUError('%s has no JSON object to persist' % context)
        path = os.path.abspath(str(path or ''))
        directory = os.path.dirname(path)
        if not path or not directory or not os.path.isdir(directory):
            raise BMCUError('%s has an invalid Snapmaker JSON path' % context)
        try:
            payload = json.dumps(config, indent=4, allow_nan=False) + '\n'
            return self._durable_writer.write(
                path, payload, mode=None, timeout=30.0)
        except Exception as exc:
            if isinstance(exc, BMCUError):
                raise
            raise BMCUError('%s could not durably store %s: %s' %
                            (context, os.path.basename(path), exc))

    def _persist_u1_print_stats(self, context='BMCU U1 prime journal'):
        stats = self.printer.lookup_object('print_stats', None)
        config = getattr(stats, '_config', None) if stats is not None else None
        path = str(getattr(stats, '_config_path', '') or '') if stats else ''
        if not isinstance(config, dict) or not path:
            raise BMCUError('Snapmaker U1 print_stats.json is unavailable')
        return self._persist_u1_json(path, config, context)

    def _restore_u1_original_snapshot(self):

        original = self._u1_original
        if not isinstance(original, dict) or not original:
            return False
        _task, config = self._u1_task_config()
        if config is None:
            return False
        live_original = original.get('live')
        reprint_original = original.get('reprint')
        if not isinstance(live_original, dict) or not isinstance(
                reprint_original, dict):
            return False
        if self.print_transaction_phase != 'restoring':
            self.print_transaction_phase = 'restoring'
            self._save_print_session()

        if not self.print_stock_reset_observed:
            for key, values in live_original.items():
                config[key] = copy.deepcopy(values)
        reprint = self._u1_reprint_info(config)
        for key, values in reprint_original.items():
            reprint[key] = copy.deepcopy(values)
        try:
            self._persist_u1_print_task('BMCU U1 rollback')
        except Exception as exc:
            self._record_error(
                'U1_MAP_RESTORE_PERSIST_FAILED', details=str(exc))
            self.print_transaction_phase = 'recovery'
            self._save_print_session()
            return False
        self._u1_original.clear()
        self._u1_map_backup.clear()
        self._u1_used_backup.clear()
        self._u1_end_unload_backup.clear()
        self.print_transaction_phase = ''
        self._u1_restore_pending_reported = False
        return True

    def _u1_logical_tools_used(self):

        task = self.printer.lookup_object('print_task_config', None)
        config2 = getattr(task, 'print_task_config_2', None) if task else None
        if not isinstance(config2, dict):
            return set()
        used_g = config2.get('filament_used_g')
        used_mm = config2.get('filament_used_mm')
        if not isinstance(used_g, list):
            used_g = []
        if not isinstance(used_mm, list):
            used_mm = []
        tools = set()
        for tool in range(U1_LOGICAL_TOOL_LIMIT):
            values = []
            if tool < len(used_g):
                values.append(used_g[tool])
            if tool < len(used_mm):
                values.append(used_mm[tool])
            for value in values:
                try:
                    amount = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(amount) and amount > 0.0:
                    tools.add(tool)
                    break
        return tools

    def _reset_u1_runtime_used_for_plan(self):
        _task, config = self._u1_task_config()
        if config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        used, reprint_used = self._ensure_u1_dual_runtime_list(
            config, 'extruders_used', U1_NATIVE_TOOL_COUNT,
            lambda index: False)
        for head in range(U1_NATIVE_TOOL_COUNT):
            used[head] = False
            reprint_used[head] = False
        return True

    def _set_u1_runtime_tool_map(self, logical_tool, physical_head, remember=True):
        logical_tool = int(logical_tool)
        physical_head = int(physical_head)
        if logical_tool < 0 or logical_tool >= 32:
            raise BMCUError('Snapmaker U1 supports logical tools T0-T31; got T%d' % logical_tool)
        if physical_head < 0 or physical_head > 3:
            raise BMCUError('Snapmaker U1 physical head must be 0-3')
        task, config = self._u1_task_config()
        if config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')

        mappings, reprint_mappings = self._ensure_u1_dual_runtime_list(
            config, 'extruder_map_table', 32,
            lambda index: index if index < 4 else 0)
        used, reprint_used = self._ensure_u1_dual_runtime_list(
            config, 'extruders_used', 4, lambda index: False)
        key = str(logical_tool)
        head_key = str(physical_head)
        if remember:
            self._u1_map_backup.setdefault(key, int(mappings[logical_tool]))
            self._u1_used_backup.setdefault(head_key, bool(used[physical_head]))
        mappings[logical_tool] = physical_head
        reprint_mappings[logical_tool] = physical_head
        used[physical_head] = True
        reprint_used[physical_head] = True
        return True

    def _sync_u1_print_tool(self, logical_tool, device, channel, remember=True):
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False
        head = int(endpoint.get('head_index', -1))
        self._set_u1_runtime_tool_map(
            logical_tool, head, remember=remember)
        _task, config = self._u1_task_config()
        if config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        end_unload, reprint_end_unload = self._ensure_u1_dual_runtime_list(
            config, 'end_unload_filament', 4, lambda index: False)
        if remember:
            self._u1_end_unload_backup.setdefault(
                str(head), bool(end_unload[head]))

        end_unload[head] = False
        reprint_end_unload[head] = False
        return True

    @staticmethod
    def _endpoint_nozzle_diameter(endpoint):
        extruder_name = str(endpoint.get('extruder', '') or '').strip()
        if not extruder_name:
            head = int(endpoint.get('head_index', 0) or 0)
            extruder_name = 'extruder' if head == 0 else 'extruder%d' % head
        extruder = endpoint.printer.lookup_object(extruder_name, None)
        if extruder is None:
            return None
        try:
            value = float(getattr(extruder, 'nozzle_diameter'))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0.0 else None

    def refill_route_plan(self, source_endpoint, target_endpoint):

        if source_endpoint is None or target_endpoint is None:
            return {'supported': False, 'mode': 'invalid',
                    'reason': 'source or target endpoint is unavailable'}
        if source_endpoint.name == target_endpoint.name:
            return {'supported': True, 'mode': 'same_endpoint'}

        if (source_endpoint.driver == 'snapmaker_u1' and
                target_endpoint.driver == 'snapmaker_u1'):
            try:
                source_head = int(source_endpoint.get('head_index', -1))
                target_head = int(target_endpoint.get('head_index', -1))
            except (TypeError, ValueError):
                source_head = target_head = -1
            if source_head not in range(4) or target_head not in range(4):
                return {'supported': False, 'mode': 'u1',
                        'reason': 'Snapmaker U1 head index must be 0-3'}
            if source_head == target_head:
                return {'supported': False, 'mode': 'u1',
                        'reason': 'two endpoint definitions refer to the same U1 head'}
            source_nozzle = self._endpoint_nozzle_diameter(source_endpoint)
            target_nozzle = self._endpoint_nozzle_diameter(target_endpoint)
            if (source_nozzle is not None and target_nozzle is not None and
                    abs(source_nozzle - target_nozzle) > 0.0005):
                return {'supported': False, 'mode': 'u1',
                        'reason': 'U1 source and replacement nozzle diameters differ'}
            task, _config = self._u1_task_config()
            if task is None:
                return {'supported': False, 'mode': 'u1',
                        'reason': 'Snapmaker U1 print_task_config is unavailable'}
            return {
                'supported': True, 'mode': 'snapmaker_u1',
                'source_head': source_head, 'target_head': target_head,
                'source_nozzle': source_nozzle, 'target_nozzle': target_nozzle,
            }

        macro = str(source_endpoint.get('cross_refill_resume_macro', '') or '').strip()
        enabled = bool(source_endpoint.get('cross_endpoint_refill', False))
        if enabled and macro:
            return {'supported': True, 'mode': 'custom_macro', 'macro': macro}
        return {
            'supported': False, 'mode': 'unsupported',
            'reason': ('printer has no verified cross-endpoint refill integration; '
                       'use the same endpoint or configure an explicit advanced macro'),
        }

    def _u1_mapping_head(self, mapping):
        if not isinstance(mapping, dict):
            return None
        if mapping.get('native'):
            try:
                head = int(mapping.get('head', -1))
            except (TypeError, ValueError, OverflowError):
                return None
            return head if 0 <= head < 4 else None
        device = self._mapping_device(mapping)
        try:
            channel = int(mapping.get('channel', -1))
        except (TypeError, ValueError, OverflowError):
            return None
        endpoint = (self._endpoint_for_channel(device, channel)
                    if device is not None and 0 <= channel <= 3 else None)
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return None
        try:
            head = int(endpoint.get('head_index', -1))
        except (TypeError, ValueError, OverflowError):
            return None
        return head if 0 <= head < 4 else None

    def _recompute_u1_runtime_used_from_print_plan(self, remember=True):
        _task, config = self._u1_task_config()
        if config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        used, reprint_used = self._ensure_u1_dual_runtime_list(
            config, 'extruders_used', 4, lambda index: False)
        if remember:
            for head in range(4):
                self._u1_used_backup.setdefault(str(head), bool(used[head]))
        wanted = [False] * 4
        for mapping in self.print_tools.values():
            head = self._u1_mapping_head(mapping)
            if head is not None:
                wanted[head] = True
        for head in range(4):
            used[head] = wanted[head]
            reprint_used[head] = wanted[head]
        return wanted

    def _apply_u1_replenish_map(self, logical_tool, source_head, target_head,
                                remember=True):
        logical_tool = int(logical_tool)
        source_head = int(source_head)
        target_head = int(target_head)
        if logical_tool < 0 or logical_tool >= 32:
            raise BMCUError('Snapmaker U1 logical refill tool must be T0-T31')
        task, config = self._u1_task_config()
        if task is None or config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        mappings, reprint_mappings = self._ensure_u1_dual_runtime_list(
            config, 'extruder_map_table', 32,
            lambda index: index if index < 4 else 0)
        flow, reprint_flow = self._ensure_u1_dual_runtime_list(
            config, 'flow_calib_extruders', 4, lambda index: True)
        if int(mappings[logical_tool]) != source_head:
            raise BMCUError(
                'Snapmaker U1 logical T%d no longer maps to source Head %d' %
                (logical_tool, source_head + 1))
        if remember:
            self._u1_map_backup.setdefault(
                str(logical_tool), int(mappings[logical_tool]))
        mappings[logical_tool] = target_head
        reprint_mappings[logical_tool] = target_head
        flow[target_head] = True
        reprint_flow[target_head] = True
        self._recompute_u1_runtime_used_from_print_plan(remember=remember)
        self.print_transaction_phase = 'active'
        self._save_print_session()
        self._persist_u1_print_task('BMCU cross-head refill map')
        return True

    def _apply_u1_stock_style_replenish_map(
            self, source_head, target_head, route_mapping=None):
        source_head = int(source_head)
        target_head = int(target_head)
        task, config = self._u1_task_config()
        if task is None or config is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        mappings, reprint_mappings = self._ensure_u1_dual_runtime_list(
            config, 'extruder_map_table', 32,
            lambda index: index if index < 4 else 0)
        used, reprint_used = self._ensure_u1_dual_runtime_list(
            config, 'extruders_used', 4, lambda index: False)
        flow, reprint_flow = self._ensure_u1_dual_runtime_list(
            config, 'flow_calib_extruders', 4, lambda index: True)
        aliases = [index for index, head in enumerate(mappings)
                   if int(head) == source_head]
        for logical_tool in aliases:
            self._u1_map_backup.setdefault(
                str(logical_tool), int(mappings[logical_tool]))
            mappings[logical_tool] = target_head
            reprint_mappings[logical_tool] = target_head
            if isinstance(route_mapping, dict):
                self.print_tools[str(logical_tool)] = copy.deepcopy(route_mapping)
        self._u1_used_backup.setdefault(str(source_head), bool(used[source_head]))
        self._u1_used_backup.setdefault(str(target_head), bool(used[target_head]))
        used[source_head] = False
        reprint_used[source_head] = False
        used[target_head] = True
        reprint_used[target_head] = True
        flow[target_head] = True
        reprint_flow[target_head] = True
        replenished = self._ensure_u1_runtime_list(
            config, 'extruders_replenished', 4, lambda index: index)
        replenished[source_head] = target_head
        if isinstance(route_mapping, dict):
            self.print_map_active = True
        self.print_transaction_phase = 'active'
        self._save_runtime()
        self._save_print_session()
        self._persist_u1_print_task('BMCU mixed stock-style refill map')
        return aliases

    def _resume_u1_stock_replenish(self, target_head, endpoint):
        task, _config = self._u1_task_config()
        if task is None:
            raise BMCUError('Snapmaker U1 print_task_config is unavailable')
        task.perform_auto_replenish = True
        self.gcode.run_script_from_command(
            'RESUME REPLENISH=1 REPLENISH_EXTRUDER=%d' % int(target_head))
        timeout_value = endpoint.get('refill_resume_timeout', 30.0)
        timeout = 30.0 if timeout_value is None else float(timeout_value)
        deadline = self.reactor.monotonic() + max(1.0, timeout)
        while self.reactor.monotonic() < deadline:
            state = self._print_state()
            if state == 'printing':
                return True
            if state in ('complete', 'completed', 'cancelled', 'canceled',
                         'error', 'failed', 'standby', 'ready', 'idle'):
                raise BMCUError(
                    'AUTO_REFILL_RESUME_FAILED: printer entered %s after mixed refill' %
                    state)
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        raise BMCUError(
            'AUTO_REFILL_RESUME_FAILED: printer did not confirm mixed refill RESUME')

    def _device_by_stable_uid(self, uid):
        uid = str(uid or '').upper()
        if not re.fullmatch(r'[0-9A-F]{24}', uid):
            return None
        device = self.devices_by_uid.get(uid)
        if device is not None:
            return device
        for candidate in self.devices:
            if self._device_uid(candidate) == uid:
                return candidate
        return None

    def begin_u1_cross_refill(self, logical_tool, source_device,
                              source_channel, source_endpoint,
                              replacement_device, replacement_channel,
                              replacement_endpoint):

        if self.u1_cross_refill_pending:
            raise BMCUError('another Snapmaker U1 refill transaction is pending')
        logical_tool = int(logical_tool)
        source_channel = int(source_channel)
        replacement_channel = int(replacement_channel)
        if logical_tool not in range(32):
            raise BMCUError('Snapmaker U1 cross-head refill tool must be T0-T31')
        if (source_endpoint.driver != 'snapmaker_u1' or
                replacement_endpoint.driver != 'snapmaker_u1'):
            raise BMCUError('cross refill journal is only valid for Snapmaker U1 heads')
        source_head = int(source_endpoint.get('head_index', -1))
        replacement_head = int(replacement_endpoint.get('head_index', -1))
        if (source_head not in range(4) or replacement_head not in range(4) or
                source_head == replacement_head):
            raise BMCUError('invalid Snapmaker U1 cross-head refill endpoints')
        source_uid = self._device_uid(source_device)
        replacement_uid = self._device_uid(replacement_device)
        if (not re.fullmatch(r'[0-9A-F]{24}', source_uid or '') or
                not re.fullmatch(r'[0-9A-F]{24}', replacement_uid or '')):
            raise BMCUError('cross-head refill requires stable BMCU hardware UIDs')
        if self._route_state(source_device, source_channel) != protocol.ROUTE_EMPTY:
            raise BMCUError('source BMCU route is not confirmed EMPTY after tail drain')
        if self._route_state(replacement_device, replacement_channel) != protocol.ROUTE_EMPTY:
            raise BMCUError('replacement BMCU route is not confirmed EMPTY before load')
        pending = {
            'kind': 'bmcu_cross',
            'source_head': source_head,
            'replacement_head': replacement_head,
            'source_tool': logical_tool,
            'source_endpoint': source_endpoint.name,
            'replacement_endpoint': replacement_endpoint.name,
            'source_device_uid': source_uid,
            'source_channel': source_channel,
            'replacement_device_uid': replacement_uid,
            'replacement_channel': replacement_channel,
            'phase': 'loading',
            'error': '',
        }
        self.u1_cross_refill_pending = pending
        self._save_print_session()
        return copy.deepcopy(pending)

    def capture_u1_cross_refill(self, logical_tool, replacement_device,
                                replacement_channel, replacement_endpoint):

        pending = self.u1_cross_refill_pending
        if not isinstance(pending, dict) or pending.get('kind') != 'bmcu_cross':
            raise BMCUError('Snapmaker U1 cross-head refill journal is missing')
        if (int(pending.get('source_tool', -1)) != int(logical_tool) or
                str(pending.get('replacement_device_uid', '')).upper() !=
                self._device_uid(replacement_device) or
                int(pending.get('replacement_channel', -1)) !=
                int(replacement_channel) or
                pending.get('replacement_endpoint') != replacement_endpoint.name):
            raise BMCUError('Snapmaker U1 cross-head refill journal does not match target')
        replacement_device.refresh()
        if self._route_state(
                replacement_device, replacement_channel) != protocol.ROUTE_LOADED:
            pending['phase'] = 'load_failed'
            pending['error'] = 'replacement route did not confirm LOADED'
            self._save_print_session()
            raise BMCUError(pending['error'])
        mapping = self.print_tools.get(str(int(logical_tool)))
        if (not isinstance(mapping, dict) or
                str(mapping.get('device_uid', '')).upper() !=
                self._device_uid(replacement_device) or
                int(mapping.get('channel', -1)) != int(replacement_channel)):
            pending['phase'] = 'load_failed'
            pending['error'] = 'replacement print mapping was not committed in host state'
            self._save_print_session()
            raise BMCUError(pending['error'])
        pending['phase'] = 'captured'
        pending['error'] = ''
        self._save_print_session()
        return copy.deepcopy(pending)

    def fail_u1_cross_refill(self, logical_tool, error):
        pending = self.u1_cross_refill_pending
        if (not isinstance(pending, dict) or pending.get('kind') != 'bmcu_cross' or
                int(pending.get('source_tool', -1)) != int(logical_tool)):
            return False
        phase = str(pending.get('phase', '') or '').lower()
        if phase == 'loading':
            pending['phase'] = 'load_failed'
        elif phase == 'map_committing':
            pending['phase'] = 'commit_failed'
        elif phase in ('committed', 'resume_preparing', 'resume_ready'):
            pending['phase'] = 'resume_failed'
        pending['error'] = str(error or '')[:240]
        self._save_print_session()
        return True

    def _u1_cross_refill_context(self, pending):
        source_device = self._device_by_stable_uid(
            pending.get('source_device_uid'))
        replacement_device = self._device_by_stable_uid(
            pending.get('replacement_device_uid'))
        source_endpoint = self.endpoints.get(pending.get('source_endpoint'))
        replacement_endpoint = self.endpoints.get(
            pending.get('replacement_endpoint'))
        if (source_device is None or replacement_device is None or
                source_endpoint is None or replacement_endpoint is None):
            raise BMCUError('Snapmaker U1 cross-head refill devices/endpoints are offline')
        if (source_endpoint.driver != 'snapmaker_u1' or
                replacement_endpoint.driver != 'snapmaker_u1'):
            raise BMCUError('Snapmaker U1 cross-head refill endpoint type changed')
        return source_device, replacement_device, source_endpoint, replacement_endpoint

    def _prepare_u1_refill_resume(self, source_head, replacement_head):

        macro = self.printer.lookup_object('gcode_macro INNER_RESUME', None)
        toolhead = self.printer.lookup_object('toolhead', None)
        if macro is None or toolhead is None:
            raise BMCUError('Snapmaker U1 resume integration is unavailable')
        if int(toolhead.get_extruder().extruder_index) != int(source_head):
            raise BMCUError('Snapmaker U1 active head changed before refill resume')
        current_temp = float(macro.variables.get('last_extruder_temp', 0) or 0)
        source_name = 'extruder' if source_head == 0 else 'extruder%d' % source_head
        replacement_name = ('extruder' if replacement_head == 0 else
                            'extruder%d' % replacement_head)
        virtual_sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if virtual_sdcard is not None:
            virtual_sdcard.record_pl_print_temperature_env({
                replacement_name: current_temp,
                source_name: 0,
            }, ignore_pl_condition=True)
            virtual_sdcard.force_refresh_move_env_extruder(replacement_name)
        self.gcode.run_script_from_command(
            'SET_GCODE_VARIABLE MACRO=INNER_RESUME '
            'VARIABLE=extruder%d_temp VALUE=0' % source_head)
        self.gcode.run_script_from_command('M104 S0 T%d A0' % source_head)

    def _wait_u1_refill_resume(self, pending, endpoint):
        replacement = int(pending['replacement_head'])
        self.gcode.run_script_from_command(
            'RESUME REPLENISH=1 REPLENISH_EXTRUDER=%d' % replacement)
        timeout_value = endpoint.get('refill_resume_timeout', 30.0)
        timeout = 30.0 if timeout_value is None else float(timeout_value)
        deadline = self.reactor.monotonic() + max(1.0, timeout)
        while self.reactor.monotonic() < deadline:
            state = self._print_state()
            if state == 'printing':
                self.u1_cross_refill_pending = {}
                self._save_print_session()
                return True
            if state in ('complete', 'completed', 'cancelled', 'canceled',
                         'error', 'failed', 'standby', 'ready', 'idle'):
                pending['phase'] = 'resume_failed'
                pending['error'] = 'printer entered %s after head change' % state
                self._save_print_session()
                raise BMCUError('AUTO_REFILL_RESUME_FAILED: %s' % pending['error'])
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        pending['phase'] = 'resume_failed'
        pending['error'] = 'printer did not confirm cross-head RESUME'
        self._save_print_session()
        raise BMCUError('AUTO_REFILL_RESUME_FAILED: %s' % pending['error'])

    def _resume_u1_cross_refill_pending(self, pending):
        if self._print_state() not in ('paused', 'pause'):
            raise BMCUError('Snapmaker U1 print is no longer paused')
        (source_device, replacement_device, source_endpoint,
         replacement_endpoint) = self._u1_cross_refill_context(pending)
        source_channel = int(pending['source_channel'])
        replacement_channel = int(pending['replacement_channel'])
        source_tool = int(pending['source_tool'])
        source_head = int(pending['source_head'])
        replacement_head = int(pending['replacement_head'])
        source_device.refresh()
        replacement_device.refresh()
        if self._route_state(source_device, source_channel) != protocol.ROUTE_EMPTY:
            raise BMCUError('source BMCU route is not confirmed EMPTY')
        if self._route_state(
                replacement_device, replacement_channel) != protocol.ROUTE_LOADED:
            raise BMCUError('replacement BMCU route is not confirmed LOADED')
        mapping = self.print_tools.get(str(source_tool))
        if (not isinstance(mapping, dict) or
                str(mapping.get('device_uid', '')).upper() !=
                self._device_uid(replacement_device) or
                int(mapping.get('channel', -1)) != replacement_channel):
            raise BMCUError('cross-head refill print mapping does not select replacement route')
        phase = str(pending.get('phase', '') or '').lower()
        if phase in ('loading', 'load_failed'):

            pending['phase'] = 'captured'
            pending['error'] = ''
            self._save_print_session()
            phase = 'captured'
        if phase in ('captured', 'map_committing', 'commit_failed'):
            pending['phase'] = 'map_committing'
            pending['error'] = ''
            self._save_print_session()
            try:
                _task, current_config = self._u1_task_config()
                live = (current_config.get('extruder_map_table', [])
                        if isinstance(current_config, dict) else [])
                reprint_info = (current_config.get('reprint_info', {})
                                if isinstance(current_config, dict) else {})
                reprint_live = (reprint_info.get('extruder_map_table', [])
                                if isinstance(reprint_info, dict) else [])
                if (source_tool >= len(live) or
                        source_tool >= len(reprint_live)):
                    raise BMCUError('Snapmaker U1 refill map is unavailable')
                live_head = int(live[source_tool])
                reprint_head = int(reprint_live[source_tool])
                if live_head == source_head and reprint_head == source_head:
                    self._apply_u1_replenish_map(
                        source_tool, source_head, replacement_head, remember=True)
                elif (live_head == replacement_head and
                      reprint_head == replacement_head):

                    self._recompute_u1_runtime_used_from_print_plan(remember=True)
                    self._persist_u1_print_task(
                        'BMCU cross-head refill commit recovery')
                else:
                    raise BMCUError(
                        'live/reprint refill maps disagree (%d/%d); exact recovery required' %
                        (live_head, reprint_head))
            except Exception as exc:
                pending['phase'] = 'commit_failed'
                pending['error'] = str(exc)[:240]
                self._save_print_session()
                raise
            pending['phase'] = 'committed'
            self._save_print_session()
            phase = 'committed'
        if phase not in ('committed', 'resume_preparing', 'resume_ready',
                         'resume_failed'):
            raise BMCUError('Snapmaker U1 cross-head refill journal phase is invalid')
        task, config = self._u1_task_config()
        live_map = config.get('extruder_map_table', []) if isinstance(config, dict) else []
        reprint = config.get('reprint_info', {}) if isinstance(config, dict) else {}
        reprint_map = reprint.get('extruder_map_table', []) if isinstance(reprint, dict) else []
        if (task is None or source_tool >= len(live_map) or
                source_tool >= len(reprint_map) or
                int(live_map[source_tool]) != replacement_head or
                int(reprint_map[source_tool]) != replacement_head):
            raise BMCUError('cross-head refill map is not durable in live and reprint state')
        pending['phase'] = 'resume_preparing'
        pending['error'] = ''
        self._save_print_session()
        try:
            self._prepare_u1_refill_resume(source_head, replacement_head)
        except Exception as exc:
            pending['phase'] = 'resume_failed'
            pending['error'] = str(exc)[:240]
            self._save_print_session()
            raise
        pending['phase'] = 'resume_ready'
        self._save_print_session()
        return self._wait_u1_refill_resume(pending, replacement_endpoint)

    def resume_cross_endpoint_refill(self, logical_tool, source_endpoint,
                                     target_endpoint, plan=None):
        plan = plan or self.refill_route_plan(source_endpoint, target_endpoint)
        if not plan.get('supported'):
            raise BMCUError(plan.get('reason', 'cross-endpoint refill is unsupported'))
        mode = plan.get('mode')
        if mode == 'snapmaker_u1':
            pending = self.u1_cross_refill_pending
            if (not isinstance(pending, dict) or
                    pending.get('kind') != 'bmcu_cross' or
                    int(pending.get('source_tool', -1)) != int(logical_tool)):
                raise BMCUError('Snapmaker U1 cross-head refill journal is missing')
            return self._resume_u1_cross_refill_pending(pending)
        if mode == 'custom_macro':
            source_endpoint.run_macro(
                'cross_refill_resume_macro',
                source_endpoint=source_endpoint.name,
                target_endpoint=target_endpoint.name,
                source_head=source_endpoint.get('head_index', -1),
                target_head=target_endpoint.get('head_index', -1),
                logical_tool=int(logical_tool))
        else:
            return self._resume_after_refill(target_endpoint)

        timeout_value = target_endpoint.get('refill_resume_timeout', 30.0)
        timeout = 30.0 if timeout_value is None else float(timeout_value)
        deadline = self.reactor.monotonic() + max(1.0, timeout)
        while self.reactor.monotonic() < deadline:
            state = self._print_state()
            if state == 'printing':
                return True
            if state in ('complete', 'completed', 'cancelled', 'canceled',
                         'error', 'failed', 'standby', 'ready', 'idle'):
                raise BMCUError(
                    'AUTO_REFILL_RESUME_FAILED: printer entered %s after head change' % state)
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        raise BMCUError(
            'AUTO_REFILL_RESUME_FAILED: printer did not confirm cross-head RESUME')

    def _reapply_u1_print_maps(self):
        if not self.print_map_active:
            return False
        if (self._u1_map_backup or self._u1_used_backup or
                self._u1_end_unload_backup) and not self._u1_original:
            self._record_error(
                'U1_PRINT_TRANSACTION_INVALID',
                details='exact U1 rollback snapshot is unavailable')
            self._safe_pause()
            return False
        self._reset_u1_runtime_used_for_plan()
        failed = False
        for tool_text, mapping in sorted(self.print_tools.items(), key=lambda item: int(item[0])):
            try:
                tool = int(tool_text)
                if mapping.get('native'):
                    self._set_u1_runtime_tool_map(tool, int(mapping.get('head', tool)), remember=False)
                    continue
                device = self._mapping_device(mapping)
                if device is not None:
                    self._sync_u1_print_tool(tool, device, int(mapping.get('channel', -1)), remember=False)
            except Exception:
                failed = True
                logging.exception('BMCU could not restore U1 runtime mapping for T%s', tool_text)
        if failed:
            self.print_transaction_phase = 'recovery'
            self._save_print_session()
            self._safe_pause()
            return False
        try:
            self._persist_u1_print_task(
                'BMCU map reapply after Klipper restart')
        except Exception as exc:
            self.print_transaction_phase = 'recovery'
            self._save_print_session()
            self._record_error(
                'U1_MAP_REAPPLY_PERSIST_FAILED', details=str(exc))
            self._safe_pause()
            return False
        self.print_transaction_phase = 'active'
        self._save_print_session()
        return True

    def _restore_u1_print_maps(self):
        if self._u1_original:
            return self._restore_u1_original_snapshot()
        if (not self._u1_map_backup and not self._u1_used_backup and
                not self._u1_end_unload_backup):
            return False
        task, config = self._u1_task_config()
        if config is None:
            return False
        mappings = self._ensure_u1_runtime_list(
            config, 'extruder_map_table', 32,
            lambda index: index if index < 4 else 0)
        used = self._ensure_u1_runtime_list(
            config, 'extruders_used', 4, lambda index: False)
        end_unload = self._ensure_u1_runtime_list(
            config, 'end_unload_filament', 4, lambda index: False)
        for tool_text, previous in self._u1_map_backup.items():
            try:
                tool = int(tool_text)
                value = int(previous)
            except (TypeError, ValueError):
                continue
            if 0 <= tool < 32 and 0 <= value < 4:
                mappings[tool] = value
        for head_text, previous in self._u1_used_backup.items():
            try:
                head = int(head_text)
            except (TypeError, ValueError):
                continue
            if 0 <= head < 4:
                used[head] = bool(previous)
        for head_text, previous in self._u1_end_unload_backup.items():
            try:
                head = int(head_text)
            except (TypeError, ValueError):
                continue
            if 0 <= head < 4:
                end_unload[head] = bool(previous)
        for tool_text, previous in tuple(self._u1_map_backup.items()):
            tool = int(tool_text)
            if int(mappings[tool]) != int(previous):
                return False
        for head_text, previous in tuple(self._u1_used_backup.items()):
            head = int(head_text)
            if bool(used[head]) != bool(previous):
                return False
        for head_text, previous in tuple(
                self._u1_end_unload_backup.items()):
            head = int(head_text)
            if bool(end_unload[head]) != bool(previous):
                return False
        self._u1_map_backup.clear()
        self._u1_used_backup.clear()
        self._u1_end_unload_backup.clear()
        self._u1_restore_pending_reported = False
        return True

    def _sync_device_slots(self, eventtime, device):
        if not device.ready:
            device.slot_sync_required = True
            return
        try:
            slots = []
            for channel in range(4):
                meta = self._channel_metadata(device, channel)
                slots.append({
                    'color': str(meta.get('color', '#FFFFFF') or '#FFFFFF'),
                    'name': str(meta.get('name') or 'Channel %d' % (channel + 1)),
                    'temperature_min': int(meta.get('temperature_min', 0) or 0),
                    'temperature_max': int(meta.get('temperature_max', 0) or 0),
                    'material': str(meta.get('material', '') or ''),
                })
            device.set_slots(slots)
            device.slots = [dict(slot, channel=index) for index, slot in enumerate(slots)]
            device.slot_sync_required = False
        except Exception:
            device.slot_sync_required = True
            logging.exception('BMCU %s runtime Channel metadata sync failed', device.name)

    def _endpoint_for_device(self, device, channel=None):

        if channel is not None:
            return self._endpoint_for_channel(device, channel)
        names = sorted(set(
            self._channel_endpoint_name(device, index)
            for index in range(4)
            if self._channel_endpoint_name(device, index)))
        if len(names) != 1:
            return None
        return self.endpoints.get(names[0])

    def _clear_endpoint_projection(self, endpoint):
        if endpoint is not None and hasattr(endpoint, 'clear_active_filament'):
            endpoint.clear_active_filament()

    def _u1_endpoint_transition_reserved(self, endpoint_name):
        job = self._u1_background_jobs.get(str(endpoint_name or ''))
        if not isinstance(job, dict):
            return False
        return str(job.get('state', '') or '') in (
            'starting', 'running', 'ready', 'partial', 'ready_native', 'handoff')

    def _schedule_endpoint_projection_clear(self, endpoint_name):
        endpoint_name = str(endpoint_name or '')
        if not endpoint_name or endpoint_name in self._endpoint_projection_clear_pending:
            return
        self._endpoint_projection_clear_pending.add(endpoint_name)
        key = 'endpoint-projection-clear:%s' % endpoint_name
        queued = self._queue_deferred_task(
            key,
            lambda eventtime, name=endpoint_name:
                self._run_scheduled_endpoint_projection_clear(eventtime, name))
        if not queued and key not in self._deferred_task_keys:
            self._endpoint_projection_clear_pending.discard(endpoint_name)

    def _run_scheduled_endpoint_projection_clear(self, eventtime, endpoint_name):
        self._endpoint_projection_clear_pending.discard(endpoint_name)
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or not self.owns_filament_callbacks:
            return

        if self._u1_endpoint_transition_reserved(endpoint_name):
            return
        if self._routes_for_endpoint(
                endpoint_name,
                (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)):
            return
        if self._prestaged_devices_for_endpoint(endpoint_name):
            return
        if endpoint_name in self.endpoint_locks:

            return
        try:
            if endpoint.driver == 'snapmaker_u1':
                owner = str(self._u1_lease_state.get(
                    endpoint_name, {}).get('owner', '') or '').lower()
                if owner not in ('bmcu', 'bmcu_busy'):
                    self._endpoint_projection_clear_retries.pop(
                        endpoint_name, None)
                    return
                path = endpoint.native_path_status()
                if not path.get('known') or path.get('busy'):
                    return
                if hasattr(endpoint, 'commit_native_path_empty'):
                    endpoint.commit_native_path_empty(
                        route_empty_verified=True)
            self._clear_endpoint_projection(endpoint)
            self._endpoint_projection_clear_retries.pop(endpoint_name, None)
        except Exception as exc:

            attempts = int(self._endpoint_projection_clear_retries.get(
                endpoint_name, 0))
            if ('RFID detector did not become idle' in str(exc) and
                    attempts < 2):
                self._endpoint_projection_clear_retries[endpoint_name] = attempts + 1
                logging.info(
                    'BMCU waiting for U1 RFID cache before clearing %s '
                    '(retry %d/2)', endpoint_name, attempts + 1)
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                self._schedule_endpoint_projection_clear(endpoint_name)
                return
            self._endpoint_projection_clear_retries.pop(endpoint_name, None)
            logging.warning(
                'BMCU deferred U1 filament display cleanup skipped for %s: %s',
                endpoint_name, exc)

    def device_status_callback_failed(self, device, exc, previous, current):
        self._record_error(
            'STATUS_RECONCILIATION_FAILED', device=device.name,
            phase='STATUS_RECONCILIATION', details=str(exc), evidence={
                'pending_requests': len(getattr(device, 'pending', {})),
                'route_state': list(current.get('route_state', [])),
                'motion': list(current.get('motion', [])),
            })

    def _await_required_transport_release(self, device):

        if self._klippy_disconnecting:
            return False
        if self._critical_motion_depth:

            return False

        deadline = self.reactor.monotonic() + self.required_runtime_sync_timeout
        while self._critical_motion_active:
            now = self.reactor.monotonic()
            if now >= deadline:
                return False

            self.reactor.pause(min(deadline, now + 0.020))

        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is not None:
            try:
                if self._printer_motion_busy(self.reactor.monotonic()):
                    toolhead.wait_moves()
            except Exception:
                return False

        try:
            device.set_transport_paused(False, required=True)
        except Exception:
            return False

        settle_deadline = min(deadline, self.reactor.monotonic() + 0.250)
        while self.reactor.monotonic() < settle_deadline:
            if getattr(device, 'connected', False):
                break
            self.reactor.pause(min(
                settle_deadline, self.reactor.monotonic() + 0.020))
        try:
            return bool(self._ensure_required_runtime_ready(device))
        except Exception as exc:
            device.last_error = str(exc)
            return False

    def _ensure_required_runtime_ready(self, device):

        if device.ready and getattr(device, 'runtime_configured', True):
            return True
        if getattr(device, 'suspended', False):
            raise BMCUError('%s is suspended' % device.name)

        deadline = self.reactor.monotonic() + self.required_runtime_sync_timeout
        toolhead = self.printer.lookup_object('toolhead', None)
        printer_drained = False
        last_error = ''
        while self.reactor.monotonic() < deadline:
            now = self.reactor.monotonic()

            if self._critical_motion_active or self._critical_motion_depth:
                self.reactor.pause(min(deadline, now + 0.020))
                continue

            if not printer_drained and toolhead is not None:
                try:
                    if self._printer_motion_busy(now):

                        toolhead.wait_moves()
                    printer_drained = True
                except Exception as exc:
                    last_error = str(exc)
                    self.reactor.pause(min(deadline, now + 0.050))
                    continue

            if device.ready and getattr(device, 'runtime_configured', True):
                return True

            device.set_transport_paused(False, required=True)
            if not device.connected or not device.hello_validated:
                try:
                    device.tick(now, transport_only=True)
                except Exception as exc:
                    last_error = str(exc)
                self.reactor.pause(min(deadline, now + 0.050))
                continue

            device.runtime_config_sync_pending = True
            try:
                self._sync_device_runtime_config(now, device)
            except Exception as exc:

                last_error = str(exc)
            if device.ready and getattr(device, 'runtime_configured', True):
                logging.info(
                    'BMCU %s runtime policy synchronized on required print path',
                    device.name)
                return True
            last_error = str(getattr(device, 'last_error', '') or last_error)
            self.reactor.pause(min(deadline, self.reactor.monotonic() + 0.050))

        detail = ('; %s' % last_error) if last_error else ''
        raise BMCUError(
            '%s runtime configuration could not synchronize before the '
            'required BMCU operation%s' % (device.name, detail))

    def _check_automatic_ready(self, device, channel, allow_uncertain=False,
                               require_present=True,
                               allow_detached_handoff=False):

        self._required_transport_users += 1
        try:
            return self._check_automatic_ready_required(
                device, channel, allow_uncertain=allow_uncertain,
                require_present=require_present,
                allow_detached_handoff=allow_detached_handoff)
        finally:
            self._required_transport_users = max(
                0, self._required_transport_users - 1)

    def _check_automatic_ready_required(
            self, device, channel, allow_uncertain=False,
            require_present=True, allow_detached_handoff=False):

        if (getattr(device, '_reactor_quiesced', False) or
                getattr(device, '_transport_paused', False)):
            if not self._await_required_transport_release(device):
                raise BMCUError(
                    '%s control plane did not resume after printer motion' %
                    device.name)
        status = device.status
        if not device.ready or not getattr(device, 'runtime_configured', True):
            self._ensure_required_runtime_ready(device)
            status = device.status
        if not device.ready or not getattr(device, 'runtime_configured', True):
            raise BMCUError('%s is not ready (runtime configuration not synchronized)' % device.name)
        if getattr(device, 'slot_sync_required', False):
            self._sync_device_slots(self.reactor.monotonic(), device)
        status = device.status
        if bool(status.get('nvm_fault', False)):
            raise BMCUError('%s has a fatal BMCU NVM fault; route-changing motion is disabled' % device.name)
        route_state = self._route_state(device, channel)
        if (allow_detached_handoff and
                self._durable_tail_route(device, channel) is not None and
                self._route_states_from_status(status)[channel] ==
                protocol.ROUTE_EMPTY):
            route_state = protocol.ROUTE_EMPTY
        if route_state == protocol.ROUTE_UNCERTAIN and not allow_uncertain:
            raise BMCUError(
                '%s Channel %d route is UNCERTAIN; confirm EMPTY or LOADED in the BMCU panel' %
                (device.name, channel + 1))
        if not (status['calibration_valid_mask'] & (1 << channel)):
            raise BMCUError('%s Channel %d buffer is not calibrated' % (device.name, channel + 1))
        if require_present and not status['present'][channel]:
            raise BMCUError('%s Channel %d has no filament' % (device.name, channel + 1))
        metadata = self._channel_metadata(device, channel)
        encoder = metadata.get('encoder_status', 'UNTESTED')
        if not (status['encoder_io_mask'] & (1 << channel)):
            encoder = 'FAULT'
        if encoder == 'FAULT':
            raise BMCUError('%s Channel %d encoder failed' % (device.name, channel + 1))

    @staticmethod
    def _u1_native_feeder_status(endpoint, strict=False):

        try:
            return endpoint._native_feeder_status(strict=bool(strict))
        except TypeError as exc:

            if "unexpected keyword argument 'strict'" not in str(exc):
                raise
            return endpoint._native_feeder_status()

    def _u1_ownership_record(self, endpoint_name):
        records = self.state.data.setdefault('u1_ownership', {})
        record = records.get(endpoint_name)
        if not isinstance(record, dict):
            record = {
                'baseline_captured': False,
                'baseline_disabled': False,
                'baseline_filament': {},
                'head_index': -1,
                'persistent_hold': False,
                'tail_detached': False,
                'tail_sensor_cleared': False,
                'follower_pending': False,
                'follower_kind': 'bmcu',
                'follower_device': '',
                'follower_uid': '',
                'follower_channel': -1,
                'follower_tool': -1,
                'generation': 0,
                'generation_open': False,
                'device': '',
                'device_uid': '',
                'channel': -1,
                'route_state': 'EMPTY',
                'reason': '',
            }
            records[endpoint_name] = record
        record.setdefault('baseline_filament', {})
        return record

    def _capture_u1_baseline(self, endpoint):
        record = self._u1_ownership_record(endpoint.name)
        try:
            record['head_index'] = int(endpoint.get('head_index', -1))
        except (TypeError, ValueError, OverflowError):
            record['head_index'] = -1
        if (record.get('baseline_captured') and
                record.get('generation_open')):
            return record
        feeder = self._u1_native_feeder_status(endpoint, strict=True)
        record['baseline_captured'] = True
        record['baseline_disabled'] = bool(
            feeder.get('disable_auto', False))
        task, config = self._u1_task_config()
        head = int(record.get('head_index', -1))
        baseline = {}
        if task is not None and isinstance(config, dict) and 0 <= head < 4:
            for key in ('filament_vendor', 'filament_type',
                        'filament_sub_type', 'filament_soft',
                        'filament_color', 'filament_color_rgba',
                        'filament_color_multi', 'filament_official',
                        'filament_sku', 'filament_exist', 'filament_edit'):
                values = config.get(key)
                if isinstance(values, list) and head < len(values):
                    baseline[key] = copy.deepcopy(values[head])
        record['baseline_filament'] = baseline
        record['generation_open'] = True
        record['generation'] = min(
            0x7FFFFFFF, int(record.get('generation', 0)) + 1)
        record['reason'] = 'captured stock feeder baseline for lease generation'
        self.state.save()
        return record

    def _handoff_u1_to_native(self, endpoint, reason='verified EMPTY handoff',
                              save=False, close_generation=False):

        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False
        record = self._u1_ownership_record(endpoint.name)
        if self._endpoint_has_loaded_or_active_route(endpoint.name):
            raise BMCUError(
                '%s still has a loaded/active BMCU route' % endpoint.name)
        self._refresh_u1_disconnect_hazards(endpoint.name)
        if self._u1_disconnect_hazards.get(endpoint.name):
            raise BMCUError(
                '%s has an unresolved BMCU disconnect hazard' % endpoint.name)
        assigned = self._assigned_routes_for_endpoint(endpoint.name)
        unverified = [
            device.name for device, _channel in assigned
            if device.name not in self._u1_devices_reconciled_once]
        if unverified:
            raise BMCUError(
                '%s route snapshot is not verified for: %s' %
                (endpoint.name, ', '.join(sorted(set(unverified)))))
        path = endpoint.native_path_status()
        if not path.get('known') or path.get('busy'):
            raise BMCUError(
                '%s shared path is not positively EMPTY (%s)' %
                (endpoint.name, path.get('channel_state', 'unknown')))

        generation_open = bool(record.get('generation_open', False))
        if record.get('baseline_captured') and generation_open:
            enabled = not bool(record.get('baseline_disabled', False))

            endpoint.restore_native_feeder(save=True, enabled=enabled)
        elif record.get('persistent_hold'):

            raise BMCUError(
                '%s has a persistent BMCU hold without an open captured '
                'baseline generation; exact recovery is required' % endpoint.name)
        else:

            pass
        endpoint.release_runtime_sensor_takeover()
        record['persistent_hold'] = False
        record['tail_detached'] = False
        record['tail_sensor_cleared'] = False
        record['follower_pending'] = False
        record['follower_kind'] = 'bmcu'
        record['follower_device'] = ''
        record['follower_uid'] = ''
        record['follower_channel'] = -1
        record['follower_tool'] = -1
        record['route_state'] = 'EMPTY'
        record['device'] = ''
        record['device_uid'] = ''
        record['channel'] = -1
        if close_generation:

            record['generation_open'] = False
        record['reason'] = str(reason or 'verified EMPTY handoff')[:160]
        self._set_u1_lease_state(
            endpoint.name, 'native', record['reason'],
            path=path, online=bool(
                self._assigned_routes_for_endpoint(endpoint.name)))
        self.state.save()
        return True

    def _u1_tail_detached_matches(self, endpoint, device, channel):
        if (endpoint is None or endpoint.driver != 'snapmaker_u1' or
                getattr(self, 'state', None) is None):
            return False
        record = self._u1_ownership_record(endpoint.name)
        if not bool(record.get('tail_detached', False)):
            return False
        try:
            record_channel = int(record.get('channel', -1))
        except (TypeError, ValueError, OverflowError):
            return False
        if record_channel != int(channel):
            return False
        expected_uid = str(record.get('device_uid', '') or '').upper()
        actual_uid = self._device_uid(device)
        if expected_uid and actual_uid:
            return expected_uid == actual_uid
        return str(record.get('device', '') or '') == device.name

    def _mark_u1_tail_detached(self, endpoint, device, channel, reason):
        if (endpoint is None or endpoint.driver != 'snapmaker_u1' or
                getattr(self, 'state', None) is None):
            return False
        channel = int(channel)
        record = self._u1_ownership_record(endpoint.name)
        changed = not self._u1_tail_detached_matches(
            endpoint, device, channel)
        record['tail_detached'] = True
        if changed:
            record['tail_sensor_cleared'] = False
            self._clear_u1_follower_commit(record)
        record['device'] = device.name
        record['device_uid'] = self._device_uid(device)
        record['channel'] = channel
        record['route_state'] = 'LOADED'
        record['reason'] = str(reason or
                               'BMCU input empty while downstream route remains loaded')[:160]

        self.state.save()
        return changed

    def _u1_tail_sensor_boundary_cleared(
            self, endpoint, device, channel):
        if not self._u1_tail_detached_matches(
                endpoint, device, int(channel)):
            return False
        record = self._u1_ownership_record(endpoint.name)
        return bool(record.get('tail_sensor_cleared', False))

    def _mark_u1_tail_sensor_cleared(
            self, endpoint, device, channel, reason='head sensor cleared'):
        if not self._u1_tail_detached_matches(
                endpoint, device, int(channel)):
            raise BMCUError(
                'cannot record a tail sensor boundary without detached ownership')
        record = self._u1_ownership_record(endpoint.name)
        changed = not bool(record.get('tail_sensor_cleared', False))
        record['tail_sensor_cleared'] = True
        record['reason'] = str(reason or 'head sensor cleared')[:160]
        self.state.save()
        return changed

    def _arm_u1_follower_commit(
            self, endpoint, source_device, source_channel, follower_device,
            follower_channel, logical_tool=-1):

        source_channel = int(source_channel)
        follower_channel = int(follower_channel)
        try:
            logical_tool = int(logical_tool)
        except (TypeError, ValueError, OverflowError):
            logical_tool = -1
        if logical_tool < 0 or logical_tool >= U1_LOGICAL_TOOL_LIMIT:
            logical_tool = -1
        if not self._u1_tail_detached_matches(
                endpoint, source_device, source_channel):
            raise BMCUError(
                'detached-tail ownership is missing before follower commit')
        record = self._u1_ownership_record(endpoint.name)
        record['follower_pending'] = True
        record['follower_kind'] = 'bmcu'
        record['follower_device'] = follower_device.name
        record['follower_uid'] = self._device_uid(follower_device)
        record['follower_channel'] = follower_channel
        record['follower_tool'] = logical_tool
        record['reason'] = (
            'follower physically captured; awaiting persistent route commit')
        self.state.save()
        return True

    def _arm_u1_native_follower_commit(
            self, endpoint, source_device, source_channel, logical_tool=-1):

        source_channel = int(source_channel)
        try:
            logical_tool = int(logical_tool)
        except (TypeError, ValueError, OverflowError):
            logical_tool = -1
        if logical_tool < 0 or logical_tool >= U1_LOGICAL_TOOL_LIMIT:
            logical_tool = -1
        if not self._u1_tail_detached_matches(
                endpoint, source_device, source_channel):
            raise BMCUError(
                'detached-tail ownership is missing before native follower commit')
        record = self._u1_ownership_record(endpoint.name)
        record['follower_pending'] = True
        record['follower_kind'] = 'native'
        record['follower_device'] = ''
        record['follower_uid'] = ''
        record['follower_channel'] = -1
        record['follower_tool'] = logical_tool
        record['reason'] = (
            'native follower load armed; awaiting stock load_finish commit')
        self.state.save()
        return True

    def _clear_u1_follower_commit(self, record):
        record['follower_pending'] = False
        record['follower_kind'] = 'bmcu'
        record['follower_device'] = ''
        record['follower_uid'] = ''
        record['follower_channel'] = -1
        record['follower_tool'] = -1

    def _reconcile_u1_pending_follower_commit(self, endpoint):

        if (endpoint is None or endpoint.driver != 'snapmaker_u1' or
                getattr(self, 'state', None) is None):
            return False
        record = self._u1_ownership_record(endpoint.name)
        if not (record.get('tail_detached') and
                record.get('follower_pending')):
            return False
        try:
            source_channel = int(record.get('channel', -1))
        except (TypeError, ValueError, OverflowError):
            return False
        source_device = None
        source_uid = str(record.get('device_uid', '') or '').upper()
        for candidate in self.devices:
            candidate_uid = self._device_uid(candidate)
            if (source_uid and candidate_uid == source_uid) or (
                    not source_uid and candidate.name ==
                    str(record.get('device', '') or '')):
                source_device = candidate
                break
        if source_device is None or not (0 <= source_channel <= 3):
            return False

        follower_kind = str(
            record.get('follower_kind', 'bmcu') or 'bmcu').lower()
        if follower_kind == 'native':
            source_raw = self._route_states_from_status(
                source_device.status)[source_channel]
            source_present = source_device.status.get(
                'present', [False] * 4)
            if (source_raw != protocol.ROUTE_EMPTY or
                    source_channel >= len(source_present) or
                    bool(source_present[source_channel])):
                return False
            try:
                feeder = self._u1_native_feeder_status(
                    endpoint, strict=True)
                entry = endpoint.sensor_detected('entry_sensor')
            except Exception:
                return False
            if (str(feeder.get('channel_state', '') or '').strip().lower() !=
                    'load_finish' or entry is not True):
                return False
            try:
                logical_tool = int(record.get('follower_tool', -1))
            except (TypeError, ValueError, OverflowError):
                logical_tool = -1
            if not self._finalize_snapmaker_tail_handoff_after_follower(
                    source_device, source_channel, endpoint,
                    logical_tool=logical_tool,
                    reason=('recovered crash-interrupted native follower '
                            'load')):
                return False
            try:
                endpoint.release_runtime_sensor_takeover()
            except Exception:
                logging.exception(
                    'BMCU could not release sensor takeover after recovering '
                    'native follower commit')
            logging.warning(
                'BMCU recovered detached-tail native follower commit on %s',
                endpoint.name)
            return True

        try:
            follower_channel = int(record.get('follower_channel', -1))
        except (TypeError, ValueError, OverflowError):
            return False
        follower_device = None
        follower_uid = str(record.get('follower_uid', '') or '').upper()
        for candidate in self.devices:
            candidate_uid = self._device_uid(candidate)
            if (follower_uid and candidate_uid == follower_uid) or (
                    not follower_uid and candidate.name ==
                    str(record.get('follower_device', '') or '')):
                follower_device = candidate
                break
        if (follower_device is None or
                not (0 <= follower_channel <= 3)):
            return False
        if (self._channel_endpoint_name(
                follower_device, follower_channel) != endpoint.name):
            return False
        follower_raw = self._route_states_from_status(
            follower_device.status)[follower_channel]
        follower_present = follower_device.status.get(
            'present', [False] * 4)
        if (follower_raw != protocol.ROUTE_LOADED or
                follower_channel >= len(follower_present) or
                not bool(follower_present[follower_channel])):
            return False
        same_route = bool(
            source_device.name == follower_device.name and
            source_channel == follower_channel)
        source_raw = self._route_states_from_status(
            source_device.status)[source_channel]
        if same_route:
            if source_raw != protocol.ROUTE_LOADED:
                return False
        elif source_raw == protocol.ROUTE_LOADED:

            source_present = source_device.status.get(
                'present', [False] * 4)
            if (source_channel >= len(source_present) or
                    bool(source_present[source_channel])):
                return False
            try:
                source_device.mark_unloaded(source_channel)
                source_device.refresh()
            except Exception:
                return False
            source_raw = self._route_states_from_status(
                source_device.status)[source_channel]
            if source_raw != protocol.ROUTE_EMPTY:
                return False
        elif source_raw != protocol.ROUTE_EMPTY:
            return False

        try:
            logical_tool = int(record.get('follower_tool', -1))
        except (TypeError, ValueError, OverflowError):
            logical_tool = -1
        if not self._finalize_snapmaker_tail_handoff_after_follower(
                source_device, source_channel, endpoint,
                follower_device=follower_device,
                follower_channel=follower_channel,
                logical_tool=logical_tool,
                reason=('recovered crash-interrupted BMCU follower route '
                        'commit')):
            return False
        logging.warning(
            'BMCU recovered detached-tail follower commit on %s: %s Channel %d',
            endpoint.name, follower_device.name, follower_channel + 1)
        return True

    def _clear_u1_tail_detached(self, endpoint, device=None, channel=None,
                                reason='verified route empty'):
        if (endpoint is None or endpoint.driver != 'snapmaker_u1' or
                getattr(self, 'state', None) is None):
            return False
        record = self._u1_ownership_record(endpoint.name)
        if not bool(record.get('tail_detached', False)):
            return False
        if device is not None and channel is not None and not \
                self._u1_tail_detached_matches(endpoint, device, int(channel)):
            return False
        record['tail_detached'] = False
        record['tail_sensor_cleared'] = False
        self._clear_u1_follower_commit(record)
        record['reason'] = str(reason or 'verified route empty')[:160]
        self.state.save()
        return True

    def _u1_operation_source(self, endpoint_name):
        for device_name, operation in self.active_operations.items():
            if not isinstance(operation, dict):
                continue
            names = list(operation.get('endpoints', []))
            if operation.get('endpoint'):
                names.append(operation.get('endpoint'))
            if endpoint_name not in names:
                continue
            device = self.devices_by_name.get(device_name)
            if device is None:
                continue
            channels = self._u1_routes_for_device_endpoint(
                device, endpoint_name)
            channel = channels[0] if channels else -1
            return device, channel
        return None, -1

    def _arm_u1_persistent_hold(self, endpoint, device=None, channel=-1,
                                reason='BMCU route motion'):
        if (endpoint.driver != 'snapmaker_u1' or
                not endpoint.get('u1_native_feeder_takeover', False)):
            return None
        record = self._u1_ownership_record(endpoint.name)
        if record.get('persistent_hold'):
            if (not record.get('baseline_captured') or
                    not record.get('generation_open')):
                raise BMCUError(
                    '%s has active BMCU ownership without an open native '
                    'feeder baseline' % endpoint.name)
            self._set_u1_lease_state(
                endpoint.name, 'bmcu',
                str(reason or 'BMCU route motion'),
                device=(device.name if device is not None else
                        str(record.get('device', '') or '')),
                channel=(int(channel) if channel is not None else
                         int(record.get('channel', -1))))
            if endpoint.name not in self._u1_persistent_reasserted:
                endpoint.set_native_feeder_enabled(False, save=True)
                self._u1_persistent_reasserted.add(endpoint.name)
            else:
                endpoint.ensure_native_feeder_takeover(
                    save=False, allow_occupied=True)
            endpoint.activate_runtime_sensor_takeover()
            return record
        path = endpoint.native_path_status()
        bmcu_occupied = bool(self._routes_for_endpoint(
            endpoint.name,
            (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)))
        bmcu_occupied = bmcu_occupied or bool(
            self._prestaged_devices_for_endpoint(endpoint.name))
        if path.get('busy') and not bmcu_occupied:
            raise BMCUError(
                'U1 native path %s is occupied (%s); manually retract the stock filament '
                'before BMCU takeover' %
                (endpoint.name, path.get('channel_state', 'unknown')))
        if (bmcu_occupied and
                not (record.get('baseline_captured') and
                     record.get('generation_open'))):
            raise BMCUError(
                '%s has BMCU route ownership without a captured native '
                'feeder baseline' % endpoint.name)
        record = self._capture_u1_baseline(endpoint)
        if device is None:
            device, inferred = self._u1_operation_source(endpoint.name)
            if channel is None or int(channel) < 0:
                channel = inferred
        try:
            channel = int(channel)
        except (TypeError, ValueError, OverflowError):
            channel = -1
        record['persistent_hold'] = True

        record['generation_open'] = True
        record['device'] = device.name if device is not None else ''
        record['device_uid'] = (
            self._device_uid(device) if device is not None else '')
        record['channel'] = channel if 0 <= channel <= 3 else -1
        route = (self._route_state(device, channel)
                 if device is not None and 0 <= channel <= 3
                 else protocol.ROUTE_UNCERTAIN)
        record['route_state'] = protocol.ROUTE_NAMES.get(
            route, 'UNCERTAIN')
        record['reason'] = str(reason or 'BMCU route motion')[:160]
        self._set_u1_lease_state(
            endpoint.name, 'bmcu', record['reason'],
            device=(device.name if device is not None else ''),
            channel=record['channel'])

        self.state.save()
        endpoint.set_native_feeder_enabled(False, save=True)
        self._u1_persistent_reasserted.add(endpoint.name)
        endpoint.activate_runtime_sensor_takeover()
        return record

    def _release_u1_persistent_hold_if_safe(self, endpoint, reason='path empty',
                                                   close_generation=False):
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False
        record = self._u1_ownership_record(endpoint.name)
        if not record.get('persistent_hold'):
            return False
        if self._endpoint_has_loaded_or_active_route(endpoint.name):
            return False
        self._refresh_u1_disconnect_hazards(endpoint.name)
        if self._u1_disconnect_hazards.get(endpoint.name):
            return False
        assigned = self._assigned_routes_for_endpoint(endpoint.name)
        unverified = [
            device.name for device, _channel in assigned
            if device.name not in self._u1_devices_reconciled_once]
        if unverified:
            return False
        path = endpoint.native_path_status()
        if not path.get('known') or path.get('busy'):
            return False
        return self._handoff_u1_to_native(
            endpoint, reason=reason, save=True,
            close_generation=close_generation)

    def _ensure_u1_takeover(self, endpoint, save=False):
        try:
            if endpoint.driver == 'snapmaker_u1' and endpoint.get('u1_native_feeder_takeover', False):
                if save:
                    self._arm_u1_persistent_hold(endpoint)
                else:
                    self._capture_u1_baseline(endpoint)
                    endpoint.ensure_native_feeder_takeover(
                        save=False,
                        allow_occupied=self._endpoint_has_loaded_or_active_route(
                            endpoint.name))
        except Exception:
            logging.exception('BMCU failed to enforce U1 feeder takeover for %s', endpoint.name)
            raise

    def _u1_routes_for_device_endpoint(self, device, endpoint_name):
        return [channel for channel in range(4)
                if self._channel_endpoint_name(device, channel) == endpoint_name]

    def _u1_device_endpoint_unsafe(self, device, endpoint_name):
        channels = self._u1_routes_for_device_endpoint(device, endpoint_name)
        if any(self._route_state(device, channel) != protocol.ROUTE_EMPTY
               for channel in channels):
            return True
        if any(key.startswith(device.name + ':') and
               isinstance(value, dict) and value.get('endpoint') == endpoint_name
               for key, value in self.prestaged.items()):
            return True
        operation = self.active_operations.get(device.name)
        return bool(isinstance(operation, dict) and
                    operation.get('endpoint') == endpoint_name)

    def _set_u1_disconnect_hazard(self, endpoint_name, device_name,
                                  kind, message):
        endpoint_name = str(endpoint_name or '')
        device_name = str(device_name or '')
        if not endpoint_name or not device_name:
            return False
        hazards = self._u1_disconnect_hazards.setdefault(endpoint_name, set())
        changed = device_name not in hazards
        hazards.add(device_name)
        reasons = self._u1_disconnect_hazard_reasons.setdefault(
            endpoint_name, {})
        value = {
            'kind': str(kind or 'unknown'),
            'message': str(message or 'BMCU route state is unresolved'),
        }
        if reasons.get(device_name) != value:
            reasons[device_name] = value
            changed = True
        if changed:
            self._u1_lease_dirty = True
        return changed

    def _clear_u1_disconnect_hazard(self, endpoint_name, device_name,
                                    kind=None):
        endpoint_name = str(endpoint_name or '')
        device_name = str(device_name or '')
        reasons = self._u1_disconnect_hazard_reasons.get(endpoint_name, {})
        current = reasons.get(device_name, {})
        if kind is not None and current.get('kind') != str(kind):
            return False
        hazards = self._u1_disconnect_hazards.get(endpoint_name, set())
        changed = device_name in hazards or device_name in reasons
        hazards.discard(device_name)
        reasons.pop(device_name, None)
        if hazards:
            self._u1_disconnect_hazards[endpoint_name] = hazards
        else:
            self._u1_disconnect_hazards.pop(endpoint_name, None)
        if reasons:
            self._u1_disconnect_hazard_reasons[endpoint_name] = reasons
        else:
            self._u1_disconnect_hazard_reasons.pop(endpoint_name, None)
        if changed:
            self._u1_lease_dirty = True
        return changed

    def _refresh_u1_disconnect_hazards(self, endpoint_name=None):

        names = set()
        if endpoint_name:
            names.add(str(endpoint_name))
        else:
            names.update(self._u1_disconnect_hazards)
            for device in self.devices:
                for channel in range(4):
                    name = self._channel_endpoint_name(device, channel)
                    if name:
                        names.add(name)
        changed = False
        for name in names:
            existing = set(self._u1_disconnect_hazards.get(name, set()))
            if not existing:
                continue
            remaining = set()
            for device_name in existing:
                device = self.devices_by_name.get(device_name)
                if device is None:

                    continue
                routed = self._u1_routes_for_device_endpoint(device, name)
                if not routed:
                    continue
                fresh = bool(
                    device.ready and device.runtime_configured and
                    device.status_reconciled)
                if (not fresh or
                        self._u1_device_endpoint_unsafe(device, name)):
                    remaining.add(device_name)
            reasons = self._u1_disconnect_hazard_reasons.get(name, {})
            for cleared in existing - remaining:
                reasons.pop(cleared, None)
            if remaining:
                if remaining != existing:
                    self._u1_disconnect_hazards[name] = remaining
                    changed = True
                if reasons:
                    self._u1_disconnect_hazard_reasons[name] = reasons
                else:
                    self._u1_disconnect_hazard_reasons.pop(name, None)
            else:
                self._u1_disconnect_hazards.pop(name, None)
                self._u1_disconnect_hazard_reasons.pop(name, None)
                changed = True
        if changed:
            self._u1_lease_dirty = True
        return changed

    def _u1_disconnect_hazard_reason(self, endpoint_name, hazards=None):
        hazards = set(
            self._u1_disconnect_hazards.get(endpoint_name, set())
            if hazards is None else hazards)
        reason_map = self._u1_disconnect_hazard_reasons.get(
            endpoint_name, {})
        details = []
        for device_name in sorted(hazards):
            device = self.devices_by_name.get(device_name)
            reason = reason_map.get(device_name, {})
            reason_text = str(reason.get(
                'message', 'physical BMCU link lost with unresolved route'))
            if device is None:
                details.append('%s: %s; device unavailable' % (
                    device_name, reason_text))
                continue
            channels = []
            for channel in self._u1_routes_for_device_endpoint(
                    device, endpoint_name):
                route = self._route_state(device, channel)
                if route != protocol.ROUTE_EMPTY:
                    channels.append('Channel %d=%s' % (
                        channel + 1, protocol.ROUTE_NAMES.get(
                            route, 'UNCERTAIN')))
            operation = self.active_operations.get(device.name)
            if isinstance(operation, dict) and operation.get('endpoint') == endpoint_name:
                channels.append('operation active')
            suffix = (', '.join(channels) if channels else
                      'snapshot not yet reconciled')
            details.append('%s: %s; %s' % (
                device_name, reason_text, suffix))
        return ('BMCU path safety hold: %s' % '; '.join(details)
                if details else 'BMCU path safety hold')

    def device_connection_changed(self, device, online, was_ready=False,
                                  was_reconciled=False, physical_link=True,
                                  reason=''):

        if getattr(self, '_klippy_disconnecting', False):
            return
        physical_link = bool(physical_link)
        if not online and physical_link:
            self._calibration_policy_suspended.discard(device.name)
        if online and device.ready and device.status_reconciled:
            self._u1_devices_reconciled_once.add(device.name)
        endpoint_names = set(
            self._channel_endpoint_name(device, channel)
            for channel in range(4))
        endpoint_names.discard('')
        for endpoint_name in endpoint_names:
            endpoint = self.endpoints.get(endpoint_name)
            if endpoint is None or endpoint.driver != 'snapmaker_u1':
                continue
            if online:
                if (device.ready and device.status_reconciled and
                        not self._u1_device_endpoint_unsafe(
                            device, endpoint_name)):
                    self._clear_u1_disconnect_hazard(
                        endpoint_name, device.name)
                elif device.ready and device.status_reconciled:

                    self._clear_u1_disconnect_hazard(
                        endpoint_name, device.name, kind='sidecar_ipc')
            elif (physical_link and (was_ready or was_reconciled) and
                  self._u1_device_endpoint_unsafe(device, endpoint_name)):
                self._set_u1_disconnect_hazard(
                    endpoint_name, device.name, 'physical_usb',
                    reason or 'physical BMCU USB link disconnected')

            if online:
                self._refresh_u1_disconnect_hazards(endpoint_name)
        if online and device.ready and device.status_reconciled:
            device.ipc_outage_escalated = False
            if (isinstance(self.last_error, dict) and
                    self.last_error.get('code') == 'SIDECAR_IPC_UNAVAILABLE' and
                    not any(self._u1_disconnect_hazards.values())):
                self.last_error = None
                self._sync_status_cache_runtime()
        self._u1_lease_dirty = True

    def _set_u1_lease_state(self, endpoint_name, owner, reason='', **extra):
        previous = self._u1_lease_state.get(endpoint_name, {})
        current = {'owner': owner, 'reason': str(reason or '')}
        current.update(extra)
        self._u1_lease_state[endpoint_name] = current
        record = self.state.data.get('u1_ownership', {}).get(endpoint_name, {})
        for target in (self._status_cache, self._critical_status_fallback):
            if not isinstance(target, dict):
                continue
            endpoint_status = target.get('endpoints', {}).get(endpoint_name)
            if not isinstance(endpoint_status, dict):
                continue
            owner_status = endpoint_status.get('owner')
            if not isinstance(owner_status, dict):
                continue
            owner_status['owner'] = owner
            owner_status['reason'] = current['reason']
            if isinstance(extra.get('path'), dict):
                path = copy.deepcopy(extra['path'])
                owner_status['path'] = path
                stock = owner_status.get('stock_source')
                if isinstance(stock, dict):
                    stock['input_detected'] = path.get('feeder_input_detected')
                    stock['path_known'] = bool(path.get('known'))
                    stock['path_busy'] = bool(path.get('busy'))
                    stock['channel_state'] = str(
                        path.get('channel_state', 'unknown') or 'unknown')
            stock = owner_status.get('stock_source')
            if (isinstance(stock, dict) and isinstance(record, dict) and
                    record.get('baseline_captured') and
                    record.get('generation_open')):
                stock['material_origin'] = 'captured'
                stock['user_auto_enabled'] = not bool(
                    record.get('baseline_disabled', False))
        return previous.get('owner') != owner or previous.get('reason') != current['reason']

    def _u1_owner_status(self, endpoint):
        assigned = self._assigned_routes_for_endpoint(endpoint.name)
        online = [(device, channel) for device, channel in assigned
                  if device.ready and device.runtime_configured and
                  device.status_reconciled]
        feeder = self._u1_native_feeder_status(endpoint)
        native_disabled = None if feeder is None else bool(
            feeder.get('disable_auto', False))
        value = dict(self._u1_lease_state.get(endpoint.name, {}))
        if not value:
            value = {'owner': 'unknown', 'reason': 'lease not reconciled'}
        ownership = self._u1_ownership_record(endpoint.name)
        evidence = self._u1_endpoint_ownership_evidence(endpoint.name)
        hazards = set(self._u1_disconnect_hazards.get(endpoint.name, set()))
        if hazards:
            value = {
                'owner': 'safety_hold',
                'reason': self._u1_disconnect_hazard_reason(
                    endpoint.name, hazards),
            }
        elif (ownership.get('persistent_hold') or
              any(item['claimed'] for item in evidence) or
              endpoint.name in getattr(self, '_u1_background_jobs', {})):
            busy = any(
                item['raw_state'] == protocol.ROUTE_UNCERTAIN or
                item['inflight'] for item in evidence)
            value = {
                'owner': 'bmcu_busy' if busy else 'bmcu',
                'reason': ('BMCU route operation owns the shared path' if busy
                           else 'BMCU route journal owns the shared path'),
            }
        path = endpoint.native_path_status()
        head = int(endpoint.get('head_index', -1))
        stock_snapshot = {}
        stock_origin = 'unknown'
        if (ownership.get('baseline_captured') and
                ownership.get('generation_open')):
            stock_snapshot = copy.deepcopy(
                ownership.get('baseline_filament', {}))
            stock_origin = 'captured'
        elif value.get('owner') in ('native', 'native_busy'):
            _task, config = self._u1_task_config()
            if isinstance(config, dict) and 0 <= head < 4:
                for key in ('filament_vendor', 'filament_type',
                            'filament_sub_type', 'filament_color_rgba',
                            'filament_official', 'filament_exist'):
                    values = config.get(key)
                    if isinstance(values, list) and head < len(values):
                        stock_snapshot[key] = copy.deepcopy(values[head])
                stock_origin = 'current' if stock_snapshot else 'unknown'
        rgba = str(stock_snapshot.get('filament_color_rgba', '') or '').upper()
        color = '#%s' % rgba[:6] if re.fullmatch(r'[0-9A-F]{8}', rgba) else '#FFFFFF'
        module, native_channel = endpoint.native_feeder_target()
        user_auto_enabled = None
        if ownership.get('baseline_captured') and ownership.get('generation_open'):
            user_auto_enabled = not bool(ownership.get('baseline_disabled', False))
        elif native_disabled is not None:
            user_auto_enabled = not native_disabled
        value.update({
            'path': path,
            'assigned_channels': [
                {'device': device.name, 'channel': channel}
                for device, channel in assigned],
            'online_channels': [
                {'device': device.name, 'channel': channel}
                for device, channel in online],
            'native_feeder_enabled': (None if native_disabled is None
                                      else not native_disabled),
            'stock_source': {
                'tool': head,
                'module': module,
                'channel': native_channel,
                'input_detected': path.get('feeder_input_detected'),
                'path_known': bool(path.get('known')),
                'path_busy': bool(path.get('busy')),
                'channel_state': str(path.get('channel_state', 'unknown') or 'unknown'),
                'runtime_auto_enabled': (None if native_disabled is None
                                         else not native_disabled),
                'user_auto_enabled': user_auto_enabled,
                'material_origin': stock_origin,
                'material': str(stock_snapshot.get('filament_type', '') or ''),
                'vendor': str(stock_snapshot.get('filament_vendor', '') or ''),
                'subtype': str(stock_snapshot.get('filament_sub_type', '') or ''),
                'color': color,
                'official': bool(stock_snapshot.get('filament_official', False)),
                'configured_exists': bool(stock_snapshot.get('filament_exist', False)),
            },
            'disconnect_hazards': sorted(
                self._u1_disconnect_hazards.get(endpoint.name, set())),
            'disconnect_hazard_details': copy.deepcopy(
                self._u1_disconnect_hazard_reasons.get(endpoint.name, {})),
            'persistent_journal': dict(
                self._u1_ownership_record(endpoint.name)),
        })
        return value

    def _reconcile_u1_leases(self, eventtime=None, force=False):
        if getattr(self, '_klippy_disconnecting', False):
            return
        self._u1_lease_dirty = False
        for endpoint in tuple(self.endpoints.values()):
            if endpoint.driver != 'snapmaker_u1':
                continue
            endpoint_started = self.reactor.monotonic()
            assigned = self._assigned_routes_for_endpoint(endpoint.name)
            online = [(device, channel) for device, channel in assigned
                      if device.ready and device.runtime_configured and
                      device.status_reconciled]
            for device, _channel in online:
                self._u1_devices_reconciled_once.add(device.name)
            hazards = set(self._u1_disconnect_hazards.get(
                endpoint.name, set()))
            active = self._endpoint_has_loaded_or_active_route(endpoint.name)
            feeder = self._u1_native_feeder_status(endpoint)
            native_disabled = None if feeder is None else bool(
                feeder.get('disable_auto', False))
            ownership = self._u1_ownership_record(endpoint.name)
            persistent_hold = bool(ownership.get('persistent_hold', False))
            try:
                path = endpoint.native_path_status()
            except Exception as exc:
                path = {'known': False, 'busy': True,
                        'channel_state': 'unknown', 'error': str(exc)}

            managed_journal = bool(
                persistent_hold or ownership.get('generation_open', False) or
                ownership.get('baseline_captured', False))
            if not assigned and not active and not hazards and not managed_journal:
                endpoint.release_runtime_sensor_takeover()
                self._set_u1_lease_state(
                    endpoint.name,
                    'native_busy' if path.get('busy') else 'native',
                    ('no BMCU route assigned; stock feeder untouched' if not
                     path.get('busy') else
                     'native filament loaded; no BMCU route assigned'),
                    path=path, online=False)
                continue

            try:
                if (persistent_hold and endpoint.name not in
                        self._u1_persistent_reasserted):

                    endpoint.set_native_feeder_enabled(False, save=True)
                    endpoint.activate_runtime_sensor_takeover()
                    self._u1_persistent_reasserted.add(endpoint.name)
                    native_disabled = True

                unverified = sorted(set(
                    device.name for device, _channel in assigned
                    if device.name not in self._u1_devices_reconciled_once))
                if (assigned and not online and unverified and
                        not persistent_hold and not hazards):

                    endpoint.release_runtime_sensor_takeover()
                    self._set_u1_lease_state(
                        endpoint.name, 'native',
                        ('BMCU absent; stock feeder state preserved (AUTO=%s)' %
                         ('unknown' if native_disabled is None else
                          ('0' if native_disabled else '1'))),
                        path=path, online=False, unverified=unverified)
                    if (isinstance(self.last_error, dict) and
                            self.last_error.get('code') == 'U1_STARTUP_UNVERIFIED' and
                            self.last_error.get('endpoint') == endpoint.name):
                        self.last_error = None
                        self._sync_status_cache_runtime()
                    continue

                if hazards or (active and not online):

                    if not persistent_hold:
                        source = assigned[0] if assigned else (None, -1)
                        self._arm_u1_persistent_hold(
                            endpoint, source[0], source[1],
                            'offline or disconnected occupied BMCU route')
                        persistent_hold = True
                    else:
                        endpoint.ensure_native_feeder_takeover(
                            save=False, allow_occupied=True)
                    endpoint.activate_runtime_sensor_takeover()
                    reason = (self._u1_disconnect_hazard_reason(
                                  endpoint.name, hazards) if hazards else
                              'BMCU route occupied while controller is offline')
                    changed = self._set_u1_lease_state(
                        endpoint.name, 'safety_hold', reason,
                        path=path, online=bool(online))
                    if changed:
                        self._record_error(
                            'U1_FEEDER_SAFETY_HOLD', endpoint=endpoint.name,
                            details=reason)
                        self._safe_pause()
                    continue

                if online:

                    if not active and not persistent_hold and not hazards:
                        if (ownership.get('generation_open', False) and
                                ownership.get('baseline_captured', False)):
                            if path.get('known') and not path.get('busy'):
                                self._handoff_u1_to_native(
                                    endpoint,
                                    'online BMCU routes are EMPTY',
                                    save=True, close_generation=True)
                                feeder = self._u1_native_feeder_status(endpoint)
                                native_disabled = None if feeder is None else bool(
                                    feeder.get('disable_auto', False))
                            elif native_disabled:
                                endpoint.activate_runtime_sensor_takeover()
                                changed = self._set_u1_lease_state(
                                    endpoint.name, 'safety_hold',
                                    'previous BMCU lease awaits confirmed EMPTY',
                                    path=path, online=True)
                                if changed:
                                    self._record_error(
                                        'U1_PERSISTENT_HOLD',
                                        endpoint=endpoint.name,
                                        details='stock feeder remains disabled until EMPTY is confirmed')
                                continue
                        endpoint.release_runtime_sensor_takeover()
                        self._set_u1_lease_state(
                            endpoint.name,
                            'native_busy' if path.get('busy') else 'native',
                            ('native filament is loaded; BMCU routes remain idle'
                             if path.get('busy') else
                             'BMCU routes are EMPTY; stock feeder remains available'),
                            path=path, online=True)
                        continue

                    self._capture_u1_baseline(endpoint)
                    if active and not persistent_hold:
                        source = next(
                            ((device, channel) for device, channel in online
                             if self._route_state(device, channel) !=
                             protocol.ROUTE_EMPTY),
                            online[0])
                        self._arm_u1_persistent_hold(
                            endpoint, source[0], source[1],
                            'reconciled occupied BMCU route')
                        persistent_hold = True
                        native_disabled = True
                    if (persistent_hold and not active and not hazards and
                            path.get('known') and not path.get('busy')):
                        self._release_u1_persistent_hold_if_safe(
                            endpoint,
                            'online BMCU confirmed every route EMPTY')
                        ownership = self._u1_ownership_record(endpoint.name)
                        persistent_hold = bool(
                            ownership.get('persistent_hold', False))
                        feeder = self._u1_native_feeder_status(endpoint)
                        native_disabled = None if feeder is None else bool(
                            feeder.get('disable_auto', False))

                    if path.get('busy') and not active and not native_disabled:
                        endpoint.release_runtime_sensor_takeover()
                        self._set_u1_lease_state(
                            endpoint.name, 'native_busy',
                            'native filament is loaded; manually retract it before using BMCU',
                            path=path, online=True)
                        continue
                    if path.get('busy') and not active and native_disabled:
                        endpoint.activate_runtime_sensor_takeover()
                        changed = self._set_u1_lease_state(
                            endpoint.name, 'safety_hold',
                            'filament detected in a disabled/unknown path',
                            path=path, online=True)
                        if changed:
                            self._record_error(
                                'U1_PATH_OCCUPIED', endpoint=endpoint.name,
                                details='filament detected before BMCU route ownership could be proven')
                            self._safe_pause()
                        continue
                    endpoint.ensure_native_feeder_takeover(
                        save=False, allow_occupied=active)
                    endpoint.activate_runtime_sensor_takeover()
                    self._set_u1_lease_state(
                        endpoint.name, 'bmcu',
                        'routed BMCU online; native feeder disabled in RAM',
                        path=path, online=True)
                    continue

                if native_disabled and path.get('busy'):
                    changed = self._set_u1_lease_state(
                        endpoint.name, 'safety_hold',
                        'cannot restore native feeder while path is occupied',
                        path=path, online=False)
                    if changed:
                        self._record_error(
                            'U1_NATIVE_RESTORE_BLOCKED', endpoint=endpoint.name,
                            details='shared filament path is not confirmed empty')
                        self._safe_pause()
                    continue
                if persistent_hold:
                    if not self._release_u1_persistent_hold_if_safe(
                            endpoint,
                            'BMCU disconnected after confirmed EMPTY',
                            close_generation=True):
                        changed = self._set_u1_lease_state(
                            endpoint.name, 'safety_hold',
                            'persistent BMCU ownership awaits confirmed EMPTY',
                            path=path, online=False)
                        if changed:
                            self._record_error(
                                'U1_PERSISTENT_HOLD', endpoint=endpoint.name,
                                details='stock feeder remains disabled until EMPTY is confirmed')
                        continue
                else:
                    self._handoff_u1_to_native(
                        endpoint,
                        ('no routed BMCU online' if assigned else
                         'no BMCU route assigned'),
                        save=False, close_generation=True)
                self._set_u1_lease_state(
                    endpoint.name, 'native',
                    ('no routed BMCU online' if assigned else
                     'no BMCU route assigned'),
                    path=path, online=False)
            except Exception as exc:
                changed = self._set_u1_lease_state(
                    endpoint.name, 'error', str(exc), path=path,
                    online=bool(online))
                if changed:
                    self._record_error(
                        'U1_FEEDER_LEASE_FAILED', endpoint=endpoint.name,
                        details=str(exc))
                    self._safe_pause()
            finally:
                endpoint_ms = max(
                    0.0, (self.reactor.monotonic() - endpoint_started) * 1000.0)
                self._u1_lease_endpoint_max_ms[endpoint.name] = max(
                    float(self._u1_lease_endpoint_max_ms.get(
                        endpoint.name, 0.0)), endpoint_ms)

        self._clear_resolved_u1_startup_error()

        self._u1_lease_watchdog_fingerprint = None

    def _clear_resolved_u1_startup_error(self):

        if not isinstance(self.last_error, dict):
            return False
        code = self.last_error.get('code')
        if code not in (
                'U1_STARTUP_UNVERIFIED', 'U1_FEEDER_SAFETY_HOLD',
                'U1_PERSISTENT_HOLD', 'U1_FEEDER_LEASE_FAILED',
                'SIDECAR_IPC_UNAVAILABLE'):
            return False
        self._refresh_u1_disconnect_hazards()
        unresolved = [
            value for value in self._u1_lease_state.values()
            if isinstance(value, dict) and
            value.get('owner') == 'safety_hold']
        if unresolved or any(self._u1_disconnect_hazards.values()):
            return False
        self.last_error = None
        self._sync_status_cache_runtime()
        return True

    def _assigned_routes_for_endpoint(self, endpoint_name, excluded_route=None):
        routes = []
        for device in self.devices:
            for channel in range(4):
                route_key = self._route_key(device, channel)
                if excluded_route is not None and route_key == excluded_route:
                    continue
                if self._channel_endpoint_name(device, channel) == endpoint_name:
                    routes.append((device, channel))
        return routes

    def _assign_channel_endpoint(self, device, channel, endpoint_name, save=True):
        channel = int(channel)
        route_key = self._route_key(device, channel)
        endpoint_name = str(endpoint_name or '').strip()
        durable_tail = self._durable_tail_route(device, channel)
        restoring_tail_route = bool(
            durable_tail and endpoint_name and
            endpoint_name == durable_tail.get('endpoint_name') and
            durable_tail.get('endpoint_compatible', True))
        if self.print_plan_open or self.print_map_active:
            raise BMCUError(
                'routing cannot change while a print plan is open or active')
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise BMCUError('routing cannot change while the printer is active')
        if self.active_operations:
            raise BMCUError(
                'routing cannot change during a BMCU operation')
        refill = getattr(self, 'refill', None)
        if (refill is not None and
                (getattr(refill, 'transactions', {}) or
                 getattr(refill, '_pending', set()))):
            raise BMCUError(
                'routing cannot change during refill recovery')
        if (getattr(self, 'print_transaction_phase', '') or
                getattr(self, 'u1_cross_refill_pending', {}) or
                getattr(self, '_u1_background_jobs', {}) or
                getattr(self, 'prestaged', {})):
            raise BMCUError(
                'routing cannot change during background preparation, '
                'recovery or while a prestage is retained')
        if not restoring_tail_route:
            for ownership in self.state.data.get('u1_ownership', {}).values():
                if (isinstance(ownership, dict) and
                        (ownership.get('tail_detached') or
                         ownership.get('follower_pending'))):
                    raise BMCUError(
                        'routing cannot change while a detached-tail journal is active')
            for candidate in self.devices:
                for candidate_channel in range(4):
                    candidate_record = self._channel_record(
                        candidate, candidate_channel)
                    if (candidate_record.get('tail_detached') or
                            candidate_record.get('tail_follower_pending')):
                        raise BMCUError(
                            'routing cannot change while a detached-tail journal is active')
        channel_record = self._channel_record(device, channel)
        if (not restoring_tail_route and
                (channel_record.get('tail_detached') or
                 channel_record.get('tail_follower_pending'))):
            raise BMCUError(
                'cannot change %s Channel %d routing while its detached-tail '
                'journal is active' % (device.name, channel + 1))
        if (not restoring_tail_route and
                self._route_state(device, channel) != protocol.ROUTE_EMPTY):
            raise BMCUError(
                'unload or confirm EMPTY before changing %s Channel %d routing' %
                (device.name, channel + 1))
        if route_key in self.prestaged:
            raise BMCUError(
                'clear prestage before changing %s Channel %d routing' %
                (device.name, channel + 1))
        if endpoint_name and self.controller_mode != 'standalone':
            raise BMCUError(
                'cannot assign a route while BMCU motion ownership is blocked: %s' %
                (self.controller_block_reason or 'controller mode blocked'))
        if endpoint_name and endpoint_name not in self.endpoints:
            raise BMCUError('unknown endpoint %s' % endpoint_name)
        previous_name = self._channel_endpoint_name(device, channel)
        selected = self.endpoints.get(endpoint_name) if endpoint_name else None
        if previous_name == endpoint_name:
            if (selected is not None and selected.driver == 'snapmaker_u1' and
                    not selected.get('u1_native_feeder_takeover', False)):
                selected.config['u1_native_feeder_takeover'] = True
                self.state.data['endpoints'][endpoint_name][
                    'u1_native_feeder_takeover'] = True
                if save:
                    self.state.save()
                self._u1_lease_dirty = True
            return
        if selected is not None:
            validation = selected.validate()
            if not validation.get('valid', False):
                raise BMCUError(
                    'endpoint %s is incomplete: %s' %
                    (endpoint_name,
                     '; '.join(validation.get('errors', []))))
        snapshot = copy.deepcopy(self.state.data)
        previous = self.endpoints.get(previous_name)
        try:

            if selected is not None and selected.driver == 'snapmaker_u1':
                selected.config['u1_native_feeder_takeover'] = True
                self.state.data['endpoints'][endpoint_name][
                    'u1_native_feeder_takeover'] = True
            channel_record['endpoint'] = endpoint_name
            if not restoring_tail_route:

                channel_record.update({
                    'path_length_mm': 0.0,
                    'path_length_endpoint': '',
                    'path_length_source': 'none',
                    'path_measure_pending': True,
                    'tail_detached': False,
                    'tail_endpoint': '',
                    'tail_path_length_mm': 0.0,
                    'tail_follower_pending': False,
                    'tail_follower_device': '',
                    'tail_follower_uid': '',
                    'tail_follower_channel': -1,
                    'tail_follower_tool': -1,
                })
                self._clear_path_learning_observation(device, channel)
            previous_still_assigned = bool(
                previous_name and self._assigned_routes_for_endpoint(
                    previous_name, excluded_route=route_key))
            if previous is not None and previous_name != endpoint_name and not previous_still_assigned:
                previous.release_runtime_sensor_takeover()
                if (previous.driver == 'snapmaker_u1' and
                        previous.get('u1_native_feeder_takeover', False)):
                    previous.config['u1_native_feeder_takeover'] = False
                    if previous_name in self.state.data['endpoints']:
                        self.state.data['endpoints'][previous_name][
                            'u1_native_feeder_takeover'] = False
            if save:
                self.state.save()
            self._u1_lease_dirty = True
        except Exception as exc:
            self.state.data = snapshot
            self._load_endpoints()
            if isinstance(exc, BMCUError):
                raise
            raise BMCUError(str(exc))

    def _devices_using_endpoint(self, endpoint_name, excluded_device=None):
        routed = []
        seen = set()
        for device in self.devices:
            if excluded_device is not None and device.name == excluded_device.name:
                continue
            if any(self._channel_endpoint_name(device, channel) == endpoint_name
                   for channel in range(4)):
                if device.name not in seen:
                    routed.append(device)
                    seen.add(device.name)
        return routed

    def _endpoint_in_use_by_other_device(self, endpoint_name, excluded_device=None):
        return bool(self._devices_using_endpoint(endpoint_name, excluded_device))

    def _require_exclusive_endpoint_assignment(self, device, endpoint_name):

        if endpoint_name not in self.endpoints:
            raise BMCUError('unknown endpoint %s' % endpoint_name)

    def _endpoint_routed(self, endpoint_name, excluded_device=None):
        return self._endpoint_in_use_by_other_device(endpoint_name, excluded_device)

    def _prestaged_devices_for_endpoint(self, endpoint_name, excluded_device=None,
                                        excluded_route=None):
        staged = []
        for route_key, record in self.prestaged.items():
            if not isinstance(record, dict) or record.get('endpoint') != endpoint_name:
                continue
            if excluded_route is not None and route_key == excluded_route:
                continue
            device = self.devices_by_name.get(record.get('device'))
            if device is None:
                device_name = str(route_key).split(':', 1)[0]
                device = self.devices_by_name.get(device_name)
            if device is None:
                continue
            if excluded_device is not None and device.name == excluded_device.name:
                continue
            staged.append((device, record))
        return staged

    def _u1_route_ownership_evidence(self, device, channel):

        channel = int(channel)
        route_key = self._route_key(device, channel)
        endpoint_name = self._channel_endpoint_name(device, channel)
        raw_state = self._route_state(device, channel)
        try:
            journalled = bool(
                self._journal_route_aliases(device, channel).intersection(
                    self.print_loaded_routes))
        except Exception:
            journalled = False
        runtime_tool = self.loaded_tools.get(route_key)
        prestaged = route_key in self.prestaged
        operation = self.active_operations.get(device.name)
        operation_active = False
        if isinstance(operation, dict):
            operation_endpoints = list(operation.get('endpoints', []))
            if operation.get('endpoint'):
                operation_endpoints.append(operation.get('endpoint'))
            operation_channel = operation.get('channel', -1)
            try:
                operation_channel = int(operation_channel)
            except (TypeError, ValueError, OverflowError):
                operation_channel = -1
            operation_active = bool(
                endpoint_name in operation_endpoints and
                operation_channel in (-1, channel, 0xff))

        ownership = self.state.data.get('u1_ownership', {}).get(
            endpoint_name, {})
        ownership_match = False
        ownership_state = 'EMPTY'
        if isinstance(ownership, dict):
            try:
                ownership_channel = int(ownership.get('channel', -1))
            except (TypeError, ValueError, OverflowError):
                ownership_channel = -1
            expected_uid = str(
                ownership.get('device_uid', '') or '').upper()
            actual_uid = self._device_uid(device)
            same_device = bool(
                expected_uid and actual_uid and expected_uid == actual_uid)
            if not expected_uid:
                same_device = str(
                    ownership.get('device', '') or '') == device.name
            ownership_match = bool(
                ownership.get('persistent_hold') and same_device and
                ownership_channel == channel)
            ownership_state = str(
                ownership.get('route_state', 'EMPTY') or 'EMPTY').upper()

        durable_loaded = bool(journalled or runtime_tool is not None or
                              (ownership_match and
                               ownership_state in ('LOADED', 'UNCERTAIN')))
        inflight = bool(prestaged or operation_active)
        claimed = bool(raw_state != protocol.ROUTE_EMPTY or durable_loaded or
                       inflight or ownership_match)
        mismatch = bool(
            raw_state == protocol.ROUTE_EMPTY and durable_loaded and
            not inflight)
        return {
            'device': device,
            'channel': channel,
            'route_key': route_key,
            'endpoint': endpoint_name,
            'raw_state': raw_state,
            'journalled': journalled,
            'runtime_tool': runtime_tool,
            'prestaged': prestaged,
            'operation_active': operation_active,
            'ownership_match': ownership_match,
            'ownership_state': ownership_state,
            'durable_loaded': durable_loaded,
            'inflight': inflight,
            'claimed': claimed,
            'mismatch': mismatch,
        }

    def _u1_endpoint_ownership_evidence(self, endpoint_name):
        evidence = []
        for device, channel in self._assigned_routes_for_endpoint(endpoint_name):
            evidence.append(self._u1_route_ownership_evidence(device, channel))
        return evidence

    @staticmethod
    def _u1_evidence_label(evidence):
        device = evidence.get('device')
        name = device.name if device is not None else 'unknown BMCU'
        return '%s Channel %d' % (
            name, int(evidence.get('channel', -1)) + 1)

    def _u1_require_consistent_route_ownership(
            self, endpoint_name, allowed_uncertain_routes=None):

        evidence = self._u1_endpoint_ownership_evidence(endpoint_name)
        allowed_uncertain_routes = set(allowed_uncertain_routes or ())
        uncertain = [item for item in evidence
                     if (item['raw_state'] == protocol.ROUTE_UNCERTAIN and
                         item['route_key'] not in allowed_uncertain_routes)]
        mismatched = [item for item in evidence if item['mismatch']]
        if uncertain:
            raise BMCUError(
                '%s has an UNCERTAIN BMCU route: %s' % (
                    endpoint_name, ', '.join(
                        self._u1_evidence_label(item)
                        for item in uncertain)))
        if mismatched:
            raise BMCUError(
                '%s route journal disagrees with the live EMPTY snapshot: %s; '
                'confirm the physical route before moving filament' % (
                    endpoint_name, ', '.join(
                        self._u1_evidence_label(item)
                        for item in mismatched)))
        loaded = [item for item in evidence
                  if item['raw_state'] == protocol.ROUTE_LOADED]
        if len(loaded) > 1:
            raise BMCUError(
                '%s has multiple loaded BMCU routes: %s' % (
                    endpoint_name, ', '.join(
                        self._u1_evidence_label(item)
                        for item in loaded)))
        return evidence

    def _endpoint_has_loaded_or_active_route(self, endpoint_name):
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is not None and endpoint.driver == 'snapmaker_u1':
            for evidence in self._u1_endpoint_ownership_evidence(endpoint_name):
                if (evidence['raw_state'] != protocol.ROUTE_EMPTY or
                        evidence['durable_loaded'] or evidence['inflight']):
                    return True
        elif self._routes_for_endpoint(
                endpoint_name,
                (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)):
            return True
        if self._prestaged_devices_for_endpoint(endpoint_name):
            return True
        for device in self.devices:
            for channel in range(4):
                if self._channel_endpoint_name(device, channel) != endpoint_name:
                    continue
                record = self._channel_record(device, channel)
                if (record.get('tail_detached') or
                        record.get('tail_follower_pending')):
                    return True
        ownership = self.state.data.get('u1_ownership', {}).get(endpoint_name, {})
        if isinstance(ownership, dict) and (
                ownership.get('tail_detached') or
                ownership.get('follower_pending')):
            return True
        for operation in self.active_operations.values():
            if not isinstance(operation, dict):
                continue
            operation_endpoints = list(operation.get('endpoints', []))
            if operation.get('endpoint'):
                operation_endpoints.append(operation.get('endpoint'))
            if endpoint_name in operation_endpoints:
                return True
        refill = getattr(self, 'refill', None)
        if refill is not None:
            transactions = getattr(refill, 'transactions', {})
            if isinstance(transactions, dict):
                for transaction in transactions.values():
                    if not isinstance(transaction, dict):
                        continue
                    if endpoint_name in (
                            transaction.get('endpoint'),
                            transaction.get('candidate_endpoint')):
                        return True
            pending = getattr(refill, '_pending', set())
            for route_key in pending if isinstance(pending, set) else ():
                try:
                    device_name, raw_channel = str(route_key).rsplit(':', 1)
                    device = self.devices_by_name.get(device_name)
                    channel = int(raw_channel)
                except (TypeError, ValueError):
                    continue
                if (device is not None and 0 <= channel <= 3 and
                        self._channel_endpoint_name(device, channel) == endpoint_name):
                    return True
        return False

    def _release_prestage_sensor_ownership_if_unused(self, endpoint_name):
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None:
            return
        if self._loaded_devices_for_endpoint(endpoint_name):
            return
        if self._prestaged_devices_for_endpoint(endpoint_name):
            return
        endpoint.release_runtime_sensor_takeover()

    def _drop_prestage_record(self, route_or_device, release_sensor=True):
        text = str(route_or_device)
        keys = ([text] if ':' in text else
                [key for key in list(self.prestaged)
                 if key == text or key.startswith(text + ':')])
        removed = []
        endpoints = set()
        for key in keys:
            staged = self.prestaged.pop(key, None)
            if staged:
                removed.append(staged)
                endpoints.add(staged.get('endpoint', ''))
        if release_sensor:
            for endpoint_name in endpoints:
                self._release_prestage_sensor_ownership_if_unused(endpoint_name)
        if not removed:
            return None
        return removed[0] if len(removed) == 1 else removed

    def _restore_endpoint_runtime_ownership(self, endpoint_name):
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None:
            return
        routed_active = bool(self._routes_for_endpoint(
            endpoint_name,
            (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)))
        if endpoint.driver == 'snapmaker_u1' and endpoint.get('u1_native_feeder_takeover', False):
            self._ensure_u1_takeover(endpoint, save=False)
        if routed_active and endpoint.wants_runtime_sensor_takeover():
            endpoint.activate_runtime_sensor_takeover()

    def _loaded_devices_for_endpoint(self, endpoint_name, excluded_device=None,
                                     excluded_route=None):
        loaded = []
        for device, channel, _route in self._routes_for_endpoint(
                endpoint_name, (protocol.ROUTE_LOADED,)):
            if excluded_device is not None and device.name == excluded_device.name:
                continue
            if excluded_route is not None and self._route_key(device, channel) == excluded_route:
                continue
            loaded.append((device, channel))
        return loaded

    def _halt_endpoint_route_conflict(self, endpoint_name):
        occupied = self._loaded_devices_for_endpoint(endpoint_name)
        if len(occupied) < 2:
            self._conflicted_endpoints.discard(endpoint_name)
            return False
        self._conflicted_endpoints.add(endpoint_name)
        summary = ', '.join('%s Channel %d' % (device.name, channel + 1)
                            for device, channel in occupied)
        details = ('multiple independent BMCU Channels report the same occupied Endpoint %s: %s' %
                   (endpoint_name, summary))
        stopped = set()
        for device, channel in occupied:
            if device.name not in stopped:
                try:
                    device.stop_all()
                except Exception:
                    logging.exception('BMCU could not stop conflicting device %s', device.name)
                stopped.add(device.name)
            self.loaded_tools.pop(self._route_key(device, channel), None)
        self.active_tool = -1
        duplicate = (isinstance(self.last_error, dict) and
                     self.last_error.get('code') == 'ENDPOINT_MULTIPLE_CHANNELS' and
                     self.last_error.get('details') == details)
        if not duplicate:
            if self.owns_filament_callbacks:
                try:
                    self._clear_endpoint_projection(self.endpoints.get(endpoint_name))
                except Exception:
                    logging.exception('BMCU could not clear projection for conflicting Endpoint %s', endpoint_name)
            self._record_error('ENDPOINT_MULTIPLE_CHANNELS', details=details)
            self._safe_pause()
        return True

    def _require_exclusive_endpoint_route(self, device, endpoint, channel=None):
        excluded_route = (self._route_key(device, channel)
                          if channel is not None else None)
        occupied = self._loaded_devices_for_endpoint(
            endpoint.name, excluded_route=excluded_route)
        if not occupied:
            return
        summary = ', '.join('%s Channel %d' % (other.name, other_channel + 1)
                            for other, other_channel in occupied)
        raise BMCUError(
            'endpoint %s is already occupied by %s; unload that route before loading another Channel' %
            (endpoint.name, summary))

    def _lock(self, device, endpoint, operation, channel=None):
        active = self.active_operations.get(device.name)
        if active is not None:
            raise BMCUError('%s is busy: %s' % (device.name, active))
        if endpoint.name in self.endpoint_locks:
            raise BMCUError('endpoint %s is busy' % endpoint.name)
        path_group = endpoint.shared_path_group()
        if path_group in self.path_locks:
            raise BMCUError('shared path %s is busy' % path_group)
        record = {'name': operation, 'device': device.name, 'endpoint': endpoint.name,
                  'shared_path_group': path_group}
        if channel is not None:
            record['channel'] = int(channel)
        self.active_operations[device.name] = record
        self.endpoint_locks.add(endpoint.name)
        self.path_locks.add(path_group)

    def _unlock(self, device, endpoint):
        operation = self.active_operations.get(device.name, {})
        self.endpoint_locks.discard(endpoint.name)
        self.path_locks.discard(operation.get('shared_path_group', endpoint.shared_path_group()))
        self.active_operations.pop(device.name, None)

    def _lock_channel_input(self, device, operation, channel):
        active = self.active_operations.get(device.name)
        if active is not None:
            raise BMCUError('%s is busy: %s' % (device.name, active))
        self.active_operations[device.name] = {
            'name': operation, 'device': device.name,
            'endpoint': '', 'channel': int(channel),
            'input_side_only': True,
        }

    def _unlock_channel_input(self, device):
        self.active_operations.pop(device.name, None)

    def _lock_refill(self, source_device, replacement_device, endpoint, operation,
                     replacement_endpoint=None):
        devices = []
        for device in (source_device, replacement_device):
            if device is not None and device.name not in [item.name for item in devices]:
                devices.append(device)
        endpoints = []
        for item in (endpoint, replacement_endpoint):
            if item is not None and item.name not in [value.name for value in endpoints]:
                endpoints.append(item)
        for device in devices:
            if device.name in self.active_operations:
                raise BMCUError('%s is busy' % device.name)
        path_groups = []
        for item in endpoints:
            if item.name in self.endpoint_locks:
                raise BMCUError('endpoint %s is busy' % item.name)
            group = item.shared_path_group()
            if group in self.path_locks:
                raise BMCUError('shared path %s is busy' % group)
            if group not in path_groups:
                path_groups.append(group)
        for item in endpoints:
            self.endpoint_locks.add(item.name)
        for group in path_groups:
            self.path_locks.add(group)
        endpoint_names = [item.name for item in endpoints]
        for device in devices:
            self.active_operations[device.name] = {
                'name': operation, 'device': device.name,
                'endpoint': endpoint_names[0] if endpoint_names else '',
                'endpoints': list(endpoint_names),
                'shared_path_group': path_groups[0] if path_groups else '',
                'shared_path_groups': list(path_groups), 'refill': True,
            }

    def _unlock_refill(self, source_device, replacement_device, endpoint,
                       replacement_endpoint=None):
        devices = []
        for device in (source_device, replacement_device):
            if device is not None and device.name not in [item.name for item in devices]:
                devices.append(device)
        endpoint_names = []
        path_groups = []
        for item in (endpoint, replacement_endpoint):
            if item is None:
                continue
            if item.name not in endpoint_names:
                endpoint_names.append(item.name)
            group = item.shared_path_group()
            if group not in path_groups:
                path_groups.append(group)
        for device in devices:
            operation = self.active_operations.pop(device.name, {})
            for name in operation.get('endpoints', []):
                if name not in endpoint_names:
                    endpoint_names.append(name)
            for group in operation.get('shared_path_groups', []):
                if group not in path_groups:
                    path_groups.append(group)
        for name in endpoint_names:
            self.endpoint_locks.discard(name)
        for group in path_groups:
            self.path_locks.discard(group)

    def _debug_log(self, message, *args):
        if self.debug_enabled:
            logging.info('BMCU DEBUG ' + message, *args)

    def _set_phase(self, device, phase):
        operation = self.active_operations.get(device.name)
        if operation is not None:
            previous = str(operation.get('phase', '') or '')
            now = self.reactor.monotonic()
            previous_started = operation.get('_debug_phase_started_at')
            previous_elapsed_ms = None
            if previous_started is not None:
                try:
                    previous_elapsed_ms = max(
                        0.0, (float(now) - float(previous_started)) * 1000.0)
                except (TypeError, ValueError, OverflowError):
                    previous_elapsed_ms = None
            operation['phase'] = str(phase)
            operation['_debug_phase_started_at'] = float(now)
            if self.debug_enabled and previous != str(phase):
                perf = {}
                try:
                    perf = device.performance_status() or {}
                except Exception:
                    perf = {}
                cached_status = []
                status = getattr(device, 'status', {}) or {}
                for raw_channel in operation.get('channels', []):
                    try:
                        channel = int(raw_channel)
                        cached_status.append({
                            'ch': channel + 1,
                            'buffer': status.get('buffer_pct', [])[channel],
                            'meters': status.get('meters', [])[channel],
                            'pwm': status.get('motor_pwm', [])[channel],
                            'motion': status.get('motion', [])[channel],
                            'present': status.get('present', [])[channel],
                        })
                    except (TypeError, ValueError, IndexError, KeyError):
                        continue
                self._debug_log(
                    '%s phase %s -> %s prev_elapsed_ms=%s endpoints=%s '
                    'channels=%s transport_pending=%s tx_bytes=%s '
                    'last_rx_age_ms=%s quiesced=%s paused=%s cached=%s',
                    device.name, previous or 'NONE', str(phase),
                    ('-' if previous_elapsed_ms is None else
                     '%.3f' % previous_elapsed_ms),
                    ','.join(str(v) for v in operation.get('endpoints', [])) or '-',
                    ','.join(str(v) for v in operation.get('channels', [])) or '-',
                    perf.get('pending_requests', '-'),
                    perf.get('tx_queue_bytes', '-'),
                    ('-' if perf.get('last_rx_age_s') is None else
                     '%.3f' % (float(perf.get('last_rx_age_s', 0.0)) * 1000.0)),
                    perf.get('reactor_quiesced', '-'),
                    perf.get('transport_paused', '-'),
                    json.dumps(cached_status, sort_keys=True,
                               separators=(',', ':')))

    def _validate_endpoint_for_operation(self, endpoint,
                                         require_u1_ownership=True):
        try:
            validation = endpoint.validate()
            if validation.get('errors'):
                raise BMCUError('endpoint %s is invalid: %s' %
                                (endpoint.name,
                                 '; '.join(validation['errors'])))
            if endpoint.driver == 'generic_single_extruder':
                try:
                    endpoint.validate_generic_operation_contract()
                except Exception as exc:
                    raise BMCUError(
                        'endpoint %s generic toolhead macros are not ready: %s' %
                        (endpoint.name, exc))
            if endpoint.driver == 'snapmaker_u1':
                integration = endpoint.integration_check(
                    require_runtime_ownership=bool(require_u1_ownership))
                failed = [item for item in integration.get('checks', [])
                          if item.get('required') and not item.get('ok')]
                if failed:
                    details = '; '.join('%s: %s' %
                                       (item.get('name'),
                                        item.get('detail', ''))
                                       for item in failed)
                    raise BMCUError(
                        'endpoint %s U1 integration check failed: %s' %
                        (endpoint.name, details))
                validation['integration'] = integration
            return validation
        except BMCUError:
            raise
        except Exception as exc:
            raise BMCUError(
                'endpoint %s validation failed: %s' %
                (endpoint.name, exc))

    def _wait(self, device, predicate, timeout, code, details):
        deadline = self.reactor.monotonic() + timeout
        next_poll = 0.0
        while True:
            if predicate():
                return
            now = self.reactor.monotonic()
            if now >= deadline:
                raise BMCUError('%s: %s' % (code, details))
            if now >= next_poll:
                try:
                    device.send(protocol.MSG_GET_STATUS)
                except Exception:
                    pass
                next_poll = now + 0.15
            self.reactor.pause(min(deadline, now + 0.100))

    @staticmethod
    def _apply_endpoint_sensor_result(result, sensor_triggered):
        result = dict(result)
        if not sensor_triggered:
            return result

        if result.get('reason') in ('aborted', 'target', 'contact', 'distance_limit', 'none'):
            result.update({
                'ok': True,
                'reason': 'endpoint_sensor',
                'sensor_triggered': True,
            })
        return result

    def _effective_feed_timeout(self, device, maximum_mm, configured_timeout):
        try:
            speed = float(device.motion_config.get(
                protocol.CONFIG_LOAD_SPEED_MMS, 80.0) or 80.0)
        except (TypeError, ValueError, OverflowError):
            speed = 80.0
        if not math.isfinite(speed) or speed < 1.0:
            speed = 80.0
        try:
            maximum = max(0.0, float(maximum_mm))
            configured = max(1.0, float(configured_timeout))
        except (TypeError, ValueError, OverflowError):
            maximum, configured = 0.0, 45.0

        return min(300.0, max(configured, maximum / speed + 10.0))

    @staticmethod
    def _u1_has_authoritative_entry_sensor(endpoint):
        return bool(
            endpoint is not None and
            endpoint.driver == 'snapmaker_u1' and
            str(endpoint.get('entry_sensor', '') or '').strip())

    @staticmethod
    def _u1_load_pressure_pct(device):
        try:
            value = float(device.motion_config.get(
                protocol.CONFIG_LOAD_PRESSURE_PCT, 82.0) or 82.0)
        except (TypeError, ValueError, OverflowError, AttributeError):
            value = 82.0
        if not math.isfinite(value):
            value = 82.0
        return max(75.0, min(95.0, value))

    def _wait_u1_firmware_pressure_hold(
            self, device, channel, endpoint, configured_timeout,
            arrival_evidence=None):

        arrival_evidence = (arrival_evidence
                            if isinstance(arrival_evidence, dict) else {})
        if not arrival_evidence.get('sensor_triggered'):
            raise BMCUError(
                'U1_LOAD_PRESSURE_NOT_READY: no Head entry-sensor confirmation')
        try:
            buffer_pct = int(device.status['buffer_pct'][channel])
            motion = int(device.status['motion'][channel])
        except (TypeError, ValueError, KeyError, IndexError):
            buffer_pct = -1
            motion = -1
        evidence = {
            'arrival_motion': bool(arrival_evidence.get('sensor_triggered')),
            'arrival_contact': bool(arrival_evidence.get('controller_contact')),
            'buffer_pct': buffer_pct,
            'configured_target_pct': self._u1_load_pressure_pct(device),
            'motion': motion,
            'firmware_owned': True,
            'host_waited': False,
        }
        logging.info(
            'BMCU Snapmaker %s pressure control handed to firmware: '
            'buffer=%d%% target=%.0f%% motion=%d',
            endpoint.name, buffer_pct, evidence['configured_target_pct'], motion)
        return evidence

    def _start_endpoint_arrival_operation(
            self, device, channel, endpoint, maximum_mm, contact_pct,
            timeout_s, parked_precharge=False):

        timeout_ms = int(float(timeout_s) * 1000.0)
        sensor_authoritative = bool(
            endpoint is not None and
            str(endpoint.get('entry_sensor', '') or '').strip())
        if (endpoint is not None and endpoint.driver == 'snapmaker_u1' and
                not sensor_authoritative):
            raise BMCUError('U1_ENTRY_SENSOR_REQUIRED: configure the Head entry sensor')
        if self._u1_has_authoritative_entry_sensor(endpoint):
            if not parked_precharge:
                endpoint.verify_selected()
            endpoint.require_entry_sensor_snapshot(timeout=1.25, expected=False)
        elif sensor_authoritative:
            if endpoint.sensor_detected('entry_sensor') is not False:
                raise BMCUError(
                    'ENTRY_SENSOR_NOT_CLEAR: configured arrival sensor must '
                    'confirm an empty entry before SEND_OUT')
        if sensor_authoritative:
            target_pct = int(contact_pct) if parked_precharge else 98
            deadline = self.reactor.monotonic() + timeout_s + 2.0
            start = (device.start_feed_to_contact if parked_precharge
                     else device.start_feed_distance)
            op_id = start(
                channel, maximum_mm, target_pct, timeout_ms)
            return op_id, {
                'sensor_authoritative': True,
                'contact_authoritative': False,
                'precharge_buffer_pct': target_pct,
                'contact_pct': target_pct,
                'operation_timeout_ms': timeout_ms,
                'deadline': deadline,
                'parked_precharge': bool(parked_precharge),
                'operation': ('u1_parked_neutral_precharge'
                              if parked_precharge else
                              'feed_to_endpoint_sensor'),
            }
        op_id = device.start_feed_to_contact(
            channel, maximum_mm, contact_pct, timeout_ms)
        return op_id, {
            'sensor_authoritative': False,
            'contact_buffer_pct': int(contact_pct),
            'parked_precharge': False,
            'operation': 'feed_to_controller_contact',
        }

    def _resolve_endpoint_arrival_result(
            self, device, channel, endpoint, result, policy,
            configured_timeout, allow_partial=False):

        result = dict(result or {})
        if endpoint is None:
            return result
        sensor_authoritative = bool(
            isinstance(policy, dict) and policy.get('sensor_authoritative'))
        if not sensor_authoritative:
            return result

        result['sensor_authoritative'] = True
        result['controller_contact'] = False
        if result.get('cancel_requested'):
            result['ok'] = False
            return result
        if result.get('ok') and result.get('sensor_triggered'):
            result.update({
                'ok': True,
                'reason': 'endpoint_sensor',
                'sensor_triggered': True,
            })
            return result

        reason = str(result.get('reason', '') or '')
        if (reason == 'aborted' and
                result.get('foreground_handoff_requested')):
            return result
        parked_precharge = bool(policy.get('parked_precharge'))
        if not (allow_partial and parked_precharge and reason == 'contact'):
            result['ok'] = False
            if reason in ('target', 'contact', 'none'):
                result['reason'] = 'entry_sensor_not_triggered'
            return result
        if (not result.get('ok') or
                int(result.get('state', -1)) != protocol.OP_STATE_DONE or
                int(result.get('channel', -1)) != int(channel)):
            result['ok'] = False
            return result

        device.set_motion(channel, protocol.MOTION_IDLE)
        device.refresh()
        present = bool(device.status['present'][channel])
        connected = bool(device.status['connected_mask'] & (1 << channel))
        encoder_ok = bool(device.status['encoder_io_mask'] & (1 << channel))
        motion = int(device.status['motion'][channel])
        buffer_pct = int(device.status['buffer_pct'][channel])
        controller_faults = int(device.status.get(
            'controller_fault_flags', 0) or 0)
        nvm_fault = bool(device.status.get('nvm_fault', False))
        route_state = self._route_states_from_status(device.status)[channel]
        if (not present or not connected or not encoder_ok or
                controller_faults or nvm_fault or
                motion != protocol.MOTION_IDLE or
                route_state not in (
                    protocol.ROUTE_EMPTY, protocol.ROUTE_UNCERTAIN) or
                not (2 < buffer_pct < 100)):
            raise BMCUError(
                'U1_ARRIVAL_BOUNDARY_INVALID: present=%d connected=%d '
                'encoder=%d motion=%d buffer=%d controller_faults=%d '
                'nvm_fault=%d route=%s' %
                (int(present), int(connected), int(encoder_ok), motion,
                 buffer_pct, controller_faults, int(nvm_fault), route_state))

        precharge_pct = int(policy.get('precharge_buffer_pct', buffer_pct))
        result.update({
            'ok': True,
            'reason': 'sensor_precharge_neutral',
            'sensor_precharge_hold': False,
            'sensor_precharge_neutral': True,
            'partial': True,
            'precharge_buffer_pct': precharge_pct,
            'observed_buffer_pct': buffer_pct,
            'target_selected_at_precharge': False,
        })
        logging.info(
            'BMCU Snapmaker %s parked precharge ended neutral IDLE at '
            '%d%%; selected Head will continue to its entry sensor',
            endpoint.name, buffer_pct)
        return result

    def _wait_unload_buffer_ready(self, device, channel, timeout=4.0,
                                  pause_for_critical=False,
                                  cancel_check=None):
        deadline = self.reactor.monotonic() + max(0.5, float(timeout))
        next_poll = 0.0
        while True:
            cancel_requested = bool(
                callable(cancel_check) and cancel_check())
            if (pause_for_critical and
                    self._critical_control_plane_blocked()):
                before = self.reactor.monotonic()
                cancel_requested = bool(
                    self._wait_background_control_plane(
                        cancel_check=cancel_check) or cancel_requested)
                deadline += max(
                    0.0, self.reactor.monotonic() - before)
            if cancel_requested:
                try:
                    device.stop_all()
                except Exception:
                    pass
                raise BMCUError(
                    'UNLOAD_BUFFER_CANCELLED: operation was cancelled')
            if not device.connected:
                raise BMCUError('DEVICE_OFFLINE during unload release')
            buffer_pct = int(device.status['buffer_pct'][channel])
            if 5 < buffer_pct < 95:
                return buffer_pct
            now = self.reactor.monotonic()
            if now >= deadline:
                raise BMCUError(
                    'UNLOAD_BUFFER_NOT_READY: buffer remained at %d%%' %
                    buffer_pct)
            if now >= next_poll:
                try:
                    device.send(protocol.MSG_GET_STATUS)
                except Exception:
                    pass
                next_poll = now + 0.15
            self.reactor.pause(min(deadline, now + 0.100))

    def _wait_pullback_safe(self, device, channel, timeout,
                            cancel_check=None, start_m=None,
                            poll_interval=0.100,
                            pause_for_critical=False,
                            progress_callback=None,
                            allow_transient_buffer_extremes=False):
        try:
            poll_interval = max(0.050, min(1.0, float(poll_interval)))
        except (TypeError, ValueError, OverflowError):
            poll_interval = 0.100
        deadline = self.reactor.monotonic() + float(timeout)
        next_poll = 0.0
        extreme_since = None
        extreme_samples = 0
        last_status_revision = int(getattr(device, 'status_revision', 0))
        if start_m is None:
            start_m = float(device.status['meters'][channel])
        else:
            try:
                start_m = float(start_m)
            except (TypeError, ValueError, OverflowError):
                raise BMCUError('PULLBACK_METER_INVALID: start position is invalid')
            if not math.isfinite(start_m):
                raise BMCUError('PULLBACK_METER_INVALID: start position is not finite')
        if pause_for_critical:
            self._wait_background_control_plane(cancel_check=cancel_check)
        device.refresh()
        while True:
            cancel_requested = bool(
                callable(cancel_check) and cancel_check())
            if (pause_for_critical and
                    self._critical_control_plane_blocked()):
                before = self.reactor.monotonic()
                cancel_requested = bool(
                    self._wait_background_control_plane(
                        cancel_check=cancel_check,
                        poll_interval=poll_interval) or cancel_requested)
                deadline += max(
                    0.0, self.reactor.monotonic() - before)
            if cancel_requested:
                try:
                    device.stop_all()
                except Exception:
                    pass
                raise BMCUError('PULLBACK_CANCELLED: operation was cancelled')
            if not device.connected:
                raise BMCUError('DEVICE_OFFLINE during BMCU pullback')
            motion = int(device.status['motion'][channel])
            buffer_pct = int(device.status['buffer_pct'][channel])
            encoder_mm = abs(
                float(device.status['meters'][channel]) - start_m) * 1000.0
            now = self.reactor.monotonic()
            if callable(progress_callback):
                try:
                    progress_callback({
                        'motion': motion, 'buffer_pct': buffer_pct,
                        'encoder_mm': encoder_mm, 'eventtime': now,
                    })
                except Exception:

                    logging.exception(
                        'BMCU pullback progress callback failed; continuing pullback')
                    progress_callback = None
            if motion == protocol.MOTION_IDLE:
                return {
                    'buffer_pct': buffer_pct,
                    'encoder_mm': encoder_mm,
                }
            if not allow_transient_buffer_extremes:
                status_revision = int(
                    getattr(device, 'status_revision', 0))
                if status_revision != last_status_revision:
                    last_status_revision = status_revision
                    if buffer_pct <= 2 or buffer_pct >= 98:
                        if extreme_samples == 0:
                            extreme_since = now
                        extreme_samples += 1
                        if (extreme_samples >= 3 and
                                extreme_since is not None and
                                now - extreme_since >= 0.40):
                            try:
                                device.stop_all()
                            except Exception:
                                pass
                            raise BMCUError(
                                'PARK_JAM: buffer remained at %d%% across '
                                '%d fresh status frames; motors stopped' %
                                (buffer_pct, extreme_samples))
                    else:
                        extreme_since = None
                        extreme_samples = 0

            if now >= deadline:
                try:
                    device.stop_all()
                except Exception:
                    pass
                raise BMCUError(
                    'PARK_TIMEOUT: BMCU pullback did not finish')
            if now >= next_poll:
                try:
                    device.send(protocol.MSG_GET_STATUS)
                except Exception:
                    pass

                minimum_query_interval = (
                    1.00 if allow_transient_buffer_extremes else 0.15)
                next_poll = now + max(minimum_query_interval, poll_interval)
            self.reactor.pause(min(deadline, now + poll_interval))

    def _wait_feed_operation(self, device, op_id, timeout, endpoint=None,
                             sensor_role='', poll_interval=None,
                             pause_for_critical=False,
                             interrupt_check=None, cancel_check=None):
        if poll_interval is None:
            poll_interval = (0.100 if endpoint is not None and sensor_role
                             else 0.250)
        try:
            poll_interval = max(0.050, min(1.0, float(poll_interval)))
        except (TypeError, ValueError, OverflowError):
            poll_interval = 0.250
        deadline = (None if timeout is None else
                    self.reactor.monotonic() + max(0.0, float(timeout)))
        sensor_triggered = False
        motion_evidence = None
        interrupted = False
        cancel_requested = False
        next_query = self.reactor.monotonic() + 1.0

        def finish(raw_result):
            result = self._apply_endpoint_sensor_result(
                raw_result, sensor_triggered)
            if sensor_triggered and motion_evidence is not None:
                result = dict(result)
                result['motion_evidence'] = copy.deepcopy(motion_evidence)
            if interrupted:
                result = dict(result)
                result['foreground_handoff_requested'] = True
            if cancel_requested:
                result = dict(result)
                result['cancel_requested'] = True
                result['ok'] = False
                result['reason'] = 'cancelled'
            return result

        while True:
            if (self._u1_cancel_requested or
                    (callable(cancel_check) and cancel_check())):
                cancel_requested = True
            if (pause_for_critical and
                    self._critical_control_plane_blocked()):
                before = self.reactor.monotonic()
                if self._wait_background_control_plane(
                        cancel_check=lambda: bool(
                            self._u1_cancel_requested or
                            (callable(cancel_check) and cancel_check())),
                        poll_interval=poll_interval):
                    cancel_requested = True
                if deadline is not None:
                    deadline += max(
                        0.0, self.reactor.monotonic() - before)
            if cancel_requested:
                if device.connected:
                    try:
                        device.abort_operation()
                    except Exception:
                        try:
                            device.stop_all()
                        except Exception:
                            logging.exception(
                                'BMCU could not stop SEND_OUT after U1 UI cancel')

                completed_result = (
                    device.last_op
                    if device.last_op and device.last_op.get('op_id') == op_id
                    else None)
                return finish(completed_result or {
                    'op_id': int(op_id),
                    'state': protocol.OP_STATE_ABORTED,
                    'ok': False,
                    'reason': 'cancelled',
                    'measured_mm': 0.0,
                })
            completed_result = (
                device.last_op
                if device.last_op and device.last_op.get('op_id') == op_id
                else None)
            if not device.connected:
                raise BMCUError('DEVICE_OFFLINE during feed operation')

            now = self.reactor.monotonic()
            detected = False
            if endpoint is not None and sensor_role:
                if (endpoint.driver == 'snapmaker_u1' and
                        sensor_role in ('entry_sensor', 'motion_sensor')):

                    snapshot = endpoint.entry_sensor_snapshot()
                    detected = bool(
                        snapshot.get('available') and
                        snapshot.get('coherent') and
                        snapshot.get('physical_detected') is True)
                else:
                    detected = endpoint.sensor_detected(sensor_role) is True

            if detected and not sensor_triggered:

                sensor_triggered = True
                if (endpoint is not None and
                        endpoint.driver == 'snapmaker_u1'):
                    motion_evidence = dict(snapshot)
                try:
                    device.abort_operation()
                except Exception:

                    pass
                if device.last_op and device.last_op.get('op_id') == op_id:
                    completed_result = device.last_op

            if completed_result is not None:
                return finish(completed_result)
            if deadline is not None and now >= deadline:
                break

            if (not sensor_triggered and not interrupted and
                    callable(interrupt_check)):
                try:
                    should_interrupt = bool(interrupt_check())
                except Exception:
                    logging.exception(
                        'BMCU feed-operation interrupt predicate failed')
                    should_interrupt = False
                if should_interrupt:
                    interrupted = True
                    try:
                        device.abort_operation()
                    except Exception:
                        pass

            if now >= next_query:
                try:
                    result = device.query_operation(op_id, timeout=0.75)
                    if result and result.get('op_id') == op_id:
                        return finish(result)
                except Exception:
                    pass
                next_query = now + 1.0
            wake = now + poll_interval
            self.reactor.pause(wake if deadline is None else min(deadline, wake))
        try:
            device.abort_operation()
        except Exception:
            pass
        raise BMCUError('feed operation %d timed out' % op_id)

    def _wait_sensor_arrival(
            self, device, channel, endpoint, op_id, arrival_policy,
            maximum_mm, poll_interval=0.250, pause_for_critical=False,
            interrupt_check=None, cancel_check=None):

        timeout = max(0.0, float(arrival_policy['deadline']) -
                      self.reactor.monotonic())
        result = self._wait_feed_operation(
            device, op_id, timeout, endpoint, 'entry_sensor',
            poll_interval=poll_interval,
            pause_for_critical=pause_for_critical,
            interrupt_check=interrupt_check, cancel_check=cancel_check)
        result = dict(result or {})
        result['distance_limit_mm'] = float(maximum_mm)
        result['segment_count'] = 1
        return result

    def _record_error(self, code, device='', channel=-1, endpoint='', phase='', details='', evidence=None):
        self.last_error = {
            'code': code, 'device': device, 'channel': channel,
            'endpoint': endpoint, 'phase': phase, 'details': details,
            'evidence': evidence or {},
        }
        logging.error('BMCU %s', self.last_error)
        self._sync_status_cache_runtime()

    def _print_state(self):
        stats = self.printer.lookup_object('print_stats', None)
        if stats is not None:
            state = getattr(stats, 'state', None)
            if state is None and hasattr(stats, 'get_status'):
                try:
                    state = stats.get_status(self.reactor.monotonic()).get('state')
                except Exception:
                    state = None
            if state:
                return str(state).lower()
        idle = self.printer.lookup_object('idle_timeout', None)
        if idle is not None:
            try:
                return str(idle.get_status(self.reactor.monotonic()).get('state', '')).lower()
            except Exception:
                pass
        return ''

    def _recover_u1_print_transaction(self):

        if not self._u1_original:
            return True
        if (self.print_transaction_phase == 'applying' and
                self.print_map_active and self.print_tools):
            return self._reapply_u1_print_maps()
        if self.print_transaction_phase == 'active' and self.print_map_active:
            return self._reapply_u1_print_maps()
        if not self._restore_u1_original_snapshot():
            self._safe_pause()
            return False
        self._save_print_session()
        return True

    def _clear_print_session(self, reason='', preserve_loaded=False):
        had_session = bool(self.print_map_active or self.print_tools or
                           self.print_loaded_routes or
                           self.print_terminal_unload_pending or
                           self.refill.print_backups or self.print_job_id or
                           self._u1_original or self.print_transaction_phase or
                           self._u1_map_backup or self._u1_used_backup or
                           self._u1_end_unload_backup or
                           self.u1_cross_refill_pending)
        if self.u1_cross_refill_pending:
            self._record_error(
                'U1_CROSS_REFILL_RECOVERY_REQUIRED',
                details=('refusing to clear print session during cross-head refill '
                         'phase %s%s') % (
                             self.u1_cross_refill_pending.get('phase', 'unknown'),
                             (' after ' + reason) if reason else ''))
            return False

        if self._u1_background_jobs:
            try:
                self._cancel_u1_background_jobs(
                    reason or 'print session cleared', wait=True)
            except Exception as exc:
                self._record_error(
                    'SNAPMAKER_BACKGROUND_CLEANUP_FAILED',
                    phase='PRINT_SESSION_CLEAR', details=str(exc))
                return False
        restore_required = bool(
            self._u1_original or self._u1_map_backup or self._u1_used_backup or
            self._u1_end_unload_backup)
        if restore_required and not self._restore_u1_print_maps():
            if not self._u1_restore_pending_reported:
                self._u1_restore_pending_reported = True
                self._record_error(
                    'U1_MAP_RESTORE_PENDING',
                    details='print_task_config is unavailable or runtime map readback failed; retrying')
            return False
        self.print_map_active = False
        self.print_job_id = ''
        self.print_tools.clear()
        self.print_plan_tools.clear()
        self.print_plan_required.clear()
        self.print_plan_open = False
        self.print_plan_interrupted = False
        self.print_plan_schema = PRINT_PLAN_SCHEMA
        self.print_transaction_phase = ''
        self._u1_preextrude_primed_tools.clear()
        self._u1_prepared_heads.clear()
        self._reset_u1_lookahead(stop_jobs=False)
        if not preserve_loaded:
            self.print_loaded_routes.clear()
            self.print_route_journal_initialized = False
            self.print_terminal_unload_pending = False
        self.refill.clear_print()
        self._refill_runout_latched.clear()
        self._print_session_seen_active = False
        self.print_stock_reset_observed = False
        if had_session:
            self._save_print_session()
            logging.info('BMCU cleared per-print mapping%s',
                         (' after ' + reason) if reason else '')
        return had_session

    def _check_print_lifecycle(self):
        state = self._print_state()
        previous = self._last_print_state
        self._last_print_state = state
        if state in ('printing', 'paused', 'pause'):
            entering_print = state == 'printing' and previous != 'printing'
            self._print_session_seen_active = True
            if entering_print:
                self._scan_loaded_runouts()
            return
        if not self.print_map_active:
            return

        if state in ('complete', 'completed', 'cancelled', 'canceled', 'error', 'failed'):
            if self._u1_original:
                self.print_stock_reset_observed = True
            keep_loaded = bool(self.print_loaded_routes)
            if keep_loaded:
                self.print_terminal_unload_pending = True
            self._clear_print_session(
                'print state %s' % state, preserve_loaded=keep_loaded)
        elif (state in ('standby', 'ready', 'idle') and
              (self._print_session_seen_active or
               previous in ('printing', 'paused', 'pause'))):
            keep_loaded = bool(self.print_loaded_routes)
            if keep_loaded:
                self.print_terminal_unload_pending = True
            self._clear_print_session(
                'print returned to %s' % state,
                preserve_loaded=keep_loaded)

    def _virtual_sd_dispatch_active(self):
        virtual_sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if virtual_sdcard is None:
            return False
        checker = getattr(virtual_sdcard, 'is_cmd_from_sd', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        return bool(getattr(virtual_sdcard, 'cmd_from_sd', False))

    def _safe_pause(self, defer_to_virtual_sd=False):
        if not self.pause_on_error:
            return False

        if defer_to_virtual_sd and self._virtual_sd_dispatch_active():
            return False
        try:
            if self._print_state() in ('printing',):
                self.gcode.run_script_from_command('PAUSE')
                return True
        except Exception:
            logging.exception('BMCU could not pause after failure')
        return False

    @staticmethod
    def _command_pause_error(gcmd, message):

        message = str(message)
        try:
            return gcmd.error(
                message, action='pause', id=522, index=0, code=0,
                oneshot=0, level=2)
        except TypeError:
            return gcmd.error(message)

    @classmethod
    def _normalized_command_pause_error(cls, gcmd, error):

        if getattr(error, 'action', None) == 'pause':
            return error
        return cls._command_pause_error(gcmd, error)

    def _pause_for_refill(self, endpoint):
        if self._print_state() != 'printing':
            return False
        macro = str(endpoint.get('refill_pause_macro', '') or '').strip()
        if macro:
            endpoint.run_macro('refill_pause_macro', endpoint=endpoint.name)
        else:
            self.gcode.run_script_from_command('PAUSE')
        deadline = self.reactor.monotonic() + 8.0
        while self.reactor.monotonic() < deadline:
            if self._print_state() in ('paused', 'pause'):
                return True
            self.reactor.pause(self.reactor.monotonic() + 0.05)

        raise BMCUError('AUTO_REFILL_PAUSE_FAILED: printer did not confirm PAUSE')

    def _resume_after_refill(self, endpoint):
        macro = str(endpoint.get('refill_resume_macro', '') or '').strip()
        if macro:
            endpoint.run_macro('refill_resume_macro', endpoint=endpoint.name)
        else:
            self.gcode.run_script_from_command('RESUME')
        timeout_value = endpoint.get('refill_resume_timeout', 12.0)
        timeout = 12.0 if timeout_value is None else float(timeout_value)
        deadline = self.reactor.monotonic() + max(1.0, timeout)
        while self.reactor.monotonic() < deadline:
            state = self._print_state()
            if state == 'printing':
                return True
            if state in ('complete', 'completed', 'cancelled', 'canceled',
                         'error', 'failed', 'standby', 'ready', 'idle'):
                raise BMCUError(
                    'AUTO_REFILL_RESUME_FAILED: printer entered %s after RESUME' % state)
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        raise BMCUError('AUTO_REFILL_RESUME_FAILED: printer did not confirm RESUME')

    def _require_no_u1_cross_refill_pending(self):
        if self.u1_cross_refill_pending:
            pending = self.u1_cross_refill_pending
            raise BMCUError(
                'Snapmaker U1 cross-head refill recovery is pending (%s); '
                'run BMCU_REFILL_RESUME after verifying both BMCU routes, '
                'or complete exact recovery from the panel' %
                pending.get('phase', 'unknown'))

    def _stop_on_failure(self, device, endpoint, exc, phase, channel,
                         pause_print=True, restore_sensors=True,
                         wait_for_control_plane=False):
        preserve_hold = bool(getattr(exc, 'preserve_bmcu_hold', False))
        if not preserve_hold:
            if (wait_for_control_plane and
                    self._critical_control_plane_blocked()):
                try:
                    self._wait_background_control_plane(
                        poll_interval=self._u1_background_poll_interval)
                except Exception:
                    logging.exception(
                        'BMCU could not wait for printer motion before failure stop')
            try:
                device.stop_all()
            except Exception:
                logging.exception(
                    'BMCU could not stop %s after failure during %s',
                    device.name, phase)
        else:
            logging.error(
                'BMCU preserving firmware-local BEFORE_ON_USE pressure hold '
                'after %s on %s Channel %d',
                type(exc).__name__, device.name, channel + 1)
        if restore_sensors:
            try:
                endpoint.restore_managed_sensors()
            except Exception:
                pass
        self._record_error(type(exc).__name__, device.name, channel, endpoint.name,
                           phase, str(exc), {
                               'buffer_pct': device.status['buffer_pct'][channel],
                               'meters': device.status['meters'][channel],
                               'motor_pwm': device.status['motor_pwm'][channel],
                               'motion': device.status['motion'][channel],
                               'present': device.status['present'][channel],
                               'preserved_pressure_hold': preserve_hold,
                           })

        if pause_print:
            self._safe_pause(defer_to_virtual_sd=True)

    def _reset_u1_lookahead(self, stop_jobs=False, reason=''):
        self._u1_toolchange_plan = []
        self._u1_toolchange_cursor = -1
        self._u1_toolchange_plan_path = ''
        self._u1_tool_temperature_defaults = {}
        if stop_jobs:
            self._cancel_u1_background_jobs(reason or 'U1 look-ahead reset')

    def _virtual_sd_file_path(self):
        virtual_sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if virtual_sdcard is None:
            return ''
        current = getattr(virtual_sdcard, 'current_file', None)
        path = getattr(current, 'name', '') if current is not None else ''
        path = str(path or '').strip()
        if path and not os.path.isabs(path):
            base = str(getattr(virtual_sdcard, 'sdcard_dirname', '') or '')
            if base:
                path = os.path.join(base, path)
        return os.path.abspath(path) if path else ''

    @staticmethod
    def _u1_file_identity(info):
        mtime_ns = getattr(info, 'st_mtime_ns', None)
        if mtime_ns is None:
            mtime_ns = int(float(info.st_mtime) * 1000000000.0)
        return {
            'dev': int(info.st_dev),
            'ino': int(info.st_ino),
            'size': int(info.st_size),
            'mtime_ns': int(mtime_ns),
        }

    @staticmethod
    def _u1_identity_matches(actual, expected):
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        try:
            return all(int(actual[key]) == int(expected[key])
                       for key in ('dev', 'ino', 'size', 'mtime_ns'))
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def _read_u1_planner_result(self, path, request_id):
        flags = os.O_RDONLY
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                raise BMCUError('U1 planner returned an invalid result file')
            chunks = []
            remaining = int(info.st_size) + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if sum(len(chunk) for chunk in chunks) != int(info.st_size):
                raise BMCUError('U1 planner result changed while reading')
        finally:
            os.close(fd)
        try:
            payload = json.loads(b''.join(chunks).decode('utf-8'))
        except Exception:
            raise BMCUError('U1 planner returned malformed JSON')
        if (not isinstance(payload, dict) or
                str(payload.get('request_id', '')) != request_id or
                int(payload.get('schema', -1)) != U1_SOURCE_PLAN_SCHEMA):
            raise BMCUError('U1 planner returned a mismatched result')
        if not payload.get('ok', False):
            raise BMCUError(
                'U1 source planning failed: %s' %
                str(payload.get('error', 'unknown planner error')))
        return payload

    def _validate_u1_external_plan(self, result, path, identity):
        if not isinstance(result, dict):
            raise BMCUError('U1 planner result is incomplete')
        if (int(result.get('schema', -1)) != U1_SOURCE_PLAN_SCHEMA or
                os.path.abspath(str(result.get('path', '') or '')) != path or
                not self._u1_identity_matches(
                    result.get('identity'), identity)):
            raise BMCUError('U1 planner result does not match the active G-code')
        raw_plan = result.get('plan')
        raw_defaults = result.get('defaults')
        if not isinstance(raw_plan, list) or not isinstance(raw_defaults, dict):
            raise BMCUError('U1 planner result has invalid plan data')
        plan = []
        previous_offset = -1
        for raw_item in raw_plan:
            if not isinstance(raw_item, dict):
                raise BMCUError('U1 planner result contains an invalid entry')
            try:
                offset = int(raw_item['offset'])
                tool = int(raw_item['tool'])
            except (KeyError, TypeError, ValueError, OverflowError):
                raise BMCUError('U1 planner entry has no valid source identity')
            if (offset < 0 or offset < previous_offset or
                    not 0 <= tool < U1_LOGICAL_TOOL_LIMIT):
                raise BMCUError('U1 planner entry is outside the valid range')
            item = {'offset': offset, 'tool': tool}
            for key in ('temperature', 'temperature_min', 'temperature_max'):
                if raw_item.get(key) is not None:
                    try:
                        value = float(raw_item[key])
                    except (TypeError, ValueError, OverflowError):
                        raise BMCUError('U1 planner entry has an invalid temperature')
                    if not 0.0 <= value <= 350.0:
                        raise BMCUError('U1 planner temperature is outside the valid range')
                    item[key] = value
            source = str(raw_item.get('temperature_source', '') or '')
            if source:
                item['temperature_source'] = source
            plan.append(item)
            previous_offset = offset
        defaults = {}
        for raw_tool, raw_profile in raw_defaults.items():
            try:
                tool = int(raw_tool)
            except (TypeError, ValueError, OverflowError):
                raise BMCUError('U1 planner defaults contain an invalid tool')
            if not 0 <= tool < U1_LOGICAL_TOOL_LIMIT or not isinstance(raw_profile, dict):
                raise BMCUError('U1 planner defaults are outside the valid range')
            profile = {}
            for key in ('temperature', 'initial_temperature',
                        'temperature_min', 'temperature_max'):
                if raw_profile.get(key) is None:
                    continue
                try:
                    value = float(raw_profile[key])
                except (TypeError, ValueError, OverflowError):
                    raise BMCUError('U1 planner default temperature is invalid')
                if not 0.0 <= value <= 350.0:
                    raise BMCUError('U1 planner default temperature is outside the valid range')
                profile[key] = value
            source = str(raw_profile.get('source', '') or '')
            if source:
                profile['source'] = source
            defaults[tool] = profile
        return plan, defaults

    def _load_u1_toolchange_plan(self):

        self._reset_u1_lookahead(stop_jobs=False)
        if not self.printer_analysis.get('features', {}).get('snapmaker_u1'):
            return False
        path = self._virtual_sd_file_path()
        if not path:
            raise BMCUError('U1 source planning has no active virtual-SD file')
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise BMCUError('U1 source planning cannot stat G-code: %s' % exc)
        if os.path.islink(path) or not stat.S_ISREG(info.st_mode):
            raise BMCUError('U1 source planning requires a regular G-code file')
        identity = self._u1_file_identity(info)
        request_id = os.urandom(16).hex()
        result_path = os.path.join(
            self._u1_planner_result_dir, request_id + '.json')
        request = json.dumps({
            'id': request_id,
            'path': path,
            'identity': identity,
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self._u1_planner_requests += 1
        started = self.reactor.monotonic()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                client.settimeout(0.20)
                client.sendto(request, self._u1_planner_socket)
            finally:
                client.close()
            deadline = started + float(self.u1_planner_timeout)
            payload = None
            while True:
                try:
                    payload = self._read_u1_planner_result(
                        result_path, request_id)
                    break
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                    now = self.reactor.monotonic()
                    if now >= deadline:
                        raise BMCUError(
                            'U1 source planner did not answer within %.1f s' %
                            self.u1_planner_timeout)
                    self.reactor.pause(min(
                        deadline, now + self.u1_planner_poll_interval))
            result = payload.get('result')
            plan, defaults = self._validate_u1_external_plan(
                result, path, identity)
            self._u1_toolchange_plan = plan
            self._u1_toolchange_cursor = -1
            self._u1_toolchange_plan_path = path
            self._u1_tool_temperature_defaults = defaults
            self._u1_planner_last_scan_ms = float(
                result.get('elapsed_ms', 0.0) or 0.0)
            elapsed = max(0.0, (self.reactor.monotonic() - started) * 1000.0)
            self._u1_planner_last_wait_ms = elapsed
            self._u1_planner_max_wait_ms = max(
                self._u1_planner_max_wait_ms, elapsed)
            logging.info(
                'BMCU Snapmaker source plan loaded %d tool changes outside '
                'Klipper in %.3f ms (Klipper wait %.3f ms)',
                len(plan), self._u1_planner_last_scan_ms, elapsed)
            return True
        except Exception:
            self._u1_planner_failures += 1
            raise
        finally:
            try:
                os.unlink(result_path)
            except OSError:
                pass

    def _scan_u1_toolchange_plan(self):

        return self._load_u1_toolchange_plan()

    def _u1_temperature_profile(self, plan_index=None, tool=None):
        item = None
        plan = getattr(self, '_u1_toolchange_plan', [])
        if plan_index is not None:
            try:
                index = int(plan_index)
            except (TypeError, ValueError, OverflowError):
                index = -1
            if 0 <= index < len(plan):
                candidate = plan[index]
                if tool is None or int(candidate.get('tool', -1)) == int(tool):
                    item = candidate
        if item is not None and item.get('temperature') is not None:
            resolved_tool = int(
                item.get('tool', tool if tool is not None else -1))
            defaults = getattr(
                self, '_u1_tool_temperature_defaults', {}).get(
                resolved_tool, {})
            temperature = float(item['temperature'])
            minimum = (defaults.get('temperature_min')
                       if isinstance(defaults, dict) else None)
            maximum = (defaults.get('temperature_max')
                       if isinstance(defaults, dict) else None)
            source = str(
                item.get('temperature_source', 'gcode_toolchange') or
                'gcode_toolchange')

            result = {
                'temperature': temperature,
                'source': source,
                'tool': resolved_tool,
            }
            for key in ('temperature_min', 'temperature_max'):
                value = item.get(key)
                if value is None and isinstance(defaults, dict):
                    value = defaults.get(key)
                if value is not None:
                    result[key] = float(value)
            return result
        if tool is not None:
            profile = getattr(
                self, '_u1_tool_temperature_defaults', {}).get(int(tool))
            if isinstance(profile, dict):
                result = copy.deepcopy(profile)
                result['tool'] = int(tool)
                return result
        return None

    def _u1_active_temperature_profile(self):
        active_tool = getattr(self, 'active_tool', -1)
        tool = int(active_tool) if active_tool is not None else -1
        cursor = getattr(self, '_u1_toolchange_cursor', None)
        if cursor is not None:
            profile = self._u1_temperature_profile(cursor, tool=tool)
            if profile is not None:
                return profile
        return self._u1_temperature_profile(tool=tool)

    def _u1_head_loaded_evidence(self, endpoint):

        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False, [], {}
        evidence = []
        occupied = self._loaded_devices_for_endpoint(endpoint.name)
        if occupied:
            evidence.append('bmcu_loaded_route:' + ','.join(
                '%s/ch%d' % (device.name, int(channel) + 1)
                for device, channel in occupied))
        path = {}
        try:
            path = endpoint.native_path_status()
        except Exception as exc:
            logging.warning(
                'BMCU could not inspect initial U1 Head %d filament state: %s',
                int(endpoint.get('head_index', -1)) + 1, exc)
            path = {'known': False, 'channel_state': 'unknown',
                    'entry_detected': None}
        entry = path.get('entry_detected')
        state = str(path.get('channel_state', '') or '').strip().lower()
        auto_disabled = path.get('auto_disabled')

        if (entry is True and
                (occupied or state == 'load_finish' or auto_disabled is False)):
            evidence.append('head_entry_sensor')
        elif entry is True:
            path['entry_ignored_as_stale_possible'] = True
        if state == 'load_finish' and entry is not False:
            evidence.append('stock_load_finish')
        return bool(evidence), evidence, path

    def _initialize_u1_prepared_heads_from_physical_state(
            self, source='print_begin', persist=False):

        changed = False
        snapshots = []
        for endpoint in sorted(
                self.endpoints.values(), key=lambda item: str(item.name)):
            if endpoint.driver != 'snapmaker_u1':
                continue
            try:
                head = int(endpoint.get('head_index', -1))
            except (TypeError, ValueError, OverflowError):
                continue
            if head not in range(4):
                continue
            loaded, evidence, path = self._u1_head_loaded_evidence(endpoint)
            snapshots.append({
                'head': head, 'loaded': bool(loaded),
                'evidence': list(evidence),
                'channel_state': path.get('channel_state', 'unknown'),
                'entry_detected': path.get('entry_detected'),
                'auto_disabled': path.get('auto_disabled'),
                'entry_ignored_as_stale_possible': bool(
                    path.get('entry_ignored_as_stale_possible', False)),
            })
            if loaded and head not in self._u1_prepared_heads:
                self._u1_prepared_heads.add(head)
                changed = True
                logging.info(
                    'BMCU Snapmaker physical Head %d was already loaded at %s '
                    '(%s); elevated first-load cleaning is skipped for this print',
                    head + 1, source, ', '.join(evidence))
        logging.info(
            'BMCU U1 initial loaded-Head snapshot at %s: %s', source,
            json.dumps(snapshots, sort_keys=True))
        if changed and persist:
            self._save_print_session()
        return snapshots

    def _u1_load_temperature_profile(self, endpoint, profile=None, material=''):

        result = copy.deepcopy(profile) if isinstance(profile, dict) else {}
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return result or None
        head = int(endpoint.get('head_index', -1))
        print_active = self._print_state() in ('printing', 'paused', 'pause')
        first_load = bool(
            print_active and head in range(4) and
            head not in self._u1_prepared_heads)
        result['physical_head'] = head
        result['first_physical_head_load'] = first_load
        result['print_active'] = print_active
        tip_profile = self._u1_tip_profile_for_material(material)
        mode = str(tip_profile.get('temperature_mode', 'project') or
                   'project').strip().lower()
        result['load_temperature_mode'] = mode
        if mode == 'custom' and not first_load:
            result['load_temperature'] = float(tip_profile['temperature'])
            result['load_temperature_source'] = 'custom_tip_profile'
        return result

    def _mark_u1_head_prepared(self, head, source='load'):
        try:
            head = int(head)
        except (TypeError, ValueError, OverflowError):
            return False
        if head not in range(4):
            return False
        if self._print_state() not in ('printing', 'paused', 'pause'):
            return False
        if head in self._u1_prepared_heads:
            return False
        self._u1_prepared_heads.add(head)
        self._save_print_session()
        logging.info(
            'BMCU Snapmaker physical Head %d is prepared for this print (%s); '
            'later loads now follow the selected material temperature mode',
            head + 1, source)
        return True

    def _run_u1_stock_auto_feed(self, endpoint, head, source):

        before = endpoint.native_path_status()
        before_loaded = bool(
            before.get('known') and
            str(before.get('channel_state', '')).lower() == 'load_finish')
        with self.printer_critical_section(
                'snapmaker_u1_stock_auto_feed'):
            self.gcode.run_script_from_command(
                'SM_PRINT_AUTO_FEED EXTRUDER=%d' % int(head))
            after = endpoint.native_path_status()
        after_loaded = bool(
            after.get('known') and
            str(after.get('channel_state', '')).lower() == 'load_finish')
        if not after_loaded:
            raise BMCUError(
                'stock Snapmaker auto-feed did not confirm LOADED for head %d '
                '(before=%s after=%s)' %
                (int(head), before.get('channel_state', 'unknown'),
                 after.get('channel_state', 'unknown')))
        actual_load = not before_loaded
        self._mark_u1_head_prepared(
            head, source=(source if actual_load else
                          '%s (already loaded before stock auto-feed)' % source))
        logging.info(
            'BMCU stock auto-feed Head %d: before=%s after=%s actual_load=%d',
            int(head) + 1, before.get('channel_state', 'unknown'),
            after.get('channel_state', 'unknown'), 1 if actual_load else 0)
        return actual_load

    def _u1_current_sd_position(self):
        virtual_sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if virtual_sdcard is None:
            return 0, 0, False
        current = int(getattr(virtual_sdcard, 'file_position', 0) or 0)
        next_position = int(
            getattr(virtual_sdcard, 'next_file_position', current) or current)
        from_sd = bool(getattr(virtual_sdcard, 'cmd_from_sd', False))
        return current, max(current, next_position), from_sd

    def _locate_u1_toolchange(self, tool):

        tool = int(tool)
        if not self._u1_toolchange_plan:
            return -1
        current, next_position, from_sd = self._u1_current_sd_position()
        candidates = [
            index for index in range(self._u1_toolchange_cursor + 1,
                                     len(self._u1_toolchange_plan))
            if int(self._u1_toolchange_plan[index].get('tool', -1)) == tool]
        if not candidates:
            logging.warning(
                'BMCU U1 look-ahead has no remaining logical T%d entry', tool)
            return -1
        if from_sd:

            exact = [index for index in candidates
                     if current <= int(self._u1_toolchange_plan[index].get(
                         'offset', 0)) < max(next_position + 1, current + 1)]
            if exact:
                return exact[0]
            future = [index for index in candidates
                      if int(self._u1_toolchange_plan[index].get(
                          'offset', 0)) >= current]
            if future:
                return future[0]
            logging.warning(
                'BMCU U1 look-ahead could not align T%d at SD byte %d',
                tool, current)
            return -1

        return candidates[0]

    def _commit_u1_toolchange(self, index):
        if index is None or int(index) < 0:
            return False
        index = int(index)
        if index >= len(self._u1_toolchange_plan):
            return False
        if index < self._u1_toolchange_cursor:
            return False
        self._u1_toolchange_cursor = index
        return True

    def _u1_logical_route(self, tool):
        try:
            tool = int(tool)
        except (TypeError, ValueError):
            return None
        mapping = self.print_tools.get(str(tool)) if self.print_map_active else None
        if isinstance(mapping, dict):
            if mapping.get('native'):
                head = int(mapping.get('head', tool))
                endpoint = self._u1_endpoint_for_head(head)
                return ({'tool': tool, 'kind': 'native', 'head': head,
                         'endpoint': endpoint} if endpoint is not None else None)
            device = self._mapping_device(mapping)
            if device is None:
                return None
            channel = int(mapping.get('channel', -1))
            if channel < 0 or channel > 3:
                return None
            endpoint = self._endpoint_for_channel(device, channel)
            if endpoint is None:
                return None
            return {'tool': tool, 'kind': 'bmcu', 'device': device,
                    'channel': channel, 'endpoint': endpoint}
        if 0 <= tool < U1_NATIVE_TOOL_COUNT:
            endpoint = self._u1_endpoint_for_head(tool)
            return ({'tool': tool, 'kind': 'native', 'head': tool,
                     'endpoint': endpoint} if endpoint is not None else None)
        return None

    def _u1_source_identity(self, route):
        if not isinstance(route, dict):
            return None
        kind = str(route.get('kind', '') or '').lower()
        if kind == 'native':
            return ('native', int(route.get('head', -1)))
        if kind == 'bmcu':
            device = route.get('device')
            if device is None:
                return None
            uid = self._device_uid(device) or str(device.name)
            return ('bmcu', str(uid).upper(), int(route.get('channel', -1)))
        return None

    def _u1_same_physical_source(self, first, second):
        identity = self._u1_source_identity(first)
        return identity is not None and identity == self._u1_source_identity(second)

    def _next_u1_route_for_endpoint(self, endpoint_name, after_index):
        if after_index is None or int(after_index) < 0:
            return None
        for index in range(int(after_index) + 1,
                           len(self._u1_toolchange_plan)):
            item = self._u1_toolchange_plan[index]
            tool = int(item.get('tool', -1))
            route = self._u1_logical_route(tool)
            if (route is not None and route.get('endpoint') is not None and
                    route['endpoint'].name == endpoint_name):
                result = dict(route)
                result['plan_index'] = index
                return result
        return None

    def _first_u1_route_for_endpoint(self, endpoint_name):
        current, _next_position, from_sd = self._u1_current_sd_position()
        for index, item in enumerate(self._u1_toolchange_plan):
            if from_sd and int(item.get('offset', 0)) < current:
                continue
            route = self._u1_logical_route(int(item.get('tool', -1)))
            if (route is not None and route.get('endpoint') is not None and
                    route['endpoint'].name == endpoint_name):
                result = dict(route)
                result['plan_index'] = index
                return result
        return None

    def _u1_background_controller_conflict(self, device_names, after_index,
                                           before_index, path_groups=()):

        names = set(str(name) for name in device_names if name)
        groups = set(path_groups)
        if not names and not groups:
            return None
        start = max(0, int(after_index) + 1)
        stop = min(len(self._u1_toolchange_plan), int(before_index))
        for index in range(start, stop):
            tool = int(self._u1_toolchange_plan[index].get('tool', -1))
            route = self._u1_logical_route(tool)
            if route is None:
                continue
            device = route.get('device')
            endpoint = route.get('endpoint')
            if ((device is not None and device.name in names) or
                    (endpoint is not None and
                     endpoint.shared_path_group() in groups)):
                return {'index': index, 'tool': tool,
                        'device': device.name if device is not None else 'native',
                        'shared_path_group': (
                            endpoint.shared_path_group() if endpoint is not None
                            else '')}
        return None

    def _set_u1_background_phase(self, job, phase):
        job['phase'] = str(phase)
        devices = [job.get('source_device'), job.get('target_device')]
        for device in devices:
            if device is None:
                continue
            operation = self.active_operations.get(device.name)
            if operation is not None:
                operation['phase'] = str(phase)
                operation['background'] = True
                operation['source_tool'] = int(job.get('source_tool', -1))
                operation['source_device'] = str(
                    getattr(job.get('source_device'), 'name', '') or '')
                operation['source_channel'] = int(
                    job.get('source_channel', -1))
                operation['target_tool'] = int(job.get('target_tool', -1))
                operation['target_kind'] = str(job.get('target_kind', '') or '')
                operation['target_device'] = str(
                    getattr(job.get('target_device'), 'name', '') or '')
                operation['target_channel'] = int(
                    job.get('target_channel', -1)
                    if job.get('target_channel') is not None else -1)

    def _unlock_u1_background_job(self, job):
        if not job.get('locked'):
            return
        source_device = job.get('source_device')
        target_device = job.get('target_device')
        endpoint = job.get('endpoint')
        if job.get('lock_kind') == 'refill':
            self._unlock_refill(source_device, target_device, endpoint)
        elif source_device is not None and endpoint is not None:
            self._unlock(source_device, endpoint)
        job['locked'] = False

    def _clear_ready_u1_background_job(self, endpoint_name, job, reason=''):
        if self._u1_background_jobs.get(endpoint_name) is not job:
            return
        if job.get('foreground_consuming'):
            self._u1_background_jobs.pop(endpoint_name, None)
            return
        target_device = job.get('target_device')
        target_channel = job.get('target_channel')
        endpoint = job.get('endpoint')
        if (target_device is not None and target_channel is not None and
                endpoint is not None):
            route_key = self._route_key(target_device, int(target_channel))
            staged = self.prestaged.get(route_key)
            if staged is not None:
                self._lock(
                    target_device, endpoint,
                    'CLEAR CANCELLED SNAPMAKER PRESTAGE',
                    channel=int(target_channel))
                try:
                    self._clear_prestage_locked(
                        target_device, endpoint, staged,
                        target_selected=bool(
                            job.get('foreground_head_prefetched')))
                finally:
                    self._unlock(target_device, endpoint)
        if (endpoint is not None and
                job.get('foreground_load_context_prepared')):
            try:
                endpoint.finish_load_temperature(success=False)
            except Exception:
                logging.exception(
                    'BMCU could not restore cancelled U1 prefetch temperature on %s',
                    endpoint.name)
            job['foreground_load_context_prepared'] = False
        self._unlock_u1_background_job(job)
        self._u1_background_jobs.pop(endpoint_name, None)
        if endpoint is not None:
            self._release_u1_persistent_hold_if_safe(
                endpoint, reason or 'background preparation cleared')

    def _cancel_u1_background_jobs(self, reason='', wait=True):

        reason = str(reason or 'cancelled')
        jobs = list(self._u1_background_jobs.items())
        for _endpoint_name, job in jobs:
            job['cancelled'] = True
            job['cancel_reason'] = reason
        if self._critical_control_plane_blocked():
            self._wait_background_control_plane()
        stopped = set()
        for endpoint_name, job in jobs:
            state = str(job.get('state', '') or '')
            worker_pending = bool(
                job.get('worker_scheduled') and
                not job.get('worker_finished'))
            if (not worker_pending and
                    state not in ('starting', 'running')):
                continue
            for device in (job.get('source_device'), job.get('target_device')):
                if device is None or device.name in stopped:
                    continue
                stopped.add(device.name)
                if self._critical_control_plane_blocked():
                    self._wait_background_control_plane()
                try:
                    device.stop_all()
                except Exception:
                    logging.exception(
                        'BMCU could not stop %s while cancelling Snapmaker background job',
                        device.name)
        if not wait:
            return
        deadline = self.reactor.monotonic() + self._u1_background_wait_timeout(
            job for _name, job in jobs)
        while any(
                self._u1_background_jobs.get(endpoint_name) is job and
                (job.get('state') in ('starting', 'running') or
                 (job.get('worker_scheduled') and
                  not job.get('worker_finished')))
                for endpoint_name, job in jobs):
            if self.reactor.monotonic() >= deadline:
                raise BMCUError(
                    'timed out while cancelling Snapmaker background motion')
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        failures = []
        for endpoint_name, job in jobs:
            if (self._u1_background_jobs.get(endpoint_name) is job and
                    job.get('cancelled')):
                try:
                    self._clear_ready_u1_background_job(
                        endpoint_name, job, reason=reason)
                except Exception as exc:
                    failures.append('%s: %s' % (endpoint_name, exc))
        if failures:
            raise BMCUError('; '.join(failures))

    def _u1_background_wait_timeout(self, jobs):
        timeout = max(self.unload_timeout + self.contact_timeout + 10.0, 30.0)
        for job in jobs:
            endpoint = job.get('endpoint')
            if endpoint is None:
                continue
            feed_timeout = float(endpoint.get(
                'contact_timeout', self.contact_timeout) or self.contact_timeout)
            device = job.get('target_device')
            if device is not None:
                feed_timeout = self._effective_feed_timeout(
                    device, endpoint.get('max_route_mm', self.max_route_mm),
                    feed_timeout)
            ticket = job.get('parked_tip_tail') or {}
            tail_time = float(ticket.get('post_marker_duration_s', 0.0) or 0.0)
            park_time = float(endpoint.get('u1_prestage_park_timeout', 20.0) or 20.0)
            timeout = max(timeout, self.unload_timeout + feed_timeout +
                          tail_time + park_time + 10.0)
        return timeout

    def _drain_u1_background_jobs(self, clear_ready=True):
        deadline = self.reactor.monotonic() + self._u1_background_wait_timeout(
            self._u1_background_jobs.values())
        while any(
                job.get('state') in ('starting', 'running') or
                (job.get('worker_scheduled') and
                 not job.get('worker_finished'))
                for job in self._u1_background_jobs.values()):
            if self.reactor.monotonic() >= deadline:
                raise BMCUError(
                    'timed out waiting for Snapmaker background filament preparation')
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        failures = [job for job in self._u1_background_jobs.values()
                    if job.get('state') == 'error' and not job.get('cancelled')]
        if failures:
            raise BMCUError('; '.join(
                str(job.get('error', 'Snapmaker background operation failed'))
                for job in failures))
        if clear_ready:
            self._cancel_u1_background_jobs(
                'print ended before the prepared source was consumed', wait=True)

    def _prestage_u1_to_entry_locked(self, device, channel, endpoint, tool,
                                     cancel_check=None,
                                     allowed_detached_source=None,
                                     background=False,
                                     handoff_check=None,
                                     operation_started_callback=None,
                                     target_selected=False):
        if callable(cancel_check) and cancel_check():
            raise BMCUError('background prestage was cancelled before motion')
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            raise BMCUError('background prestage was cancelled before motion')
        route_key = self._route_key(device, channel)
        self._check_automatic_ready(device, channel)
        self._arm_u1_persistent_hold(
            endpoint, device, channel, 'background prestage T%d' % tool)
        self._validate_endpoint_for_operation(
            endpoint, require_u1_ownership=True)
        if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
            raise BMCUError(
                '%s Channel %d is not EMPTY before background prestage' %
                (device.name, channel + 1))
        occupied = self._loaded_devices_for_endpoint(
            endpoint.name, excluded_route=route_key)
        if allowed_detached_source is not None:
            allowed_device, allowed_channel = allowed_detached_source
            allowed_channel = int(allowed_channel)
            occupied = [
                (item_device, item_channel)
                for item_device, item_channel in occupied
                if not (item_device.name == allowed_device.name and
                        int(item_channel) == allowed_channel and
                        self._u1_tail_detached_matches(
                            endpoint, item_device, int(item_channel)) and
                        self._route_states_from_status(
                            item_device.status)[int(item_channel)] ==
                        protocol.ROUTE_EMPTY)
            ]
        if occupied:
            raise BMCUError(
                'endpoint %s is still occupied before background prestage' %
                endpoint.name)
        if self._prestaged_devices_for_endpoint(
                endpoint.name, excluded_route=route_key):
            raise BMCUError(
                'endpoint %s already contains another prestaged source' %
                endpoint.name)
        endpoint.suspend_managed_sensors()
        if target_selected:
            prepare_selected = getattr(
                endpoint, 'prepare_selected_prestage', None)
            if not callable(prepare_selected):
                raise BMCUError(
                    'endpoint %s cannot start selected-Head background arrival' %
                    endpoint.name)
            endpoint.verify_selected()
            prepare_selected()
        else:
            park_timeout = float(endpoint.get(
                'u1_prestage_park_timeout', 20.0) or 20.0)
            endpoint.wait_prestage_safe(park_timeout)
            endpoint.prepare_prestage()
        if self._u1_has_authoritative_entry_sensor(endpoint):

            endpoint.require_entry_sensor_snapshot(timeout=1.25)
        elif endpoint.sensor_detected('entry_sensor') is True:
            raise BMCUError(
                'endpoint %s entry sensor is active before T%d prestage' %
                (endpoint.name, tool))
        maximum_mm = float(endpoint.get('max_route_mm', self.max_route_mm) or
                           self.max_route_mm)
        normal_contact_pct = self._device_loading_handoff_pct(
            device, endpoint)
        if (self._u1_has_authoritative_entry_sensor(endpoint) and
                not target_selected):
            parked_limit = int(endpoint.get(
                'prestage_buffer_limit_pct', 63.0) or 63.0)
            contact_pct = max(55, min(normal_contact_pct, parked_limit, 63))
        else:
            contact_pct = normal_contact_pct
        timeout_s = float(endpoint.get(
            'contact_timeout', self.contact_timeout) or self.contact_timeout)
        timeout_s = self._effective_feed_timeout(device, maximum_mm, timeout_s)
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            raise BMCUError('background prestage was cancelled before motion')
        op_id, arrival_policy = self._start_endpoint_arrival_operation(
            device, channel, endpoint, maximum_mm, contact_pct, timeout_s,
            parked_precharge=bool(
                self._u1_has_authoritative_entry_sensor(endpoint) and
                not target_selected))
        if callable(operation_started_callback):
            try:
                operation_started_callback(
                    int(op_id), copy.deepcopy(arrival_policy))
            except Exception:

                logging.exception(
                    'BMCU could not publish background prestage operation start; '
                    'continuing with the target Head parked')
        if arrival_policy.get('sensor_authoritative'):
            result = self._wait_sensor_arrival(
                device, channel, endpoint, op_id, arrival_policy,
                maximum_mm,
                poll_interval=(self._u1_background_poll_interval
                               if background else 0.250),
                pause_for_critical=bool(background),
                interrupt_check=(handoff_check if background else None),
                cancel_check=cancel_check)
        else:
            result = self._wait_feed_operation(
                device, op_id, timeout_s + 2.0, endpoint, 'entry_sensor',
                poll_interval=(self._u1_background_poll_interval
                               if background else None),
                pause_for_critical=bool(background),
                interrupt_check=(handoff_check if background else None),
                cancel_check=cancel_check)
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            try:
                device.stop_all()
            except Exception:
                pass
            raise BMCUError('background prestage was cancelled before commit')
        result = self._resolve_endpoint_arrival_result(
            device, channel, endpoint, result, arrival_policy,
            timeout_s, allow_partial=bool(background))
        if callable(cancel_check) and cancel_check():
            try:
                device.stop_all()
            except Exception:
                pass
            raise BMCUError('background prestage was cancelled before commit')

        entry_confirmed = (
            bool(result.get('ok') and result.get('sensor_triggered'))
            if self._u1_has_authoritative_entry_sensor(endpoint) else
            endpoint.sensor_detected('entry_sensor') is True)
        interrupted = bool(result.get('foreground_handoff_requested'))
        try:
            measured_mm = float(result.get('measured_mm', 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            measured_mm = -1.0
        try:
            buffer_pct = int(device.status['buffer_pct'][channel])
        except (TypeError, ValueError, OverflowError, KeyError, IndexError):
            buffer_pct = -1
        present = bool(
            isinstance(device.status.get('present'), (list, tuple)) and
            channel < len(device.status.get('present')) and
            device.status.get('present')[channel])
        result_reason = str(result.get('reason', '') or '')
        sensor_authoritative = bool(result.get('sensor_authoritative'))
        normal_partial_reason = (
            result_reason in (
                'aborted', 'sensor_precharge_neutral')
            if sensor_authoritative else
            result_reason in (
                'target', 'contact', 'buffer_limit', 'aborted', 'none'))
        safe_partial = bool(
            background and not entry_confirmed and
            (result.get('ok') or interrupted) and normal_partial_reason and
            math.isfinite(measured_mm) and measured_mm >= 0.0 and
            measured_mm <= maximum_mm + 5.0 and present and
            2 < buffer_pct <= 100)

        if not result.get('ok') and not safe_partial:
            raise BMCUError(
                'BACKGROUND_PRESTAGE_FAILED: %s after %.1f mm' %
                (result.get('reason'), max(0.0, measured_mm)))
        if not entry_confirmed and not safe_partial:
            raise BMCUError(
                'BACKGROUND_PRESTAGE_FAILED: Snapmaker entry sensor did not confirm T%d' %
                tool)

        staged = {
            'device': device.name, 'tool': int(tool), 'channel': int(channel),
            'endpoint': endpoint.name,
            'distance_mm': max(0.0, measured_mm),
            'result': dict(result), 'at_entry': bool(entry_confirmed),
            'partial': bool(not entry_confirmed),
            'partial_commit': bool(not entry_confirmed),
            'background': True,
            'target_selected': bool(target_selected),
            'buffer_pct': int(buffer_pct),
            'reason': ('entry_confirmed' if entry_confirmed else
                       'foreground_handoff' if interrupted else
                       'sensor_precharge_neutral' if
                       result.get('sensor_precharge_neutral') else
                       'controller_contact_before_entry'),
        }
        self.prestaged[route_key] = staged
        endpoint.activate_runtime_sensor_takeover(force=True)
        self.last_diagnostic = {
            'operation': ('snapmaker_background_prestage' if entry_confirmed
                          else 'snapmaker_background_partial_prestage'),
            'tool': int(tool), 'device': device.name,
            'channel': int(channel), 'endpoint': endpoint.name,
            'arrival': dict(result), 'buffer_pct': int(buffer_pct),
            'distance_mm': max(0.0, measured_mm),
        }
        if not entry_confirmed:
            logging.info(
                'BMCU Snapmaker background left T%d partially prestaged on %s '
                'after %.1f mm at %d%% buffer; selected Head will finish the '
                'sensor search',
                int(tool), endpoint.name, max(0.0, measured_mm),
                int(buffer_pct))
        return staged

    def _schedule_u1_background_worker(self, endpoint_name):

        endpoint_name = str(endpoint_name or '')
        job = self._u1_background_jobs.get(endpoint_name)
        if job is None:
            raise BMCUError(
                'Snapmaker background job disappeared before scheduling')
        if job.get('worker_scheduled'):
            raise BMCUError(
                'Snapmaker background worker is already scheduled on %s' %
                endpoint_name)
        job['worker_scheduled'] = True
        self._set_u1_background_phase(
            job, 'BACKGROUND_SOURCE_LANE' if job.get('source_lane_ready')
            else 'WAIT_FOREGROUND_HEAD')
        try:
            self.reactor.register_callback(
                lambda eventtime, name=endpoint_name:
                    self._run_u1_background_callback(eventtime, name),
                waketime=(self.reactor.monotonic() +
                          self.manager_work_yield_interval))
        except Exception:
            job['worker_scheduled'] = False
            raise
        if job.get('source_lane_ready'):
            logging.info(
                'BMCU scheduled parked-head background T%d to T%d on %s; '
                'old-head tail/pullback lane is released immediately after '
                'confirmed PARKED state',
                int(job.get('source_tool', -1)),
                int(job.get('target_tool', -1)), endpoint_name)
        else:
            logging.info(
                'BMCU scheduled parked-head background T%d to T%d on %s; '
                'waiting for foreground Head activation',
                int(job.get('source_tool', -1)),
                int(job.get('target_tool', -1)), endpoint_name)
        return True

    def _release_u1_background_worker(self, endpoint_name):
        endpoint_name = str(endpoint_name or '')
        job = self._u1_background_jobs.get(endpoint_name)
        if job is None:
            return False
        if job.get('foreground_released'):
            return True
        job['foreground_released'] = True
        logging.info(
            'BMCU recorded foreground Head activation for background T%d to '
            'T%d on %s; the independent old-head source lane was not delayed',
            int(job.get('source_tool', -1)),
            int(job.get('target_tool', -1)), endpoint_name)
        return True

    def _run_u1_background_callback(self, eventtime, endpoint_name):
        job = self._u1_background_jobs.get(endpoint_name)
        if job is None:
            return
        job['worker_started'] = True

        if not job.get('source_lane_ready'):
            while (not job.get('foreground_released') and
                   not self._klippy_disconnecting and
                   not job.get('cancelled')):
                self.reactor.pause(
                    self.reactor.monotonic() +
                    self._u1_background_poll_interval)
            while (self._critical_control_plane_blocked() and
                   not self._klippy_disconnecting and
                   not job.get('cancelled')):
                self.reactor.pause(
                    self.reactor.monotonic() +
                    self._u1_background_poll_interval)
        if self._klippy_disconnecting and not job.get('cancelled'):
            job['cancelled'] = True
            job['cancel_reason'] = 'Klipper disconnect'
        try:
            self._u1_background_worker(eventtime, endpoint_name)
        finally:
            if self._u1_background_jobs.get(endpoint_name) is job:
                job['worker_finished'] = True

    def _u1_background_worker(self, eventtime, endpoint_name):
        job = self._u1_background_jobs.get(endpoint_name)
        if job is None:
            return
        source_device = job['source_device']
        source_channel = int(job['source_channel'])
        endpoint = job['endpoint']
        failing_device = source_device
        failing_channel = source_channel
        phase = ('BACKGROUND_PRESTAGE' if job.get('source_tail_prepared')
                 else 'WAIT_SOURCE_PARKED')
        try:
            target_kind = str(job.get('target_kind', '') or '')
            self._set_u1_background_phase(job, phase)

            if job.get('source_tail_prepared'):
                pullback = {
                    'skipped': True,
                    'reason': ('ungripped tail reached the head-sensor handoff '
                               'boundary; follower filament still owns final purge'),
                    'tail_handoff': copy.deepcopy(
                        job.get('tail_handoff', {})),
                }
            else:
                context = job['unload_context']
                tail_plan = context.get('parked_tip_tail_plan')
                if tail_plan and not context.get('parked_tip_tail_complete'):
                    phase = 'BACKGROUND_TIP_TAIL'
                    self._set_u1_background_phase(job, phase)
                    ticket = job.get('parked_tip_tail')
                    if not isinstance(ticket, dict) or 'id' not in ticket:
                        if job.get('cancelled'):
                            raise BMCUError(
                                'Snapmaker background swap cancelled before '
                                'exact tip program ticket became available')
                        raise BMCUError(
                            'Snapmaker background source is missing the exact '
                            'single-lane tip ticket; tail-only fallback is disabled')
                    logging.info(
                        'BMCU background worker adopted the source Head exact '
                        'single-lane tip program queued before its first E move '
                        'on %s', endpoint.name)
                    try:
                        tail_result = endpoint.wait_parked_tip_tail(
                            ticket['id'],
                            cancel_check=lambda: bool(job.get('cancelled')),
                            poll_interval=self._u1_background_poll_interval)
                    except Exception:

                        if context.get('heater_restore_deferred_to_tip_tail'):
                            restore_target = context.get('heater_target')
                            try:
                                if restore_target is not None:
                                    endpoint.restore_heater_target(restore_target)
                                context['heater_restore_deferred_to_tip_tail'] = False
                            except Exception:
                                logging.exception(
                                    'BMCU could not restore %s heater target '
                                    'after exact tip temperature-program failure',
                                    endpoint.name)
                        raise
                    context['parked_tip_tail_complete'] = True
                    job['parked_tip_tail'] = copy.deepcopy(tail_result)
                    if context.get('heater_restore_deferred_to_tip_tail'):
                        restore_target = context.get('heater_target')
                        if restore_target is not None:
                            endpoint.restore_heater_target(restore_target)
                        context['heater_restore_deferred_to_tip_tail'] = False
                        logging.info(
                            'BMCU restored %s captured heater target after '
                            'exact tip temperature program completion',
                            endpoint.name)
                    if job.get('cancelled'):
                        raise BMCUError(
                            'Snapmaker background swap cancelled after parked tip tail: %s' %
                            job.get('cancel_reason', 'cancelled'))
                self._start_delegated_u1_pullback_locked(
                    source_device, endpoint, source_channel, context,
                    require_park=bool(context.get('reconcile_requires_park')),
                    cancel_check=lambda: bool(job.get('cancelled')),
                    background=True)
                phase = 'BACKGROUND_PULLBACK'
                self._set_u1_background_phase(job, phase)

                job['head_pick_safe'] = True
                job['head_pick_safe_evidence'] = {
                    'reason': 'parked_tip_tail_complete_pullback_started',
                    'source_channel': int(source_channel),
                    'observed_at': self.reactor.monotonic(),
                }
                context['head_prefetch_safe_mm'] = 0.0
                context['head_prefetch_clearance_source'] = (
                    'parked_tip_tail_complete_pullback_started')
                job['head_prefetch_safe_mm'] = 0.0
                job['head_prefetch_clearance_source'] = (
                    'parked_tip_tail_complete_pullback_started')
                logging.info(
                    'BMCU Snapmaker %s released target Head pickup immediately '
                    'after the parked negative tail completed and Channel %d '
                    'long pullback started; source retract remains independent',
                    endpoint.name, source_channel + 1)

                pullback = self._finish_unload_locked(
                    source_device, endpoint, source_channel, context,
                    release_endpoint=False,
                    preserve_sensor_takeover=True,
                    cancel_check=lambda: bool(job.get('cancelled')),
                    background=True)
            job['pullback'] = copy.deepcopy(pullback)
            job['source_route_empty'] = bool(
                self._route_state(source_device, source_channel) ==
                protocol.ROUTE_EMPTY)
            if not job.get('source_route_empty'):
                raise BMCUError(
                    'BACKGROUND_SOURCE_NOT_EMPTY after confirmed pullback')
            if job.get('cancelled'):
                raise BMCUError(
                    'Snapmaker background swap cancelled: %s' %
                    job.get('cancel_reason', 'cancelled'))
            redirect_route = job.get('redirect_route')
            if isinstance(redirect_route, dict):

                job['target_identity'] = self._u1_source_identity(redirect_route)
                job['target_kind'] = str(redirect_route.get('kind', '') or '')
                job['target_tool'] = int(redirect_route.get('tool', -1))
                job['target_device'] = (
                    redirect_route.get('device')
                    if redirect_route.get('kind') == 'bmcu' else None)
                job['target_channel'] = (
                    int(redirect_route.get('channel'))
                    if redirect_route.get('kind') == 'bmcu' else None)
                job['state'] = 'handoff'
                job['phase'] = 'READY_EMPTY'
                logging.info(
                    'BMCU redirected background preparation on %s to requested T%d before prestage',
                    endpoint.name, int(job.get('target_tool', -1)))
                return
            target_device = job.get('target_device')
            target_channel = job.get('target_channel')
            target_tool = int(job.get('target_tool', -1))
            if target_kind == 'bmcu':
                if target_device is None or target_channel is None:
                    raise BMCUError('background BMCU target is incomplete')
                target_selected = bool(
                    job.get('foreground_target_requested') and
                    job.get('foreground_head_prefetched'))
                if target_selected:
                    logging.info(
                        'BMCU source route is EMPTY on %s; starting immediate '
                        'background SEND_OUT for T%d into the already selected '
                        'target Head', endpoint.name, target_tool)
                elif job.get('foreground_target_requested'):
                    logging.info(
                        'BMCU foreground requested exact target T%d on %s after '
                        'source EMPTY; starting one continuous background SEND_OUT '
                        'lease that may overlap PICK/ACTIVATE',
                        target_tool, endpoint.name)
                failing_device = target_device
                failing_channel = int(target_channel)
                phase = 'BACKGROUND_PRESTAGE'
                self._set_u1_background_phase(job, phase)
                def publish_prestage_operation(op_id, policy):
                    job['background_prestage_motion_started'] = True
                    job['background_prestage_op_id'] = int(op_id)
                    job['background_prestage_policy'] = copy.deepcopy(policy)
                    job['background_prestage_started_at'] = (
                        self.reactor.monotonic())
                    logging.info(
                        'BMCU background SEND_OUT lease op=%d started for exact '
                        'target T%d on %s %s', int(op_id), target_tool,
                        'selected' if target_selected else 'parked',
                        endpoint.name)

                staged = self._prestage_u1_to_entry_locked(
                    target_device, int(target_channel), endpoint, target_tool,
                    cancel_check=lambda: bool(job.get('cancelled')),
                    allowed_detached_source=(
                        (source_device, source_channel)
                        if job.get('source_tail_prepared') else None),
                    background=True,

                    handoff_check=lambda: bool(
                        isinstance(job.get('redirect_route'), dict)),
                    operation_started_callback=publish_prestage_operation,
                    target_selected=target_selected)
                if job.get('cancelled'):
                    raise BMCUError(
                        'Snapmaker background swap cancelled after prestage: %s' %
                        job.get('cancel_reason', 'cancelled'))
                if staged.get('at_entry'):
                    job['state'] = 'ready'
                    job['phase'] = 'READY_AT_ENTRY'
                    logging.info(
                        'BMCU Snapmaker background swap prepared T%d on %s',
                        target_tool, endpoint.name)
                else:
                    job['state'] = 'partial'
                    job['phase'] = 'READY_PARTIAL'
                    job['partial_prestage'] = copy.deepcopy(staged)
                    logging.info(
                        'BMCU Snapmaker background handed partially prestaged '
                        'T%d to foreground Head %s', target_tool, endpoint.name)
            elif target_kind == 'native':
                phase = 'BACKGROUND_NATIVE_HANDOFF'
                self._set_u1_background_phase(job, phase)
                if job.get('source_tail_prepared'):

                    job['state'] = 'ready_native'
                    job['phase'] = 'READY_NATIVE_FOLLOWER'
                    logging.info(
                        'BMCU Snapmaker background prepared native T%d follower '
                        'handoff on %s', target_tool, endpoint.name)
                else:
                    if not self._release_u1_persistent_hold_if_safe(
                            endpoint,
                            'background unload prepared the native source',
                            close_generation=True):
                        owner = self._u1_owner_status(endpoint)
                        if owner.get('owner') not in ('native', 'native_busy'):
                            raise BMCUError(
                                'native feeder ownership was not restored after background unload')
                    job['state'] = 'ready_native'
                    job['phase'] = 'READY_NATIVE'
                    logging.info(
                        'BMCU Snapmaker background unload prepared native T%d on %s',
                        target_tool, endpoint.name)
            else:
                raise BMCUError('background transition has no future source')
        except Exception as exc:
            job['state'] = 'cancelled' if job.get('cancelled') else 'error'
            job['phase'] = phase
            job['error'] = str(exc)
            self._stop_on_failure(
                failing_device, endpoint, exc, phase, failing_channel,
                pause_print=False, restore_sensors=False,
                wait_for_control_plane=True)
            if not job.get('cancelled'):
                logging.exception(
                    'BMCU Snapmaker background swap failed on %s; active print continues',
                    endpoint.name)
        finally:
            self._unlock_u1_background_job(job)
            self._save_runtime()

    def _start_u1_background_transition(self, source, target, gcmd=None):
        if not isinstance(target, dict):
            return False
        if self._u1_same_physical_source(source, target):
            return False
        source_device = source['device']
        source_channel = int(source['channel'])
        endpoint = source['endpoint']
        source_tool = int(source['tool'])
        if endpoint.name in self._u1_background_jobs:
            raise BMCUError(
                'endpoint %s already has a Snapmaker background filament operation' %
                endpoint.name)
        target_kind = str(target.get('kind', '') or '')
        target_device = target.get('device') if target_kind == 'bmcu' else None
        target_channel = (int(target.get('channel'))
                          if target_kind == 'bmcu' else None)
        target_tool = int(target.get('tool', -1))
        target_identity = self._u1_source_identity(target)
        if target_kind not in ('bmcu', 'native') or target_identity is None:
            raise BMCUError('invalid Snapmaker background target')
        self._preempt_refill_for_toolchange(
            source_device, source_channel, endpoint)
        try:
            if target_device is not None and target_device.name != source_device.name:
                self._lock_refill(
                    source_device, target_device, endpoint,
                    'SNAPMAKER BACKGROUND T%d TO T%d' %
                    (source_tool, target_tool))
                lock_kind = 'refill'
            else:
                self._lock(
                    source_device, endpoint,
                    'SNAPMAKER BACKGROUND T%d' % source_tool,
                    channel=source_channel)
                lock_kind = 'single'
        except Exception as exc:

            logging.info(
                'BMCU skipped optional background preparation on %s because '
                'the required lock became unavailable: %s', endpoint.name, exc)
            if gcmd is not None:
                gcmd.respond_info(
                    'BMCU left T%d loaded because background resources became busy' %
                    source_tool)
            return False
        job = {
            'state': 'starting', 'phase': 'BACKGROUND_RELEASE',
            'source_tool': source_tool, 'source_device': source_device,
            'source_channel': source_channel, 'target_tool': target_tool,
            'target_kind': target_kind, 'target_identity': target_identity,
            'target_device': target_device, 'target_channel': target_channel,
            'endpoint': endpoint, 'lock_kind': lock_kind, 'locked': True,
            'cancelled': False, 'worker_scheduled': False,
            'worker_started': False, 'worker_finished': False,
            'foreground_released': False, 'source_lane_ready': False,
            'parked_tip_tail_queued_early': False,
            'head_pick_safe': False,
            'source_route_empty': False,
            'background_prestage_motion_started': False,
            'background_prestage_op_id': None,
            'foreground_head_prefetched': False,
            'foreground_load_context_prepared': False,
            'foreground_discard_position_prepared': False,
        }
        self._u1_background_jobs[endpoint.name] = job
        self._set_u1_background_phase(job, 'BACKGROUND_RELEASE')
        try:
            if self._snapmaker_ungripped_tail(
                    source_device, source_channel, endpoint):
                job['tail_handoff'] = (
                    self._prepare_snapmaker_ungripped_tail_handoff_locked(
                        source_device, endpoint, source_channel,
                        temperature_profile=source.get('temperature_profile'),
                        restore_heater=True,
                        cancel_check=lambda: bool(job.get('cancelled'))))
                job['source_tail_prepared'] = True
            else:
                context = self._begin_unload_locked(
                    source_device, endpoint, source_channel,
                    reconcile_requires_park=True,
                    temperature_profile=source.get('temperature_profile'))
                job['unload_context'] = context

                tail_plan = context.get('parked_tip_tail_plan')
                if tail_plan and not context.get('parked_tip_tail_complete'):
                    self._set_u1_background_phase(
                        job, 'BACKGROUND_TIP_TAIL_QUEUE')
                    ticket = context.get('parked_tip_tail_ticket')
                    if not isinstance(ticket, dict) or 'id' not in ticket:
                        raise BMCUError(
                            'Snapmaker source Head tip tail was not queued '
                            'before pre-park blob cleanup')
                    job['parked_tip_tail'] = copy.deepcopy(ticket)
                    job['parked_tip_tail_queued_early'] = True
                    context['parked_tip_tail_queued'] = True
                    logging.info(
                        'BMCU adopted source Head exact single-lane tip program '
                        'queued before its first E move on %s; cutoff/park crossed '
                        'the internal marker and foreground work may continue '
                        'independently', endpoint.name)
                job['source_lane_ready'] = bool(
                    context.get('park_confirmed_before_pullback'))
            if not job.get('source_lane_ready'):
                endpoint.park_selected_head_for_pullback()
                context = job.get('unload_context')
                if isinstance(context, dict):
                    context['endpoint_released'] = True
                    context['park_confirmed_before_pullback'] = True
                job['source_lane_ready'] = True
            job['state'] = 'running'
            self._set_u1_background_phase(
                job, 'BACKGROUND_PRESTAGE' if job.get('source_tail_prepared')
                else 'BACKGROUND_SOURCE_LANE' if job.get('source_lane_ready')
                else 'WAIT_SOURCE_PARKED')
            self._schedule_u1_background_worker(endpoint.name)
        except Exception as exc:
            phase = job.get('phase', 'BACKGROUND_RELEASE')
            error = BMCUError(
                'Snapmaker background release of T%d failed during %s: %s' %
                (source_tool, phase, exc))
            job['state'] = 'error'
            job['error'] = str(error)
            self._stop_on_failure(
                source_device, endpoint, error, phase, source_channel)
            self._unlock_u1_background_job(job)
            self._u1_background_jobs.pop(endpoint.name, None)
            raise error
        if gcmd is not None:
            if target_kind == 'bmcu':
                gcmd.respond_info(
                    'BMCU armed background T%d unload and T%d prestage on %s' %
                    (source_tool, target_tool, endpoint.name))
            else:
                gcmd.respond_info(
                    'BMCU armed background T%d unload for future native T%d on %s' %
                    (source_tool, target_tool, endpoint.name))
        return endpoint.name

    def _park_snapmaker_detached_tail_for_later(
            self, source, reason, gcmd=None):

        if not isinstance(source, dict):
            return False
        device = source.get('device')
        endpoint = source.get('endpoint')
        try:
            channel = int(source.get('channel', -1))
        except (TypeError, ValueError, OverflowError):
            return False
        if (device is None or endpoint is None or channel < 0 or
                endpoint.driver != 'snapmaker_u1'):
            return False
        self._preempt_refill_for_toolchange(device, channel, endpoint)
        self._mark_u1_tail_detached(endpoint, device, channel, reason)

        device.set_motion(channel, protocol.MOTION_IDLE)

        self._refill_runout_latched.add((device.name, channel))
        self._save_runtime()
        if gcmd is not None:
            gcmd.respond_info(
                'BMCU parked detached T%d tail in %s for later use' %
                (int(source.get('tool', -1)), endpoint.name))
        return True

    def _rearm_snapmaker_detached_tail_monitor(
            self, device, channel, tool, endpoint):

        channel = int(channel)
        if not self._u1_tail_detached_matches(
                endpoint, device, channel):
            return False
        present = getattr(device, 'status', {}).get('present')
        if (isinstance(present, (list, tuple)) and channel < len(present) and
                bool(present[channel])):
            raise BMCUError(
                'exhausted source Channel %d contains newly inserted filament '
                'while its old detached tail is still in %s; remove the new '
                'filament before selecting this route' %
                (channel + 1, endpoint.name))
        self._refill_runout_latched.discard((device.name, channel))
        scheduled = self._notify_refill_runout_once(
            device, channel, int(tool), endpoint)
        if scheduled:

            self.reactor.pause(self.reactor.monotonic() + 0.100)
        return scheduled

    def _prepare_u1_background_transition(self, target_tool, plan_index, gcmd=None):

        if plan_index is None or int(plan_index) < 0 or not self.print_map_active:
            return False
        source = self._u1_logical_route(self.active_tool)
        target_now = self._u1_logical_route(target_tool)
        if (source is None or source.get('kind') != 'bmcu' or
                source.get('endpoint') is None or target_now is None or
                target_now.get('endpoint') is None):
            return False
        endpoint = source['endpoint']
        if target_now['endpoint'].name == endpoint.name:

            return False
        if self._route_state(
                source['device'], source['channel']) != protocol.ROUTE_LOADED:
            return False
        source_ungripped = self._snapmaker_ungripped_tail(
            source['device'], source['channel'], endpoint)
        if endpoint.name in self._u1_background_jobs:
            logging.info(
                'BMCU skipped optional background swap on %s because that '
                'head already has a background job', endpoint.name)
            return False
        next_route = self._next_u1_route_for_endpoint(
            endpoint.name, int(plan_index))
        if next_route is None:

            if source_ungripped:
                self._park_snapmaker_detached_tail_for_later(
                    source,
                    'detached tail parked after final planned use; terminal '
                    'print-end policy owns evacuation',
                    gcmd=gcmd)
            return False
        if self._u1_same_physical_source(source, next_route):

            if source_ungripped:
                self._park_snapmaker_detached_tail_for_later(
                    source,
                    'detached tail parked because the same physical source is '
                    'planned again',
                    gcmd=gcmd)
            return False
        source['temperature_profile'] = self._u1_active_temperature_profile()

        required_now = set()
        if target_now.get('kind') == 'bmcu' and target_now.get('device') is not None:
            required_now.add(target_now['device'].name)
        background_devices = {source['device'].name}
        if next_route.get('kind') == 'bmcu' and next_route.get('device') is not None:
            background_devices.add(next_route['device'].name)
        if required_now.intersection(background_devices):
            logging.info(
                'BMCU skipped optional background swap on %s because the current '
                'tool requires the same controller', endpoint.name)
            return False
        if endpoint.shared_path_group() == target_now['endpoint'].shared_path_group():
            logging.info(
                'BMCU skipped optional background swap on %s because the '
                'current tool requires the same shared filament path', endpoint.name)
            return False
        if any(name in self.active_operations for name in background_devices):
            logging.info(
                'BMCU skipped optional background swap on %s because a '
                'required controller is already busy', endpoint.name)
            return False
        conflict = self._u1_background_controller_conflict(
            background_devices, int(plan_index),
            int(next_route.get('plan_index', len(self._u1_toolchange_plan))),
            path_groups=(endpoint.shared_path_group(),))
        if conflict is not None:
            logging.info(
                'BMCU skipped optional background swap on %s because T%d '
                'needs controller %s or its shared filament path first', endpoint.name,
                int(conflict['tool']), conflict['device'])
            return False

        if next_route.get('kind') == 'bmcu':
            try:
                target_device = next_route.get('device')
                target_channel = int(next_route.get('channel', -1))
                if target_device is None or target_channel < 0:
                    raise BMCUError('future BMCU source is incomplete')
                if self._route_state(
                        target_device, target_channel) != protocol.ROUTE_EMPTY:
                    raise BMCUError(
                        '%s Channel %d is not EMPTY' %
                        (target_device.name, target_channel + 1))
                self._check_automatic_ready(target_device, target_channel)
            except Exception as exc:

                logging.warning(
                    'BMCU skipped optional future-channel preparation on %s: %s',
                    endpoint.name, exc)
                return False
        elif next_route.get('kind') == 'native':
            try:
                record = self._u1_ownership_record(endpoint.name)
                expected_enabled = None
                if record.get('baseline_captured'):
                    expected_enabled = not bool(record.get('baseline_disabled'))
                endpoint.native_source_preflight(
                    expected_enabled=expected_enabled, require_filament=True)
            except Exception as exc:

                logging.warning(
                    'BMCU skipped optional native preparation on %s: %s',
                    endpoint.name, exc)
                return False
        return self._start_u1_background_transition(
            source, next_route, gcmd=gcmd)

    def _redirect_ready_u1_background_job(self, job, route):
        endpoint = job.get('endpoint')
        if endpoint is None:
            raise BMCUError('background redirect lost its endpoint')
        state = str(job.get('state', '') or '')
        if state in ('ready', 'partial'):
            target_device = job.get('target_device')
            target_channel = job.get('target_channel')
            if target_device is None or target_channel is None:
                raise BMCUError('prepared background source is incomplete')
            route_key = self._route_key(target_device, int(target_channel))
            staged = self.prestaged.get(route_key)
            if staged is not None:
                self._lock(
                    target_device, endpoint,
                    'REDIRECT SNAPMAKER PRESTAGE',
                    channel=int(target_channel))
                try:
                    self._clear_prestage_locked(
                        target_device, endpoint, staged,
                        preserve_sensor_takeover=True,
                        target_selected=bool(
                            job.get('foreground_head_prefetched')))
                finally:
                    self._unlock(target_device, endpoint)
        elif state not in ('ready_native', 'handoff'):
            raise BMCUError(
                'background redirect cannot recover state %s' % state)
        if (route.get('kind') != 'bmcu' and
                job.get('foreground_load_context_prepared')):
            try:
                endpoint.finish_load_temperature(success=False)
            except Exception:
                logging.exception(
                    'BMCU could not restore redirected U1 prefetch temperature on %s',
                    endpoint.name)
            job['foreground_load_context_prepared'] = False
            job['foreground_discard_position_prepared'] = False
        job['redirect_route'] = route
        job['target_identity'] = self._u1_source_identity(route)
        job['target_kind'] = str(route.get('kind', '') or '')
        job['target_tool'] = int(route.get('tool', -1))
        job['target_device'] = (route.get('device')
                                if route.get('kind') == 'bmcu' else None)
        job['target_channel'] = (int(route.get('channel'))
                                 if route.get('kind') == 'bmcu' else None)
        job['state'] = 'handoff'
        job['phase'] = 'READY_EMPTY'
        return job

    def _prefetch_u1_background_target_head(
            self, job, route, temperature_profile=None):

        phase = str(job.get('phase', '') or '') if isinstance(job, dict) else ''
        pullback_overlap = bool(
            isinstance(job, dict) and
            phase == 'BACKGROUND_PULLBACK' and
            job.get('head_pick_safe'))
        live_prestage_overlap = bool(
            isinstance(job, dict) and
            phase == 'BACKGROUND_PRESTAGE' and
            job.get('source_route_empty') and
            job.get('background_prestage_motion_started') and
            job.get('background_prestage_op_id') is not None)
        if (not isinstance(job, dict) or not isinstance(route, dict) or
                route.get('kind') != 'bmcu' or
                not (pullback_overlap or live_prestage_overlap)):
            return False
        if job.get('foreground_head_prefetched'):
            return True
        endpoint = route.get('endpoint')
        device = route.get('device')
        try:
            channel = int(route.get('channel'))
        except (TypeError, ValueError, OverflowError):
            raise BMCUError('foreground Head prefetch has an invalid Channel')
        if (endpoint is None or device is None or
                endpoint is not job.get('endpoint') or
                device is not job.get('target_device') or
                channel != int(job.get('target_channel', -1)) or
                self._u1_source_identity(route) != job.get('target_identity')):
            raise BMCUError(
                'foreground Head prefetch target does not match background job')
        if job.get('redirect_route') is not None:
            return False
        context = job.get('unload_context')
        if not isinstance(context, dict):
            return False

        metadata = self._channel_metadata(device, channel)
        material = metadata.get('material', '')
        profile = self._u1_load_temperature_profile(
            endpoint, temperature_profile, material=material)
        load_context_started = False
        selected = False
        context['foreground_head_prefetch_in_progress'] = True
        try:
            endpoint.suspend_managed_sensors()
            endpoint.select()
            endpoint.verify_selected()
            selected = True
            if pullback_overlap:
                context['foreground_head_reselected_during_pullback'] = True
            job['foreground_head_prefetched'] = True
            job['foreground_head_prefetch_mode'] = (
                'source_pullback' if pullback_overlap else
                'continuous_background_sendout')
            job['foreground_head_prefetched_at'] = self.reactor.monotonic()

            self._endpoint_temperature_call(
                endpoint, 'prepare_load', material, profile)
            load_context_started = True
            job['foreground_load_context_prepared'] = True
            job['foreground_prefetch_material'] = str(material or '')
            job['foreground_temperature_profile'] = copy.deepcopy(profile)

            prepare_position = getattr(endpoint, 'prepare_load_position', None)
            if callable(prepare_position):
                prepare_position(material, profile)
                job['foreground_discard_position_prepared'] = True
            if live_prestage_overlap:
                logging.info(
                    'BMCU picked target T%d Head %s while background SEND_OUT '
                    'op=%d kept its continuous lease; no ABORT, terminal IDLE or '
                    'second SEND_OUT was issued', int(route.get('tool', -1)),
                    endpoint.name, int(job.get('background_prestage_op_id')))
            else:
                logging.info(
                    'BMCU prefetched target T%d Head %s during source pullback; '
                    'heater and discard position are ready before EMPTY',
                    int(route.get('tool', -1)), endpoint.name)
            return True
        except Exception:

            if selected:
                if pullback_overlap:
                    context['foreground_head_reselected_during_pullback'] = True
                job['foreground_head_prefetched'] = True
            if load_context_started:
                try:
                    endpoint.finish_load_temperature(success=False)
                except Exception:
                    logging.exception(
                        'BMCU could not restore speculative U1 prefetch temperature')
                job['foreground_load_context_prepared'] = False
            raise
        finally:
            context['foreground_head_prefetch_in_progress'] = False

    def _wait_u1_background_for_route(
            self, route, gcmd=None, temperature_profile=None):
        if not isinstance(route, dict) or route.get('endpoint') is None:
            return None
        endpoint = route['endpoint']
        job = self._u1_background_jobs.get(endpoint.name)
        if job is None:
            return None
        target_identity = self._u1_source_identity(route)
        if target_identity is None:
            message = 'requested Snapmaker source has no physical identity'
            if gcmd is not None:
                raise self._command_pause_error(gcmd, message)
            raise BMCUError(message)

        redirect = target_identity != job.get('target_identity')
        if not redirect:
            job['foreground_target_requested'] = True
            job['foreground_target_tool'] = int(route.get('tool', -1))
        if redirect:

            job['redirect_route'] = route
            job['redirect_identity'] = target_identity
            logging.info(
                'BMCU redirecting background work on %s to requested T%d',
                endpoint.name, int(route.get('tool', -1)))

        deadline = self.reactor.monotonic() + self._u1_background_wait_timeout([job])
        safe_wait_done = False

        def move_to_safe_wait_before_pause():
            nonlocal safe_wait_done
            if safe_wait_done:
                return
            try:

                endpoint.move_to_safe_wait()
                safe_wait_done = True
            except Exception as exc:
                message = 'could not move active U1 head to safe wait position: %s' % exc
                if gcmd is not None:
                    raise self._command_pause_error(gcmd, message)
                raise BMCUError(message)

        while (job.get('state') in ('starting', 'running') or
               (job.get('worker_scheduled') and
                not job.get('worker_finished'))):
            if self._u1_cancel_requested or job.get('cancelled'):
                raise BMCUError('U1_BACKGROUND_WAIT_CANCELLED: %s' % endpoint.name)
            prefetch_ready = bool(
                (job.get('phase') == 'BACKGROUND_PULLBACK' and
                 job.get('head_pick_safe')) or
                (job.get('phase') == 'BACKGROUND_PRESTAGE' and
                 job.get('source_route_empty') and
                 job.get('background_prestage_motion_started') and
                 job.get('background_prestage_op_id') is not None))
            if (not redirect and route.get('kind') == 'bmcu' and
                    prefetch_ready and
                    not job.get('foreground_head_prefetched') and
                    not job.get('foreground_head_prefetch_error')):
                try:
                    self._prefetch_u1_background_target_head(
                        job, route, temperature_profile=temperature_profile)
                except Exception as exc:

                    job['foreground_head_prefetch_error'] = str(exc)
                    logging.exception(
                        'BMCU target Head prefetch failed on %s; source pullback continues',
                        endpoint.name)
            if self.reactor.monotonic() >= deadline:
                message = 'Snapmaker background preparation timed out on %s' % endpoint.name
                if gcmd is not None:
                    raise self._command_pause_error(gcmd, message)
                raise BMCUError(message)
            self.reactor.pause(self.reactor.monotonic() + 0.250)

        if job.get('state') == 'error':
            move_to_safe_wait_before_pause()
            message = 'Snapmaker background preparation failed on %s: %s' % (
                endpoint.name, job.get('error', 'unknown error'))
            if gcmd is not None:
                raise self._command_pause_error(gcmd, message)
            raise BMCUError(message)
        if job.get('state') == 'cancelled':
            move_to_safe_wait_before_pause()
            message = 'Snapmaker background preparation was cancelled on %s' % endpoint.name
            if gcmd is not None:
                raise self._command_pause_error(gcmd, message)
            raise BMCUError(message)
        if job.get('foreground_head_prefetch_error'):
            move_to_safe_wait_before_pause()
            message = 'Snapmaker target Head prefetch failed on %s: %s' % (
                endpoint.name, job.get('foreground_head_prefetch_error'))
            if gcmd is not None:
                raise self._command_pause_error(gcmd, message)
            raise BMCUError(message)

        if redirect:
            return self._redirect_ready_u1_background_job(job, route)

        if job.get('state') == 'handoff':
            return job

        expected_states = (('ready', 'partial')
                           if route.get('kind') == 'bmcu'
                           else ('ready_native',))
        if job.get('state') not in expected_states:
            move_to_safe_wait_before_pause()
            message = (
                'Snapmaker background preparation on %s ended in invalid state %s' %
                (endpoint.name, job.get('state', 'unknown')))
            if gcmd is not None:
                raise self._command_pause_error(gcmd, message)
            raise BMCUError(message)
        return job

    def _consume_u1_background_route(self, route):
        if not isinstance(route, dict) or route.get('endpoint') is None:
            return False
        endpoint = route['endpoint']
        job = self._u1_background_jobs.get(endpoint.name)
        if (job is not None and
                self._u1_source_identity(route) == job.get('target_identity') and
                (not job.get('worker_scheduled') or job.get('worker_finished')) and
                job.get('state') in ('ready', 'partial', 'ready_native', 'handoff')):
            self._unlock_u1_background_job(job)
            self._u1_background_jobs.pop(endpoint.name, None)
            return True
        return False

    @staticmethod
    def _endpoint_temperature_call(endpoint, method_name, material,
                                   temperature_profile=None, **context):

        method = getattr(endpoint, method_name)
        adapter = getattr(endpoint, 'material_operation_kwargs', None)
        if callable(adapter):
            kwargs = adapter(
                method_name, temperature_profile=temperature_profile,
                **context)
        else:

            kwargs = {}
            if bool(getattr(
                    endpoint, 'supports_print_temperature_profile', False)):
                kwargs['temperature_profile'] = temperature_profile
        return method(material, **kwargs)

    def _preempt_refill_for_toolchange(self, device, channel, endpoint):
        refill = getattr(self, 'refill', None)
        preempt = getattr(refill, 'preempt_for_toolchange', None)
        if not callable(preempt):
            return None
        try:
            return preempt(
                device, int(channel), endpoint, timeout=5.0)
        except Exception as exc:
            raise BMCUError(
                'could not yield runout-tail recovery on %s for tool change: %s' %
                (endpoint.name, exc))

    def _snapmaker_ungripped_tail(self, device, channel, endpoint):
        if endpoint.driver != 'snapmaker_u1':
            return False
        channel = int(channel)
        status = getattr(device, 'status', None)
        present = status.get('present') if isinstance(status, dict) else None

        if not isinstance(present, (list, tuple)):
            return False
        if channel >= len(present):
            raise BMCUError(
                'BMCU present state is incomplete for Channel %d' %
                (channel + 1))
        if self._u1_tail_detached_matches(endpoint, device, channel):
            return True
        if bool(present[channel]):
            return False

        raw_state = self._route_states_from_status(status)[channel]
        route_key = self._route_key(device, channel)
        journal_loaded = bool(
            self._journal_route_aliases(device, channel).intersection(
                self.print_loaded_routes))
        logical_loaded = route_key in self.loaded_tools
        return bool(
            raw_state == protocol.ROUTE_LOADED or
            journal_loaded or logical_loaded)

    def _ungripped_tail(self, device, channel, endpoint):

        if endpoint is None:
            return False
        if endpoint.driver == 'snapmaker_u1':
            return self._snapmaker_ungripped_tail(
                device, int(channel), endpoint)

        channel = int(channel)
        if self._generic_tail_detached_matches(
                endpoint, device, channel):
            return True
        present = getattr(device, 'status', {}).get('present')
        if not isinstance(present, (list, tuple)) or channel >= len(present):
            return False
        if bool(present[channel]):
            return False
        raw = self._route_states_from_status(device.status)[channel]
        route_key = self._route_key(device, channel)
        journal_loaded = bool(
            self._journal_route_aliases(device, channel).intersection(
                self.print_loaded_routes))
        logical_loaded = route_key in self.loaded_tools
        if not (raw == protocol.ROUTE_LOADED or journal_loaded or logical_loaded):
            return False
        self._mark_generic_tail_detached(
            endpoint, device, channel,
            'BMCU input empty while the route still owns the endpoint')
        return True

    def _prepare_generic_ungripped_tail_handoff_locked(
            self, device, endpoint, channel):

        channel = int(channel)
        if not self._ungripped_tail(device, channel, endpoint):
            return None
        if not device.connected:
            raise BMCUError(
                'source BMCU disconnected before forward-only handoff')
        device.stop_all()
        device.mark_unloaded(channel)
        device.refresh()
        if self._route_states_from_status(device.status)[channel] != protocol.ROUTE_EMPTY:
            raise BMCUError(
                'exhausted source route did not commit upstream EMPTY')
        self._save_runtime()
        return {'forward_only': True}

    def _prepare_snapmaker_ungripped_tail_handoff_locked(
            self, device, endpoint, channel, temperature_profile=None,
            restore_heater=True, cancel_check=None):

        channel = int(channel)
        if not self._snapmaker_ungripped_tail(device, channel, endpoint):
            return None
        if not device.connected:
            raise BMCUError(
                'source BMCU disconnected before detached-tail handoff')
        prepare = getattr(endpoint, 'prepare_runout_tail_handoff', None)
        if not callable(prepare):
            raise BMCUError(
                'Snapmaker endpoint %s has no detached-tail handoff support' %
                endpoint.name)
        self._arm_u1_persistent_hold(
            endpoint, device, channel, 'prepare detached tail for follower')

        already_marked = self._u1_tail_detached_matches(
            endpoint, device, channel)
        if not already_marked:
            for sample in range(2):
                device.refresh()
                current_present = device.status.get('present', [])
                if (channel >= len(current_present) or
                        bool(current_present[channel])):
                    raise BMCUError(
                        'BMCU Channel %d input recovered before detached-tail '
                        'handoff; retry the tool change so normal reverse unload '
                        'can be evaluated safely' % (channel + 1))
                if sample == 0:
                    self.reactor.pause(
                        self.reactor.monotonic() + 0.10)
        self._mark_u1_tail_detached(
            endpoint, device, channel,
            'BMCU input lost the loaded spool tail; follower handoff required')
        current_present = getattr(device, 'status', {}).get('present')
        if (isinstance(current_present, (list, tuple)) and
                channel < len(current_present) and
                bool(current_present[channel])):
            raise BMCUError(
                'exhausted source Channel %d was refilled before its old tail '
                'reached the Snapmaker handoff boundary; remove that newly '
                'inserted filament and retry' % (channel + 1))

        started_in_print = self._print_state() in (
            'printing', 'paused', 'pause')
        phase = 'TAIL_FOLLOWER_HANDOFF'
        self._set_phase(device, phase)
        endpoint.suspend_managed_sensors()
        endpoint.select()
        endpoint.verify_selected()
        device.set_motion(channel, protocol.MOTION_IDLE)
        metadata = self._channel_metadata(device, channel)
        material = metadata.get('material', '')

        def tail_cancelled_or_reinserted():
            if not device.connected:
                return True
            if callable(cancel_check) and cancel_check():
                return True
            if (started_in_print and self._print_state() not in
                    ('printing', 'paused', 'pause')):
                return True
            current_status = getattr(device, 'status', None)
            present = (current_status.get('present')
                       if isinstance(current_status, dict) else None)
            return bool(
                isinstance(present, (list, tuple)) and
                channel < len(present) and present[channel])

        evidence = self._endpoint_temperature_call(
            endpoint, 'prepare_runout_tail_handoff', material,
            temperature_profile,
            cancel_check=tail_cancelled_or_reinserted,
            restore_heater=bool(restore_heater))
        device.refresh()
        present = device.status.get('present', [0, 0, 0, 0])
        if channel >= len(present) or bool(present[channel]):
            raise BMCUError(
                'source BMCU Channel %d became present during detached-tail '
                'handoff; the route was not released' % (channel + 1))
        if endpoint.sensor_detected('entry_sensor') is not False:
            raise BMCUError(
                'Snapmaker head sensor did not reach the follower handoff boundary')
        self._mark_u1_tail_sensor_cleared(
            endpoint, device, channel,
            'forward tail handoff reached the head-sensor boundary')

        device.mark_unloaded(channel)
        device.refresh()
        raw_route = self._route_states_from_status(device.status)[channel]
        if raw_route != protocol.ROUTE_EMPTY:
            raise BMCUError(
                'source BMCU Channel %d did not commit upstream EMPTY at the '
                'detached-tail boundary' % (channel + 1))
        self._save_runtime()
        self.last_diagnostic = {
            'operation': 'snapmaker_detached_tail_handoff',
            'device': device.name,
            'channel': channel,
            'endpoint': endpoint.name,
            'evidence': copy.deepcopy(evidence),
            'effective_route': 'LOADED_UNTIL_FOLLOWER',
            'phase': phase,
        }
        logging.info(
            'BMCU prepared exhausted tail from %s Channel %d on %s for a '
            'follower filament (%.1f mm to head-sensor clear)',
            device.name, channel + 1, endpoint.name,
            float((evidence or {}).get('total_mm', 0.0) or 0.0))
        return evidence

    def _capture_snapmaker_detached_tail_with_follower(
            self, source_device, source_channel, follower_device,
            follower_channel, endpoint, material='', cancel_check=None):

        source_channel = int(source_channel)
        follower_channel = int(follower_channel)
        if endpoint.driver != 'snapmaker_u1':
            return None
        if not self._u1_tail_detached_matches(
                endpoint, source_device, source_channel):
            return None
        if endpoint.sensor_detected('entry_sensor') is not True:
            raise BMCUError(
                'detached-tail follower has not reached the Snapmaker head sensor')
        if not source_device.connected or not follower_device.connected:
            raise BMCUError(
                'BMCU disconnected before detached-tail follower capture')

        same_route = bool(
            source_device.name == follower_device.name and
            source_channel == follower_channel)
        maximum = float(endpoint.get('refill_handoff_max_mm', 120.0) or 120.0)
        chunk = float(endpoint.get('refill_handoff_chunk_mm', 5.0) or 5.0)
        feed = int(float(endpoint.get('refill_handoff_feed', 240.0) or 240.0))
        cleanup_every = float(
            endpoint.get('snap_tail_cleanup_every_mm', 20.0) or 20.0)
        if not (5.0 <= maximum <= 500.0):
            raise BMCUError('refill_handoff_max_mm must be 5..500')
        if not (1.0 <= chunk <= min(50.0, maximum)):
            raise BMCUError(
                'refill_handoff_chunk_mm must be 1..50 and no larger than max')
        if not (30 <= feed <= 3000):
            raise BMCUError('refill_handoff_feed must be 30..3000')
        if not (5.0 <= cleanup_every <= 100.0):
            raise BMCUError('snap_tail_cleanup_every_mm must be 5..100')

        follower_device.set_motion(
            follower_channel, protocol.MOTION_BEFORE_ON_USE)
        moved_total = 0.0
        since_cleanup = 0.0
        evidence = []
        captured = False
        signal_start = endpoint.capture_signal()
        while moved_total + 0.0001 < maximum:
            if callable(cancel_check) and cancel_check():
                raise BMCUError(
                    'detached-tail follower capture was cancelled')
            if not source_device.connected or not follower_device.connected:
                raise BMCUError(
                    'BMCU disconnected during detached-tail follower capture')
            source_present = source_device.status.get('present', [])
            if (not same_route and source_channel < len(source_present) and
                    bool(source_present[source_channel])):
                raise BMCUError(
                    'exhausted source Channel became present during follower '
                    'capture; remove the newly inserted source filament')
            follower_present = follower_device.status.get('present', [])
            if (follower_channel >= len(follower_present) or
                    not bool(follower_present[follower_channel])):
                raise BMCUError(
                    'replacement filament disappeared during detached-tail handoff')
            encoder_mask = int(
                follower_device.status.get('encoder_io_mask', 0) or 0)
            if not (encoder_mask & (1 << follower_channel)):
                raise BMCUError(
                    'replacement encoder is unavailable during detached-tail handoff')

            step = min(chunk, maximum - moved_total)
            before_m = float(
                follower_device.status['meters'][follower_channel])
            before_buffer = float(
                follower_device.status['buffer_pct'][follower_channel])
            endpoint.extrude(step, feed)
            moved_total += step
            since_cleanup += step
            if (since_cleanup + 0.0001 >= cleanup_every and
                    hasattr(endpoint, 'discard_runout_tail_chunk')):
                endpoint.discard_runout_tail_chunk(final=False)
                since_cleanup = 0.0
            follower_device.refresh()
            encoder_mm = abs(
                float(follower_device.status['meters'][follower_channel]) -
                before_m) * 1000.0
            buffer_drop = (before_buffer - float(
                follower_device.status['buffer_pct'][follower_channel]))
            post_gears = endpoint.sensor_detected('post_gears_sensor')
            signal_delta = endpoint.capture_signal_delta(
                signal_start, material)
            signal_ok = endpoint.capture_signal_ok(signal_delta, material)
            sample = {
                'extruded_mm': moved_total,
                'encoder_mm': encoder_mm,
                'buffer_drop': buffer_drop,
                'post_gears': post_gears,
                'native_signal_delta': signal_delta,
            }
            evidence.append(sample)
            if (post_gears is True or signal_ok or
                    encoder_mm >= step * self.bite_encoder_ratio or
                    buffer_drop >= self.bite_buffer_delta):
                captured = True
                break

        if not captured:
            raise BMCUError(
                'replacement was not captured behind the detached tail within '
                '%.1f mm' % maximum)
        if (since_cleanup > 0.0001 and
                hasattr(endpoint, 'discard_runout_tail_chunk')):
            endpoint.discard_runout_tail_chunk(final=False)
        result = {
            'captured': True,
            'same_route': same_route,
            'max_mm': maximum,
            'extruded_mm': moved_total,
            'cleanup_every_mm': cleanup_every,
            'samples': evidence[-16:],
        }
        self.last_diagnostic = {
            'operation': 'snapmaker_detached_tail_follower_capture',
            'source_device': source_device.name,
            'source_channel': source_channel,
            'follower_device': follower_device.name,
            'follower_channel': follower_channel,
            'endpoint': endpoint.name,
            'evidence': copy.deepcopy(result),
        }
        return result

    def _finalize_snapmaker_tail_handoff_after_follower(
            self, source_device, source_channel, endpoint,
            follower_device=None, follower_channel=None, logical_tool=None,
            reason='follower filament captured and primed'):

        source_channel = int(source_channel)
        if not self._u1_tail_detached_matches(
                endpoint, source_device, source_channel):
            return False
        record = self._u1_ownership_record(endpoint.name)
        follower_kind = str(
            record.get('follower_kind', 'bmcu') or 'bmcu').lower()
        native = follower_kind == 'native' and follower_device is None
        if not native:
            if follower_device is None:
                raise BMCUError('Snapmaker follower device is missing')
            follower_channel = int(follower_channel)
            if not 0 <= follower_channel <= 3:
                raise BMCUError('Snapmaker follower Channel is invalid')
            journal_uid = str(record.get('follower_uid', '') or '').upper()
            actual_uid = self._device_uid(follower_device)
            if record.get('follower_pending') and (
                    record.get('follower_kind') != 'bmcu' or
                    int(record.get('follower_channel', -1)) != follower_channel or
                    (journal_uid and journal_uid != actual_uid) or
                    (not journal_uid and str(
                        record.get('follower_device', '') or '') !=
                        follower_device.name)):
                raise BMCUError(
                    'Snapmaker follower does not match the durable journal')
            target_raw = self._route_states_from_status(
                follower_device.status)[follower_channel]
            target_present = follower_device.status.get(
                'present', [False] * 4)
            if (target_raw != protocol.ROUTE_LOADED or
                    follower_channel >= len(target_present) or
                    not bool(target_present[follower_channel])):
                raise BMCUError(
                    'Snapmaker follower is not positively LOADED/present')

        same_route = bool(
            not native and follower_device.name == source_device.name and
            follower_channel == source_channel)
        raw_state = self._route_states_from_status(
            source_device.status)[source_channel]
        if same_route:
            if raw_state != protocol.ROUTE_LOADED:
                raise BMCUError(
                    'same-Channel follower did not restore source route LOADED')
        elif raw_state != protocol.ROUTE_EMPTY:
            raise BMCUError(
                'detached source Channel %d is not raw EMPTY after follower '
                'capture' % (source_channel + 1))

        try:
            requested_tool = int(logical_tool)
        except (TypeError, ValueError, OverflowError):
            requested_tool = -1
        if not 0 <= requested_tool < U1_LOGICAL_TOOL_LIMIT:
            try:
                requested_tool = int(record.get('follower_tool', -1))
            except (TypeError, ValueError, OverflowError):
                requested_tool = -1
        source_key = self._route_key(source_device, source_channel)
        try:
            source_tool = int(self.loaded_tools.get(source_key, -1))
        except (TypeError, ValueError, OverflowError):
            source_tool = -1
        if not 0 <= requested_tool < U1_LOGICAL_TOOL_LIMIT:
            requested_tool = source_tool
        if not 0 <= requested_tool < U1_LOGICAL_TOOL_LIMIT:
            projected = []
            for tool_text, mapping in self.print_tools.items():
                if not isinstance(mapping, dict):
                    continue
                try:
                    tool_value = int(tool_text)
                    mapping_channel = int(mapping.get('channel', -1))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (0 <= tool_value < U1_LOGICAL_TOOL_LIMIT and
                        not mapping.get('native') and
                        self._mapping_matches_device(mapping, source_device) and
                        mapping_channel == source_channel):
                    projected.append(tool_value)
            projected = sorted(set(projected))
            if self.active_tool in projected:
                requested_tool = int(self.active_tool)
            elif len(projected) == 1:
                requested_tool = projected[0]
        if not 0 <= requested_tool < U1_LOGICAL_TOOL_LIMIT:
            raise BMCUError(
                'Snapmaker follower journal has no unambiguous logical tool; '
                'ownership remains pending for safe recovery')

        snapshot = {
            'record': copy.deepcopy(record),
            'loaded_tools': dict(self.loaded_tools),
            'print_tools': copy.deepcopy(self.print_tools),
            'print_map_active': bool(self.print_map_active),
            'active_tool': self.active_tool,
            'print_loaded_routes': set(self.print_loaded_routes),
            'print_route_journal_initialized': bool(
                self.print_route_journal_initialized),
            'print_terminal_unload_pending': bool(
                self.print_terminal_unload_pending),
            'print_session': copy.deepcopy(
                self.state.data.get('print_session', {})),
        }
        print_active = bool(
            self.print_map_active or
            self._print_state() in ('printing', 'paused', 'pause'))
        follower_key = None
        try:
            if not same_route:
                self.loaded_tools.pop(source_key, None)
                self.print_loaded_routes.difference_update(
                    self._journal_route_aliases(
                        source_device, source_channel))
            if native:
                if requested_tool >= 0 and print_active:
                    self.print_tools[str(requested_tool)] = {
                        'native': True,
                        'head': int(endpoint.get('head_index', -1)),
                    }
                    self.print_map_active = True
                    self.active_tool = requested_tool
            else:
                follower_key = self._route_key(
                    follower_device, follower_channel)
                if requested_tool >= 0:
                    self.loaded_tools[follower_key] = requested_tool
                    self.active_tool = requested_tool
                    if print_active:
                        self.print_tools[str(requested_tool)] = (
                            self._mapping_payload(
                                follower_device, follower_channel))
                        self.print_map_active = True
                        self.print_route_journal_initialized = True
                        self.print_loaded_routes.add(
                            self._journal_route_key(
                                follower_device, follower_channel))
            if not self.print_loaded_routes:
                self.print_terminal_unload_pending = False
            self._clear_u1_follower_commit(record)
            record['tail_detached'] = False
            record['tail_sensor_cleared'] = False
            if native:
                record['persistent_hold'] = False
                record['route_state'] = 'EMPTY'
                record['device'] = ''
                record['device_uid'] = ''
                record['channel'] = -1
                record['generation_open'] = False
            else:
                record['persistent_hold'] = True
                record['route_state'] = 'LOADED'
                record['device'] = follower_device.name
                record['device_uid'] = self._device_uid(follower_device)
                record['channel'] = follower_channel
            record['reason'] = str(reason or '')[:160]
            if print_active:
                self._save_print_session()
            else:
                self.state.save()
        except Exception:
            record.clear()
            record.update(snapshot['record'])
            self.loaded_tools = snapshot['loaded_tools']
            self.print_tools = snapshot['print_tools']
            self.print_map_active = snapshot['print_map_active']
            self.active_tool = snapshot['active_tool']
            self.print_loaded_routes = snapshot['print_loaded_routes']
            self.print_route_journal_initialized = (
                snapshot['print_route_journal_initialized'])
            self.print_terminal_unload_pending = (
                snapshot['print_terminal_unload_pending'])
            self.state.data['print_session'] = snapshot['print_session']
            raise

        self._refill_runout_latched.discard(
            (source_device.name, source_channel))
        if not same_route:
            self._drop_prestage_record(
                source_key, release_sensor=False)
        self._save_runtime()
        return True

    def _load_native_follower_for_detached_tail(
            self, source_device, source_channel, endpoint, logical_tool=-1):

        source_channel = int(source_channel)
        record = self._u1_ownership_record(endpoint.name)
        if not self._u1_tail_detached_matches(
                endpoint, source_device, source_channel):
            raise BMCUError(
                'native follower handoff lost its detached-tail ownership')
        if (not record.get('baseline_captured') or
                not record.get('generation_open')):
            raise BMCUError(
                'native follower handoff has no open captured feeder baseline')
        if bool(record.get('baseline_disabled', False)):
            raise BMCUError(
                'native feeder was disabled in the captured user baseline; '
                'it cannot be selected as an automatic follower')
        endpoint.native_source_preflight(
            expected_enabled=True, require_filament=True)
        enabled = False
        self._arm_u1_native_follower_commit(
            endpoint, source_device, source_channel,
            logical_tool=logical_tool)
        try:
            endpoint.set_native_feeder_enabled(True, save=True)
            enabled = True
            endpoint.release_runtime_sensor_takeover()
            endpoint.native_feeder_load(printing=True)
        except Exception:
            if enabled:
                try:
                    endpoint.set_native_feeder_enabled(False, save=True)
                except Exception:
                    logging.exception(
                        'BMCU could not restore native feeder takeover after '
                        'failed detached-tail follower load')
            try:
                endpoint.activate_runtime_sensor_takeover(force=True)
            except Exception:
                logging.exception(
                    'BMCU could not restore U1 sensor takeover after failed '
                    'native follower load')
            raise

        return self._finalize_snapmaker_tail_handoff_after_follower(
            source_device, source_channel, endpoint,
            logical_tool=logical_tool,
            reason=('native U1 follower completed stock load and flush through '
                    'the detached hotend remnant'))

    def _can_retain_selected_u1_head_for_swap(
            self, endpoint, source_device, source_channel,
            same_channel_follower=False):

        return bool(
            source_device is not None and
            endpoint.driver == 'snapmaker_u1' and
            not same_channel_follower and
            not self._ungripped_tail(
                source_device, source_channel, endpoint))

    def _rollback_incomplete_u1_load_locked(
            self, device, endpoint, channel, failed_phase,
            forward_mm=None, arrival_start_m=None, preexisting_mm=0.0):

        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False
        if getattr(self, '_klippy_disconnecting', False):
            return False
        phase = str(failed_phase or '')
        if phase not in ('POSITION_FOR_LOAD', 'ARRIVAL_SEARCH',
                         'PREPARE_BITE'):
            return False

        route_key = self._route_key(device, channel)
        device.stop_all()
        device.refresh()
        endpoint.verify_selected()
        channel_metadata = self._channel_metadata(device, channel)

        current_segment_candidates = []
        try:
            value = float(forward_mm)
            if math.isfinite(value) and value >= 0.0:
                current_segment_candidates.append(value)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            if arrival_start_m is not None:
                meter_mm = abs(float(device.status['meters'][channel]) -
                               float(arrival_start_m)) * 1000.0
                if math.isfinite(meter_mm):
                    current_segment_candidates.append(max(0.0, meter_mm))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            staged_mm = float(preexisting_mm)
        except (TypeError, ValueError, OverflowError):
            staged_mm = 0.0
        if not math.isfinite(staged_mm):
            staged_mm = 0.0
        staged_mm = max(0.0, staged_mm)
        current_mm = (max(current_segment_candidates)
                      if current_segment_candidates else 0.0)

        try:
            configured_mm = float(
                channel_metadata.get('unload_retract_mm', 850.0) or 850.0)
        except (TypeError, ValueError, OverflowError):
            configured_mm = 850.0
        if not math.isfinite(configured_mm):
            configured_mm = 850.0
        configured_mm = max(10.0, min(2000.0, configured_mm))
        fed_mm = staged_mm + current_mm
        required_mm = max(configured_mm, fed_mm)

        context = {
            'material': channel_metadata.get('material', ''),
            'channel_metadata': channel_metadata,
            'delegated_pullback': True,
            'heater_target': None,
            'reconcile_requires_park': False,
            'retain_selected_endpoint': True,
            'selected_confirmed_before_tip': True,
            'selected_confirmed_before_pullback': True,
            'same_head_select_fallback': False,
            'temperature_profile': None,
            'tip_profile': None,
            'before_pullback_started': True,
            'pullback_started': False,
            'pullback_start_m': None,
            'release_buffer_pct': int(device.status['buffer_pct'][channel]),
            'unload_assist': None,
            'endpoint_released': False,
            'park_confirmed_before_pullback': False,
            'parked_at_tip_marker': False,
            'parked_tip_tail_plan': None,
            'parked_tip_tail_ticket': None,
            'parked_tip_tail_complete': False,
            'recovery_from_incomplete_load': True,
            'expected_retract_mm_override': required_mm,
            'pullback_prior_encoder_mm': 0.0,
            'pullback_segments': [],
        }

        remaining_mm = required_mm
        rollback_error = None
        try:
            while remaining_mm > 2000.0 + 0.001:
                segment_mm = 2000.0
                self._send_device_runtime_config(
                    device, channel_retract_override=(channel, segment_mm))
                device.refresh()
                segment_start_m = float(device.status['meters'][channel])
                device.set_motion(channel, protocol.MOTION_BEFORE_PULL_BACK)
                self.reactor.pause(self.reactor.monotonic() + 0.2)
                device.refresh()
                device.set_motion(channel, protocol.MOTION_PULL_BACK)
                result = self._wait_pullback_safe(
                    device, channel, self.unload_timeout,
                    start_m=segment_start_m,
                    allow_transient_buffer_extremes=True)
                measured = float(result.get('encoder_mm', 0.0) or 0.0)
                if measured + 0.001 < segment_mm:
                    raise BMCUError(
                        'INCOMPLETE_LOAD_ROLLBACK_SHORT: %.1f mm of %.1f mm '
                        'was returned' % (measured, segment_mm))
                context['pullback_prior_encoder_mm'] += measured
                context['pullback_segments'].append(measured)
                remaining_mm = max(0.0, remaining_mm - segment_mm)

            final_mm = max(10.0, remaining_mm)
            self._send_device_runtime_config(
                device, channel_retract_override=(channel, final_mm))
            device.refresh()
            context['pullback_start_m'] = float(
                device.status['meters'][channel])
            device.set_motion(channel, protocol.MOTION_BEFORE_PULL_BACK)
            self.reactor.pause(self.reactor.monotonic() + 0.2)
            device.refresh()
            context['release_buffer_pct'] = int(
                device.status['buffer_pct'][channel])
            device.set_motion(channel, protocol.MOTION_PULL_BACK)
            context['pullback_started'] = True
            self._finish_unload_locked(
                device, endpoint, channel, context,
                release_endpoint=False, preserve_sensor_takeover=False,
                background=False)
        except Exception as exc:
            rollback_error = exc
            raise
        finally:

            try:
                self._send_device_runtime_config(device)
            except Exception:
                if rollback_error is None:
                    raise
                logging.exception(
                    'BMCU could not restore normal runtime retract policy after '
                    'failed incomplete-load rollback')

        self._uncertain_routes.discard(route_key)
        self._drop_prestage_record(route_key, release_sensor=False)
        self._u1_lease_dirty = True
        self._release_u1_persistent_hold_if_safe(
            endpoint, 'incomplete pre-BITE U1 load rolled back EMPTY')
        self._save_runtime()
        logging.warning(
            'BMCU rolled back incomplete %s Channel %d load from %s: '
            'fed %.1f mm, returned %.1f mm in %d native segment(s)',
            device.name, channel + 1, phase, fed_mm, required_mm,
            len(context['pullback_segments']) + 1)
        return True

    def load_tool(self, tool, gcmd=None, prime_handshake=False,
                  temperature_profile=None):
        if temperature_profile is None:
            temperature_profile = getattr(
                self, '_u1_pending_temperature_profile', None)
        device, channel, _endpoint = self._resolve_tool(tool)
        return self.load_channel(
            device, channel, gcmd=gcmd, tool=int(tool),
            prime_handshake=prime_handshake,
            temperature_profile=temperature_profile)

    def load_channel(self, device, channel, gcmd=None, tool=None,
                     prime_handshake=False, temperature_profile=None):
        self._require_standalone_operation('BMCU_LOAD', gcmd)
        channel = int(channel)
        if channel < 0 or channel > 3:
            raise BMCUError('BMCU channel must be within 0..3')
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise BMCUError(
                '%s Channel %d is not connected to a printer head/extruder' %
                (device.name, channel + 1))
        if tool is None:
            tool = self._tool_for_channel(device, channel)
        try:
            tool = int(tool)
        except (TypeError, ValueError):
            tool = -1
        operation_label = ('T%d' % tool if tool >= 0 else
                           '%s Channel %d' % (device.name, channel + 1))
        route_key = self._route_key(device, channel)
        background_job = self._u1_background_jobs.get(endpoint.name)
        prefetched_u1_target = bool(
            endpoint.driver == 'snapmaker_u1' and
            isinstance(background_job, dict) and
            background_job.get('foreground_head_prefetched') and
            background_job.get('target_device') is device and
            int(background_job.get('target_channel', -1)) == channel and
            int(background_job.get('target_tool', -1)) == tool and
            background_job.get('endpoint') is endpoint and
            background_job.get('state') in ('handoff', 'ready', 'partial'))

        self._validate_endpoint_for_operation(
            endpoint, require_u1_ownership=False)
        self._reconcile_u1_pending_follower_commit(endpoint)
        self._reconcile_generic_pending_follower_commit(endpoint)
        occupied = self._loaded_devices_for_endpoint(
            endpoint.name, excluded_route=route_key)
        if len(occupied) > 1:
            raise BMCUError(
                'endpoint %s has multiple loaded Channels; reconcile manually' %
                endpoint.name)
        same_channel_follower = False
        if (self._route_state(device, channel) == protocol.ROUTE_LOADED and
                not occupied):
            detached_tail = bool(
                self._u1_tail_detached_matches(
                    endpoint, device, channel) or
                self._generic_tail_detached_matches(
                    endpoint, device, channel))
            if detached_tail:
                present = getattr(device, 'status', {}).get('present')
                same_channel_follower = bool(
                    isinstance(present, (list, tuple)) and
                    channel < len(present) and present[channel])
            if same_channel_follower:

                if (endpoint.driver == 'snapmaker_u1' and
                        not self._u1_tail_sensor_boundary_cleared(
                            endpoint, device, channel)):
                    raise BMCUError(
                        'new filament was inserted into exhausted Channel %d '
                        'before the old tail cleared the Snapmaker head sensor; '
                        'remove it and retry after the printer pauses at runout' %
                        (channel + 1))
            else:

                self._lock(device, endpoint, 'SELECT LOADED %s' % operation_label, channel=channel)
                phase = 'SELECT_LOADED_ENDPOINT'
                self._set_phase(device, phase)
                try:
                    self._arm_u1_persistent_hold(
                        endpoint, device, channel, 'select loaded %s' % operation_label)
                    self._validate_endpoint_for_operation(endpoint)
                    endpoint.suspend_managed_sensors()
                    endpoint.select()
                    endpoint.verify_selected()
                    device.set_motion(channel, protocol.MOTION_ON_USE)
                    endpoint.activate_runtime_sensor_takeover()
                    if hasattr(endpoint, 'sync_active_filament'):
                        endpoint.sync_active_filament(
                            self._channel_metadata(device, channel))
                    if tool >= 0:
                        self.loaded_tools[route_key] = tool
                        self.active_tool = tool
                        self._mark_print_route_loaded(
                            device, channel, tool)
                    else:
                        self.loaded_tools.pop(route_key, None)
                    self._save_runtime()
                    if gcmd:
                        gcmd.respond_info(
                            'BMCU selected already-loaded %s Channel %d on %s' %
                            (device.name, channel + 1, endpoint.name))
                except Exception as exc:
                    self._stop_on_failure(
                        device, endpoint, exc, phase, channel)
                    if gcmd:
                        raise gcmd.error(str(exc))
                    raise
                finally:
                    self._unlock(device, endpoint)
                if (detached_tail and tool >= 0 and
                        endpoint.driver == 'snapmaker_u1'):
                    self._rearm_snapmaker_detached_tail_monitor(
                        device, channel, tool, endpoint)
                if endpoint.driver == 'snapmaker_u1':
                    self._mark_u1_head_prepared(
                        endpoint.get('head_index', -1),
                        source='already-loaded BMCU route selected during print')

                return False
        source_device, source_channel = (
            (device, channel) if same_channel_follower else
            (occupied[0] if occupied else (None, None)))
        retain_selected_u1_head = (
            self._can_retain_selected_u1_head_for_swap(
                endpoint, source_device, source_channel,
                same_channel_follower=same_channel_follower))
        if source_device is not None:

            if endpoint.driver == 'snapmaker_u1':
                self._mark_u1_head_prepared(
                    endpoint.get('head_index', -1),
                    source='loaded BMCU source present before tool change')
            self._preempt_refill_for_toolchange(
                source_device, source_channel, endpoint)
        multi_device_switch = (
            source_device is not None and source_device.name != device.name)
        if multi_device_switch:
            self._lock_refill(source_device, device, endpoint, 'TOOL_SWITCH %s' % operation_label)
        else:
            self._lock(device, endpoint, 'LOAD %s' % operation_label, channel=channel)
        phase = 'PRECHECK'
        failure_device = device
        failure_channel = channel
        projection_snapshot = None
        path_calibration_sample = None
        path_calibration = None
        arrival = {}
        arrival_search_start_m = None
        arrival_preexisting_mm = 0.0
        target_physically_captured = False
        target_motion_started = False
        target_recovery_emptied = False
        release_new_u1_hold = False
        detached_source_handoff = False
        u1_hold_was_active = bool(
            endpoint.driver == 'snapmaker_u1' and
            self._u1_ownership_record(endpoint.name).get('persistent_hold'))
        self._set_phase(device, phase)
        load_completed = False
        load_temperature_finalized = False
        try:
            if (endpoint.driver == 'snapmaker_u1' and
                    isinstance(background_job, dict) and
                    background_job.get('target_device') is device and
                    int(background_job.get('target_channel', -1)) == channel):
                if (background_job.get('worker_scheduled') and
                        not background_job.get('worker_finished')):
                    raise BMCUError('U1 background worker has not released the route')
                background_job['foreground_consuming'] = True
            staged_record = self.prestaged.get(route_key)

            staged_for_target = bool(
                tool >= 0 and staged_record and
                staged_record.get('endpoint') == endpoint.name)
            if same_channel_follower:
                device.mark_unloaded(channel)
                device.refresh()
                if self._route_states_from_status(
                        device.status)[channel] != protocol.ROUTE_EMPTY:
                    raise BMCUError(
                        'same-Channel follower could not release the exhausted '
                        'upstream route before load')
            self._check_automatic_ready(
                device, channel, allow_uncertain=staged_for_target,
                allow_detached_handoff=same_channel_follower)
            self._arm_u1_persistent_hold(
                endpoint, device, channel, 'load %s' % operation_label)

            self._validate_endpoint_for_operation(endpoint)
            if source_device is not None:

                phase = 'UNLOAD_SOURCE'
                failure_device = source_device
                failure_channel = source_channel
                self._set_phase(source_device, phase)
                target_motion_started = True
                if self._ungripped_tail(
                        source_device, source_channel, endpoint):
                    if endpoint.driver == 'snapmaker_u1':
                        if same_channel_follower:
                            if not self._u1_tail_sensor_boundary_cleared(
                                    endpoint, device, channel):
                                raise BMCUError(
                                    'same-Channel follower cannot start until '
                                    'the old tail clears the Snapmaker head sensor')
                        else:
                            job = self._u1_background_jobs.get(endpoint.name)
                            source_prepared = bool(
                                isinstance(job, dict) and
                                job.get('source_tail_prepared') and
                                job.get('source_device') is source_device and
                                int(job.get('source_channel', -1)) ==
                                int(source_channel))
                            if not source_prepared:
                                self._prepare_snapmaker_ungripped_tail_handoff_locked(
                                    source_device, endpoint, source_channel,
                                    temperature_profile=(
                                        self._u1_active_temperature_profile()),
                                    restore_heater=False)
                    else:
                        self._prepare_generic_ungripped_tail_handoff_locked(
                            source_device, endpoint, source_channel)
                    detached_source_handoff = True
                else:
                    self._unload_locked(
                        source_device, endpoint, source_channel,
                        release_endpoint=False,
                        preserve_sensor_takeover=True,
                        retain_selected_endpoint=retain_selected_u1_head,
                        temperature_profile=self._u1_active_temperature_profile(),
                        reason='toolchange')
                failure_device = device
                failure_channel = channel
                phase = 'PRECHECK_TARGET'
                self._set_phase(device, phase)
            target_state = self._route_state(device, channel)
            if target_state == protocol.ROUTE_UNCERTAIN and not staged_for_target:
                raise BMCUError(
                    '%s Channel %d route is UNCERTAIN' %
                    (device.name, channel + 1))

            phase = 'SELECT_ENDPOINT'
            self._set_phase(device, phase)
            endpoint.suspend_managed_sensors()
            if retain_selected_u1_head:

                endpoint.verify_selected()
                logging.info(
                    'BMCU same-Head swap retained selected %s between Channels %d and %d',
                    endpoint.name, source_channel + 1, channel + 1)
            elif prefetched_u1_target:
                endpoint.verify_selected()
                logging.info(
                    'BMCU reused target Head %s selected during source pullback',
                    endpoint.name)
            else:
                endpoint.select()
                endpoint.verify_selected()
            if endpoint.driver == 'snapmaker_u1':
                staged = self.prestaged.get(route_key)
                if isinstance(staged, dict):
                    staged['target_selected'] = True
            capture_projection = getattr(
                endpoint, 'capture_active_filament_state', None)
            if callable(capture_projection):
                projection_snapshot = capture_projection()
            channel_metadata = self._channel_metadata(device, channel)
            material = channel_metadata.get('material', '')
            load_temperature_profile = self._u1_load_temperature_profile(
                endpoint, temperature_profile, material=material)
            logging.info(
                'BMCU load source: %s Channel %d -> %s; material=%s '
                'vendor=%s subtype=%s logical_tool=%s',
                device.name, channel + 1, endpoint.name,
                material or 'Unknown',
                channel_metadata.get('vendor', '') or 'generic',
                channel_metadata.get('subtype', '') or
                channel_metadata.get('profile_id', '') or 'generic',
                tool if tool >= 0 else 'manual')
            if hasattr(endpoint, 'sync_active_filament'):
                endpoint.sync_active_filament(channel_metadata)
            prepare_load_kwargs = {}
            if endpoint.driver == 'snapmaker_u1':

                prepare_load_kwargs['discard_position_prepared'] = bool(
                    retain_selected_u1_head or
                    (prefetched_u1_target and background_job.get(
                        'foreground_discard_position_prepared')))
            reuse_prepared_context = False
            if prefetched_u1_target:
                verifier = getattr(
                    endpoint, 'prepared_load_context_matches', None)
                reuse_prepared_context = bool(
                    callable(verifier) and
                    verifier(material, load_temperature_profile))
            if reuse_prepared_context:
                logging.info(
                    'BMCU reused nonblocking load temperature prepared during '
                    'source pullback for T%d on %s', tool, endpoint.name)
            else:
                self._endpoint_temperature_call(
                    endpoint, 'prepare_load', material, load_temperature_profile,
                    **prepare_load_kwargs)

            phase = 'ARRIVAL_SEARCH'
            self._set_phase(device, phase)
            is_prestaged = staged_for_target
            if is_prestaged:
                try:
                    arrival_preexisting_mm = max(
                        0.0, float(staged_record.get('distance_mm', 0.0) or 0.0))
                except (TypeError, ValueError, OverflowError):
                    arrival_preexisting_mm = 0.0
            entry_before = endpoint.sensor_detected('entry_sensor')
            configured_entry_sensor = str(
                endpoint.get('entry_sensor', '') or '').strip()
            if configured_entry_sensor and is_prestaged:
                if self._u1_has_authoritative_entry_sensor(endpoint):
                    entry_before = endpoint.require_entry_sensor_snapshot().get(
                        'physical_detected')
                staged_record['at_entry'] = entry_before is True
                staged_record['partial'] = entry_before is not True
                if entry_before is True:
                    staged_record['result'] = dict(
                        staged_record.get('result', {}),
                        ok=True, sensor_triggered=True,
                        controller_contact=False,
                        reason='owned_prestage_at_entry')
            partial_prestage = bool(
                is_prestaged and staged_record.get('partial') and
                not staged_record.get('at_entry'))
            staged_at_entry = bool(
                is_prestaged and staged_record.get('at_entry'))
            if partial_prestage and not staged_at_entry:
                max_route = float(endpoint.get(
                    'max_route_mm', self.max_route_mm) or self.max_route_mm)
                try:
                    staged_distance = float(
                        staged_record.get('distance_mm', 0.0) or 0.0)
                except (TypeError, ValueError, OverflowError):
                    staged_distance = 0.0
                if not math.isfinite(staged_distance):
                    staged_distance = 0.0
                remaining = max(50.0, max_route - max(0.0, staged_distance))
                final_search = float(endpoint.get(
                    'final_search_mm', 250.0) or 250.0)
                maximum_mm = min(max_route, max(final_search, remaining))
            else:
                maximum_mm = float(endpoint.get(
                    'final_search_mm' if is_prestaged else 'max_route_mm',
                    250.0 if is_prestaged else self.max_route_mm) or
                    (250.0 if is_prestaged else self.max_route_mm))
            contact_pct = self._device_loading_handoff_pct(
                device, endpoint)
            if partial_prestage and not staged_at_entry:
                if configured_entry_sensor:
                    logging.info(
                        'BMCU continuing partial T%d prestage on selected %s: '
                        'already %.1f mm, remaining entry-sensor '
                        'search %.1f mm',
                        int(tool), endpoint.name, max(0.0, staged_distance),
                        float(maximum_mm))
                else:
                    try:
                        partial_buffer_pct = int(
                            staged_record.get('buffer_pct', contact_pct))
                    except (TypeError, ValueError, OverflowError):
                        partial_buffer_pct = contact_pct
                    contact_pct = min(
                        94, max(contact_pct, partial_buffer_pct + 8))
                    logging.info(
                        'BMCU continuing partial T%d prestage on selected %s: '
                        'already %.1f mm, remaining search %.1f mm, controller '
                        'contact target %d%%',
                        int(tool), endpoint.name, max(0.0, staged_distance),
                        float(maximum_mm), int(contact_pct))
            timeout_s = float(endpoint.get(
                'contact_timeout', self.contact_timeout) or self.contact_timeout)
            timeout_s = self._effective_feed_timeout(
                device, maximum_mm, timeout_s)

            detached_follower_at_entry = False
            prepare_load_position = getattr(
                endpoint, 'prepare_load_position', None)
            if staged_at_entry or detached_follower_at_entry:

                if callable(prepare_load_position):
                    phase = 'POSITION_FOR_LOAD'
                    self._set_phase(device, phase)
                    prepare_load_position(material, load_temperature_profile)
                    phase = 'ARRIVAL_SEARCH'
                    self._set_phase(device, phase)
                arrival = (copy.deepcopy(staged_record.get('result', {}))
                           if staged_at_entry else {})
                arrival.update({
                    'ok': True,
                    'reason': (
                        'background_partial_reached_entry'
                        if partial_prestage and
                           not staged_record.get('at_entry') else
                        'background_prestaged_at_entry'
                        if staged_at_entry else
                        'detached_follower_already_at_entry'),
                })
            else:
                if entry_before is True and not is_prestaged:
                    raise BMCUError(
                        'RESIDUAL_FILAMENT: entry sensor is already active before load')
                target_motion_started = True
                if partial_prestage:

                    device.set_motion(channel, protocol.MOTION_IDLE)
                    device.refresh()
                    if int(device.status['motion'][channel]) != (
                            protocol.MOTION_IDLE):
                        raise BMCUError(
                            'U1_PRECHARGE_NOT_IDLE before selected-Head '
                            'arrival search')
                    logging.info(
                        'BMCU continuing partial T%d prestage on selected %s '
                        'with a separate SEND_OUT arrival search',
                        int(tool), endpoint.name)

                device.refresh()
                arrival_search_start_m = float(
                    device.status['meters'][channel])
                op_id, arrival_policy = (
                    self._start_endpoint_arrival_operation(
                        device, channel, endpoint, maximum_mm, contact_pct,
                        timeout_s, parked_precharge=False))

                if callable(prepare_load_position):
                    phase = 'POSITION_FOR_LOAD'
                    self._set_phase(device, phase)
                    prepare_load_position(material, load_temperature_profile)
                    phase = 'ARRIVAL_SEARCH'
                    self._set_phase(device, phase)
                if arrival_policy.get('sensor_authoritative'):
                    arrival = self._wait_sensor_arrival(
                        device, channel, endpoint, op_id, arrival_policy,
                        maximum_mm, poll_interval=0.050)
                else:
                    arrival = self._wait_feed_operation(
                        device, op_id, timeout_s + 2.0, endpoint,
                        'entry_sensor')
                arrival = self._resolve_endpoint_arrival_result(
                    device, channel, endpoint, arrival, arrival_policy,
                    timeout_s, allow_partial=False)
                if self.debug_enabled:
                    snapshot = endpoint.entry_debug_snapshot()
                    if snapshot.get('detailed'):
                        self._debug_log(
                            'U1 %s SEND_OUT result reason=%s ok=%d measured=%.1fmm '
                            'sensor_event=%d contact=%d buffer=%s%% motion=%s '
                            'entry_phase=%s source=%s enabled=%s public=%s callback=%s',
                            endpoint.name, arrival.get('reason'),
                            1 if arrival.get('ok') else 0,
                            float(arrival.get('measured_mm', 0.0) or 0.0),
                            1 if arrival.get('sensor_triggered') else 0,
                            1 if arrival.get('controller_contact') else 0,
                            device.status['buffer_pct'][channel],
                            device.status['motion'][channel],
                            snapshot.get('physical_detected'), snapshot.get('source'),
                            snapshot.get('enabled'), snapshot.get('public_detected'),
                            snapshot.get('callback_detected'))
                    else:
                        self._debug_log(
                            'BMCU generic %s SEND_OUT result reason=%s ok=%d '
                            'measured=%.1fmm contact=%d buffer=%s%% motion=%s '
                            'entry_sensor=%s',
                            endpoint.name, arrival.get('reason'),
                            1 if arrival.get('ok') else 0,
                            float(arrival.get('measured_mm', 0.0) or 0.0),
                            1 if arrival.get('controller_contact') else 0,
                            device.status['buffer_pct'][channel],
                            device.status['motion'][channel],
                            snapshot.get('detected'))
                if arrival.get('cancel_requested'):
                    raise BMCUError('U1_SEND_OUT_CANCELLED')
                if partial_prestage:
                    arrival['continued_from_neutral_precharge'] = True
                if not arrival.get('ok'):
                    raise BMCUError('ENDPOINT_NOT_REACHED: %s after %.1f mm' %
                                    (arrival.get('reason'),
                                     arrival.get('measured_mm', 0.0)))
                if (configured_entry_sensor and
                        not arrival.get('sensor_triggered')):
                    raise BMCUError(
                        'ENDPOINT_NOT_REACHED: %s stopped after %.1f mm at '
                        '%d%% buffer without entry-sensor confirmation' %
                        (arrival.get('reason'),
                         float(arrival.get('measured_mm', 0.0) or 0.0),
                         int(device.status['buffer_pct'][channel])))
                if partial_prestage:
                    try:
                        continuation_mm = float(
                            arrival.get('measured_mm', 0.0) or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        continuation_mm = 0.0
                    if math.isfinite(continuation_mm):
                        staged_record['distance_mm'] = min(
                            max_route,
                            max(0.0, staged_distance) +
                            max(0.0, continuation_mm))
                    staged_record['buffer_pct'] = int(
                        device.status['buffer_pct'][channel])
                    staged_record['result'] = dict(arrival)
                if (not self._u1_has_authoritative_entry_sensor(endpoint) and
                        configured_entry_sensor and
                        endpoint.sensor_detected('entry_sensor') is not True):
                    raise BMCUError(
                        'ENDPOINT_NOT_REACHED: %s stopped after %.1f mm at '
                        '%d%% buffer without configured entry-sensor confirmation' %
                        (arrival.get('reason'),
                         float(arrival.get('measured_mm', 0.0) or 0.0),
                         int(device.status['buffer_pct'][channel])))

            generic_contract = (endpoint.driver == 'generic_single_extruder')
            if (not generic_contract and not is_prestaged and
                    not detached_source_handoff):
                path_calibration_sample = self._measure_path_calibration(
                    device, channel, endpoint, arrival)
            elif not generic_contract:
                self._clear_path_learning_observation(device, channel)
            phase = ('TOOLHEAD_PREPARATION' if generic_contract
                     else 'PREPARE_BITE')
            self._set_phase(device, phase)
            u1_pressure_evidence = None
            if self.debug_enabled and self._u1_has_authoritative_entry_sensor(endpoint):
                snapshot = endpoint.entry_debug_snapshot()
                self._debug_log(
                    'U1 %s PREPARE_BITE begin buffer=%s%% motion=%s '
                    'entry_phase=%s source=%s enabled=%s',
                    endpoint.name, device.status['buffer_pct'][channel],
                    device.status['motion'][channel],
                    snapshot.get('physical_detected'), snapshot.get('source'),
                    snapshot.get('enabled'))
            if self._u1_has_authoritative_entry_sensor(endpoint):

                device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
            if not generic_contract:
                self._endpoint_temperature_call(
                    endpoint, 'ensure_bite_ready', material,
                    load_temperature_profile)
            if self._u1_has_authoritative_entry_sensor(endpoint):
                u1_pressure_evidence = self._wait_u1_firmware_pressure_hold(
                    device, channel, endpoint, timeout_s,
                    arrival_evidence=arrival)
            else:
                device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)

            if not generic_contract:
                phase = 'BITE'
                self._set_phase(device, phase)
                if endpoint.driver == 'snapmaker_u1':
                    self._drop_prestage_record(route_key, release_sensor=False)
            bite_ok = False
            bite_evidence = {}

            confirmed_head_advance_mm = 0.0
            if detached_source_handoff:
                if generic_contract:

                    endpoint.prepare_toolhead_for_use(material, reason='refill')
                    path_calibration_sample = self._measure_path_calibration(
                        device, channel, endpoint, arrival)
                    bite_ok = True
                    bite_evidence = {
                        'confirmation': 'printer_toolhead_prepare_macro',
                        'reason': 'refill',
                        'post_gears': endpoint.sensor_detected(
                            'post_gears_sensor'),
                    }
                else:

                    handoff_started_in_print = self._print_state() in (
                        'printing', 'paused', 'pause')
                    endpoint.record_load_coil_evidence(
                        coil_path_baseline=endpoint.capture_signal())
                    bite_evidence = (
                        self._capture_snapmaker_detached_tail_with_follower(
                            source_device, source_channel, device, channel,
                            endpoint, material=material,
                            cancel_check=lambda: bool(
                                handoff_started_in_print and
                                self._print_state() not in
                                ('printing', 'paused', 'pause'))))
                    bite_ok = bool(
                        isinstance(bite_evidence, dict) and
                        bite_evidence.get('captured'))
                    if bite_ok:
                        try:
                            confirmed_head_advance_mm += max(
                                0.0, float(
                                    bite_evidence.get('extruded_mm', 0.0)))
                        except (TypeError, ValueError, OverflowError):
                            pass
            else:
                if self._u1_has_authoritative_entry_sensor(endpoint):

                    native_signal_start = endpoint.capture_signal()
                    endpoint.record_load_coil_evidence(
                        coil_path_baseline=native_signal_start)
                    endpoint.extrude(self.bite_mm, self.bite_feed)
                    entry_after_bite = endpoint.entry_sensor_snapshot()
                    native_signal_delta = None
                    try:
                        buffer_after_bite = int(
                            device.status['buffer_pct'][channel])
                    except (TypeError, ValueError, KeyError, IndexError):
                        buffer_after_bite = -1
                    arrival_confirmed = bool(
                        arrival.get('sensor_triggered'))
                    bite_evidence = {
                        'attempt': 1,
                        'confirmation': 'u1_arrival_and_firmware_pressure',
                        'entry_sensor_phase_diagnostic': copy.deepcopy(
                            entry_after_bite),
                        'buffer_pct': buffer_after_bite,
                        'pressure_hold': copy.deepcopy(u1_pressure_evidence),
                        'arrival_motion': bool(arrival.get('sensor_triggered')),
                        'arrival_contact': bool(arrival.get('controller_contact')),
                        'native_signal_delta': native_signal_delta,
                    }

                    bite_ok = bool(
                        arrival_confirmed and
                        isinstance(u1_pressure_evidence, dict))
                    if bite_ok:
                        confirmed_head_advance_mm += float(self.bite_mm)
                elif generic_contract:
                    endpoint.prepare_toolhead_for_use(
                        material, reason=('toolchange' if source_device is not None else 'load'))
                    path_calibration_sample = self._measure_path_calibration(
                        device, channel, endpoint, arrival)
                    bite_ok = True
                    bite_evidence = {
                        'confirmation': 'printer_toolhead_load_macro',
                        'post_gears': endpoint.sensor_detected('post_gears_sensor'),
                    }
                else:
                    for attempt in range(self.bite_retries):
                        before_m = device.status['meters'][channel]
                        before_buffer = device.status['buffer_pct'][channel]
                        native_signal_start = endpoint.capture_signal()
                        endpoint.extrude(self.bite_mm, self.bite_feed)
                        device.refresh()
                        moved_mm = abs(
                            device.status['meters'][channel] - before_m) * 1000.0
                        buffer_drop = (
                            before_buffer - device.status['buffer_pct'][channel])
                        post_gears = endpoint.sensor_detected(
                            'post_gears_sensor')
                        native_signal_delta = endpoint.capture_signal_delta(
                            native_signal_start, material)
                        bite_evidence = {
                            'attempt': attempt + 1, 'encoder_mm': moved_mm,
                            'buffer_drop': buffer_drop,
                            'post_gears': post_gears,
                            'native_signal_delta': native_signal_delta,
                        }
                        if (post_gears is True or
                                moved_mm >=
                                self.bite_mm * self.bite_encoder_ratio or
                                buffer_drop >= self.bite_buffer_delta or
                                endpoint.capture_signal_ok(
                                    native_signal_delta, material)):
                            bite_ok = True
                            confirmed_head_advance_mm += float(self.bite_mm)
                            break
                        if self.retry_retract_mm > 0:
                            endpoint.extrude(
                                -self.retry_retract_mm, self.bite_feed)
                            device.set_motion(
                                channel, protocol.MOTION_SEND_OUT)
                            self.reactor.pause(
                                self.reactor.monotonic() + 0.25)
                            device.set_motion(
                                channel, protocol.MOTION_BEFORE_ON_USE)
            if self.debug_enabled:
                self._debug_log(
                    'U1 %s BITE result ok=%d evidence=%s', endpoint.name,
                    1 if bite_ok else 0,
                    json.dumps(bite_evidence, sort_keys=True, default=str))
            if not bite_ok:
                raise BMCUError(
                    'BITE_FAILED: %s' %
                    json.dumps(bite_evidence, sort_keys=True))
            if generic_contract:
                phase = 'TOOLHEAD_PREPARATION'
                self._set_phase(device, phase)
                entry_sensor_after_capture = endpoint.sensor_detected('entry_sensor')
                post_gears = endpoint.sensor_detected('post_gears_sensor')
                if str(endpoint.get('post_gears_sensor', '') or '').strip() and post_gears is False:
                    raise BMCUError(
                        'TOOLHEAD_LOAD_FAILED: configured post-gears sensor is not active after printer load macro')
                capture_moved = 0.0
                capture_mm = 0.0
                native_capture_delta = None
                target_physically_captured = True
            else:
                phase = 'CAPTURE'
                self._set_phase(device, phase)
                before_capture = device.status['meters'][channel]
                u1_readonly_path = self._u1_has_authoritative_entry_sensor(endpoint)
                capture_mm = float(self.capture_mm)
                native_capture_start = (
                    None if u1_readonly_path else endpoint.capture_signal())
                endpoint.extrude(capture_mm, self.capture_feed)
                entry_sensor_after_capture = (
                    endpoint.entry_sensor_snapshot()
                    if u1_readonly_path else
                    endpoint.sensor_detected('entry_sensor'))
                post_gears = endpoint.sensor_detected('post_gears_sensor')
                native_capture_end = (
                    None if u1_readonly_path else endpoint.capture_signal())
                native_capture_delta = None
                if native_capture_start is not None and native_capture_end is not None:
                    native_capture_delta = abs(
                        float(native_capture_end) - float(native_capture_start))
                if u1_readonly_path:

                    capture_moved = 0.0
                    if (not arrival.get('sensor_triggered') or
                            not isinstance(u1_pressure_evidence, dict)):
                        raise BMCUError(
                            'CAPTURE_FAILED: Snapmaker route lacks prior arrival and '
                            'firmware-pressure evidence')
                    logging.info(
                        'BMCU Snapmaker %s fixed capture accepted: arrival_ok=1 '
                        'buffer=%d%% phase=%s coil_boundary_query=skipped',
                        endpoint.name,
                        int(device.status['buffer_pct'][channel]),
                        entry_sensor_after_capture.get('physical_detected')
                        if isinstance(entry_sensor_after_capture, dict) else None)
                else:
                    device.refresh()
                    capture_moved = abs(
                        device.status['meters'][channel] - before_capture) * 1000.0
                    if (post_gears is not True and
                            capture_moved <
                            capture_mm * self.capture_encoder_ratio and
                            not endpoint.capture_signal_ok(
                                native_capture_delta, material)):
                        raise BMCUError(
                            'CAPTURE_FAILED: encoder moved %.2f mm; entry_sensor=%s '
                            'post_gears=%s native_signal_delta=%s' %
                            (capture_moved, entry_sensor_after_capture, post_gears,
                             native_capture_delta))
                if self.debug_enabled and endpoint.driver == 'snapmaker_u1':
                    self._debug_log(
                        'U1 %s CAPTURE completed buffer=%s%% entry=%s '
                        'coil_boundary_query=skipped confirmed_head_before=%.1fmm',
                        endpoint.name, device.status['buffer_pct'][channel],
                        (entry_sensor_after_capture.get('physical_detected')
                         if isinstance(entry_sensor_after_capture, dict)
                         else entry_sensor_after_capture),
                        confirmed_head_advance_mm)
                target_physically_captured = True
                confirmed_head_advance_mm += float(capture_mm)
                record_load_path_advance = getattr(
                    endpoint, 'record_load_path_advance', None)
                if callable(record_load_path_advance):
                    record_load_path_advance(confirmed_head_advance_mm)

            if detached_source_handoff:

                phase = 'COMMIT_FOLLOWER_ROUTE'
                self._set_phase(device, phase)
                if endpoint.driver == 'snapmaker_u1':
                    self._arm_u1_follower_commit(
                        endpoint, source_device, source_channel,
                        device, channel, logical_tool=tool)
                else:
                    self._arm_generic_follower_commit(
                        endpoint, source_device, source_channel,
                        device, channel, logical_tool=tool)
                device.mark_loaded(channel)
                device.refresh()
                raw_target = self._route_states_from_status(
                    device.status)[channel]
                if raw_target != protocol.ROUTE_LOADED:
                    raise BMCUError(
                        '%s Channel %d did not commit LOADED after detached-tail '
                        'follower capture' % (device.name, channel + 1))
                if endpoint.driver == 'snapmaker_u1':
                    self._finalize_snapmaker_tail_handoff_after_follower(
                        source_device, source_channel, endpoint,
                        follower_device=device, follower_channel=channel,
                        logical_tool=tool,
                        reason=('new BMCU follower physically captured and '
                                'committed LOADED before final prime'))
                else:
                    self._finalize_generic_tail_handoff_after_follower(
                        source_device, source_channel, endpoint,
                        follower_device=device, follower_channel=channel,
                        reason=('generic follower physically captured and '
                                'committed LOADED before final prime'))

            phase = ('BEFORE_ON_USE'
                     if endpoint.driver == 'snapmaker_u1' else 'TOOLHEAD_PREPARATION')
            self._set_phase(device, phase)
            load_ready_evidence = self._endpoint_temperature_call(
                endpoint, 'load_ready', material, load_temperature_profile)
            if self.debug_enabled and endpoint.driver == 'snapmaker_u1':
                self._debug_log(
                    'U1 %s BEFORE_ON_USE passive evidence accepted: %s',
                    endpoint.name,
                    json.dumps(load_ready_evidence or {}, sort_keys=True,
                               default=str))
            self._endpoint_temperature_call(
                endpoint, 'prime', material, load_temperature_profile)
            if self.debug_enabled and endpoint.driver == 'snapmaker_u1':
                self._debug_log(
                    'U1 %s prime completed; switching firmware motion '
                    'BEFORE_ON_USE -> ON_USE', endpoint.name)
            device.set_motion(channel, protocol.MOTION_ON_USE)
            endpoint.activate_runtime_sensor_takeover()
            if hasattr(endpoint, 'sync_active_filament'):
                endpoint.sync_active_filament(
                    self._channel_metadata(device, channel))
            self._clear_u1_tail_detached(
                endpoint, device, channel,
                'new BMCU source physically captured and loaded')
            if tool >= 0:
                self.loaded_tools[route_key] = tool
                self.active_tool = tool
                self._mark_print_route_loaded(device, channel, tool)
            else:
                self.loaded_tools.pop(route_key, None)
            self._save_runtime()
            self.prestaged.pop(route_key, None)
            if path_calibration_sample is not None:
                try:
                    path_calibration = self._commit_path_calibration(
                        device, channel, endpoint, path_calibration_sample,
                        reason='complete load, capture and prime succeeded')
                except Exception as calibration_exc:
                    logging.exception(
                        'BMCU could not persist route-length calibration for %s Channel %d',
                        device.name, channel + 1)
                    path_calibration = {
                        'error': str(calibration_exc)[:160],
                        'sample_mm': path_calibration_sample.get('sample_mm'),
                        'measurement': path_calibration_sample.get('measurement'),
                    }
            finish_load_temperature = getattr(
                endpoint, 'finish_load_temperature', None)
            if callable(finish_load_temperature):
                phase = 'RESTORE_WORKING_TEMPERATURE'
                self._set_phase(device, phase)
                if not finish_load_temperature(success=True):
                    raise BMCUError(
                        'Snapmaker Head %d did not restore its working '
                        'temperature after BMCU load' % (endpoint._head() + 1))
                load_temperature_finalized = True
            finish_load_position = getattr(
                endpoint, 'finish_load_position', None)
            if callable(finish_load_position):
                phase = 'SAFE_LOAD_EGRESS'
                self._set_phase(device, phase)
                if not finish_load_position():
                    raise BMCUError(
                        'Snapmaker Head %d did not reach the stock XY idle '
                        'position after BMCU load cleaning' %
                        (endpoint._head() + 1))
            if (prime_handshake and tool >= 0 and
                    endpoint.driver == 'snapmaker_u1'):

                self._u1_preextrude_primed_tools[tool] = (
                    self.reactor.monotonic())
            load_completed = True
            if endpoint.driver == 'snapmaker_u1':
                self._mark_u1_head_prepared(
                    endpoint.get('head_index', -1),
                    source=('BMCU T%d' % tool if tool >= 0 else
                            '%s Channel %d' % (device.name, channel + 1)))
            self.last_diagnostic = {
                'operation': 'load', 'tool': tool, 'device': device.name,
                'channel': channel, 'endpoint': endpoint.name,
                'arrival': arrival, 'bite': bite_evidence,
                'path_calibration': copy.deepcopy(path_calibration),
                'capture_encoder_mm': capture_moved,
                'capture_entry_sensor': entry_sensor_after_capture,
                'capture_native_signal_delta': native_capture_delta,
                'u1_pressure_hold': copy.deepcopy(u1_pressure_evidence),
                'confirmed_head_advance_mm': confirmed_head_advance_mm,
                'nozzle_path': {
                    'stock_total_mm': endpoint.config.get(
                        '_u1_last_nozzle_target_mm'),
                    'remaining_mm': endpoint.config.get(
                        '_u1_last_nozzle_search_mm'),
                    'coil_delta': endpoint.config.get(
                        '_u1_last_coil_delta'),
                    'coil_capture_delta': endpoint.config.get(
                        '_u1_last_coil_capture_delta'),
                    'coil_final_delta': endpoint.config.get(
                        '_u1_last_coil_final_delta'),
                    'coil_path_delta': endpoint.config.get(
                        '_u1_last_coil_path_delta'),
                    'coil_confirmed': endpoint.config.get(
                        '_u1_last_coil_confirmed'),
                } if endpoint.driver == 'snapmaker_u1' else {},
                'temperature_profile': copy.deepcopy(load_temperature_profile),
                'load_temperature': {
                    'finalized': bool(load_temperature_finalized),
                    'head': int(endpoint.get('head_index', -1)),
                },
            }
            if gcmd:
                if tool >= 0:
                    gcmd.respond_info(
                        'BMCU loaded T%d from %s Channel %d into %s' %
                        (tool, device.name, channel + 1, endpoint.name))
                else:
                    gcmd.respond_info(
                        'BMCU loaded %s Channel %d into %s' %
                        (device.name, channel + 1, endpoint.name))
        except Exception as exc:
            cancel_during_load = bool(
                self._u1_cancel_requested or
                (isinstance(arrival, dict) and
                 arrival.get('cancel_requested')))
            if (endpoint.driver == 'snapmaker_u1' and
                    not cancel_during_load and
                    (target_motion_started or arrival_preexisting_mm > 0.0) and
                    not target_physically_captured and
                    not detached_source_handoff and
                    phase in ('POSITION_FOR_LOAD', 'ARRIVAL_SEARCH',
                              'PREPARE_BITE')):
                try:
                    target_recovery_emptied = (
                        self._rollback_incomplete_u1_load_locked(
                            device, endpoint, channel, phase,
                            forward_mm=(arrival.get('measured_mm')
                                        if isinstance(arrival, dict) else None),
                            arrival_start_m=arrival_search_start_m,
                            preexisting_mm=arrival_preexisting_mm))
                except Exception as recovery_exc:

                    try:
                        device.stop_all()
                    except Exception:
                        pass
                    logging.exception(
                        'BMCU incomplete pre-BITE U1 load rollback failed for '
                        '%s Channel %d after %s: %s',
                        device.name, channel + 1, phase, recovery_exc)
            if (endpoint.driver == 'snapmaker_u1' and
                    not u1_hold_was_active and not target_motion_started):
                release_new_u1_hold = True
            if (projection_snapshot is not None and
                    not target_physically_captured and
                    not target_recovery_emptied):
                restore_projection = getattr(
                    endpoint, 'restore_active_filament_state', None)
                if callable(restore_projection):
                    try:
                        restore_projection(projection_snapshot)
                    except Exception:
                        logging.exception(
                            'BMCU could not roll back Endpoint %s material projection',
                            endpoint.name)
            preserve_target_hold = bool(
                getattr(exc, 'preserve_bmcu_hold', False))
            if (source_device is not None and
                    source_device is not failure_device and
                    not preserve_target_hold):
                try:
                    source_device.stop_all()
                except Exception:
                    logging.exception(
                        'BMCU could not stop source Channel after tool-switch failure')
            self._stop_on_failure(
                failure_device, endpoint, exc, phase, failure_channel,
                pause_print=not cancel_during_load)
            if gcmd:
                raise gcmd.error(str(exc))
            raise
        finally:
            finish_load_temperature = getattr(
                endpoint, 'finish_load_temperature', None)
            if (not load_temperature_finalized and
                    callable(finish_load_temperature)):
                try:
                    if not finish_load_temperature(success=False):
                        logging.error(
                            'BMCU could not restore the pre-load temperature '
                            'target on %s', endpoint.name)
                except Exception:
                    logging.exception(
                        'BMCU load temperature cleanup failed on %s',
                        endpoint.name)
            if multi_device_switch:
                self._unlock_refill(source_device, device, endpoint)
            else:
                self._unlock(device, endpoint)
            if release_new_u1_hold:
                try:
                    self._release_u1_persistent_hold_if_safe(
                        endpoint, 'load preflight failed before filament motion')
                except Exception:
                    logging.exception(
                        'BMCU could not release U1 ownership after load preflight failure')

        return True

    def _start_delegated_u1_pullback_locked(
            self, device, endpoint, channel, context, require_park=False,
            cancel_check=None, background=False):

        if context.get('pullback_started'):
            return context
        if callable(cancel_check) and cancel_check():
            raise BMCUError('BMCU pullback was cancelled before start')
        retain_selected = bool(
            endpoint.driver == 'snapmaker_u1' and
            context.get('delegated_pullback') and
            context.get('retain_selected_endpoint'))
        if (endpoint.driver == 'snapmaker_u1' and
                context.get('delegated_pullback') and
                not require_park and not retain_selected):
            raise BMCUError(
                'Snapmaker long pullback was blocked because neither a '
                'confirmed Head park nor an explicit same-Head reload was requested')
        if retain_selected:
            endpoint.verify_selected()
            context['selected_confirmed_before_pullback'] = True
        if require_park:
            phase = 'WAIT_SOURCE_PARKED'
            self._set_phase(device, phase)
            timeout = float(endpoint.get(
                'u1_prestage_park_timeout', 20.0) or 20.0)
            endpoint.wait_parked_for_prestage(timeout=timeout)
            context['park_confirmed_before_pullback'] = True
            logging.info(
                'BMCU Snapmaker Head %d PARKED confirmation accepted before '
                'long Channel %d pullback',
                int(endpoint.get('head_index', -1)) + 1, channel + 1)
        if callable(cancel_check) and cancel_check():
            raise BMCUError('BMCU pullback was cancelled before start')
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            raise BMCUError('BMCU pullback was cancelled before start')

        if not context.get('before_pullback_started'):

            device.refresh()
            if context.get('pullback_start_m') is None:
                context['pullback_start_m'] = float(
                    device.status['meters'][channel])
            phase = 'BEFORE_PULLBACK'
            self._set_phase(device, phase)
            device.set_motion(channel, protocol.MOTION_BEFORE_PULL_BACK)
            context['before_pullback_started'] = True
            self.reactor.pause(self.reactor.monotonic() + 0.2)
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            try:
                device.stop_all()
            except Exception:
                pass
            raise BMCUError('BMCU pullback was cancelled before motion')
        device.refresh()
        buffer_pct = int(device.status['buffer_pct'][channel])
        if not (5 < buffer_pct < 95):
            buffer_pct = self._wait_unload_buffer_ready(
                device, channel, timeout=4.0,
                pause_for_critical=bool(background),
                cancel_check=cancel_check)
        context['release_buffer_pct'] = buffer_pct

        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            try:
                device.stop_all()
            except Exception:
                pass
            raise BMCUError('BMCU pullback was cancelled before motion')
        if callable(cancel_check) and cancel_check():
            try:
                device.stop_all()
            except Exception:
                pass
            raise BMCUError('BMCU pullback was cancelled before motion')
        phase = 'PULLBACK'
        self._set_phase(device, phase)
        device.set_motion(channel, protocol.MOTION_PULL_BACK)
        context['pullback_started'] = True
        return context

    def _begin_unload_locked(self, device, endpoint, channel,
                             reconcile_requires_park=False,
                             retain_selected_endpoint=False,
                             temperature_profile=None,
                             reason='unload'):
        self._arm_u1_persistent_hold(
            endpoint, device, channel, 'unload BMCU route')
        phase = 'PRECHECK_UNLOAD'
        self._set_phase(device, phase)
        channel_metadata = self._channel_metadata(device, channel)
        material = channel_metadata.get('material', '')
        delegated_pullback = bool(
            endpoint.delegates_long_unload_to_feeder())
        assist_limit_mm = (0.0 if delegated_pullback else float(
            endpoint.get('unload_assist_limit_mm', 0.0) or 0.0))
        heater_target = None
        tip_profile = None
        if endpoint.driver == 'snapmaker_u1' and delegated_pullback:
            heater_target = endpoint.capture_heater_target()
            if heater_target is None:
                raise BMCUError(
                    'U1 heater target is unavailable; fast unload was blocked '
                    'before heating the nozzle')
            tip_profile = self._u1_tip_profile_for_material(material)
        park_at_tip_marker = bool(
            delegated_pullback and not retain_selected_endpoint)
        if park_at_tip_marker and self.release_retract_mm > 0:

            raise BMCUError(
                'release_retract_mm must be 0 when U1 early tip-marker '
                'parking is active')

        context = {
            'material': material,
            'channel_metadata': channel_metadata,
            'delegated_pullback': delegated_pullback,
            'heater_target': heater_target,
            'reconcile_requires_park': bool(reconcile_requires_park),
            'retain_selected_endpoint': bool(retain_selected_endpoint),
            'selected_confirmed_before_tip': False,
            'selected_confirmed_before_pullback': False,
            'same_head_select_fallback': False,
            'temperature_profile': copy.deepcopy(temperature_profile),
            'tip_profile': copy.deepcopy(tip_profile),
            'before_pullback_started': False,
            'pullback_started': False,
            'pullback_start_m': None,
            'release_buffer_pct': None,
            'unload_assist': None,
            'endpoint_released': False,
            'park_confirmed_before_pullback': False,
            'parked_at_tip_marker': False,
            'parked_tip_tail_plan': None,
            'parked_tip_tail_ticket': None,
            'parked_tip_tail_complete': False,
            'tip_temperature_program_active': False,
            'tip_fan_program_active': False,
            'heater_restore_deferred_to_tip_tail': False,
        }

        try:
            phase = 'SELECT_ENDPOINT'
            self._set_phase(device, phase)
            endpoint.suspend_managed_sensors()
            if retain_selected_endpoint:

                try:
                    endpoint.verify_selected()
                    context['selected_confirmed_before_tip'] = True
                except Exception:
                    logging.warning(
                        'BMCU same-Head swap expected %s selected before tip; '
                        'falling back to stock select', endpoint.name)
                    endpoint.select()
                    endpoint.verify_selected()
                    context['selected_confirmed_before_tip'] = True
                    context['same_head_select_fallback'] = True
            else:
                endpoint.select()
                endpoint.verify_selected()
            self._endpoint_temperature_call(
                endpoint, 'prepare_unload', material, temperature_profile,
                tip_profile=tip_profile)

            phase = 'CUT_OR_FORM'
            self._set_phase(device, phase)

            device.refresh()
            context['pullback_start_m'] = float(
                device.status['meters'][channel])
            device.set_motion(channel, protocol.MOTION_BEFORE_PULL_BACK)
            context['before_pullback_started'] = True
            self.reactor.pause(self.reactor.monotonic() + 0.2)

            if endpoint.driver == 'generic_single_extruder':
                endpoint.prepare_toolhead_for_pullback(
                    material, reason=reason)
                tip_result = {}
            else:
                tip_result = self._endpoint_temperature_call(
                    endpoint, 'cut_or_form_tip', material, temperature_profile,
                    tip_profile=tip_profile,
                    park_at_marker=park_at_tip_marker)
                tip_result = tip_result if isinstance(tip_result, dict) else {}
            parked_at_marker = bool(tip_result.get('parked_at_marker'))
            context['parked_at_tip_marker'] = parked_at_marker
            context['parked_tip_tail_plan'] = copy.deepcopy(
                tip_result.get('parked_tip_tail_plan'))
            context['parked_tip_tail_ticket'] = copy.deepcopy(
                tip_result.get('parked_tip_tail_ticket'))
            tail_ticket = context.get('parked_tip_tail_ticket')
            context['tip_temperature_program_active'] = bool(
                isinstance(tail_ticket, dict) and
                int(tail_ticket.get('temperature_event_count', 0) or 0) > 0)
            context['tip_fan_program_active'] = bool(
                isinstance(tail_ticket, dict) and
                int(tail_ticket.get('fan_event_count', 0) or 0) > 0)
            if parked_at_marker:

                if not context['parked_tip_tail_plan']:
                    raise BMCUError(
                        'Snapmaker park marker did not produce a post-marker tip program')
                tail_ticket = context.get('parked_tip_tail_ticket')
                if (not isinstance(tail_ticket, dict) or
                        'id' not in tail_ticket):
                    raise BMCUError(
                        'Snapmaker park marker did not queue the source Head '
                        'post-marker tip program before cleanup and park')
                context['parked_tip_tail_queued'] = True
                context['endpoint_released'] = True
                context['park_confirmed_before_pullback'] = True
            else:
                endpoint.release_filament(material)
                endpoint.verify_release(material)
                if (endpoint.driver != 'generic_single_extruder' and
                        self.release_retract_mm > 0):
                    endpoint.extrude(
                        -self.release_retract_mm, self.release_retract_feed)

            phase = 'TOOLHEAD_RELEASE'
            self._set_phase(device, phase)
            context['unload_assist'] = endpoint.assist_unload(
                material, assist_limit_mm)

            if delegated_pullback:

                defer_heater_restore = bool(
                    context.get('parked_at_tip_marker') and
                    context.get('tip_temperature_program_active') and
                    not context.get('parked_tip_tail_complete'))
                if defer_heater_restore:
                    context['heater_restore_deferred_to_tip_tail'] = True
                    logging.info(
                        'BMCU deferred %s heater restore until its exact tip '
                        'temperature program completes', endpoint.name)
                else:
                    endpoint.restore_heater_target(heater_target)
                if not reconcile_requires_park:
                    if retain_selected_endpoint:

                        endpoint.verify_selected()
                        context['selected_confirmed_before_pullback'] = True
                        self._start_delegated_u1_pullback_locked(
                            device, endpoint, channel, context,
                            require_park=False)
                    else:
                        if not context.get('parked_at_tip_marker'):
                            park_for_pullback = getattr(
                                endpoint, 'park_selected_head_for_pullback', None)
                            if not callable(park_for_pullback):
                                raise BMCUError(
                                    'Snapmaker endpoint cannot perform the required '
                                    'stock Head park before long pullback')
                            phase = 'PARK_SOURCE_HEAD'
                            self._set_phase(device, phase)
                            park_for_pullback()
                            context['endpoint_released'] = True
                            context['park_confirmed_before_pullback'] = True
                        self._start_delegated_u1_pullback_locked(
                            device, endpoint, channel, context,
                            require_park=True)
            else:
                device.refresh()
                release_buffer_pct = int(
                    device.status['buffer_pct'][channel])
                if not (5 < release_buffer_pct < 95):
                    release_buffer_pct = self._wait_unload_buffer_ready(
                        device, channel, timeout=4.0)
                context['release_buffer_pct'] = release_buffer_pct
                phase = 'PULLBACK'
                self._set_phase(device, phase)
                device.set_motion(channel, protocol.MOTION_PULL_BACK)
                context['pullback_started'] = True
        except Exception:
            tail_ticket = context.get('parked_tip_tail_ticket')
            if ((context.get('tip_temperature_program_active') or
                     context.get('tip_fan_program_active')) and
                    not context.get('parked_tip_tail_complete') and
                    isinstance(tail_ticket, dict) and 'id' in tail_ticket):
                try:
                    endpoint.wait_parked_tip_tail(
                        tail_ticket['id'],
                        poll_interval=self._u1_background_poll_interval)
                    context['parked_tip_tail_complete'] = True
                except Exception:
                    logging.exception(
                        'BMCU could not finish/restore exact U1 tip lane after '
                        'unload-start failure')
            if heater_target is not None:
                try:
                    endpoint.restore_heater_target(heater_target)
                    context['heater_restore_deferred_to_tip_tail'] = False
                except Exception:
                    logging.exception(
                        'BMCU could not restore U1 heater target after unload start failure')
            raise
        return context

    def _finish_unload_locked(self, device, endpoint, channel, context,
                              release_endpoint=True,
                              preserve_sensor_takeover=False,
                              cancel_check=None, background=False,
                              progress_callback=None):
        route_key = self._route_key(device, channel)
        phase = 'PULLBACK'
        self._set_phase(device, phase)
        delegated_u1_pullback = bool(
            endpoint.driver == 'snapmaker_u1' and
            context.get('delegated_pullback'))
        pullback = self._wait_pullback_safe(
            device, channel, self.unload_timeout,
            cancel_check=cancel_check,
            start_m=context.get('pullback_start_m'),
            poll_interval=(self._u1_background_poll_interval
                           if delegated_u1_pullback else 0.100),
            pause_for_critical=bool(background),
            progress_callback=progress_callback,
            allow_transient_buffer_extremes=delegated_u1_pullback)
        prior_encoder_mm = float(
            context.get('pullback_prior_encoder_mm', 0.0) or 0.0)
        if prior_encoder_mm > 0.0:
            pullback = dict(pullback)
            final_encoder_mm = float(pullback.get('encoder_mm', 0.0) or 0.0)
            pullback['last_segment_encoder_mm'] = final_encoder_mm
            pullback['encoder_mm'] = prior_encoder_mm + final_encoder_mm
            pullback['segments_mm'] = list(
                context.get('pullback_segments', [])) + [final_encoder_mm]
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            raise BMCUError('BMCU pullback was cancelled before commit')
        if callable(cancel_check) and cancel_check():
            raise BMCUError('BMCU pullback was cancelled before commit')
        device.refresh()

        phase = 'VERIFY_SOURCE_STATE'
        self._set_phase(device, phase)
        while context.get('foreground_head_prefetch_in_progress'):
            if callable(cancel_check) and cancel_check():
                raise BMCUError('BMCU pullback was cancelled during Head pickup')
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        buffer_pct = device.status['buffer_pct'][channel]
        if (not delegated_u1_pullback and
                (buffer_pct <= 3 or buffer_pct >= 97)):
            raise BMCUError('PARK_FAILED: buffer ended at %d%%' % buffer_pct)
        foreground_reselected = bool(
            context.get('foreground_head_reselected_during_pullback'))
        if delegated_u1_pullback:
            if (context.get('retain_selected_endpoint') or
                    foreground_reselected):
                try:
                    endpoint.verify_selected()
                    context['selected_confirmed_before_pullback'] = True
                except Exception as exc:
                    reason = ('foreground-prefetched' if foreground_reselected
                              else 'same-Head')
                    raise BMCUError(
                        'HEAD_STATE_FAILED: Snapmaker Head did not remain '
                        'selected during %s BMCU pullback: %s' % (reason, exc))
            else:
                try:
                    endpoint.verify_parked_for_prestage()
                    context['park_confirmed_before_pullback'] = True
                except Exception as exc:
                    raise BMCUError(
                        'PARK_FAILED: Snapmaker Head was not PARKED after long '
                        'pullback: %s' % exc)
        sensor_reconciliation = None
        delegated_u1 = bool(
            endpoint.driver == 'snapmaker_u1' and
            context.get('delegated_pullback') and
            hasattr(endpoint, 'reconcile_entry_sensor_empty_after_pullback'))
        if delegated_u1:
            channel_metadata = context.get(
                'channel_metadata', self._channel_metadata(device, channel))
            expected_retract_mm = float(
                context.get('expected_retract_mm_override',
                            channel_metadata.get(
                                'unload_retract_mm', 200.0) or 200.0))
            try:
                sensor_reconciliation = (
                    endpoint.reconcile_entry_sensor_empty_after_pullback(
                        pullback, expected_retract_mm,
                        require_park=bool(
                            not foreground_reselected and
                            (context.get('park_confirmed_before_pullback') or
                             context.get('reconcile_requires_park', False))),
                        allow_verified_cache_reconcile=bool(background)))
            except Exception as exc:
                measured = pullback.get('encoder_mm') if isinstance(
                    pullback, dict) else None
                measured_text = ('unknown' if measured is None else
                                 '%.1f mm' % float(measured))
                raise BMCUError(
                    'ENDPOINT_NOT_CLEARED: Snapmaker route finalization '
                    'failed after BMCU pullback completion (%s observed): %s' %
                    (measured_text, exc))
        elif endpoint.driver != 'snapmaker_u1':
            entry_detected = endpoint.sensor_detected('entry_sensor')
            if entry_detected is True:
                raise BMCUError(
                    'ENDPOINT_NOT_CLEARED: entry sensor remains active after pullback')
        if (background and
                self._wait_background_control_plane(
                    cancel_check=cancel_check,
                    poll_interval=self._u1_background_poll_interval)):
            raise BMCUError('BMCU pullback was cancelled before EMPTY commit')
        if callable(cancel_check) and cancel_check():
            raise BMCUError('BMCU pullback was cancelled before EMPTY commit')
        device.mark_unloaded(channel)
        device.refresh()
        if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
            raise BMCUError(
                'PARK_FAILED: BMCU did not commit Channel %d as EMPTY' %
                (channel + 1))
        if not self._routes_for_endpoint(
                endpoint.name,
                (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)):
            if (endpoint.driver == 'snapmaker_u1' and
                    hasattr(endpoint, 'commit_native_path_empty')):
                endpoint.commit_native_path_empty(
                    route_empty_verified=True)

            if not preserve_sensor_takeover:
                endpoint.release_runtime_sensor_takeover()
                self._clear_endpoint_projection(endpoint)
        if release_endpoint and not context.get('endpoint_released'):
            endpoint.release_endpoint()
        unloaded_tool = self.loaded_tools.pop(route_key, -1)
        self._unmark_print_route(device, channel)
        if self.active_tool == unloaded_tool:
            self.active_tool = -1
        self._save_runtime()
        self._drop_prestage_record(route_key, release_sensor=False)
        channel_metadata = context.get(
            'channel_metadata', self._channel_metadata(device, channel))
        self.last_diagnostic = {
            'operation': 'unload', 'device': device.name,
            'channel': channel, 'endpoint': endpoint.name,
            'buffer_pct': buffer_pct,
            'release_buffer_pct': context.get('release_buffer_pct'),
            'toolhead_assist': context.get('unload_assist'),
            'pullback': pullback,
            'sensor_reconciliation': sensor_reconciliation,
            'preserve_sensor_takeover': bool(preserve_sensor_takeover),
            'endpoint_released': bool(context.get('endpoint_released')),
            'park_confirmed_before_pullback': bool(
                context.get('park_confirmed_before_pullback')),
            'retained_selected_endpoint': bool(
                context.get('retain_selected_endpoint')),
            'selected_confirmed_before_tip': bool(
                context.get('selected_confirmed_before_tip')),
            'selected_confirmed_before_pullback': bool(
                context.get('selected_confirmed_before_pullback')),
            'foreground_head_reselected_during_pullback': bool(
                context.get('foreground_head_reselected_during_pullback')),
            'head_prefetch_safe_mm': context.get('head_prefetch_safe_mm'),
            'head_prefetch_clearance_source': context.get(
                'head_prefetch_clearance_source'),
            'same_head_select_fallback': bool(
                context.get('same_head_select_fallback')),
            'parked_at_tip_marker': bool(
                context.get('parked_at_tip_marker')),
            'park_retract_mm': float(
                channel_metadata.get('unload_retract_mm', 200.0)),
            'phase': phase,
        }
        return pullback

    def _unload_locked(self, device, endpoint, channel, release_endpoint=True,
                       reconcile_requires_park=False,
                       preserve_sensor_takeover=False,
                       retain_selected_endpoint=False,
                       cancel_check=None, temperature_profile=None,
                       reason='unload'):

        synchronous_u1_pullback = bool(
            endpoint.driver == 'snapmaker_u1' and
            endpoint.delegates_long_unload_to_feeder())
        if synchronous_u1_pullback:
            if reconcile_requires_park:
                raise BMCUError(
                    'a synchronous Snapmaker unload cannot request early Head '
                    'park; use the split background transition that owns the '
                    'parked negative tip tail')
            retain_selected_endpoint = True
        context = self._begin_unload_locked(
            device, endpoint, channel,
            reconcile_requires_park=reconcile_requires_park,
            retain_selected_endpoint=retain_selected_endpoint,
            temperature_profile=temperature_profile,
            reason=reason)
        return self._finish_unload_locked(
            device, endpoint, channel, context,
            release_endpoint=release_endpoint,
            preserve_sensor_takeover=preserve_sensor_takeover,
            cancel_check=cancel_check)

    def unload(self, gcmd=None):
        self._require_standalone_operation('BMCU_UNLOAD', gcmd)
        requested_tool = gcmd.get_int(
            'TOOL', None, minval=0, maxval=255) if gcmd else None
        requested_device = gcmd.get('DEVICE', None) if gcmd else None
        requested_channel = gcmd.get_int(
            'CHANNEL', None, minval=0, maxval=3) if gcmd else None
        candidates = []
        if requested_device is not None and requested_channel is not None:
            requested = self.devices_by_name.get(requested_device)
            if requested is not None and self._route_state(requested, requested_channel) == protocol.ROUTE_UNCERTAIN:
                raise BMCUError(
                    '%s Channel %d route is UNCERTAIN; open BMCU Diagnostics and choose Confirm EMPTY, Recover loose input filament, or Mark LOADED before unloading' %
                    (requested.name, requested_channel + 1))
        if requested_tool is not None:
            device, channel, endpoint = self._resolve_tool(requested_tool)
            if self._route_state(device, channel) != protocol.ROUTE_LOADED:
                raise BMCUError('T%d is not physically loaded' % requested_tool)
            candidates.append((device, channel, endpoint))
        else:
            for device in self.devices:
                if requested_device and device.name != requested_device:
                    continue
                for channel in self._loaded_channels(device):
                    if requested_channel is not None and channel != requested_channel:
                        continue
                    endpoint = self._endpoint_for_channel(device, channel)
                    if endpoint is not None:
                        candidates.append((device, channel, endpoint))
        if not candidates:
            if gcmd:
                gcmd.respond_info('BMCU: no matching loaded Channel')
            return
        if len(candidates) > 1:
            raise BMCUError(
                'multiple Channels are loaded; specify TOOL= or DEVICE= and CHANNEL=')
        device, channel, endpoint = candidates[0]
        self._validate_endpoint_for_operation(endpoint)
        self._preempt_refill_for_toolchange(device, channel, endpoint)
        self._lock(device, endpoint, 'UNLOAD Channel %d' % (channel + 1), channel=channel)
        unloaded = False
        try:
            if self._snapmaker_ungripped_tail(device, channel, endpoint):
                self._prepare_snapmaker_ungripped_tail_handoff_locked(
                    device, endpoint, channel,
                    temperature_profile=self._u1_active_temperature_profile(),
                    restore_heater=True)
                raise BMCUError(
                    'the exhausted Snapmaker tail reached the safe follower '
                    'boundary, but a replacement filament is required to push '
                    'the final hotend remnant through the nozzle; load another '
                    'source on this head instead of confirming EMPTY')
            else:
                self._unload_locked(device, endpoint, channel)
            unloaded = True
            if gcmd:
                gcmd.respond_info(
                    'BMCU unloaded %s Channel %d from %s' %
                    (device.name, channel + 1, endpoint.name))
        except Exception as exc:
            phase = self.active_operations.get(
                device.name, {}).get('phase', 'UNLOAD')
            self._stop_on_failure(
                device, endpoint, exc, phase, channel)
            if gcmd:
                raise gcmd.error(str(exc))
            raise
        finally:
            self._unlock(device, endpoint)
        if unloaded:
            self._release_u1_persistent_hold_if_safe(
                endpoint, 'manual unload confirmed BMCU route EMPTY')

    def _clear_prestage_locked(self, device, endpoint, staged,
                               preserve_sensor_takeover=False,
                               target_selected=False):
        channel = int(staged.get('channel', -1))
        self._arm_u1_persistent_hold(
            endpoint, device, channel, 'clear prestaged BMCU route')
        route_key = self._route_key(device, channel)
        if channel < 0 or channel > 3:
            self._drop_prestage_record(
                route_key, release_sensor=not preserve_sensor_takeover)
            raise BMCUError('invalid prestage Channel for %s' % device.name)
        phase = 'CLEAR_PRESTAGE'
        self._set_phase(device, phase)
        try:
            if endpoint.driver == 'snapmaker_u1':
                allow_selected = bool(
                    target_selected or staged.get('target_selected'))
                target_selected = False
                try:
                    endpoint.verify_parked_for_prestage()
                except Exception:
                    if not allow_selected:
                        raise
                    endpoint.verify_selected()
                    target_selected = True
                logging.info(
                    'BMCU Snapmaker Head %d %s confirmation accepted '
                    'before clearing prestaged Channel %d',
                    int(endpoint.get('head_index', -1)) + 1,
                    'SELECTED' if target_selected else 'PARKED', channel + 1)
            device.set_motion(channel, protocol.MOTION_PULL_BACK)
            self._wait_pullback_safe(
                device, channel, self.unload_timeout,
                allow_transient_buffer_extremes=(
                    endpoint.driver == 'snapmaker_u1'))
            if endpoint.driver == 'snapmaker_u1':
                endpoint.require_entry_sensor_snapshot(expected=False)
            elif endpoint.sensor_detected('entry_sensor') is True:
                raise BMCUError(
                    'PRESTAGE_NOT_CLEARED: entry sensor remains active')
            device.mark_unloaded(channel)
            device.refresh()
            if self._route_states_from_status(device.status)[channel] != protocol.ROUTE_EMPTY:
                raise BMCUError('PRESTAGE_NOT_CLEARED: route did not commit EMPTY')
            self._drop_prestage_record(
                route_key, release_sensor=not preserve_sensor_takeover)
        except Exception:
            try:
                device.stop_all()
            except Exception:
                pass
            raise

    def clear_prestage(self, device_name=None, tool=None, channel=None, gcmd=None):
        self._require_standalone_operation('BMCU_CLEAR_PRESTAGE', gcmd)
        route_key = None
        device = None
        if tool is not None:
            device, channel, _endpoint = self._resolve_tool(tool)
            route_key = self._route_key(device, channel)
        elif device_name is not None:
            device = self._require_device(gcmd, device_name)
            if channel is not None:
                route_key = self._route_key(device, channel)
            else:
                keys = [key for key in self.prestaged
                        if key.startswith(device.name + ':')]
                if len(keys) != 1:
                    raise BMCUError(
                        'specify CHANNEL when zero or multiple prestages exist on %s' %
                        device.name)
                route_key = keys[0]
        else:
            keys = list(self.prestaged)
            if len(keys) != 1:
                raise BMCUError(
                    'specify DEVICE/CHANNEL or TOOL when zero or multiple prestages exist')
            route_key = keys[0]
            device = self.devices_by_name.get(route_key.split(':', 1)[0])
        staged = self.prestaged.get(route_key)
        if not isinstance(staged, dict):
            if gcmd:
                gcmd.respond_info('%s has no prestaged filament' % route_key)
            return False
        if device is None:
            device = self.devices_by_name.get(staged.get('device'))
        if device is None:
            raise BMCUError('prestaged BMCU device no longer exists')
        endpoint = self.endpoints.get(staged.get('endpoint'))
        if endpoint is None:
            raise BMCUError(
                'prestaged endpoint %s no longer exists' %
                staged.get('endpoint'))
        self._lock(device, endpoint, 'CLEAR_PRESTAGE', channel=int(staged.get('channel', -1)))
        cleared = False
        try:
            self._clear_prestage_locked(device, endpoint, staged)
            cleared = True
            if gcmd:
                gcmd.respond_info(
                    'BMCU cleared prestage from %s Channel %d' %
                    (device.name, int(staged.get('channel', -1)) + 1))
        except Exception as exc:
            self._stop_on_failure(
                device, endpoint, exc, 'CLEAR_PRESTAGE',
                int(staged.get('channel', 0)))
            if gcmd:
                raise gcmd.error(str(exc))
            raise
        finally:
            self._unlock(device, endpoint)
        if cleared:
            self._release_u1_persistent_hold_if_safe(
                endpoint, 'prestage clear confirmed BMCU route EMPTY')
            return True
        return False

    def prestage_tool(self, tool, gcmd=None):
        self._require_standalone_operation('BMCU_PRESTAGE', gcmd)
        device, channel, endpoint = self._resolve_tool(tool)
        route_key = self._route_key(device, channel)
        existing = self.prestaged.get(route_key)
        if isinstance(existing, dict):
            if (existing.get('tool') == tool and
                    existing.get('endpoint') == endpoint.name):
                if gcmd:
                    gcmd.respond_info(
                        'BMCU T%d is already prestaged to %s' %
                        (tool, endpoint.name))
                return
            self.clear_prestage(
                device_name=device.name, channel=channel)
        if not endpoint.supports_background_prestage():
            message = 'endpoint %s does not allow background prestage' % endpoint.name
            if gcmd:
                raise gcmd.error(message)
            raise BMCUError(message)
        distance = float(endpoint.get('prestage_distance_mm', 0.0) or 0.0)
        if distance <= 0:
            message = 'endpoint prestage_distance_mm is not configured'
            if gcmd:
                raise gcmd.error(message)
            raise BMCUError(message)
        if self._loaded_devices_for_endpoint(
                endpoint.name, excluded_route=route_key):
            raise BMCUError(
                'endpoint %s already contains filament from another Channel' %
                endpoint.name)
        if self._prestaged_devices_for_endpoint(
                endpoint.name, excluded_route=route_key):
            raise BMCUError(
                'endpoint %s already has another prestaged Channel' %
                endpoint.name)
        self._validate_endpoint_for_operation(
            endpoint, require_u1_ownership=False)
        self._lock(device, endpoint, 'PRESTAGE T%d' % tool, channel=channel)
        phase = 'PRESTAGE'
        motion_started = False
        release_new_u1_hold = False
        u1_hold_was_active = bool(
            endpoint.driver == 'snapmaker_u1' and
            self._u1_ownership_record(endpoint.name).get('persistent_hold'))
        self._set_phase(device, phase)
        try:
            self._check_automatic_ready(device, channel)
            self._arm_u1_persistent_hold(
                endpoint, device, channel, 'prestage logical T%d' % tool)
            self._validate_endpoint_for_operation(endpoint)
            if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
                raise BMCUError(
                    '%s Channel %d route is not empty' %
                    (device.name, channel + 1))
            endpoint.suspend_managed_sensors()
            endpoint.prepare_prestage()
            safety_pct = int(endpoint.get(
                'prestage_buffer_limit_pct', self.contact_buffer_pct) or
                self.contact_buffer_pct)
            timeout_s = float(endpoint.get(
                'prestage_timeout', self.contact_timeout) or
                self.contact_timeout)
            motion_started = True
            op_id = device.start_feed_distance(
                channel, distance, safety_pct, int(timeout_s * 1000.0))
            result = self._wait_feed_operation(
                device, op_id, timeout_s + 2.0)
            if not result.get('ok'):
                raise BMCUError('PRESTAGE_FAILED: %s after %.1f mm' %
                                (result.get('reason'),
                                 result.get('measured_mm', 0.0)))
            self.prestaged[route_key] = {
                'device': device.name, 'tool': tool, 'channel': channel,
                'endpoint': endpoint.name, 'distance_mm': distance,
                'result': result,
            }
            endpoint.activate_runtime_sensor_takeover(force=True)
            if gcmd:
                gcmd.respond_info(
                    'BMCU prestaged T%d to %s (%.1f mm)' %
                    (tool, endpoint.name, distance))
        except Exception as exc:
            if (endpoint.driver == 'snapmaker_u1' and
                    not u1_hold_was_active and not motion_started):
                release_new_u1_hold = True
            self._stop_on_failure(device, endpoint, exc, phase, channel)
            if gcmd:
                raise gcmd.error(str(exc))
            raise
        finally:
            self._unlock(device, endpoint)
            if release_new_u1_hold:
                try:
                    self._release_u1_persistent_hold_if_safe(
                        endpoint, 'prestage preflight failed before filament motion')
                except Exception:
                    logging.exception(
                        'BMCU could not release U1 ownership after prestage preflight failure')

    def cmd_STATUS(self, gcmd):
        status = self.get_status()
        gcmd.respond_info(json.dumps(status, indent=2, sort_keys=True))

    def cmd_REFRESH(self, gcmd):
        for device in self.devices:
            if device.ready:
                device.refresh()
                for channel in range(4):
                    device.calibration[channel] = device.calibration_get(channel)
                    try:
                        device.request(
                            protocol.MSG_GET_SLOT_INFO, bytes([channel]),
                            expected=(protocol.MSG_SLOT_INFO,), timeout=1.0)
                    except Exception:
                        pass
        gcmd.respond_info('BMCU status refreshed')

    def cmd_STOP(self, gcmd):
        if self._u1_background_jobs:
            self._cancel_u1_background_jobs('BMCU_STOP', wait=False)
        for device in self.devices:
            if device.ready:
                device.stop_all()
        gcmd.respond_info('BMCU all motion stopped')

    def cmd_CALIBRATE(self, gcmd):
        device = self._require_device(gcmd)
        if not device.ready or not device.runtime_configured:
            raise gcmd.error('%s is not ready' % device.name)
        if device.name in self.active_operations:
            raise gcmd.error('%s is busy' % device.name)

        selection = str(gcmd.get('CHANNEL', 'ALL')).strip().upper()
        if selection in ('', 'ALL'):
            selected_mask = 0x0f
            selected_label = 'all Channels'
            selected_channels = list(range(4))
            selected_channel = 0xff
        else:
            try:
                selected_channel = int(selection, 10)
            except (TypeError, ValueError):
                raise gcmd.error('CHANNEL must be ALL or a number from 0 to 3')
            if selected_channel < 0 or selected_channel > 3:
                raise gcmd.error('CHANNEL must be ALL or a number from 0 to 3')
            selected_mask = 1 << selected_channel
            selected_label = 'Channel %d' % (selected_channel + 1)
            selected_channels = [selected_channel]

        for key in list(self._autoload_pending):
            if key[0] == device.name:
                self._autoload_pending.discard(key)
        for key in list(self._autoload_observation):
            if key[0] == device.name:
                self._autoload_observation.pop(key, None)
        device.stop_all()
        self.reactor.pause(self.reactor.monotonic() + 0.15)
        refreshed = dict(device.refresh())
        connected_mask = int(refreshed.get('connected_mask', 0) or 0) & 0x0f
        if selection in ('', 'ALL'):
            selected_mask &= connected_mask
            selected_channels = [channel for channel in range(4)
                                 if selected_mask & (1 << channel)]
            if not selected_channels:
                raise gcmd.error('%s has no connected BMCU channels' % device.name)
            selected_label = 'all connected Channels'
        elif not (connected_mask & (1 << selected_channel)):
            raise gcmd.error('%s Channel %d is disconnected' %
                             (device.name, selected_channel + 1))
        durable_tails = [
            channel for channel in selected_channels
            if self._durable_tail_route(device, channel) is not None]
        if durable_tails:
            raise gcmd.error(
                'Finish detached-tail recovery before calibration: Channel %s '
                'still has filament in the downstream route' %
                ', '.join(str(channel + 1) for channel in durable_tails))

        print_busy = bool(
            self.print_map_active or self.print_plan_open or
            self.print_transaction_phase or self.u1_cross_refill_pending)
        if not print_busy and not self.active_operations:
            changed = False
            previous = dict(refreshed)
            for channel in selected_channels:
                if (self._route_state(device, channel, refreshed) == protocol.ROUTE_UNCERTAIN and
                        not bool(refreshed.get('present', [0, 0, 0, 0])[channel])):
                    device.mark_unloaded(channel)
                    self._uncertain_routes.discard(self._route_key(device, channel))
                    self.loaded_tools.pop(self._route_key(device, channel), None)
                    self._unmark_print_route(device, channel)
                    changed = True
            if changed:
                refreshed = dict(device.refresh())
                self.device_status_changed(device, previous, refreshed)
                self._save_runtime()

        filament_present = [channel for channel in selected_channels
                            if bool(refreshed.get('present', [0, 0, 0, 0])[channel])]
        if filament_present:
            raise gcmd.error(
                'Remove filament before calibration: Channel %s still detects filament' %
                ', '.join(str(channel + 1) for channel in filament_present))
        occupied = [
            (channel, self._route_state(device, channel, refreshed))
            for channel in selected_channels
            if self._route_state(device, channel, refreshed) != protocol.ROUTE_EMPTY
        ]
        if occupied:
            raise gcmd.error(
                'Every selected route must be EMPTY before calibration: %s' %
                ', '.join('Channel %d=%s' %
                          (channel + 1,
                           protocol.ROUTE_NAMES.get(route, 'UNCERTAIN'))
                          for channel, route in occupied))
        if any(int(value) != protocol.MOTION_IDLE
               for value in device.status.get('motion', [])):
            raise gcmd.error('Stop BMCU motion before calibration')

        self._calibration_policy_suspended.add(device.name)
        try:
            self._send_device_runtime_config(device)
        except Exception:
            self._calibration_policy_suspended.discard(device.name)
            raise

        record = {
            'name': 'BUFFER_CALIBRATION',
            'type': protocol.OP_BUFFER_CALIBRATION,
            'type_name': 'buffer_calibration',
            'device': device.name,
            'channel': selected_channel,
            'progress': 0, 'stage': 0, 'done_mask': 0,
            'selected_mask': selected_mask,
        }
        self.active_operations[device.name] = record
        try:
            op_id = device.start_auto_calibration(selected_mask)
            record['op_id'] = op_id
        except Exception:
            self.active_operations.pop(device.name, None)
            self._calibration_policy_suspended.discard(device.name)
            try:
                self._send_device_runtime_config(device)
            except Exception:
                logging.exception(
                    'BMCU %s could not restore runtime policy after calibration start failure',
                    device.name)
            raise
        gcmd.respond_info('%s automatic calibration started for %s' %
                          (device.name, selected_label))

    def cmd_CALIBRATE_CANCEL(self, gcmd):
        device = self._require_device(gcmd)
        auto_cal = device.status.get('auto_calibration', {})
        active = (auto_cal.get('active') or
                  self.active_operations.get(device.name, {}).get('type') ==
                  protocol.OP_BUFFER_CALIBRATION)
        if not active:
            raise gcmd.error('%s has no active calibration' % device.name)
        device.abort_operation()
        gcmd.respond_info('%s calibration cancel requested' % device.name)

    def cmd_CALIBRATE_POINT(self, gcmd):
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        point_name = gcmd.get('POINT').upper()
        points = {'MIN': protocol.CAL_MIN, 'NEUTRAL': protocol.CAL_NEUTRAL, 'MAX': protocol.CAL_MAX}
        if point_name not in points:
            raise gcmd.error('POINT must be MIN, NEUTRAL or MAX')
        calibration = device.calibration_point(channel, points[point_name])
        gcmd.respond_info('%s Channel %d captured %s raw=%.5f mask=0x%X' %
                          (device.name, channel + 1, point_name, calibration['current_raw'], calibration['capture_mask']))

    def cmd_CALIBRATE_COMMIT(self, gcmd):
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        calibration = device.calibration_commit(channel)
        if not calibration.get('valid'):
            raise gcmd.error('buffer calibration validation failed')
        gcmd.respond_info('%s Channel %d calibration saved min=%.4f neutral=%.4f max=%.4f' %
                          (device.name, channel + 1, calibration['minimum'], calibration['neutral'], calibration['maximum']))

    def cmd_CALIBRATION_STATUS(self, gcmd):
        device = self._require_device(gcmd)
        channel = gcmd.get_int('CHANNEL', None, minval=0, maxval=3)
        channels = range(4) if channel is None else [channel]
        for ch in channels:
            cal = device.calibration_get(ch)
            gcmd.respond_info('%s Channel %d valid=%s capture=0x%X raw=%.5f min=%.4f neutral=%.4f max=%.4f' %
                              (device.name, ch + 1, cal['valid'], cal['capture_mask'], cal['current_raw'],
                               cal['minimum'], cal['neutral'], cal['maximum']))

    def _run_distance_test(self, gcmd, msg_type, default_mm, label):
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        millimeters = self._finite_gcmd_float(
            gcmd, 'MM', default_mm, minval=5.0, maxval=250.0)
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise gcmd.error(
                '%s Channel %d is not assigned to an endpoint' %
                (device.name, channel + 1))
        self._validate_endpoint_for_operation(endpoint)
        self._lock(device, endpoint, label)
        try:
            if not device.ready:
                raise BMCUError('%s is offline' % device.name)
            if not (device.status['calibration_valid_mask'] & (1 << channel)):
                raise BMCUError('%s Channel %d buffer is not calibrated' %
                                (device.name, channel + 1))
            if not device.status['present'][channel]:
                raise BMCUError('%s Channel %d has no filament' %
                                (device.name, channel + 1))
            if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
                raise BMCUError(
                    '%s Channel %d route is occupied; diagnostic movement is blocked' %
                    (device.name, channel + 1))
            if self._loaded_devices_for_endpoint(
                    endpoint.name,
                    excluded_route=self._route_key(device, channel)):
                raise BMCUError(
                    'endpoint %s is occupied by another Channel' % endpoint.name)
            if any(int(value) != protocol.MOTION_IDLE
                   for value in device.status.get('motion', [])):
                raise BMCUError(
                    'BMCU has active motion; diagnostic movement is blocked')
            self._arm_u1_persistent_hold(
                endpoint, device, channel,
                'manual %s' % label.lower())
            op_id = device.start_distance_operation(
                msg_type, channel, millimeters)
            minimum_s = 3.0 if msg_type == protocol.MSG_TEST_ENCODER else 6.0
            timeout_s = self._distance_wait_timeout(
                device, millimeters, minimum_s)
            result = device.wait_for_op(op_id, timeout=timeout_s)
            if not result['ok']:
                raise BMCUError('%s failed: %s measured=%.2f mm' %
                                (label, result['reason'],
                                 result['measured_mm']))
            gcmd.respond_info(
                '%s OK: %s Channel %d commanded=%.2f mm measured=%.2f mm duration=%d ms' %
                (label, device.name, channel + 1, result['target_mm'],
                 result['measured_mm'], result['duration_ms']))
        except Exception as exc:
            self._stop_on_failure(
                device, endpoint, exc,
                label.upper().replace(' ', '_'), channel)
            raise gcmd.error(str(exc))
        finally:
            self._unlock(device, endpoint)

    def cmd_TEST_ENCODER(self, gcmd):
        self._require_standalone_operation('BMCU_TEST_ENCODER', gcmd)
        self._run_distance_test(gcmd, protocol.MSG_TEST_ENCODER, self.encoder_test_mm, 'Encoder test')

    def cmd_CHANNEL_AUTOLOAD(self, gcmd):
        self._require_standalone_operation('BMCU_CHANNEL_AUTOLOAD', gcmd)
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        default_mm = float(
            self._channel_metadata(device, channel).get('autoload_mm', 120.0))
        self._run_distance_test(
            gcmd, protocol.MSG_CHANNEL_AUTOLOAD, default_mm, 'Channel autoload')

    def cmd_CHANNEL_RETRACT(self, gcmd):
        self._require_standalone_operation('BMCU_CHANNEL_RETRACT', gcmd)
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        self._lock_channel_input(
            device, 'RETRACT_INPUT %s:%d' % (device.name, channel), channel)
        try:
            if not self._channel_retract_runtime_supported(device):
                raise BMCUError(
                    '%s firmware does not support explicit Channel retract; '
                    'flash the firmware included with this package' % device.name)
            if not device.ready:
                raise BMCUError('%s is offline' % device.name)
            if not (device.status['calibration_valid_mask'] & (1 << channel)):
                raise BMCUError('%s Channel %d buffer is not calibrated' %
                                (device.name, channel + 1))
            if not device.status['present'][channel]:
                raise BMCUError('%s Channel %d has no input filament to retract' %
                                (device.name, channel + 1))
            if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
                raise BMCUError(
                    '%s Channel %d is routed to a toolhead; use BMCU_UNLOAD instead' %
                    (device.name, channel + 1))
            route_key = self._route_key(device, channel)
            if route_key in self.prestaged:
                raise BMCUError(
                    '%s Channel %d is prestaged; clear or unload that route first' %
                    (device.name, channel + 1))
            if self._durable_tail_route(device, channel) is not None:
                raise BMCUError(
                    '%s Channel %d has a detached toolhead tail; repair that route first' %
                    (device.name, channel + 1))
            if any(int(value) != protocol.MOTION_IDLE
                   for value in device.status.get('motion', [])):
                raise BMCUError(
                    '%s has active motion; input retract is blocked' % device.name)

            op_id = device.start_channel_retract(channel)
            result = device.wait_for_op(op_id, timeout=300.0)
            if not result['ok']:
                raise BMCUError(
                    'Channel retract failed: %s measured=%.2f mm' %
                    (result['reason'], result['measured_mm']))
            device.refresh()
            if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
                raise BMCUError(
                    'Channel retract completed but route ownership changed; '
                    'routing remains blocked for inspection')
            if (isinstance(self.last_error, dict) and
                    self.last_error.get('device') == device.name and
                    self.last_error.get('channel', -1) == channel and
                    self.last_error.get('code') in (
                        'CHANNEL_RETRACT_FAILED', 'ROUTE_RECOVERY_FAILED')):
                self.last_error = None
                self._sync_status_cache_runtime()
                self._save_runtime()
            gcmd.respond_info(
                '%s Channel %d input filament retracted (%.2f mm)' %
                (device.name, channel + 1, result['measured_mm']))
        except Exception as exc:
            try:
                device.stop_all()
            except Exception:
                logging.exception(
                    'BMCU %s could not stop after Channel retract failure',
                    device.name)
            self._record_error(
                'CHANNEL_RETRACT_FAILED', device=device.name, channel=channel,
                phase='INPUT_RETRACT', details=str(exc))
            raise gcmd.error(str(exc))
        finally:
            self._unlock_channel_input(device)

    def cmd_APPLY_PRESET(self, gcmd):
        preset_name = gcmd.get('PRESET')
        count = gcmd.get_int('COUNT', 4, minval=1, maxval=255)
        try:
            generated = presets.build(preset_name, count)
        except ValueError as exc:
            raise gcmd.error(str(exc))
        analysis = compat.analyze_printer(self.printer, self.controller_mode)
        canonical_preset = presets.canonical_name(preset_name)
        topology = str(analysis.get('topology', '') or '')
        allowed = ({'snapmaker_u1'} if topology == 'snapmaker_u1' else
                   {'generic_single_extruder'})
        if canonical_preset not in allowed:
            raise gcmd.error(
                'preset %s is not valid for detected topology %s' %
                (canonical_preset, topology or 'unknown'))
        preset_warnings = []
        for endpoint_name, endpoint_config in generated.items():
            candidate = create_endpoint(self, endpoint_name, endpoint_config)
            validation = candidate.validate()
            if validation.get('errors'):
                raise gcmd.error(
                    'preset %s endpoint %s is unsafe on this printer: %s' %
                    (preset_name, endpoint_name,
                     '; '.join(validation.get('errors', []))))
            for warning in validation.get('warnings', []):
                preset_warnings.append('%s: %s' % (endpoint_name, warning))
        replace = bool(gcmd.get_int('REPLACE', 0, minval=0, maxval=1))
        missing_only = bool(gcmd.get_int(
            'MISSING_ONLY', 0, minval=0, maxval=1))
        if replace and missing_only:
            raise gcmd.error('REPLACE and MISSING_ONLY cannot be used together')
        existing = self.state.data.get('endpoints', {})
        resulting = dict(generated) if replace else dict(existing)
        if not replace:
            for name, values in generated.items():
                if missing_only and name in resulting:
                    continue
                current = dict(resulting.get(name, {}))
                current.update(values)
                resulting[name] = current
        if len(resulting) > MAX_STATE_ENDPOINTS:
            raise gcmd.error(
                'preset would exceed the %d Endpoint state limit' %
                MAX_STATE_ENDPOINTS)

        removed_or_changed = set()
        for name, old in existing.items():
            new = resulting.get(name)
            if new is None or str(new.get('driver', '')).lower() != str(old.get('driver', '')).lower():
                removed_or_changed.add(name)
        assigned = []
        for device in self.devices:
            for channel in range(4):
                endpoint_name = self._channel_endpoint_name(device, channel)
                if endpoint_name in removed_or_changed:
                    assigned.append((device, channel, endpoint_name))
        if assigned:
            summary = ', '.join('%s Channel %d -> %s' % (item[0].name, item[1] + 1, item[2])
                                for item in assigned)
            raise gcmd.error(
                'disconnect these channel routes before replacing/removing their endpoints: %s' %
                summary)

        snapshot = copy.deepcopy(self.state.data)
        restored = []
        try:
            for name in removed_or_changed:
                old = existing.get(name, {})
                if (str(old.get('driver', '')).lower() == 'snapmaker_u1' and
                        old.get('u1_native_feeder_takeover', False)):
                    endpoint = self.endpoints.get(name) or create_endpoint(self, name, old)
                    self._handoff_u1_to_native(
                        endpoint, 'endpoint removed by preset', save=False,
                        close_generation=True)
                    restored.append(name)
            self.state.data['endpoints'] = resulting
            self._load_endpoints()
            self.state.save()

            device_name = gcmd.get('DEVICE', None)
            channel = gcmd.get_int('CHANNEL', None, minval=0, maxval=3)
            endpoint_name = gcmd.get('ENDPOINT', None)
            head = gcmd.get_int('HEAD', None, minval=0, maxval=254)
            if endpoint_name is None and head is not None and str(preset_name).lower() in ('snapmaker_u1', 'u1'):
                endpoint_name = 'u1_head%d' % head
            if device_name is not None or channel is not None or endpoint_name is not None:
                if device_name is None or channel is None or endpoint_name is None:
                    raise BMCUError(
                        'DEVICE, CHANNEL and ENDPOINT/HEAD are all required for one-step routing')
                device = self._require_device(gcmd, device_name)
                self._assign_channel_endpoint(
                    device, channel, endpoint_name, save=True)
        except Exception as exc:
            self.state.data = snapshot
            self._load_endpoints()
            self._u1_lease_dirty = True
            try:
                self.state.save()
            except Exception as rollback_exc:
                raise gcmd.error(
                    '%s; preset rollback could not be saved: %s' %
                    (exc, rollback_exc))
            raise gcmd.error(str(exc))
        message = 'BMCU preset %s applied: %s' % (
            preset_name, ', '.join(sorted(generated)))
        if preset_warnings:
            message += '\nWarnings: ' + '; '.join(preset_warnings)
        gcmd.respond_info(message)

    def cmd_SET_FILAMENT(self, gcmd):
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        metadata = self._channel_metadata(device, channel)
        material = str(
            gcmd.get('MATERIAL', metadata.get('material', 'PLA')) or '').strip().upper()
        if self.printer_analysis.get('features', {}).get('snapmaker_u1'):
            try:
                material = normalize_u1_material_name(material)
            except Exception as exc:
                raise gcmd.error(str(exc))
        color = gcmd.get('COLOR', metadata.get('color', '#FFFFFF')).upper()
        if not color.startswith('#'):
            color = '#' + color
        if not re.match(r'^#[0-9A-F]{6}$', color):
            raise gcmd.error('COLOR must be RRGGBB or #RRGGBB')
        clear_name = bool(gcmd.get_int('CLEAR_NAME', 0, minval=0, maxval=1))
        name = '' if clear_name else gcmd.get(
            'NAME', metadata.get('name', ''))
        vendor = gcmd.get('VENDOR', metadata.get('vendor', ''))
        profile_id = gcmd.get('PROFILE_ID', metadata.get('profile_id', ''))
        spool_id = gcmd.get_int('SPOOL_ID', metadata.get('spool_id'), minval=-1)
        subtype = gcmd.get('SUBTYPE', metadata.get('subtype', 'generic'))
        colors_text = gcmd.get('COLORS', None)
        color_mode = gcmd.get_int('COLOR_MODE', int(metadata.get('color_mode', 0) or 0), minval=0, maxval=255)
        refill_enabled = gcmd.get_int('REFILL', None, minval=0, maxval=1)
        clear_refill_group = bool(gcmd.get_int(
            'CLEAR_REFILL_GROUP', 0, minval=0, maxval=1))
        refill_group = '' if clear_refill_group else gcmd.get('REFILL_GROUP', None)
        refill_priority = gcmd.get_int('REFILL_PRIORITY', None, minval=0, maxval=10000)
        unload_retract_mm = self._finite_gcmd_float(
            gcmd, 'UNLOAD_RETRACT_MM', None, minval=10.0, maxval=2000.0)
        autoload_mm = self._finite_gcmd_float(
            gcmd, 'AUTOLOAD_MM', None, minval=10.0, maxval=1000.0)
        logical_tool = gcmd.get_int(
            'TOOL', None, minval=GENERIC_BMCU_TOOL_MIN,
            maxval=GENERIC_LOGICAL_TOOL_LIMIT - 1)
        swap_tool = bool(gcmd.get_int('SWAP', 0, minval=0, maxval=1))
        tool_change = self._prepare_logical_tool_change(
            gcmd, device, channel, logical_tool, swap=swap_tool)
        tmin = gcmd.get_int('TEMP_MIN', int(metadata.get('temperature_min', 170) or 170), minval=0, maxval=500)
        tmax = gcmd.get_int('TEMP_MAX', int(metadata.get('temperature_max', 300) or 300), minval=0, maxval=500)
        if tmin > tmax:
            raise gcmd.error('TEMP_MIN cannot exceed TEMP_MAX')
        colors = list(metadata.get('colors', [color]))
        if colors_text is not None:
            colors = []
            for value in colors_text.split(',')[:5]:
                value = value.strip().upper()
                if not value.startswith('#'):
                    value = '#' + value
                if not re.match(r'^#[0-9A-F]{6}$', value):
                    raise gcmd.error('COLORS must contain comma-separated RRGGBB values')
                colors.append(value)
            if not colors:
                colors = [color]
            color = colors[0]
        else:
            colors = [color] + [value for value in colors if value != color]
            colors = colors[:5]

        metadata_snapshot = copy.deepcopy(metadata)
        observation_key = self._path_learning_key(device, channel)
        observation_store = getattr(self, '_path_learning_observation', {})
        observation_present = (
            isinstance(observation_store, dict) and
            observation_key in observation_store)
        observation_snapshot = (
            copy.deepcopy(observation_store.get(observation_key))
            if observation_present else None)
        tool_updates = []
        try:
            metadata.update({'material': material, 'subtype': subtype,
                             'color': color, 'colors': colors, 'color_mode': color_mode,
                             'name': name, 'vendor': vendor, 'profile_id': profile_id,
                             'spool_id': (None if spool_id is None or spool_id < 0 else int(spool_id)),
                             'temperature_min': tmin, 'temperature_max': tmax})
            if refill_enabled is not None:
                metadata['refill_enabled'] = bool(refill_enabled)
            if refill_group is not None:
                metadata['refill_group'] = refill_group
            if refill_priority is not None:
                metadata['refill_priority'] = int(refill_priority)
            if unload_retract_mm is not None:
                metadata['unload_retract_mm'] = round(float(unload_retract_mm), 2)
            if autoload_mm is not None:
                metadata['autoload_mm'] = round(float(autoload_mm), 2)
            tool_updates = self._stage_logical_tool_change(tool_change)
            self.state.save()
        except Exception as exc:
            self._rollback_logical_tool_change(tool_updates)
            metadata.clear()
            metadata.update(metadata_snapshot)
            if not isinstance(
                    getattr(self, '_path_learning_observation', None), dict):
                self._path_learning_observation = {}
            if observation_present:
                self._path_learning_observation[observation_key] = (
                    observation_snapshot)
            else:
                self._path_learning_observation.pop(observation_key, None)
            raise gcmd.error(str(exc))

        self._commit_logical_tool_runtime(tool_change)
        device.slot_sync_required = True
        sync_note = 'LED sync pending until BMCU reconnects'
        if device.ready:
            try:
                device.set_slot(channel, color, name, tmin, tmax, material)
                device.slot_sync_required = False
                sync_note = 'LED updated'
            except Exception:
                logging.exception('BMCU %s Channel %d metadata saved; runtime LED sync deferred',
                                  device.name, channel + 1)
                sync_note = 'saved; LED sync pending'
        if (unload_retract_mm is not None or autoload_mm is not None) and device.ready:
            device.runtime_configured = False
            device.runtime_config_sync_pending = True
            self._sync_device_runtime_config(self.reactor.monotonic(), device)
        self._sync_channel_status_cache(device, channel)
        if tool_change is not None and tool_change.get('owner') is not None:
            owner_device, owner_channel = tool_change['owner']
            self._sync_channel_status_cache(owner_device, owner_channel)
        configured_autoload = float(metadata.get('autoload_mm', 120.0))
        effective_autoload = self._effective_channel_autoload_mm(device, channel)
        gcmd.respond_info(
            '%s Channel %d filament=%s color=%s retract=%.2fmm '
            'autoload=%.2fmm effective=%.2fmm last_path=%.2fmm (%s)' %
            (device.name, channel + 1, material, color,
             float(metadata.get('unload_retract_mm', 200.0)),
             configured_autoload, effective_autoload,
             float(metadata.get('path_length_mm', 0.0) or 0.0), sync_note))
        if tool_change is not None:
            gcmd.respond_info(self._logical_tool_change_message(tool_change))

    def cmd_APPLY_TIP_TEMP(self, gcmd):

        if not self.printer_analysis.get(
                'features', {}).get('snapmaker_u1'):
            raise gcmd.error(
                'BMCU attached tip temperature events are Snapmaker U1 only')
        head = gcmd.get_int('HEAD', minval=1, maxval=4)
        target = gcmd.get_float('TARGET')
        if not math.isfinite(target):
            raise gcmd.error('BMCU tip temperature TARGET must be finite')
        endpoint_name = 'u1_head%d' % (head - 1)
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or getattr(endpoint, 'driver', '') != 'snapmaker_u1':
            raise gcmd.error(
                'BMCU physical U1 Head %d endpoint is unavailable' % head)
        try:
            endpoint.queue_attached_tip_temperature_event(target)
        except Exception as exc:
            raise gcmd.error(str(exc))

    def cmd_APPLY_TIP_FAN(self, gcmd):

        if not self.printer_analysis.get(
                'features', {}).get('snapmaker_u1'):
            raise gcmd.error(
                'BMCU attached tip hotend-fan events are Snapmaker U1 only')
        head = gcmd.get_int('HEAD', minval=1, maxval=4)
        speed_raw = gcmd.get('SPEED', None)
        reset = gcmd.get_int('RESET', 0, minval=0, maxval=1)
        if (speed_raw is None) == (reset == 0):
            raise gcmd.error(
                'BMCU tip fan event requires exactly SPEED=<0..1> or RESET=1')
        speed = None
        if speed_raw is not None:
            try:
                speed = float(speed_raw)
            except (TypeError, ValueError):
                raise gcmd.error('BMCU tip fan SPEED must be numeric')
            if not math.isfinite(speed) or speed < 0.0 or speed > 1.0:
                raise gcmd.error('BMCU tip fan SPEED must be 0.0..1.0')
        endpoint_name = 'u1_head%d' % (head - 1)
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or getattr(endpoint, 'driver', '') != 'snapmaker_u1':
            raise gcmd.error(
                'BMCU physical U1 Head %d endpoint is unavailable' % head)
        try:
            endpoint.queue_attached_tip_hotend_fan_event(
                speed=speed, reset=bool(reset))
        except Exception as exc:
            raise gcmd.error(str(exc))

    def cmd_U1_GCODE(self, gcmd):
        if not self.printer_analysis.get(
                'features', {}).get('snapmaker_u1'):
            raise gcmd.error(
                'BMCU tip-forming profiles are available only on Snapmaker U1')
        action = str(gcmd.get('ACTION', 'GET') or 'GET').strip().upper()
        if action not in ('GET', 'SET', 'RESET', 'DELETE'):
            raise gcmd.error('ACTION must be GET, SET, RESET or DELETE')
        requested = str(
            gcmd.get('PROFILE', 'DEFAULT') or 'DEFAULT').strip()
        if requested.upper() == 'DEFAULT':
            profile_name = 'DEFAULT'
        else:
            try:
                profile_name = normalize_u1_material_name(requested)
            except Exception as exc:
                raise gcmd.error(str(exc))
        if action == 'GET':
            gcmd.respond_info(json.dumps({
                'profile': profile_name,
                'profiles': self._u1_tip_profiles_status(),
            }, sort_keys=True))
            return

        refill = getattr(self, 'refill', None)
        if (getattr(self, 'active_operations', {}) or
                getattr(self, '_u1_background_jobs', {}) or
                getattr(self, 'prestaged', {}) or
                getattr(self, 'u1_cross_refill_pending', {}) or
                (refill is not None and
                 (getattr(refill, 'transactions', {}) or
                  getattr(refill, '_pending', set())))):
            raise gcmd.error(
                'U1 tip-forming profiles cannot change during active BMCU motion or recovery; the editor values can be saved as soon as BMCU is idle')

        raw_profiles = self.state.data.get('u1_tip_profiles', {})
        raw_profiles = (copy.deepcopy(raw_profiles)
                        if isinstance(raw_profiles, dict) else {})
        materials = raw_profiles.setdefault('materials', {})
        if not isinstance(materials, dict):
            materials = {}
            raw_profiles['materials'] = materials

        if action in ('RESET', 'DELETE'):
            if profile_name == 'DEFAULT':
                if action == 'DELETE':
                    raise gcmd.error('the default tip profile cannot be deleted')
                raw_profiles.pop('default', None)
            else:
                materials.pop(profile_name, None)
        else:
            encoded = str(gcmd.get('DATA') or '').strip()
            if (not encoded or len(encoded) > 32768 or
                    re.fullmatch(r'[A-Za-z0-9_-]+', encoded) is None):
                raise gcmd.error('DATA must contain bounded URL-safe base64')
            try:
                padding = '=' * ((4 - len(encoded) % 4) % 4)
                raw = base64.urlsafe_b64decode(
                    (encoded + padding).encode('ascii'))
                profile = json.loads(raw.decode('utf-8'))
                profile = validate_u1_tip_profile(profile)
            except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
                raise gcmd.error(
                    'invalid U1 tip profile encoding: %s' % exc)
            except Exception as exc:
                raise gcmd.error(str(exc))
            if profile_name == 'DEFAULT':
                raw_profiles['default'] = profile
            else:
                if (profile_name not in materials and
                        len(materials) >= MAX_U1_TIP_PROFILES):
                    raise gcmd.error(
                        'BMCU supports at most %d material tip overrides' %
                        MAX_U1_TIP_PROFILES)
                materials[profile_name] = profile

        if not materials:
            raw_profiles.pop('materials', None)
        self.state.data['u1_tip_profiles'] = raw_profiles

        self._u1_tip_profile_store()
        self.state.save()
        verb = {
            'SET': 'saved', 'RESET': 'restored to package default',
            'DELETE': 'deleted',
        }[action]
        gcmd.respond_info(
            'BMCU U1 tip profile %s (%s)' % (verb, profile_name))

    def cmd_SET_PREFERENCES(self, gcmd):
        self._require_standalone_operation('BMCU_SET_PREFERENCES', gcmd)
        if self.active_operations:
            raise gcmd.error(
                'cannot change BMCU print-end preferences during an active operation')
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'cannot change BMCU print-end preferences during a print')
        value = bool(gcmd.get_int(
            'LEAVE_FINAL_FILAMENT_LOADED',
            1 if self._leave_final_filament_loaded() else 0,
            minval=0, maxval=1))
        preferences = self.state.data.setdefault('preferences', {})
        preferences['schema'] = 1
        preferences['leave_final_filament_loaded'] = value
        self.state.save()
        gcmd.respond_info(
            'BMCU final filament policy: %s' %
            ('leave the last confirmed route loaded' if value else
             'unload every confirmed BMCU route at normal print end'))

    def cmd_SET_ENDPOINT(self, gcmd):
        name = gcmd.get('NAME')
        if not re.match(r'^[A-Za-z0-9_.-]{1,64}$', str(name or '')):
            raise gcmd.error(
                'Endpoint name must use 1..64 letters, numbers, dot, dash or underscore')
        if (name not in self.state.data['endpoints'] and
                len(self.state.data['endpoints']) >= MAX_STATE_ENDPOINTS):
            raise gcmd.error(
                'Endpoint count exceeds the supported state limit of %d' %
                MAX_STATE_ENDPOINTS)
        old_config = copy.deepcopy(self.state.data['endpoints'].get(name, {}))
        old_endpoint = self.endpoints.get(name)
        if self.print_plan_open or self.print_map_active:
            raise gcmd.error(
                'endpoint configuration cannot change while a print plan is open or active')
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'endpoint configuration cannot change while the printer is active')
        refill = getattr(self, 'refill', None)
        if (getattr(self, 'active_operations', {}) or
                (refill is not None and
                 (getattr(refill, 'transactions', {}) or
                  getattr(refill, '_pending', set()))) or
                getattr(self, 'print_transaction_phase', '') or
                getattr(self, 'u1_cross_refill_pending', {}) or
                getattr(self, '_u1_background_jobs', {}) or
                getattr(self, 'prestaged', {})):
            raise gcmd.error(
                'endpoint configuration cannot change during BMCU motion, '
                'refill recovery, background preparation or prestage')
        for ownership in self.state.data.get('u1_ownership', {}).values():
            if (isinstance(ownership, dict) and
                    (ownership.get('tail_detached') or
                     ownership.get('follower_pending'))):
                raise gcmd.error(
                    'endpoint configuration cannot change while a detached-tail journal is active')
        for candidate in self.devices:
            for candidate_channel in range(4):
                candidate_record = self._channel_record(
                    candidate, candidate_channel)
                if (candidate_record.get('tail_detached') or
                        candidate_record.get('tail_follower_pending')):
                    raise gcmd.error(
                        'endpoint configuration cannot change while a detached-tail journal is active')
        if self._endpoint_has_loaded_or_active_route(name):
            raise gcmd.error('unload and stop all BMCU routes before changing endpoint %s' % name)
        current = dict(old_config)
        fields = {
            'DRIVER': 'driver', 'EXTRUDER': 'extruder', 'HEATER': 'heater',
            'EXPECTED_ACTIVE_EXTRUDER': 'expected_active_extruder',
            'SELECT_MACRO': 'select_macro', 'DESELECT_MACRO': 'deselect_macro',
            'VERIFY_MACRO': 'verify_selected_macro',
            'TOOLHEAD_PREPARE_MACRO': 'toolhead_prepare_macro',
            'BEFORE_PULLBACK_MACRO': 'before_pullback_macro',
            'PREPARE_LOAD_MACRO': 'prepare_load_macro',
            'TOOLHEAD_LOAD_MODE': 'toolhead_load_mode',
            'TOOLHEAD_LOAD_MACRO': 'toolhead_load_macro',
            'LOAD_READY_MACRO': 'load_ready_macro', 'PURGE_MACRO': 'purge_macro',
            'WIPE_MACRO': 'wipe_macro', 'PRIME_MACRO': 'prime_macro',
            'PREPARE_UNLOAD_MACRO': 'prepare_unload_macro',
            'RELEASE_MACRO': 'release_macro', 'TIP_FORM_MACRO': 'tip_form_macro',
            'CUT_MACRO': 'cut_macro', 'POST_CUT_MACRO': 'post_cut_macro',
            'CUTTER_MODE': 'cutter_mode', 'ENTRY_SENSOR': 'entry_sensor',
            'POST_GEARS_SENSOR': 'post_gears_sensor', 'MOTION_SENSOR': 'motion_sensor',
            'SENSOR_POLICY': 'sensor_policy', 'PREPARE_PRESTAGE_MACRO': 'prepare_prestage_macro',
            'BEFORE_BITE_MACRO': 'before_bite_macro',
            'VERIFY_RELEASE_MACRO': 'verify_release_macro',
            'SHARED_PATH_GROUP': 'shared_path_group',
            'SNAP_FEEDER_MODULE': 'u1_feeder_module',
            'SNAP_UNLOAD_MODE': 'u1_unload_mode',
            'REFILL_MODE': 'refill_mode', 'REFILL_MATCH': 'refill_match',
            'REFILL_PAUSE_MACRO': 'refill_pause_macro',
            'REFILL_RESUME_MACRO': 'refill_resume_macro',
            'REFILL_PRIME_MACRO': 'refill_prime_macro',
            'TAIL_MODE': 'tail_tracking_mode',
        }
        for parameter, key in fields.items():
            value = gcmd.get(parameter, None)
            if value is not None:
                current[key] = value
        head = gcmd.get_int('HEAD', None, minval=-1, maxval=255)
        if head is not None:
            current['head_index'] = head
        u1_feeder_channel = gcmd.get_int('SNAP_FEEDER_CHANNEL', None, minval=0, maxval=1)
        if u1_feeder_channel is not None:
            current['u1_feeder_channel'] = u1_feeder_channel
        prestage = gcmd.get_int('PRESTAGE', None, minval=0, maxval=1)
        if prestage is not None:
            current['prestage_while_unselected'] = bool(prestage)
        distance = self._finite_gcmd_float(gcmd, 'PRESTAGE_MM', None, minval=0.0, maxval=3000.0)
        if distance is not None:
            current['prestage_distance_mm'] = distance
        min_temp = self._finite_gcmd_float(gcmd, 'MIN_BITE_TEMP', None, minval=0.0, maxval=500.0)
        if min_temp is not None:
            current['min_bite_temp'] = min_temp
        min_unload_temp = self._finite_gcmd_float(gcmd, 'MIN_UNLOAD_TEMP', None, minval=0.0, maxval=500.0)
        if min_unload_temp is not None:
            current['min_unload_temp'] = min_unload_temp
        numeric_fields = (
            ('MAX_ROUTE_MM', 'max_route_mm', 10.0, 5000.0),
            ('FINAL_SEARCH_MM', 'final_search_mm', 5.0, 1000.0),
            ('CONTACT_BUFFER_PCT', 'contact_buffer_pct', 60.0, 98.0),
            ('TOOLHEAD_LOAD_MM', 'toolhead_load_mm', 10.0, 2000.0),
            ('CONTACT_TIMEOUT', 'contact_timeout', 1.0, 180.0),
            ('PRESTAGE_BUFFER_PCT', 'prestage_buffer_limit_pct', 55.0, 98.0),
            ('PRESTAGE_TIMEOUT', 'prestage_timeout', 1.0, 180.0),
            ('TAIL_TO_OUTPUT_MM', 'tail_to_output_mm', 0.0, 5000.0),
            ('SENSOR_TAIL_REMAINING_MM', 'sensor_tail_remaining_mm', 0.0, 1000.0),
            ('TAIL_RESERVE_MM', 'tail_reserve_mm', 0.0, 500.0),
            ('REFILL_BUFFER_PCT', 'refill_contact_buffer_pct', 60.0, 98.0),
            ('REFILL_TIMEOUT', 'refill_timeout', 2.0, 300.0),
            ('REFILL_RUNOUT_DEBOUNCE', 'refill_runout_debounce', 0.0, 5.0),
            ('REFILL_HANDOFF_MAX_MM', 'refill_handoff_max_mm', 5.0, 500.0),
            ('REFILL_HANDOFF_CHUNK_MM', 'refill_handoff_chunk_mm', 1.0, 30.0),
            ('REFILL_HANDOFF_FEED', 'refill_handoff_feed', 10.0, 3000.0),
            ('SNAP_COIL_THRESHOLD_SOFT', 'u1_coil_threshold_soft', 100.0, 10000.0),
            ('SNAP_COIL_THRESHOLD_HARD', 'u1_coil_threshold_hard', 100.0, 10000.0),
            ('SNAP_LOAD_TEMP', 'u1_load_temp', 0.0, 350.0),
            ('SNAP_UNLOAD_TEMP', 'u1_unload_temp', 0.0, 350.0),
            ('SNAP_LOAD_TO_NOZZLE_MM', 'u1_load_to_nozzle_mm', 1.0, 300.0),
            ('SNAP_PRIME_LENGTH_MM', 'u1_prime_length_mm', 0.0, 100.0),
            ('SNAP_REFILL_PRIME_LENGTH_MM', 'u1_refill_prime_length_mm', 0.0, 100.0),
            ('SNAP_TAIL_CHUNK_MM', 'snap_tail_chunk_mm', 1.0, 50.0),
            ('SNAP_TAIL_CLEANUP_EVERY_MM', 'snap_tail_cleanup_every_mm', 5.0, 100.0),
            ('SNAP_TAIL_MAX_MM', 'snap_tail_max_mm', 50.0, 5000.0),
            ('SNAP_TAIL_FEED', 'snap_tail_feed', 30.0, 1200.0),
            ('SNAP_TAIL_SOFT_FEED', 'snap_tail_soft_feed', 30.0, 1200.0),
            ('SNAP_TAIL_SETTLE', 'snap_tail_settle_s', 0.0, 1.0),
        )
        for parameter, key, minimum, maximum in numeric_fields:
            value = self._finite_gcmd_float(gcmd, parameter, None, minval=minimum, maxval=maximum)
            if value is not None:
                current[key] = value
        boolean_fields = (
            ('COLD_PRELOAD', 'cold_preload_allowed'),
            ('REQUIRE_SELECT_MACRO', 'require_select_macro'),
            ('VERIFY_ACTIVE_EXTRUDER', 'verify_active_extruder'),
            ('NATIVE_FILAMENT_MANAGER', 'native_filament_manager'),
            ('SNAP_FEEDER_TAKEOVER', 'u1_native_feeder_takeover'),
            ('SNAP_REQUIRE_FEEDER_CONFIRMATION', 'u1_require_feeder_confirmation'),
            ('AUTO_REFILL', 'auto_refill_enabled'),
            ('TAIL_RUNOUT', 'tail_runout_enabled'),
            ('SNAP_SENSOR_TAKEOVER', 'u1_sensor_takeover'),
            ('SNAP_NATIVE_HOTEND_SEQUENCES', 'u1_native_hotend_sequences'),
            ('SNAP_AUTO_HOME_XY', 'u1_auto_home_xy'),
            ('SNAP_REQUIRE_COIL_CONFIRMATION', 'u1_require_coil_confirmation'),
            ('SNAP_REQUIRE_HEAD_CONFIRMATION', 'u1_require_head_confirmation'),
            ('SNAP_TAIL_HANDOFF', 'snap_tail_handoff_enabled'),
        )
        for parameter, key in boolean_fields:
            value = gcmd.get_int(parameter, None, minval=0, maxval=1)
            if value is not None:
                current[key] = bool(value)
        if str(current.get('driver', '') or '').lower() == 'generic_single_extruder':

            for obsolete_key in (
                    'profile_label', 'heater', 'toolhead_load_mode',
                    'toolhead_load_macro', 'toolhead_load_mm', 'min_bite_temp',
                    'min_unload_temp', 'cold_preload_allowed',
                    'prepare_prestage_macro', 'prepare_load_macro',
                    'before_bite_macro',
                    'load_ready_macro', 'purge_macro', 'wipe_macro',
                    'prime_macro', 'prepare_unload_macro', 'tip_form_macro',
                    'cut_macro', 'post_cut_macro', 'release_macro',
                    'verify_release_macro', 'cutter_mode',
                    'refill_prime_macro', 'refill_handoff_max_mm',
                    'refill_handoff_chunk_mm', 'refill_handoff_feed',
                    'unload_assist_limit_mm'):
                current.pop(obsolete_key, None)

        if str(current.get('driver', '') or '').lower() == 'snapmaker_u1':

            current['u1_sensor_takeover'] = False
            current['u1_require_coil_confirmation'] = True
            current.pop('u1_load_search_max_mm', None)
            current.pop('u1_load_to_nozzle_feed', None)
            if float(current.get('u1_load_to_nozzle_mm', 70.0) or 0.0) <= 0.0:
                current['u1_load_to_nozzle_mm'] = 70.0

        takeover_parameter = gcmd.get_int('SNAP_FEEDER_TAKEOVER', None, minval=0, maxval=1)
        old_is_u1 = str(old_config.get('driver', '')).lower() == 'snapmaker_u1'
        new_is_u1 = str(current.get('driver', '')).lower() == 'snapmaker_u1'
        old_takeover = bool(old_config.get('u1_native_feeder_takeover', False))
        new_takeover = bool(current.get('u1_native_feeder_takeover', False))
        routed = self._endpoint_routed(name)
        old_target = (
            int(old_config.get('head_index', -1) or -1),
            str(old_config.get('u1_feeder_module', '') or ''),
            int(old_config.get('u1_feeder_channel', 0) or 0),
        )
        new_target = (
            int(current.get('head_index', -1) or -1),
            str(current.get('u1_feeder_module', '') or ''),
            int(current.get('u1_feeder_channel', 0) or 0),
        )
        if new_is_u1 and new_takeover and not routed:

            current['u1_native_feeder_takeover'] = False
            new_takeover = False
        old_ownership_must_end = (
            old_is_u1 and old_takeover and
            (not new_is_u1 or not new_takeover or old_target != new_target))
        if old_ownership_must_end and routed:
            raise gcmd.error(
                'detach every BMCU Output from %s before changing its U1 feeder ownership' %
                name)
        if not new_is_u1:
            current['u1_native_feeder_takeover'] = False
            new_takeover = False

        endpoint = create_endpoint(self, name, current)
        validation = endpoint.validate()
        if validation.get('errors'):
            raise gcmd.error('endpoint %s is invalid: %s' %
                             (name, '; '.join(validation['errors'])))
        if old_endpoint is not None:
            endpoint.runtime_sensor_restore = dict(old_endpoint.runtime_sensor_restore)
            endpoint.sensor_restore = dict(old_endpoint.sensor_restore)

        try:

            if old_ownership_must_end:
                previous_u1 = old_endpoint or create_endpoint(self, name, old_config)
                self._handoff_u1_to_native(
                    previous_u1, 'endpoint ownership configuration changed',
                    save=False, close_generation=True)
            if endpoint.driver == 'snapmaker_u1':
                if (takeover_parameter is not None and not new_takeover and
                        old_is_u1 and not routed):
                    self._handoff_u1_to_native(
                        endpoint, 'U1 feeder takeover disabled', save=False,
                        close_generation=True)

            keep_runtime_takeover = (
                self._endpoint_has_loaded_or_active_route(name) and
                endpoint.wants_runtime_sensor_takeover())
            if old_endpoint is not None and not keep_runtime_takeover:
                old_endpoint.release_runtime_sensor_takeover()
                endpoint.runtime_sensor_restore.clear()
                endpoint.sensor_restore.clear()
            elif keep_runtime_takeover:
                endpoint.activate_runtime_sensor_takeover()

            self.state.data['endpoints'][name] = current
            self.endpoints[name] = endpoint
            self.state.save()
            self._u1_lease_dirty = True
            if self._manual_refill_resume_needed():
                self._install_manual_refill_resume_wrapper()
        except Exception as exc:
            if old_config:
                self.state.data['endpoints'][name] = old_config
            else:
                self.state.data['endpoints'].pop(name, None)
            if old_endpoint is not None:
                self.endpoints[name] = old_endpoint
            else:
                self.endpoints.pop(name, None)
            raise gcmd.error(str(exc))

        gcmd.respond_info('BMCU endpoint %s saved: %s' %
                          (name, json.dumps(current, sort_keys=True)))

    def cmd_SET_OUTPUT(self, gcmd):
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        endpoint_name = str(gcmd.get('ENDPOINT')).strip()
        if endpoint_name.upper() in ('NONE', 'UNASSIGNED', '-'):
            endpoint_name = ''
        try:
            self._assign_channel_endpoint(
                device, channel, endpoint_name, save=True)
        except BMCUError as exc:
            raise gcmd.error(str(exc))
        gcmd.respond_info(
            '%s Channel %d -> %s' %
            (device.name, channel + 1, endpoint_name or 'not connected'))

    def cmd_SNAP_CHECK(self, gcmd):
        endpoint_name = gcmd.get('ENDPOINT')
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            raise gcmd.error('ENDPOINT must name a Snapmaker U1 endpoint')
        result = endpoint.integration_check()
        gcmd.respond_info(json.dumps(result, indent=2, sort_keys=True))
        if bool(gcmd.get_int('STRICT', 0, minval=0, maxval=1)) and not result['ok']:
            raise gcmd.error('Snapmaker U1 integration check failed for %s' % endpoint_name)

    def cmd_SNAP_FEEDER(self, gcmd):
        self._require_standalone_operation('BMCU_SNAP_FEEDER', gcmd)
        endpoint_name = gcmd.get('ENDPOINT')
        endpoint = self.endpoints.get(endpoint_name)
        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            raise gcmd.error('ENDPOINT must name a Snapmaker U1 endpoint')
        takeover = bool(gcmd.get_int('TAKEOVER', minval=0, maxval=1))
        save = bool(gcmd.get_int('SAVE', 0, minval=0, maxval=1))
        routed = self._endpoint_routed(endpoint_name)
        if takeover and not routed:
            raise gcmd.error(
                'assign at least one BMCU Channel to %s before taking over its native feeder' %
                endpoint_name)
        if not takeover and routed:
            raise gcmd.error(
                'detach every BMCU Channel from %s before restoring its native feeder' %
                endpoint_name)
        if takeover and save:
            raise gcmd.error(
                'persistent U1 feeder disable is forbidden; BMCU takeover is always a runtime lease')

        old_takeover = bool(endpoint.get('u1_native_feeder_takeover', False))
        state_config = self.state.data['endpoints'][endpoint_name]
        endpoint.config['u1_native_feeder_takeover'] = takeover
        try:
            state_config['u1_native_feeder_takeover'] = takeover
            self.state.save()
            if takeover:
                self._u1_lease_dirty = True
                self._reconcile_u1_leases(
                    self.reactor.monotonic(), force=True)
                owner = self._u1_owner_status(endpoint).get('owner', 'unknown')
                state_text = 'runtime lease reconciled; owner=%s' % owner
            else:

                self._handoff_u1_to_native(
                    endpoint, 'explicit BMCU feeder handoff', save=True,
                    close_generation=True)
                record = self._u1_ownership_record(endpoint_name)
                if record.get('generation_open'):

                    raise BMCUError(
                        '%s feeder lease remained open after handoff' %
                        endpoint_name)
                state_text = (
                    'Snapmaker owns the feed path; the current AUTO preference '
                    'was restored from the open lease baseline or left unchanged '
                    'when no active lease existed')
        except Exception as exc:
            endpoint.config['u1_native_feeder_takeover'] = old_takeover
            state_config['u1_native_feeder_takeover'] = old_takeover
            try:
                self.state.save()
            except Exception:
                logging.exception(
                    'BMCU could not persist U1 feeder ownership rollback for %s',
                    endpoint_name)
            try:
                if old_takeover:
                    self._u1_lease_dirty = True
                    self._reconcile_u1_leases(
                        self.reactor.monotonic(), force=True)
                else:
                    self._handoff_u1_to_native(
                        endpoint, 'U1 feeder ownership rollback', save=True,
                        close_generation=True)
            except Exception:
                logging.exception(
                    'BMCU could not roll back U1 feeder ownership for %s',
                    endpoint_name)
            raise gcmd.error(str(exc))
        gcmd.respond_info('%s native feeder %s' % (endpoint_name, state_text))

    def _prepare_logical_tool_change(
            self, gcmd, device, channel, logical_tool, swap=False):

        if logical_tool is None:
            if swap:
                raise gcmd.error('SWAP requires TOOL')
            return None
        logical_tool = int(logical_tool)
        is_u1 = self._snapmaker_platform()
        minimum = U1_NATIVE_TOOL_COUNT if is_u1 else GENERIC_BMCU_TOOL_MIN
        limit = U1_LOGICAL_TOOL_LIMIT if is_u1 else GENERIC_LOGICAL_TOOL_LIMIT
        if logical_tool < minimum or logical_tool >= limit:
            if is_u1:
                raise gcmd.error('Snapmaker U1 BMCU sources use T4-T31')
            raise gcmd.error('generic External is T0; BMCU sources use T1-T255')
        current_tool = self._virtual_tool_for_source(device, channel)
        if current_tool < minimum or current_tool >= limit:
            raise gcmd.error('%s Channel %d has no valid logical tool' %
                             (device.name, channel + 1))
        if logical_tool == current_tool:
            return {
                'changed': False,
                'device': device,
                'channel': int(channel),
                'current_tool': current_tool,
                'logical_tool': logical_tool,
                'owner': None,
            }
        confirmed = bool(gcmd.get_int(
            'CONFIRM_ORCA', 0, minval=0, maxval=1))
        if not confirmed:
            raise gcmd.error(
                'changing Orca slot %d -> %d changes the persistent slicer/G-code mapping; '
                'update or regenerate the OrcaSlicer printer profile, then repeat with CONFIRM_ORCA=1' %
                (current_tool + 1, logical_tool + 1))
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'logical tool identities cannot change while the printer is active')
        if self.print_plan_open or self.print_map_active:
            raise gcmd.error(
                'logical tool identities cannot change while a print plan is open or active')
        if getattr(self, 'active_operations', {}):
            raise gcmd.error(
                'logical tool identities cannot change during a BMCU operation')
        refill = getattr(self, 'refill', None)
        if (refill is not None and
                (getattr(refill, 'transactions', {}) or
                 getattr(refill, '_pending', set()))):
            raise gcmd.error(
                'logical tool identities cannot change during refill recovery')
        if (getattr(self, 'u1_cross_refill_pending', {}) or
                getattr(self, '_u1_background_jobs', {}) or
                getattr(self, 'prestaged', {})):
            raise gcmd.error(
                'logical tool identities cannot change during background preparation or while a prestage is retained')
        for ownership in self.state.data.get('u1_ownership', {}).values():
            if (isinstance(ownership, dict) and
                    (ownership.get('tail_detached') or
                     ownership.get('follower_pending'))):
                raise gcmd.error(
                    'logical tool identities cannot change while a detached-tail journal is active')
        for candidate in self.devices:
            for candidate_channel in range(4):
                candidate_record = self._channel_record(
                    candidate, candidate_channel)
                if (candidate_record.get('tail_detached') or
                        candidate_record.get('tail_follower_pending')):
                    raise gcmd.error(
                        'logical tool identities cannot change while a detached-tail journal is active')
        owner = self._tool_assignment_owner(
            logical_tool, exclude=(device, channel))
        if owner is not None and not swap:
            raise gcmd.error(
                'Orca slot %d is already assigned to %s Channel %d; repeat '
                'with SWAP=1 CONFIRM_ORCA=1 to exchange slots %d and %d atomically' %
                (logical_tool + 1, owner[0].name, owner[1] + 1,
                 current_tool + 1, logical_tool + 1))
        return {
            'changed': True,
            'device': device,
            'channel': int(channel),
            'current_tool': current_tool,
            'logical_tool': logical_tool,
            'owner': owner,
        }

    def _stage_logical_tool_change(self, change):
        if not change or not change.get('changed'):
            return []
        device = change['device']
        channel = int(change['channel'])
        record = self._channel_record(device, channel)
        updates = [(record, int(record.get('logical_tool', -1)),
                    int(change['logical_tool']))]
        owner = change.get('owner')
        if owner is not None:
            owner_record = self._channel_record(owner[0], owner[1])
            updates.append((owner_record,
                            int(owner_record.get('logical_tool', -1)),
                            int(change['current_tool'])))
        for record, _previous, replacement in updates:
            record['logical_tool'] = replacement
        return updates

    @staticmethod
    def _rollback_logical_tool_change(updates):
        for record, previous, _replacement in updates:
            record['logical_tool'] = previous

    def _commit_logical_tool_runtime(self, change):
        if not change or not change.get('changed'):
            return
        replacements = {
            int(change['current_tool']): int(change['logical_tool'])}
        if change.get('owner') is not None:
            replacements[int(change['logical_tool'])] = int(
                change['current_tool'])
        for route_key, value in list(self.loaded_tools.items()):
            try:
                value = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if value in replacements:
                self.loaded_tools[route_key] = replacements[value]
        if self.active_tool in replacements:
            self.active_tool = replacements[self.active_tool]

    @staticmethod
    def _logical_tool_change_message(change):
        if not change or not change.get('changed'):
            if change:
                return '%s Channel %d already uses Orca slot %d (T%d)' % (
                    change['device'].name, int(change['channel']) + 1,
                    int(change['logical_tool']) + 1,
                    int(change['logical_tool']))
            return ''
        device = change['device']
        channel = int(change['channel'])
        owner = change.get('owner')
        if owner is None:
            return '%s Channel %d changed Orca slot %d -> %d (T%d -> T%d)' % (
                device.name, channel + 1, int(change['current_tool']) + 1,
                int(change['logical_tool']) + 1, int(change['current_tool']),
                int(change['logical_tool']))
        return '%s Channel %d (Orca slot %d / T%d) <-> %s Channel %d (Orca slot %d / T%d)' % (
            device.name, channel + 1, int(change['logical_tool']) + 1,
            int(change['logical_tool']), owner[0].name, owner[1] + 1,
            int(change['current_tool']) + 1, int(change['current_tool']))

    def cmd_SET_TOOL(self, gcmd):

        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        logical_tool = gcmd.get_int(
            'TOOL', minval=GENERIC_BMCU_TOOL_MIN,
            maxval=GENERIC_LOGICAL_TOOL_LIMIT - 1)
        swap = bool(gcmd.get_int('SWAP', 0, minval=0, maxval=1))
        change = self._prepare_logical_tool_change(
            gcmd, device, channel, logical_tool, swap=swap)
        updates = []
        try:
            updates = self._stage_logical_tool_change(change)
            if updates:
                self.state.save()
        except Exception as exc:
            self._rollback_logical_tool_change(updates)
            raise gcmd.error(str(exc))
        self._commit_logical_tool_runtime(change)
        gcmd.respond_info(self._logical_tool_change_message(change))

    def cmd_MAP_TOOL(self, gcmd):
        if not self.print_plan_open:
            raise gcmd.error(
                'BMCU_MAP_TOOL is print-plan-only; use BMCU_SET_TOOL for a '
                'persistent channel T assignment, or open a print plan first')

        self.cmd_PRINT_MAP(gcmd)

    def _native_route_is_loaded(self, route):
        if not isinstance(route, dict) or route.get('kind') != 'native':
            return False
        endpoint = route.get('endpoint')
        if endpoint is None:
            return False
        state = self._u1_native_feeder_status(endpoint, strict=True)
        return (str(state.get('channel_state', '') or '').strip().lower() ==
                'load_finish')

    def _u1_verified_background_prestage_route(
            self, endpoint, target_device, target_channel):

        if (endpoint is None or target_device is None or
                endpoint.driver != 'snapmaker_u1'):
            return None
        try:
            target_channel = int(target_channel)
        except (TypeError, ValueError, OverflowError):
            return None
        if target_channel < 0 or target_channel > 3:
            return None
        route_key = self._route_key(target_device, target_channel)
        staged = self.prestaged.get(route_key)
        job = self._u1_background_jobs.get(endpoint.name)
        if not isinstance(staged, dict) or not isinstance(job, dict):
            return None
        result = staged.get('result')
        if not isinstance(result, dict):
            return None
        at_entry = bool(staged.get('at_entry'))
        partial = bool(staged.get('partial') and not at_entry)
        if ((at_entry and not result.get('ok')) or
                (partial and not staged.get('partial_commit'))):
            return None
        if not (staged.get('background') and (at_entry or partial)):
            return None
        if (str(staged.get('device', '') or '') != target_device.name or
                int(staged.get('channel', -1)) != target_channel or
                str(staged.get('endpoint', '') or '') != endpoint.name):
            return None
        expected_state = 'ready' if at_entry else 'partial'
        if (job.get('state') != expected_state or job.get('cancelled') or
                job.get('error') or not job.get('worker_finished')):
            return None
        if (job.get('target_device') is not target_device or
                int(job.get('target_channel', -1)) != target_channel or
                job.get('endpoint') is not endpoint):
            return None
        return route_key

    def _prepare_native_path_for_bmcu(self, endpoint, target_device=None,
                                      target_channel=-1):

        if endpoint is None or endpoint.driver != 'snapmaker_u1':
            return False
        if target_device is not None:
            try:
                target_channel = int(target_channel)
            except (TypeError, ValueError, OverflowError):
                target_channel = -1
            if (target_channel < 0 or target_channel > 3 or
                    self._channel_endpoint_name(
                        target_device, target_channel) != endpoint.name):
                raise BMCUError(
                    'target BMCU route does not belong to %s' % endpoint.name)

        allowed_uncertain = set()
        verified_prestage = self._u1_verified_background_prestage_route(
            endpoint, target_device, target_channel)
        if verified_prestage is not None:
            allowed_uncertain.add(verified_prestage)
        evidence = self._u1_require_consistent_route_ownership(
            endpoint.name, allowed_uncertain_routes=allowed_uncertain)
        if any(item['claimed'] for item in evidence):
            return False

        ownership = self._u1_ownership_record(endpoint.name)
        if (ownership.get('persistent_hold') or
                endpoint.name in getattr(self, '_u1_background_jobs', {})):
            return False

        owner = self._u1_owner_status(endpoint)
        owner_name = str(owner.get('owner', 'unknown') or 'unknown')
        if owner_name in ('bmcu', 'bmcu_busy'):
            return False
        if owner_name not in ('native', 'native_busy'):
            raise BMCUError(
                '%s ownership is %s; reconcile it before moving filament' %
                (endpoint.name, owner_name))

        path = endpoint.native_path_status()
        if not path.get('known'):
            raise BMCUError(
                '%s stock feeder state is unavailable; BMCU takeover is blocked' %
                endpoint.name)
        if path.get('busy'):
            state = str(path.get('channel_state', 'unknown') or 'unknown')
            raise BMCUError(
                '%s stock filament still occupies the shared path (%s); '
                'retract the stock filament manually before using BMCU on '
                'this Head' % (endpoint.name, state))
        return True

    def _activate_native_u1_route(self, route, gcmd=None):

        endpoint = route.get('endpoint') if isinstance(route, dict) else None
        if endpoint is None or route.get('kind') != 'native':
            raise BMCUError('native Snapmaker route is unavailable')
        self._reconcile_u1_pending_follower_commit(endpoint)
        self._wait_u1_background_for_route(route, gcmd=gcmd)

        self._u1_require_consistent_route_ownership(endpoint.name)
        loaded = self._loaded_devices_for_endpoint(endpoint.name)
        detached_source = None
        if len(loaded) > 1:
            raise BMCUError(
                'multiple BMCU routes are loaded into %s' % endpoint.name)
        if loaded:
            device, channel = loaded[0]
            self._preempt_refill_for_toolchange(
                device, int(channel), endpoint)
            self._lock(
                device, endpoint, 'SYNCHRONOUS SNAPMAKER NATIVE HANDOFF',
                channel=int(channel))
            try:
                self._validate_endpoint_for_operation(endpoint)
                if self._snapmaker_ungripped_tail(
                        device, int(channel), endpoint):
                    job = self._u1_background_jobs.get(endpoint.name)
                    source_prepared = bool(
                        isinstance(job, dict) and
                        job.get('source_tail_prepared') and
                        job.get('source_device') is device and
                        int(job.get('source_channel', -1)) == int(channel))
                    if not source_prepared:
                        self._prepare_snapmaker_ungripped_tail_handoff_locked(
                            device, endpoint, int(channel),
                            temperature_profile=(
                                self._u1_active_temperature_profile()),
                            restore_heater=False)
                    detached_source = (device, int(channel))
                else:
                    self._unload_locked(
                        device, endpoint, int(channel), release_endpoint=False,
                        reconcile_requires_park=False,
                        temperature_profile=self._u1_active_temperature_profile())
            finally:
                self._unlock(device, endpoint)

        if detached_source is not None:
            self._load_native_follower_for_detached_tail(
                detached_source[0], detached_source[1], endpoint,
                logical_tool=int(route['tool']))
        else:
            owner = self._u1_owner_status(endpoint)
            if owner.get('owner') not in ('native', 'native_busy'):
                released = self._release_u1_persistent_hold_if_safe(
                    endpoint, 'native source requested by tool change',
                    close_generation=True)
                if not released:
                    owner = self._u1_owner_status(endpoint)
                    if owner.get('owner') not in ('native', 'native_busy'):
                        raise BMCUError(
                            'native Snapmaker feeder ownership could not be restored')

            if not self._native_route_is_loaded(route):
                endpoint.native_source_preflight(
                    expected_enabled=True, require_filament=True)
                endpoint.native_feeder_load(printing=True)
                self._mark_u1_head_prepared(
                    endpoint.get('head_index', -1), source='stock native feeder')
        self._activate_u1_logical_tool(int(route['tool']), endpoint)
        self._consume_u1_background_route(route)
        return True

    def _activate_generic_external_tool(self, gcmd=None):
        endpoint = self._generic_external_endpoint()
        if endpoint is None:
            raise BMCUError(
                'generic External T0 requires one configured generic extruder endpoint')

        for staged in list(self.prestaged.values()):
            if not isinstance(staged, dict) or staged.get('endpoint') != endpoint.name:
                continue
            device = self.devices_by_name.get(staged.get('device'))
            if device is None:
                raise BMCUError('prestaged BMCU device no longer exists')
            self.clear_prestage(
                device_name=device.name,
                channel=int(staged.get('channel', -1)))

        uncertain = self._routes_for_endpoint(
            endpoint.name, (protocol.ROUTE_UNCERTAIN,))
        if uncertain:
            raise BMCUError(
                'cannot switch to External T0 while a BMCU route is uncertain')
        loaded = self._loaded_devices_for_endpoint(endpoint.name)
        if len(loaded) > 1:
            raise BMCUError(
                'generic endpoint %s has multiple loaded BMCU routes' % endpoint.name)
        if loaded:
            device, channel = loaded[0]
            self._preempt_refill_for_toolchange(device, channel, endpoint)
            self._lock(
                device, endpoint, 'SWITCH TO EXTERNAL T0', channel=channel)
            try:
                self._validate_endpoint_for_operation(endpoint)
                if self._ungripped_tail(device, channel, endpoint):
                    self._prepare_generic_ungripped_tail_handoff_locked(
                        device, endpoint, channel)
                    self.loaded_tools.pop(self._route_key(device, channel), None)
                    self._unmark_print_route(device, channel)
                    self._clear_generic_tail_detached(
                        endpoint, device, channel,
                        'manual External T0 selected')
                    endpoint.release_runtime_sensor_takeover()
                    self._clear_endpoint_projection(endpoint)
                    endpoint.release_endpoint()
                else:
                    self._unload_locked(
                        device, endpoint, channel, reason='toolchange')
            finally:
                self._unlock(device, endpoint)

        self.active_tool = GENERIC_EXTERNAL_TOOL
        self._save_runtime()
        if gcmd:
            gcmd.respond_info(
                'BMCU selected generic External T0; manual filament is not tracked')
        return True

    def cmd_TOOL_CHANGE(self, gcmd):
        self._require_standalone_operation('BMCU_TOOL_CHANGE', gcmd)
        tool = gcmd.get_int('TOOL', minval=0, maxval=255)
        if self.print_plan_open:
            raise gcmd.error(
                'BMCU print source plan is not committed; run '
                'BMCU_PRINT_COMMIT before the first tool change')
        is_snapmaker = bool(
            self.printer_analysis.get('features', {}).get('snapmaker_u1'))
        if is_snapmaker and tool >= 32:
            raise gcmd.error('Snapmaker accepts logical tools T0-T31')

        plan_index = -1
        background_endpoint = None
        try:
            self._require_no_u1_cross_refill_pending()
            plan_index = self._locate_u1_toolchange(tool) \
                if is_snapmaker else -1
            temperature_profile = (self._u1_temperature_profile(
                plan_index, tool=tool) if is_snapmaker else None)
            target_route = self._u1_logical_route(tool) \
                if is_snapmaker else None
            if is_snapmaker and target_route is not None:
                background_endpoint = self._prepare_u1_background_transition(
                    tool, plan_index, gcmd=gcmd)

                self._wait_u1_background_for_route(
                    target_route, gcmd=gcmd,
                    temperature_profile=temperature_profile)

            per_print = (self.print_tools.get(str(tool))
                         if self.print_map_active else None)
            if (not is_snapmaker and
                    (tool == GENERIC_EXTERNAL_TOOL or
                     (isinstance(per_print, dict) and per_print.get('external')))):
                self._activate_generic_external_tool(gcmd=gcmd)
            elif (isinstance(per_print, dict) and per_print.get('native')) or (
                    target_route is not None and
                    target_route.get('kind') == 'native'):
                route = target_route
                if route is None:
                    native_head = int(per_print.get('head', tool))
                    endpoint = self._u1_endpoint_for_head(native_head)
                    route = {'tool': int(tool), 'kind': 'native',
                             'head': native_head, 'endpoint': endpoint}
                self._activate_native_u1_route(route, gcmd=gcmd)
                gcmd.respond_info(
                    'BMCU delegated logical T%d to native Snapmaker head %d' %
                    (tool, int(route.get('head', 0))))
            elif self._effective_tool_mapping(tool):
                device, channel, endpoint = self._resolve_tool(tool)
                if is_snapmaker and endpoint.driver == 'snapmaker_u1':
                    self._prepare_native_path_for_bmcu(
                        endpoint, device, channel)
                self.load_channel(
                    device, channel, gcmd=gcmd, tool=int(tool),
                    prime_handshake=(is_snapmaker and
                                     endpoint.driver == 'snapmaker_u1'),
                    temperature_profile=temperature_profile)
                if is_snapmaker and endpoint.driver == 'snapmaker_u1':
                    self._activate_u1_logical_tool(tool, endpoint)
                    route = self._u1_logical_route(tool)
                    self._consume_u1_background_route(route)
            else:
                if not is_snapmaker:
                    raise BMCUError(
                        'generic T%d is not assigned: T0 is External and BMCU sources use T1+' % tool)
                script = ('T%d A0' % tool if tool < 4 else 'T%d' % tool)
                with self.printer_critical_section(
                        'delegated_native_toolchange'):
                    self.gcode.run_script_from_command(script)
                self.active_tool = tool
                self._save_runtime()
                gcmd.respond_info(
                    'BMCU delegated T%d to native printer toolchange' % tool)

            if is_snapmaker:
                self._commit_u1_toolchange(plan_index)
            if background_endpoint:

                self._release_u1_background_worker(background_endpoint)
        except Exception as exc:
            if is_snapmaker and self._u1_background_jobs:
                try:
                    self._cancel_u1_background_jobs(
                        'foreground T%d toolchange failed: %s' % (tool, exc),
                        wait=True)
                except Exception:
                    logging.exception(
                        'BMCU could not stop Snapmaker background work after '
                        'foreground toolchange failure')
            self._record_error(
                ('SNAPMAKER_TOOL_CHANGE_FAILED' if is_snapmaker else
                 'GENERIC_TOOL_CHANGE_FAILED'),
                phase='TOOL_CHANGE', details=str(exc))
            self._safe_pause(defer_to_virtual_sd=True)

            raise self._command_pause_error(gcmd, exc)

    def cmd_SNAP_REFILL_RESUME(self, gcmd):
        self._require_standalone_operation('BMCU_REFILL_RESUME', gcmd)
        pending = copy.deepcopy(self.u1_cross_refill_pending)
        if not pending:
            raise gcmd.error('no Snapmaker U1 cross-head refill recovery is pending')
        if pending.get('kind') != 'bmcu_cross':
            raise gcmd.error('invalid Snapmaker U1 cross-head refill journal')
        try:
            self._resume_u1_cross_refill_pending(pending)
        except Exception as exc:
            raise self._normalized_command_pause_error(gcmd, exc)
        gcmd.respond_info(
            'Snapmaker U1 BMCU cross-head refill resumed from durable journal')

    def cmd_SNAP_AUTO_FEED(self, gcmd):

        self._require_standalone_operation('BMCU_AUTO_FEED', gcmd)
        head = gcmd.get_int('EXTRUDER', minval=0, maxval=3)
        initial = gcmd.get_int('INITIAL_TOOL', -1, minval=-1, maxval=31)
        require_plan = bool(gcmd.get_int(
            'REQUIRE_PLAN', 0, minval=0, maxval=1))
        if require_plan and not self.print_map_active:
            raise gcmd.error(
                'BMCU print plan is not committed before automatic feed; '
                'keep the generated BMCU_PRINT_BEGIN/MAP/COMMIT block above '
                'PRINT_START')
        endpoint = self._u1_endpoint_for_head(head)
        if endpoint is None:
            raise gcmd.error(
                'Snapmaker endpoint for physical head %d is unavailable' % head)

        try:
            self._require_no_u1_cross_refill_pending()
            candidates = []
            identities = set()
            if self.print_map_active:
                for tool_text in sorted(
                        self.print_tools, key=lambda value: int(value)):
                    route = self._u1_logical_route(int(tool_text))
                    if (route is None or route.get('endpoint') is None or
                            route['endpoint'].name != endpoint.name):
                        continue
                    identity = self._u1_source_identity(route)
                    if identity is None or identity in identities:
                        continue
                    identities.add(identity)
                    candidates.append(route)

            selected = self._first_u1_route_for_endpoint(endpoint.name)
            if selected is None and initial >= 0:
                initial_route = self._u1_logical_route(initial)
                if (initial_route is not None and
                        initial_route.get('endpoint') is not None and
                        initial_route['endpoint'].name == endpoint.name):
                    selected = initial_route
            if selected is None and len(candidates) == 1:
                selected = candidates[0]
            if selected is None and len(candidates) > 1:
                raise BMCUError(
                    'automatic feed cannot determine the first physical source '
                    'for hybrid head %d from the current print position' % head)

            if selected is not None and selected.get('kind') == 'bmcu':
                device, channel, _target = self._resolve_tool(selected['tool'])
                self._prepare_native_path_for_bmcu(
                    endpoint, device, channel)
                previous_profile = getattr(
                    self, '_u1_pending_temperature_profile', None)
                self._u1_pending_temperature_profile = (
                    self._u1_temperature_profile(
                        selected.get('plan_index'), tool=selected['tool']))
                try:

                    self.load_tool(selected['tool'], gcmd)
                finally:
                    self._u1_pending_temperature_profile = previous_profile
                if self._route_state(device, channel) != protocol.ROUTE_LOADED:
                    raise BMCUError(
                        'automatic feed returned without a confirmed LOADED '
                        'route for T%d' % int(selected['tool']))
                gcmd.respond_info(
                    'BMCU automatic feed confirmed T%d loaded for Snapmaker head %d' %
                    (int(selected['tool']), head))
                return

            if selected is not None and selected.get('kind') == 'native':
                if self._loaded_devices_for_endpoint(endpoint.name):
                    raise BMCUError(
                        'native startup source is blocked by a loaded BMCU route '
                        'on %s' % endpoint.name)
                record = self._u1_ownership_record(endpoint.name)
                expected_enabled = None
                if record.get('baseline_captured'):
                    expected_enabled = not bool(record.get('baseline_disabled'))
                endpoint.native_source_preflight(
                    expected_enabled=expected_enabled, require_filament=True)
                owner = self._u1_owner_status(endpoint)
                if owner.get('owner') not in ('native', 'native_busy'):
                    if not self._release_u1_persistent_hold_if_safe(
                            endpoint, 'native startup source selected',
                            close_generation=True):
                        raise BMCUError(
                            'native feeder ownership could not be restored for '
                            'head %d' % head)
                self._run_u1_stock_auto_feed(
                    endpoint, head, source='stock startup auto-feed')

                self.active_tool = int(selected['tool'])
                self._save_runtime()
                gcmd.respond_info(
                    'BMCU delegated Snapmaker head %d auto-feed to the stock feeder' %
                    head)
                return

            owner = self._u1_owner_status(endpoint)
            _task, task_config = self._u1_task_config()
            used = bool(task_config is not None and
                        len(task_config.get('extruders_used', [])) > head and
                        task_config['extruders_used'][head])
            if not used:
                gcmd.respond_info(
                    'BMCU skipped unused Snapmaker head %d auto-feed' % head)
                return
            if owner.get('owner') in ('native', 'native_busy'):
                self._run_u1_stock_auto_feed(
                    endpoint, head, source='stock owner auto-feed')
                self.active_tool = int(head)
                self._save_runtime()
                gcmd.respond_info(
                    'BMCU delegated Snapmaker head %d auto-feed to the stock feeder' %
                    head)
                return
            raise BMCUError(
                'Snapmaker head %d has no unambiguous source in the committed '
                'print plan (%s)' %
                (head, owner.get('reason', 'ownership is not reconciled')))
        except Exception as exc:
            self._record_error(
                'SNAPMAKER_AUTO_FEED_FAILED', endpoint=endpoint.name,
                phase='AUTO_FEED', details=str(exc))
            raise self._command_pause_error(gcmd, exc)

    def cmd_LOAD(self, gcmd):
        try:
            tool = gcmd.get_int('TOOL', None, minval=0, maxval=255)
            device_name = gcmd.get('DEVICE', None)
            channel = gcmd.get_int('CHANNEL', None, minval=0, maxval=3)
            if tool is not None:
                if device_name is not None or channel is not None:
                    raise gcmd.error('use either TOOL or DEVICE+CHANNEL, not both')
                self.load_tool(tool, gcmd)
                return
            if device_name is None or channel is None:
                raise gcmd.error('BMCU_LOAD requires TOOL or DEVICE+CHANNEL')
            device = self.devices_by_name.get(device_name)
            if device is None:
                raise gcmd.error('unknown BMCU device %s' % device_name)
            self.load_channel(device, channel, gcmd=gcmd)
        except Exception as exc:

            raise self._normalized_command_pause_error(gcmd, exc)

    def cmd_UNLOAD(self, gcmd):
        try:
            self.unload(gcmd)
        except Exception as exc:
            raise self._normalized_command_pause_error(gcmd, exc)

    def cmd_PRESTAGE(self, gcmd):
        try:
            self.prestage_tool(
                gcmd.get_int('TOOL', minval=0, maxval=255), gcmd)
        except Exception as exc:
            raise self._normalized_command_pause_error(gcmd, exc)

    def cmd_CLEAR_PRESTAGE(self, gcmd):
        try:
            tool = gcmd.get_int('TOOL', None, minval=0, maxval=255)
            device_name = gcmd.get('DEVICE', None)
            self.clear_prestage(
                device_name=device_name, tool=tool, gcmd=gcmd)
        except Exception as exc:
            raise self._normalized_command_pause_error(gcmd, exc)

    def cmd_SAVE_STATE(self, gcmd):
        self.state.save()
        gcmd.respond_info('BMCU state saved to %s' % self.state.path)

    def cmd_CLEAR_ERROR(self, gcmd):
        device = self._require_device(gcmd)
        device.reset_error()
        device.refresh()
        device.last_error = ''
        self.last_error = None
        self._sync_status_cache_runtime()
        gcmd.respond_info('%s error state cleared; physical snapshot refreshed' % device.name)

    def cmd_ANALYZE_PRINTER(self, gcmd):
        self.printer_analysis = compat.analyze_printer(
            self.printer, self.controller_mode)
        gcmd.respond_info(json.dumps(self.printer_analysis, indent=2, sort_keys=True))

    def _adopt_previous_print_routes(self):

        if not (self.print_loaded_routes or
                self.print_terminal_unload_pending):
            return []

        route_index = {}
        for device in self.devices:
            for channel in range(4):
                try:
                    aliases = self._journal_route_aliases(device, channel)
                except Exception:
                    continue
                for alias in aliases:
                    route_index[alias] = (device, channel)

        refreshed = {}
        adopted = []
        stale_aliases = set()
        endpoints = {}
        for route_key in sorted(set(self.print_loaded_routes)):
            route = route_index.get(route_key)
            if route is None:
                raise BMCUError(
                    'previous print route %s cannot be matched to connected '
                    'BMCU hardware; recovery is required before a new print' %
                    route_key)
            device, channel = route
            if not device.ready:
                raise BMCUError(
                    '%s is offline while previous print route %s is still '
                    'journalled; reconnect it before starting a new print' %
                    (device.name, route_key))
            if device.name not in refreshed:
                refreshed[device.name] = device.refresh()
            route_state = self._route_state(
                device, channel, refreshed[device.name])
            if route_state == protocol.ROUTE_EMPTY:
                stale_aliases.update(
                    self._journal_route_aliases(device, channel))
                continue
            if route_state != protocol.ROUTE_LOADED:
                raise BMCUError(
                    'previous print route %s is UNCERTAIN; confirm EMPTY or '
                    'LOADED before starting another print' % route_key)
            endpoint = self._endpoint_for_channel(device, channel)
            if endpoint is None:
                raise BMCUError(
                    'previous print route %s has no configured destination '
                    'Endpoint; restore its route before starting another print' %
                    route_key)
            occupied = endpoints.setdefault(endpoint.name, [])
            occupied.append((device, int(channel), route_key))
            adopted.append((device, int(channel), endpoint, route_key))

        conflicts = []
        for endpoint_name, occupied in sorted(endpoints.items()):
            if len(occupied) > 1:
                conflicts.append('%s: %s' % (
                    endpoint_name, ', '.join(
                        '%s Channel %d' % (device.name, channel + 1)
                        for device, channel, _key in occupied)))
        if conflicts:
            raise BMCUError(
                'previous print left multiple loaded routes on one Endpoint; '
                'manual recovery is required: %s' % '; '.join(conflicts))

        if stale_aliases:
            self.print_loaded_routes.difference_update(stale_aliases)

        self.print_route_journal_initialized = True

        self.print_terminal_unload_pending = bool(adopted)
        self._save_print_session()
        return adopted

    def cmd_PRINT_BEGIN(self, gcmd):
        self._require_no_u1_cross_refill_pending()
        self._require_standalone_operation('BMCU_PRINT_BEGIN', gcmd)
        schema = gcmd.get_int(
            'SCHEMA', PRINT_PLAN_SCHEMA, minval=1, maxval=999)
        if schema != PRINT_PLAN_SCHEMA:
            raise gcmd.error(
                'unsupported BMCU print-plan schema %d; required %d' %
                (schema, PRINT_PLAN_SCHEMA))
        is_snapmaker = bool(
            self.printer_analysis.get('features', {}).get('snapmaker_u1'))
        requested_job_id = gcmd.get('JOB', '')
        reset = bool(gcmd.get_int('RESET', 1, minval=0, maxval=1))
        if not reset:
            raise gcmd.error(
                'RESET=0 was removed: every public print must submit one '
                'complete atomic source plan with RESET=1')
        inherited_routes = []
        if reset:
            print_state = self._print_state()
            safe_reset_states = (
                'standby', 'ready', 'idle', 'complete', 'completed',
                'cancelled', 'canceled', 'error', 'failed')
            if (self.print_map_active and
                    print_state not in safe_reset_states):
                raise gcmd.error(
                    'cannot replace the active BMCU print plan while printer state is %s' %
                    (print_state or 'unknown'))
            restore_required = bool(self._u1_original or
                self._u1_map_backup or self._u1_used_backup or
                self._u1_end_unload_backup)
            if restore_required and not self._restore_u1_print_maps():
                raise gcmd.error(
                    'cannot open a new BMCU print plan: the previous U1 runtime map '
                    'has not been restored yet')
            try:
                inherited_routes = self._adopt_previous_print_routes()
            except Exception as exc:
                raise gcmd.error(str(exc))
            self.print_tools.clear()

            self.print_route_journal_initialized = True
            self.print_terminal_unload_pending = bool(inherited_routes)
            self.print_stock_reset_observed = False
            self.print_transaction_phase = ''
            self._u1_preextrude_primed_tools.clear()
            self._u1_prepared_heads.clear()
            self._initialize_u1_prepared_heads_from_physical_state(
                source='BMCU_PRINT_BEGIN', persist=False)
            self._reset_u1_lookahead(
                stop_jobs=True, reason='new BMCU print plan')
            self.refill.clear_print()
            self._refill_runout_latched.clear()
        self.print_job_id = requested_job_id
        self.print_plan_schema = PRINT_PLAN_SCHEMA
        self.print_plan_tools.clear()
        self.print_plan_required.clear()
        self.print_plan_open = True
        self.print_plan_interrupted = False

        self.print_map_active = False
        self._save_print_session()
        inherited_note = ''
        if inherited_routes:
            inherited_note = '; adopted %d confirmed loaded route%s for automatic reuse/switch' % (
                len(inherited_routes), '' if len(inherited_routes) == 1 else 's')
        gcmd.respond_info('BMCU print source plan opened%s%s' %
                          (((' job=' + self.print_job_id)
                            if self.print_job_id else ''),
                           inherited_note))

    def cmd_PRINT_REQUIRE(self, gcmd):
        self._require_standalone_operation('BMCU_PRINT_REQUIRE', gcmd)
        if not self.print_plan_open:
            raise gcmd.error('run BMCU_PRINT_BEGIN before BMCU_PRINT_REQUIRE')
        tool = gcmd.get_int('TOOL', minval=0, maxval=255)
        if (self.printer_analysis.get('features', {}).get('snapmaker_u1') and
                tool >= 32):
            raise gcmd.error('Snapmaker U1 accepts logical sources T0-T31')
        self.print_plan_required.add(tool)
        gcmd.respond_info(
            'BMCU requires an explicit source for logical T%d' % tool)

    def cmd_PRINT_MAP(self, gcmd):
        self._require_standalone_operation('BMCU_PRINT_MAP', gcmd)
        if not self.print_plan_open:
            raise gcmd.error('run BMCU_PRINT_BEGIN before BMCU_PRINT_MAP')
        tool = gcmd.get_int('TOOL', minval=0, maxval=255)
        is_u1 = bool(
            self.printer_analysis.get('features', {}).get('snapmaker_u1'))
        if is_u1 and tool >= 32:
            raise gcmd.error('Snapmaker U1 accepts logical sources T0-T31')

        native_head = gcmd.get_int('NATIVE_HEAD', None, minval=0, maxval=3)
        uid_arg = gcmd.get('UID', None)
        device_arg = gcmd.get('DEVICE', None)
        channel_arg = gcmd.get('CHANNEL', None)
        explicit_source = any(
            value is not None for value in (uid_arg, device_arg, channel_arg))
        expected_endpoint = str(
            gcmd.get('EXPECT_ENDPOINT', '') or '').strip()
        expected_material = str(
            gcmd.get('EXPECT_MATERIAL', '') or '').strip()
        expected_spool = gcmd.get_int(
            'EXPECT_SPOOL', None, minval=0, maxval=0x7FFFFFFF)
        slicer_material = gcmd.get('MATERIAL', None)
        slicer_color = gcmd.get('COLOR', None)
        raw_commandline = gcmd.get_commandline()
        raw_material = re.search(
            r'(?i)(?:^|\s)MATERIAL\s*=\s*"([^"\r\n]*)"',
            raw_commandline)
        raw_color = re.search(
            r"(?i)(?:^|\s)COLOR\s*=\s*[\"']?#?"
            r"([0-9a-f]{6})(?:[0-9a-f]{2})?[\"']?(?=\s|$)",
            raw_commandline)
        if raw_material is not None:
            slicer_material = raw_material.group(1)
        if raw_color is not None:
            slicer_color = raw_color.group(1)
        if slicer_material is not None:
            slicer_material = str(slicer_material or '').strip().upper()
            if (not slicer_material or len(slicer_material) > 40 or
                    any(ord(ch) < 32 or ord(ch) == 127
                        for ch in slicer_material)):
                slicer_material = None
            elif is_u1:
                try:
                    slicer_material = normalize_u1_material_name(
                        slicer_material)
                except Exception:
                    slicer_material = None
        if slicer_color is not None:
            slicer_color = str(slicer_color or '').strip().upper()
            if slicer_color.startswith('#'):
                slicer_color = slicer_color[1:]
            if re.fullmatch(r'[0-9A-F]{8}', slicer_color) is not None:
                slicer_color = slicer_color[:6]
            if re.fullmatch(r'[0-9A-F]{6}', slicer_color) is None:
                slicer_color = None
            else:
                slicer_color = '#' + slicer_color

        if native_head is not None:
            if slicer_material is not None or slicer_color is not None:
                raise gcmd.error(
                    'MATERIAL and COLOR synchronization applies only to BMCU sources')
            if explicit_source:
                raise gcmd.error(
                    'NATIVE_HEAD cannot be combined with UID, DEVICE or CHANNEL')
            if not is_u1:
                raise gcmd.error(
                    'NATIVE_HEAD is available only on Snapmaker U1')
            self.print_plan_required.add(tool)
            self.print_plan_tools[str(tool)] = {
                'native': True, 'head': int(native_head)}
            gcmd.respond_info(
                'BMCU staged logical T%d -> native U1 Head %d' %
                (tool, native_head + 1))
            return

        device = None
        channel = None
        if not explicit_source:

            if is_u1 and tool < U1_NATIVE_TOOL_COUNT:
                if slicer_material is not None or slicer_color is not None:
                    raise gcmd.error(
                        'MATERIAL and COLOR synchronization applies only to BMCU sources')
                self.print_plan_required.add(tool)
                self.print_plan_tools[str(tool)] = {
                    'native': True, 'head': int(tool)}
                gcmd.respond_info(
                    'BMCU staged logical T%d -> native U1 Head %d' %
                    (tool, tool + 1))
                return
            if not is_u1 and tool == GENERIC_EXTERNAL_TOOL:
                if slicer_material is not None or slicer_color is not None:
                    raise gcmd.error(
                        'MATERIAL and COLOR synchronization applies only to BMCU sources')
                endpoint = self._generic_external_endpoint()
                if endpoint is None:
                    raise gcmd.error(
                        'generic External T0 requires one configured generic extruder endpoint')
                self.print_plan_required.add(tool)
                self.print_plan_tools[str(tool)] = {
                    'external': True, 'endpoint': endpoint.name}
                gcmd.respond_info(
                    'BMCU staged logical T0 -> manual External on %s' %
                    endpoint.name)
                return
            matches = []
            for candidate in self.devices:
                for candidate_channel in range(4):
                    if self._virtual_tool_for_source(
                            candidate, candidate_channel) == tool:
                        matches.append((candidate, candidate_channel))
            if not matches:
                raise gcmd.error(
                    'logical T%d is not assigned to a configured BMCU source' %
                    tool)
            if len(matches) != 1:
                raise gcmd.error(
                    'logical T%d resolves to more than one BMCU source' % tool)
            device, channel = matches[0]
        else:
            if device_arg is not None:
                raise gcmd.error(
                    'print plans require immutable UID+CHANNEL, not DEVICE')
            uid = str(uid_arg or '').strip().upper()
            if (re.fullmatch(r'[0-9A-F]{24}', uid) is None or
                    uid in ('0' * 24, 'F' * 24)):
                if is_u1:
                    raise gcmd.error(
                        'use BMCU_PRINT_MAP TOOL=N on Snapmaker U1, or provide a valid UID+CHANNEL')
                raise gcmd.error(
                    'UID must be the 24-hex hardware UID shown by the BMCU panel')
            device = self.devices_by_uid.get(uid)
            if device is None:
                raise gcmd.error('unknown BMCU UID %s' % uid)
            channel = self._require_channel(gcmd)

        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise gcmd.error(
                '%s Channel %d is not connected to a printer head/extruder' %
                (device.name, channel + 1))
        if endpoint.driver == 'snapmaker_u1' and tool >= U1_LOGICAL_TOOL_LIMIT:
            raise gcmd.error('Snapmaker U1 accepts logical sources T0-T31')
        expected_tool = self._virtual_tool_for_source(device, channel)
        if expected_tool < 0:
            if is_u1:
                raise gcmd.error(
                    '%s Channel %d has no available U1 T number; U1 supports at most 28 BMCU Channels (T4-T31)' %
                    (device.name, channel + 1))
            raise gcmd.error(
                '%s Channel %d has no generic BMCU T assignment' %
                (device.name, channel + 1))
        if tool != expected_tool:
            raise gcmd.error(
                '%s Channel %d is assigned T%d, not T%d. Update the OrcaSlicer source mapping after changing a channel T assignment.' %
                (device.name, channel + 1, expected_tool, tool))

        metadata = self._channel_metadata(device, channel)
        if expected_endpoint and endpoint.name != expected_endpoint:
            raise gcmd.error(
                'T%d expected Endpoint %s, but %s Channel %d is routed to %s' %
                (tool, expected_endpoint, device.name, channel + 1,
                 endpoint.name))
        if (expected_material and
                str(metadata.get('material', '')).strip().upper() !=
                expected_material.upper()):
            raise gcmd.error(
                'T%d expected material %s, but %s Channel %d contains %s' %
                (tool, expected_material, device.name, channel + 1,
                 metadata.get('material', 'unknown')))
        if (expected_spool is not None and
                metadata.get('spool_id') != expected_spool):
            raise gcmd.error(
                'T%d expected spool %d, but %s Channel %d reports %s' %
                (tool, expected_spool, device.name, channel + 1,
                 metadata.get('spool_id')))

        mapping = self._mapping_payload(device, channel)
        if expected_endpoint:
            mapping['expected_endpoint'] = expected_endpoint
        if expected_material:
            mapping['expected_material'] = expected_material
        if expected_spool is not None:
            mapping['expected_spool'] = expected_spool
        if slicer_material is not None:
            mapping['slicer_material'] = slicer_material
        if slicer_color is not None:
            mapping['slicer_color'] = slicer_color
        self.print_plan_required.add(tool)
        self.print_plan_tools[str(tool)] = mapping
        gcmd.respond_info(
            'BMCU staged T%d -> %s Channel %d -> %s' %
            (tool, device.name, channel + 1, endpoint.name))

    def cmd_PRINT_COMMIT(self, gcmd):
        self._require_standalone_operation('BMCU_PRINT_COMMIT', gcmd)
        if not self.print_plan_open:
            raise gcmd.error('no open BMCU print source plan')
        if self.print_plan_interrupted:
            raise gcmd.error(
                'the open BMCU print plan was interrupted by a Klipper restart; '
                'restart the print so Begin/Map/Commit is emitted again')

        self._reconcile_u1_leases(self.reactor.monotonic(), force=True)
        is_u1 = bool(
            self.printer_analysis.get('features', {}).get('snapmaker_u1'))
        required_tools = set(self.print_plan_required)
        if is_u1:
            required_tools.update(self._u1_logical_tools_used())
        planned_tools = set(int(value) for value in self.print_plan_tools)
        missing = sorted(required_tools - planned_tools)
        if missing:
            raise gcmd.error(
                'BMCU print plan is incomplete; missing logical source%s %s' %
                ('s' if len(missing) != 1 else '',
                 ', '.join('T%d' % value for value in missing)))
        resolved = []
        for tool_text, mapping in sorted(
                self.print_plan_tools.items(), key=lambda item: int(item[0])):
            tool = int(tool_text)
            if mapping.get('external'):
                if is_u1 or tool != GENERIC_EXTERNAL_TOOL:
                    raise gcmd.error('External mapping is valid only as generic T0')
                endpoint = self._generic_external_endpoint()
                if endpoint is None:
                    raise gcmd.error(
                        'generic External T0 requires one configured generic extruder endpoint')
                expected_name = str(mapping.get('endpoint', '') or '')
                if expected_name and expected_name != endpoint.name:
                    raise gcmd.error(
                        'External T0 endpoint changed after staging: expected %s, got %s' %
                        (expected_name, endpoint.name))
                resolved.append((tool, None, None, None))
                continue
            if mapping.get('native'):
                if not self.printer_analysis.get('features', {}).get('snapmaker_u1'):
                    raise gcmd.error(
                        'T%d requests a native U1 head on a non-U1 printer' % tool)
                head = int(mapping.get('head', -1))
                endpoint = next(
                    (candidate for candidate in self.endpoints.values()
                     if candidate.driver == 'snapmaker_u1' and
                     int(candidate.get('head_index', -1)) == head), None)
                if endpoint is None:
                    raise gcmd.error(
                        'T%d native U1 Head %d endpoint is unavailable' %
                        (tool, head + 1))
                owner = self._u1_owner_status(endpoint)
                if owner.get('owner') not in ('native', 'native_busy'):
                    raise gcmd.error(
                        'T%d cannot use native U1 Head %d: owner is %s (%s)' %
                        (tool, head + 1, owner.get('owner', 'unknown'),
                         owner.get('reason', '')))
                resolved.append((tool, None, None, head))
                continue
            device = self._mapping_device(mapping)
            if device is None:
                raise gcmd.error(
                    'T%d source device %s is unavailable' %
                    (tool, mapping.get('device_uid') or mapping.get('device')))
            channel = int(mapping.get('channel', -1))
            endpoint = self._endpoint_for_channel(device, channel)
            if endpoint is None:
                raise gcmd.error(
                    'T%d source %s Channel %d has no physical destination' %
                    (tool, device.name, channel + 1))
            expected_endpoint = str(
                mapping.get('expected_endpoint', '') or '')
            if expected_endpoint and endpoint.name != expected_endpoint:
                raise gcmd.error(
                    'T%d route changed after staging: expected %s, got %s' %
                    (tool, expected_endpoint, endpoint.name))
            metadata = self._channel_metadata(device, channel)
            expected_material = str(
                mapping.get('expected_material', '') or '')
            if (expected_material and
                    str(metadata.get('material', '')).strip().upper() !=
                    expected_material.upper()):
                raise gcmd.error(
                    'T%d material changed after staging: expected %s, got %s' %
                    (tool, expected_material,
                     metadata.get('material', 'unknown')))
            if ('expected_spool' in mapping and
                    metadata.get('spool_id') != mapping.get('expected_spool')):
                raise gcmd.error(
                    'T%d spool changed after staging: expected %s, got %s' %
                    (tool, mapping.get('expected_spool'),
                     metadata.get('spool_id')))
            validation = endpoint.validate()
            if not validation.get('valid', False):
                raise gcmd.error(
                    'T%d destination %s is incomplete: %s' %
                    (tool, endpoint.name,
                     '; '.join(validation.get('errors', []))))
            try:
                self._check_automatic_ready(device, channel)
            except Exception as exc:
                raise gcmd.error('T%d preflight failed: %s' % (tool, exc))
            if endpoint.driver == 'snapmaker_u1':
                self._refresh_u1_disconnect_hazards(endpoint.name)
                owner = self._u1_owner_status(endpoint)
                owner_name = owner.get('owner', 'unknown')
                if (owner_name == 'safety_hold' and
                        not self._u1_disconnect_hazards.get(endpoint.name) and
                        not self._endpoint_has_loaded_or_active_route(
                            endpoint.name)):

                    self._reconcile_u1_leases(
                        self.reactor.monotonic(), force=True)
                    owner = self._u1_owner_status(endpoint)
                    owner_name = owner.get('owner', 'unknown')
                if owner_name == 'native':
                    path = endpoint.native_path_status()
                    if not path.get('known') or path.get('busy'):
                        raise gcmd.error(
                            'T%d cannot reserve %s: the native U1 path is not '
                            'positively empty (%s)' %
                            (tool, endpoint.name,
                             path.get('channel_state', 'unknown')))
                elif owner_name != 'bmcu':
                    raise gcmd.error(
                        'T%d cannot use %s: feeder owner is %s (%s)' %
                        (tool, endpoint.name, owner_name,
                         owner.get('reason', '')))
            resolved.append((tool, device, channel, None))

        metadata_snapshots = {}
        metadata_sync_devices = {}
        old_tools = copy.deepcopy(self.print_tools)
        old_original = copy.deepcopy(self._u1_original)
        old_phase = self.print_transaction_phase
        old_backup = copy.deepcopy(self._u1_map_backup)
        old_used = copy.deepcopy(self._u1_used_backup)
        old_end_unload = copy.deepcopy(self._u1_end_unload_backup)
        _task, u1_config = self._u1_task_config()
        u1_map_snapshot = (copy.deepcopy(u1_config.get('extruder_map_table'))
                           if u1_config is not None else None)
        u1_used_snapshot = (copy.deepcopy(u1_config.get('extruders_used'))
                            if u1_config is not None else None)
        u1_end_unload_present = bool(
            u1_config is not None and 'end_unload_filament' in u1_config)
        u1_end_unload_snapshot = (
            copy.deepcopy(u1_config.get('end_unload_filament'))
            if u1_end_unload_present else None)
        u1_flow_snapshot = (copy.deepcopy(
            u1_config.get('flow_calib_extruders'))
            if u1_config is not None else None)
        u1_replenished_snapshot = (copy.deepcopy(
            u1_config.get('extruders_replenished'))
            if u1_config is not None else None)
        u1_reprint_present = bool(
            u1_config is not None and isinstance(
                u1_config.get('reprint_info'), dict))
        u1_reprint_snapshot = (copy.deepcopy(
            u1_config.get('reprint_info'))
            if u1_reprint_present else None)
        try:
            for tool, device, channel, native_head in resolved:
                if device is None or native_head is not None:
                    continue
                mapping = self.print_plan_tools.get(str(tool), {})
                material = mapping.get('slicer_material')
                color = mapping.get('slicer_color')
                if material is None and color is None:
                    continue
                metadata = self._channel_metadata(device, channel)
                key = (device.name, int(channel))
                if key not in metadata_snapshots:
                    metadata_snapshots[key] = (
                        device, int(channel), copy.deepcopy(metadata))
                changed = False
                if material is not None and metadata.get('material') != material:
                    metadata['material'] = material
                    changed = True
                if color is not None and metadata.get('color') != color:
                    metadata['color'] = color
                    colors = list(metadata.get('colors', []) or [])
                    metadata['colors'] = ([color] + [
                        value for value in colors if value != color])[:5]
                    changed = True
                if changed or getattr(device, 'slot_sync_required', False):
                    metadata_sync_devices[device.name] = device

            if is_u1:

                self._load_u1_toolchange_plan()
                self._capture_u1_print_original()
            self.print_tools = copy.deepcopy(self.print_plan_tools)
            if is_u1:

                self._reset_u1_runtime_used_for_plan()
            for tool, device, channel, native_head in resolved:
                if native_head is not None:
                    self._set_u1_runtime_tool_map(
                        tool, native_head, remember=True)
                elif device is not None:
                    self._sync_u1_print_tool(
                        tool, device, channel, remember=True)
            self.print_plan_tools.clear()
            self.print_plan_required.clear()
            self.print_plan_open = False
            self.print_plan_interrupted = False
            self.print_map_active = True
            self.print_transaction_phase = 'active' if is_u1 else ''
            self.print_stock_reset_observed = False

            self.print_terminal_unload_pending = False
            self._save_print_session()
            if is_u1:
                self._persist_u1_print_task(
                    'BMCU committed U1 print map')
        except Exception as exc:
            for device, channel, snapshot in metadata_snapshots.values():
                metadata = self._channel_metadata(device, channel)
                metadata.clear()
                metadata.update(snapshot)
            if u1_config is not None:
                if u1_map_snapshot is not None:
                    u1_config['extruder_map_table'] = u1_map_snapshot
                if u1_used_snapshot is not None:
                    u1_config['extruders_used'] = u1_used_snapshot
                if u1_end_unload_present:
                    u1_config['end_unload_filament'] = u1_end_unload_snapshot
                else:
                    u1_config.pop('end_unload_filament', None)
                if u1_flow_snapshot is not None:
                    u1_config['flow_calib_extruders'] = u1_flow_snapshot
                if u1_replenished_snapshot is not None:
                    u1_config['extruders_replenished'] = u1_replenished_snapshot
                if u1_reprint_present:
                    u1_config['reprint_info'] = u1_reprint_snapshot
                else:
                    u1_config.pop('reprint_info', None)
            rollback_failed = None
            if is_u1 and u1_config is not None:
                try:
                    self._persist_u1_print_task(
                        'BMCU failed commit rollback')
                except Exception as rollback_exc:
                    rollback_failed = rollback_exc
            if rollback_failed is not None:

                self.print_tools.clear()
                self.print_plan_tools.clear()
                self.print_plan_required.clear()
                self.print_plan_open = False
                self.print_plan_interrupted = True
                self.print_map_active = False
                self.print_transaction_phase = 'recovery'
                self._save_print_session()
                raise gcmd.error(
                    'BMCU print plan failed and U1 rollback is pending: %s; '
                    'rollback: %s' % (exc, rollback_failed))
            self.print_tools = old_tools
            self._u1_original = old_original
            self.print_transaction_phase = old_phase
            self._u1_map_backup = old_backup
            self._u1_used_backup = old_used
            self._u1_end_unload_backup = old_end_unload
            self._save_print_session()
            raise gcmd.error('BMCU print plan commit failed: %s' % exc)

        if metadata_sync_devices:
            for device in metadata_sync_devices.values():
                device.slot_sync_required = True
                if not self._critical_motion_active:
                    self._sync_device_slots(self.reactor.monotonic(), device)
                if device.slot_sync_required:
                    self._queue_deferred_task(
                        'slot-sync:%s' % device.name,
                        lambda eventtime, d=device:
                        self._sync_device_slots(eventtime, d))
            for device in metadata_sync_devices.values():
                for channel in range(4):
                    self._sync_channel_status_cache(device, channel)

        gcmd.respond_info(
            'BMCU print source plan committed: %d logical source%s' %
            (len(resolved), '' if len(resolved) == 1 else 's'))

    def cmd_PRINT_PREEXTRUDE(self, gcmd):

        tool = gcmd.get_int('TOOL', minval=0, maxval=31)
        if not self.printer_analysis.get('features', {}).get('snapmaker_u1'):
            raise gcmd.error('BMCU_PRINT_PREEXTRUDE is only valid on Snapmaker U1')
        primed_at = self._u1_preextrude_primed_tools.pop(tool, None)
        fresh_prime = False
        if primed_at is not None:
            try:
                age = self.reactor.monotonic() - float(primed_at)
                fresh_prime = 0.0 <= age <= 60.0
            except (TypeError, ValueError, OverflowError):
                fresh_prime = False
        if fresh_prime:

            journal = 'unavailable'
            stats = self.printer.lookup_object('print_stats', None)
            config = getattr(stats, '_config', None)
            path = getattr(stats, '_config_path', None)
            job = config.get('print_job') if isinstance(config, dict) else None
            flags = (job.get('preextrude_filament')
                     if isinstance(job, dict) else None)
            if isinstance(flags, list) and tool < len(flags):
                previous = bool(flags[tool])
                flags[tool] = True
                journal = 'already-set' if previous else 'memory-only'
                if not previous and path:
                    try:
                        if self._persist_u1_print_stats(
                                'BMCU U1 pre-extrude journal'):
                            journal = 'persisted'
                        else:
                            self._record_error(
                                'U1_PREEXTRUDE_PERSIST_FAILED',
                                details='print-stats persistence returned false')
                    except Exception as exc:
                        self._record_error(
                            'U1_PREEXTRUDE_PERSIST_FAILED',
                            details=str(exc))
            message = (
                'BMCU skipped duplicate U1 pre-extrude for newly loaded T%d '
                '(journal=%s)' % (tool, journal))
            logging.info(message)
            gcmd.respond_info(message)
            return
        try:
            logging.info(
                'BMCU running stock U1 pre-extrude for already-loaded/native T%d',
                tool)
            self.gcode.run_script_from_command(
                'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d' % tool)
        except Exception as exc:
            if self._u1_background_jobs:
                try:
                    self._cancel_u1_background_jobs(
                        'foreground T%d pre-extrude failed: %s' % (tool, exc),
                        wait=True)
                except Exception:
                    logging.exception(
                        'BMCU could not stop Snapmaker background work after '
                        'pre-extrude failure')
            raise gcmd.error(
                'Snapmaker U1 pre-extrude failed for T%d: %s' % (tool, exc))

    @staticmethod
    def _coord_value(value, name, index):
        coordinate = getattr(value, name, None)
        if coordinate is not None:
            return float(coordinate)
        return float(value[index])

    def _u1_safe_terminal_lift(self):

        toolhead = self.printer.lookup_object('toolhead', None)
        gcode_move = self.printer.lookup_object('gcode_move', None)
        if toolhead is None or gcode_move is None:
            raise BMCUError('U1 terminal unload cannot verify toolhead position')
        now = self.reactor.monotonic()
        tool_status = toolhead.get_status(now)
        homed = str(tool_status.get('homed_axes', '') or '').lower()
        if 'z' not in homed:
            raise BMCUError(
                'U1 terminal unload requires a homed Z axis; filament was left loaded')
        move_status = gcode_move.get_status(now)
        position = (move_status.get('gcode_position') or
                    move_status.get('position'))
        if position is None:
            raise BMCUError('U1 terminal unload cannot read current Z position')
        current_z = self._coord_value(position, 'z', 2)
        maximum = tool_status.get('axis_maximum')
        maximum_z = (self._coord_value(maximum, 'z', 2)
                     if maximum is not None else 270.5)
        if not (math.isfinite(current_z) and math.isfinite(maximum_z)):
            raise BMCUError('U1 terminal unload received an invalid Z limit')
        if current_z < 195.0:
            target_z = min(200.0, maximum_z)
        elif current_z < min(265.0, maximum_z - 2.0):
            target_z = min(current_z + 5.0, maximum_z)
        elif current_z < maximum_z:
            target_z = min(current_z + 2.0, maximum_z)
        else:
            target_z = current_z
        if target_z <= current_z + 0.0001:
            return
        self.gcode.run_script_from_command(
            'SAVE_GCODE_STATE NAME=BMCU_SNAP_TERMINAL_LIFT')
        try:
            self.gcode.run_script_from_command(
                'G90\nG0 Z%.3f F2000\nM400' % target_z)
        finally:
            self.gcode.run_script_from_command(
                'RESTORE_GCODE_STATE NAME=BMCU_SNAP_TERMINAL_LIFT MOVE=0')

    def _unload_all_loaded_for_print_end(self):
        self._require_standalone_operation('BMCU_PRINT_END')

        route_index = {}
        for device in self.devices:
            for channel in range(4):
                route = (device, channel)
                for alias in self._journal_route_aliases(device, channel):
                    route_index[alias] = route
        route_keys = set(self.print_loaded_routes)
        candidates = []
        for route_key in sorted(route_keys):
            route = route_index.get(route_key)
            if route is None:
                raise BMCUError(
                    'PRINT_END journal route %s is unavailable; it was kept for recovery' %
                    route_key)
            device, channel = route
            endpoint = self._endpoint_for_channel(device, channel)
            if endpoint is None:
                raise BMCUError(
                    'PRINT_END route %s has no physical Endpoint; journal kept for recovery' %
                    route_key)
            candidates.append((device, channel, endpoint))
        if any(endpoint.driver == 'snapmaker_u1'
               for _device, _channel, endpoint in candidates):
            self._u1_safe_terminal_lift()
        unloaded = 0
        for device, channel, endpoint in candidates:
            route_state = self._route_state(device, channel)
            if route_state == protocol.ROUTE_EMPTY:
                self._unmark_print_route(device, channel)
                continue
            if route_state != protocol.ROUTE_LOADED:
                raise BMCUError(
                    'PRINT_END route %s is %s, not safely unloadable' %
                    (self._journal_route_key(device, channel), route_state))
            self._validate_endpoint_for_operation(endpoint)
            self._lock(device, endpoint, 'PRINT_END_UNLOAD Channel %d' %
                       (channel + 1), channel=channel)
            emptied = False
            try:
                loaded_tool = self.loaded_tools.get(
                    self._route_key(device, channel), -1)
                temperature_profile = (
                    self._u1_temperature_profile(tool=loaded_tool)
                    if endpoint.driver == 'snapmaker_u1' and loaded_tool >= 0
                    else None)
                if self._snapmaker_ungripped_tail(
                        device, channel, endpoint):
                    self._prepare_snapmaker_ungripped_tail_handoff_locked(
                        device, endpoint, channel,
                        temperature_profile=temperature_profile,
                        restore_heater=True)
                    raise BMCUError(
                        'PRINT_END cannot prove this detached Snapmaker hotend '
                        'EMPTY without a follower filament; the route remains '
                        'reserved for the next load')
                else:
                    terminal_u1 = endpoint.driver == 'snapmaker_u1'
                    self._unload_locked(
                        device, endpoint, channel,

                        release_endpoint=not terminal_u1,
                        retain_selected_endpoint=terminal_u1,
                        temperature_profile=temperature_profile)
                emptied = True
            except Exception as exc:
                phase = self.active_operations.get(
                    device.name, {}).get('phase', 'PRINT_END_UNLOAD')
                self._stop_on_failure(
                    device, endpoint, exc, phase, channel)
                raise
            finally:
                self._unlock(device, endpoint)
            if emptied:
                unloaded += 1
                self._release_u1_persistent_hold_if_safe(
                    endpoint, 'print-end unload confirmed BMCU route EMPTY')
        self.print_terminal_unload_pending = bool(self.print_loaded_routes)
        self._save_print_session()
        return unloaded

    def cmd_PRINT_END(self, gcmd):
        mode = str(gcmd.get('MODE', '') or '').strip().upper()
        if mode:
            if mode not in ('AUTO', 'UNLOAD', 'KEEP'):
                raise gcmd.error('MODE must be AUTO, UNLOAD or KEEP')
            unload_requested = (
                mode == 'UNLOAD' or
                (mode == 'AUTO' and not self._leave_final_filament_loaded()))
        else:

            raw_unload = gcmd.get('UNLOAD', None)
            if raw_unload is None:
                mode = 'AUTO'
                unload_requested = not self._leave_final_filament_loaded()
            else:
                unload_requested = bool(gcmd.get_int(
                    'UNLOAD', 1, minval=0, maxval=1))
        unloaded = 0
        unload_error = None
        if self._u1_background_jobs:
            try:
                self._drain_u1_background_jobs(clear_ready=True)
            except Exception as exc:
                unload_error = str(exc)
                self._record_error(
                    'PRINT_END_BACKGROUND_FAILED', phase='PRINT_END_BACKGROUND',
                    details=unload_error)
        if unload_requested and unload_error is None:
            try:
                unloaded = self._unload_all_loaded_for_print_end()
            except Exception as exc:

                unload_error = str(exc)
                self.print_terminal_unload_pending = True
                self._save_print_session()
                self._record_error(
                    'PRINT_END_UNLOAD_FAILED', phase='PRINT_END_UNLOAD',
                    details=unload_error)
                logging.exception(
                    'BMCU terminal unload failed; stock PRINT_END must continue')
        elif self.print_loaded_routes:
            self.print_terminal_unload_pending = True
            self._save_print_session()
        clear = bool(gcmd.get_int('CLEAR', 1, minval=0, maxval=1))
        if unload_error:
            clear = False
        if (clear and self._u1_end_unload_backup and
                self._print_state() in ('printing', 'paused', 'pause')):

            clear = False
        if clear:
            self._clear_print_session('BMCU_PRINT_END')
            clear = not bool(
                self.print_map_active or self.print_tools or
                self.print_plan_tools or self.print_plan_required or
                self.print_plan_open or self.print_transaction_phase or
                self._u1_original or self._u1_map_backup or
                self._u1_used_backup or self._u1_end_unload_backup or
                self._u1_background_jobs)
            if not clear:
                cleanup_error = (
                    'BMCU print source-map cleanup did not complete; durable '
                    'recovery state was preserved')
                unload_error = unload_error or cleanup_error
                self._record_error(
                    'PRINT_END_CLEANUP_FAILED', phase='PRINT_END_CLEANUP',
                    details=cleanup_error)
        else:
            self.print_map_active = False
            self.print_job_id = ''
            self.print_plan_tools.clear()
            self.print_plan_required.clear()
            self.print_plan_open = False
            self.print_plan_interrupted = False
            self._print_session_seen_active = False
            self._u1_prepared_heads.clear()
            self._save_print_session()
        if unload_error:
            gcmd.respond_info(
                'BMCU print end continued with stock shutdown; unloaded=%d, '
                'remaining routes require recovery: %s' %
                (unloaded, unload_error))
        else:
            if (not unload_requested and self.print_loaded_routes):
                policy = ('automatic keep policy' if mode == 'AUTO' else
                          'explicit KEEP mode')
                gcmd.respond_info(
                    'BMCU print ended; unloaded=0, final confirmed route(s) '
                    'left loaded by %s' % policy)
            else:
                gcmd.respond_info(
                    'BMCU print ended; unloaded=%d%s' %
                    (unloaded, ' and source map cleared' if clear else ''))

    def cmd_REFILL_STATUS(self, gcmd):
        gcmd.respond_info(json.dumps(self.refill.status(), indent=2, sort_keys=True))

    def cmd_REFILL_NOW(self, gcmd):
        self._require_standalone_operation('BMCU_REFILL_NOW', gcmd)
        tool = gcmd.get_int('TOOL', minval=0, maxval=255)
        if not self.refill.trigger_now(tool):
            raise gcmd.error(
                'BMCU_REFILL_NOW was not scheduled: printing must be active, '
                'tail monitoring enabled and no refill already pending')
        gcmd.respond_info('BMCU auto refill scheduled for T%d' % tool)

    def cmd_PRINT_REFILL(self, gcmd):
        self._require_standalone_operation('BMCU_PRINT_REFILL', gcmd)
        source = gcmd.get_int('TOOL', minval=0, maxval=255)
        backup = gcmd.get_int('BACKUP', minval=0, maxval=255)
        priority = gcmd.get_int('PRIORITY', 100, minval=0, maxval=10000)
        if source == backup:
            raise gcmd.error('refill backup must be a different logical tool')
        source_device, source_channel, source_endpoint = self._resolve_tool(source)
        backup_device, backup_channel, backup_endpoint = self._resolve_tool(backup)
        if (source_device.name == backup_device.name and
                int(source_channel) == int(backup_channel)):
            raise gcmd.error('refill backup resolves to the same physical Channel')
        plan = self.refill_route_plan(source_endpoint, backup_endpoint)
        if not plan.get('supported'):
            raise gcmd.error(
                'refill route is unsafe: %s' %
                plan.get('reason', 'cross-endpoint refill is unsupported'))
        self.refill.set_print_backup(source, backup, priority)
        self._save_print_session()
        gcmd.respond_info('BMCU print refill T%d -> T%d priority=%d' %
                          (source, backup, priority))

    @staticmethod
    def _lighting_color(value, name, gcmd):
        color = str(value or '').strip().upper()
        if not color.startswith('#'):
            color = '#' + color
        if not re.match(r'^#[0-9A-F]{6}$', color):
            raise gcmd.error('%s must be RRGGBB or #RRGGBB' % name)
        return color

    def _lighting_from_gcmd(self, gcmd, current):
        lighting = copy.deepcopy(current)
        if gcmd.get_int('DEFAULTS', 0, minval=0, maxval=1):
            lighting = copy.deepcopy(DEFAULT_LIGHTING)
        scalar = (
            ('SYSTEM_BRIGHTNESS', 'system_brightness', 0, 255),
            ('FILAMENT_BRIGHTNESS', 'filament_brightness', 0, 255),
        )
        for parameter, key, minimum, maximum in scalar:
            raw = gcmd.get(parameter, None)
            if raw is not None:
                lighting[key] = gcmd.get_int(
                    parameter, minval=minimum, maxval=maximum)
        color_parameters = {
            'SYSTEM_COLOR': ('system_color', None),
            'BUFFER_MIN_COLOR': ('buffer_colors', 'minimum'),
            'BUFFER_NEUTRAL_COLOR': ('buffer_colors', 'neutral'),
            'BUFFER_MAX_COLOR': ('buffer_colors', 'maximum'),
            'STATUS_IDLE_COLOR': ('status_colors', 'idle'),
            'STATUS_BEFORE_LOAD_COLOR': ('status_colors', 'before_load'),
            'STATUS_LOADING_COLOR': ('status_colors', 'loading'),
            'STATUS_ACTIVE_COLOR': ('status_colors', 'active'),
            'STATUS_BEFORE_UNLOAD_COLOR': ('status_colors', 'before_unload'),
            'STATUS_RETRACTING_COLOR': ('status_colors', 'unloading'),

            'STATUS_UNLOADING_COLOR': ('status_colors', 'unloading'),
            'STATUS_PULLBACK_COLOR': ('status_colors', 'unloading'),
            'STATUS_ERROR_COLOR': ('status_colors', 'error'),
            'STATUS_EMPTY_COLOR': ('status_colors', 'empty'),
        }
        for parameter, (group, key) in color_parameters.items():
            raw = gcmd.get(parameter, None)
            if raw is None:
                continue
            color = self._lighting_color(raw, parameter, gcmd)
            if key is None:
                lighting[group] = color
            else:
                lighting.setdefault(group, {})[key] = color
        status_colors = lighting.setdefault('status_colors', {})
        status_colors['redetect'] = DEFAULT_LIGHTING['status_colors']['redetect']
        status_colors['pullback'] = status_colors.get(
            'unloading', DEFAULT_LIGHTING['status_colors']['unloading'])
        return lighting

    def _apply_lighting(self, device, state, lighting, persist, gcmd):
        previous_lighting = copy.deepcopy(self._lighting_config(state))
        previous_legacy = state.get('system_led_color', '#FFFFFF')
        if persist:
            state['lighting'] = copy.deepcopy(lighting)
            state['system_led_color'] = lighting['system_color']
            try:
                self.state.save()
            except Exception as exc:
                state['lighting'] = previous_lighting
                state['system_led_color'] = previous_legacy
                raise gcmd.error(str(exc))
        if not device.ready or not device.runtime_configured:
            if not persist:
                raise gcmd.error(
                    '%s is offline; temporary lighting changes require a ready BMCU' %
                    device.name)
            return 'saved; runtime sync pending'
        try:
            device.set_lighting(lighting)
            device.lighting_runtime_error = ''
            return 'saved and applied' if persist else 'applied in RAM'
        except Exception as exc:
            device.lighting_runtime_error = str(exc)
            if not persist:
                raise gcmd.error(str(exc))
            logging.exception(
                'BMCU %s lighting saved; runtime sync deferred', device.name)
            return 'saved; runtime sync deferred'

    @staticmethod
    def _lighting_profile_name(value, gcmd):
        name = str(value or '').strip()
        if name.upper() == 'DEFAULT':
            return 'DEFAULT'
        if (not name or len(name) > 40 or
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._+()-]*', name) is None):
            raise gcmd.error(
                'lighting profile name must use 1..40 letters, numbers, spaces, dot, +, parentheses, _ or -')
        return name

    def cmd_LED_PREVIEW(self, gcmd):
        device = self._require_device(gcmd)
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error('LED preview is unavailable during a print')
        if self.active_operations:
            raise gcmd.error('LED preview is unavailable during BMCU motion')
        if not self._led_preview_runtime_supported(device):
            raise gcmd.error('%s firmware does not support live LED preview' % device.name)
        target_name = str(gcmd.get('TARGET', '') or '').strip().upper()
        if target_name == 'FILAMENT':
            if not self._led_filament_preview_runtime_supported(device):
                raise gcmd.error('%s firmware does not support filament colour preview' % device.name)
            device.preview_led(3, gcmd.get_int('SCALE', 96, minval=0, maxval=255), 0, 0)
            return
        targets = {'STATUS': 0, 'SECOND': 1, 'BOARD': 2}
        if target_name not in targets:
            raise gcmd.error('TARGET must be STATUS, SECOND, BOARD or FILAMENT')
        color = self._lighting_color(gcmd.get('COLOR', ''), 'COLOR', gcmd)
        red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
        scale = gcmd.get_int('SCALE', 255, minval=0, maxval=255)
        red = (red * (scale + 1)) >> 8
        green = (green * (scale + 1)) >> 8
        blue = (blue * (scale + 1)) >> 8
        device.preview_led(targets[target_name], red, green, blue)

    def cmd_LIGHTING_PROFILE(self, gcmd):
        device = self._require_device(gcmd)
        state = self._device_state(device)
        action = str(gcmd.get('ACTION', 'GET') or 'GET').strip().upper()
        if action not in ('GET', 'SET', 'APPLY', 'DELETE'):
            raise gcmd.error('ACTION must be GET, SET, APPLY or DELETE')
        profile = self._lighting_profile_name(
            gcmd.get('PROFILE', 'DEFAULT'), gcmd)
        profiles = state.setdefault('lighting_profiles', {})
        if not isinstance(profiles, dict):
            profiles = {}
            state['lighting_profiles'] = profiles
        original_state = {
            'lighting': copy.deepcopy(state.get('lighting')),
            'system_led_color': state.get('system_led_color'),
            'lighting_profile': state.get('lighting_profile', 'DEFAULT'),
            'lighting_profiles': copy.deepcopy(profiles),
        }
        if action == 'GET':
            gcmd.respond_info(json.dumps({
                'active': state.get('lighting_profile', 'DEFAULT'),
                'default': DEFAULT_LIGHTING,
                'profiles': profiles,
            }, sort_keys=True))
            return
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error('lighting profiles cannot change during a print')
        if self.active_operations:
            raise gcmd.error('lighting profiles cannot change during BMCU motion')

        try:
            if action == 'DELETE':
                if profile == 'DEFAULT':
                    raise gcmd.error('the Default lighting profile cannot be deleted')
                if profile not in profiles:
                    raise gcmd.error('lighting profile %s does not exist' % profile)
                was_active = str(state.get('lighting_profile', 'DEFAULT')) == profile
                profiles.pop(profile, None)
                if was_active:
                    state['lighting_profile'] = 'DEFAULT'
                    note = self._apply_lighting(
                        device, state, copy.deepcopy(DEFAULT_LIGHTING), True, gcmd)
                else:
                    self.state.save()
                    note = 'deleted'
                gcmd.respond_info(
                    '%s lighting profile %s deleted (%s)' %
                    (device.name, profile, note))
                return

            if action == 'APPLY':
                if profile == 'DEFAULT':
                    lighting = copy.deepcopy(DEFAULT_LIGHTING)
                else:
                    if profile not in profiles:
                        raise gcmd.error('lighting profile %s does not exist' % profile)
                    lighting = copy.deepcopy(profiles[profile])
            else:
                if profile == 'DEFAULT':
                    raise gcmd.error(
                        'the Default lighting profile is immutable; add a named profile to customize lighting')
                if profile not in profiles and len(profiles) >= MAX_LIGHTING_PROFILES:
                    raise gcmd.error(
                        'BMCU supports at most %d custom lighting profiles per module' %
                        MAX_LIGHTING_PROFILES)
                base_lighting = (
                    copy.deepcopy(profiles[profile])
                    if profile in profiles else self._lighting_config(state))
                lighting = self._lighting_from_gcmd(gcmd, base_lighting)
                profiles[profile] = copy.deepcopy(lighting)

            state['lighting_profile'] = profile
            note = self._apply_lighting(device, state, lighting, True, gcmd)
            gcmd.respond_info(
                '%s lighting profile %s %s (%s)' %
                (device.name, profile,
                 'saved and selected' if action == 'SET' else 'selected', note))
        except Exception:
            state['lighting'] = original_state['lighting']
            state['system_led_color'] = original_state['system_led_color']
            state['lighting_profile'] = original_state['lighting_profile']
            state['lighting_profiles'] = original_state['lighting_profiles']
            raise

    def cmd_LIGHTING(self, gcmd):
        device = self._require_device(gcmd)
        state = self._device_state(device)
        current = self._lighting_config(state)
        apply_saved = bool(gcmd.get_int('APPLY_SAVED', 0, minval=0, maxval=1))
        persist = bool(gcmd.get_int('SAVE', 1, minval=0, maxval=1))
        lighting = current if apply_saved else self._lighting_from_gcmd(gcmd, current)
        note = self._apply_lighting(
            device, state, lighting, persist, gcmd)
        gcmd.respond_info(
            '%s lighting %s: %s' %
            (device.name, note, json.dumps(lighting, sort_keys=True)))

    def cmd_LED(self, gcmd):
        device = self._require_device(gcmd)
        state = self._device_state(device)
        lighting = self._lighting_config(state)
        color = gcmd.get('COLOR', lighting.get('system_color', '#FFFFFF'))
        lighting['system_color'] = self._lighting_color(color, 'COLOR', gcmd)
        raw_brightness = gcmd.get('BRIGHTNESS', None)
        if raw_brightness is not None:
            lighting['system_brightness'] = gcmd.get_int(
                'BRIGHTNESS', minval=0, maxval=255)
        persist = bool(gcmd.get_int('SAVE', 1, minval=0, maxval=1))
        note = self._apply_lighting(device, state, lighting, persist, gcmd)
        gcmd.respond_info(
            '%s system light color=%s brightness=%d (%s; Klipper-owned RAM policy)' %
            (device.name, lighting['system_color'],
             lighting['system_brightness'], note))

    def cmd_HANDOFF(self, gcmd):
        device = self._require_device(gcmd)
        target = self._finite_gcmd_float(
            gcmd, 'TARGET', None, minval=60.0, maxval=98.0)
        if target is None:
            raise gcmd.error('BMCU_HANDOFF requires TARGET=60..98')
        target = float(int(round(target)))
        state = self._device_state(device)
        stored = state.setdefault('motion_config', {})
        stored['loading_handoff_pct'] = target
        self.state.save()
        gcmd.respond_info(
            '%s loading handoff threshold saved at %.0f%%' %
            (device.name, target))

    def cmd_PRESSURE(self, gcmd):
        device = self._require_device(gcmd)
        target = self._finite_gcmd_float(
            gcmd, 'TARGET', None, minval=75.0, maxval=95.0)
        if target is None:
            raise gcmd.error('BMCU_PRESSURE requires TARGET=75..95')
        target = float(int(round(target)))
        persist = bool(gcmd.get_int('SAVE', 1, minval=0, maxval=1))
        supported = self._load_pressure_runtime_supported(device)
        if not persist and not supported:
            raise gcmd.error(
                '%s firmware does not support continuous 75..95%% pressure; '
                'flash the matching package firmware before SAVE=0' %
                device.name)

        state = self._device_state(device)
        stored = state.setdefault('motion_config', {})
        if persist:
            stored['load_pressure_pct'] = target
            self.state.save()
            runtime_note = 'saved; runtime sync pending'
            if device.ready and device.runtime_configured:
                try:
                    self._send_device_runtime_config(device)
                    if supported:
                        runtime_note = 'saved and applied'
                    else:
                        _legacy, effective = (
                            self._legacy_load_profile_for_pressure(target))
                        runtime_note = (
                            'saved; matching firmware required '
                            '(temporary legacy controller %.0f%%)' % effective)
                except Exception:
                    device.runtime_configured = False
                    if not device.runtime_config_sync_pending:
                        device.runtime_config_sync_pending = True
                        self._queue_deferred_task(
                            'runtime-sync:%s' % device.name,
                            lambda eventtime, d=device:
                            self._sync_device_runtime_config(eventtime, d))
                    logging.exception(
                        'BMCU %s pressure target saved; runtime sync deferred',
                        device.name)
            elif not supported:
                runtime_note = 'saved; matching firmware required'
        else:
            device.config_set(protocol.CONFIG_LOAD_PRESSURE_PCT, target)
            device.motion_config[protocol.CONFIG_LOAD_PRESSURE_PCT] = target
            runtime_note = 'applied in RAM'

        gcmd.respond_info(
            '%s load pressure target %.0f%% (%s)' %
            (device.name, target, runtime_note))

    def cmd_SPEED(self, gcmd):
        device = self._require_device(gcmd)
        requested = {
            'load_speed_mms': self._finite_gcmd_float(gcmd, 'LOAD', None, minval=10.0, maxval=120.0),
            'pull_speed_mms': self._finite_gcmd_float(gcmd, 'PULL', None, minval=10.0, maxval=120.0),
            'pull_speed_end_mms': self._finite_gcmd_float(gcmd, 'PULL_END', None, minval=4.0, maxval=40.0),
        }
        persist = bool(gcmd.get_int('SAVE', 1, minval=0, maxval=1))
        state = self._device_state(device)
        stored = state.setdefault('motion_config', {})
        changed = False
        for name, value in requested.items():
            if value is not None:
                if persist:
                    stored[name] = float(value)
                changed = True
        values = self._motion_wire_values(state)
        runtime_note = 'unchanged'
        if changed:
            if persist:
                self.state.save()
                device.motion_config = dict(values)
                runtime_note = 'saved; runtime sync pending'
                if device.ready and device.runtime_configured:
                    try:

                        self._send_device_runtime_config(device)
                        runtime_note = 'saved and applied'
                    except Exception:
                        device.runtime_configured = False
                        if not device.runtime_config_sync_pending:
                            device.runtime_config_sync_pending = True
                            self._queue_deferred_task(
                                'runtime-sync:%s' % device.name,
                                lambda eventtime, d=device:
                                self._sync_device_runtime_config(eventtime, d))
                        logging.exception(
                            'BMCU %s speed policy saved; runtime sync deferred',
                            device.name)
                else:
                    device.runtime_configured = False
            else:
                if not device.ready or not device.runtime_configured:
                    raise gcmd.error('%s is offline; SAVE=0 cannot apply a RAM-only speed' % device.name)
                reverse = {
                    'load_speed_mms': protocol.CONFIG_LOAD_SPEED_MMS,
                    'pull_speed_mms': protocol.CONFIG_PULL_SPEED_MMS,
                    'pull_speed_end_mms': protocol.CONFIG_PULL_SPEED_END_MMS,
                }
                for name, key in reverse.items():
                    if requested.get(name) is not None:
                        device.config_set(key, requested[name])
        current = {
            'load_speed_mms': device.motion_config.get(protocol.CONFIG_LOAD_SPEED_MMS),
            'pull_speed_mms': device.motion_config.get(protocol.CONFIG_PULL_SPEED_MMS),
            'pull_speed_end_mms': device.motion_config.get(protocol.CONFIG_PULL_SPEED_END_MMS),
            'stored_in': 'Klipper bmcu_state.json' if persist else 'BMCU RAM for this connection',
            'result': runtime_note if persist else 'applied in RAM',
        }
        gcmd.respond_info('%s speed config: %s' %
                          (device.name, json.dumps(current, sort_keys=True)))

    def _firmware_update_power_name(self):
        configured = str(self.firmware_update_power_pin or '').strip()
        if not configured:
            return ''
        return configured

    def _firmware_update_power_object(self):
        name = self._firmware_update_power_name()
        if not name:
            return '', None
        return name, self.printer.lookup_object('output_pin %s' % name, None)

    def _firmware_update_power_off(self, device):
        name, output = self._firmware_update_power_object()
        configured = str(self.firmware_update_power_pin or '').strip()
        if output is None:
            if configured:
                raise BMCUError(
                    'configured firmware_update_power_pin %s does not exist' %
                    configured)
            return ''
        live = transport.control_request(
            device.control_path, transport.CTRL_STATUS, pause=self.reactor.pause)
        if live.get('name') != device.name or not live.get('serial_released'):
            raise BMCUError('BMCU serial release is not confirmed before motor power-off')
        port = os.path.realpath(str(live.get('port') or ''))
        before = os.stat(port)
        if not stat.S_ISCHR(before.st_mode):
            raise BMCUError('BMCU runtime serial device is unavailable before motor power-off')
        current = output.get_status(self.reactor.monotonic()).get('value', 1.0)
        try:
            current = float(current)
        except (TypeError, ValueError):
            current = 1.0
        self._firmware_update_power_restore[device.name] = {
            'pin': name, 'value': current}
        try:
            self.gcode.run_script_from_command(
                'SET_PIN PIN=%s VALUE=0' % name)
            self.reactor.pause(
                self.reactor.monotonic() +
                self.firmware_update_power_settle_time)

            after = os.stat(port)
            if ((before.st_rdev, before.st_dev, before.st_ino) !=
                    (after.st_rdev, after.st_dev, after.st_ino)):
                raise BMCUError(
                    'output_pin %s removed the BMCU serial device; it does not isolate only motor power' %
                    name)
        except Exception:
            try:
                self._firmware_update_power_restore_device(device)
            except Exception:
                logging.exception(
                    'BMCU %s could not restore firmware-update power after preflight failure',
                    device.name)
            raise
        return name

    def _firmware_update_power_restore_device(self, device):
        record = self._firmware_update_power_restore.get(device.name)
        if not isinstance(record, dict):
            return
        name = str(record.get('pin', '') or '')
        try:
            value = float(record.get('value', 1.0))
        except (TypeError, ValueError):
            value = 1.0
        if not name:
            self._firmware_update_power_restore.pop(device.name, None)
            return
        self.gcode.run_script_from_command(
            'SET_PIN PIN=%s VALUE=%.6f' % (name, value))
        self.reactor.pause(
            self.reactor.monotonic() + self.firmware_update_power_settle_time)
        self._firmware_update_power_restore.pop(device.name, None)

    def _firmware_update_export_nvm(self, device, token):
        if not re.fullmatch(r'[0-9a-f]{32}', token):
            raise BMCUError('invalid firmware-update export token')
        if not device.ready or not device.runtime_configured or device.suspended:
            raise BMCUError('%s is not ready for NVM export' % device.name)
        if (self._print_state() in ('printing', 'paused', 'pause') or
                self.active_operations):
            raise BMCUError('Finish printing and BMCU operations before NVM export')
        update_prepared = False
        try:

            device.update_prepare()
            update_prepared = True
            self.reactor.pause(self.reactor.monotonic() + 0.05)
            prepared_status = dict(device.refresh())
            if any(int(value) != protocol.MOTION_IDLE
                   for value in prepared_status.get('motion', [])):
                raise BMCUError(
                    'BMCU motion did not stop before NVM export')

            root = os.path.join(
                os.path.dirname(os.path.realpath(self.state.path)),
                'bmcu', 'update', 'exports')
            current = os.path.dirname(os.path.realpath(self.state.path))
            for component in ('bmcu', 'update', 'exports'):
                current = os.path.join(current, component)
                if os.path.lexists(current):
                    info = os.lstat(current)
                    if (not stat.S_ISDIR(info.st_mode) or
                            stat.S_ISLNK(info.st_mode)):
                        raise BMCUError(
                            'unsafe firmware-update export directory: %s' %
                            current)
                else:
                    os.mkdir(current, 0o700)
                try:
                    os.chmod(current, 0o700)
                except OSError:
                    pass

            payload = bytearray()
            expected_crc = None
            offset = 0
            while offset < 4096:
                chunks = []
                cursor = offset
                while cursor < 4096 and len(chunks) < 4:
                    amount = min(224, 4096 - cursor)
                    chunks.append((cursor, amount))
                    cursor += amount
                try:
                    results = device.nvm_read_batch(chunks, timeout=15.0)
                except Exception as batch_exc:
                    if 'NVM batch timeout' not in str(batch_exc):
                        raise BMCUError(
                            'batched NVM read failed at offset=%d: %s' %
                            (offset, batch_exc))
                    logging.warning(
                        'BMCU %s batched NVM read timed out at offset=%d; '
                        'falling back to bounded single-chunk reads: %s',
                        device.name, offset, batch_exc)
                    results = []
                    for chunk_offset, amount in chunks:
                        result = None
                        for attempt in range(1, 4):
                            try:
                                result = device.nvm_read(
                                    chunk_offset, amount, timeout=15.0)
                                break
                            except Exception as exc:
                                timeout_error = (
                                    'request timeout type=0x61' in str(exc))
                                if not timeout_error or attempt >= 3:
                                    raise BMCUError(
                                        'NVM read offset=%d length=%d: %s' %
                                        (chunk_offset, amount, exc))
                                logging.warning(
                                    'BMCU %s NVM read timeout at offset=%d '
                                    'length=%d; retry %d/3',
                                    device.name, chunk_offset, amount,
                                    attempt + 1)
                                self.reactor.pause(
                                    self.reactor.monotonic() + 0.050 * attempt)
                        if result is None:
                            raise BMCUError(
                                'NVM read offset=%d length=%d produced no result' %
                                (chunk_offset, amount))
                        results.append(result)
                if len(results) != len(chunks):
                    raise BMCUError('incomplete batched NVM export response')
                for (chunk_offset, amount), result in zip(chunks, results):
                    if (int(result.get('offset', -1)) != chunk_offset or
                            int(result.get('length', -1)) != amount):
                        raise BMCUError(
                            'invalid NVM export chunk at offset %d' %
                            chunk_offset)
                    chunk = bytes(result.get('data', b''))
                    if len(chunk) != amount:
                        raise BMCUError(
                            'short NVM export chunk at offset %d' %
                            chunk_offset)
                    total_crc = int(
                        result.get('total_crc', -1)) & 0xffffffff
                    if expected_crc is None:
                        expected_crc = total_crc
                    elif total_crc != expected_crc:
                        raise BMCUError(
                            'NVM changed during export; retry the update')
                    payload.extend(chunk)
                offset = cursor
            actual_crc = binascii.crc32(payload) & 0xffffffff
            if expected_crc is None or actual_crc != expected_crc:
                raise BMCUError('NVM export CRC mismatch')
            uid = str(device.uid or '').upper()
            if not re.fullmatch(r'[0-9A-F]{24}', uid):
                raise BMCUError(
                    'invalid BMCU runtime UID during NVM export')
            binary_path = os.path.join(root, token + '.bin')
            metadata_path = os.path.join(root, token + '.json')
            if (os.path.lexists(binary_path) or
                    os.path.lexists(metadata_path)):
                raise BMCUError(
                    'firmware-update export token already exists')
            metadata = {
                'schema': 1, 'uid': uid, 'size': len(payload),
                'crc32': '%08X' % actual_crc,
                'sha256': hashlib.sha256(payload).hexdigest(),
                'device': device.name, 'port': device.port,
            }
            created = []
            try:
                for path, data in (
                        (binary_path, bytes(payload)),
                        (metadata_path, (
                            json.dumps(
                                metadata, sort_keys=True,
                                separators=(',', ':')) + '\n').encode(
                                    'utf-8'))):
                    fd, temporary = tempfile.mkstemp(
                        prefix='.' + token + '.', dir=root)
                    try:
                        os.fchmod(fd, 0o600)
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.link(temporary, path)
                        created.append(path)
                    finally:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                for path in reversed(created):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                raise
            return metadata
        except Exception:
            if update_prepared:
                try:
                    device.update_cancel()
                    device.refresh()
                except Exception:
                    logging.exception(
                        'BMCU %s could not cancel update mode after NVM '
                        'export failure', device.name)
            raise

    def cmd_UPDATE_ACCESS(self, gcmd):

        device = self._require_device(gcmd)
        action = str(gcmd.get('ACTION', 'STATUS') or 'STATUS').strip().upper()
        if action == 'STATUS':
            gcmd.respond_info('%s update access: %s' %
                              (device.name, 'released' if device.suspended else 'Klipper-owned'))
            return
        if action == 'EXPORT':
            token = str(gcmd.get('TOKEN', '') or '').strip().lower()
            try:
                metadata = self._firmware_update_export_nvm(device, token)
            except Exception as exc:
                raise gcmd.error('%s NVM export failed: %s' % (device.name, exc))
            gcmd.respond_info('%s NVM export ready: %s' %
                              (device.name, metadata['sha256']))
            return
        if action == 'QUIESCE':
            print_state = self._print_state()
            if print_state in ('printing', 'paused', 'pause'):
                raise gcmd.error(
                    'printer is %s; serial flash guard is unavailable' % print_state)
            if self.active_operations:
                raise gcmd.error('BMCU operation is active; stop it before firmware flash')
            device.suspend_for_update(
                device.suspend_reason if device.suspended else 'serial_flash_guard')
            gcmd.respond_info('%s serial transport quiesced for raw firmware flash' % device.name)
            return
        if action == 'UNQUIESCE':
            if device.suspended and device.suspend_reason == 'serial_flash_guard':
                device.resume_after_update()
            gcmd.respond_info('%s serial flash guard released' % device.name)
            return
        if action in ('RESUME', 'CANCEL'):
            try:
                self._firmware_update_power_restore_device(device)
            except Exception as exc:
                raise gcmd.error(
                    '%s motor power could not be restored after firmware flash: %s' %
                    (device.name, exc))
            if device.suspended:
                device.resume_after_update()
            if action == 'CANCEL':
                deadline = self.reactor.monotonic() + 15.0
                while self.reactor.monotonic() < deadline:
                    if device.ready and device.runtime_configured and not device.suspended:
                        break
                    now = self.reactor.monotonic()
                    self.reactor.pause(min(deadline, now + 0.1))
                if not device.ready or not device.runtime_configured:
                    raise gcmd.error('%s did not reconnect to cancel update mode' % device.name)
                try:
                    device.update_cancel()
                    device.refresh()
                except Exception as exc:
                    raise gcmd.error('%s could not cancel firmware-update mode: %s' %
                                     (device.name, exc))
                gcmd.respond_info('%s update mode cancelled and runtime restored' % device.name)
            else:
                gcmd.respond_info('%s serial reconnect scheduled' % device.name)
            return
        if action != 'PREPARE':
            raise gcmd.error('ACTION must be EXPORT, PREPARE, CANCEL, RESUME, QUIESCE, UNQUIESCE or STATUS')
        if device.suspended:
            device.suspend_for_update(device.suspend_reason)
            gcmd.respond_info('%s serial port is already released for firmware update' % device.name)
            return
        print_state = self._print_state()
        if print_state in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'printer is %s; finish or cancel the job before firmware flash' %
                print_state)
        recovery = bool(gcmd.get_int('RECOVERY', 0, minval=0, maxval=1))
        if self.active_operations:
            raise gcmd.error('BMCU operation is active; stop it before firmware flash')
        if not recovery:
            if not device.ready or not device.runtime_configured:
                raise gcmd.error('%s is not ready for firmware flash' % device.name)
            try:
                refreshed = dict(device.refresh())
            except Exception as exc:
                raise gcmd.error(
                    '%s status refresh failed before firmware flash: %s' %
                    (device.name, exc))
            present_state = list(refreshed.get('present', []))
            if len(present_state) != 4:
                raise gcmd.error(
                    '%s returned an invalid four-Channel filament status' %
                    device.name)
            present = [channel for channel in range(4)
                       if bool(present_state[channel])]
            occupied = [
                (channel, self._route_state(device, channel, refreshed))
                for channel in range(4)
                if self._route_state(device, channel, refreshed) !=
                   protocol.ROUTE_EMPTY
            ]
            if present:
                raise gcmd.error(
                    'Completely remove filament before firmware flash: Channel %s '
                    'still detects filament' %
                    ', '.join(str(channel + 1) for channel in present))
            if occupied:
                raise gcmd.error(
                    'Unload every BMCU route before firmware flash: %s' %
                    ', '.join('Channel %d=%s' %
                              (channel + 1,
                               protocol.ROUTE_NAMES.get(route, 'UNCERTAIN'))
                              for channel, route in occupied))
        if any(int(value) != protocol.MOTION_IDLE
               for value in device.status.get('motion', [])):
            raise gcmd.error('Stop BMCU motion before firmware flash')

        update_prepared = False
        if not recovery:
            try:
                device.update_prepare()
                update_prepared = True
                self.reactor.pause(self.reactor.monotonic() + 0.05)
                prepared_status = dict(device.refresh())
                if any(int(value) != protocol.MOTION_IDLE
                       for value in prepared_status.get('motion', [])):
                    raise RuntimeError('BMCU motion did not stop in update mode')
            except Exception as exc:
                if update_prepared:
                    try:
                        device.update_cancel()
                    except Exception:
                        logging.exception(
                            'BMCU %s could not cancel update mode after preflight failure',
                            device.name)
                raise gcmd.error(
                    '%s could not enter safe firmware-update mode: %s' %
                    (device.name, exc))
        device.suspend_for_update(
            'firmware_recovery' if recovery else 'firmware_update')
        power_pin = ''
        try:
            power_pin = self._firmware_update_power_off(device)
        except Exception as exc:
            try:
                device.resume_after_update()
            except Exception:
                logging.exception(
                    'BMCU %s could not resume after update power-off failure',
                    device.name)
            raise gcmd.error(
                '%s motor power could not be disabled before firmware flash: %s' %
                (device.name, exc))
        gcmd.respond_info(
            '%s serial port released for direct WCH ISP firmware flash%s' %
            (device.name,
             ('; motor power disabled through output_pin %s' % power_pin)
             if power_pin else ''))

    def cmd_PREPARE_UNINSTALL(self, gcmd):
        print_state = self._print_state()
        if print_state in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'printer is %s; finish or cancel the job before uninstall' %
                print_state)
        if self.active_operations:
            raise gcmd.error('BMCU operation is active; stop it before uninstall')
        if self.prestaged:
            raise gcmd.error('Clear every BMCU prestage before uninstall')
        occupied = []
        for device in self.devices:
            if not device.ready:
                routed_u1 = any(
                    (self._endpoint_for_channel(device, channel) is not None and
                     self._endpoint_for_channel(device, channel).driver ==
                     'snapmaker_u1')
                    for channel in range(4))
                if (routed_u1 and device.name not in
                        self._u1_devices_reconciled_once):
                    raise gcmd.error(
                        '%s is offline and has not supplied a fresh route '
                        'snapshot since Klipper started; reconnect it before '
                        'uninstall' % device.name)
                nonempty = [channel for channel in range(4)
                            if self._route_state(device, channel) != protocol.ROUTE_EMPTY]
                if nonempty:
                    raise gcmd.error(
                        '%s is offline; Channel route state cannot be verified' %
                        device.name)
            for channel in range(4):
                route = self._route_state(device, channel)
                if route != protocol.ROUTE_EMPTY:
                    occupied.append((device.name, channel,
                                     protocol.ROUTE_NAMES.get(route, 'UNCERTAIN')))
        if occupied:
            raise gcmd.error(
                'Unload or confirm EMPTY before uninstall: %s' %
                ', '.join('%s Channel %d=%s' % (item[0], item[1] + 1, item[2]) for item in occupied))

        if self.u1_cross_refill_pending:
            raise gcmd.error(
                'cannot uninstall while Snapmaker U1 cross-head refill recovery is '
                'pending; finish BMCU_REFILL_RESUME or restore the exact print '
                'transaction from the recovery panel first')
        print_transaction_present = bool(
            self.print_map_active or self.print_plan_open or self.print_tools or
            self.print_job_id or self._u1_original or
            self.print_transaction_phase or self._u1_map_backup or
            self._u1_used_backup or self._u1_end_unload_backup)
        if print_transaction_present:
            if not self._clear_print_session(
                    'verified live uninstall print-map handoff'):
                raise gcmd.error(
                    'cannot uninstall: the exact Snapmaker U1 print map could '
                    'not be restored and persisted')
            if (self.print_map_active or self.print_plan_open or
                    self.print_tools or self._u1_original or
                    self.print_transaction_phase or self._u1_map_backup or
                    self._u1_used_backup or self._u1_end_unload_backup):
                raise gcmd.error(
                    'cannot uninstall: Snapmaker U1 print transaction remains active')

        restored = []
        for endpoint in self.endpoints.values():
            if endpoint.driver != 'snapmaker_u1':
                continue
            ownership = self._u1_ownership_record(endpoint.name)
            hazards = self._u1_disconnect_hazards.get(endpoint.name, set())
            active_ownership = bool(
                hazards or
                self._endpoint_has_loaded_or_active_route(endpoint.name) or
                ownership.get('persistent_hold', False) or
                ownership.get('generation_open', False))
            if not active_ownership:
                endpoint.release_runtime_sensor_takeover()
                continue
            path = endpoint.native_path_status()
            if not path.get('known') or path.get('busy'):
                raise gcmd.error(
                    '%s shared path cannot be handed back safely (%s); '
                    'resolve the active BMCU ownership before uninstall' %
                    (endpoint.name, path.get('channel_state', 'unknown')))
            self._handoff_u1_to_native(
                endpoint, 'verified uninstall handoff to Snapmaker', save=True,
                close_generation=True)
            restored.append(endpoint.name)
        self.state.save()
        gcmd.respond_info(
            'BMCU uninstall is safe. Restored U1 endpoints: %s' %
            (', '.join(restored) if restored else 'none'))

    def cmd_FORGET_DEVICE(self, gcmd):
        device = self._require_device(gcmd)
        action = str(gcmd.get('ACTION', 'PREPARE') or 'PREPARE').strip().upper()
        if action == 'CANCEL':
            pending = self._forget_pending.get(device.name)
            if not isinstance(pending, dict):
                gcmd.respond_info('%s has no pending forget operation' % device.name)
                return
            for key, record in pending.get('device_records', {}).items():
                self.state.data.setdefault('devices', {})[key] = record
            session_tools = self.state.data.setdefault('print_session', {}).setdefault('tools', {})
            for key, mapping in pending.get('session_tools', {}).items():
                session_tools[key] = mapping
            for key, record in pending.get('ownership_records', {}).items():
                self.state.data.setdefault('u1_ownership', {})[key] = record
            self.state.save()
            self._forget_pending.pop(device.name, None)
            if device.suspended and device.suspend_reason == 'forget_device':
                device.resume_after_update()
            gcmd.respond_info('%s forget operation cancelled' % device.name)
            return
        if action != 'PREPARE':
            raise gcmd.error('ACTION must be PREPARE or CANCEL')
        if device.name in self._forget_pending:
            gcmd.respond_info('%s is already prepared for removal' % device.name)
            return
        if bool(getattr(device, 'connected', False) or getattr(device, 'ready', False)):
            raise gcmd.error('disconnect %s before removing it' % device.name)
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error('BMCU removal is unavailable while a print is active or paused')
        if self.active_operations:
            raise gcmd.error('BMCU removal is unavailable while a BMCU operation is active')
        if self.u1_cross_refill_pending:
            raise gcmd.error('BMCU removal is unavailable while Snapmaker refill recovery is pending')
        if (self.print_map_active or self.print_plan_open or self.print_tools or
                self.print_job_id or self._u1_original or
                self.print_transaction_phase or self._u1_map_backup or
                self._u1_used_backup or self._u1_end_unload_backup):
            raise gcmd.error('BMCU removal is unavailable while a print transaction is retained')
        for channel in range(4):
            evidence = self._u1_route_ownership_evidence(device, channel)
            if evidence.get('claimed'):
                raise gcmd.error(
                    '%s Channel %d still has route/ownership state; reconcile it to EMPTY before removal' %
                    (device.name, channel + 1))
        uid = self._device_uid(device)
        ownership_records = self.state.data.setdefault('u1_ownership', {})
        removed_ownership = {}
        for endpoint_name, ownership in list(ownership_records.items()):
            if not isinstance(ownership, dict):
                continue
            owner_uid = str(ownership.get('device_uid', '') or '').upper()
            owner_name = str(ownership.get('device', '') or '')
            follower_uid = str(ownership.get('follower_uid', '') or '').upper()
            follower_name = str(ownership.get('follower_device', '') or '')
            owner_match = bool(
                (uid and owner_uid == uid) or
                (not owner_uid and owner_name == device.name))
            follower_match = bool(
                (uid and follower_uid == uid) or
                (not follower_uid and follower_name == device.name))
            if follower_match and ownership.get('follower_pending'):
                raise gcmd.error(
                    '%s is a pending Snapmaker refill follower for %s; run recovery first' %
                    (device.name, endpoint_name))
            if not owner_match:
                continue
            if (any(ownership.get(key) for key in (
                    'persistent_hold', 'generation_open', 'baseline_captured',
                    'tail_detached', 'follower_pending')) or
                    str(ownership.get('route_state', 'EMPTY') or 'EMPTY').upper() != 'EMPTY'):
                raise gcmd.error(
                    '%s still participates in the Snapmaker ownership journal for %s; run recovery first' %
                    (device.name, endpoint_name))
            removed_ownership[endpoint_name] = copy.deepcopy(ownership)
            ownership_records.pop(endpoint_name, None)

        state_devices = self.state.data.setdefault('devices', {})
        matched = {}
        for key, record in list(state_devices.items()):
            if not isinstance(record, dict):
                continue
            if (key == uid or key == device.name or
                    (record.get('name') == device.name and record.get('port') == device.port)):
                matched[key] = copy.deepcopy(record)
        shadow = copy.deepcopy(next(iter(matched.values()), {
            'name': device.name, 'port': device.port, 'channels': {}
        }))
        for key in matched:
            state_devices.pop(key, None)

        removed_tools = {}
        session_tools = self.state.data.setdefault('print_session', {}).setdefault('tools', {})
        for key, mapping in list(session_tools.items()):
            if not isinstance(mapping, dict):
                continue
            mapping_uid = str(mapping.get('device_uid', '') or '').upper()
            if ((uid and mapping_uid == uid) or mapping.get('device') == device.name):
                removed_tools[key] = copy.deepcopy(mapping)
                session_tools.pop(key, None)

        self._forget_pending[device.name] = {
            'device_records': matched,
            'session_tools': removed_tools,
            'ownership_records': removed_ownership,
            'shadow': shadow,
        }
        if not device.suspended:
            device.suspend_for_update('forget_device')
        try:
            self.state.save()
        except Exception:
            pending = self._forget_pending.pop(device.name)
            for key, record in pending['device_records'].items():
                state_devices[key] = record
            for key, mapping in pending['session_tools'].items():
                session_tools[key] = mapping
            for key, record in pending.get('ownership_records', {}).items():
                ownership_records[key] = record
            if device.suspended and device.suspend_reason == 'forget_device':
                device.resume_after_update()
            raise
        gcmd.respond_info(
            '%s persistent settings were cleared and are ready for configuration removal' %
            device.name)

    def cmd_SETUP(self, gcmd):
        auto = bool(gcmd.get_int('AUTO', 0, minval=0, maxval=1))
        preset = gcmd.get('PRESET', None)
        if auto:
            self.printer_analysis = compat.analyze_printer(
                self.printer, self.controller_mode)
            preset = self.printer_analysis.get('recommended_preset')
        if not preset:
            raise gcmd.error('PRESET is required unless AUTO=1')
        overrides = {'PRESET': preset}
        if auto:
            detected_count = len(self.printer_analysis.get('extruders', []))
            if detected_count > 0:
                overrides['COUNT'] = detected_count
        for name in ('DEVICE', 'CHANNEL', 'ENDPOINT', 'HEAD', 'REPLACE'):
            value = gcmd.get(name, None)
            if value is not None:
                overrides[name] = value

        class SetupCommand(object):
            def __init__(self, source, values):
                self.source, self.values = source, values
            def get(self, name, default=None):
                return self.values.get(name, self.source.get(name, default))
            def get_int(self, name, default=None, **kwargs):
                value = self.values.get(name, None)
                if value is None:
                    return self.source.get_int(name, default, **kwargs)
                return int(value)
            def error(self, text):
                return self.source.error(text)
            def respond_info(self, text):
                return self.source.respond_info(text)
        self.cmd_APPLY_PRESET(SetupCommand(gcmd, overrides))

    def cmd_ROUTE_FEED(self, gcmd):
        self._require_standalone_operation('BMCU_ROUTE_FEED', gcmd)
        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        millimeters = self._finite_gcmd_float(
            gcmd, 'MM', minval=5.0, maxval=5000.0)
        limit = gcmd.get_int('BUFFER_LIMIT', 95, minval=55, maxval=98)
        timeout = self._finite_gcmd_float(
            gcmd, 'TIMEOUT', max(3.0, millimeters / 10.0 + 2.0),
            minval=0.25, maxval=300.0)
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise gcmd.error(
                '%s Channel %d has no configured Endpoint' %
                (device.name, channel + 1))
        try:
            self._validate_endpoint_for_operation(
                endpoint, require_u1_ownership=False)
            self._require_exclusive_endpoint_route(device, endpoint, channel)
            self._check_automatic_ready(device, channel)
        except BMCUError as exc:
            raise gcmd.error(str(exc))
        if self._route_state(device, channel) != protocol.ROUTE_EMPTY:
            raise gcmd.error(
                '%s Channel %d is not EMPTY; confirm or clear its route first' %
                (device.name, channel + 1))
        locked = False
        motion_started = False
        release_new_u1_hold = False
        u1_hold_was_active = bool(
            endpoint.driver == 'snapmaker_u1' and
            self._u1_ownership_record(endpoint.name).get('persistent_hold'))
        try:
            self._lock(device, endpoint, 'ROUTE_FEED Channel %d' % (channel + 1), channel=channel)
            locked = True
            self._arm_u1_persistent_hold(
                endpoint, device, channel, 'manual route feed')
            self._validate_endpoint_for_operation(endpoint)
            motion_started = True
            op_id = device.start_feed_distance(
                channel, millimeters, limit, int(timeout * 1000.0))
            result = self._wait_feed_operation(
                device, op_id, timeout + 2.0)
            if not result.get('ok'):
                raise BMCUError('route feed failed: %s after %.1f mm' %
                                (result.get('reason'),
                                 result.get('measured_mm', 0.0)))
        except BMCUError as exc:
            if (endpoint.driver == 'snapmaker_u1' and
                    not u1_hold_was_active and not motion_started):
                release_new_u1_hold = True
            raise gcmd.error(str(exc))
        finally:
            if locked:
                self._unlock(device, endpoint)
            if release_new_u1_hold:
                try:
                    self._release_u1_persistent_hold_if_safe(
                        endpoint, 'route-feed preflight failed before filament motion')
                except Exception:
                    logging.exception(
                        'BMCU could not release U1 ownership after route-feed preflight failure')
        gcmd.respond_info(
            '%s Channel %d fed %.1f mm; route remains UNCERTAIN until '
            'BMCU_ROUTE_CONFIRM STATE=EMPTY, STATE=PARKED or STATE=LOADED' %
            (device.name, channel + 1,
             result.get('measured_mm', millimeters)))

    def cmd_BUFFER_MODE(self, gcmd):
        device = self._require_device(gcmd)
        mode = str(gcmd.get('MODE')).strip().upper()
        if mode == 'STOP':
            channel = gcmd.get_int('CHANNEL', None, minval=0, maxval=3)
            if channel is None:
                device.stop_all()
                gcmd.respond_info('%s all buffer motion stopped' % device.name)
            else:
                device.set_motion(channel, protocol.MOTION_IDLE)
                gcmd.respond_info('%s Channel %d buffer mode -> STOP' %
                                  (device.name, channel + 1))
            return

        if mode != 'FOLLOW':
            raise gcmd.error('MODE must be FOLLOW or STOP')
        self._require_standalone_operation('BMCU_BUFFER_MODE', gcmd)
        channel = self._require_channel(gcmd)
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise gcmd.error(
                '%s Channel %d has no configured Endpoint' %
                (device.name, channel + 1))
        route = self._route_state(device, channel)
        if mode == 'FOLLOW':
            if route != protocol.ROUTE_LOADED:
                raise gcmd.error(
                    'confirm LOADED Channel %d before FOLLOW' % (channel + 1))
            try:
                self._require_exclusive_endpoint_route(
                    device, endpoint, channel)
                self._check_automatic_ready(device, channel)
            except BMCUError as exc:
                raise gcmd.error(str(exc))
            self._arm_u1_persistent_hold(
                endpoint, device, channel, 'manual buffer FOLLOW')
            device.set_motion(channel, protocol.MOTION_ON_USE)
        gcmd.respond_info('%s Channel %d buffer mode -> %s' %
                          (device.name, channel + 1, mode))

    def _wait_route_confirm_ready(self, device, timeout=3.0):

        if getattr(device, 'suspended', False):
            raise BMCUError('%s is suspended' % device.name)
        deadline = self.reactor.monotonic() + max(0.2, min(float(timeout), 5.0))
        last_error = ''
        try:
            device.set_transport_paused(False, required=True)
        except Exception as exc:
            last_error = str(exc)
        while self.reactor.monotonic() < deadline:
            if (getattr(device, 'ipc_connected', False) and
                    getattr(device, 'connected', False) and
                    getattr(device, 'hello_validated', False) and
                    getattr(device, 'ready', False) and
                    getattr(device, 'runtime_configured', False)):
                return True
            now = self.reactor.monotonic()
            if not getattr(device, 'ipc_connected', False):
                try:
                    device.tick(now, transport_only=True)
                except Exception as exc:
                    last_error = str(exc)
            current_error = str(getattr(device, 'last_error', '') or '')
            if current_error:
                last_error = current_error
            self.reactor.pause(min(deadline, now + 0.05))
        detail = ('; %s' % last_error) if last_error else ''
        raise BMCUError(
            '%s control session is not ready; retry after BMCU shows Ready%s' %
            (device.name, detail))

    def cmd_ROUTE_CONFIRM(self, gcmd):
        try:
            return self._cmd_ROUTE_CONFIRM(gcmd)
        except Exception as exc:
            command_error = getattr(self.printer, 'command_error', None)
            if isinstance(command_error, type) and isinstance(exc, command_error):
                raise
            logging.exception(
                'BMCU_ROUTE_CONFIRM unexpected failure was contained')
            raise gcmd.error(
                'BMCU route confirmation failed safely; no further action was '
                'applied. Check klippy.log before retrying: %s' % exc)

    def _cmd_ROUTE_CONFIRM(self, gcmd):
        state = str(gcmd.get('STATE')).strip().upper()
        if state not in ('EMPTY', 'PARKED', 'LOADED'):
            raise gcmd.error('STATE must be EMPTY, PARKED or LOADED')
        route_empty = state in ('EMPTY', 'PARKED')
        if self.controller_mode != 'standalone' and not route_empty:
            raise gcmd.error(
                'only STATE=EMPTY or STATE=PARKED is allowed while BMCU motion ownership is blocked')

        print_state = self._print_state()
        if print_state in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'route confirmation is unavailable while a print is active or paused')
        if self.active_operations:
            raise gcmd.error(
                'route confirmation is unavailable while a BMCU operation is active')
        refill = getattr(self, 'refill', None)
        if (refill is not None and
                (getattr(refill, 'transactions', {}) or
                 getattr(refill, '_pending', set()))):
            raise gcmd.error(
                'route confirmation is unavailable during refill recovery')

        if getattr(self, '_u1_background_jobs', {}):
            raise gcmd.error(
                'route confirmation is unavailable while background U1 filament '
                'motion is active')
        if (getattr(self, '_critical_motion_active', False) or
                getattr(self, '_critical_motion_depth', 0)):
            raise gcmd.error(
                'route confirmation is unavailable while printer motion is active')

        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        route_key = self._route_key(device, channel)
        endpoint = self._endpoint_for_channel(device, channel)

        self._required_transport_users += 1
        try:
            try:
                self._wait_route_confirm_ready(device, timeout=3.0)
                previous = dict(device.refresh())
            except (BMCUError, RuntimeError) as exc:
                raise gcmd.error(
                    '%s status is unavailable; Channel %d route confirmation '
                    'was not applied: %s' %
                    (device.name, channel + 1, exc))
            except Exception as exc:
                logging.exception(
                    'BMCU %s Channel %d route-confirm pre-state failed',
                    device.name, channel + 1)
                raise gcmd.error(
                    '%s Channel %d route confirmation was contained before any '
                    'state change: %s' % (device.name, channel + 1, exc))

            recorded = self._route_state(device, channel, previous)
            native_path = None
            native_sensor_snapshot = None

            if route_empty:
                present_values = previous.get('present')
                if (not isinstance(present_values, (list, tuple)) or
                        len(present_values) <= channel):
                    raise gcmd.error(
                        'Channel %d input detector state is unavailable' %
                        (channel + 1))
                input_present = bool(present_values[channel])

                durable_tail = self._durable_tail_route(device, channel)
                if durable_tail is not None:
                    if not durable_tail.get('routed'):
                        raise gcmd.error(
                            '%s Channel %d has a detached tail but its original '
                            'Endpoint is missing or no longer assigned; restore '
                            'that routing first' % (device.name, channel + 1))
                    raise gcmd.error(
                        '%s Channel %d still has a detached tail in the downstream '
                        'path; finish the tail handoff before confirming an empty toolhead route' %
                        (device.name, channel + 1))

                if state == 'EMPTY' and input_present:
                    raise gcmd.error(
                        'Channel %d still detects filament; choose PARKED when '
                        'filament remains in BMCU but the toolhead route is empty' %
                        (channel + 1))
                if state == 'PARKED' and not input_present:
                    raise gcmd.error(
                        'Channel %d input detector is clear; choose EMPTY when '
                        'no filament remains in BMCU' % (channel + 1))

                if endpoint is not None and endpoint.driver == 'snapmaker_u1':
                    try:
                        native_sensor_snapshot = endpoint.entry_sensor_snapshot()
                        native_path = endpoint.native_path_status(
                            sensor_snapshot=native_sensor_snapshot,
                            route_empty_verified=True)
                    except Exception as exc:
                        logging.exception(
                            'BMCU %s Channel %d U1 native path read failed',
                            device.name, channel + 1)
                        raise gcmd.error(
                            '%s toolhead path check failed: %s' %
                            (endpoint.name, exc))
                    if not native_path.get('known'):
                        raise gcmd.error(
                            '%s toolhead path state is unavailable' % endpoint.name)
                    channel_state = str(
                        native_path.get('channel_state', 'unknown') or
                        'unknown').lower()
                    if (native_path.get('busy') and
                            not native_path.get('stale_load_finish')):
                        raise gcmd.error(
                            '%s does not positively confirm an empty toolhead '
                            'path (%s)' % (endpoint.name, channel_state))
            else:
                present_values = previous.get('present')
                present = bool(
                    isinstance(present_values, (list, tuple)) and
                    len(present_values) > channel and
                    present_values[channel])
                if not present and recorded != protocol.ROUTE_UNCERTAIN:
                    raise gcmd.error(
                        'Channel %d input detector is clear and firmware did not '
                        'record an uncertain route' % (channel + 1))
                if not (int(previous.get('calibration_valid_mask', 0)) &
                        (1 << channel)):
                    raise gcmd.error(
                        'Channel %d buffer calibration is invalid' % (channel + 1))
                if endpoint is None:
                    raise gcmd.error(
                        '%s Channel %d has no configured Endpoint' %
                        (device.name, channel + 1))
                try:
                    self._require_exclusive_endpoint_route(
                        device, endpoint, channel)
                except BMCUError as exc:
                    raise gcmd.error(str(exc))
                except Exception as exc:
                    logging.exception(
                        'BMCU %s Channel %d exclusive-route check failed',
                        device.name, channel + 1)
                    raise gcmd.error(
                        '%s Channel %d route ownership check failed: %s' %
                        (device.name, channel + 1, exc))

            try:
                if route_empty:
                    device.mark_unloaded(channel)
                else:
                    device.mark_loaded(channel)
            except RuntimeError as exc:
                self._uncertain_routes.add(route_key)
                raise gcmd.error(
                    '%s Channel %d route update was not confirmed; no host '
                    'recovery state was changed. Route remains fail-closed until '
                    'a fresh status or BMCU_RECONCILE: %s' %
                    (device.name, channel + 1, exc))
            except Exception as exc:
                self._uncertain_routes.add(route_key)
                logging.exception(
                    'BMCU %s Channel %d route mutation failed',
                    device.name, channel + 1)
                raise gcmd.error(
                    '%s Channel %d route update failed safely; no host recovery '
                    'state was changed: %s' %
                    (device.name, channel + 1, exc))

            try:
                current = dict(device.refresh())
            except RuntimeError as exc:
                self._uncertain_routes.add(route_key)
                raise gcmd.error(
                    '%s Channel %d may have accepted %s, but verification is '
                    'unavailable. No host recovery state was changed; run '
                    'BMCU_RECONCILE after the device is Ready: %s' %
                    (device.name, channel + 1, state, exc))
            except Exception as exc:
                self._uncertain_routes.add(route_key)
                logging.exception(
                    'BMCU %s Channel %d post-update status failed',
                    device.name, channel + 1)
                raise gcmd.error(
                    '%s Channel %d post-update verification failed safely: %s' %
                    (device.name, channel + 1, exc))

            expected_route = (protocol.ROUTE_EMPTY if route_empty else
                              protocol.ROUTE_LOADED)
            actual_route = self._route_states_from_status(current)[channel]
            if actual_route != expected_route:
                self._uncertain_routes.add(route_key)
                raise gcmd.error(
                    '%s Channel %d did not confirm %s after the update '
                    '(firmware route=%d)' %
                    (device.name, channel + 1, state, actual_route))

            post_commit_errors = []
            if route_empty:
                if endpoint is not None and endpoint.driver == 'snapmaker_u1':
                    try:
                        if hasattr(endpoint, 'commit_native_path_empty'):
                            endpoint.commit_native_path_empty(
                                sensor_snapshot=native_sensor_snapshot,
                                manual_confirmation=True)
                    except Exception as exc:
                        logging.exception(
                            'BMCU %s Channel %d U1 EMPTY projection failed',
                            device.name, channel + 1)
                        post_commit_errors.append(
                            '%s stock feeder EMPTY projection failed: %s' %
                            (endpoint.name, exc))
                if not post_commit_errors and endpoint is not None:
                    try:
                        self._clear_u1_tail_detached(
                            endpoint, device, channel,
                            'manual %s confirmation verified the complete head path' % state)
                        self._clear_generic_tail_detached(
                            endpoint, device, channel,
                            'manual %s confirmation verified the complete route' % state)
                    except Exception as exc:
                        logging.exception(
                            'BMCU %s Channel %d tail-journal cleanup failed',
                            device.name, channel + 1)
                        post_commit_errors.append(
                            'tail recovery journal cleanup failed: %s' % exc)
            else:
                try:
                    self._arm_u1_persistent_hold(
                        endpoint, device, channel,
                        'manual LOADED route confirmation')
                except Exception as exc:
                    logging.exception(
                        'BMCU %s Channel %d LOADED ownership commit failed',
                        device.name, channel + 1)
                    post_commit_errors.append(
                        'endpoint ownership commit failed: %s' % exc)

            self._uncertain_routes.discard(route_key)
            if (isinstance(self.last_error, dict) and
                    self.last_error.get('device') == device.name and
                    self.last_error.get('channel', -1) == channel and
                    self.last_error.get('code') in (
                        'ROUTE_RECOVERY_FAILED', 'CHANNEL_RETRACT_FAILED',
                        'ROUTE_UNCERTAIN')):
                self.last_error = None
                self._sync_status_cache_runtime()
            try:
                self.device_status_changed(device, previous, current)
            except Exception as exc:
                logging.exception(
                    'BMCU %s Channel %d status reconciliation failed after '
                    'confirmed %s', device.name, channel + 1, state)
                post_commit_errors.append(
                    'host status reconciliation failed: %s' % exc)

            if route_empty:
                try:
                    unloaded_tool = self.loaded_tools.pop(route_key, -1)
                    if self.active_tool == unloaded_tool:
                        self.active_tool = -1
                    self._unmark_print_route(device, channel)
                    if endpoint is not None and not post_commit_errors:
                        self._refresh_u1_disconnect_hazards(endpoint.name)

                        self._u1_lease_dirty = True
                        self._release_u1_persistent_hold_if_safe(
                            endpoint, 'manual %s route confirmation' % state)
                        self._clear_resolved_u1_startup_error()
                    if not self.print_loaded_routes:
                        self.print_terminal_unload_pending = False
                        self._clear_print_session(
                            'all recorded routes manually confirmed empty')
                        if (isinstance(self.last_error, dict) and
                                self.last_error.get('code') in (
                                    'PRINT_END_UNLOAD_FAILED',
                                    'PRINT_END_BACKGROUND_FAILED')):
                            self.last_error = None
                            self._sync_status_cache_runtime()
                        self._save_print_session()
                except Exception as exc:
                    logging.exception(
                        'BMCU %s Channel %d EMPTY host finalization failed',
                        device.name, channel + 1)
                    post_commit_errors.append(
                        'EMPTY host finalization failed: %s' % exc)

            try:
                self._save_runtime()
            except Exception as exc:
                logging.exception(
                    'BMCU %s Channel %d runtime save failed after confirmed %s',
                    device.name, channel + 1, state)
                post_commit_errors.append('runtime state save failed: %s' % exc)

            if post_commit_errors:
                raise gcmd.error(
                    '%s Channel %d firmware confirmed %s, but host cleanup is '
                    'incomplete. Do not change routing; run BMCU_RECONCILE after '
                    'checking the physical path: %s' %
                    (device.name, channel + 1, state,
                     '; '.join(post_commit_errors)))

            gcmd.respond_info('%s Channel %d confirmed %s' %
                              (device.name, channel + 1, state))
        finally:
            self._required_transport_users = max(
                0, self._required_transport_users - 1)

    def cmd_ROUTE_RECOVER(self, gcmd):
        self._require_standalone_operation('BMCU_ROUTE_RECOVER', gcmd)
        mode = str(gcmd.get('MODE', 'INPUT_RETRACT') or '').strip().upper()
        if mode != 'INPUT_RETRACT':
            raise gcmd.error('MODE must be INPUT_RETRACT')
        if self._print_state() in ('printing', 'paused', 'pause'):
            raise gcmd.error('route recovery is unavailable while a print is active or paused')
        if self.active_operations:
            raise gcmd.error('route recovery is unavailable while a BMCU operation is active')
        if (getattr(self, '_critical_motion_active', False) or
                getattr(self, '_critical_motion_depth', 0)):
            raise gcmd.error('route recovery is unavailable while printer motion is active')

        device = self._require_device(gcmd)
        channel = self._require_channel(gcmd)
        endpoint = self._endpoint_for_channel(device, channel)
        if endpoint is None:
            raise gcmd.error('%s Channel %d has no configured Endpoint' %
                             (device.name, channel + 1))
        if endpoint.driver == 'snapmaker_u1':
            raise gcmd.error('Snapmaker U1 route recovery uses the native Head path checks')
        if not self._channel_retract_runtime_supported(device):
            raise gcmd.error('%s firmware does not support explicit Channel retract' % device.name)
        if self._durable_tail_route(device, channel) is not None:
            raise gcmd.error('%s Channel %d has a detached downstream tail; input-only recovery is not allowed' %
                             (device.name, channel + 1))

        route_key = self._route_key(device, channel)
        self._lock(device, endpoint, 'RECOVER UNCERTAIN INPUT', channel=channel)
        marked_empty = False
        recovered_empty = False
        try:
            previous = dict(device.refresh())
            if self._route_state(device, channel, previous) != protocol.ROUTE_UNCERTAIN:
                raise BMCUError('%s Channel %d route is not UNCERTAIN' %
                                (device.name, channel + 1))
            present = previous.get('present', [False] * 4)
            if not bool(present[channel]):
                raise BMCUError('Channel %d input is already clear; use Confirm toolhead route empty instead' %
                                (channel + 1))
            if not (int(previous.get('calibration_valid_mask', 0)) & (1 << channel)):
                raise BMCUError('%s Channel %d buffer is not calibrated' %
                                (device.name, channel + 1))
            if not (int(previous.get('encoder_io_mask', 0)) & (1 << channel)):
                raise BMCUError('%s Channel %d filament movement sensor has an electrical fault' %
                                (device.name, channel + 1))
            occupied = [
                (other, other_channel, state)
                for other, other_channel, state in self._routes_for_endpoint(
                    endpoint.name, (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN))
                if self._route_key(other, other_channel) != route_key]
            if occupied:
                raise BMCUError('%s has another occupied or uncertain BMCU route; recover that path first' %
                                endpoint.name)
            if any(int(value) != protocol.MOTION_IDLE
                   for value in previous.get('motion', [])):
                raise BMCUError('%s has active motion; route recovery is blocked' % device.name)

            device.mark_unloaded(channel)
            marked_empty = True
            staged = dict(device.refresh())
            raw = self._route_states_from_status(staged)[channel]
            if raw != protocol.ROUTE_EMPTY:
                raise BMCUError('%s Channel %d did not enter the guarded EMPTY retract state' %
                                (device.name, channel + 1))

            op_id = device.start_channel_retract(channel)
            result = device.wait_for_op(op_id, timeout=300.0)
            if not result['ok']:
                raise BMCUError('recovery retract failed: %s measured=%.2f mm' %
                                (result['reason'], result['measured_mm']))
            current = dict(device.refresh())
            if bool(current.get('present', [False] * 4)[channel]):
                raise BMCUError('recovery retract finished but Channel %d still detects filament' %
                                (channel + 1))
            if self._route_states_from_status(current)[channel] != protocol.ROUTE_EMPTY:
                raise BMCUError('recovery retract finished but the BMCU route is not EMPTY')
            recovered_empty = True

            self._uncertain_routes.discard(route_key)
            unloaded_tool = self.loaded_tools.pop(route_key, -1)
            if self.active_tool == unloaded_tool:
                self.active_tool = -1
            self._unmark_print_route(device, channel)
            self._drop_prestage_record(route_key, release_sensor=True)
            try:
                endpoint.release_runtime_sensor_takeover()
                self._clear_endpoint_projection(endpoint)
            except Exception:
                logging.exception('BMCU could not release generic endpoint projection after route recovery')
            if (isinstance(self.last_error, dict) and
                    self.last_error.get('device') == device.name and
                    self.last_error.get('channel', -1) == channel):
                self.last_error = None
                self._sync_status_cache_runtime()
            self.device_status_changed(device, previous, current)
            self._save_runtime()
            gcmd.respond_info('%s Channel %d loose input filament retracted; route confirmed EMPTY' %
                              (device.name, channel + 1))
        except Exception as exc:
            try:
                device.stop_all()
            except Exception:
                logging.exception('BMCU could not stop after route recovery failure')
            rollback_error = None
            if marked_empty and not recovered_empty:
                try:
                    device.mark_loaded(channel)
                    device.refresh()
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                    logging.exception('BMCU could not restore fail-closed route after recovery failure')
            details = str(exc)
            if rollback_error is not None:
                details += '; fail-closed route restore also failed: %s' % rollback_error
            self._record_error(
                'ROUTE_RECOVERY_FAILED', device=device.name, channel=channel,
                endpoint=endpoint.name, phase='INPUT_RETRACT', details=details)
            try:
                self._save_runtime()
            except Exception:
                logging.exception('BMCU could not save failed route recovery state')
            raise self._command_pause_error(gcmd, details)
        finally:
            self._unlock(device, endpoint)

    def cmd_HEAD_CONFIRM_EMPTY(self, gcmd):

        print_state = self._print_state()
        if print_state in ('printing', 'paused', 'pause'):
            raise gcmd.error(
                'Head EMPTY confirmation is unavailable while a print is active '
                'or paused')
        if self.active_operations:
            raise gcmd.error(
                'Head EMPTY confirmation is unavailable while a BMCU operation '
                'is active')
        refill = getattr(self, 'refill', None)
        if (refill is not None and
                (getattr(refill, 'transactions', {}) or
                 getattr(refill, '_pending', set()))):
            raise gcmd.error(
                'Head EMPTY confirmation is unavailable during refill recovery')
        if getattr(self, '_u1_background_jobs', {}):
            raise gcmd.error(
                'Head EMPTY confirmation is unavailable while background U1 '
                'filament motion is active')
        if (getattr(self, '_critical_motion_active', False) or
                getattr(self, '_critical_motion_depth', 0)):
            raise gcmd.error(
                'Head EMPTY confirmation is unavailable while printer motion '
                'is active')

        endpoint_name = str(gcmd.get('ENDPOINT', '') or '').strip()
        head_value = gcmd.get_int('HEAD', None, minval=1, maxval=4)
        if endpoint_name:
            endpoint = self.endpoints.get(endpoint_name)
            if endpoint is None:
                raise gcmd.error('unknown Endpoint %s' % endpoint_name)
            if endpoint.driver != 'snapmaker_u1':
                raise gcmd.error('%s is not a Snapmaker U1 Head' % endpoint_name)
            endpoint_head = int(endpoint.get('head_index', -1)) + 1
            if head_value is not None and int(head_value) != endpoint_head:
                raise gcmd.error(
                    'HEAD=%d does not match %s (Head %d)' %
                    (head_value, endpoint_name, endpoint_head))
        else:
            if head_value is None:
                raise gcmd.error('HEAD=1..4 or ENDPOINT=u1_headX is required')
            endpoint = self._u1_endpoint_for_head(int(head_value) - 1)
            if endpoint is None:
                raise gcmd.error(
                    'Snapmaker U1 Head %d Endpoint is unavailable' % head_value)
            endpoint_name = endpoint.name

        if (self._u1_disconnect_hazards.get(endpoint_name) or
                self._u1_endpoint_transition_reserved(endpoint_name) or
                endpoint_name in self.endpoint_locks):
            raise gcmd.error(
                '%s still has an active transition, lock or disconnect hazard' %
                endpoint_name)
        if self._endpoint_has_loaded_or_active_route(endpoint_name):
            raise gcmd.error(
                '%s still has a LOADED/UNCERTAIN/active BMCU route' %
                endpoint_name)
        if self._prestaged_devices_for_endpoint(endpoint_name):
            raise gcmd.error(
                '%s still has a prestaged BMCU source' % endpoint_name)

        assigned = self._assigned_routes_for_endpoint(endpoint_name)
        if not assigned:
            raise gcmd.error(
                '%s has no configured BMCU Channels' % endpoint_name)
        checked_devices = set()
        for device, channel in assigned:
            if device.name not in checked_devices:
                try:
                    status = dict(device.refresh())
                except Exception as exc:
                    raise gcmd.error(
                        '%s status is unavailable: %s' % (device.name, exc))
                checked_devices.add(device.name)
                if (not device.ready or not device.runtime_configured or
                        not device.status_reconciled):
                    raise gcmd.error(
                        '%s is not fully Ready/reconciled' % device.name)
            else:
                status = device.status
            route = self._route_states_from_status(status)[int(channel)]
            if route != protocol.ROUTE_EMPTY:
                raise gcmd.error(
                    '%s Channel %d is not firmware EMPTY' %
                    (device.name, int(channel) + 1))
            if self._durable_tail_route(device, int(channel)) is not None:
                raise gcmd.error(
                    '%s Channel %d still has a detached downstream tail' %
                    (device.name, int(channel) + 1))

        try:
            snapshot = endpoint.require_entry_sensor_snapshot(timeout=1.25)
            changed = endpoint.commit_native_path_empty(
                sensor_snapshot=snapshot, manual_confirmation=True)
            self._clear_endpoint_projection(endpoint)
            self._refresh_u1_disconnect_hazards(endpoint_name)
            self._u1_lease_dirty = True
            self._release_u1_persistent_hold_if_safe(
                endpoint, 'operator physically confirmed complete Head EMPTY')
            self._reconcile_u1_leases(
                self.reactor.monotonic(), force=True)
            self._clear_resolved_u1_startup_error()
            self._save_runtime()
        except Exception as exc:
            logging.exception(
                'BMCU manual physical Head EMPTY confirmation failed for %s',
                endpoint_name)
            raise gcmd.error(
                '%s EMPTY confirmation failed safely: %s' %
                (endpoint_name, exc))

        gcmd.respond_info(
            '%s (Head %d) physically confirmed EMPTY; stock native '
            'load_finish %s and BMCU ownership is available' %
            (endpoint_name, int(endpoint.get('head_index', -1)) + 1,
             'cleared' if changed else 'was already clear'))

    def cmd_RECONCILE(self, gcmd):
        requested = gcmd.get('DEVICE', None)
        devices = ([self._require_device(gcmd, requested)]
                   if requested else list(self.devices))
        statuses = {device.name: device.refresh() for device in devices}
        for device in devices:
            if device.ready and device.runtime_configured and device.status_reconciled:
                self._u1_devices_reconciled_once.add(device.name)
        endpoint_names = set(
            self._channel_endpoint_name(device, channel)
            for device in self.devices for channel in range(4)
            if self._channel_endpoint_name(device, channel))
        conflicts = set(
            name for name in endpoint_names
            if self._halt_endpoint_route_conflict(name))

        reconciled = []
        reconcile_error = bool(conflicts)
        for device in devices:
            status = statuses[device.name]
            raw_routes = self._route_states_from_status(status)
            device_parts = []
            for channel, raw_route in enumerate(raw_routes):
                route_key = self._route_key(device, channel)
                endpoint = self._endpoint_for_channel(device, channel)
                endpoint_name = endpoint.name if endpoint is not None else ''
                durable_tail = (self._durable_tail_route(device, channel)
                                if raw_route == protocol.ROUTE_EMPTY else None)

                if durable_tail is not None:
                    tail_endpoint = durable_tail.get('endpoint')
                    tail_endpoint_name = str(
                        durable_tail.get('endpoint_name', '') or '')
                    if (not durable_tail.get('routed') or
                            tail_endpoint_name in conflicts):
                        self.loaded_tools.pop(route_key, None)
                        self._uncertain_routes.add(route_key)
                        device_parts.append(
                            'C%d=TAIL/UNROUTED' % (channel + 1))
                        reconcile_error = True
                        continue
                    tool = self._tool_for_channel(device, channel)
                    if tool >= 0:
                        self.loaded_tools[route_key] = tool
                    else:
                        self.loaded_tools.pop(route_key, None)
                    self._uncertain_routes.discard(route_key)
                    if self.owns_filament_callbacks:
                        self._restore_endpoint_runtime_ownership(
                            tail_endpoint.name)
                        if hasattr(tail_endpoint, 'sync_active_filament'):
                            tail_endpoint.sync_active_filament(
                                self._channel_metadata(device, channel))

                    device_parts.append('C%d=TAIL' % (channel + 1))
                    continue

                if endpoint_name in conflicts and raw_route == protocol.ROUTE_LOADED:
                    self.loaded_tools.pop(route_key, None)
                    self._uncertain_routes.add(route_key)
                    device_parts.append('C%d=CONFLICT' % (channel + 1))
                    reconcile_error = True
                elif raw_route == protocol.ROUTE_LOADED:
                    tool = self._tool_for_channel(device, channel)
                    if endpoint is None:
                        self.loaded_tools.pop(route_key, None)
                        self._uncertain_routes.add(route_key)
                        device_parts.append(
                            'C%d=LOADED/UNROUTED' % (channel + 1))
                        reconcile_error = True
                    else:
                        if tool >= 0:
                            self.loaded_tools[route_key] = tool
                        else:
                            self.loaded_tools.pop(route_key, None)
                        self._uncertain_routes.discard(route_key)
                        if self.owns_filament_callbacks:
                            self._restore_endpoint_runtime_ownership(
                                endpoint.name)
                            if hasattr(endpoint, 'sync_active_filament'):
                                endpoint.sync_active_filament(
                                    self._channel_metadata(device, channel))
                        self._resume_follow_after_reconcile(device, channel)
                        device_parts.append('C%d=LOADED' % (channel + 1))
                elif raw_route == protocol.ROUTE_EMPTY:
                    self.loaded_tools.pop(route_key, None)
                    self._uncertain_routes.discard(route_key)
                    self._unmark_print_route(device, channel)
                    device_parts.append('C%d=EMPTY' % (channel + 1))
                else:
                    self.loaded_tools.pop(route_key, None)
                    self._uncertain_routes.add(route_key)
                    reconcile_error = True
                    device_parts.append('C%d=UNCERTAIN' % (channel + 1))
            self._drop_prestage_record(
                device.name,
                release_sensor=self.owns_filament_callbacks)
            reconciled.append('%s:%s' %
                              (device.name, ','.join(device_parts)))

        for endpoint_name in endpoint_names:
            if endpoint_name in conflicts:
                continue
            if not self._routes_for_endpoint(
                    endpoint_name,
                    (protocol.ROUTE_LOADED, protocol.ROUTE_UNCERTAIN)):
                endpoint = self.endpoints.get(endpoint_name)
                if (self.owns_filament_callbacks and
                        endpoint is not None and
                        endpoint.driver != 'snapmaker_u1'):
                    self._clear_endpoint_projection(endpoint)
        if reconcile_error:
            self._record_error(
                'ROUTE_RECONCILE_REQUIRED',
                details=('one or more BMCU Channel routes require physical '
                         'confirmation or routing repair'))
            self._safe_pause()
        self._save_runtime()

        if requested is None and not reconcile_error:
            self._refresh_u1_disconnect_hazards()
            self._reconcile_u1_leases(
                self.reactor.monotonic(), force=True)
            route_errors = {
                'ROUTE_UNCERTAIN', 'ROUTE_RECONCILE_REQUIRED', 'DEVICE_RESET',
                'LOADED_CHANNEL_UNMAPPED', 'LOADED_CHANNEL_UNROUTED',
                'TAIL_CHANNEL_UNROUTED', 'TAIL_REINSERT_CONFLICT',
                'ENDPOINT_MULTIPLE_CHANNELS', 'FOLLOW_NOT_RESUMED'}
            if isinstance(self.last_error, dict):
                code = self.last_error.get('code')
                if code in route_errors:
                    self.last_error = None
                elif (code in ('PRINT_END_UNLOAD_FAILED',
                               'PRINT_END_BACKGROUND_FAILED') and
                      not self.print_loaded_routes):
                    self.last_error = None
            self._sync_status_cache_runtime()
            self._clear_resolved_u1_startup_error()
        response = 'BMCU reconciled ' + ', '.join(reconciled)
        gcmd.respond_info(response)
