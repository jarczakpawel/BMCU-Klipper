#include "MC_PULL_calibration.h"
#include "Motion_control.h"
#include "ADC_DMA.h"
#include "Flash_saves.h"
#include "app_api.h"
#include "hal/time_hw.h"
#include <math.h>
#include <string.h>

extern void RGB_update();

static uint8_t g_valid_mask = 0u;
static uint8_t g_capture_mask[4] = {0u, 0u, 0u, 0u};
static float g_capture_raw[4][3] = {{0}};

static const uint8_t AUTO_REASON_NONE = 0u;
static const uint8_t AUTO_REASON_TIMEOUT = 2u;
static const uint8_t AUTO_REASON_ABORTED = 6u;
static const uint8_t AUTO_REASON_BUSY = 7u;
static const uint8_t AUTO_REASON_CAL_RANGE = 12u;
static const uint8_t AUTO_REASON_NVM = 13u;
static const uint8_t AUTO_REASON_SENSOR = 14u;
static const uint8_t AUTO_REASON_ENCODER_IO = 4u;

static const float CAL_PRESS_DELTA_V = 0.100f;
static const float CAL_CENTER_EPS_V = 0.025f;
static const float CAL_MIN_HALF_RANGE_V = 0.050f;
static const float CAL_MIN_TOTAL_RANGE_V = 0.120f;
static const uint32_t CAL_STABLE_MS = 700u;
static const uint32_t CAL_STAGE_TIMEOUT_MS = 30000u;
static const uint32_t CAL_SAMPLE_MS = 15u;
static const uint16_t CAL_BASELINE_SAMPLES = 60u;
static const uint32_t CAL_RELEASE_STABLE_MS = 1000u;
static const uint32_t CAL_POST_GUARD_STABLE_MS = 1200u;
static const float CAL_RELEASE_CENTER_EPS_V = 0.080f;

struct AutoCalibration
{
    uint8_t active;
    uint8_t selected_mask;
    uint8_t done_mask;
    uint8_t channel;
    uint8_t stage;
    uint8_t state;
    uint8_t reason;
    uint8_t result_pending;
    uint32_t op_id;
    uint64_t started_ticks;
    uint32_t stage_started_ticks;
    uint32_t last_sample_ticks;
    uint32_t stable_started_ticks;
    uint16_t baseline_samples;
    float baseline_sum[4];
    float baseline_key_sum[4];
    uint8_t staged_key_none_cv[4];
    int8_t staged_motor_direction[4];
    float staged_offset[4];
    float staged_min[4];
    float staged_max[4];
    int8_t staged_polarity[4];
    uint8_t staged_valid_mask;
    float first_raw_min;
    float first_raw_max;
    float first_normalized;
    float second_best_raw;
    int8_t current_polarity;
    uint32_t result_duration_ms;
    uint8_t wait_for_release;
    uint8_t trigger_channel;
    uint32_t release_stable_started_ticks;
};

static AutoCalibration g_auto;
static uint8_t g_motion_guard_mask = 0u;
static uint32_t g_motion_guard_centered_since = 0u;

static inline float adc_pull_raw_ch(uint8_t ch, const float *v8)
{
    switch (ch)
    {
    case 0: return v8[6];
    case 1: return v8[4];
    case 2: return v8[2];
    default:return v8[0];
    }
}

static inline float adc_key_raw_ch(uint8_t ch, const float *v8)
{
    switch (ch)
    {
    case 0: return v8[7];
    case 1: return v8[5];
    case 2: return v8[3];
    default:return v8[1];
    }
}

static inline uint8_t detector_none_threshold_cv(float idle_voltage)
{
    if (!isfinite(idle_voltage) || idle_voltage < 0.0f || idle_voltage >= 1.40f)
        return 0u;
    float scaled = idle_voltage * 100.0f - 0.0001f;
    int rounded_up = (int)scaled;
    if ((float)rounded_up < scaled) rounded_up++;
    rounded_up += 10;
    if (rounded_up < 60) rounded_up = 60;
    if (rounded_up > 139) rounded_up = 139;
    return (uint8_t)rounded_up;
}

static inline float apply_polarity(float v, int8_t polarity)
{
    return (polarity < 0) ? (3.30f - v) : v;
}

static inline bool finite_voltage(float v)
{
    return isfinite(v) && v >= 0.05f && v <= 3.25f;
}

static inline uint32_t ticks_per_ms()
{
    uint32_t value = time_hw_ticks_per_ms();
    return value ? value : 1u;
}

static inline uint32_t elapsed_ms32(uint32_t started, uint32_t now)
{
    return (uint32_t)(now - started) / ticks_per_ms();
}

static inline uint32_t elapsed_ms64(uint64_t started, uint64_t now)
{
    const uint64_t value = (now - started) / (uint64_t)ticks_per_ms();
    return value > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)value;
}

static void show_channel(uint8_t ch, uint8_t r, uint8_t g, uint8_t b)
{
    MC_PULL_ONLINE_RGB_set(ch, r, g, b);
    RGB_update();
}

static void show_selected(uint8_t mask, uint8_t r, uint8_t g, uint8_t b)
{
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (mask & (uint8_t)(1u << ch)) MC_PULL_ONLINE_RGB_set(ch, r, g, b);
    RGB_update();
}

static void auto_led(uint32_t now)
{
    if (!g_auto.active) return;
    const bool on = ((elapsed_ms32(g_auto.stage_started_ticks, now) / 220u) & 1u) == 0u;
    if (g_auto.stage == BMCU_AUTO_CAL_BASELINE)
    {
        show_selected(g_auto.selected_mask, on ? 0x0Cu : 0u,
                      on ? 0x0Cu : 0u, 0u);
        return;
    }
    if (g_auto.channel >= 4u) return;
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        if (g_auto.done_mask & (uint8_t)(1u << ch))
            MC_PULL_ONLINE_RGB_set(ch, 0u, 0x0Cu, 0u);
        else if (ch != g_auto.channel)
            MC_PULL_ONLINE_RGB_set(ch, 0u, 0u, 0u);
    }
    if (g_auto.stage == BMCU_AUTO_CAL_FIRST_MOVE ||
        g_auto.stage == BMCU_AUTO_CAL_FIRST_RELEASE)
        MC_PULL_ONLINE_RGB_set(g_auto.channel, 0u, 0u, on ? 0x16u : 0u);
    else if (g_auto.stage == BMCU_AUTO_CAL_SECOND_MOVE ||
             g_auto.stage == BMCU_AUTO_CAL_SECOND_RELEASE)
        MC_PULL_ONLINE_RGB_set(g_auto.channel, on ? 0x16u : 0u, 0u, 0u);
    else
        MC_PULL_ONLINE_RGB_set(g_auto.channel, 0x0Cu, 0x0Cu, 0u);
    RGB_update();
}

float MC_PULL_calibration_raw(uint8_t ch)
{
    if (ch >= 4u) return 0.0f;
    return adc_pull_raw_ch(ch, ADC_DMA_get_value());
}

uint8_t MC_PULL_calibration_valid_mask()
{
    return (uint8_t)(g_valid_mask & 0x0Fu);
}

bool MC_PULL_calibration_active()
{
    return g_auto.active || (g_capture_mask[0] | g_capture_mask[1] |
            g_capture_mask[2] | g_capture_mask[3]) != 0u;
}

bool MC_PULL_calibration_is_valid(uint8_t ch)
{
    return ch < 4u && ((g_valid_mask & (uint8_t)(1u << ch)) != 0u);
}

uint8_t MC_PULL_calibration_capture_mask(uint8_t ch)
{
    return ch < 4u ? (uint8_t)(g_capture_mask[ch] & 0x07u) : 0u;
}

bool MC_PULL_calibration_clear()
{
    if (g_auto.active) return false;
    const bool persisted = Flash_MC_PULL_cal_clear();
    if (!persisted) return false;
    g_valid_mask = 0u;
    memset(g_capture_mask, 0, sizeof(g_capture_mask));
    return true;
}

void MC_PULL_calibration_boot()
{
    memset(&g_auto, 0, sizeof(g_auto));
    g_auto.channel = 0xFFu;
    for (uint8_t i = 0u; i < 8u; i++)
    {
        ADC_DMA_poll();
        delay(10);
    }

    MC_PULL_detect_channels_inserted();

    float offs[4] = {0};
    float vmin[4] = {0};
    float vmax[4] = {0};
    int8_t polarity[4] = {1, 1, 1, 1};
    uint8_t valid = 0u;

    if (Flash_MC_PULL_cal_read(offs, vmin, vmax, polarity, &valid))
    {
        for (uint8_t ch = 0u; ch < 4u; ch++)
        {
            MC_PULL_V_OFFSET[ch] = offs[ch];
            MC_PULL_V_MIN[ch] = vmin[ch];
            MC_PULL_V_MAX[ch] = vmax[ch];
            MC_PULL_POLARITY[ch] = polarity[ch] < 0 ? -1 : 1;
        }
        g_valid_mask = (uint8_t)(valid & 0x0Fu);
        return;
    }

    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        const float raw = MC_PULL_calibration_raw(ch);
        MC_PULL_V_OFFSET[ch] = finite_voltage(raw) ? (1.65f - raw) : 0.0f;
        MC_PULL_V_MIN[ch] = 1.50f;
        MC_PULL_V_MAX[ch] = 1.80f;
        MC_PULL_POLARITY[ch] = 1;
    }
    g_valid_mask = 0u;
}

bool MC_PULL_calibration_capture(uint8_t ch, uint8_t point, float* raw_out)
{
    if (g_auto.active || ch >= 4u || point > (uint8_t)BMCU_BUFFER_CAL_MAX)
        return false;

    float sum = 0.0f;
    float min_v = 10.0f;
    float max_v = -10.0f;
    const uint8_t samples = 24u;
    for (uint8_t i = 0u; i < samples; i++)
    {
        ADC_DMA_poll();
        const float raw = MC_PULL_calibration_raw(ch);
        if (!finite_voltage(raw)) return false;
        sum += raw;
        if (raw < min_v) min_v = raw;
        if (raw > max_v) max_v = raw;
        delay(5);
    }

    const float average = sum / (float)samples;
    if ((max_v - min_v) > 0.040f) return false;

    g_capture_raw[ch][point] = average;
    g_capture_mask[ch] |= (uint8_t)(1u << point);
    if (raw_out) *raw_out = average;

    if (point == (uint8_t)BMCU_BUFFER_CAL_MIN) show_channel(ch, 0x00, 0x00, 0x18);
    else if (point == (uint8_t)BMCU_BUFFER_CAL_NEUTRAL) show_channel(ch, 0x18, 0x18, 0x00);
    else show_channel(ch, 0x18, 0x00, 0x00);
    return true;
}

bool MC_PULL_calibration_commit(uint8_t ch)
{
    if (g_auto.active || ch >= 4u || (g_capture_mask[ch] & 0x07u) != 0x07u)
        return false;

    const float raw_min = g_capture_raw[ch][BMCU_BUFFER_CAL_MIN];
    const float raw_neutral = g_capture_raw[ch][BMCU_BUFFER_CAL_NEUTRAL];
    const float raw_max = g_capture_raw[ch][BMCU_BUFFER_CAL_MAX];
    if (!finite_voltage(raw_min) || !finite_voltage(raw_neutral) || !finite_voltage(raw_max))
        return false;

    const float offset = 1.65f - raw_neutral;
    const float adjusted_min = raw_min + offset;
    const float adjusted_max = raw_max + offset;
    const int8_t polarity = (adjusted_min < adjusted_max) ? 1 : -1;
    const float mapped_min = apply_polarity(adjusted_min, polarity);
    const float mapped_neutral = apply_polarity(1.65f, polarity);
    const float mapped_max = apply_polarity(adjusted_max, polarity);

    if (!(mapped_min < mapped_neutral && mapped_neutral < mapped_max)) return false;
    if ((mapped_neutral - mapped_min) < CAL_MIN_HALF_RANGE_V) return false;
    if ((mapped_max - mapped_neutral) < CAL_MIN_HALF_RANGE_V) return false;
    if ((mapped_max - mapped_min) < CAL_MIN_TOTAL_RANGE_V) return false;

    float staged_offset[4], staged_min[4], staged_max[4];
    int8_t staged_polarity[4];
    for (uint8_t i = 0u; i < 4u; i++)
    {
        staged_offset[i] = MC_PULL_V_OFFSET[i];
        staged_min[i] = MC_PULL_V_MIN[i];
        staged_max[i] = MC_PULL_V_MAX[i];
        staged_polarity[i] = MC_PULL_POLARITY[i];
    }
    staged_offset[ch] = offset;
    staged_min[ch] = mapped_min;
    staged_max[ch] = mapped_max;
    staged_polarity[ch] = polarity;
    const uint8_t new_valid = (uint8_t)(g_valid_mask | (uint8_t)(1u << ch));

    if (!Flash_MC_PULL_cal_write_all(staged_offset, staged_min, staged_max,
                                      staged_polarity, new_valid))
        return false;

    MC_PULL_V_OFFSET[ch] = offset;
    MC_PULL_V_MIN[ch] = mapped_min;
    MC_PULL_V_MAX[ch] = mapped_max;
    MC_PULL_POLARITY[ch] = polarity;
    g_valid_mask = new_valid;
    g_capture_mask[ch] = 0u;
    show_channel(ch, 0x00, 0x18, 0x00);
    return true;
}

static uint8_t first_selected(uint8_t mask)
{
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (mask & (uint8_t)(1u << ch)) return ch;
    return 0xFFu;
}

static uint8_t next_selected(uint8_t mask, uint8_t current)
{
    for (uint8_t ch = (uint8_t)(current + 1u); ch < 4u; ch++)
        if (mask & (uint8_t)(1u << ch)) return ch;
    return 0xFFu;
}

static uint8_t bit_count4(uint8_t mask)
{
    uint8_t count = 0u;
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (mask & (uint8_t)(1u << ch)) count++;
    return count;
}

static void auto_finish(uint8_t state, uint8_t reason)
{
    const uint8_t active_channel = g_auto.channel;

    g_motion_guard_mask = (uint8_t)(g_auto.selected_mask & 0x0Fu);
    g_motion_guard_centered_since = 0u;
    g_auto.active = 0u;
    g_auto.stage = BMCU_AUTO_CAL_IDLE;
    g_auto.state = state;
    g_auto.reason = reason;
    g_auto.result_duration_ms = elapsed_ms64(g_auto.started_ticks, time_ticks64());
    g_auto.result_pending = 1u;
    if (state == BMCU_AUTO_CAL_STATE_DONE)
        show_selected(g_auto.selected_mask, 0u, 0x16u, 0u);
    else if (active_channel < 4u)
        show_channel(active_channel, 0x18u, 0u, 0u);
}

static void auto_fail(uint8_t reason)
{
    auto_finish(BMCU_AUTO_CAL_STATE_FAILED, reason);
}

static void auto_begin_channel(uint8_t ch, uint32_t now)
{
    g_auto.channel = ch;
    g_auto.stage = BMCU_AUTO_CAL_FIRST_MOVE;
    g_auto.stage_started_ticks = now;
    g_auto.stable_started_ticks = 0u;
    g_auto.first_raw_min = 1.65f;
    g_auto.first_raw_max = 1.65f;
    g_auto.first_normalized = 1.65f;
    g_auto.second_best_raw = 1.65f;
    g_auto.current_polarity = 1;
}

bool MC_PULL_calibration_auto_start(uint8_t selected_mask, uint32_t op_id,
                                    bool wait_for_release,
                                    uint8_t trigger_channel)
{
    selected_mask &= 0x0Fu;
    uint8_t connected_mask = 0u;
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (Motion_control_channel_connected(ch))
            connected_mask |= (uint8_t)(1u << ch);
    selected_mask &= connected_mask;
    if (!selected_mask || !op_id || g_auto.active) return false;
    if (wait_for_release && (trigger_channel >= 4u ||
        !(selected_mask & (uint8_t)(1u << trigger_channel)))) return false;

    memset(&g_auto, 0, sizeof(g_auto));
    g_motion_guard_mask = 0u;
    g_motion_guard_centered_since = 0u;
    g_auto.active = 1u;
    g_auto.selected_mask = selected_mask;
    g_auto.channel = 0xFFu;
    g_auto.stage = BMCU_AUTO_CAL_BASELINE;
    g_auto.state = BMCU_AUTO_CAL_STATE_RUNNING;
    g_auto.reason = AUTO_REASON_NONE;
    g_auto.op_id = op_id;
    g_auto.started_ticks = time_ticks64();
    g_auto.stage_started_ticks = time_ticks32();
    g_auto.last_sample_ticks = g_auto.stage_started_ticks;
    g_auto.staged_valid_mask = g_valid_mask;
    g_auto.wait_for_release = wait_for_release ? 1u : 0u;
    g_auto.trigger_channel = trigger_channel;

    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        g_auto.staged_offset[ch] = MC_PULL_V_OFFSET[ch];
        g_auto.staged_min[ch] = MC_PULL_V_MIN[ch];
        g_auto.staged_max[ch] = MC_PULL_V_MAX[ch];
        g_auto.staged_polarity[ch] = MC_PULL_POLARITY[ch];
        int threshold_cv = (int)ceilf(MC_DM_KEY_NONE_THRESH[ch] * 100.0f - 0.0001f);
        if (threshold_cv < 60) threshold_cv = 60;
        if (threshold_cv > 139) threshold_cv = 139;
        g_auto.staged_key_none_cv[ch] = (uint8_t)threshold_cv;
        g_auto.staged_motor_direction[ch] = 0;
        if (selected_mask & (uint8_t)(1u << ch)) g_capture_mask[ch] = 0u;
    }
    show_selected(selected_mask, 0x0Cu, 0x0Cu, 0u);
    return true;
}

bool MC_PULL_calibration_auto_abort()
{
    if (!g_auto.active) return false;
    auto_finish(BMCU_AUTO_CAL_STATE_ABORTED, AUTO_REASON_ABORTED);
    return true;
}

static bool sample_due(uint32_t now)
{
    if (elapsed_ms32(g_auto.last_sample_ticks, now) < CAL_SAMPLE_MS) return false;
    g_auto.last_sample_ticks = now;
    ADC_DMA_poll();
    return true;
}

static void run_baseline(uint32_t now)
{
    if (g_auto.wait_for_release)
    {
        ADC_DMA_poll();
        const uint8_t ch = g_auto.trigger_channel;
        const float raw = MC_PULL_calibration_raw(ch);
        if (!finite_voltage(raw)) { auto_fail(AUTO_REASON_SENSOR); return; }
        const float centered_raw = raw + g_auto.staged_offset[ch];
        if (fabsf(centered_raw - 1.65f) > CAL_RELEASE_CENTER_EPS_V)
        {
            g_auto.release_stable_started_ticks = 0u;
            return;
        }
        if (!g_auto.release_stable_started_ticks)
        {
            g_auto.release_stable_started_ticks = now;
            return;
        }
        if (elapsed_ms32(g_auto.release_stable_started_ticks, now) <
                CAL_RELEASE_STABLE_MS) return;

        g_auto.wait_for_release = 0u;
        g_auto.release_stable_started_ticks = 0u;
        g_auto.baseline_samples = 0u;
        memset(g_auto.baseline_sum, 0, sizeof(g_auto.baseline_sum));
        memset(g_auto.baseline_key_sum, 0, sizeof(g_auto.baseline_key_sum));
        g_auto.stage_started_ticks = now;
        g_auto.last_sample_ticks = now;
        return;
    }

    if (!sample_due(now)) return;
    const float *v8 = ADC_DMA_get_value();
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        if (!(g_auto.selected_mask & (uint8_t)(1u << ch))) continue;
        const float raw = adc_pull_raw_ch(ch, v8);
        const float key = adc_key_raw_ch(ch, v8);
        if (!finite_voltage(raw) || !isfinite(key) || key < 0.0f || key > 3.30f)
        {
            auto_fail(AUTO_REASON_SENSOR);
            return;
        }
        g_auto.baseline_sum[ch] += raw;
        g_auto.baseline_key_sum[ch] += key;
    }
    g_auto.baseline_samples++;
    if (g_auto.baseline_samples < CAL_BASELINE_SAMPLES) return;

    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        if (!(g_auto.selected_mask & (uint8_t)(1u << ch))) continue;
        const float neutral = g_auto.baseline_sum[ch] / (float)CAL_BASELINE_SAMPLES;
        const float key_idle = g_auto.baseline_key_sum[ch] / (float)CAL_BASELINE_SAMPLES;
        const uint8_t threshold_cv = detector_none_threshold_cv(key_idle);
        if (!finite_voltage(neutral) || !threshold_cv)
        {
            auto_fail(AUTO_REASON_SENSOR);
            return;
        }
        g_auto.staged_offset[ch] = 1.65f - neutral;
        g_auto.staged_key_none_cv[ch] = threshold_cv;
    }
    const uint8_t first = first_selected(g_auto.selected_mask);
    if (first >= 4u) { auto_fail(AUTO_REASON_SENSOR); return; }
    auto_begin_channel(first, now);
}

static float adjusted_raw(uint8_t ch)
{
    const float raw = MC_PULL_calibration_raw(ch);
    if (!finite_voltage(raw)) return NAN;
    return raw + g_auto.staged_offset[ch];
}

static bool centered(float value)
{
    return fabsf(value - 1.65f) <= CAL_CENTER_EPS_V;
}

static bool stable_center(uint32_t now, float value)
{
    if (!centered(value))
    {
        g_auto.stable_started_ticks = 0u;
        return false;
    }
    if (!g_auto.stable_started_ticks) g_auto.stable_started_ticks = now;
    return elapsed_ms32(g_auto.stable_started_ticks, now) >= CAL_STABLE_MS;
}

static void complete_current_channel(uint32_t now, float second_normalized)
{
    const uint8_t ch = g_auto.channel;
    const float neutral = 1.65f;
    const float minimum = g_auto.first_normalized;
    const float maximum = second_normalized;
    if (!(minimum < neutral && neutral < maximum) ||
        (neutral - minimum) < CAL_MIN_HALF_RANGE_V ||
        (maximum - neutral) < CAL_MIN_HALF_RANGE_V ||
        (maximum - minimum) < CAL_MIN_TOTAL_RANGE_V)
    {
        auto_fail(AUTO_REASON_CAL_RANGE);
        return;
    }

    g_auto.staged_min[ch] = minimum;
    g_auto.staged_max[ch] = maximum;
    g_auto.staged_polarity[ch] = g_auto.current_polarity < 0 ? -1 : 1;
    g_auto.staged_valid_mask |= (uint8_t)(1u << ch);
    g_auto.done_mask |= (uint8_t)(1u << ch);
    show_channel(ch, 0u, 0x16u, 0u);

    const uint8_t next = next_selected(g_auto.selected_mask, ch);
    if (next < 4u)
    {
        auto_begin_channel(next, now);
        return;
    }

    g_auto.channel = 0xFFu;
    g_auto.stage = BMCU_AUTO_CAL_SAVING;
    g_auto.stage_started_ticks = now;
}

static void run_channel(uint32_t now)
{
    if (!sample_due(now)) return;
    if (elapsed_ms32(g_auto.stage_started_ticks, now) >= CAL_STAGE_TIMEOUT_MS)
    {
        auto_fail(AUTO_REASON_TIMEOUT);
        return;
    }

    const float value = adjusted_raw(g_auto.channel);
    if (!isfinite(value)) { auto_fail(AUTO_REASON_SENSOR); return; }

    switch (g_auto.stage)
    {
    case BMCU_AUTO_CAL_FIRST_MOVE:
        if (fabsf(value - 1.65f) >= CAL_PRESS_DELTA_V)
        {
            g_auto.first_raw_min = value;
            g_auto.first_raw_max = value;
            g_auto.stable_started_ticks = 0u;
            g_auto.stage = BMCU_AUTO_CAL_FIRST_RELEASE;
            g_auto.stage_started_ticks = now;
        }
        break;

    case BMCU_AUTO_CAL_FIRST_RELEASE:
        if (value < g_auto.first_raw_min) g_auto.first_raw_min = value;
        if (value > g_auto.first_raw_max) g_auto.first_raw_max = value;
        if (stable_center(now, value))
        {
            const float down = 1.65f - g_auto.first_raw_min;
            const float up = g_auto.first_raw_max - 1.65f;
            if (up > down)
            {
                g_auto.current_polarity = -1;
                g_auto.first_normalized = apply_polarity(g_auto.first_raw_max, -1);
            }
            else
            {
                g_auto.current_polarity = 1;
                g_auto.first_normalized = g_auto.first_raw_min;
            }
            g_auto.second_best_raw = 1.65f;
            g_auto.stable_started_ticks = 0u;
            g_auto.stage = BMCU_AUTO_CAL_SECOND_MOVE;
            g_auto.stage_started_ticks = now;
        }
        break;

    case BMCU_AUTO_CAL_SECOND_MOVE:
        if ((g_auto.current_polarity < 0 && value <= 1.65f - CAL_PRESS_DELTA_V) ||
            (g_auto.current_polarity > 0 && value >= 1.65f + CAL_PRESS_DELTA_V))
        {
            g_auto.second_best_raw = value;
            g_auto.stable_started_ticks = 0u;
            g_auto.stage = BMCU_AUTO_CAL_SECOND_RELEASE;
            g_auto.stage_started_ticks = now;
        }
        break;

    case BMCU_AUTO_CAL_SECOND_RELEASE:
        if (g_auto.current_polarity < 0)
        {
            if (value < g_auto.second_best_raw) g_auto.second_best_raw = value;
        }
        else if (value > g_auto.second_best_raw)
            g_auto.second_best_raw = value;
        if (stable_center(now, value))
            complete_current_channel(now, apply_polarity(
                g_auto.second_best_raw, g_auto.current_polarity));
        break;

    default:
        auto_fail(AUTO_REASON_BUSY);
        break;
    }
}

static void save_transaction()
{
    Motion_control_prepare_calibration();
    if (!Motion_control_calibrate_motor_encoder(
            g_auto.selected_mask, g_auto.staged_motor_direction))
    {
        auto_fail(AUTO_REASON_ENCODER_IO);
        return;
    }

    if (!Flash_MC_PULL_cal_write_all(g_auto.staged_offset, g_auto.staged_min,
                                      g_auto.staged_max,
                                      g_auto.staged_polarity,
                                      g_auto.staged_valid_mask))
    {
        auto_fail(AUTO_REASON_NVM);
        return;
    }

    if (!Motion_control_commit_hardware_calibration(
            g_auto.selected_mask, g_auto.staged_key_none_cv,
            g_auto.staged_motor_direction))
    {

        (void)Flash_MC_PULL_cal_write_all(
            MC_PULL_V_OFFSET, MC_PULL_V_MIN, MC_PULL_V_MAX,
            MC_PULL_POLARITY, g_valid_mask);
        auto_fail(AUTO_REASON_NVM);
        return;
    }

    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        MC_PULL_V_OFFSET[ch] = g_auto.staged_offset[ch];
        MC_PULL_V_MIN[ch] = g_auto.staged_min[ch];
        MC_PULL_V_MAX[ch] = g_auto.staged_max[ch];
        MC_PULL_POLARITY[ch] = g_auto.staged_polarity[ch] < 0 ? -1 : 1;
        if (g_auto.selected_mask & (uint8_t)(1u << ch)) g_capture_mask[ch] = 0u;
    }
    g_valid_mask = (uint8_t)(g_auto.staged_valid_mask & 0x0Fu);
    auto_finish(BMCU_AUTO_CAL_STATE_DONE, AUTO_REASON_NONE);
}

void MC_PULL_calibration_auto_run()
{
    if (!g_auto.active) return;
    const uint32_t now = time_ticks32();
    auto_led(now);
    if (g_auto.stage == BMCU_AUTO_CAL_BASELINE) run_baseline(now);
    else if (g_auto.stage == BMCU_AUTO_CAL_SAVING) save_transaction();
    else run_channel(now);
}

bool MC_PULL_calibration_auto_active() { return g_auto.active != 0u; }

bool MC_PULL_calibration_motion_inhibited()
{
    if (g_auto.active) return true;
    if (!g_motion_guard_mask) return false;

    ADC_DMA_poll();
    const uint32_t now = time_ticks32();
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        if (!(g_motion_guard_mask & (uint8_t)(1u << ch))) continue;
        const float *v8 = ADC_DMA_get_value();
        const float raw = adc_pull_raw_ch(ch, v8);
        const float key = adc_key_raw_ch(ch, v8);
        if (!finite_voltage(raw) || !isfinite(key) ||
            key >= MC_DM_KEY_NONE_THRESH[ch] ||
            fabsf((raw + MC_PULL_V_OFFSET[ch]) - 1.65f) >
                CAL_RELEASE_CENTER_EPS_V)
        {
            g_motion_guard_centered_since = 0u;
            return true;
        }
    }

    if (!g_motion_guard_centered_since)
    {
        g_motion_guard_centered_since = now;
        return true;
    }
    if (elapsed_ms32(g_motion_guard_centered_since, now) <
            CAL_POST_GUARD_STABLE_MS) return true;

    g_motion_guard_mask = 0u;
    g_motion_guard_centered_since = 0u;
    return false;
}
uint32_t MC_PULL_calibration_auto_op_id() { return g_auto.op_id; }
uint8_t MC_PULL_calibration_auto_stage() { return g_auto.stage; }
uint8_t MC_PULL_calibration_auto_state() { return g_auto.state; }
uint8_t MC_PULL_calibration_auto_reason() { return g_auto.reason; }
uint8_t MC_PULL_calibration_auto_channel() { return g_auto.channel; }
uint8_t MC_PULL_calibration_auto_selected_mask() { return (uint8_t)(g_auto.selected_mask & 0x0Fu); }
uint8_t MC_PULL_calibration_auto_done_mask() { return (uint8_t)(g_auto.done_mask & 0x0Fu); }

uint8_t MC_PULL_calibration_auto_progress()
{
    if (g_auto.state == BMCU_AUTO_CAL_STATE_DONE) return 100u;
    if (!g_auto.selected_mask) return 0u;
    if (g_auto.stage == BMCU_AUTO_CAL_BASELINE)
    {
        const uint32_t value = (uint32_t)g_auto.baseline_samples * 10u /
                               (uint32_t)CAL_BASELINE_SAMPLES;
        return value > 10u ? 10u : (uint8_t)value;
    }
    const uint8_t total = bit_count4(g_auto.selected_mask);
    const uint8_t done = bit_count4(g_auto.done_mask);
    uint8_t fraction = 0u;
    switch (g_auto.stage)
    {
    case BMCU_AUTO_CAL_FIRST_MOVE: fraction = 5u; break;
    case BMCU_AUTO_CAL_FIRST_RELEASE: fraction = 25u; break;
    case BMCU_AUTO_CAL_SECOND_MOVE: fraction = 50u; break;
    case BMCU_AUTO_CAL_SECOND_RELEASE: fraction = 75u; break;
    case BMCU_AUTO_CAL_SAVING: return 98u;
    default: break;
    }
    const uint32_t channel_span = 90u / (uint32_t)total;
    uint32_t progress = 10u + (uint32_t)done * channel_span +
                        channel_span * (uint32_t)fraction / 100u;
    return progress > 99u ? 99u : (uint8_t)progress;
}

bool MC_PULL_calibration_auto_take_result(uint32_t* op_id, uint8_t* state,
                                          uint8_t* reason, uint8_t* channel,
                                          uint32_t* duration_ms)
{
    if (!g_auto.result_pending) return false;
    if (op_id) *op_id = g_auto.op_id;
    if (state) *state = g_auto.state;
    if (reason) *reason = g_auto.reason;
    if (channel) *channel = g_auto.channel;
    if (duration_ms) *duration_ms = g_auto.result_duration_ms;
    g_auto.result_pending = 0u;
    return true;
}
