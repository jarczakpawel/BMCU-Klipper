#include "bmcu_protocol.h"
#include "bmcu_uart.h"
#include "bmcu_cobs.h"
#include "bmcu_crc32.h"
#include "bmcu_uid.h"
#include "bmcu_config.h"
#include "ams.h"
#include "Motion_control.h"
#include "MC_PULL_calibration.h"
#include "Flash_saves.h"
#include "hal/time_hw.h"
#include "system_led.h"
#include "lighting.h"
#include <math.h>
#include <string.h>

extern bool RGB_preview(uint8_t target, uint8_t red, uint8_t green, uint8_t blue);

struct __attribute__((packed)) PacketHeader
{
    uint8_t ver;
    uint8_t type;
    uint16_t len;
    uint32_t seq;
    uint32_t cmd_id;
};

struct __attribute__((packed)) StatusPayload
{
    uint8_t now_filament_num;
    uint8_t route_state[4];
    uint8_t motion[4];
    uint8_t present[4];
    uint8_t buffer_pct[4];
    uint8_t calibration_valid_mask;
    uint8_t calibration_capture_mask[4];
    uint8_t encoder_ok_mask;
    uint8_t connected_mask;
    uint16_t error_flags;
    float meters[4];
    float buffer_raw[4];
    int16_t motor_pwm[4];
    uint32_t session_id;
    uint32_t event_counter;
    uint32_t active_op_id;
    uint8_t active_op_type;
    uint8_t active_op_ch;
    uint8_t active_op_state;
    uint8_t active_op_reason;
    uint8_t auto_cal_stage;
    uint8_t auto_cal_progress;
    uint8_t auto_cal_mask;
    uint8_t auto_cal_done_mask;
    uint8_t auto_cal_state;
    uint8_t auto_cal_reason;
};

struct __attribute__((packed)) CalibrationPayload
{
    uint8_t ch;
    uint8_t capture_mask;
    uint8_t valid;
    int8_t polarity;
    float current_raw;
    float offset;
    float min_value;
    float neutral_value;
    float max_value;
};

struct __attribute__((packed)) OpResultPayload
{
    uint32_t op_id;
    uint8_t type;
    uint8_t ch;
    uint8_t state;
    uint8_t reason;
    float target_mm;
    float measured_mm;
    uint32_t duration_ms;
};

struct OpResultRecord
{
    uint8_t valid;
    OpResultPayload payload;
};

static_assert(sizeof(PacketHeader) == 12u, "PacketHeader wire layout changed");
static_assert(sizeof(StatusPayload) == 88u, "StatusPayload wire layout changed");
static_assert(sizeof(CalibrationPayload) == 24u, "CalibrationPayload wire layout changed");
static_assert(sizeof(OpResultPayload) == 20u, "OpResultPayload wire layout changed");

struct DistanceOp
{
    uint8_t active;
    uint8_t type;
    uint8_t ch;
    uint8_t reason;
    uint32_t op_id;
    uint64_t started_ticks;
    uint64_t deadline_ticks;
    float start_meters;
    float target_meters;
    uint8_t contact_pct;
    uint8_t stop_on_contact;
};

enum : uint8_t
{
    OP_NONE = 0,
    OP_ENCODER_TEST = 1,
    OP_CHANNEL_AUTOLOAD = 2,
    OP_FEED_TO_CONTACT = 3,
    OP_FEED_DISTANCE = 4,
    OP_BUFFER_CALIBRATION = 5,
    OP_CHANNEL_RETRACT = 6
};

enum : uint8_t
{
    OP_STATE_IDLE = 0,
    OP_STATE_RUNNING = 1,
    OP_STATE_DONE = 2,
    OP_STATE_FAILED = 3,
    OP_STATE_ABORTED = 4
};

enum : uint8_t
{
    OP_REASON_NONE = 0,
    OP_REASON_TARGET = 1,
    OP_REASON_TIMEOUT = 2,
    OP_REASON_NO_FILAMENT = 3,
    OP_REASON_ENCODER_IO = 4,
    OP_REASON_BUFFER_LIMIT = 5,
    OP_REASON_ABORTED = 6,
    OP_REASON_BUSY = 7,
    OP_REASON_NOT_CALIBRATED = 8,
    OP_REASON_MOTION_STOPPED = 9,
    OP_REASON_CONTACT = 10,
    OP_REASON_DISTANCE_LIMIT = 11,
    OP_REASON_CAL_RANGE = 12,
    OP_REASON_NVM = 13,
    OP_REASON_SENSOR = 14
};

static uint8_t rx_cobs[165];
static uint16_t rx_cobs_len;
static uint8_t rx_discarding;
static uint8_t rx_pkt_buf[164];
static uint8_t tx_pkt_buf[248];
static uint8_t enc_buf[251];
static uint32_t seq_tx;
static uint8_t host_online;
static uint8_t host_session_ready;
static uint32_t seq_rx_last;
static uint32_t last_rx_tick;
static uint32_t last_status_tick;
static uint8_t last_motion[4];
static uint8_t last_present[4];
static uint8_t last_route_snapshot;
static uint32_t g_session_id;
static uint32_t g_event_counter;
static DistanceOp g_op;
static uint8_t g_last_op_state;
static uint8_t g_last_op_reason;
static OpResultRecord g_op_history[4];
static uint8_t g_op_history_head;
static uint8_t g_update_mode;
static uint64_t g_update_deadline_ticks;
static uint32_t g_update_nvm_crc;
static uint8_t g_update_policy_valid;
static uint8_t g_update_policy_standalone;
static uint8_t g_update_policy_assist;
static uint8_t g_update_policy_unload;

static constexpr uint32_t kCapabilities =
    BMCU_CAP_BUFFER_CAL_3PT | BMCU_CAP_ENCODER_TEST |
    BMCU_CAP_CHANNEL_AUTOLOAD | BMCU_CAP_SLOT_METADATA |
    BMCU_CAP_ASYNC_OPS | BMCU_CAP_LOCAL_CONTACT |
    BMCU_CAP_LOCAL_DISTANCE | BMCU_CAP_OP_REPLAY |
    BMCU_CAP_ROUTE_STATE | BMCU_CAP_VOLATILE_SLOTS |
    BMCU_CAP_ROUTE_CONFIRM | BMCU_CAP_HOST_RUNTIME_CONFIG |
    BMCU_CAP_SYSTEM_LED_RUNTIME | BMCU_CAP_NVM_EXPORT |
    BMCU_CAP_UPDATE_GUARD | BMCU_CAP_SCALABLE_SYNC |
    BMCU_CAP_SESSION_CONFIRM | BMCU_CAP_AUTO_CALIBRATION |
    BMCU_CAP_INDEPENDENT_OUTPUTS | BMCU_CAP_CHANNEL_AUTOLOAD_RUNTIME |
    BMCU_CAP_RUNTIME_LIGHTING | BMCU_CAP_CHANNEL_RETRACT |
    BMCU_CAP_LOAD_PRESSURE_PCT | BMCU_CAP_LED_PREVIEW |
    BMCU_CAP_LED_FILAMENT_PREVIEW;

static uint32_t now_ticks(void) { return time_ticks32(); }

static uint32_t ticks_ms(uint32_t ms)
{
    uint32_t t = time_hw_tpms;
    if (!t) t = 1u;
    return ms * t;
}

static uint64_t deadline_ticks_after_ms(uint32_t ms)
{
    uint32_t t = time_hw_tpms;
    if (!t) t = 1u;
    return time_ticks64() + (uint64_t)ms * (uint64_t)t;
}

static uint32_t elapsed_ms_from_ticks(uint64_t started_ticks, uint64_t now_ticks64)
{
    uint32_t t = time_hw_tpms;
    if (!t) t = 1u;
    const uint64_t elapsed = (now_ticks64 - started_ticks) / (uint64_t)t;
    return elapsed > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)elapsed;
}

static bool any_motion_active(void)
{
    for (uint8_t i = 0; i < 4; i++)
    {
        if ((uint8_t)ams[0].filament[i].motion !=
                (uint8_t)_filament_motion::idle ||
            Motion_control_channel_retract_active(i))
            return true;
    }
    return false;
}

static bool any_operation_active(void)
{
    return g_op.active || MC_PULL_calibration_auto_active();
}

bool bmcu_protocol_busy_for_local_calibration(void)
{
    return any_operation_active() || any_motion_active();
}

static bool runtime_value_valid(uint8_t index, float value)
{
    if (!isfinite(value)) return false;
    switch (index)
    {
    case 0u:
        return ((value >= (float)BMCU_LOAD_PRESSURE_MIN_PCT &&
                 value <= (float)BMCU_LOAD_PRESSURE_MAX_PCT) ||
                (value >= 0.0f && value <= 2.0f)) &&
               value == (float)(uint8_t)value;
    case 1u:
    case 2u: return value >= 10.0f && value <= 120.0f;
    case 3u: return value >= 4.0f && value <= 40.0f;
    case 4u: return value >= 1000.0f && value <= 120000.0f;
    case 5u: return value >= 20.0f && value <= 60.0f;
    case 6u:
    case 7u:
    case 8u:
    case 9u: return value >= 0.010f && value <= 2.000f;
    case 10u:
    case 11u:
    case 12u:
    case 13u: return value >= 0.010f && value <= 1.000f;
    default: return false;
    }
}

static bool packet_is_critical(uint8_t type, uint32_t cmd_id)
{
    if (type == MSG_STATUS) return cmd_id != 0u;
    if (type == MSG_STATE_CHANGED || type == MSG_MOTION_DONE) return false;
    return true;
}

static bool send_packet(uint8_t type, uint32_t cmd_id, const void *payload, uint16_t len)
{
    if (len && !payload) return false;
    const size_t raw_len = sizeof(PacketHeader) + (size_t)len + 4u;
    const size_t encoded_max = raw_len + raw_len / 254u + 1u;
    if (raw_len > sizeof(tx_pkt_buf) || encoded_max + 2u > sizeof(enc_buf))
        return false;

    PacketHeader h;
    h.ver = BMCU_PROTO_VER;
    h.type = type;
    h.len = len;
    h.seq = seq_tx++;
    h.cmd_id = cmd_id;

    uint16_t pos = 0;
    memcpy(tx_pkt_buf + pos, &h, sizeof(h)); pos += sizeof(h);
    if (len) { memcpy(tx_pkt_buf + pos, payload, len); pos += len; }
    const uint32_t crc = bmcu_crc32(tx_pkt_buf, pos);
    memcpy(tx_pkt_buf + pos, &crc, 4u); pos += 4u;

    const size_t enc_len = bmcu_cobs_encode(tx_pkt_buf, pos, enc_buf + 1);
    enc_buf[0] = 0;
    enc_buf[enc_len + 1u] = 0;
    return bmcu_uart_write(enc_buf, (uint16_t)(enc_len + 2u),
                           packet_is_critical(type, cmd_id));
}

static void send_ack(uint32_t cmd_id, uint8_t ok, uint16_t err)
{
    struct __attribute__((packed)) Ack { uint32_t cmd_id; uint8_t ok; uint16_t err; } a;
    a.cmd_id = cmd_id;
    a.ok = ok;
    a.err = err;
    send_packet(MSG_ACK, cmd_id, &a, sizeof(a));
}

static void send_error(uint32_t cmd_id, uint8_t ch, uint16_t err)
{
    struct __attribute__((packed)) E { uint8_t ch; uint16_t err; uint32_t details; } e;
    e.ch = ch;
    e.err = err;
    e.details = 0;
    send_packet(MSG_ERROR, cmd_id, &e, sizeof(e));
}

static void set_active_ch(uint8_t ch)
{
    if (ch >= 4) return;
    if (ams[0].now_filament_num != ch)
    {
        if (ams[0].now_filament_num < 4)
        {
            const uint8_t previous = ams[0].now_filament_num;
            ams[0].filament[previous].motion = _filament_motion::idle;
            Motion_control_set_PWM(previous, 0);
        }
        ams[0].now_filament_num = ch;
    }
}

static bool wire_to_motion(uint8_t wire, _filament_motion* motion)
{
    if (!motion) return false;
    switch (wire)
    {
    case BMCU_MOTION_IDLE:             *motion = _filament_motion::idle; return true;
    case BMCU_MOTION_SEND_OUT:         *motion = _filament_motion::send_out; return true;
    case BMCU_MOTION_BEFORE_ON_USE:    *motion = _filament_motion::before_on_use; return true;
    case BMCU_MOTION_ON_USE:           *motion = _filament_motion::on_use; return true;
    case BMCU_MOTION_BEFORE_PULL_BACK: *motion = _filament_motion::before_pull_back; return true;
    case BMCU_MOTION_PULL_BACK:        *motion = _filament_motion::pull_back; return true;
    case BMCU_MOTION_STOP_ON_USE:      *motion = _filament_motion::stop_on_use; return true;
    default: return false;
    }
}

static uint8_t motion_to_wire(_filament_motion motion)
{
    switch (motion)
    {
    case _filament_motion::idle:             return BMCU_MOTION_IDLE;
    case _filament_motion::send_out:         return BMCU_MOTION_SEND_OUT;
    case _filament_motion::before_on_use:    return BMCU_MOTION_BEFORE_ON_USE;
    case _filament_motion::on_use:           return BMCU_MOTION_ON_USE;
    case _filament_motion::before_pull_back: return BMCU_MOTION_BEFORE_PULL_BACK;
    case _filament_motion::pull_back:        return BMCU_MOTION_PULL_BACK;
    case _filament_motion::stop_on_use:      return BMCU_MOTION_STOP_ON_USE;
    default:                                 return BMCU_MOTION_IDLE;
    }
}

static bool set_motion(uint8_t ch, _filament_motion motion, bool persist_route_change = true)
{
    if (ch >= 4) return false;

    if (motion == _filament_motion::idle)
    {
        ams[0].filament[ch].motion = _filament_motion::idle;
        Motion_control_set_PWM(ch, 0);
        if (ams[0].now_filament_num == ch)
        {
            ams[0].now_filament_num = 0xFF;
            ams[0].filament_use_flag = 0;
        }
        return true;
    }

    if (persist_route_change &&
        ((motion == _filament_motion::send_out &&
          ams_state_get_route_state(ch) == AMS_ROUTE_EMPTY) ||
         motion == _filament_motion::before_on_use ||
         motion == _filament_motion::before_pull_back ||
         motion == _filament_motion::pull_back) &&
        !ams_state_begin_change(ch))
        return false;

    set_active_ch(ch);
    ams[0].filament[ch].motion = motion;

    switch (motion)
    {
    case _filament_motion::idle:

        return true;
    case _filament_motion::send_out:
        ams[0].filament_use_flag = 0x02;
        return true;
    case _filament_motion::before_on_use:
        ams[0].filament_use_flag = 0x04;
        return true;
    case _filament_motion::on_use:
        ams[0].filament_use_flag = 0x04;
        if (!ams_state_set_loaded(ch))
        {
            ams[0].filament[ch].motion = _filament_motion::idle;
            Motion_control_set_PWM(ch, 0);
            if (ams[0].now_filament_num == ch) ams[0].now_filament_num = 0xFFu;
            ams[0].filament_use_flag = 0u;
            return false;
        }
        return true;
    case _filament_motion::before_pull_back:
        ams[0].filament_use_flag = 0x04;
        return true;
    case _filament_motion::pull_back:
        ams[0].filament_use_flag = 0x02;
        return true;
    case _filament_motion::stop_on_use:
        ams[0].filament_use_flag = 0x04;
        return true;
    }
    return false;
}

static void stop_all_motion(void)
{
    Motion_control_cancel_all_channel_retracts();
    for (uint8_t i = 0; i < 4; i++)
    {
        ams[0].filament[i].motion = _filament_motion::idle;
        Motion_control_set_PWM(i, 0);
    }
    ams[0].now_filament_num = 0xFF;
    ams[0].filament_use_flag = 0;
}

static const OpResultRecord* latest_op_result(void)
{
    if (g_op_history_head == 0u)
    {
        const OpResultRecord* last = &g_op_history[3];
        return last->valid ? last : 0;
    }
    const OpResultRecord* last = &g_op_history[(uint8_t)(g_op_history_head - 1u)];
    return last->valid ? last : 0;
}

static const OpResultRecord* find_op_result(uint32_t op_id)
{
    if (op_id == 0u) return latest_op_result();
    for (uint8_t i = 0u; i < 4u; i++)
        if (g_op_history[i].valid && g_op_history[i].payload.op_id == op_id)
            return &g_op_history[i];
    return 0;
}

static void remember_op_result(const OpResultPayload& payload)
{
    OpResultRecord& slot = g_op_history[g_op_history_head];
    slot.valid = 1u;
    slot.payload = payload;
    g_op_history_head = (uint8_t)((g_op_history_head + 1u) & 0x03u);
}

static void abort_auto_calibration_for_session_reset(void)
{
    if (!MC_PULL_calibration_auto_active()) return;
    (void)MC_PULL_calibration_auto_abort();
    uint32_t op_id = 0u, duration_ms = 0u;
    uint8_t state = 0u, reason = 0u, channel = 0xFFu;
    if (!MC_PULL_calibration_auto_take_result(
            &op_id, &state, &reason, &channel, &duration_ms))
        return;
    OpResultPayload result;
    result.op_id = op_id;
    result.type = OP_BUFFER_CALIBRATION;
    result.ch = channel;
    result.state = OP_STATE_ABORTED;
    result.reason = OP_REASON_ABORTED;
    result.target_mm = 0.0f;
    result.measured_mm = 0.0f;
    result.duration_ms = duration_ms;
    remember_op_result(result);
    g_last_op_state = OP_STATE_ABORTED;
    g_last_op_reason = OP_REASON_ABORTED;
    g_event_counter++;
}

static void send_op_record(uint32_t response_cmd_id, const OpResultRecord* record)
{
    if (!record || !record->valid) return;
    send_packet(MSG_OP_RESULT, response_cmd_id, &record->payload, sizeof(record->payload));
}

static void fill_status(StatusPayload *status)
{
    memset(status, 0, sizeof(*status));
    status->now_filament_num = ams[0].now_filament_num;
    for (uint8_t ch = 0u; ch < 4u; ch++)
        status->route_state[ch] = ams_state_get_route_state(ch);
    status->calibration_valid_mask = MC_PULL_calibration_valid_mask();
    status->session_id = g_session_id;
    status->event_counter = g_event_counter;
    const OpResultRecord* last_result = latest_op_result();
    const bool auto_cal_active = MC_PULL_calibration_auto_active();
    if (auto_cal_active)
    {
        status->active_op_id = MC_PULL_calibration_auto_op_id();
        status->active_op_type = OP_BUFFER_CALIBRATION;
        status->active_op_ch = MC_PULL_calibration_auto_channel();
        status->active_op_state = OP_STATE_RUNNING;
        status->active_op_reason = OP_REASON_NONE;
    }
    else if (g_op.active)
    {
        status->active_op_id = g_op.op_id;
        status->active_op_type = g_op.type;
        status->active_op_ch = g_op.ch;
        status->active_op_state = OP_STATE_RUNNING;
        status->active_op_reason = OP_REASON_NONE;
    }
    else
    {
        status->active_op_id = last_result ? last_result->payload.op_id : 0u;
        status->active_op_type = last_result ? last_result->payload.type : static_cast<uint8_t>(OP_NONE);
        status->active_op_ch = last_result ? last_result->payload.ch : 0xFFu;
        status->active_op_state = last_result ? last_result->payload.state : g_last_op_state;
        status->active_op_reason = last_result ? last_result->payload.reason : g_last_op_reason;
    }
    status->auto_cal_stage = MC_PULL_calibration_auto_stage();
    status->auto_cal_progress = MC_PULL_calibration_auto_progress();
    status->auto_cal_mask = MC_PULL_calibration_auto_selected_mask();
    status->auto_cal_done_mask = MC_PULL_calibration_auto_done_mask();
    status->auto_cal_state = MC_PULL_calibration_auto_state();
    status->auto_cal_reason = MC_PULL_calibration_auto_reason();
    if (Flash_saves_faulted()) status->error_flags |= 0x0001u;
    if (ams_state_get_uncertain_mask() != 0u) status->error_flags |= 0x0002u;
    status->error_flags |= (uint16_t)((Flash_saves_bad_page_mask() & 0x3FFFu) << 2);

    for (uint8_t ch = 0; ch < 4; ch++)
    {

        status->motion[ch] = Motion_control_channel_retract_active(ch)
            ? BMCU_MOTION_PULL_BACK
            : motion_to_wire(ams[0].filament[ch].motion);
        status->present[ch] = Motion_control_filament_present(ch);
        status->buffer_pct[ch] = MC_PULL_pct[ch];
        status->calibration_capture_mask[ch] = MC_PULL_calibration_capture_mask(ch);
        status->meters[ch] = Motion_control_encoder_meters(ch);
        status->buffer_raw[ch] = MC_PULL_calibration_raw(ch);
        status->motor_pwm[ch] = Motion_control_get_pwm(ch);
        if (Motion_control_encoder_io_ok(ch)) status->encoder_ok_mask |= (uint8_t)(1u << ch);
        if (Motion_control_channel_connected(ch)) status->connected_mask |= (uint8_t)(1u << ch);
    }
}

static void send_status(uint32_t cmd_id)
{
    StatusPayload status;
    fill_status(&status);
    send_packet(MSG_STATUS, cmd_id, &status, sizeof(status));
}

static bool replay_known_operation(uint32_t op_id)
{
    if ((g_op.active && g_op.op_id == op_id) ||
        (MC_PULL_calibration_auto_active() &&
         MC_PULL_calibration_auto_op_id() == op_id))
    {
        send_ack(op_id, 1u, 0u);
        send_status(0u);
        return true;
    }
    const OpResultRecord* result = find_op_result(op_id);
    if (!result) return false;
    send_ack(op_id, 1u, 0u);
    send_op_record(op_id, result);
    send_status(0u);
    return true;
}

static void send_caps(uint32_t cmd_id)
{
    struct __attribute__((packed)) Caps
    {
        uint8_t channels;
        uint8_t hw_variant;
        uint8_t has_online_rgb;
        uint8_t fw_major;
        uint8_t fw_minor;
        uint8_t fw_patch;
        uint8_t protocol;
        uint8_t reserved;
        uint32_t capabilities;
        uint8_t uid[12];
        char hw_name[16];
    } caps;
    memset(&caps, 0, sizeof(caps));
    caps.channels = 4;
#if BMCU_DM_TWO_MICROSWITCH
    caps.hw_variant = 1;
#else
    caps.hw_variant = 0;
#endif
    caps.has_online_rgb = 1;
    caps.fw_major = BMCU_FW_MAJOR;
    caps.fw_minor = BMCU_FW_MINOR;
    caps.fw_patch = BMCU_FW_PATCH;
    caps.protocol = BMCU_PROTO_VER;
    caps.capabilities = kCapabilities;
    bmcu_uid_get(caps.uid);
    memcpy(caps.hw_name, "BMCU-Klipper", 12);
    send_packet(MSG_CAPS, cmd_id, &caps, sizeof(caps));
}

static void send_hello_ack(uint32_t cmd_id)
{
    struct __attribute__((packed)) HelloAck
    {
        uint8_t uid[12];
        uint8_t fw_major;
        uint8_t fw_minor;
        uint8_t fw_patch;
        uint8_t protocol;
        uint32_t capabilities;
        uint32_t session_id;
        uint8_t channels;
        uint8_t load_pressure_pct;
    } ack;
    memset(&ack, 0, sizeof(ack));
    bmcu_uid_get(ack.uid);
    ack.fw_major = BMCU_FW_MAJOR;
    ack.fw_minor = BMCU_FW_MINOR;
    ack.fw_patch = BMCU_FW_PATCH;
    ack.protocol = BMCU_PROTO_VER;
    ack.capabilities = kCapabilities;
    ack.session_id = g_session_id;
    ack.channels = 4;
    ack.load_pressure_pct = bmcu_config_load_pressure_pct();
    send_packet(MSG_HELLO_ACK, cmd_id, &ack, sizeof(ack));
}

static bool apply_slot_payload(const uint8_t *payload, uint16_t len)
{
    if (!payload || len != 37u || payload[0] >= 4u) return false;
    _filament &filament = ams[0].filament[payload[0]];
    filament.color_R = payload[1];
    filament.color_G = payload[2];
    filament.color_B = payload[3];
    filament.color_A = payload[4];
    filament.temperature_min = (int16_t)(payload[5] | ((uint16_t)payload[6] << 8));
    filament.temperature_max = (int16_t)(payload[7] | ((uint16_t)payload[8] << 8));
    memset(filament.name, 0, sizeof(filament.name));
    memcpy(filament.name, payload + 9, 20);
    filament.name[sizeof(filament.name) - 1] = 0;
    memset(filament.bambubus_filament_id, 0, sizeof(filament.bambubus_filament_id));
    memcpy(filament.bambubus_filament_id, payload + 29, 8);
    return true;
}

static void send_slot(uint32_t cmd_id, uint8_t ch)
{
    if (ch >= 4) { send_error(cmd_id, ch, 1); return; }
    struct __attribute__((packed)) Slot
    {
        uint8_t ch;
        uint8_t r, g, b, a;
        uint16_t tmin, tmax;
        char name[20];
        char mat_id[8];
    } slot;
    memset(&slot, 0, sizeof(slot));
    const _filament &f = ams[0].filament[ch];
    slot.ch = ch;
    slot.r = f.color_R;
    slot.g = f.color_G;
    slot.b = f.color_B;
    slot.a = f.color_A;
    slot.tmin = (uint16_t)f.temperature_min;
    slot.tmax = (uint16_t)f.temperature_max;
    memcpy(slot.name, f.name, sizeof(slot.name));
    memcpy(slot.mat_id, f.bambubus_filament_id, sizeof(slot.mat_id));
    send_packet(MSG_SLOT_INFO, cmd_id, &slot, sizeof(slot));
}

static void fill_calibration(CalibrationPayload *cal, uint8_t ch)
{
    cal->ch = ch;
    cal->capture_mask = MC_PULL_calibration_capture_mask(ch);
    cal->valid = MC_PULL_calibration_is_valid(ch) ? 1u : 0u;
    cal->polarity = MC_PULL_POLARITY[ch];
    cal->current_raw = MC_PULL_calibration_raw(ch);
    cal->offset = MC_PULL_V_OFFSET[ch];
    cal->min_value = MC_PULL_V_MIN[ch];
    cal->neutral_value = 1.65f;
    cal->max_value = MC_PULL_V_MAX[ch];
}

static void send_calibration(uint32_t cmd_id, uint8_t ch)
{
    if (ch >= 4u) { send_error(cmd_id, ch, 20); return; }
    CalibrationPayload cal;
    fill_calibration(&cal, ch);
    send_packet(MSG_CALIBRATION, cmd_id, &cal, sizeof(cal));
}

static void send_snapshot(uint32_t cmd_id)
{
    struct __attribute__((packed)) Snapshot
    {
        StatusPayload status;
        CalibrationPayload calibration[4];
    } snapshot;
    fill_status(&snapshot.status);
    for (uint8_t ch = 0u; ch < 4u; ch++)
        fill_calibration(&snapshot.calibration[ch], ch);
    send_packet(MSG_SNAPSHOT, cmd_id, &snapshot, sizeof(snapshot));
}

static void send_op_result(uint32_t op_id, uint8_t type, uint8_t ch,
                           uint8_t state, uint8_t reason, float target_m,
                           float measured_m, uint32_t duration_ms)
{
    OpResultPayload result;
    result.op_id = op_id;
    result.type = type;
    result.ch = ch;
    result.state = state;
    result.reason = reason;
    result.target_mm = target_m * 1000.0f;
    result.measured_mm = measured_m * 1000.0f;
    result.duration_ms = duration_ms;
    remember_op_result(result);
    const OpResultRecord* stored = find_op_result(op_id);
    send_op_record(op_id, stored);
}

static uint32_t distance_timeout_ms(float mm, float speed_mms, uint32_t minimum_ms)
{
    if (!isfinite(mm) || mm <= 0.0f) return minimum_ms;
    if (!isfinite(speed_mms) || speed_mms < 1.0f) speed_mms = 1.0f;

    float estimate = (mm / speed_mms) * 2000.0f + 1500.0f;
    if (estimate < (float)minimum_ms) estimate = (float)minimum_ms;
    if (estimate > 300000.0f) estimate = 300000.0f;
    return (uint32_t)estimate;
}

static bool start_distance_op(uint32_t op_id, uint8_t type, uint8_t ch,
                              float mm, uint32_t timeout_ms,
                              uint8_t contact_pct, bool stop_on_contact,
                              bool occupies_route)
{
    if (any_operation_active() || any_motion_active()) return false;
    if (ch >= 4u || !isfinite(mm) || mm < 5.0f || mm > 5000.0f) return false;
    if (timeout_ms < 250u || timeout_ms > 300000u) return false;
    if (contact_pct < 55u || contact_pct > 98u) return false;
    if (!MC_PULL_calibration_is_valid(ch)) return false;
    if (!Motion_control_filament_present(ch)) return false;
    if (!Motion_control_encoder_io_ok(ch)) return false;

    const bool changes_route = occupies_route;
    if (changes_route && !ams_state_begin_change(ch)) return false;

    memset(&g_op, 0, sizeof(g_op));
    g_op.active = 1u;
    g_op.type = type;
    g_op.ch = ch;
    g_op.op_id = op_id;
    g_op.started_ticks = time_ticks64();
    g_op.deadline_ticks = g_op.started_ticks +
                          (uint64_t)timeout_ms * (uint64_t)(time_hw_tpms ? time_hw_tpms : 1u);
    g_op.start_meters = Motion_control_encoder_meters(ch);
    g_op.target_meters = mm / 1000.0f;
    g_op.contact_pct = contact_pct;
    g_op.stop_on_contact = stop_on_contact ? 1u : 0u;
    g_last_op_state = OP_STATE_RUNNING;
    g_last_op_reason = OP_REASON_NONE;

    if (!set_motion(ch, _filament_motion::send_out, false))
    {
        g_op.active = 0u;
        return false;
    }
    return true;
}

static bool start_channel_retract_op(uint32_t op_id, uint8_t ch)
{
    if (any_operation_active() || any_motion_active()) return false;
    if (ch >= 4u || ams_state_get_route_state(ch) != AMS_ROUTE_EMPTY)
        return false;
    if (!MC_PULL_calibration_is_valid(ch) ||
        !Motion_control_filament_present(ch) ||
        !Motion_control_encoder_io_ok(ch))
        return false;

    memset(&g_op, 0, sizeof(g_op));
    g_op.active = 1u;
    g_op.type = OP_CHANNEL_RETRACT;
    g_op.ch = ch;
    g_op.op_id = op_id;
    g_op.started_ticks = time_ticks64();
    g_op.deadline_ticks = g_op.started_ticks +
        (uint64_t)300000u *
        (uint64_t)(time_hw_tpms ? time_hw_tpms : 1u);
    g_op.start_meters = Motion_control_encoder_meters(ch);
    g_op.target_meters = 5.0f;
    g_last_op_state = OP_STATE_RUNNING;
    g_last_op_reason = OP_REASON_NONE;
    if (!Motion_control_start_channel_retract(ch))
    {
        g_op.active = 0u;
        return false;
    }
    return true;
}

static void finish_distance_op(uint8_t state, uint8_t reason)
{
    if (!g_op.active) return;
    if (g_op.type == OP_CHANNEL_RETRACT)
        Motion_control_cancel_channel_retract(g_op.ch);
    const uint64_t now_ticks64 = time_ticks64();
    const float measured = fabsf(
        Motion_control_encoder_meters(g_op.ch) - g_op.start_meters);
    const uint32_t duration_ms = elapsed_ms_from_ticks(g_op.started_ticks, now_ticks64);
    (void)set_motion(g_op.ch, _filament_motion::idle);
    Motion_control_set_PWM(g_op.ch, 0);
    send_op_result(g_op.op_id, g_op.type, g_op.ch, state, reason,
                   g_op.target_meters, measured, duration_ms);
    g_last_op_state = state;
    g_last_op_reason = reason;
    g_op.active = 0u;
    g_event_counter++;
    send_status(0);
}

static void abort_distance_op_for_session_reset(void)
{
    if (!g_op.active) return;
    if (g_op.type == OP_CHANNEL_RETRACT)
        Motion_control_cancel_channel_retract(g_op.ch);
    const uint64_t now_ticks64 = time_ticks64();
    const float measured = fabsf(
        Motion_control_encoder_meters(g_op.ch) - g_op.start_meters);
    const uint32_t duration_ms = elapsed_ms_from_ticks(g_op.started_ticks, now_ticks64);
    (void)set_motion(g_op.ch, _filament_motion::idle);
    Motion_control_set_PWM(g_op.ch, 0);

    OpResultPayload result;
    result.op_id = g_op.op_id;
    result.type = g_op.type;
    result.ch = g_op.ch;
    result.state = OP_STATE_ABORTED;
    result.reason = OP_REASON_ABORTED;
    result.target_mm = g_op.target_meters * 1000.0f;
    result.measured_mm = measured * 1000.0f;
    result.duration_ms = duration_ms;
    remember_op_result(result);

    g_last_op_state = OP_STATE_ABORTED;
    g_last_op_reason = OP_REASON_ABORTED;
    g_op.active = 0u;
    g_event_counter++;
}

static void process_distance_op(void)
{
    if (!g_op.active) return;
    const uint64_t now_ticks64 = time_ticks64();
    const float measured = fabsf(
        Motion_control_encoder_meters(g_op.ch) - g_op.start_meters);

    if (g_op.type == OP_CHANNEL_RETRACT)
    {
        if (ams_state_get_route_state(g_op.ch) != AMS_ROUTE_EMPTY)
        {
            finish_distance_op(OP_STATE_FAILED, OP_REASON_BUSY);
            return;
        }
        if (!Motion_control_encoder_io_ok(g_op.ch))
        {
            finish_distance_op(OP_STATE_FAILED, OP_REASON_ENCODER_IO);
            return;
        }
        if (now_ticks64 >= g_op.deadline_ticks)
        {
            finish_distance_op(OP_STATE_FAILED, OP_REASON_TIMEOUT);
            return;
        }
        if (Motion_control_channel_retract_active(g_op.ch)) return;
        if (Motion_control_channel_retract_target_reached(g_op.ch))
        {
            finish_distance_op(OP_STATE_DONE, OP_REASON_TARGET);
            return;
        }
        finish_distance_op(
            OP_STATE_FAILED,
            MC_PULL_pct[g_op.ch] < 35u ? OP_REASON_BUFFER_LIMIT :
                                        OP_REASON_SENSOR);
        return;
    }

    if (!Motion_control_filament_present(g_op.ch))
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_NO_FILAMENT);
        return;
    }
    if (!Motion_control_encoder_io_ok(g_op.ch))
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_ENCODER_IO);
        return;
    }
    if (MC_PULL_pct[g_op.ch] <= 3u)
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_BUFFER_LIMIT);
        return;
    }
    if (g_op.stop_on_contact && MC_PULL_pct[g_op.ch] >= g_op.contact_pct)
    {
        finish_distance_op(OP_STATE_DONE, OP_REASON_CONTACT);
        return;
    }
    if (!g_op.stop_on_contact && MC_PULL_pct[g_op.ch] >= g_op.contact_pct)
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_BUFFER_LIMIT);
        return;
    }
    if (measured >= g_op.target_meters)
    {
        finish_distance_op(g_op.stop_on_contact ? OP_STATE_FAILED : OP_STATE_DONE,
                           g_op.stop_on_contact ? OP_REASON_DISTANCE_LIMIT : OP_REASON_TARGET);
        return;
    }
    if ((uint8_t)ams[0].filament[g_op.ch].motion == (uint8_t)_filament_motion::idle)
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_MOTION_STOPPED);
        return;
    }
    if (now_ticks64 >= g_op.deadline_ticks)
    {
        finish_distance_op(OP_STATE_FAILED, OP_REASON_TIMEOUT);
        return;
    }
}

static bool update_command_allowed(uint8_t type)
{
    switch (type)
    {
    case MSG_HELLO:
    case MSG_PING:
    case MSG_GET_STATUS:
    case MSG_SESSION_CONFIRM:
    case MSG_GET_CAPS:
    case MSG_UPDATE_PREPARE:
    case MSG_NVM_READ:
    case MSG_UPDATE_CANCEL:
    case MSG_STOP_ALL:
        return true;
    default:
        return false;
    }
}

static bool update_command_refreshes_deadline(uint8_t type)
{
    return type == MSG_UPDATE_PREPARE || type == MSG_NVM_READ;
}

static void update_mode_set(bool active)
{
    g_update_mode = active ? 1u : 0u;
    if (active)
    {
        g_update_deadline_ticks = deadline_ticks_after_ms(120000u);
        g_update_nvm_crc = bmcu_crc32((const uint8_t*)FLASH_NVM_BASE_ADDR,
                                      FLASH_NVM_TOTAL_SIZE);
    }
    else
    {
        g_update_deadline_ticks = 0ull;
        g_update_nvm_crc = 0u;
    }
    system_led_set_update_mode(active);
}

static int16_t command_payload_size(uint8_t type)
{
    switch (type)
    {
    case MSG_GET_STATUS:
    case MSG_GET_CAPS:
    case MSG_GET_SNAPSHOT:
    case MSG_CLEAR_CALIBRATION:
    case MSG_ABORT_OP:
    case MSG_CONFIG_SAVE:
    case MSG_UPDATE_PREPARE:
    case MSG_UPDATE_CANCEL:
    case MSG_STOP_ALL:
    case MSG_RESET_ERROR:
        return 0;
    case MSG_GET_CALIBRATION:
    case MSG_CAL_AUTO_START:
    case MSG_SET_ACTIVE_CH:
    case MSG_MARK_UNLOADED:
    case MSG_MARK_LOADED:
    case MSG_CAL_COMMIT:
    case MSG_GET_SLOT_INFO:
    case MSG_CHANNEL_RETRACT:
        return 1;
    case MSG_SET_MOTION:
    case MSG_CAL_CAPTURE:
    case MSG_CONFIG_GET:
        return 2;
    case MSG_SET_SYSTEM_LED:
        return 3;
    case MSG_LED_PREVIEW:
        return 4;
    case MSG_SET_LIGHTING:
        return BMCU_LIGHTING_PAYLOAD_SIZE;
    case MSG_HELLO:
    case MSG_GET_OP_RESULT:
    case MSG_NVM_READ:
    case MSG_SESSION_CONFIRM:
        return 4;
    case MSG_TEST_ENCODER:
    case MSG_CHANNEL_AUTOLOAD:
        return 5;
    case MSG_CONFIG_SET:
        return 6;
    case MSG_FEED_TO_CONTACT:
    case MSG_FEED_DISTANCE:
        return 10;
    case MSG_SET_SLOT_INFO:
        return 37;
    case MSG_RUNTIME_SYNC:
        return 63;
    case MSG_SET_SLOTS:
        return 148;
    case MSG_PING:
        return -2;
    default:
        return -1;
    }
}

static void rotate_session(uint32_t host_nonce)
{
    uint8_t entropy[44];
    uint8_t uid[12];
    bmcu_uid_get(uid);
    memcpy(entropy, uid, sizeof(uid));
    memcpy(entropy + 12u, &g_session_id, sizeof(g_session_id));
    memcpy(entropy + 16u, &host_nonce, sizeof(host_nonce));
    const uint64_t ticks = time_ticks64();
    memcpy(entropy + 20u, &ticks, sizeof(ticks));
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        const float raw = MC_PULL_calibration_raw(ch);
        memcpy(entropy + 28u + (uint16_t)ch * 4u, &raw, sizeof(raw));
    }
    g_session_id = bmcu_crc32(entropy, sizeof(entropy));
    if (!g_session_id) g_session_id = 1u;
}

static void handle_packet(const uint8_t *data, uint16_t len)
{
    if (len < sizeof(PacketHeader) + 4u) return;

    uint32_t stored;
    memcpy(&stored, data + len - 4u, sizeof(stored));
    if (stored != bmcu_crc32(data, len - 4u)) return;

    PacketHeader header;
    memcpy(&header, data, sizeof(header));
    if (header.ver != BMCU_PROTO_VER) return;
    if ((uint32_t)header.len + sizeof(PacketHeader) + 4u != len) return;
    const uint8_t *payload = data + sizeof(PacketHeader);
    const int16_t expected = command_payload_size(header.type);
    if (expected == -1)
    {
        send_error(header.cmd_id, 0xFFu, 0xFFFFu);
        return;
    }
    if ((expected >= 0 && header.len != (uint16_t)expected) ||
        (expected == -2 && header.len > 128u))
    {
        send_error(header.cmd_id, 0xFFu, 100u);
        return;
    }

    if (header.type == MSG_HELLO)
    {
        uint32_t host_nonce = 0u;
        memcpy(&host_nonce, payload, sizeof(host_nonce));
        host_online = 0u;
        host_session_ready = 0u;
        Motion_control_set_host_motion_enabled(false);
        bmcu_policy_disable();
        g_update_policy_valid = 0u;
        if (g_op.active) abort_distance_op_for_session_reset();
        abort_auto_calibration_for_session_reset();
        if (any_motion_active()) stop_all_motion();
        bmcu_uart_discard_pending();
        rotate_session(host_nonce);
        send_hello_ack(header.cmd_id);
        return;
    }

    if (header.type == MSG_SESSION_CONFIRM)
    {
        uint32_t session = 0u;
        memcpy(&session, payload, sizeof(session));
        if (session != g_session_id)
        {
            send_error(header.cmd_id, 0xFFu, 96u);
            return;
        }

        if (!host_session_ready)
        {
            host_session_ready = 1u;
            seq_rx_last = header.seq;
        }
        last_rx_tick = now_ticks();
        host_online = 1u;
        send_ack(header.cmd_id, 1u, 0u);
        return;
    }

    if (!host_session_ready)
    {
        send_error(header.cmd_id, 0xFFu, 96u);
        return;
    }
    const uint32_t rx_delta = header.seq - seq_rx_last;
    if (!rx_delta || rx_delta >= 0x80000000u) return;
    seq_rx_last = header.seq;

    last_rx_tick = now_ticks();
    host_online = 1;
    if (g_update_mode && update_command_refreshes_deadline(header.type))
        g_update_deadline_ticks = deadline_ticks_after_ms(120000u);

    if (g_update_mode && !update_command_allowed(header.type))
    {
        send_error(header.cmd_id, 0xFFu, 90u);
        return;
    }

    switch (header.type)
    {
    case MSG_PING:
        send_packet(MSG_PONG, header.cmd_id, payload, header.len);
        break;
    case MSG_GET_STATUS:
        send_status(header.cmd_id);
        break;
    case MSG_GET_CAPS:
        send_caps(header.cmd_id);
        break;
    case MSG_GET_CALIBRATION:
        send_calibration(header.cmd_id, payload[0]);
        break;
    case MSG_GET_OP_RESULT:
        {
            uint32_t requested_op = 0u;
            memcpy(&requested_op, payload, sizeof(requested_op));
            const OpResultRecord* result = find_op_result(requested_op);
            if (!result) { send_error(header.cmd_id, 0xFF, 22); break; }
            send_op_record(header.cmd_id, result);
        }
        break;
    case MSG_GET_SNAPSHOT:
        send_snapshot(header.cmd_id);
        break;
    case MSG_SET_ACTIVE_CH:
        if (payload[0] >= 4u) { send_error(header.cmd_id, 0xFF, 2); break; }
        set_active_ch(payload[0]);
        send_ack(header.cmd_id, 1, 0);
        send_status(0);
        break;
    case MSG_SET_MOTION:
        {
            _filament_motion requested = _filament_motion::idle;
            if (payload[0] >= 4u || any_operation_active() ||
                    !wire_to_motion(payload[1], &requested))
            {
                send_error(header.cmd_id, 0xFF, 3);
                break;
            }
            if (set_motion(payload[0], requested))
            {
                last_motion[payload[0]] = (uint8_t)requested;
                g_event_counter++;
                send_ack(header.cmd_id, 1, 0);
                send_status(0);
            }
            else send_error(header.cmd_id, payload[0], 4);
        }
        break;
    case MSG_MARK_UNLOADED:
        if (payload[0] >= 4u || any_operation_active())
        {
            send_error(header.cmd_id, 0xFF, 5);
            break;
        }
        {
            const uint8_t ch = payload[0];

            ams[0].filament[ch].motion = _filament_motion::idle;
            Motion_control_set_PWM(ch, 0);
            if (!ams_state_set_unloaded(ch) || !ams_state_save_run())
            {
                send_error(header.cmd_id, ch, 71);
                send_status(0);
                break;
            }
            if (ams[0].now_filament_num == ch)
            {
                ams[0].now_filament_num = 0xFF;
                ams[0].filament_use_flag = 0;
            }
            send_ack(header.cmd_id, 1, 0);
            g_event_counter++;
            send_status(0);
        }
        break;
    case MSG_MARK_LOADED:
        if (payload[0] >= 4u || any_operation_active())
        {
            send_error(header.cmd_id, 0xFF, 5);
            break;
        }
        {
            const uint8_t ch = payload[0];

            ams[0].filament[ch].motion = _filament_motion::idle;
            Motion_control_set_PWM(ch, 0);
            if (!ams_state_set_loaded(ch) || !ams_state_save_run())
            {
                send_error(header.cmd_id, ch, 71);
                send_status(0);
                break;
            }
            if (ams[0].now_filament_num == ch)
            {
                ams[0].now_filament_num = 0xFF;
                ams[0].filament_use_flag = 0;
            }
            send_ack(header.cmd_id, 1, 0);
            g_event_counter++;
            send_status(0);
        }
        break;
    case MSG_CAL_CAPTURE:
        if (payload[0] >= 4u || payload[1] > 2u || any_operation_active() || any_motion_active())
        {
            send_error(header.cmd_id, 0xFF, 21);
            break;
        }
        {
            float raw = 0.0f;
            if (!MC_PULL_calibration_capture(payload[0], payload[1], &raw))
            {
                send_error(header.cmd_id, payload[0], 22);
                break;
            }
            send_calibration(header.cmd_id, payload[0]);
            send_status(0);
        }
        break;
    case MSG_CAL_COMMIT:
        if (payload[0] >= 4u || any_operation_active() || any_motion_active())
        {
            send_error(header.cmd_id, 0xFF, 23);
            break;
        }
        if (!MC_PULL_calibration_commit(payload[0]))
        {
            send_error(header.cmd_id, payload[0], 24);
            break;
        }
        send_calibration(header.cmd_id, payload[0]);
        send_status(0);
        break;
    case MSG_CLEAR_CALIBRATION:
        if (any_operation_active() || any_motion_active()) { send_error(header.cmd_id, 0xFF, 25); break; }
        if (!MC_PULL_calibration_clear())
        {
            send_error(header.cmd_id, 0xFFu, 72u);
            send_status(0);
            break;
        }
        send_ack(header.cmd_id, 1, 0);
        send_status(0);
        break;
    case MSG_CAL_AUTO_START:
        if (replay_known_operation(header.cmd_id)) break;
        {
            const uint8_t mask = (uint8_t)(payload[0] & 0x0Fu);
            const uint8_t occupied = (uint8_t)(ams_state_get_loaded_mask() |
                                               ams_state_get_uncertain_mask());
            if (!mask || any_operation_active() || any_motion_active() ||
                (occupied & mask) != 0u)
            {
                send_op_result(header.cmd_id, OP_BUFFER_CALIBRATION, 0xFFu,
                               OP_STATE_FAILED, OP_REASON_BUSY, 0.0f, 0.0f, 0u);
                break;
            }
            stop_all_motion();
            Motion_control_prepare_calibration();
            if (!MC_PULL_calibration_auto_start(mask, header.cmd_id))
            {
                send_op_result(header.cmd_id, OP_BUFFER_CALIBRATION, 0xFFu,
                               OP_STATE_FAILED, OP_REASON_BUSY, 0.0f, 0.0f, 0u);
                break;
            }
            g_last_op_state = OP_STATE_RUNNING;
            g_last_op_reason = OP_REASON_NONE;
            send_ack(header.cmd_id, 1u, 0u);
            g_event_counter++;
            send_status(0u);
        }
        break;
    case MSG_TEST_ENCODER:
    case MSG_CHANNEL_AUTOLOAD:
        if (replay_known_operation(header.cmd_id)) break;
        if (payload[0] >= 4u)
        {
            send_error(header.cmd_id, 0xFF, 30);
            break;
        }
        {
            float mm = 0.0f;
            memcpy(&mm, payload + 1, sizeof(float));
            const uint8_t type = header.type == MSG_TEST_ENCODER ? OP_ENCODER_TEST : OP_CHANNEL_AUTOLOAD;
            const uint32_t timeout = distance_timeout_ms(
                mm, bmcu_config_load_speed(), type == OP_ENCODER_TEST ? 3000u : 6000u);
            if (!start_distance_op(header.cmd_id, type, payload[0], mm, timeout, 95u, false, false))
            {
                uint8_t reason = OP_REASON_BUSY;
                if (!MC_PULL_calibration_is_valid(payload[0])) reason = OP_REASON_NOT_CALIBRATED;
                else if (!Motion_control_filament_present(payload[0])) reason = OP_REASON_NO_FILAMENT;
                else if (!Motion_control_encoder_io_ok(payload[0])) reason = OP_REASON_ENCODER_IO;
                send_op_result(header.cmd_id, type, payload[0], OP_STATE_FAILED, reason, mm / 1000.0f, 0.0f, 0u);
                break;
            }
            send_ack(header.cmd_id, 1, 0);
            send_status(0);
        }
        break;
    case MSG_FEED_TO_CONTACT:
    case MSG_FEED_DISTANCE:
        if (replay_known_operation(header.cmd_id)) break;
        if (payload[0] >= 4u)
        {
            send_error(header.cmd_id, 0xFF, 31);
            break;
        }
        {
            float mm = 0.0f;
            uint32_t timeout_ms = 0u;
            memcpy(&mm, payload + 1, sizeof(float));
            const uint8_t contact_pct = payload[5];
            memcpy(&timeout_ms, payload + 6, sizeof(uint32_t));
            const bool contact = header.type == MSG_FEED_TO_CONTACT;
            const uint8_t type = contact ? OP_FEED_TO_CONTACT : OP_FEED_DISTANCE;
            if (!start_distance_op(header.cmd_id, type, payload[0], mm,
                                   timeout_ms, contact_pct, contact, true))
            {
                uint8_t reason = OP_REASON_BUSY;
                if (!MC_PULL_calibration_is_valid(payload[0])) reason = OP_REASON_NOT_CALIBRATED;
                else if (!Motion_control_filament_present(payload[0])) reason = OP_REASON_NO_FILAMENT;
                else if (!Motion_control_encoder_io_ok(payload[0])) reason = OP_REASON_ENCODER_IO;
                send_op_result(header.cmd_id, type, payload[0], OP_STATE_FAILED,
                               reason, mm / 1000.0f, 0.0f, 0u);
                break;
            }
            send_ack(header.cmd_id, 1, 0);
            send_status(0);
        }
        break;
    case MSG_ABORT_OP:
        if (g_op.active) finish_distance_op(OP_STATE_ABORTED, OP_REASON_ABORTED);
        if (MC_PULL_calibration_auto_active())
            (void)MC_PULL_calibration_auto_abort();
        send_ack(header.cmd_id, 1, 0);
        send_status(0u);
        break;
    case MSG_CHANNEL_RETRACT:
        if (replay_known_operation(header.cmd_id)) break;
        if (payload[0] >= 4u)
        {
            send_error(header.cmd_id, 0xFFu, 96u);
            break;
        }
        if (!start_channel_retract_op(header.cmd_id, payload[0]))
        {
            uint8_t reason = OP_REASON_BUSY;
            if (!MC_PULL_calibration_is_valid(payload[0]))
                reason = OP_REASON_NOT_CALIBRATED;
            else if (!Motion_control_filament_present(payload[0]))
                reason = OP_REASON_NO_FILAMENT;
            else if (!Motion_control_encoder_io_ok(payload[0]))
                reason = OP_REASON_ENCODER_IO;
            send_op_result(header.cmd_id, OP_CHANNEL_RETRACT, payload[0],
                           OP_STATE_FAILED, reason, 5.0f, 0.0f, 0u);
            break;
        }
        send_ack(header.cmd_id, 1u, 0u);
        g_event_counter++;
        send_status(0u);
        break;
    case MSG_SET_SLOT_INFO:
        if (!apply_slot_payload(payload, header.len))
        {
            send_error(header.cmd_id, 0xFFu, 5u);
            break;
        }
        send_slot(header.cmd_id, payload[0]);
        break;
    case MSG_SET_SLOTS:
        for (uint8_t ch = 0u; ch < 4u; ch++)
        {
            const uint8_t *slot = payload + (uint16_t)ch * 37u;
            if (slot[0] != ch || slot[0] >= 4u)
            {
                send_error(header.cmd_id, ch, 13u);
                return;
            }
        }
        for (uint8_t ch = 0u; ch < 4u; ch++)
            (void)apply_slot_payload(payload + (uint16_t)ch * 37u, 37u);
        send_ack(header.cmd_id, 1u, 0u);
        break;
    case MSG_GET_SLOT_INFO:
        if (payload[0] >= 4u) { send_error(header.cmd_id, 0xFF, 6); break; }
        send_slot(header.cmd_id, payload[0]);
        break;
    case MSG_CONFIG_GET:
        {
            const uint16_t key = (uint16_t)(payload[0] | (payload[1] << 8));
            float value = 0.0f;
            if (!bmcu_config_get(key, &value)) { send_error(header.cmd_id, 0xFF, 8); break; }
            struct __attribute__((packed)) CV { uint16_t key; float value; } cv;
            cv.key = key;
            cv.value = value;
            send_packet(MSG_CONFIG_VAL, header.cmd_id, &cv, sizeof(cv));
        }
        break;
    case MSG_CONFIG_SET:
        {
            const uint16_t key = (uint16_t)(payload[0] | (payload[1] << 8));
            float value;
            memcpy(&value, payload + 2, sizeof(float));
            if (!bmcu_config_set(key, value)) { send_error(header.cmd_id, 0xFF, 10); break; }
            send_ack(header.cmd_id, 1, 0);
        }
        break;
    case MSG_CONFIG_SAVE:
        send_ack(header.cmd_id, bmcu_config_save() ? 1u : 0u, 0);
        break;
    case MSG_SET_SYSTEM_LED:
        system_led_set_normal_rgb(payload[0], payload[1], payload[2]);
        send_ack(header.cmd_id, 1u, 0u);
        break;
    case MSG_SET_LIGHTING:
        if (!lighting_apply_payload(payload, header.len))
        {
            send_error(header.cmd_id, 0xFFu, 37u);
            break;
        }
        send_ack(header.cmd_id, 1u, 0u);
        break;
    case MSG_LED_PREVIEW:
        if (g_update_mode || any_operation_active() || any_motion_active())
        {
            send_error(header.cmd_id, 0xFFu, 38u);
            break;
        }
        if (!RGB_preview(payload[0], payload[1], payload[2], payload[3]))
        {
            send_error(header.cmd_id, 0xFFu, 39u);
            break;
        }
        send_ack(header.cmd_id, 1u, 0u);
        break;
    case MSG_RUNTIME_SYNC:
        {

            static const uint16_t keys[6] = {
                0x0002u, 0x0003u, 0x0004u, 0x0005u, 0x0006u, 0x0009u
            };
            float values[14];
            bool ok = true;
            for (uint8_t index = 0u; index < 14u; index++)
            {
                memcpy(&values[index], payload + (uint16_t)index * 4u,
                       sizeof(values[index]));
                if (!runtime_value_valid(index, values[index]))
                {
                    ok = false;
                    break;
                }
            }
            BmcuMotionNvm previous = g_bmcu_nvm;
            float previous_retract[4];
            float previous_autoload[4];
            for (uint8_t channel = 0u; channel < 4u; channel++)
            {
                previous_retract[channel] = bmcu_config_retract_len(channel);
                previous_autoload[channel] = bmcu_config_autoload_len(channel);
            }
            if (ok)
            {
                for (uint8_t index = 0u; index < 6u; index++)
                {
                    if (!bmcu_config_set(keys[index], values[index]))
                    {
                        ok = false;
                        break;
                    }
                }
            }
            if (ok)
                ok = bmcu_runtime_set_retract_lengths(values + 6u);
            if (ok)
                ok = bmcu_runtime_set_autoload_lengths(values + 10u);
            if (!ok)
            {
                g_bmcu_nvm = previous;
                (void)bmcu_runtime_set_retract_lengths(previous_retract);
                (void)bmcu_runtime_set_autoload_lengths(previous_autoload);
                send_error(header.cmd_id, 0xFFu, 35u);
                break;
            }
            if (payload[62] != 1u || payload[59] > 1u ||
                    payload[60] > 1u || payload[61] > 1u)
            {
                g_bmcu_nvm = previous;
                (void)bmcu_runtime_set_retract_lengths(previous_retract);
                (void)bmcu_runtime_set_autoload_lengths(previous_autoload);
                bmcu_policy_disable();
                send_error(header.cmd_id, 0xFFu, 36u);
                break;
            }
            system_led_set_normal_rgb(payload[56], payload[57], payload[58]);
            bmcu_policy_set(payload[59] != 0u, payload[60] != 0u,
                            payload[61] != 0u);
            Motion_control_set_host_motion_enabled(true);
            send_ack(header.cmd_id, 1u, 0u);
        }
        break;
    case MSG_UPDATE_PREPARE:
        if (g_update_mode)
        {
            g_update_deadline_ticks = deadline_ticks_after_ms(120000u);
            send_ack(header.cmd_id, 1u, 0u);
            break;
        }
        if (any_operation_active() || any_motion_active())
        {
            send_error(header.cmd_id, 0xFFu, 91u);
            break;
        }
        g_update_policy_standalone = bmcu_policy_standalone() ? 1u : 0u;
        g_update_policy_assist = bmcu_policy_autonomous_assist() ? 1u : 0u;
        g_update_policy_unload = bmcu_policy_autonomous_unload() ? 1u : 0u;
        g_update_policy_valid = 1u;
        Motion_control_set_host_motion_enabled(false);
        bmcu_policy_disable();
        stop_all_motion();
        if (!ams_state_save_run())
        {
            g_update_policy_valid = 0u;
            send_error(header.cmd_id, 0xFFu, 92u);
            break;
        }
        update_mode_set(true);
        send_ack(header.cmd_id, 1u, 0u);
        send_status(0u);
        break;
    case MSG_NVM_READ:
        if (!g_update_mode)
        {
            send_error(header.cmd_id, 0xFFu, 93u);
            break;
        }
        {
            const uint16_t offset = (uint16_t)(payload[0] | ((uint16_t)payload[1] << 8));
            const uint16_t amount = (uint16_t)(payload[2] | ((uint16_t)payload[3] << 8));
            if (!amount || amount > 224u ||
                (uint32_t)offset + (uint32_t)amount > FLASH_NVM_TOTAL_SIZE)
            {
                send_error(header.cmd_id, 0xFFu, 94u);
                break;
            }
            uint8_t response[232];
            response[0] = (uint8_t)offset;
            response[1] = (uint8_t)(offset >> 8);
            response[2] = (uint8_t)amount;
            response[3] = (uint8_t)(amount >> 8);
            memcpy(response + 4u, &g_update_nvm_crc, sizeof(g_update_nvm_crc));
            if (!Flash_NVM_read_raw(offset, response + 8u, amount))
            {
                send_error(header.cmd_id, 0xFFu, 95u);
                break;
            }
            send_packet(MSG_NVM_DATA, header.cmd_id, response, (uint16_t)(8u + amount));
        }
        break;
    case MSG_UPDATE_CANCEL:
        update_mode_set(false);
        if (g_update_policy_valid)
        {
            bmcu_policy_set(g_update_policy_standalone != 0u,
                            g_update_policy_assist != 0u,
                            g_update_policy_unload != 0u);
            Motion_control_set_host_motion_enabled(true);
        }
        g_update_policy_valid = 0u;
        send_ack(header.cmd_id, 1u, 0u);
        break;
    case MSG_STOP_ALL:
        if (g_op.active) finish_distance_op(OP_STATE_ABORTED, OP_REASON_ABORTED);
        if (MC_PULL_calibration_auto_active())
            (void)MC_PULL_calibration_auto_abort();
        stop_all_motion();
        send_ack(header.cmd_id, 1, 0);
        g_event_counter++;
        send_status(0);
        break;
    case MSG_RESET_ERROR:
        if (g_op.active) finish_distance_op(OP_STATE_ABORTED, OP_REASON_ABORTED);
        if (MC_PULL_calibration_auto_active())
            (void)MC_PULL_calibration_auto_abort();
        stop_all_motion();
        Motion_control_clear_faults();
        send_ack(header.cmd_id, 1, 0);
        g_event_counter++;
        send_status(0);
        break;
    default:
        send_error(header.cmd_id, 0xFF, 0xFFFF);
        break;
    }
}

static void process_rx(void)
{
    uint8_t byte = 0u;
    uint16_t bytes = 0u;
    uint8_t frames = 0u;
    while (bytes < 256u && frames < 4u && bmcu_uart_critical_free() >= 4u &&
           bmcu_uart_read_byte(&byte))
    {
        bytes++;
        if (byte == 0u)
        {
            if (rx_discarding)
            {
                rx_discarding = 0u;
                rx_cobs_len = 0u;
                continue;
            }
            if (rx_cobs_len)
            {
                const int decoded = bmcu_cobs_decode(rx_cobs, rx_cobs_len, rx_pkt_buf);
                rx_cobs_len = 0u;
                if (decoded > 0)
                {
                    handle_packet(rx_pkt_buf, (uint16_t)decoded);
                    frames++;
                }
            }
            continue;
        }
        if (rx_discarding) continue;
        if (rx_cobs_len < sizeof(rx_cobs)) rx_cobs[rx_cobs_len++] = byte;
        else
        {
            rx_cobs_len = 0u;
            rx_discarding = 1u;
        }
    }
}

static void process_auto_calibration(void)
{
    if (g_update_mode) return;
    MC_PULL_calibration_auto_run();

    uint32_t op_id = 0u, duration_ms = 0u;
    uint8_t state = 0u, reason = 0u, channel = 0xFFu;
    if (!MC_PULL_calibration_auto_take_result(
            &op_id, &state, &reason, &channel, &duration_ms))
        return;

    uint8_t wire_state = OP_STATE_FAILED;
    if (state == BMCU_AUTO_CAL_STATE_DONE) wire_state = OP_STATE_DONE;
    else if (state == BMCU_AUTO_CAL_STATE_ABORTED) wire_state = OP_STATE_ABORTED;
    send_op_result(op_id, OP_BUFFER_CALIBRATION, channel, wire_state, reason,
                   0.0f, 0.0f, duration_ms);
    g_last_op_state = wire_state;
    g_last_op_reason = reason;
    g_event_counter++;
    send_status(0u);
}

static void process_events(void)
{
    if (!host_online) return;
    const uint32_t now = now_ticks();

    if ((uint32_t)(now - last_rx_tick) > ticks_ms(15000u))
    {
        if (g_op.active) finish_distance_op(OP_STATE_ABORTED, OP_REASON_ABORTED);
        if (MC_PULL_calibration_auto_active())
            (void)MC_PULL_calibration_auto_abort();
        if (any_motion_active()) stop_all_motion();
        Motion_control_set_host_motion_enabled(false);
        bmcu_policy_disable();
        g_update_policy_valid = 0u;
        host_online = 0;
        host_session_ready = 0u;
        return;
    }

    bool status_changed = false;
    for (uint8_t ch = 0; ch < 4; ch++)
    {
        const uint8_t motion = (uint8_t)ams[0].filament[ch].motion;
        if (motion != last_motion[ch])
        {
            last_motion[ch] = motion;
            g_event_counter++;
            status_changed = true;
        }
        const uint8_t present = Motion_control_filament_present(ch);
        if (present != last_present[ch])
        {
            last_present[ch] = present;
            g_event_counter++;
            status_changed = true;
        }
    }

    const uint8_t route_snapshot = (uint8_t)(ams_state_get_loaded_mask() |
                                             (ams_state_get_uncertain_mask() << 4));
    if (route_snapshot != last_route_snapshot)
    {
        last_route_snapshot = route_snapshot;
        g_event_counter++;
        status_changed = true;
    }
    if (status_changed)
    {
        last_status_tick = now;
        send_status(0u);
    }

    const uint32_t period = any_motion_active() || any_operation_active() ? ticks_ms(500u) : ticks_ms(30000u);
    if ((uint32_t)(now - last_status_tick) >= period)
    {
        last_status_tick = now;
        send_status(0);
    }
}

void bmcu_protocol_init(void)
{
    rx_cobs_len = 0;
    rx_discarding = 0u;
    seq_tx = 1;
    host_online = 0;
    host_session_ready = 0u;
    seq_rx_last = 0u;
    last_rx_tick = now_ticks();
    last_status_tick = now_ticks();
    last_route_snapshot = (uint8_t)(ams_state_get_loaded_mask() |
                                            (ams_state_get_uncertain_mask() << 4));
    g_event_counter = 1u;
    memset(&g_op, 0, sizeof(g_op));
    memset(g_op_history, 0, sizeof(g_op_history));
    g_op_history_head = 0u;
    g_last_op_state = OP_STATE_IDLE;
    g_last_op_reason = OP_REASON_NONE;
    g_update_mode = 0u;
    g_update_deadline_ticks = 0ull;
    g_update_nvm_crc = 0u;
    g_update_policy_valid = 0u;
    g_update_policy_standalone = 0u;
    g_update_policy_assist = 0u;
    g_update_policy_unload = 0u;
    Motion_control_set_host_motion_enabled(false);
    bmcu_policy_disable();
    system_led_set_update_mode(false);
    for (uint8_t ch = 0; ch < 4; ch++)
    {
        last_motion[ch] = (uint8_t)ams[0].filament[ch].motion;
        last_present[ch] = Motion_control_filament_present(ch);
    }

    g_session_id = 0u;
    rotate_session(now_ticks());
}

void bmcu_protocol_send_hello(void)
{
    send_hello_ack(0);
    send_caps(0);
    send_status(0);
}

void bmcu_protocol_run(void)
{
    bmcu_uart_poll();
    process_rx();
    if (g_update_mode && time_ticks64() >= g_update_deadline_ticks)
    {
        update_mode_set(false);
        g_update_policy_valid = 0u;
        Motion_control_set_host_motion_enabled(false);
        bmcu_policy_disable();
    }
    if (!g_update_mode)
    {
        process_distance_op();
        process_auto_calibration();
    }
    process_events();
    bmcu_uart_poll();
    const bool setup_required = (
        Flash_saves_faulted() || Flash_saves_bad_page_mask() != 0u ||
        MC_PULL_calibration_valid_mask() != 0x0Fu);
    system_led_run(host_online != 0u, MC_PULL_calibration_active(),
                   setup_required);
}

int bmcu_protocol_error_state(void)
{
    return host_online ? 0 : -1;
}

bool bmcu_protocol_update_mode(void)
{
    return g_update_mode != 0u;
}
