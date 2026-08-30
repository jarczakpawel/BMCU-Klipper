# SPDX-License-Identifier: GPL-3.0-or-later

from copy import deepcopy

COMMON_ENDPOINT = {

    'driver': 'generic_single_extruder',
    'extruder': 'extruder', 'head_index': -1,
    'sensor_policy': 'managed', 'entry_sensor': '', 'post_gears_sensor': '',
    'motion_sensor': '',
    'tail_tracking_mode': 'auto',
    'select_macro': '', 'verify_selected_macro': '',
    'expected_active_extruder': '', 'verify_active_extruder': False,
    'require_select_macro': False, 'deselect_macro': '',
    'prestage_while_unselected': False, 'prestage_distance_mm': 0.0,
    'max_route_mm': 1500.0, 'final_search_mm': 250.0,
    'contact_buffer_pct': 82.0, 'contact_timeout': 45.0,
    'prestage_buffer_limit_pct': 82.0, 'prestage_timeout': 45.0,
    'auto_refill_enabled': False, 'tail_runout_enabled': True,
    'refill_mode': 'pause', 'refill_match': 'exact',
    'tail_to_output_mm': 0.0,
    'sensor_tail_remaining_mm': 0.0, 'tail_reserve_mm': 20.0,
    'refill_contact_buffer_pct': 82.0, 'refill_timeout': 75.0,
    'refill_runout_debounce': 0.4,
    'refill_pause_macro': '', 'refill_resume_macro': '',
    'refill_resume_timeout': 12.0,
}

NATIVE_ENDPOINT_MECHANICS = {
    'heater': 'extruder',
    'toolhead_load_mode': 'distance', 'toolhead_load_macro': '',
    'min_bite_temp': 180.0, 'min_unload_temp': 180.0,
    'toolhead_load_mm': 36.0, 'cold_preload_allowed': True,
    'cutter_mode': 'none',
    'prepare_prestage_macro': '', 'prepare_load_macro': '',
    'before_bite_macro': '', 'load_ready_macro': '',
    'purge_macro': '', 'wipe_macro': '', 'prime_macro': '',
    'prepare_unload_macro': '', 'tip_form_macro': '', 'cut_macro': '',
    'post_cut_macro': '', 'release_macro': '', 'verify_release_macro': '',
    'refill_handoff_max_mm': 120.0, 'refill_handoff_chunk_mm': 5.0,
    'refill_handoff_feed': 240.0, 'refill_prime_macro': '',
    'unload_assist_limit_mm': 0.0,
}

def _common_endpoint(name, **overrides):
    result = deepcopy(COMMON_ENDPOINT)
    result.update(overrides)
    result.setdefault('shared_path_group', name)
    return result

def _native_endpoint(name, **overrides):
    result = _common_endpoint(name)
    result.update(deepcopy(NATIVE_ENDPOINT_MECHANICS))
    result.update(overrides)
    return result

def _generic_endpoint(name, **overrides):
    result = _common_endpoint(name, **overrides)
    result['driver'] = 'generic_single_extruder'
    result['toolhead_prepare_macro'] = ''
    result['before_pullback_macro'] = ''
    return result

def generic_single_extruder(name='extruder'):
    return {name: _generic_endpoint(name)}

def _extruder(index):
    return 'extruder' if index == 0 else 'extruder%d' % index

def snapmaker_u1():
    feeder_map = {0: ('left', 1), 1: ('left', 0),
                  2: ('right', 0), 3: ('right', 1)}
    result = {}
    for head in range(4):
        extruder = _extruder(head)
        module, channel = feeder_map[head]
        name = 'u1_head%d' % head
        result[name] = _native_endpoint(
            name, driver='snapmaker_u1', head_index=head,
            extruder=extruder, heater=extruder,
            entry_sensor='filament_motion_sensor:e%d_filament' % head,
            motion_sensor='filament_motion_sensor:e%d_filament' % head,
            expected_active_extruder=extruder,
            verify_active_extruder=True, native_filament_manager=True,
            u1_native_hotend_sequences=True, u1_unload_mode='fast',
            u1_load_temp=250.0, u1_unload_temp=250.0,
            min_unload_temp=250.0,
            u1_auto_home_xy=True, u1_require_coil_confirmation=True,
            u1_require_head_confirmation=True,
            u1_load_to_nozzle_mm=70.0,
            unload_assist_limit_mm=0.0,
            u1_prestage_park_timeout=20.0,
            u1_prime_length_mm=20.0, u1_refill_prime_length_mm=20.0,
            snap_tail_handoff_enabled=True,
            snap_tail_chunk_mm=20.0,
            snap_tail_cleanup_every_mm=20.0,
            snap_tail_max_mm=1500.0,
            snap_tail_feed=360.0,
            snap_tail_soft_feed=120.0,
            snap_tail_settle_s=0.05,
            u1_native_feeder_takeover=False,
            u1_require_feeder_confirmation=True, u1_rfid_quiesce_timeout=3.0,
            u1_feeder_module=module, u1_feeder_channel=channel,
            max_route_mm=5000.0, contact_timeout=100.0,
            prestage_buffer_limit_pct=63.0,
            auto_refill_enabled=True, tail_reserve_mm=20.0,
            refill_timeout=100.0, u1_coil_threshold_soft=800.0,
            u1_coil_threshold_hard=1500.0, u1_sensor_takeover=False,
            u1_allow_nonstandard_topology=False,
)
    return result

_BUILDERS = {
    'generic_single_extruder': lambda count: generic_single_extruder(),
    'snapmaker_u1': lambda count: snapmaker_u1(),
}
_ALIASES = {
    'generic': 'generic_single_extruder', 'single': 'generic_single_extruder',
    'u1': 'snapmaker_u1',
}

def canonical_name(name):
    key = str(name or '').strip().lower()
    return _ALIASES.get(key, key)

def names():
    return tuple(sorted(_BUILDERS))

def build(name, count=4):
    key = canonical_name(name)
    builder = _BUILDERS.get(key)
    if builder is None:
        raise ValueError('unknown preset %s; available: %s' %
                         (name, ', '.join(names())))
    return builder(count)
