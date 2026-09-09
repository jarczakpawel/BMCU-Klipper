# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import logging

from . import protocol

class RefillError(RuntimeError):
    pass

class AutoRefillController(object):
    def __init__(self, manager):
        self.manager = manager
        self.reactor = manager.reactor
        self.transactions = {}
        self.print_backups = {}
        self._pending = set()
        self._toolchange_preemptions = set()
        self.manual_pending = None
        self.last = None

    def status(self):
        return {
            'active': {key: dict(value) for key, value in self.transactions.items()},
            'print_backups': {str(key): list(value) for key, value in self.print_backups.items()},
            'manual_pending': (dict(self.manual_pending)
                               if isinstance(self.manual_pending, dict) else None),
            'last': dict(self.last) if isinstance(self.last, dict) else self.last,
        }

    def clear_print(self):
        self.print_backups.clear()
        self.manual_pending = None

    def _route_key(self, device, channel):
        helper = getattr(self.manager, '_route_key', None)
        if callable(helper):
            return helper(device, int(channel))

        return '%s:%d' % (device.name, int(channel))

    def _effective_feed_timeout(self, device, maximum_mm, timeout_s):
        helper = getattr(self.manager, '_effective_feed_timeout', None)
        if callable(helper):
            return helper(device, maximum_mm, timeout_s)
        return float(timeout_s)

    def set_print_backup(self, source_tool, backup_tool, priority=100):
        source_tool = int(source_tool)
        backup_tool = int(backup_tool)
        values = [item for item in self.print_backups.get(source_tool, [])
                  if int(item.get('tool', -1)) != backup_tool]
        values.append({'tool': backup_tool, 'priority': int(priority)})
        values.sort(key=lambda item: (int(item.get('priority', 100)), int(item.get('tool', -1))))
        self.print_backups[source_tool] = values

    def _is_printing(self):
        print_stats = self.manager.printer.lookup_object('print_stats', None)
        if print_stats is not None:
            state = getattr(print_stats, 'state', None)
            if state is None and hasattr(print_stats, 'get_status'):
                try:
                    state = print_stats.get_status(self.reactor.monotonic()).get('state')
                except Exception:
                    state = None
            if str(state or '').lower() == 'printing':
                return True
        idle = self.manager.printer.lookup_object('idle_timeout', None)
        if idle is not None:
            try:
                return idle.get_status(self.reactor.monotonic()).get('state') == 'Printing'
            except Exception:
                pass
        return False

    def _refill_motion_cancelled(self):
        state_getter = getattr(self.manager, '_print_state', None)
        if not callable(state_getter):
            return False
        return str(state_getter() or '').lower() not in (
            'printing', 'paused', 'pause')

    def _endpoint_sensor_role(self, endpoint):
        resolver = getattr(endpoint, 'tail_sensor_role', None)
        if callable(resolver):
            return str(resolver() or '')
        for role in ('post_gears_sensor', 'motion_sensor', 'entry_sensor'):
            if endpoint.get(role, ''):
                return role
        return ''

    def _tail_path_mm(self, transaction, endpoint):
        resolver = getattr(
            self.manager, '_effective_channel_path_mm', None)
        if callable(resolver):
            value = resolver(
                transaction['source_device'],
                int(transaction['source_channel']), endpoint)
            if float(value or 0.0) > 0.0:
                return float(value)
        return 0.0

    def _continuous_trigger_mm(self, transaction, endpoint):
        uses_distance = getattr(endpoint, 'uses_distance_tail_tracking', None)
        if callable(uses_distance) and uses_distance():

            path = self._tail_path_mm(transaction, endpoint)
            handoff = float(
                endpoint.get('refill_handoff_max_mm', 120.0) or 120.0)
            reserve = float(endpoint.get('tail_reserve_mm', 20.0) or 20.0)
            if path > 0.0:
                return max(0.0, path - max(handoff, reserve))
            return -1.0
        configured = float(endpoint.get('tail_to_output_mm', 0.0) or 0.0)
        if configured > 0.0:
            return configured
        return -1.0

    def _extruder_position(self, endpoint):
        name = str(endpoint.get('extruder', 'extruder') or 'extruder')
        obj = self.manager.printer.lookup_object(name, None)
        if obj is not None:
            for attribute in ('last_position', 'position'):
                value = getattr(obj, attribute, None)
                if isinstance(value, (int, float)):
                    return float(value)
        toolhead = self.manager.printer.lookup_object('toolhead', None)
        if toolhead is not None:
            try:
                obj = toolhead.get_extruder()
                value = getattr(obj, 'last_position', None)
                if isinstance(value, (int, float)):
                    return float(value)
            except Exception:
                pass
        move = self.manager.printer.lookup_object('gcode_move', None)
        if move is not None:
            try:
                status = move.get_status(self.reactor.monotonic())

                position = status.get('position')
                if position and len(position) > 3:
                    return float(position[3])
            except Exception:
                pass
        return None

    @staticmethod
    def _advance_consumption(transaction, current_e):
        if current_e is None:
            return
        current = float(current_e)
        previous = transaction.get('last_e')
        transaction['last_e'] = current
        if previous is None:
            transaction['e_high_water'] = current
            return
        previous = float(previous)
        high_water = float(transaction.get('e_high_water', previous))

        if current > high_water:
            transaction['consumed_mm'] = (float(transaction.get('consumed_mm', 0.0)) +
                                          (current - high_water))
            transaction['e_high_water'] = current

    def _metadata_matches(self, source, candidate, mode):
        source_material = str(source.get('material', '') or '').strip().upper()
        candidate_material = str(candidate.get('material', '') or '').strip().upper()
        source_color = str(source.get('color', '') or '').strip().lstrip('#').upper()
        candidate_color = str(candidate.get('color', '') or '').strip().lstrip('#').upper()
        if mode == 'exact':
            if not (source_material and source_material == candidate_material and
                    source_color and source_color == candidate_color):
                return False
            for key in ('vendor', 'subtype', 'profile_id'):
                left = str(source.get(key, '') or '').strip().upper()
                right = str(candidate.get(key, '') or '').strip().upper()
                if left != right:
                    return False
            def color_identity(metadata, primary):
                raw = metadata.get('colors', [])
                if not isinstance(raw, (list, tuple)):
                    raw = []
                values = []
                for value in [primary] + list(raw):
                    value = str(value or '').strip().lstrip('#').upper()
                    if value and value not in values:
                        values.append(value)
                    if len(values) == 5:
                        break
                try:
                    mode_value = int(metadata.get('color_mode', 0) or 0) & 0xff
                except (TypeError, ValueError):
                    mode_value = 0
                return tuple(values), mode_value
            return (color_identity(source, source_color) ==
                    color_identity(candidate, candidate_color))
        if mode == 'material':
            return bool(source_material and source_material == candidate_material)
        if mode == 'group':
            group = str(source.get('refill_group', '')).strip()
            return bool(group and group == str(candidate.get('refill_group', '')).strip())
        return mode == 'any'

    def _candidate_from_channel(self, device, channel, source_device,
                                source_channel, endpoint, tool=-1):
        channel = int(channel)
        if (source_device is not None and
                device.name == source_device.name and
                channel == int(source_channel)):
            return None
        try:
            candidate_endpoint = self.manager._endpoint_for_channel(
                device, channel)
        except Exception:
            return None
        if candidate_endpoint is None:
            return None
        metadata = self.manager._channel_metadata(device, channel)
        if not bool(metadata.get('refill_enabled', True)):
            return None

        route_reader = getattr(self.manager, '_route_state', None)
        route_state = (route_reader(device, channel) if callable(route_reader)
                       else protocol.ROUTE_EMPTY)
        if route_state != protocol.ROUTE_EMPTY:
            return None
        try:
            self.manager._check_automatic_ready(device, channel)
        except Exception:
            return None
        planner = getattr(self.manager, 'refill_route_plan', None)
        plan = (planner(endpoint, candidate_endpoint) if callable(planner) else
                {'supported': candidate_endpoint.name == endpoint.name,
                 'mode': ('same_endpoint' if candidate_endpoint.name == endpoint.name
                          else 'unsupported')})
        if not plan.get('supported'):
            return None
        if candidate_endpoint.name != endpoint.name:
            try:
                require_exclusive = getattr(
                    self.manager, '_require_exclusive_endpoint_route', None)
                if callable(require_exclusive):
                    require_exclusive(device, candidate_endpoint, channel)
            except Exception:
                return None
        return {
            'tool': int(tool), 'device': device, 'channel': channel,
            'endpoint': candidate_endpoint, 'metadata': metadata,
            'route_plan': plan,
            'cross_endpoint': candidate_endpoint.name != endpoint.name,
        }

    def _candidate_from_tool(self, tool, source_device, source_channel, endpoint):
        try:
            device, channel, _candidate_endpoint = self.manager._resolve_tool(
                int(tool))
        except Exception:
            return None
        return self._candidate_from_channel(
            device, channel, source_device, source_channel, endpoint,
            tool=int(tool))

    def _same_channel_reinsert_candidate(
            self, source_tool, source_device, source_channel, endpoint):

        source_channel = int(source_channel)
        present = source_device.status.get('present', [0, 0, 0, 0])
        if (source_channel >= len(present) or
                not bool(present[source_channel])):
            return None
        u1_matcher = getattr(
            self.manager, '_u1_tail_detached_matches', None)
        generic_matcher = getattr(
            self.manager, '_generic_tail_detached_matches', None)
        detached = bool(
            (callable(u1_matcher) and
             u1_matcher(endpoint, source_device, source_channel)) or
            (callable(generic_matcher) and
             generic_matcher(endpoint, source_device, source_channel)))
        if not detached:
            return None
        metadata = self.manager._channel_metadata(
            source_device, source_channel)
        return {
            'tool': int(source_tool),
            'device': source_device,
            'channel': source_channel,
            'endpoint': endpoint,
            'metadata': metadata,
            'route_plan': {
                'supported': True, 'mode': 'same_endpoint',
                'reason': 'same exhausted Channel reinsert follower',
            },
            'cross_endpoint': False,
            'same_source_channel': True,
            'selection': 'same_channel_reinsert',
            'priority': -1,
        }

    def _cross_endpoint_preserves_plan(self, source_tool, source_endpoint,
                                       candidate):
        if not candidate.get('cross_endpoint'):
            return True
        if (source_endpoint.driver != 'snapmaker_u1' or
                candidate['endpoint'].driver != 'snapmaker_u1'):
            return True

        return bool(self._endpoint_sensor_role(source_endpoint))

    def choose_candidate(self, source_tool, source_device, source_channel, endpoint):

        for item in self.print_backups.get(int(source_tool), []):
            candidate = self._candidate_from_tool(
                item.get('tool'), source_device, source_channel, endpoint)
            if (candidate is not None and
                    self._cross_endpoint_preserves_plan(
                        source_tool, endpoint, candidate)):
                candidate['selection'] = 'print_map'
                return candidate

        source = self.manager._channel_metadata(source_device, source_channel)
        mode = str(endpoint.get('refill_match', 'exact')).lower()
        candidates = []
        effective = (self.manager.print_tools
                     if self.manager.print_map_active else {})
        seen = set()
        for tool_text in effective:
            try:
                tool = int(tool_text)
            except (TypeError, ValueError):
                continue
            candidate = self._candidate_from_tool(
                tool, source_device, source_channel, endpoint)
            if candidate is None:
                continue
            if not self._cross_endpoint_preserves_plan(
                    source_tool, endpoint, candidate):
                continue
            key = (candidate['device'].name, candidate['channel'])
            if key in seen:
                continue
            seen.add(key)
            metadata = candidate['metadata']
            if not self._metadata_matches(source, metadata, mode):
                continue
            candidate['selection'] = mode
            candidate['priority'] = int(metadata.get('refill_priority', candidate['channel']))
            candidates.append(candidate)

        for device in self.manager.devices:
            for channel in range(4):
                key = (device.name, channel)
                if key in seen:
                    continue
                candidate = self._candidate_from_channel(
                    device, channel, source_device, source_channel, endpoint,
                    tool=int(source_tool))
                if candidate is None:
                    continue
                if not self._cross_endpoint_preserves_plan(
                        source_tool, endpoint, candidate):
                    continue
                seen.add(key)
                metadata = candidate['metadata']
                if not self._metadata_matches(source, metadata, mode):
                    continue
                candidate['selection'] = mode
                candidate['priority'] = int(
                    metadata.get('refill_priority', channel))
                candidates.append(candidate)

        candidates.sort(key=lambda item: (
            1 if item.get('cross_endpoint') else 0,
            item.get('priority', 0), item['device'].name,
            item['channel'], item['tool']))
        return candidates[0] if candidates else None

    @staticmethod
    def _u1_projected_metadata(metadata):
        raw_material = str(metadata.get('material', '') or '').strip()
        material = ('Unknown' if not raw_material or
                    raw_material.upper() in ('NONE', 'UNKNOWN') else
                    raw_material)
        vendor = str(metadata.get('vendor', '') or 'generic').strip() or 'generic'
        subtype = str(metadata.get('subtype', '') or
                      metadata.get('profile_id', '') or
                      'generic').strip() or 'generic'
        color = str(metadata.get('color', '#FFFFFF')).lstrip('#').upper()
        if len(color) != 6 or any(ch not in '0123456789ABCDEF' for ch in color):
            color = 'FFFFFF'
        raw_colors = metadata.get('colors', [])
        if not isinstance(raw_colors, (list, tuple)):
            raw_colors = []
        colors = [color]
        for item in raw_colors:
            item = str(item or '').lstrip('#').upper()
            if (len(item) == 6 and
                    all(ch in '0123456789ABCDEF' for ch in item) and
                    item not in colors):
                colors.append(item)
            if len(colors) == 5:
                break
        try:
            mode = int(metadata.get('color_mode', 0) or 0) & 0xff
        except (TypeError, ValueError, OverflowError):
            mode = 0
        return {
            'vendor': vendor,
            'material': material,
            'subtype': subtype,
            'color_multi': {
                'nums': len(colors), 'alpha': 255,
                'colors': colors, 'mode': mode,
            },
        }

    def _u1_task_metadata(self, head, backup=False, native_baseline=True):
        head = int(head)
        task, config = self.manager._u1_task_config()
        if task is None or config is None or head < 0 or head >= 4:
            return None
        source = None
        if backup:
            source = getattr(task, 'filament_info_backup', None)
        elif native_baseline:
            endpoint = self.manager._u1_endpoint_for_head(head)
            if endpoint is not None:
                record = self.manager._u1_ownership_record(endpoint.name)
                baseline = record.get('baseline_filament', {})
                if (record.get('generation_open') and
                        record.get('baseline_captured')):
                    if not isinstance(baseline, dict) or not baseline:
                        return None
                    source = baseline
        if source is None:
            source = config
        def read(key, default=None):
            value = source.get(key, default) if isinstance(source, dict) else default
            if isinstance(value, list):
                return copy.deepcopy(value[head]) if head < len(value) else default
            return copy.deepcopy(value)
        vendor = read('filament_vendor', 'NONE')
        material = read('filament_type', 'NONE')
        subtype = read('filament_sub_type', '')
        color_multi = read('filament_color_multi', {})
        if not isinstance(color_multi, dict):
            color_multi = {}
        return {
            'vendor': str(vendor or ''),
            'material': str(material or ''),
            'subtype': str(subtype or ''),
            'color_multi': color_multi,
        }

    @staticmethod
    def _u1_stock_match(source, candidate, ignore_color):
        if not isinstance(source, dict) or not isinstance(candidate, dict):
            return None
        if (not source.get('vendor') or source.get('vendor') == 'NONE' or
                not source.get('material') or source.get('material') == 'NONE'):
            return None
        if (candidate.get('vendor') != source.get('vendor') or
                candidate.get('material') != source.get('material') or
                candidate.get('subtype') != source.get('subtype')):
            return None
        if candidate.get('color_multi') == source.get('color_multi'):
            return 0
        return 1 if ignore_color else None

    def _u1_nozzle_diameter(self, head):
        head = int(head)
        name = 'extruder' if head == 0 else 'extruder%d' % head
        obj = self.manager.printer.lookup_object(name, None)
        try:
            return float(obj.nozzle_diameter) if obj is not None else None
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None

    def _u1_native_candidate(self, head, source_kind, source_head,
                             source_device=None, source_channel=-1):
        head = int(head)
        endpoint = self.manager._u1_endpoint_for_head(head)
        if endpoint is None:
            return None
        loaded = self.manager._loaded_devices_for_endpoint(endpoint.name)
        if loaded:
            allowed = bool(
                source_kind == 'bmcu' and head == int(source_head) and
                len(loaded) == 1 and loaded[0][0] is source_device and
                int(loaded[0][1]) == int(source_channel))
            if not allowed:
                return None
        try:
            status = self.manager._u1_native_feeder_status(endpoint, strict=False)
        except Exception:
            return None
        if not isinstance(status, dict) or not status.get('module_exist'):
            return None
        sensor = self.manager.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor is None:
            return None
        try:
            sensor_enabled = bool(sensor.get_status(0).get('enabled'))
        except Exception:
            return None
        record = self.manager._u1_ownership_record(endpoint.name)
        takeover = bool(
            record.get('generation_open') and record.get('baseline_captured'))
        if takeover and getattr(endpoint, 'runtime_sensor_restore', None):
            sensor_enabled = any(bool(value) for value in
                                 endpoint.runtime_sensor_restore.values())
        enabled = (not bool(record.get('baseline_disabled', False))
                   if takeover else not bool(status.get('disable_auto', False)))
        detected = bool(status.get('filament_detected', False))
        current_reinsert = bool(
            source_kind == 'native' and head == int(source_head) and
            enabled and detected and sensor_enabled)
        loaded_native = bool(
            not takeover and
            not (source_kind == 'native' and head == int(source_head)) and
            str(status.get('channel_state', '') or '').strip().lower() ==
            'load_finish')
        if not current_reinsert and not loaded_native and not (
                enabled and detected and sensor_enabled):
            return None
        metadata = self._u1_task_metadata(head, backup=False,
                                          native_baseline=takeover)
        return {
            'kind': 'native', 'head': head, 'endpoint': endpoint,
            'metadata': metadata, 'device': None, 'channel': -1,
            'tool': -1, 'cross_endpoint': head != int(source_head),
            'current_reinsert': current_reinsert,
        }

    def choose_u1_candidate(self, source_kind, source_head, source_metadata,
                            source_tool=-1, source_device=None,
                            source_channel=-1, source_endpoint=None):
        task, config = self.manager._u1_task_config()
        if task is None or config is None:
            return None
        if not bool(config.get('auto_replenish_filament', False)):
            return None
        ignore_color = bool(config.get('replenish_ignore_color', False))
        source_nozzle = self._u1_nozzle_diameter(source_head)
        if source_nozzle is None:
            return None
        candidates = []
        for head in range(4):
            if self._u1_nozzle_diameter(head) != source_nozzle:
                continue
            candidate = self._u1_native_candidate(
                head, source_kind, source_head,
                source_device=source_device, source_channel=source_channel)
            if candidate is None:
                continue
            if candidate.get('current_reinsert'):
                candidate['match_rank'] = -1
            else:
                rank = self._u1_stock_match(
                    source_metadata, candidate.get('metadata'), ignore_color)
                if rank is None:
                    continue
                candidate['match_rank'] = rank
            candidate['kind_rank'] = 0
            candidate['priority'] = head
            candidates.append(candidate)
        for device in self.manager.devices:
            for channel in range(4):
                candidate = self._candidate_from_channel(
                    device, channel, source_device, source_channel,
                    source_endpoint, tool=int(source_tool))
                if candidate is None:
                    continue
                endpoint = candidate['endpoint']
                if endpoint.driver != 'snapmaker_u1':
                    continue
                head = int(endpoint.get('head_index', -1))
                if head < 0 or head >= 4 or self._u1_nozzle_diameter(head) != source_nozzle:
                    continue
                if (source_endpoint is not None and
                        not self._cross_endpoint_preserves_plan(
                            source_tool, source_endpoint, candidate)):
                    continue
                if source_kind == 'native':
                    try:
                        native_path = endpoint.native_path_status()
                    except Exception:
                        continue
                    if (not native_path.get('known') or
                            native_path.get('busy')):
                        continue
                metadata = self._u1_projected_metadata(candidate['metadata'])
                rank = self._u1_stock_match(source_metadata, metadata, ignore_color)
                if rank is None:
                    continue
                candidate['kind'] = 'bmcu'
                candidate['head'] = head
                candidate['stock_metadata'] = metadata
                candidate['match_rank'] = rank
                candidate['kind_rank'] = 1
                candidate['priority'] = int(
                    candidate['metadata'].get('refill_priority', channel))
                candidates.append(candidate)
        candidates.sort(key=lambda item: (
            int(item.get('match_rank', 99)),
            int(item.get('kind_rank', 1)),
            1 if item.get('cross_endpoint') else 0,
            int(item.get('priority', 0)),
            str(item['device'].name if item.get('device') is not None else ''),
            int(item.get('channel', -1))))
        return candidates[0] if candidates else None

    def handle_u1_native_runout(self, gcmd):
        task, config = self.manager._u1_task_config()
        if task is None or config is None:
            return False
        task.perform_auto_replenish = False
        if not bool(config.get('auto_replenish_filament', False)):
            return False
        try:
            source_head = int(gcmd.get_int('EXTRUDER'))
        except Exception:
            return False
        if source_head < 0 or source_head >= 4:
            return False
        toolhead = self.manager.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return False
        try:
            if int(toolhead.get_extruder().extruder_index) != source_head:
                return False
        except Exception:
            return False
        stats = self.manager.printer.lookup_object('print_stats', None)
        if stats is None or str(getattr(stats, 'state', '')).lower() != 'paused':
            return False
        if bool(getattr(task, 'is_exec_print_end_action', False)):
            return False
        source = self._u1_task_metadata(
            source_head, backup=True, native_baseline=False)
        if source is None or source.get('material') in ('', 'NONE'):
            return False
        candidate = self.choose_u1_candidate(
            'native', source_head, source, source_tool=source_head,
            source_device=None, source_channel=-1,
            source_endpoint=self.manager._u1_endpoint_for_head(source_head))
        if candidate is None or candidate.get('kind') != 'bmcu':
            return False
        target_endpoint = candidate['endpoint']
        target_head = int(candidate['head'])
        aliases = [index for index, head in enumerate(
            config.get('extruder_map_table', [])) if int(head) == source_head]
        active_tool = int(getattr(self.manager, 'active_tool', -1))
        if active_tool not in aliases:
            active_tool = source_head if source_head in aliases else (
                aliases[0] if aliases else source_head)
        self.manager._prepare_native_path_for_bmcu(
            target_endpoint, candidate['device'], candidate['channel'])
        self.manager.load_channel(
            candidate['device'], candidate['channel'], tool=active_tool,
            prime_handshake=False,
            temperature_profile=self.manager._u1_active_temperature_profile())
        mapping = self.manager._mapping_payload(
            candidate['device'], candidate['channel'])
        aliases = self.manager._apply_u1_stock_style_replenish_map(
            source_head, target_head, route_mapping=mapping)
        self.manager.loaded_tools[
            self.manager._route_key(candidate['device'], candidate['channel'])] = active_tool
        self.manager.active_tool = active_tool
        self.manager._mark_print_route_loaded(
            candidate['device'], candidate['channel'], active_tool)
        self.manager._activate_u1_logical_tool(active_tool, target_endpoint)
        logging.info(
            'BMCU mixed auto replenish: native Head %d -> %s Channel %d Head %d, aliases=%s',
            source_head + 1, candidate['device'].name,
            int(candidate['channel']) + 1, target_head + 1, aliases)
        self.manager._resume_u1_stock_replenish(target_head, target_endpoint)
        return True

    def on_channel_empty(self, device, channel, source_tool, endpoint):
        key = '%s:%d' % (device.name, int(channel))
        if key in self._pending or key in self.transactions:
            return False
        if isinstance(self.manual_pending, dict):
            return False
        if not bool(endpoint.get('tail_runout_enabled', True)):
            return False
        if not self._is_printing():
            return False
        self._pending.add(key)
        manual_generic = bool(
            endpoint.driver == 'generic_single_extruder' and
            not self._endpoint_sensor_role(endpoint))
        if manual_generic:
            try:
                self.reactor.register_callback(
                    lambda eventtime, d=device, ch=int(channel),
                           t=int(source_tool), ep=endpoint:
                    self._run(eventtime, d, ch, t, ep),
                    waketime=(self.reactor.monotonic() +
                              self.manager.manager_work_yield_interval))
            except Exception:
                self._pending.discard(key)
                logging.exception(
                    'BMCU could not schedule Generic manual runout handling')
                return False
            return True
        queued = self.manager._queue_deferred_task(
            'refill:%s' % key,
            lambda eventtime, d=device, ch=int(channel),
                   t=int(source_tool), ep=endpoint:
            self._run(eventtime, d, ch, t, ep))
        if not queued:
            self._pending.discard(key)
            return False
        return True

    def _set_manual_pending(self, device, channel, source_tool, endpoint):
        self.manual_pending = {
            'device': device.name,
            'device_uid': str(self.manager._device_uid(device) or ''),
            'channel': int(channel),
            'tool': int(source_tool),
            'endpoint': endpoint.name,
            'started': self.reactor.monotonic(),
            'reason': 'runout_waiting_for_same_channel_replacement',
        }
        sync_cache = getattr(self.manager, '_sync_status_cache_runtime', None)
        if callable(sync_cache):
            sync_cache()
        return dict(self.manual_pending)

    def _manual_pending_context(self):
        pending = self.manual_pending
        if not isinstance(pending, dict):
            return None
        device = self.manager.devices_by_name.get(str(pending.get('device', '') or ''))
        if device is None:
            raise RefillError('manual refill BMCU device is unavailable')
        expected_uid = str(pending.get('device_uid', '') or '').upper()
        if expected_uid and self.manager._device_uid(device) != expected_uid:
            raise RefillError('manual refill BMCU identity changed')
        channel = int(pending.get('channel', -1))
        if channel < 0 or channel > 3:
            raise RefillError('manual refill Channel is invalid')
        endpoint = self.manager._endpoint_for_channel(device, channel)
        if endpoint is None or endpoint.name != str(pending.get('endpoint', '') or ''):
            raise RefillError('manual refill endpoint assignment changed')
        if endpoint.driver not in ('generic_single_extruder', 'snapmaker_u1'):
            raise RefillError('manual refill endpoint type is unsupported')
        return pending, device, channel, int(pending.get('tool', -1)), endpoint

    def resume_manual_refill(self, gcmd=None):
        context = self._manual_pending_context()
        if context is None:
            return False
        pending, device, channel, tool, endpoint = context
        if self.manager._print_state() not in ('paused', 'pause'):
            raise RefillError('printer must remain paused during manual refill')
        if not device.ready or not device.status_reconciled:
            raise RefillError('BMCU is not ready for manual refill')
        device.refresh()
        present = device.status.get('present', [False] * 4)
        if channel >= len(present) or not bool(present[channel]):
            raise RefillError(
                'insert replacement filament into %s Channel %d, then press Resume again' %
                (device.name, channel + 1))
        if endpoint.driver == 'snapmaker_u1':
            detached = self.manager._u1_tail_detached_matches(
                endpoint, device, channel)
            if detached and not self.manager._u1_tail_sensor_boundary_cleared(
                    endpoint, device, channel):
                raise RefillError(
                    'old filament has not cleared the Snapmaker head sensor yet')
        else:
            detached = self.manager._generic_tail_detached_matches(
                endpoint, device, channel)
        if not detached:
            raise RefillError('manual refill detached-tail state is unavailable')

        self.last = {
            'source_device_name': device.name,
            'source_channel': channel,
            'source_tool': tool,
            'endpoint': endpoint.name,
            'phase': 'MANUAL_REFILL_LOAD',
            'result': 'loading',
        }
        try:
            temperature_profile = (
                self.manager._u1_active_temperature_profile()
                if endpoint.driver == 'snapmaker_u1' else None)
            self.manager.load_channel(
                device, channel, gcmd=None, tool=tool,
                prime_handshake=False,
                temperature_profile=temperature_profile)
        except Exception as exc:
            self.last.update({'result': 'failed', 'error': str(exc)})
            raise

        self.manual_pending = None
        sync_cache = getattr(self.manager, '_sync_status_cache_runtime', None)
        if callable(sync_cache):
            sync_cache()
        self.last = {
            'source_device_name': device.name,
            'source_channel': channel,
            'source_tool': tool,
            'endpoint': endpoint.name,
            'phase': 'MANUAL_REFILL_COMPLETE',
            'result': 'ok',
        }
        return True

    def trigger_now(self, source_tool):
        device, channel, endpoint = self.manager._resolve_tool(int(source_tool))
        return self.on_channel_empty(device, channel, int(source_tool), endpoint)

    def preempt_for_toolchange(self, device, channel, endpoint, timeout=5.0):

        key = '%s:%d' % (device.name, int(channel))
        transaction = self.transactions.get(key)
        if transaction is None and key not in self._pending:
            return None
        if transaction is not None:
            phase = str(transaction.get('phase', '') or '')
            if phase not in ('TAIL_DRAIN', 'RUNOUT_DEBOUNCE'):
                raise RefillError(
                    'runout recovery on %s is already in phase %s and cannot '
                    'be preempted by a tool change' % (endpoint.name, phase))
            transaction['preempt_for_toolchange'] = True
        self._toolchange_preemptions.add(key)
        deadline = self.reactor.monotonic() + max(0.5, float(timeout or 5.0))
        snapshot = dict(transaction) if isinstance(transaction, dict) else None
        try:
            while key in self._pending or key in self.transactions:
                if self.reactor.monotonic() >= deadline:
                    raise RefillError(
                        'timed out preempting runout-tail monitor on %s' %
                        endpoint.name)
                current = self.transactions.get(key)
                if isinstance(current, dict):
                    phase = str(current.get('phase', '') or '')
                    if phase not in ('TAIL_DRAIN', 'RUNOUT_DEBOUNCE',
                                      'PREEMPTED_FOR_TOOLCHANGE'):
                        raise RefillError(
                            'runout recovery on %s advanced to phase %s while '
                            'tool change was waiting' % (endpoint.name, phase))
                    current['preempt_for_toolchange'] = True
                    snapshot = dict(current)
                self.reactor.pause(self.reactor.monotonic() + 0.025)
            return snapshot
        finally:
            self._toolchange_preemptions.discard(key)

    def _tail_deadline_reached(self, transaction, endpoint, sensor_role):
        reserve = float(endpoint.get('tail_reserve_mm', 20.0) or 20.0)
        consumed = float(transaction.get('consumed_mm', 0.0))
        if sensor_role:
            detected = endpoint.sensor_detected(sensor_role)
            transaction['endpoint_sensor'] = detected
            if detected is not False:
                return False
            if 'sensor_runout_consumed_mm' not in transaction:
                transaction['sensor_runout_consumed_mm'] = consumed
            transaction['consumed_after_sensor_mm'] = max(
                0.0, consumed - float(transaction['sensor_runout_consumed_mm']))

            if getattr(endpoint, 'driver', '') == 'snapmaker_u1':
                transaction['u1_source_sensor_cleared'] = True
                marker = getattr(
                    self.manager, '_mark_u1_tail_sensor_cleared', None)
                if callable(marker):
                    marker(
                        endpoint, transaction['source_device'],
                        int(transaction['source_channel']),
                        'passive runout monitor observed head sensor clear')
                return True

            remaining = float(endpoint.get('sensor_tail_remaining_mm', 0.0) or 0.0)
            return transaction['consumed_after_sensor_mm'] >= max(
                0.0, remaining - reserve)
        tail_remaining = self._tail_path_mm(transaction, endpoint)
        transaction['tail_path_mm'] = tail_remaining
        if tail_remaining <= 0.0:
            transaction['needs_tail_configuration'] = True
            return True
        transaction['tail_remaining_estimate_mm'] = max(
            0.0, tail_remaining - consumed)
        return consumed >= max(0.0, tail_remaining - reserve)

    def _can_continuous(self, transaction, endpoint, candidate):

        if endpoint.driver == 'generic_single_extruder':
            return False
        if str(endpoint.get('refill_mode', 'pause')).lower() != 'continuous':
            return False
        if candidate is None or candidate.get('selection') not in ('exact', 'print_map'):
            return False

        if candidate.get('cross_endpoint'):
            return False
        source_meta = transaction['source_metadata']
        candidate_meta = candidate['metadata']
        if not self._metadata_matches(source_meta, candidate_meta, 'exact'):
            return False
        return self._continuous_trigger_mm(transaction, endpoint) >= 0.0

    def _continuous_handoff(self, transaction, candidate, endpoint):

        manager = self.manager
        source_device = transaction['source_device']
        source_channel = int(transaction['source_channel'])
        device = candidate['device']
        channel = int(candidate['channel'])
        if candidate.get('cross_endpoint'):
            return False
        if (source_device.name == device.name and
                source_channel == channel):
            return False
        if candidate['endpoint'].name != endpoint.name:
            return False
        if not self._metadata_matches(
                transaction['source_metadata'], candidate['metadata'],
                'exact'):
            return False
        source_present = source_device.status.get('present', [])
        if (source_channel >= len(source_present) or
                bool(source_present[source_channel])):
            return False

        transaction['phase'] = 'CONTINUOUS_CONTACT'
        manager._check_automatic_ready(device, channel)
        target_key = self._route_key(device, channel)
        occupied = manager._loaded_devices_for_endpoint(
            endpoint.name, excluded_route=target_key)
        unexpected = [
            (loaded_device, loaded_channel)
            for loaded_device, loaded_channel in occupied
            if not (loaded_device.name == source_device.name and
                    int(loaded_channel) == source_channel)
        ]
        if unexpected:
            raise RefillError(
                'continuous refill endpoint %s has another occupied route' %
                endpoint.name)

        maximum_mm = float(endpoint.get(
            'max_route_mm', manager.max_route_mm) or manager.max_route_mm)
        contact_pct = manager._device_loading_handoff_pct(
            device, endpoint, refill=True)
        timeout_s = float(endpoint.get('refill_timeout', 75.0) or 75.0)
        timeout_s = self._effective_feed_timeout(
            device, maximum_mm, timeout_s)
        op_id = device.start_feed_to_contact(
            channel, maximum_mm, contact_pct, int(timeout_s * 1000.0))

        result = manager._wait_feed_operation(
            device, op_id, timeout_s + 2.0, None, '')
        transaction['continuous_contact'] = dict(result or {})
        if (not isinstance(result, dict) or not result.get('ok') or
                str(result.get('reason', '') or '') != 'contact'):
            raise RefillError(
                'continuous replacement did not obtain controller contact: %s'
                % str((result or {}).get('reason', 'unknown')))
        measured_mm = float(result.get('measured_mm', 0.0) or 0.0)

        if measured_mm <= 0.0:
            raise RefillError(
                'continuous replacement reported contact without encoder travel')
        device.refresh()
        target_present = device.status.get('present', [])
        if (channel >= len(target_present) or
                not bool(target_present[channel])):
            raise RefillError(
                'continuous replacement disappeared after contact')
        if not (int(device.status.get('encoder_io_mask', 0) or 0) &
                (1 << channel)):
            raise RefillError(
                'continuous replacement encoder became unavailable after contact')

        if endpoint.driver == 'snapmaker_u1':
            arm = getattr(manager, '_arm_u1_follower_commit', None)
            if not callable(arm):
                raise RefillError(
                    'Snapmaker continuous refill has no follower journal')
            arm(
                endpoint, source_device, source_channel, device, channel,
                logical_tool=int(transaction['source_tool']))
        else:
            marker = getattr(manager, '_mark_generic_tail_detached', None)
            matcher = getattr(manager, '_generic_tail_detached_matches', None)
            if (callable(matcher) and not matcher(
                    endpoint, source_device, source_channel)):
                if not callable(marker):
                    raise RefillError(
                        'generic continuous refill has no detached-tail journal')
                marker(
                    endpoint, source_device, source_channel,
                    'continuous refill reached irreversible follower contact')
            arm = getattr(manager, '_arm_generic_follower_commit', None)
            if not callable(arm):
                raise RefillError(
                    'generic continuous refill has no follower journal')
            arm(
                endpoint, source_device, source_channel, device, channel,
                logical_tool=int(transaction['source_tool']))

        device.mark_loaded(channel)
        device.refresh()
        if manager._route_states_from_status(
                device.status)[channel] != protocol.ROUTE_LOADED:
            raise RefillError(
                'continuous replacement did not commit target LOADED')
        source_device.mark_unloaded(source_channel)
        source_device.refresh()
        if manager._route_states_from_status(
                source_device.status)[source_channel] != protocol.ROUTE_EMPTY:
            raise RefillError(
                'continuous refill could not commit exhausted source EMPTY')

        if endpoint.driver == 'snapmaker_u1':
            finalize = getattr(
                manager, '_finalize_snapmaker_tail_handoff_after_follower',
                None)
            if (not callable(finalize) or not finalize(
                    source_device, source_channel, endpoint,
                    follower_device=device, follower_channel=channel,
                    logical_tool=int(transaction['source_tool']),
                    reason=('continuous exact-match follower obtained BMCU '
                            'contact and committed LOADED'))):
                raise RefillError(
                    'Snapmaker continuous follower journal could not commit')
        else:
            finalize = getattr(
                manager, '_finalize_generic_tail_handoff_after_follower',
                None)
            if (not callable(finalize) or not finalize(
                    source_device, source_channel, endpoint,
                    follower_device=device, follower_channel=channel,
                    reason=('generic continuous exact-match follower obtained '
                            'contact and committed LOADED'))):
                raise RefillError(
                    'generic continuous follower journal could not commit')
        transaction['detached_tail_finalized'] = True

        device.set_motion(channel, protocol.MOTION_ON_USE)
        transaction['continuous_contact_mm'] = measured_mm
        transaction['_replacement_physically_captured'] = True
        self._activate_candidate(transaction, candidate, endpoint)
        return True

    def _activate_candidate(self, transaction, candidate, endpoint,
                            cross_endpoint=False):
        source_device = transaction['source_device']
        source_channel = int(transaction['source_channel'])
        source_tool = int(transaction['source_tool'])
        device = candidate['device']
        channel = int(candidate['channel'])
        source_key = self._route_key(source_device, source_channel)
        candidate_key = self._route_key(device, channel)
        same_route = bool(
            source_device.name == device.name and
            source_channel == channel)

        source_device.set_motion(source_channel, protocol.MOTION_IDLE)
        if not cross_endpoint and not same_route:

            source_device.mark_unloaded(source_channel)
            source_device.refresh()
            source_raw = self.manager._route_states_from_status(
                source_device.status)[source_channel]
            if source_raw != protocol.ROUTE_EMPTY:
                raise RefillError(
                    'exhausted source route did not remain EMPTY after '
                    'replacement capture')
        elif cross_endpoint:

            transaction['source_route_retained'] = True

        device.mark_loaded(channel)
        device.refresh()
        candidate_raw = self.manager._route_states_from_status(
            device.status)[channel]
        if candidate_raw != protocol.ROUTE_LOADED:
            raise RefillError(
                'replacement %s Channel %d did not commit LOADED' %
                (device.name, channel + 1))

        finalized = bool(transaction.get('detached_tail_finalized', False))
        if not cross_endpoint:
            finalize = getattr(
                self.manager,
                '_finalize_snapmaker_tail_handoff_after_follower', None)
            if not finalized and callable(finalize):
                finalized = bool(finalize(
                    source_device, source_channel, endpoint,
                    follower_device=device, follower_channel=channel,
                    logical_tool=source_tool,
                    reason=('same-head refill follower physically captured, '
                            'primed and committed LOADED')))
            if not finalized:
                generic_finalize = getattr(
                    self.manager,
                    '_finalize_generic_tail_handoff_after_follower', None)
                if callable(generic_finalize):
                    finalized = bool(generic_finalize(
                        source_device, source_channel, endpoint,
                        follower_device=device, follower_channel=channel,
                        reason=('generic same-hotend follower physically '
                                'captured and committed LOADED')))
            if finalized:
                transaction['detached_tail_finalized'] = True
            if not finalized and not same_route:
                self.manager.loaded_tools.pop(source_key, None)
                unmark = getattr(self.manager, '_unmark_print_route', None)
                if callable(unmark):
                    unmark(source_device, source_channel)

        device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
        self.reactor.pause(self.reactor.monotonic() + 0.2)
        device.set_motion(channel, protocol.MOTION_ON_USE)

        if not finalized:

            mapping = self.manager._mapping_payload(device, channel)
            self.manager.print_tools[str(source_tool)] = mapping
            self.manager.print_map_active = True
            self.manager.loaded_tools[candidate_key] = source_tool
            self.manager.active_tool = source_tool
            mark = getattr(self.manager, '_mark_print_route_loaded', None)
            if callable(mark):
                mark(device, channel, source_tool)
            self.manager._save_runtime()
            if hasattr(self.manager, '_save_print_session'):
                self.manager._save_print_session()
        endpoint.activate_runtime_sensor_takeover()
        if hasattr(endpoint, 'sync_active_filament'):
            endpoint.sync_active_filament(candidate['metadata'])

    def _drain_u1_source_tail(self, transaction, endpoint,
                              temperature_profile=None):

        if endpoint.driver != 'snapmaker_u1':
            return False
        role = transaction.get('sensor_role', '')
        if not role:
            raise RefillError(
                'Snapmaker cross-head refill requires the built-in head sensor')
        detected = endpoint.sensor_detected(role)
        transaction['endpoint_sensor'] = detected
        if detected is not False:
            raise RefillError(
                'source Snapmaker head sensor has not cleared; head change is blocked')
        source_device = transaction['source_device']
        marker = getattr(
            self.manager, '_mark_u1_tail_sensor_cleared', None)
        if callable(marker):
            marker(
                endpoint, source_device,
                int(transaction['source_channel']),
                'cross-head refill verified source head sensor clear')
        source_channel = int(transaction['source_channel'])
        source_device.set_motion(source_channel, protocol.MOTION_IDLE)
        source_device.mark_unloaded(source_channel)
        source_device.refresh()
        raw_state = self.manager._route_states_from_status(
            source_device.status)[source_channel]
        if raw_state != protocol.ROUTE_EMPTY:
            raise RefillError(
                'source BMCU upstream route did not commit EMPTY after sensor clear')
        transaction['source_upstream_released'] = True
        transaction['source_route_retained'] = True
        transaction['source_tail_location'] = (
            'parked_hotend_downstream_of_sensor_waiting_for_future_follower')
        if hasattr(self.manager, '_save_runtime'):
            self.manager._save_runtime()
        return True

    def _u1_load_native_replacement(
            self, transaction, candidate, endpoint, resume=False):
        source_device = transaction['source_device']
        source_channel = int(transaction['source_channel'])
        source_tool = int(transaction['source_tool'])
        source_head = int(endpoint.get('head_index', -1))
        target_endpoint = candidate['endpoint']
        target_head = int(candidate['head'])
        temperature_profile = self.manager._u1_active_temperature_profile()
        self._drain_u1_source_tail(
            transaction, endpoint, temperature_profile=temperature_profile)
        if target_head == source_head:
            self.manager._load_native_follower_for_detached_tail(
                source_device, source_channel, endpoint,
                logical_tool=source_tool)
        else:
            task, config = self.manager._u1_task_config()
            if task is None or config is None:
                raise RefillError('Snapmaker U1 print_task_config is unavailable')
            reprint = config.get('reprint_info')
            if not isinstance(reprint, dict):
                raise RefillError('Snapmaker U1 reprint_info is unavailable')
            previous_mapping = copy.deepcopy(
                self.manager.print_tools.get(str(source_tool)))
            previous_map_active = bool(self.manager.print_map_active)
            runtime_snapshot = {
                'extruder_map_table': copy.deepcopy(config.get('extruder_map_table')),
                'extruders_used': copy.deepcopy(config.get('extruders_used')),
                'flow_calib_extruders': copy.deepcopy(config.get('flow_calib_extruders')),
                'reprint_map': copy.deepcopy(reprint.get('extruder_map_table')),
                'reprint_used': copy.deepcopy(reprint.get('extruders_used')),
                'reprint_flow': copy.deepcopy(reprint.get('flow_calib_extruders')),
                'map_backup': copy.deepcopy(self.manager._u1_map_backup),
                'used_backup': copy.deepcopy(self.manager._u1_used_backup),
            }
            self.manager.print_tools[str(source_tool)] = {
                'native': True, 'head': target_head}
            self.manager.print_map_active = True
            try:
                self.manager._apply_u1_replenish_map(
                    source_tool, source_head, target_head)
                self.manager._activate_native_u1_route({
                    'tool': source_tool, 'kind': 'native',
                    'head': target_head, 'endpoint': target_endpoint})
            except Exception:
                config['extruder_map_table'] = runtime_snapshot['extruder_map_table']
                config['extruders_used'] = runtime_snapshot['extruders_used']
                config['flow_calib_extruders'] = runtime_snapshot['flow_calib_extruders']
                reprint['extruder_map_table'] = runtime_snapshot['reprint_map']
                reprint['extruders_used'] = runtime_snapshot['reprint_used']
                reprint['flow_calib_extruders'] = runtime_snapshot['reprint_flow']
                self.manager._u1_map_backup = runtime_snapshot['map_backup']
                self.manager._u1_used_backup = runtime_snapshot['used_backup']
                self.manager.print_map_active = previous_map_active
                if previous_mapping is None:
                    self.manager.print_tools.pop(str(source_tool), None)
                else:
                    self.manager.print_tools[str(source_tool)] = previous_mapping
                self.manager._save_runtime()
                self.manager._save_print_session()
                self.manager._persist_u1_print_task(
                    'BMCU mixed refill rollback after native activation failure')
                raise
        logging.info(
            'BMCU mixed auto replenish: BMCU %s Channel %d Head %d -> stock Head %d',
            source_device.name, source_channel + 1, source_head + 1,
            target_head + 1)
        if resume:
            self.manager._resume_u1_stock_replenish(
                target_head, target_endpoint)
        return True

    def _safe_replacement_load(self, transaction, candidate, endpoint):

        manager = self.manager
        source_device = transaction['source_device']
        source_channel = transaction['source_channel']
        device = candidate['device']
        channel = candidate['channel']
        metadata = candidate['metadata']
        material = metadata.get('material', '')
        temperature_profile = (
            manager._u1_active_temperature_profile()
            if bool(getattr(endpoint,
                            'supports_print_temperature_profile', False))
            else None)

        transaction['phase'] = 'SAFE_LOAD_SELECT'
        transaction['_replacement_metadata'] = dict(metadata)
        transaction['_replacement_physically_captured'] = False
        capture_projection = getattr(endpoint, 'capture_active_filament_state', None)
        if callable(capture_projection) and '_projection_snapshot' not in transaction:
            transaction['_projection_snapshot'] = capture_projection()
        source_device.set_motion(source_channel, protocol.MOTION_IDLE)

        source_device.mark_unloaded(source_channel)
        source_device.refresh()
        if manager._route_states_from_status(
                source_device.status)[int(source_channel)] != protocol.ROUTE_EMPTY:
            raise RefillError(
                'exhausted source route did not commit upstream EMPTY before '
                'same-head follower load')
        manager._check_automatic_ready(
            device, channel,
            allow_detached_handoff=bool(
                candidate.get('same_source_channel')))
        endpoint.suspend_managed_sensors()
        endpoint.select()
        endpoint.verify_selected()
        if hasattr(endpoint, 'sync_active_filament'):
            endpoint.sync_active_filament(metadata)
        manager._endpoint_temperature_call(
            endpoint, 'prepare_load', material, temperature_profile)

        transaction['phase'] = 'SAFE_LOAD_ARRIVAL'
        maximum_mm = float(endpoint.get('max_route_mm', manager.max_route_mm) or manager.max_route_mm)
        contact_pct = manager._device_loading_handoff_pct(
            device, endpoint, refill=True)
        timeout_s = float(endpoint.get('refill_timeout', 75.0) or 75.0)
        timeout_s = self._effective_feed_timeout(
            device, maximum_mm, timeout_s)
        op_id, arrival_policy = manager._start_endpoint_arrival_operation(
            device, channel, endpoint, maximum_mm, contact_pct, timeout_s)
        if arrival_policy.get('sensor_authoritative'):
            arrival = manager._wait_sensor_arrival(
                device, channel, endpoint, op_id, arrival_policy,
                maximum_mm)
        else:
            arrival = manager._wait_feed_operation(
                device, op_id, timeout_s + 2.0, endpoint, 'entry_sensor')
        arrival = manager._resolve_endpoint_arrival_result(
            device, channel, endpoint, arrival, arrival_policy,
            timeout_s, allow_partial=False)
        transaction['arrival'] = arrival
        if not arrival.get('ok'):
            raise RefillError(
                'replacement did not reach tail/endpoint: %s' %
                arrival.get('reason'))
        if endpoint.driver != 'generic_single_extruder':
            manager._record_path_calibration(
                device, channel, endpoint, arrival,
                reason='paused refill reached endpoint contact')

        if endpoint.driver == 'generic_single_extruder':

            transaction['phase'] = 'TOOLHEAD_PREPARATION'
            device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
            endpoint.prepare_toolhead_for_use(material, reason='refill')
            path_measurement = manager._measure_path_calibration(
                device, channel, endpoint, arrival)
            configured_post = str(
                endpoint.get('post_gears_sensor', '') or '').strip()
            if (configured_post and
                    endpoint.sensor_detected('post_gears_sensor') is not True):
                raise RefillError(
                    'Toolhead preparation macro completed but configured '
                    'post-gears sensor is not active')
            transaction['_replacement_physically_captured'] = True
            self._activate_candidate(transaction, candidate, endpoint)
            if path_measurement is not None:
                try:
                    manager._commit_path_calibration(
                        device, channel, endpoint, path_measurement,
                        reason='Generic refill LOAD completed')
                except Exception:
                    logging.exception(
                        'BMCU could not persist Generic refill path measurement')
            return

        manager._endpoint_temperature_call(
            endpoint, 'ensure_bite_ready', material, temperature_profile)
        transaction['phase'] = 'TAIL_HANDOFF'
        if endpoint.driver == 'snapmaker_u1':
            endpoint.record_load_coil_evidence(
                coil_path_baseline=endpoint.capture_signal())
            try:
                handoff = (
                    manager._capture_snapmaker_detached_tail_with_follower(
                        source_device, source_channel, device, channel,
                        endpoint, material=material,
                        cancel_check=self._refill_motion_cancelled))
            except Exception as exc:
                raise RefillError(str(exc))
            if not isinstance(handoff, dict) or not handoff.get('captured'):
                raise RefillError(
                    'Snapmaker detached-tail follower capture returned no proof')
            transaction['handoff'] = handoff
        else:
            raise RefillError(
                'printer-native refill handoff is not implemented for driver %s' %
                endpoint.driver)

        transaction['phase'] = 'SAFE_LOAD_CAPTURE'
        before_capture = float(device.status['meters'][channel])
        signal_capture_start = endpoint.capture_signal()
        endpoint.extrude(manager.capture_mm, manager.capture_feed)
        entry_after_capture = (
            endpoint.entry_sensor_snapshot()
            if manager._u1_has_authoritative_entry_sensor(endpoint) else
            endpoint.sensor_detected('entry_sensor'))
        post_gears = endpoint.sensor_detected('post_gears_sensor')
        signal_capture_end = endpoint.capture_signal()
        signal_delta = None
        if signal_capture_start is not None and signal_capture_end is not None:
            signal_delta = abs(
                float(signal_capture_end) - float(signal_capture_start))
        if endpoint.driver == 'snapmaker_u1':
            endpoint.record_load_coil_evidence(
                coil_before_capture=signal_capture_start,
                coil_after_capture=signal_capture_end,
                coil_capture_delta=signal_delta)
        if manager._u1_has_authoritative_entry_sensor(endpoint):
            capture_moved = 0.0
            if not arrival.get('sensor_triggered'):
                raise RefillError(
                    'replacement CAPTURE lacks prior Snapmaker arrival evidence')
        else:
            device.refresh()
            capture_moved = abs(
                float(device.status['meters'][channel]) - before_capture) * 1000.0
            if (post_gears is not True and
                    capture_moved <
                    manager.capture_mm * manager.capture_encoder_ratio and
                    not endpoint.capture_signal_ok(signal_delta, material)):
                raise RefillError('replacement CAPTURE failed')
        transaction['capture_encoder_mm'] = capture_moved
        transaction['capture_entry_sensor'] = entry_after_capture
        transaction['capture_native_signal_delta'] = signal_delta

        transaction['_replacement_physically_captured'] = True
        if endpoint.driver == 'snapmaker_u1':

            transaction['phase'] = 'COMMIT_FOLLOWER_ROUTE'
            manager._arm_u1_follower_commit(
                endpoint, source_device, source_channel, device, channel,
                logical_tool=int(transaction['source_tool']))
            device.mark_loaded(channel)
            device.refresh()
            raw_target = manager._route_states_from_status(
                device.status)[channel]
            if raw_target != protocol.ROUTE_LOADED:
                raise RefillError(
                    'replacement %s Channel %d did not commit LOADED after '
                    'detached-tail CAPTURE' %
                    (device.name, channel + 1))
            finalized = manager._finalize_snapmaker_tail_handoff_after_follower(
                source_device, source_channel, endpoint,
                follower_device=device, follower_channel=channel,
                logical_tool=int(transaction['source_tool']),
                reason=('auto-refill follower physically captured and '
                        'committed LOADED before final prime'))
            if not finalized:
                raise RefillError(
                    'detached-tail ownership disappeared before follower commit')
            transaction['detached_tail_finalized'] = True
        else:
            generic_matcher = getattr(
                manager, '_generic_tail_detached_matches', None)
            if (callable(generic_matcher) and generic_matcher(
                    endpoint, source_device, source_channel)):
                transaction['phase'] = 'COMMIT_FOLLOWER_ROUTE'
                manager._arm_generic_follower_commit(
                    endpoint, source_device, source_channel,
                    device, channel,
                    logical_tool=int(transaction['source_tool']))
                device.mark_loaded(channel)
                device.refresh()
                if manager._route_states_from_status(
                        device.status)[channel] != protocol.ROUTE_LOADED:
                    raise RefillError(
                        'generic replacement did not commit LOADED after CAPTURE')
                if not manager._finalize_generic_tail_handoff_after_follower(
                        source_device, source_channel, endpoint,
                        follower_device=device, follower_channel=channel,
                        reason=('auto-refill follower captured and committed '
                                'before final prime')):
                    raise RefillError(
                        'generic detached-tail ownership disappeared before commit')
                transaction['detached_tail_finalized'] = True

        manager._endpoint_temperature_call(
            endpoint, 'load_ready', material, temperature_profile)
        source_meta = transaction['source_metadata']
        exact = self._metadata_matches(source_meta, metadata, 'exact')
        manager._endpoint_temperature_call(
            endpoint, 'refill_prime', material, temperature_profile,
            exact_match=exact)
        self._activate_candidate(transaction, candidate, endpoint)

    def _independent_endpoint_load(self, transaction, candidate, source_endpoint):

        manager = self.manager
        source_device = transaction['source_device']
        source_channel = int(transaction['source_channel'])
        device = candidate['device']
        channel = int(candidate['channel'])
        endpoint = candidate['endpoint']
        metadata = candidate['metadata']
        material = metadata.get('material', '')
        temperature_profile = (
            manager._u1_active_temperature_profile()
            if bool(getattr(endpoint,
                            'supports_print_temperature_profile', False))
            else None)
        plan = candidate.get('route_plan') or manager.refill_route_plan(
            source_endpoint, endpoint)
        if not plan.get('supported') or plan.get('mode') == 'same_endpoint':
            raise RefillError(plan.get('reason', 'invalid cross-endpoint refill plan'))

        transaction['phase'] = 'CROSS_ENDPOINT_SELECT'
        transaction['target_endpoint'] = endpoint.name
        transaction['route_mode'] = plan.get('mode', '')
        transaction['_replacement_metadata'] = dict(metadata)
        transaction['_replacement_physically_captured'] = False
        transaction['_projection_endpoint'] = endpoint
        capture_projection = getattr(endpoint, 'capture_active_filament_state', None)
        if callable(capture_projection):
            transaction['_projection_snapshot'] = capture_projection()

        source_device.set_motion(source_channel, protocol.MOTION_IDLE)
        manager._validate_endpoint_for_operation(endpoint)
        manager._check_automatic_ready(device, channel)
        manager._require_exclusive_endpoint_route(device, endpoint, channel)
        endpoint.suspend_managed_sensors()
        endpoint.select()
        endpoint.verify_selected()
        if hasattr(endpoint, 'sync_active_filament'):
            endpoint.sync_active_filament(metadata)
        manager._endpoint_temperature_call(
            endpoint, 'prepare_load', material, temperature_profile)

        transaction['phase'] = 'CROSS_ENDPOINT_ARRIVAL'
        maximum_mm = float(endpoint.get(
            'max_route_mm', manager.max_route_mm) or manager.max_route_mm)
        contact_pct = manager._device_loading_handoff_pct(
            device, endpoint)
        timeout_s = float(endpoint.get(
            'contact_timeout', manager.contact_timeout) or manager.contact_timeout)
        timeout_s = self._effective_feed_timeout(
            device, maximum_mm, timeout_s)
        if endpoint.sensor_detected('entry_sensor') is True:
            raise RefillError(
                'target endpoint %s already contains filament' % endpoint.name)
        op_id, arrival_policy = manager._start_endpoint_arrival_operation(
            device, channel, endpoint, maximum_mm, contact_pct, timeout_s)
        if arrival_policy.get('sensor_authoritative'):
            arrival = manager._wait_sensor_arrival(
                device, channel, endpoint, op_id, arrival_policy,
                maximum_mm)
        else:
            arrival = manager._wait_feed_operation(
                device, op_id, timeout_s + 2.0, endpoint, 'entry_sensor')
        arrival = manager._resolve_endpoint_arrival_result(
            device, channel, endpoint, arrival, arrival_policy,
            timeout_s, allow_partial=False)
        transaction['arrival'] = arrival
        if not arrival.get('ok'):
            raise RefillError(
                'replacement did not reach %s: %s after %.1f mm' %
                (endpoint.name, arrival.get('reason'),
                 float(arrival.get('measured_mm', 0.0) or 0.0)))
        manager._record_path_calibration(
            device, channel, endpoint, arrival,
            reason='cross-endpoint refill reached endpoint contact')

        if endpoint.driver == 'generic_single_extruder':
            transaction['phase'] = 'TOOLHEAD_PREPARATION'
            device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
            endpoint.prepare_toolhead_for_use(material, reason='refill')
            configured_post = str(
                endpoint.get('post_gears_sensor', '') or '').strip()
            if (configured_post and
                    endpoint.sensor_detected('post_gears_sensor') is not True):
                raise RefillError(
                    'Toolhead preparation macro completed but configured '
                    'post-gears sensor is not active')
            transaction['_replacement_physically_captured'] = True
            self._activate_candidate(
                transaction, candidate, endpoint, cross_endpoint=True)
            return plan

        transaction['phase'] = 'CROSS_ENDPOINT_BITE'
        u1_pressure_evidence = None
        if manager._u1_has_authoritative_entry_sensor(endpoint):
            device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
        manager._endpoint_temperature_call(
            endpoint, 'ensure_bite_ready', material, temperature_profile)
        if manager._u1_has_authoritative_entry_sensor(endpoint):
            u1_pressure_evidence = manager._wait_u1_firmware_pressure_hold(
                device, channel, endpoint, timeout_s,
                arrival_evidence=arrival)
        else:
            device.set_motion(channel, protocol.MOTION_BEFORE_ON_USE)
        bite_ok = False
        bite_evidence = {}
        if manager._u1_has_authoritative_entry_sensor(endpoint):
            signal_start = endpoint.capture_signal()
            endpoint.record_load_coil_evidence(
                coil_path_baseline=signal_start)
            endpoint.extrude(manager.bite_mm, manager.bite_feed)
            entry_after_bite = endpoint.entry_sensor_snapshot()
            signal_end = endpoint.capture_signal()
            signal_delta = None
            if signal_start is not None and signal_end is not None:
                signal_delta = abs(float(signal_end) - float(signal_start))
            endpoint.record_load_coil_evidence(
                coil_bite_delta=signal_delta,
                coil_after_bite=signal_end)
            arrival_confirmed = bool(
                arrival.get('sensor_triggered'))
            bite_evidence = {
                'attempt': 1,
                'confirmation': 'u1_arrival_and_firmware_pressure',
                'entry_sensor_phase_diagnostic': copy.deepcopy(entry_after_bite),
                'pressure_hold': copy.deepcopy(u1_pressure_evidence),
                'arrival_motion': bool(arrival.get('sensor_triggered')),
                'arrival_contact': bool(arrival.get('controller_contact')),
                'native_signal_delta': signal_delta,
            }
            bite_ok = bool(
                arrival_confirmed and
                isinstance(u1_pressure_evidence, dict))
        else:
            for attempt in range(manager.bite_retries):
                before_m = float(device.status['meters'][channel])
                before_buffer = float(device.status['buffer_pct'][channel])
                signal_start = endpoint.capture_signal()
                endpoint.extrude(manager.bite_mm, manager.bite_feed)
                device.refresh()
                moved_mm = abs(
                    float(device.status['meters'][channel]) - before_m) * 1000.0
                buffer_drop = before_buffer - float(
                    device.status['buffer_pct'][channel])
                post_gears = endpoint.sensor_detected('post_gears_sensor')
                signal_delta = endpoint.capture_signal_delta(signal_start, material)
                bite_evidence = {
                    'attempt': attempt + 1,
                    'encoder_mm': moved_mm,
                    'buffer_drop': buffer_drop,
                    'post_gears': post_gears,
                    'native_signal_delta': signal_delta,
                }
                if (post_gears is True or
                        moved_mm >=
                        manager.bite_mm * manager.bite_encoder_ratio or
                        buffer_drop >= manager.bite_buffer_delta or
                        endpoint.capture_signal_ok(signal_delta, material)):
                    bite_ok = True
                    break
                if manager.retry_retract_mm > 0.0:
                    endpoint.retract(
                        manager.retry_retract_mm,
                        int(endpoint.get(
                            'retry_retract_feed', 180.0) or 180.0))
        transaction['bite'] = bite_evidence
        transaction['u1_pressure_hold'] = copy.deepcopy(u1_pressure_evidence)
        if not bite_ok:
            raise RefillError(
                'replacement did not engage target endpoint %s' % endpoint.name)

        transaction['phase'] = 'CROSS_ENDPOINT_CAPTURE'
        before_capture = float(device.status['meters'][channel])
        signal_start = endpoint.capture_signal()
        endpoint.extrude(manager.capture_mm, manager.capture_feed)
        entry_after_capture = (
            endpoint.entry_sensor_snapshot()
            if manager._u1_has_authoritative_entry_sensor(endpoint) else
            endpoint.sensor_detected('entry_sensor'))
        post_gears = endpoint.sensor_detected('post_gears_sensor')
        signal_end = endpoint.capture_signal()
        signal_delta = None
        if signal_start is not None and signal_end is not None:
            signal_delta = abs(float(signal_end) - float(signal_start))
        if endpoint.driver == 'snapmaker_u1':
            endpoint.record_load_coil_evidence(
                coil_before_capture=signal_start,
                coil_after_capture=signal_end,
                coil_capture_delta=signal_delta)
        if manager._u1_has_authoritative_entry_sensor(endpoint):
            capture_moved = 0.0
            if (not arrival.get('sensor_triggered') or
                    not isinstance(u1_pressure_evidence, dict)):
                raise RefillError(
                    'replacement CAPTURE lacks prior Snapmaker arrival and '
                    'firmware-pressure evidence')
        else:
            device.refresh()
            capture_moved = abs(
                float(device.status['meters'][channel]) - before_capture) * 1000.0
            if (post_gears is not True and
                    capture_moved <
                    manager.capture_mm * manager.capture_encoder_ratio and
                    not endpoint.capture_signal_ok(signal_delta, material)):
                raise RefillError(
                    'replacement CAPTURE failed at %s' % endpoint.name)
        transaction['capture_encoder_mm'] = capture_moved
        transaction['capture_entry_sensor'] = entry_after_capture
        transaction['capture_native_signal_delta'] = signal_delta

        transaction['_replacement_physically_captured'] = True
        transaction['phase'] = 'CROSS_ENDPOINT_READY'
        manager._endpoint_temperature_call(
            endpoint, 'load_ready', material, temperature_profile)
        exact = self._metadata_matches(
            transaction['source_metadata'], metadata, 'exact')

        if plan.get('mode') != 'snapmaker_u1':
            manager._endpoint_temperature_call(
                endpoint, 'refill_prime', material, temperature_profile,
                exact_match=exact)
        self._activate_candidate(
            transaction, candidate, endpoint, cross_endpoint=True)
        if plan.get('mode') == 'snapmaker_u1':
            manager.capture_u1_cross_refill(
                int(transaction['source_tool']), device, channel, endpoint)
        return plan

    def _run(self, eventtime, source_device, source_channel, source_tool, endpoint):
        key = '%s:%d' % (source_device.name, source_channel)
        self._pending.discard(key)
        if key in self._toolchange_preemptions:
            self.last = {
                'source_device_name': source_device.name,
                'source_channel': source_channel,
                'source_tool': source_tool,
                'endpoint': endpoint.name,
                'phase': 'PREEMPTED_FOR_TOOLCHANGE',
                'result': 'preempted',
            }
            return
        if key in self.transactions:
            return

        debounce_value = endpoint.get('refill_runout_debounce', 0.4)
        debounce = 0.4 if debounce_value is None else float(debounce_value)
        deadline = self.reactor.monotonic() + max(0.0, debounce)
        next_poll = 0.0
        while self.reactor.monotonic() < deadline:
            if key in self._toolchange_preemptions:
                self.last = {
                    'source_device_name': source_device.name,
                    'source_channel': source_channel,
                    'source_tool': source_tool,
                    'endpoint': endpoint.name,
                    'phase': 'PREEMPTED_FOR_TOOLCHANGE',
                    'result': 'preempted',
                }
                return
            if source_device.status.get('present', [0, 0, 0, 0])[source_channel]:
                self.last = {
                    'source_device_name': source_device.name,
                    'source_channel': source_channel,
                    'source_tool': source_tool,
                    'endpoint': endpoint.name,
                    'phase': 'RUNOUT_DEBOUNCE',
                    'result': 'ignored_transient',
                }
                return
            now = self.reactor.monotonic()
            if source_device.connected and now >= next_poll:
                try:
                    source_device.send(protocol.MSG_GET_STATUS)
                except Exception:
                    logging.exception('BMCU could not refresh present state during refill debounce')
                next_poll = now + 0.1
            self.reactor.pause(min(deadline, now + 0.025))
        if source_device.status.get('present', [0, 0, 0, 0])[source_channel]:
            return

        mark_detached = getattr(
            self.manager, '_mark_u1_tail_detached', None)
        if endpoint.driver == 'snapmaker_u1' and callable(mark_detached):
            mark_detached(
                endpoint, source_device, source_channel,
                'runout debounce confirmed BMCU input empty while route is loaded')
        elif endpoint.driver != 'snapmaker_u1':
            generic_marker = getattr(
                self.manager, '_mark_generic_tail_detached', None)
            if callable(generic_marker):
                generic_marker(
                    endpoint, source_device, source_channel,
                    'runout debounce confirmed input empty; reverse unload disabled')

        sensor_role = self._endpoint_sensor_role(endpoint)
        if (endpoint.driver == 'generic_single_extruder' and
                not sensor_role):
            try:
                source_device.set_motion(source_channel, protocol.MOTION_IDLE)
                self._set_manual_pending(
                    source_device, source_channel, source_tool, endpoint)
                paused = self.manager._pause_for_refill(endpoint)
                if (not paused and
                        self.manager._print_state() not in ('paused', 'pause')):
                    raise RefillError(
                        'printer did not remain paused for manual refill')
            except Exception as exc:
                self.manual_pending = None
                sync_cache = getattr(
                    self.manager, '_sync_status_cache_runtime', None)
                if callable(sync_cache):
                    sync_cache()
                self.manager._record_error(
                    'MANUAL_REFILL_PAUSE_FAILED', source_device.name,
                    source_channel, endpoint.name, 'PAUSE_FOR_MANUAL_REFILL',
                    str(exc))
                self.manager._safe_pause()
                logging.exception('BMCU could not enter manual Generic refill pause')
                return
            self.last = {
                'source_device_name': source_device.name,
                'source_channel': source_channel,
                'source_tool': source_tool,
                'endpoint': endpoint.name,
                'phase': 'PAUSED_MANUAL_REFILL',
                'result': 'paused',
                'manual_refill': True,
            }
            logging.warning(
                'BMCU Generic runout on %s Channel %d: no endpoint sensor; '
                'insert replacement filament into the same Channel and press Resume',
                source_device.name, source_channel + 1)
            return

        if endpoint.driver == 'snapmaker_u1':
            task, u1_config = self.manager._u1_task_config()
            refill_enabled = bool(
                isinstance(u1_config, dict) and
                u1_config.get('auto_replenish_filament', False))
            source_stock_metadata = self._u1_projected_metadata(
                self.manager._channel_metadata(source_device, source_channel))
            candidate = (self.choose_u1_candidate(
                'bmcu', int(endpoint.get('head_index', -1)),
                source_stock_metadata, source_tool=source_tool,
                source_device=source_device, source_channel=source_channel,
                source_endpoint=endpoint) if refill_enabled else None)
        else:
            refill_enabled = bool(endpoint.get('auto_refill_enabled', False))
            candidate = (self.choose_candidate(
                source_tool, source_device, source_channel, endpoint)
                if refill_enabled else None)
        transaction = {
            'source_device': source_device,
            'source_device_name': source_device.name,
            'source_channel': source_channel,
            'source_tool': source_tool,
            'source_metadata': dict(self.manager._channel_metadata(source_device, source_channel)),
            'endpoint': endpoint.name,
            'phase': 'TAIL_DRAIN',
            'consumed_mm': 0.0,
            'last_e': self._extruder_position(endpoint),
            'sensor_role': self._endpoint_sensor_role(endpoint),
            'candidate_tool': candidate['tool'] if candidate else -1,
            'candidate_device': (candidate['device'].name
                                 if candidate and candidate.get('device') is not None
                                 else ('STOCK' if candidate else '')),
            'candidate_channel': candidate['channel'] if candidate else -1,
            'candidate_endpoint': candidate['endpoint'].name if candidate else '',
            'cross_endpoint': bool(candidate and candidate.get('cross_endpoint')),
            'mode': str(endpoint.get('refill_mode', 'pause')).lower(),
            'match_mode': ('printer' if endpoint.driver == 'snapmaker_u1' else
                           str(endpoint.get('refill_match', 'exact')).lower()),
            'started': self.reactor.monotonic(),
        }
        self.transactions[key] = transaction
        locked = False
        paused_by_bmcu = False
        try:
            replacement_endpoint = (
                candidate['endpoint']
                if candidate and candidate.get('cross_endpoint') else None)
            if replacement_endpoint is None:
                self.manager._lock_refill(
                    source_device,
                    candidate.get('device') if candidate else None,
                    endpoint, 'AUTO_REFILL T%d' % source_tool)
            else:
                self.manager._lock_refill(
                    source_device, candidate.get('device'), endpoint,
                    'AUTO_REFILL T%d' % source_tool, replacement_endpoint)
            locked = True
            endpoint.ensure_runtime_sensor_takeover()
            continuous_attempted = False
            while True:
                if (transaction.get('preempt_for_toolchange') or
                        key in self._toolchange_preemptions):
                    transaction['phase'] = 'PREEMPTED_FOR_TOOLCHANGE'
                    transaction['result'] = 'preempted'
                    self.last = self._public_transaction(transaction)
                    return
                current_e = self._extruder_position(endpoint)
                if (not transaction.get('sensor_role') and current_e is None):

                    transaction['needs_extrusion_position'] = True
                    break
                self._advance_consumption(transaction, current_e)
                if not source_device.connected:
                    raise RefillError('source BMCU disconnected during tail drain')
                if not self._is_printing():
                    raise RefillError('print left printing state during tail drain')
                if (candidate and candidate.get('kind') != 'native' and
                        self._can_continuous(transaction, endpoint, candidate)):
                    tail_to_output = self._continuous_trigger_mm(
                        transaction, endpoint)
                    transaction['continuous_trigger_mm'] = tail_to_output
                    if (not continuous_attempted and
                            tail_to_output >= 0.0 and
                            transaction['consumed_mm'] >= tail_to_output):
                        continuous_attempted = True
                        if self._continuous_handoff(transaction, candidate, endpoint):
                            transaction['phase'] = 'COMPLETE_CONTINUOUS'
                            transaction['result'] = 'ok'
                            self.last = self._public_transaction(transaction)
                            return
                if self._tail_deadline_reached(transaction, endpoint,
                                               transaction.get('sensor_role', '')):
                    break
                self.reactor.pause(self.reactor.monotonic() + 0.05)

            transaction['phase'] = 'PAUSE_FOR_REFILL'
            paused_by_bmcu = self.manager._pause_for_refill(endpoint)
            source_device.set_motion(source_channel, protocol.MOTION_IDLE)
            same_channel = self._same_channel_reinsert_candidate(
                source_tool, source_device, source_channel, endpoint)
            if same_channel is not None:
                candidate = same_channel
                transaction['candidate_tool'] = candidate['tool']
                transaction['candidate_device'] = candidate['device'].name
                transaction['candidate_channel'] = candidate['channel']
                transaction['candidate_endpoint'] = candidate['endpoint'].name
                transaction['cross_endpoint'] = False
                transaction['selection'] = 'same_channel_reinsert'
            if candidate is not None:
                if candidate.get('kind') == 'native':
                    self._u1_load_native_replacement(
                        transaction, candidate, endpoint,
                        resume=paused_by_bmcu)
                    transaction['phase'] = 'COMPLETE_NATIVE'
                    transaction['result'] = 'ok'
                elif candidate.get('cross_endpoint'):
                    if (endpoint.driver == 'snapmaker_u1' and
                            candidate['endpoint'].driver == 'snapmaker_u1'):
                        self._drain_u1_source_tail(
                            transaction, endpoint,
                            temperature_profile=(
                                self.manager._u1_active_temperature_profile()
                                if bool(getattr(
                                    endpoint,
                                    'supports_print_temperature_profile',
                                    False)) else None))
                        self.manager.begin_u1_cross_refill(
                            source_tool, source_device, source_channel, endpoint,
                            candidate['device'], candidate['channel'],
                            candidate['endpoint'])
                    plan = self._independent_endpoint_load(
                        transaction, candidate, endpoint)
                    transaction['phase'] = 'CROSS_ENDPOINT_RESUME'
                    if paused_by_bmcu:
                        self.manager.resume_cross_endpoint_refill(
                            source_tool, endpoint, candidate['endpoint'], plan)
                    transaction['phase'] = 'COMPLETE_CROSS_ENDPOINT'
                else:
                    self._safe_replacement_load(transaction, candidate, endpoint)
                    transaction['phase'] = 'COMPLETE_PAUSED'
                    if paused_by_bmcu:
                        self.manager._resume_after_refill(endpoint)
                transaction['result'] = 'ok'
            elif endpoint.driver in ('generic_single_extruder', 'snapmaker_u1'):
                self._set_manual_pending(
                    source_device, source_channel, source_tool, endpoint)
                transaction['phase'] = 'PAUSED_MANUAL_REFILL'
                transaction['result'] = 'paused'
                transaction['manual_refill'] = True
                transaction['automatic_refill_enabled'] = bool(refill_enabled)
                self.last = self._public_transaction(transaction)
                self.manager._record_error(
                    'FILAMENT_RUNOUT', source_device.name, source_channel,
                    endpoint.name, transaction['phase'],
                    'no compatible automatic replacement is ready; insert '
                    'filament into the same exhausted BMCU Channel and press Resume',
                    self._public_transaction(transaction))
                return
            elif not refill_enabled:
                transaction['phase'] = 'PAUSED_RUNOUT'
                transaction['result'] = 'paused'
                self.last = self._public_transaction(transaction)
                self.manager._record_error(
                    'FILAMENT_RUNOUT', source_device.name, source_channel,
                    endpoint.name, transaction['phase'],
                    'filament exhausted; printer paused at the configured tail point',
                    self._public_transaction(transaction))
                return
            else:
                transaction['phase'] = 'PAUSED_NO_BACKUP'
                transaction['result'] = 'no_backup'
                raise RefillError('no compatible refill Channel is ready')
            self.last = self._public_transaction(transaction)
        except Exception as exc:
            if endpoint.driver == 'snapmaker_u1':
                task, _config = self.manager._u1_task_config()
                if task is not None:
                    task.perform_auto_replenish = False

            stopped = set()
            for motion_device in (
                    source_device, candidate.get('device') if candidate else None):
                if motion_device is None or motion_device.name in stopped:
                    continue
                stopped.add(motion_device.name)
                try:
                    motion_device.stop_all()
                except Exception:
                    logging.exception('BMCU could not stop %s after refill failure',
                                      motion_device.name)

            projection_restored = False
            projection_endpoint = transaction.get('_projection_endpoint', endpoint)
            if not transaction.get('_replacement_physically_captured', False):
                snapshot = transaction.get('_projection_snapshot')
                restore_projection = getattr(
                    projection_endpoint, 'restore_active_filament_state', None)
                if snapshot is not None and callable(restore_projection):
                    try:
                        restore_projection(snapshot)
                        projection_restored = True
                    except Exception:
                        logging.exception(
                            'BMCU could not restore endpoint projection after refill failure')
            else:
                replacement_metadata = transaction.get('_replacement_metadata')
                project_replacement = getattr(
                    projection_endpoint, 'sync_active_filament', None)
                if replacement_metadata is not None and callable(project_replacement):
                    try:
                        project_replacement(replacement_metadata)
                        projection_restored = True
                    except Exception:
                        logging.exception(
                            'BMCU could not retain replacement projection after capture failure')
            if not projection_restored:
                try:
                    clear_projection = getattr(
                        projection_endpoint, 'clear_active_filament', None)
                    if callable(clear_projection):
                        clear_projection()
                except Exception:
                    logging.exception(
                        'BMCU could not clear endpoint projection after refill failure')
            for restore_endpoint in (endpoint, transaction.get('_projection_endpoint')):
                if restore_endpoint is None:
                    continue
                try:
                    restore_endpoint.activate_runtime_sensor_takeover()
                except Exception:
                    logging.exception(
                        'BMCU could not restore endpoint sensor ownership after refill failure')
            transaction['result'] = 'failed'
            transaction['error'] = str(exc)
            if (candidate is not None and candidate.get('cross_endpoint') and
                    endpoint.driver == 'snapmaker_u1' and
                    candidate['endpoint'].driver == 'snapmaker_u1'):
                try:
                    self.manager.fail_u1_cross_refill(source_tool, exc)
                except Exception:
                    logging.exception(
                        'BMCU could not persist failed U1 cross-head refill phase')
            self.last = self._public_transaction(transaction)
            self.manager._record_error(
                'AUTO_REFILL_FAILED', source_device.name, source_channel,
                endpoint.name, transaction.get('phase', 'AUTO_REFILL'), str(exc),
                self._public_transaction(transaction))
            self.manager._safe_pause()
            logging.exception('BMCU auto refill failed')
        finally:
            if locked:
                replacement_endpoint = (
                    candidate['endpoint']
                    if candidate and candidate.get('cross_endpoint') else None)
                if replacement_endpoint is None:
                    self.manager._unlock_refill(
                        source_device,
                        candidate['device'] if candidate else None,
                        endpoint)
                else:
                    self.manager._unlock_refill(
                        source_device, candidate['device'], endpoint,
                        replacement_endpoint)
            self.transactions.pop(key, None)

    @staticmethod
    def _public_transaction(transaction):
        hidden = {
            'source_device', 'source_metadata', 'last_e',
            '_projection_endpoint',
            '_projection_snapshot', '_replacement_metadata',
            '_replacement_physically_captured',
        }
        return {key: value for key, value in transaction.items() if key not in hidden}
