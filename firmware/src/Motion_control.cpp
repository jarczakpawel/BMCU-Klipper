#include "Motion_control.h"
#include "ams.h"
#include "ADC_DMA.h"
#include "Flash_saves.h"
#include "bmcu_config.h"
#include "many_soft_AS5600.h"
#include "MC_PULL_calibration.h"
#include "app_api.h"
#include "hal/time_hw.h"

static inline float absf(float x) { return (x < 0.0f) ? -x : x; }
static inline float clampf(float x, float a, float b)
{
    if (x < a) return a;
    if (x > b) return b;
    return x;
}

static inline uint8_t dm_key_v_to_centi_ceil(float v)
{
    if (v <= 0.0f) return 0u;

    float x = v * 100.0f - 0.0001f;
    int iv = (int)x;
    if ((float)iv < x) iv++;

    if (iv < 0) iv = 0;
    if (iv > 255) iv = 255;
    return (uint8_t)iv;
}

static inline float dm_key_centi_to_v(uint8_t cv)
{
    return 0.01f * (float)cv;
}

static uint64_t g_time_last_ticks64 = 0ull;
static uint32_t g_time_rem_ticks32  = 0u;
static uint64_t g_time_ms64         = 0ull;
static uint32_t g_time_tpm_last     = 0u;
static uint8_t  g_time_inited       = 0u;

static inline __attribute__((always_inline)) uint64_t time_ms_fast_from_ticks64(uint64_t now_ticks)
{
    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    if (!g_time_inited || (tpm != g_time_tpm_last))
    {
        g_time_inited = 1u;
        g_time_tpm_last = tpm;
        g_time_last_ticks64 = now_ticks;

        g_time_ms64 = now_ticks / (uint64_t)tpm;
        g_time_rem_ticks32 = (uint32_t)(now_ticks - g_time_ms64 * (uint64_t)tpm);
        return g_time_ms64;
    }

    const uint64_t dt64 = now_ticks - g_time_last_ticks64;
    g_time_last_ticks64 = now_ticks;

    if (__builtin_expect(dt64 > 0xFFFFFFFFull, 0))
    {
        g_time_ms64 = now_ticks / (uint64_t)tpm;
        g_time_rem_ticks32 = (uint32_t)(now_ticks - g_time_ms64 * (uint64_t)tpm);
        return g_time_ms64;
    }

    const uint32_t dt  = (uint32_t)dt64;
    const uint32_t rem = g_time_rem_ticks32;

    if (__builtin_expect(dt > (0xFFFFFFFFu - rem), 0))
    {
        g_time_ms64 = now_ticks / (uint64_t)tpm;
        g_time_rem_ticks32 = (uint32_t)(now_ticks - g_time_ms64 * (uint64_t)tpm);
        return g_time_ms64;
    }

    const uint32_t acc = dt + rem;

    if (tpm <= 1u)
    {
        g_time_ms64 += (uint64_t)acc;
        g_time_rem_ticks32 = 0u;
        return g_time_ms64;
    }

    if (__builtin_expect(acc < tpm, 1))
    {
        g_time_rem_ticks32 = acc;
        return g_time_ms64;
    }
    if (__builtin_expect(tpm <= 0x7FFFFFFFu && acc < tpm * 2u, 1))
    {
        g_time_rem_ticks32 = acc - tpm;
        g_time_ms64++;
        return g_time_ms64;
    }

    const uint32_t inc = acc / tpm;
    g_time_rem_ticks32 = acc - inc * tpm;
    g_time_ms64 += (uint64_t)inc;
    return g_time_ms64;
}

static inline __attribute__((always_inline)) uint64_t time_ms_fast(void)
{
    return time_ms_fast_from_ticks64(time_ticks64());
}

static inline float retract_mag_from_err(float err, float mag_max)
{
    constexpr float e0 = 0.10f;
    constexpr float e1 = 0.35f;
    constexpr float e2 = 2.35f;

    if (err <= e0) return 0.0f;

    float mag;
    if (err < e1)
    {
        float t = (err - e0) / (e1 - e0);
        t = clampf(t, 0.0f, 1.0f);
        mag = 450.0f + 100.0f * t;
    }
    else
    {
        float t = (err - e1) / (e2 - e1);
        t = clampf(t, 0.0f, 1.0f);
        mag = 550.0f + 300.0f * t;
    }

    if (mag > mag_max) mag = mag_max;
    return mag;
}

static inline uint8_t hyst_u8(uint8_t active, float v, float start, float stop)
{
    if (active) { if (v <= stop)  active = 0; }
    else        { if (v >= start) active = 1; }
    return active;
}

static constexpr uint8_t  kChCount = 4;
static constexpr int      PWM_lim  = 1000;
static constexpr float    kAS5600_PI = 3.14159265358979323846f;

static constexpr float kAS5600_MM_PER_CNT = -(kAS5600_PI * 7.5f) / 4096.0f;

AS5600_soft_IIC_many MC_AS5600;
static GPIO_TypeDef* const AS5600_SCL_PORT[4] = { GPIOB, GPIOB, GPIOB, GPIOB };
static const uint16_t      AS5600_SCL_PIN [4] = { GPIO_Pin_15, GPIO_Pin_14, GPIO_Pin_13, GPIO_Pin_12 };
static GPIO_TypeDef* const AS5600_SDA_PORT[4] = { GPIOD, GPIOC, GPIOC, GPIOC };
static const uint16_t      AS5600_SDA_PIN [4] = { GPIO_Pin_0, GPIO_Pin_15, GPIO_Pin_14, GPIO_Pin_13 };

float speed_as5600[4] = {0, 0, 0, 0};

static uint8_t g_as5600_good[4]     = {0,0,0,0};
static uint8_t g_as5600_fail[4]     = {0,0,0,0};
static uint8_t g_as5600_okstreak[4] = {0,0,0,0};
static constexpr uint8_t kAS5600_FAIL_TRIP   = 3;
static constexpr uint8_t kAS5600_OK_RECOVER  = 2;
static inline bool AS5600_is_good(uint8_t ch) { return g_as5600_good[ch] != 0; }

static constexpr float PULL_V_FAST_DEFAULT   = 60.0f;
static constexpr float PULL_V_END_DEFAULT    = 12.0f;
#define PULL_V_FAST (bmcu_config_pull_speed())
#define PULL_V_END  (bmcu_config_pull_speed_end())
static constexpr float PULL_RAMP_M   = 0.015f;
static constexpr float PULL_PWM_MIN  = 400.0f;

static float g_pull_remain_m[4]  = {0,0,0,0};
static float g_pull_speed_set[4] = {-60.0f,-60.0f,-60.0f,-60.0f};

float MC_PULL_V_OFFSET[4]      = {0.0f, 0.0f, 0.0f, 0.0f};
float MC_PULL_V_MIN[4]         = {1.00f, 1.00f, 1.00f, 1.00f};
float MC_PULL_V_MAX[4]         = {2.00f, 2.00f, 2.00f, 2.00f};
int8_t MC_PULL_POLARITY[4]     = {1, 1, 1, 1};
float MC_DM_KEY_NONE_THRESH[4] = {0.60f, 0.60f, 0.60f, 0.60f};

uint8_t MC_PULL_pct[4]        = {50, 50, 50, 50};
static float MC_PULL_pct_f[4] = {50.0f, 50.0f, 50.0f, 50.0f};

static float  MC_PULL_stu_raw[4]        = {1.65f, 1.65f, 1.65f, 1.65f};
static int8_t MC_PULL_stu[4]            = {0, 0, 0, 0};

static uint8_t  MC_ONLINE_key_stu[4]    = {0, 0, 0, 0};
static uint8_t  g_on_use_low_latch[4]   = {0, 0, 0, 0};
static uint8_t  g_on_use_jam_latch[4]   = {0, 0, 0, 0};
static uint32_t g_on_use_hi_pwm_us[4]   = {0u, 0u, 0u, 0u};

static inline __attribute__((always_inline)) void MC_STU_RGB_set_latch(uint8_t ch, uint8_t r, uint8_t g, uint8_t b, uint64_t now_ms, uint8_t blink)
{
    if (!g_on_use_low_latch[ch]) { MC_STU_RGB_set(ch, r, g, b); return; }

    if (!blink || (((now_ms / 1000ull) & 1ull) != 0ull))
        MC_STU_RGB_set(ch, 0xFFu, 0x00u, 0x00u);
    else
        MC_STU_RGB_set(ch, r, g, b);
}

static inline __attribute__((always_inline)) void MC_STU_STATUS_set_latch(
    uint8_t ch, uint8_t status, uint64_t now_ms, uint8_t blink)
{
    if (!g_on_use_low_latch[ch]) { MC_STU_STATUS_set(ch, status); return; }
    if (!blink || (((now_ms / 1000ull) & 1ull) != 0ull))
        MC_STU_STATUS_set(ch, BMCU_LED_ERROR);
    else
        MC_STU_STATUS_set(ch, status);
}

#if BMCU_DM_TWO_MICROSWITCH
static inline uint8_t dm_key_to_state(uint8_t ch, float v)
{
    const float none_thr = MC_DM_KEY_NONE_THRESH[ch];

    if (v < none_thr) return 0u;
    if (v > 1.7f)     return 1u;
    if (v > 1.4f)     return 2u;
    return 3u;
}

static constexpr uint64_t DM_AUTO_S1_DEBOUNCE_MS       = 100ull;
static constexpr uint64_t DM_AUTO_S1_TIMEOUT_MS        = 5000ull;
static constexpr uint64_t DM_AUTO_S1_FAIL_RETRACT_MS   = 1500ull;

static constexpr float    DM_AUTO_BUF_ABORT_PCT        = 75.0f;
static constexpr float    DM_AUTO_BUF_RECOVER_PCT      = 50.2f;
static constexpr uint64_t DM_AUTO_FAIL_EXTRA_MS        = 1500ull;
static constexpr float    DM_AUTO_PWM_PUSH             = 900.0f;
static constexpr float    DM_AUTO_PWM_PULL             = 900.0f;
static constexpr float    DM_AUTO_IDLE_LIM             = 950.0f;

enum : uint8_t
{
    DM_AUTO_IDLE = 0,
    DM_AUTO_S1_DEBOUNCE,
    DM_AUTO_S1_PUSH,
    DM_AUTO_S1_FAIL_RETRACT,
    DM_AUTO_S2_PUSH,
    DM_AUTO_S2_RETRACT,
    DM_AUTO_S2_FAIL_RETRACT,
    DM_AUTO_S2_FAIL_EXTRA,
};

static uint8_t  dm_loaded[4]            = {1,1,1,1};
static uint8_t  dm_fail_latch[4]        = {0,0,0,0};
static uint8_t  dm_auto_state[4]        = {0,0,0,0};
static uint8_t  dm_autoload_gate[4]     = {0,0,0,0};
static uint8_t  dm_auto_try[4]          = {0,0,0,0};
static uint64_t dm_auto_t0_ms[4]        = {0ull,0ull,0ull,0ull};
static float    dm_auto_remain_m[4]     = {0,0,0,0};
static float    dm_auto_last_m[4]       = {0,0,0,0};

static uint64_t dm_loaded_drop_t0_ms[4] = {0ull,0ull,0ull,0ull};
#endif

static constexpr float    AUTO_UNLOAD_START_PCT      = 80.0f;
static constexpr float    AUTO_UNLOAD_NEUTRAL_LO_PCT = 45.0f;
static constexpr float    AUTO_UNLOAD_NEUTRAL_HI_PCT = 55.0f;
static constexpr float    AUTO_UNLOAD_ABORT_PCT      = 35.0f;
static constexpr uint64_t AUTO_UNLOAD_ARM_MS         = 1000ull;
static constexpr uint64_t AUTO_UNLOAD_MAX_MS         = 300000ull;
static constexpr uint64_t AUTO_UNLOAD_EMPTY_MS       = 1500ull;
static constexpr uint64_t AUTO_UNLOAD_REARM_NEUTRAL_MS = 500ull;
static constexpr float    AUTO_UNLOAD_MAX_M          = 5.0f;
static constexpr float    AUTO_UNLOAD_PWM_PULL       = 850.0f;

static uint8_t  auto_unload_arm[4]          = {0,0,0,0};
static uint8_t  auto_unload_active[4]       = {0,0,0,0};
static uint8_t  auto_unload_explicit[4]     = {0,0,0,0};
static uint8_t  auto_unload_blocked[4]      = {0,0,0,0};
static uint64_t auto_unload_arm_t0_ms[4]    = {0ull,0ull,0ull,0ull};
static uint64_t auto_unload_active_t0_ms[4] = {0ull,0ull,0ull,0ull};
static uint64_t auto_unload_empty_t0_ms[4]  = {0ull,0ull,0ull,0ull};
static uint64_t auto_unload_rearm_t0_ms[4]  = {0ull,0ull,0ull,0ull};
static float    auto_unload_start_m[4]       = {0.0f,0.0f,0.0f,0.0f};

static constexpr uint64_t HOST_MOTION_SETTLE_MS = 2000ull;
static constexpr float HOST_MOTION_NEUTRAL_LO_PCT = 35.0f;
static constexpr float HOST_MOTION_NEUTRAL_HI_PCT = 65.0f;
static constexpr float HOST_MOTION_STABLE_DELTA_PCT = 1.5f;

static bool     g_host_motion_requested          = false;
static bool     g_host_motion_enabled            = false;
static bool     g_host_motion_sample_valid       = false;
static uint64_t g_host_motion_stable_t0_ms       = 0ull;
static uint8_t  g_host_motion_last_key[4]        = {0u,0u,0u,0u};
static float    g_host_motion_last_pct[4]        = {0.0f,0.0f,0.0f,0.0f};

static inline void auto_unload_reset(uint8_t channel, uint8_t blocked)
{
    if (channel >= 4u) return;
    auto_unload_arm[channel] = 0u;
    auto_unload_active[channel] = 0u;
    auto_unload_explicit[channel] = 0u;
    auto_unload_blocked[channel] = blocked ? 1u : 0u;
    auto_unload_arm_t0_ms[channel] = 0ull;
    auto_unload_active_t0_ms[channel] = 0ull;
    auto_unload_empty_t0_ms[channel] = 0ull;
    auto_unload_rearm_t0_ms[channel] = 0ull;
    auto_unload_start_m[channel] = 0.0f;
}

bool filament_channel_inserted[4]       = {false, false, false, false};

uint8_t Motion_control_filament_present(uint8_t CHx)
{
    if (CHx >= kChCount) return 0u;
    return (MC_ONLINE_key_stu[CHx] != 0u) ? 1u : 0u;
}

uint8_t Motion_control_channel_retract_target_reached(uint8_t CHx)
{
    if (CHx >= kChCount) return 0u;
#if BMCU_DM_TWO_MICROSWITCH
    // State 1 means both microswitches are pressed. During pull-back the
    // internal switch clears first while the external insertion switch may
    // intentionally remain pressed. That is the physical end of retract.
    return (MC_ONLINE_key_stu[CHx] != 1u) ? 1u : 0u;
#else
    return (MC_ONLINE_key_stu[CHx] == 0u) ? 1u : 0u;
#endif
}

uint8_t Motion_control_channel_connected(uint8_t CHx)
{
    if (CHx >= kChCount) return 0u;
    return filament_channel_inserted[CHx] ? 1u : 0u;
}

uint8_t Motion_control_encoder_io_ok(uint8_t CHx)
{
    if (CHx >= kChCount) return 0u;
    return g_as5600_good[CHx] ? 1u : 0u;
}

float Motion_control_encoder_meters(uint8_t CHx)
{
    if (CHx >= kChCount) return 0.0f;

    return ams[motion_control_ams_num].filament[CHx].meters;
}

static constexpr float MC_PULL_PIDP_PCT = 25.0f;

static constexpr int MC_PULL_DEADBAND_PCT_LOW  = 30;
static constexpr int MC_PULL_DEADBAND_PCT_HIGH = 70;

struct MC_LOAD_TUNING
{
    uint8_t pressure_pct;
    int s1_fast_pct;
    int s1_hard_stop_pct;
    int s1_hard_hys;
    float s2_hold_target_pct;
    float s2_hold_band_lo_delta;
    float s2_push_start_pct;
    float s2_pwm_hi;
    float s2_pwm_lo;
    float on_use_target_pct;
    float on_use_band_lo_delta;
    float on_use_band_hi_pct;
};

static MC_LOAD_TUNING g_mc_load_tuning = {};
static uint8_t g_mc_load_tuning_pressure = 0xFFu;

static inline float mc_load_lerp(float minimum_value, float maximum_value,
                                 float blend)
{
    return minimum_value + (maximum_value - minimum_value) * blend;
}

static inline const MC_LOAD_TUNING& MC_LOAD_TUNING_runtime()
{
    const uint8_t pressure = bmcu_config_load_pressure_pct();
    if (pressure != g_mc_load_tuning_pressure)
    {
        const float blend =
            ((float)pressure - (float)BMCU_LOAD_PRESSURE_MIN_PCT) /
            ((float)BMCU_LOAD_PRESSURE_MAX_PCT -
             (float)BMCU_LOAD_PRESSURE_MIN_PCT);
        MC_LOAD_TUNING tuning;
        tuning.pressure_pct = pressure;
        tuning.s1_fast_pct = (int)(mc_load_lerp(75.0f, 88.0f, blend) + 0.5f);
        tuning.s1_hard_stop_pct =
            (int)(mc_load_lerp(90.0f, 97.0f, blend) + 0.5f);
        tuning.s1_hard_hys = 2;
        tuning.s2_hold_target_pct = (float)pressure;
        tuning.s2_hold_band_lo_delta = mc_load_lerp(0.3f, 1.0f, blend);
        tuning.s2_push_start_pct = mc_load_lerp(55.0f, 88.0f, blend);
        tuning.s2_pwm_hi = mc_load_lerp(480.0f, 550.0f, blend);
        tuning.s2_pwm_lo = 1000.0f;
        tuning.on_use_target_pct = mc_load_lerp(52.0f, 54.0f, blend);
        tuning.on_use_band_lo_delta = 0.2f;
        tuning.on_use_band_hi_pct = mc_load_lerp(60.0f, 65.0f, blend);
        g_mc_load_tuning = tuning;
        g_mc_load_tuning_pressure = pressure;
    }
    return g_mc_load_tuning;
}

static inline int MC_LOAD_S1_FAST_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().s1_fast_pct;
}

static inline int MC_LOAD_S1_HARD_STOP_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().s1_hard_stop_pct;
}

static inline int MC_LOAD_S1_HARD_HYS_runtime()
{
    return MC_LOAD_TUNING_runtime().s1_hard_hys;
}

static inline float MC_LOAD_S2_HOLD_TARGET_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().s2_hold_target_pct;
}

static inline float MC_LOAD_S2_HOLD_BAND_LO_DELTA_runtime()
{
    return MC_LOAD_TUNING_runtime().s2_hold_band_lo_delta;
}

static inline float MC_LOAD_S2_PUSH_START_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().s2_push_start_pct;
}

static inline float MC_LOAD_S2_PWM_HI_runtime()
{
    return MC_LOAD_TUNING_runtime().s2_pwm_hi;
}

static inline float MC_LOAD_S2_PWM_LO_runtime()
{
    return MC_LOAD_TUNING_runtime().s2_pwm_lo;
}

static inline float MC_ON_USE_TARGET_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().on_use_target_pct;
}

static inline float MC_ON_USE_BAND_LO_DELTA_runtime()
{
    return MC_LOAD_TUNING_runtime().on_use_band_lo_delta;
}

static inline float MC_ON_USE_BAND_HI_PCT_runtime()
{
    return MC_LOAD_TUNING_runtime().on_use_band_hi_pct;
}

#undef MC_LOAD_S1_FAST_PCT
#undef MC_LOAD_S1_HARD_STOP_PCT
#undef MC_LOAD_S1_HARD_HYS
#undef MC_LOAD_S2_HOLD_TARGET_PCT
#undef MC_LOAD_S2_HOLD_BAND_LO_DELTA
#undef MC_LOAD_S2_PUSH_START_PCT
#undef MC_LOAD_S2_PWM_HI
#undef MC_LOAD_S2_PWM_LO
#undef MC_ON_USE_TARGET_PCT
#undef MC_ON_USE_BAND_LO_DELTA
#undef MC_ON_USE_BAND_HI_PCT
#define MC_LOAD_S1_FAST_PCT              (MC_LOAD_S1_FAST_PCT_runtime())
#define MC_LOAD_S1_HARD_STOP_PCT         (MC_LOAD_S1_HARD_STOP_PCT_runtime())
#define MC_LOAD_S1_HARD_HYS              (MC_LOAD_S1_HARD_HYS_runtime())
#define MC_LOAD_S2_HOLD_TARGET_PCT       (MC_LOAD_S2_HOLD_TARGET_PCT_runtime())
#define MC_LOAD_S2_HOLD_BAND_LO_DELTA    (MC_LOAD_S2_HOLD_BAND_LO_DELTA_runtime())
#define MC_LOAD_S2_PUSH_START_PCT        (MC_LOAD_S2_PUSH_START_PCT_runtime())
#define MC_LOAD_S2_PWM_HI                (MC_LOAD_S2_PWM_HI_runtime())
#define MC_LOAD_S2_PWM_LO                (MC_LOAD_S2_PWM_LO_runtime())
#define MC_ON_USE_TARGET_PCT             (MC_ON_USE_TARGET_PCT_runtime())
#define MC_ON_USE_BAND_LO_DELTA          (MC_ON_USE_BAND_LO_DELTA_runtime())
#define MC_ON_USE_BAND_HI_PCT            (MC_ON_USE_BAND_HI_PCT_runtime())

static constexpr uint32_t CAL_START_HOLD_MS     = 5000;
static constexpr int      CAL_START_PCT_THRESH  = 15;
static constexpr float    CAL_START_V_DELTA     = 0.10f;
static constexpr float    CAL_START_NEAR_MIN    = 0.03f;

static uint8_t calibration_breathe(uint64_t milliseconds,
                                   uint32_t period_ms,
                                   uint8_t minimum,
                                   uint8_t maximum)
{
    if (period_ms < 2u || maximum <= minimum) return maximum;
    const uint32_t half = period_ms / 2u;
    uint32_t phase = (uint32_t)(milliseconds % period_ms);
    uint32_t x = phase <= half ? phase : period_ms - phase;
    x = (x * 255u) / half;
    const uint32_t smooth =
        (x * x * (765u - 2u * x) + 32512u) / 65025u;
    return (uint8_t)(minimum +
        ((uint32_t)(maximum - minimum) * smooth + 127u) / 255u);
}

static int      g_hold_ch = -1;
static uint32_t g_hold_t0_ticks = 0;
static bool     g_cal_start_latched = false;
static bool     g_calibration_motion_inhibit = false;

static uint64_t g_last_on_use_exit_ms[4] = {0,0,0,0};

// Smooth handoff from loading pressure to normal on-use pressure control.
// For a short window the printer is allowed to consume the buffered filament
// naturally. BMCU only enforces the entry pressure as an upper ceiling and the
// normal on-use target as the lower bound.
static constexpr uint64_t ON_USE_HANDOFF_MS = 10000ull;
static constexpr float ON_USE_HANDOFF_CEILING_HYS_PCT = 0.25f;

extern void RGB_update();

static inline bool all_no_filament()
{
    return ((MC_ONLINE_key_stu[0] | MC_ONLINE_key_stu[1] | MC_ONLINE_key_stu[2] | MC_ONLINE_key_stu[3]) == 0);
}

void Motion_control_prepare_calibration(void)
{
    auto &A = ams[motion_control_ams_num];
    for (uint8_t i = 0; i < kChCount; i++)
    {
        A.filament[i].motion = _filament_motion::idle;
        Motion_control_set_PWM(i, 0);
        auto_unload_reset(i, 1u);
#if BMCU_DM_TWO_MICROSWITCH
        dm_auto_state[i] = DM_AUTO_IDLE;
        dm_autoload_gate[i] = 1u;
        dm_auto_try[i] = 0u;
        dm_auto_t0_ms[i] = 0ull;
        dm_auto_remain_m[i] = 0.0f;
        dm_auto_last_m[i] = 0.0f;
#endif
    }
    A.now_filament_num = 0xFFu;
    A.filament_use_flag = 0u;
}

static void calibration_start_from_buffer_button()
{
    Motion_control_prepare_calibration();

    uint32_t op_id = 0x80000000u | (time_ticks32() & 0x7FFFFFFFu);
    if (op_id == 0u) op_id = 0x80000001u;
    (void)MC_PULL_calibration_auto_start(0x0Fu, op_id, true,
                                         (uint8_t)g_hold_ch);
}

static float pull_v_to_percent_f(uint8_t ch, float v)
{
    constexpr float c = 1.65f;

    float vmin = MC_PULL_V_MIN[ch];
    float vmax = MC_PULL_V_MAX[ch];

    if (vmin > 1.60f) vmin = 1.60f;
    if (vmax < 1.70f) vmax = 1.70f;
    if (vmax <= (vmin + 0.10f)) { vmin = 1.55f; vmax = 1.75f; }

    float pos01;
    if (v <= c)
    {
        float den = c - vmin;
        if (den < 0.05f) den = 0.05f;
        pos01 = 0.5f * (v - vmin) / den;
    }
    else
    {
        float den = vmax - c;
        if (den < 0.05f) den = 0.05f;
        pos01 = 0.5f + 0.5f * (v - c) / den;
    }

    return clampf(pos01, 0.0f, 1.0f) * 100.0f;
}

static inline float pull_v_apply_polarity(uint8_t ch, float v)
{
    if (MC_PULL_POLARITY[ch] < 0) return 3.30f - v;
    return v;
}

void MC_PULL_detect_channels_inserted()
{
    if (!ADC_DMA_is_inited())
    {
        for (uint8_t ch = 0; ch < kChCount; ch++) filament_channel_inserted[ch] = false;
        return;
    }

    ADC_DMA_gpio_analog();
    ADC_DMA_filter_reset();
    ADC_DMA_wait_full();
    if (!ADC_DMA_ready())
    {
        for (uint8_t ch = 0; ch < kChCount; ch++)
            filament_channel_inserted[ch] = false;
        return;
    }

    constexpr uint8_t idx[kChCount] = {6,4,2,0};
    constexpr int N = 16;
    float s[kChCount] = {0,0,0,0};

    for (int i = 0; i < N; i++)
    {
        const float *v = ADC_DMA_get_value();
        for (uint8_t ch = 0; ch < kChCount; ch++) s[ch] += v[idx[ch]];
        delay(2);
    }

    constexpr float VMIN = 0.30f;
    constexpr float VMAX = 3.00f;
    constexpr float invN = 1.0f / (float)N;

    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        const float a = s[ch] * invN;
        filament_channel_inserted[ch] = (a > VMIN) && (a < VMAX);
    }
}

static inline void MC_PULL_ONLINE_init()
{
    MC_PULL_detect_channels_inserted();
}

static inline bool MC_PULL_ONLINE_read(uint32_t now_ticks)
{
    const float *data = ADC_DMA_get_value();
    if (!ADC_DMA_sample_ready())
        return false;

    MC_PULL_stu_raw[3] = pull_v_apply_polarity(3u, data[0] + MC_PULL_V_OFFSET[3]);
    const float key3   = data[1];

    MC_PULL_stu_raw[2] = pull_v_apply_polarity(2u, data[2] + MC_PULL_V_OFFSET[2]);
    const float key2   = data[3];

    MC_PULL_stu_raw[1] = pull_v_apply_polarity(1u, data[4] + MC_PULL_V_OFFSET[1]);
    const float key1   = data[5];

    MC_PULL_stu_raw[0] = pull_v_apply_polarity(0u, data[6] + MC_PULL_V_OFFSET[0]);
    const float key0   = data[7];

#if BMCU_DM_TWO_MICROSWITCH
    const float keyv[4] = { key0, key1, key2, key3 };

    static uint32_t gst_t0_ticks[4]     = {0,0,0,0};
    static uint8_t  gst_step[4]         = {0,0,0,0};
    static bool     gst_active[4]       = {false,false,false,false};
    static uint32_t gst_act_t0_ticks[4] = {0,0,0,0};

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    const uint32_t T100  = 100u  * tpm;
    const uint32_t T2000 = 2000u * tpm;
    const uint32_t T5500 = 5500u * tpm;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (!filament_channel_inserted[i])
        {
            gst_step[i] = 0;
            gst_active[i] = false;
            gst_t0_ticks[i] = 0;
            gst_act_t0_ticks[i] = 0;
            MC_ONLINE_key_stu[i] = 0u;
            continue;
        }

        if (dm_fail_latch[i])
        {
            gst_step[i] = 0;
            gst_active[i] = false;
        }

        if (!gst_active[i])
        {
            const float pct_f = pull_v_to_percent_f(i, MC_PULL_stu_raw[i]);

            if (gst_step[i] == 0)
            {
                if (pct_f < 10.0f) { gst_step[i] = 1; gst_t0_ticks[i] = now_ticks; }
            }
            else if (gst_step[i] == 1)
            {
                if (pct_f > 15.0f) { gst_step[i] = 0; }
                else if ((uint32_t)(now_ticks - gst_t0_ticks[i]) >= T100)
                {
                    gst_step[i] = 2;
                }
            }
            else
            {
                if ((uint32_t)(now_ticks - gst_t0_ticks[i]) > T2000)
                {
                    gst_step[i] = 0;
                }
                else if (pct_f >= 45.0f && pct_f <= 55.0f)
                {
                    gst_active[i] = true;
                    gst_act_t0_ticks[i] = now_ticks;
                    gst_step[i] = 0;
                }
            }
        }

        if (gst_active[i])
        {
            if (keyv[i] > 1.7f) gst_active[i] = false;
            else if ((uint32_t)(now_ticks - gst_act_t0_ticks[i]) > T5500) gst_active[i] = false;
        }

        const uint8_t phys = dm_key_to_state(i, keyv[i]);
        uint8_t state = phys;

        if (gst_active[i] && (phys == 0u)) state = 2u;

        MC_ONLINE_key_stu[i] = state;
    }

#else

    MC_ONLINE_key_stu[3] = (filament_channel_inserted[3] && (key3 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[2] = (filament_channel_inserted[2] && (key2 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[1] = (filament_channel_inserted[1] && (key1 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[0] = (filament_channel_inserted[0] && (key0 > 1.7f)) ? 1u : 0u;
#endif

    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ins = filament_channel_inserted[i];

        if (!ins)
        {
            MC_ONLINE_key_stu[i] = 0;
            MC_PULL_pct_f[i] = 50.0f;
            MC_PULL_pct[i]   = 50;
            MC_PULL_stu[i]   = 0;
            continue;
        }

        const float pct_f = pull_v_to_percent_f(i, MC_PULL_stu_raw[i]);
        MC_PULL_pct_f[i] = pct_f;

        int pct = (int)(pct_f + 0.5f);
        if (pct < 0) pct = 0;
        if (pct > 100) pct = 100;
        MC_PULL_pct[i] = (uint8_t)pct;

        if      (pct > MC_PULL_DEADBAND_PCT_HIGH) MC_PULL_stu[i] = 1;
        else if (pct < MC_PULL_DEADBAND_PCT_LOW)  MC_PULL_stu[i] = -1;
        else                                      MC_PULL_stu[i] = 0;
    }

    ams[motion_control_ams_num].pressure = 0xFFFF;
    return true;

}

using Motion_control_save_struct = BmcuMotionNvm;
#define Motion_control_data_save g_bmcu_nvm

static inline void Motion_control_defaults()
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        Motion_control_data_save.Motion_control_dir[i] = 0;
        Motion_control_data_save.dm_key_none_cv[i] = 60u;
    }

    Motion_control_data_save.check = 0x40614061u;
}

static inline void Motion_control_apply_saved()
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (Motion_control_data_save.dm_key_none_cv[i] < 60u)
            Motion_control_data_save.dm_key_none_cv[i] = 60u;

        MC_DM_KEY_NONE_THRESH[i] = dm_key_centi_to_v(Motion_control_data_save.dm_key_none_cv[i]);
    }
}

static inline bool Motion_control_read()
{

    BmcuMotionNvm stored{};
    if (!Flash_Motion_read(&stored, (uint16_t)sizeof(stored)) ||
        stored.check != 0x40614061u)
    {
        Motion_control_defaults();
        Motion_control_apply_saved();
        return false;
    }

    for (uint8_t i = 0u; i < kChCount; i++)
    {
        Motion_control_data_save.Motion_control_dir[i] = stored.Motion_control_dir[i];
        Motion_control_data_save.dm_key_none_cv[i] = stored.dm_key_none_cv[i];
    }
    Motion_control_data_save.check = stored.check;
    Motion_control_apply_saved();
    return true;
}

static inline bool Motion_control_save()
{
    Motion_control_data_save.check = 0x40614061u;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        uint8_t cv = dm_key_v_to_centi_ceil(MC_DM_KEY_NONE_THRESH[i]);
        if (cv < 60u) cv = 60u;
        Motion_control_data_save.dm_key_none_cv[i] = cv;
    }

    return bmcu_config_save();
}

bool Motion_control_save_dm_key_none_thresholds(void)
{
    float thr[4];
    for (uint8_t i = 0; i < kChCount; i++)
        thr[i] = MC_DM_KEY_NONE_THRESH[i];

    (void)Motion_control_read();

    for (uint8_t i = 0; i < kChCount; i++)
        MC_DM_KEY_NONE_THRESH[i] = thr[i];

    return Motion_control_save();
}

class MOTOR_PID
{
    float P = 0;
    float I = 0;
    float D = 0;
    float I_save = 0;
    float E_last = 0;

    float pid_MAX = PWM_lim;
    float pid_MIN = -PWM_lim;
    float pid_range = (pid_MAX - pid_MIN) * 0.5f;

public:
    MOTOR_PID() = default;

    MOTOR_PID(float P_set, float I_set, float D_set)
    {
        init_PID(P_set, I_set, D_set);
    }

    void init_PID(float P_set, float I_set, float D_set)
    {
        P = P_set;
        I = I_set;
        D = D_set;
        I_save = 0;
        E_last = 0;
    }

    float caculate(float E, float time_E)
    {
        I_save += I * E * time_E;
        if (I_save > pid_range)  I_save = pid_range;
        if (I_save < -pid_range) I_save = -pid_range;

        float out;
        if (time_E != 0.0f)
            out = P * E + I_save + D * (E - E_last) / time_E;
        else
            out = P * E + I_save;

        if (out > pid_MAX) out = pid_MAX;
        if (out < pid_MIN) out = pid_MIN;

        E_last = E;
        return out;
    }

    void clear()
    {
        I_save = 0;
        E_last = 0;
    }
};

enum class filament_motion_enum
{
    filament_motion_send,
    filament_motion_redetect,
    filament_motion_pull,
    filament_motion_stop,
    filament_motion_before_on_use,
    filament_motion_stop_on_use,
    filament_motion_pressure_ctrl_on_use,
    filament_motion_pressure_ctrl_idle,
    filament_motion_before_pull_back,
};

class _MOTOR_CONTROL
{
public:
    filament_motion_enum motion = filament_motion_enum::filament_motion_stop;
    int CHx = 0;

    uint8_t pwm_zeroed = 1;

    uint64_t motor_stop_time = 0;

    float    post_sendout_retract_thresh_pct = -1.0f;
    uint8_t  retract_hys_active = 0;
    float    on_use_handoff_ceiling_pct = -1.0f;
    uint64_t on_use_handoff_t0_ms = 0ull;

    uint64_t send_start_ms = 0;
    float    send_start_m  = 0.0f;
    uint8_t  send_len_abort = 0;

    uint64_t pull_start_ms = 0;

    bool send_stop_latch = false;

    MOTOR_PID PID_speed    = MOTOR_PID(2, 20, 0);
    MOTOR_PID PID_pressure = MOTOR_PID(MC_PULL_PIDP_PCT, 0, 0);

    float pwm_zero = 500;
    float dir = 0;

    static float x_prev[4];

    bool  send_hard = false;

    _MOTOR_CONTROL(int _CHx) : CHx(_CHx) {}

    void set_pwm_zero(float _pwm_zero) { pwm_zero = _pwm_zero; }

    void set_motion(filament_motion_enum _motion, uint64_t over_time)
    {
        set_motion(_motion, over_time, time_ms_fast());
    }

    void set_motion(filament_motion_enum _motion, uint64_t over_time, uint64_t time_now)
    {
        motor_stop_time = (_motion == filament_motion_enum::filament_motion_stop) ? 0 : (time_now + over_time);

        if (motion == _motion) return;

        const filament_motion_enum prev = motion;
        motion = _motion;

        if ((_motion != filament_motion_enum::filament_motion_pressure_ctrl_on_use) &&
            g_on_use_low_latch[CHx] && !g_on_use_jam_latch[CHx])
        {
            g_on_use_low_latch[CHx] = 0u;
            g_on_use_hi_pwm_us[CHx] = 0u;
        }

        pwm_zeroed = 0;

        if (_motion == filament_motion_enum::filament_motion_send) {
            send_start_ms = time_now;
            send_stop_latch = false;
            send_len_abort = 0;
            send_start_m = ams[motion_control_ams_num].filament[CHx].meters;
        }

        if (_motion == filament_motion_enum::filament_motion_pull) {
            pull_start_ms = time_now;
        }

        if (prev == filament_motion_enum::filament_motion_send &&
            _motion != filament_motion_enum::filament_motion_send)
        {
            send_start_ms = 0;
            send_stop_latch = false;
            send_len_abort = 0;
            send_start_m = 0.0f;
        }

        if (prev == filament_motion_enum::filament_motion_pull &&
            _motion != filament_motion_enum::filament_motion_pull)
        {
            pull_start_ms = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            if (g_last_on_use_exit_ms[CHx] == 0) g_last_on_use_exit_ms[CHx] = 1;
        }

        if (prev == filament_motion_enum::filament_motion_pressure_ctrl_on_use &&
            _motion != filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            g_last_on_use_exit_ms[CHx] = time_now;
        }

        if (_motion == filament_motion_enum::filament_motion_send ||
            _motion == filament_motion_enum::filament_motion_pull)
        {
            g_last_on_use_exit_ms[CHx] = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_send)
        {
            send_hard = false;
        }

        if (prev == filament_motion_enum::filament_motion_send &&
            _motion != filament_motion_enum::filament_motion_send)
        {
            send_hard = false;
        }

        PID_speed.clear();
        PID_pressure.clear();

        const bool keep_pwm =
            (prev == filament_motion_enum::filament_motion_send) &&
            (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use);

        if (_motion == filament_motion_enum::filament_motion_send)
        {
            post_sendout_retract_thresh_pct = -1.0f;
            retract_hys_active = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_before_on_use || _motion == filament_motion_enum::filament_motion_stop_on_use)
        {
            float p = MC_PULL_pct_f[CHx];
            if (p < 0.0f) p = 0.0f;
            if (p > 100.0f) p = 100.0f;
            post_sendout_retract_thresh_pct = p;
            retract_hys_active = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            retract_hys_active = 0;
            float p = MC_PULL_pct_f[CHx];
            if (p < 0.0f) p = 0.0f;
            if (p > 100.0f) p = 100.0f;

            post_sendout_retract_thresh_pct = p;

            if (prev == filament_motion_enum::filament_motion_before_on_use ||
                prev == filament_motion_enum::filament_motion_stop_on_use ||
                prev == filament_motion_enum::filament_motion_send)
            {
                const float band_hi = MC_ON_USE_BAND_HI_PCT;
                if (p > band_hi)
                {
                    on_use_handoff_ceiling_pct = p;
                    on_use_handoff_t0_ms = time_now;
                }
                else
                {
                    on_use_handoff_ceiling_pct = -1.0f;
                    on_use_handoff_t0_ms = 0ull;
                }
            }
            else
            {
                on_use_handoff_ceiling_pct = -1.0f;
                on_use_handoff_t0_ms = 0ull;
            }
        }
        else if (prev == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            post_sendout_retract_thresh_pct = -1.0f;
            retract_hys_active   = 0;
            on_use_handoff_ceiling_pct   = -1.0f;
            on_use_handoff_t0_ms = 0ull;
        }

        if (_motion == filament_motion_enum::filament_motion_pull)
        {
            post_sendout_retract_thresh_pct = -1.0f;
            retract_hys_active = 0;
        }

        if (!keep_pwm)
        {
            x_prev[CHx] = 0.0f;
        }
        else
        {
            if (x_prev[CHx] > 600.0f)  x_prev[CHx] = 600.0f;
            if (x_prev[CHx] < -850.0f) x_prev[CHx] = -850.0f;
        }
    }

    filament_motion_enum get_motion() { return motion; }

    static inline void hold_load(
        float pct,
        float dir,
        MOTOR_PID &PID_pressure,
        float &post_sendout_retract_thresh_pct,
        uint8_t &retract_hys_active,
        float &x,
        bool  &on_use_need_move,
        float &on_use_abs_err,
        bool  &on_use_linear
    )
    {
        const float hold_target = MC_LOAD_S2_HOLD_TARGET_PCT;

        float thresh = post_sendout_retract_thresh_pct;
        if (thresh < hold_target) thresh = hold_target;

        if (pct > thresh)
        {
            const float target = thresh;

            const float start_retract = target + 0.25f;
            const float stop_retract  = target + 0.00f;

            retract_hys_active = hyst_u8(retract_hys_active, pct, start_retract, stop_retract);

            if (!retract_hys_active)
            {
                x = 0.0f;
                PID_pressure.clear();
                on_use_need_move = false;
                on_use_abs_err   = 0.0f;
                on_use_linear    = false;
            }
            else
            {
                const float err = pct - target;
                on_use_need_move = true;
                on_use_abs_err   = err;
                on_use_linear    = false;

                const float mag = retract_mag_from_err(err, 850.0f);

                x = dir * mag;
                if (x * dir < 0.0f) x = 0.0f;
            }
        }
        else
        {
            retract_hys_active = 0;

            const float push_hi_pct    = hold_target - MC_LOAD_S2_HOLD_BAND_LO_DELTA;
            const float push_start_pct = MC_LOAD_S2_PUSH_START_PCT;
            const float pwm_hi         = MC_LOAD_S2_PWM_HI;
            const float pwm_lo         = MC_LOAD_S2_PWM_LO;
            const float slope          = (pwm_lo - pwm_hi) / (push_hi_pct - push_start_pct);

            if (pct >= push_hi_pct)
            {
                x = 0.0f;
                PID_pressure.clear();
                on_use_need_move = false;
                on_use_abs_err   = 0.0f;
                on_use_linear    = false;
            }
            else
            {
                float pwm;
                if (pct <= push_start_pct) pwm = pwm_lo;
                else                       pwm = pwm_hi + (push_hi_pct - pct) * slope;

                x = -dir * pwm;
                PID_pressure.clear();

                on_use_need_move = true;
                on_use_abs_err   = hold_target - pct;
                on_use_linear    = true;
            }
        }
    }

    void run(float time_E, uint64_t now_ms)
    {
        if (motion == filament_motion_enum::filament_motion_stop &&
            motor_stop_time == 0 &&
            pwm_zeroed)
            return;

        if (motion != filament_motion_enum::filament_motion_stop &&
            motor_stop_time != 0 &&
            now_ms > motor_stop_time)
        {
            if (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
                g_last_on_use_exit_ms[CHx] = now_ms;

            PID_speed.clear();
            PID_pressure.clear();
            pwm_zeroed = 1;
            x_prev[CHx] = 0.0f;
            motion = filament_motion_enum::filament_motion_stop;
            Motion_control_set_PWM(CHx, 0);
            return;
        }

        if (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use && g_on_use_low_latch[CHx])
        {
            g_on_use_hi_pwm_us[CHx] = 0u;
            PID_speed.clear();
            PID_pressure.clear();
            pwm_zeroed = 1;
            x_prev[CHx] = 0.0f;
            Motion_control_set_PWM(CHx, 0);
            return;
        }

        float speed_set = 0.0f;
        const float now_speed = speed_as5600[CHx];
        float x = 0.0f;
#if BMCU_DM_TWO_MICROSWITCH
        bool  dm_autoload_active = false;
        float dm_autoload_x      = 0.0f;
#endif

        const uint64_t t_exit  = g_last_on_use_exit_ms[CHx];
        const bool had_on_use  = (t_exit != 0);
        const bool has_exit_ts = (t_exit > 1);
        uint64_t dt_exit = 0;
        if (has_exit_ts) dt_exit = (now_ms - t_exit);

        const bool post_on_use_active =
            (motion == filament_motion_enum::filament_motion_pressure_ctrl_idle) &&
            (MC_ONLINE_key_stu[CHx] == 0) &&
            filament_channel_inserted[CHx] &&
            had_on_use;

        const bool post_on_use_10s  = post_on_use_active && has_exit_ts && (dt_exit < 10000ull);

        const bool on_use_like =
            (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use) ||
            (motion == filament_motion_enum::filament_motion_before_on_use) ||
            (motion == filament_motion_enum::filament_motion_stop_on_use) ||
            post_on_use_10s ||
            ((motion == filament_motion_enum::filament_motion_send) && send_stop_latch);

        bool  on_use_need_move = false;
        float on_use_abs_err   = 0.0f;
        bool  on_use_linear    = false;

        if (motion == filament_motion_enum::filament_motion_pressure_ctrl_idle)
        {
        #if BMCU_DM_TWO_MICROSWITCH

                    if (!bmcu_policy_autonomous_assist() ||
                            g_calibration_motion_inhibit)
                    {
                        dm_auto_state[CHx] = DM_AUTO_IDLE;
                        dm_auto_try[CHx] = 0u;
                        dm_auto_t0_ms[CHx] = 0ull;
                        dm_auto_remain_m[CHx] = 0.0f;
                        dm_autoload_active = false;
                        dm_autoload_x = 0.0f;
                    }
                    else if (filament_channel_inserted[CHx] &&
                             (dm_loaded[CHx] == 0u))
                    {
                        const uint8_t ks = MC_ONLINE_key_stu[CHx];
                        auto &A = ams[motion_control_ams_num];
                        const float cur_m = A.filament[CHx].meters;

                        if (dm_fail_latch[CHx])
                        {
                            dm_autoload_active = true;
                            dm_autoload_x = 0.0f;
                            MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);
                        }
                        else
                        {
                            if (dm_auto_state[CHx] == DM_AUTO_IDLE)
                            {
                                if (ks == 2u)
                                {
                                    if (dm_autoload_gate[CHx] == 0u)
                                    {
                                        dm_autoload_gate[CHx] = 1u;
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                }
                                else if (ks == 1u &&
                                         dm_autoload_gate[CHx] == 0u)
                                {
                                    dm_autoload_gate[CHx] = 1u;
                                    dm_auto_state[CHx]    = DM_AUTO_S2_PUSH;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = bmcu_config_autoload_len(CHx);
                                    dm_auto_last_m[CHx]   = cur_m;
                                }
                            }

                            switch (dm_auto_state[CHx])
                            {
                            case DM_AUTO_S1_DEBOUNCE:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks != 2u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0ull;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_DEBOUNCE_MS)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_S1_PUSH;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                break;

                            case DM_AUTO_S1_PUSH:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0ull;
                                }
                                else if (ks == 1u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_S2_PUSH;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = bmcu_config_autoload_len(CHx);
                                    dm_auto_last_m[CHx]   = cur_m;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_TIMEOUT_MS)
                                {
                                    dm_fail_latch[CHx] = 1u;
                                    dm_auto_state[CHx] = DM_AUTO_S1_FAIL_RETRACT;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                else
                                {
                                    dm_autoload_x = -dir * DM_AUTO_PWM_PUSH;
                                }
                                break;

                            case DM_AUTO_S1_FAIL_RETRACT:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0ull;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_FAIL_RETRACT_MS)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0ull;
                                }
                                else
                                {
                                    dm_autoload_x = dir * DM_AUTO_PWM_PULL;
                                }
                                break;

                            case DM_AUTO_S2_PUSH:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks != 1u)
                                {
                                    if (ks == 2u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                        dm_auto_try[CHx]      = 0u;
                                        dm_auto_remain_m[CHx] = 0.0f;
                                        dm_auto_t0_ms[CHx]    = 0ull;
                                    }
                                    break;
                                }

                                {
                                    const float moved = absf(cur_m - dm_auto_last_m[CHx]);
                                    dm_auto_last_m[CHx] = cur_m;

                                    float r = dm_auto_remain_m[CHx] - moved;
                                    if (r < 0.0f) r = 0.0f;
                                    dm_auto_remain_m[CHx] = r;
                                }

                                if (MC_PULL_pct_f[CHx] > DM_AUTO_BUF_ABORT_PCT)
                                {
                                    uint8_t t = dm_auto_try[CHx];
                                    if (t < 255u) t++;
                                    dm_auto_try[CHx] = t;

                                    dm_auto_last_m[CHx] = cur_m;

                                    if (t >= 3u)
                                    {
                                        dm_fail_latch[CHx] = 1u;
                                        dm_auto_state[CHx] = DM_AUTO_S2_FAIL_RETRACT;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S2_RETRACT;
                                    }

                                    MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);
                                }
                                else if (dm_auto_remain_m[CHx] <= 0.0f)
                                {
                                    dm_loaded[CHx] = 1u;

                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = 0.0f;
                                    dm_auto_t0_ms[CHx]    = 0ull;

                                    MC_STU_RGB_set(CHx, 0x38, 0x35, 0x32);
                                    dm_autoload_x = 0.0f;
                                }
                                else
                                {
                                    dm_autoload_x = -dir * DM_AUTO_PWM_PUSH;
                                }
                                break;

                            case DM_AUTO_S2_RETRACT:
                                dm_autoload_active = true;

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = 0.0f;
                                    dm_auto_t0_ms[CHx]    = 0ull;
                                    break;
                                }

                                {
                                    const float moved = absf(cur_m - dm_auto_last_m[CHx]);
                                    dm_auto_last_m[CHx] = cur_m;

                                    float r = dm_auto_remain_m[CHx] + moved;
                                    const float autoload_target = bmcu_config_autoload_len(CHx);
                                    if (r > autoload_target) r = autoload_target;
                                    dm_auto_remain_m[CHx] = r;
                                }

                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if ((MC_PULL_pct_f[CHx] <= DM_AUTO_BUF_RECOVER_PCT) || (ks == 2u))
                                {
                                    dm_auto_last_m[CHx] = cur_m;

                                    if (ks == 1u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S2_PUSH;
                                    }
                                    else if (ks == 2u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                        dm_auto_try[CHx]      = 0u;
                                        dm_auto_remain_m[CHx] = 0.0f;
                                        dm_auto_t0_ms[CHx]    = 0ull;
                                    }
                                    dm_autoload_x = 0.0f;
                                }
                                else
                                {
                                    dm_autoload_x = dir * DM_AUTO_PWM_PULL;
                                }
                                break;

                            case DM_AUTO_S2_FAIL_RETRACT:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = 0.0f;
                                    dm_auto_t0_ms[CHx]    = 0ull;
                                }
                                else if (ks == 2u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_S2_FAIL_EXTRA;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                else
                                {
                                    dm_autoload_x = dir * DM_AUTO_PWM_PULL;
                                }
                                break;

                            case DM_AUTO_S2_FAIL_EXTRA:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = 0.0f;
                                    dm_auto_t0_ms[CHx]    = 0ull;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_FAIL_EXTRA_MS)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_m[CHx] = 0.0f;
                                    dm_auto_t0_ms[CHx]    = 0ull;
                                }
                                else
                                {
                                    dm_autoload_x = dir * DM_AUTO_PWM_PULL;
                                }
                                break;

                            default:
                                dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                dm_auto_try[CHx]      = 0u;
                                dm_auto_remain_m[CHx] = 0.0f;
                                dm_auto_t0_ms[CHx]    = 0ull;
                                break;
                            }
                        }
                    }

                    if (dm_autoload_active)
                    {
                        x = dm_autoload_x;
                        PID_pressure.clear();
                        PID_speed.clear();
                    }
                    else
        #endif

            if (MC_ONLINE_key_stu[CHx] == 0)
            {
                if (!filament_channel_inserted[CHx] || !had_on_use)
                {
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0.0f;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }

                if (post_on_use_10s)
                {
                    if ((uint8_t)MC_PULL_pct[CHx] >= 49u)
                    {
                        x = 0.0f;
                        PID_pressure.clear();
                        on_use_need_move = false;
                        on_use_abs_err = 0.0f;
                    }
                    else
                    {
                        const float pct = MC_PULL_pct_f[CHx];
                        const float err = pct - 49.0f;

                        on_use_need_move = true;
                        on_use_abs_err   = -err;

                        x = dir * PID_pressure.caculate(err, time_E);

                        float lim_f = 500.0f + 80.0f * on_use_abs_err;
                        if (lim_f > 900.0f) lim_f = 900.0f;

                        if (x >  lim_f) x =  lim_f;
                        if (x < -lim_f) x = -lim_f;
                        if (x * dir > 0.0f)
                        {
                            x = 0.0f;
                            PID_pressure.clear();
                            on_use_need_move = false;
                            on_use_abs_err   = 0.0f;
                        }
                    }
                }
                else
                {

                    if (MC_PULL_stu[CHx] != 0)
                    {
                        const float pct = MC_PULL_pct_f[CHx];
                        x = dir * PID_pressure.caculate(pct - 50.0f, time_E);
                    }
                    else
                    {
                        x = 0.0f;
                        PID_pressure.clear();
                    }
                }
            }
            else
            {

                if (MC_PULL_stu[CHx] != 0)
                {
                    const float pct = MC_PULL_pct_f[CHx];
                    x = dir * PID_pressure.caculate(pct - 50.0f, time_E);
                }
                else
                {
                    x = 0.0f;
                    PID_pressure.clear();
                }
            }
        }
        else if (motion == filament_motion_enum::filament_motion_redetect)
        {
            x = -dir * 900.0f;
        }
        else if (MC_ONLINE_key_stu[CHx] != 0)
        {
            if (motion == filament_motion_enum::filament_motion_before_pull_back)
            {
                const float pct = MC_PULL_pct_f[CHx];
                const float target = bmcu_config_before_pullback_target_pct();
                constexpr float band = 1.0f;

                if (pct >= (target - band) && pct <= (target + band))
                {
                    x = 0.0f;
                    PID_pressure.clear();
                    on_use_need_move = false;
                    on_use_abs_err   = 0.0f;
                }
                else if (pct < (target - band))
                {

                    const float err = target - pct;
                    on_use_need_move = true;
                    on_use_abs_err   = err;
                    float mag = 380.0f + 80.0f * err;
                    if (mag > 900.0f) mag = 900.0f;
                    x = -dir * mag;
                    PID_pressure.clear();
                }
                else
                {

                    const float err = pct - target;
                    on_use_need_move = true;
                    on_use_abs_err   = err;
                    const float mag = retract_mag_from_err(err, 850.0f);
                    x = dir * mag;
                    if (x * dir < 0.0f) x = 0.0f;
                }
            }
            else if (motion == filament_motion_enum::filament_motion_before_on_use)
            {
                const float pct = MC_PULL_pct_f[CHx];

                hold_load(
                    pct,
                    dir,
                    PID_pressure,
                    post_sendout_retract_thresh_pct,
                    retract_hys_active,
                    x,
                    on_use_need_move,
                    on_use_abs_err,
                    on_use_linear
                );
            }
            else if (motion == filament_motion_enum::filament_motion_stop_on_use)
            {
                PID_pressure.clear();
                pwm_zeroed = 1;
                x_prev[CHx] = 0.0f;
                Motion_control_set_PWM(CHx, 0);
                return;
            }
            else if (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
            {
                const float pct = MC_PULL_pct_f[CHx];

                const float target_pct = MC_ON_USE_TARGET_PCT;
                const float band_hi    = MC_ON_USE_BAND_HI_PCT;

                bool handoff_active = false;
                if (on_use_handoff_ceiling_pct >= 0.0f)
                {
                    const bool timed_out =
                        (on_use_handoff_t0_ms == 0ull) ||
                        ((now_ms - on_use_handoff_t0_ms) >= ON_USE_HANDOFF_MS);
                    const bool reached_target = pct <= target_pct;

                    if (timed_out || reached_target)
                    {
                        on_use_handoff_ceiling_pct = -1.0f;
                        on_use_handoff_t0_ms = 0ull;
                    }
                    else
                    {
                        handoff_active = true;
                    }
                }

                const float band_hi_eff = handoff_active
                    ? (on_use_handoff_ceiling_pct + ON_USE_HANDOFF_CEILING_HYS_PCT)
                    : band_hi;

                constexpr float pwm_lo          = 380.0f;
                constexpr float pct_fast_onuse  = 50.0f;
                constexpr float pwm_fast_onuse  = 900.0f;
                constexpr float pwm_cap         = 900.0f;

                const float slope =
                    (pwm_fast_onuse - pwm_lo) / ((target_pct - MC_ON_USE_BAND_LO_DELTA) - pct_fast_onuse);

                retract_hys_active = 0;

                if (pct >= (target_pct - MC_ON_USE_BAND_LO_DELTA) && pct <= band_hi_eff)
                {
                    x = 0.0f;
                    PID_pressure.clear();
                    on_use_need_move = false;
                    on_use_abs_err   = 0.0f;
                }
                else if (pct < (target_pct - MC_ON_USE_BAND_LO_DELTA))
                {
                    const float err = pct - target_pct;
                    on_use_need_move = true;
                    on_use_abs_err   = -err;

                    float pwm;
                    if (pct >= pct_fast_onuse)
                        pwm = pwm_lo + ((target_pct - MC_ON_USE_BAND_LO_DELTA) - pct) * slope;
                    else
                        pwm = pwm_fast_onuse + (pct_fast_onuse - pct) * slope;

                    if (pwm > pwm_cap) pwm = pwm_cap;

                    x = -dir * pwm;
                    PID_pressure.clear();
                    on_use_linear = true;
                }
                else
                {
                    on_use_need_move = true;

                    const float control_target = handoff_active
                        ? on_use_handoff_ceiling_pct : target_pct;
                    const float err = pct - control_target;
                    on_use_abs_err = (err < 0.0f) ? -err : err;

                    x = dir * PID_pressure.caculate(err, time_E);

                    float lim_f = 500.0f + 80.0f * on_use_abs_err;
                    if (lim_f > 900.0f) lim_f = 900.0f;

                    if (x >  lim_f) x =  lim_f;
                    if (x < -lim_f) x = -lim_f;

                    constexpr float retrig = 55.0f;
                    if (!handoff_active && err > 0.0f && pct >= retrig)
                    {
                        float mul = 1.0f + 0.5f * (pct - retrig);
                        if (mul > 3.0f) mul = 3.0f;
                        x *= mul;
                        if (x >  950.0f) x =  950.0f;
                        if (x < -950.0f) x = -950.0f;
                    }
                }
            }
            else
            {
                if (motion == filament_motion_enum::filament_motion_stop)
                {
                    PID_speed.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0.0f;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }

                bool do_speed_pid = true;

                if (motion == filament_motion_enum::filament_motion_send)
                {
                    const float pct = MC_PULL_pct_f[CHx];

                    if (!send_len_abort)
                    {
                        constexpr float SEND_MAX_M = 10.0f;
                        const float moved_m = absf(ams[motion_control_ams_num].filament[CHx].meters - send_start_m);
                        if (moved_m >= SEND_MAX_M) send_len_abort = 1;
                    }

                    if (send_len_abort)
                    {
                        PID_speed.clear();
                        PID_pressure.clear();
                        pwm_zeroed = 1;
                        x_prev[CHx] = 0.0f;
                        Motion_control_set_PWM(CHx, 0);
                        return;
                    }

                    if (pct >= (float)MC_LOAD_S1_HARD_STOP_PCT)
                    {
                        send_hard = true;
                        PID_speed.clear();
                        PID_pressure.clear();
                        pwm_zeroed = 1;
                        x_prev[CHx] = 0.0f;
                        Motion_control_set_PWM(CHx, 0);
                        return;
                    }

                    if (send_hard)
                    {
                        if (pct >= (float)(MC_LOAD_S1_HARD_STOP_PCT - MC_LOAD_S1_HARD_HYS))
                        {
                            PID_speed.clear();
                            PID_pressure.clear();
                            pwm_zeroed = 1;
                            x_prev[CHx] = 0.0f;
                            Motion_control_set_PWM(CHx, 0);
                            return;
                        }
                        send_hard = false;
                    }

                    if (!send_stop_latch && (pct >= (float)MC_LOAD_S1_FAST_PCT))
                    {
                        send_stop_latch = true;

                        float p = pct;
                        if (p < 0.0f) p = 0.0f;
                        if (p > 100.0f) p = 100.0f;

                        post_sendout_retract_thresh_pct = p;
                        retract_hys_active = 0;

                        PID_speed.clear();
                        PID_pressure.clear();
                    }

                    if (send_stop_latch)
                    {
                        do_speed_pid = false;

                        hold_load(
                            pct,
                            dir,
                            PID_pressure,
                            post_sendout_retract_thresh_pct,
                            retract_hys_active,
                            x,
                            on_use_need_move,
                            on_use_abs_err,
                            on_use_linear
                        );
                    }
                    else
                    {
                        constexpr uint64_t SEND_SOFTSTART_MS = 300ull;
                        const float V = bmcu_config_load_speed();
                        const float V0 = (V < 10.0f) ? V : 10.0f;

                        const uint64_t dt = (send_start_ms != 0) ? (now_ms - send_start_ms) : 1000000ull;

                        if (dt < SEND_SOFTSTART_MS)
                        {
                            float t = (float)dt / (float)SEND_SOFTSTART_MS;
                            if (t < 0.0f) t = 0.0f;
                            if (t > 1.0f) t = 1.0f;
                            speed_set = V0 + (V - V0) * t;
                        }
                        else
                        {
                            speed_set = V;
                        }
                    }
                }

                if (motion == filament_motion_enum::filament_motion_pull)
                {
                    speed_set = g_pull_speed_set[CHx];
                }

                if (do_speed_pid)
                    x = dir * PID_speed.caculate(now_speed - speed_set, time_E);
            }
        }
        else
        {
            x = 0.0f;
        }

        const bool pull_mode = (motion == filament_motion_enum::filament_motion_pull);
        const bool pb_mode = (motion == filament_motion_enum::filament_motion_before_pull_back);

        const bool send_stop_hold_mode =
            (motion == filament_motion_enum::filament_motion_send) && send_stop_latch;

        const bool hold_mode =
            (motion == filament_motion_enum::filament_motion_pressure_ctrl_idle) ||
            (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use) ||
            (motion == filament_motion_enum::filament_motion_before_on_use) ||
            (motion == filament_motion_enum::filament_motion_stop_on_use) ||
            post_on_use_active ||
            send_stop_hold_mode;

        const int deadband =
            pb_mode ? 0 :
            (hold_mode ? 1 : (pull_mode ? 2 : 10));

        float pwm0 =
            pb_mode ? 0.0f :
            (hold_mode ? 420.0f : pwm_zero);

        if (pull_mode)
        {
            float k = g_pull_remain_m[CHx] / PULL_RAMP_M;
            k = clampf(k, 0.0f, 1.0f);

            pwm0 = PULL_PWM_MIN + (pwm_zero - PULL_PWM_MIN) * k;

            if (pwm0 < PULL_PWM_MIN) pwm0 = PULL_PWM_MIN;
        }

        if (x > (float)deadband)
        {
            if (x < pwm0) x = pwm0;
        }
        else if (x < (float)-deadband)
        {
            if (-x < pwm0) x = -pwm0;
        }
        else
        {
            x = 0.0f;
        }

        if (motion == filament_motion_enum::filament_motion_pressure_ctrl_idle)
        {
        #if BMCU_DM_TWO_MICROSWITCH
            const float lim = dm_autoload_active ? DM_AUTO_IDLE_LIM : 800.0f;
            if (x >  lim) x =  lim;
            if (x < -lim) x = -lim;
        #else
            constexpr float PWM_IDLE_LIM = 800.0f;
            if (x >  PWM_IDLE_LIM) x =  PWM_IDLE_LIM;
            if (x < -PWM_IDLE_LIM) x = -PWM_IDLE_LIM;
        #endif
        }
        else
        {
            if (x >  (float)PWM_lim) x =  (float)PWM_lim;
            if (x < (float)-PWM_lim) x = (float)-PWM_lim;
        }

        static float    stall_s[4] = {0,0,0,0};
        static uint64_t block_until_ms[4] = {0,0,0,0};

        if (on_use_like)
        {
            if (now_ms < block_until_ms[CHx])
            {
                PID_pressure.clear();
                pwm_zeroed = 1;
                x_prev[CHx] = 0.0f;
                Motion_control_set_PWM(CHx, 0);
                return;
            }

            if (on_use_need_move && x != 0.0f)
            {
                if (!on_use_linear)
                {
                    const int MIN_MOVE_PWM = (on_use_abs_err >= 1.3f) ? 500 : 0;
                    if (MIN_MOVE_PWM)
                    {
                        int xi = (int)(x + ((x >= 0.0f) ? 0.5f : -0.5f));
                        const int ax = (xi < 0) ? -xi : xi;
                        if (ax < MIN_MOVE_PWM)
                            x = (x > 0.0f) ? (float)MIN_MOVE_PWM : (float)-MIN_MOVE_PWM;
                    }
                }
            }

            const bool motor_not_moving = (absf(now_speed) < 1.0f);

            if (on_use_need_move && motor_not_moving && (on_use_abs_err >= 2.0f) && (absf(x) >= 450.0f))
            {
                stall_s[CHx] += time_E;

                if (stall_s[CHx] > 0.15f)
                {
                    const float KICK_PWM = 850.0f;
                    x = (x > 0.0f) ? KICK_PWM : -KICK_PWM;
                }

                if (stall_s[CHx] > 0.8f)
                {
                    stall_s[CHx] = 0.0f;
                    block_until_ms[CHx] = now_ms + 500;
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0.0f;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }
            }
            else
            {
                stall_s[CHx] = 0.0f;
            }
        }
        else
        {
            stall_s[CHx] = 0.0f;
            block_until_ms[CHx] = 0ull;
        }

        if (motion == filament_motion_enum::filament_motion_redetect)
        {
            const int pwm_out = (int)x;
            pwm_zeroed = (pwm_out == 0);
            x_prev[CHx] = x;
            Motion_control_set_PWM(CHx, pwm_out);
            return;
        }

        const bool use_ramping =
            ((motion == filament_motion_enum::filament_motion_send) && !send_stop_latch) ||
            (motion == filament_motion_enum::filament_motion_pull);

        if (use_ramping)
        {
            const bool pull_soft_start =
                (motion == filament_motion_enum::filament_motion_pull) &&
                (pull_start_ms != 0) &&
                ((now_ms - pull_start_ms) < 400ull);

            float rate_up   = 4500.0f;
            float rate_down = 6500.0f;

            if (pull_soft_start) rate_up = 2500.0f;

            if (motion == filament_motion_enum::filament_motion_send)
            {
                rate_down = 25000.0f;
                rate_up   = 18000.0f;
            }

            const float max_step_up   = rate_up   * time_E;
            const float max_step_down = rate_down * time_E;

            const float prev = x_prev[CHx];
            const float lo = prev - max_step_down;
            const float hi = prev + max_step_up;

            if (x < lo) x = lo;
            if (x > hi) x = hi;
        }

        const int pwm_out0 = (int)x;

#if BMCU_KLIPPER_HOST_JAM
        g_on_use_hi_pwm_us[CHx] = 0u;
#else
        if (motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use && !g_on_use_low_latch[CHx])
        {
            if (MC_ONLINE_key_stu[CHx] == 0u)
            {
                g_on_use_hi_pwm_us[CHx] = 0u;
            }
            else
            {
                const float pct = MC_PULL_pct_f[CHx];

                if (pct < 40.0f)
                {
                    g_on_use_low_latch[CHx] = 1u;
                    g_on_use_jam_latch[CHx] = 1u;
                }
                else
                {
                    const int pwm_cmd = pwm_out0;
                    const int ax = (pwm_cmd < 0) ? -pwm_cmd : pwm_cmd;

                    const bool push_hi =
                        (dir != 0.0f) &&
                        (((float)pwm_cmd) * dir < 0.0f) &&
                        (ax > 800);

                    if (push_hi)
                    {
                        const uint32_t add_us = (uint32_t)(time_E * 1000000.0f + 0.5f);

                        uint32_t t1 = g_on_use_hi_pwm_us[CHx] + add_us;
                        if (t1 > 20000000u) t1 = 20000000u;
                        g_on_use_hi_pwm_us[CHx] = t1;

                        if (t1 >= 20000000u)
                        {
                            g_on_use_low_latch[CHx] = 1u;
                            g_on_use_jam_latch[CHx] = 0u;
                        }
                    }
                    else
                    {
                        g_on_use_hi_pwm_us[CHx] = 0u;
                    }
                }

                if (g_on_use_low_latch[CHx])
                {
                    g_on_use_hi_pwm_us[CHx] = 0u;

                    MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                    PID_speed.clear();
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0.0f;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }
            }
        }
        else
        {
            g_on_use_hi_pwm_us[CHx] = 0u;
        }
#endif

        const int pwm_out = pwm_out0;
        pwm_zeroed = (pwm_out == 0);
        x_prev[CHx] = x;
        Motion_control_set_PWM(CHx, pwm_out);
    }
};

_MOTOR_CONTROL MOTOR_CONTROL[4] = {_MOTOR_CONTROL(0), _MOTOR_CONTROL(1), _MOTOR_CONTROL(2), _MOTOR_CONTROL(3)};

bool Motion_control_start_channel_retract(uint8_t channel)
{
    if (channel >= kChCount || !g_host_motion_enabled ||
        ams_state_get_route_state(channel) != AMS_ROUTE_EMPTY ||
        !filament_channel_inserted[channel] ||
        MC_ONLINE_key_stu[channel] == 0u ||
        !MC_PULL_calibration_is_valid(channel) ||
        !AS5600_is_good(channel) ||
        MOTOR_CONTROL[channel].motion !=
            filament_motion_enum::filament_motion_pressure_ctrl_idle ||
        auto_unload_active[channel])
        return false;

    auto_unload_arm[channel] = 0u;
    auto_unload_active[channel] = 1u;
    auto_unload_explicit[channel] = 1u;
    auto_unload_blocked[channel] = 1u;
    auto_unload_arm_t0_ms[channel] = 0ull;
    auto_unload_active_t0_ms[channel] =
        time_ms_fast_from_ticks64(time_ticks64());
    auto_unload_empty_t0_ms[channel] = 0ull;
    auto_unload_rearm_t0_ms[channel] = 0ull;
    auto_unload_start_m[channel] =
        ams[motion_control_ams_num].filament[channel].meters;
    return true;
}

bool Motion_control_channel_retract_active(uint8_t channel)
{
    return channel < kChCount && auto_unload_active[channel] != 0u;
}

void Motion_control_cancel_channel_retract(uint8_t channel)
{
    if (channel >= kChCount) return;
    auto_unload_reset(channel, 1u);
    MOTOR_CONTROL[channel].PID_speed.clear();
    MOTOR_CONTROL[channel].PID_pressure.clear();
    MOTOR_CONTROL[channel].pwm_zeroed = 1u;
    _MOTOR_CONTROL::x_prev[channel] = 0.0f;
    Motion_control_set_PWM(channel, 0);
}

void Motion_control_cancel_all_channel_retracts(void)
{
    for (uint8_t channel = 0u; channel < kChCount; channel++)
        Motion_control_cancel_channel_retract(channel);
}

void Motion_control_clear_faults(void)
{
    Motion_control_cancel_all_channel_retracts();
    const uint64_t now_ms = time_ms_fast_from_ticks64(time_ticks64());
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        g_on_use_low_latch[ch] = 0u;
        g_on_use_jam_latch[ch] = 0u;
        g_on_use_hi_pwm_us[ch] = 0u;
        MOTOR_CONTROL[ch].PID_pressure.clear();
        MOTOR_CONTROL[ch].set_motion(filament_motion_enum::filament_motion_stop, 100u, now_ms);
        Motion_control_set_PWM(ch, 0);
    }
}
float _MOTOR_CONTROL::x_prev[4] = {0,0,0,0};

int16_t Motion_control_get_pwm(uint8_t CHx)
{
    if (CHx >= 4) return 0;
    float v = _MOTOR_CONTROL::x_prev[CHx];
    if (v > 32767.0f) v = 32767.0f;
    if (v < -32768.0f) v = -32768.0f;
    return (int16_t)v;
}

static void motion_control_force_all_motors_off(void)
{
    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        MOTOR_CONTROL[ch].PID_speed.clear();
        MOTOR_CONTROL[ch].PID_pressure.clear();
        MOTOR_CONTROL[ch].pwm_zeroed = 1u;
        _MOTOR_CONTROL::x_prev[ch] = 0.0f;
        Motion_control_set_PWM(ch, 0);
    }
}

void Motion_control_set_host_motion_enabled(bool enabled)
{
    if (enabled)
    {

        if (g_host_motion_requested) return;
        g_host_motion_requested = true;
        g_host_motion_enabled = false;
        g_host_motion_sample_valid = false;
        g_host_motion_stable_t0_ms = 0ull;
        motion_control_force_all_motors_off();
        return;
    }

    g_host_motion_requested = false;
    g_host_motion_enabled = false;
    g_host_motion_sample_valid = false;
    g_host_motion_stable_t0_ms = 0ull;
    motion_control_force_all_motors_off();
}

static void motion_control_service_host_motion_gate(uint64_t now_ms)
{
    if (!g_host_motion_requested || g_host_motion_enabled) return;

    bool stable = g_host_motion_sample_valid;
    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const uint8_t key = MC_ONLINE_key_stu[ch];
        const float pct = MC_PULL_pct_f[ch];

        if (g_host_motion_sample_valid &&
            (key != g_host_motion_last_key[ch] ||
             absf(pct - g_host_motion_last_pct[ch]) >
                HOST_MOTION_STABLE_DELTA_PCT))
            stable = false;

        if (filament_channel_inserted[ch] &&
            MC_PULL_calibration_is_valid(ch) &&
            (pct < HOST_MOTION_NEUTRAL_LO_PCT ||
             pct > HOST_MOTION_NEUTRAL_HI_PCT))
            stable = false;

        g_host_motion_last_key[ch] = key;
        g_host_motion_last_pct[ch] = pct;
    }

    if (!g_host_motion_sample_valid)
    {
        g_host_motion_sample_valid = true;
        g_host_motion_stable_t0_ms = 0ull;
        motion_control_force_all_motors_off();
        return;
    }

    if (!stable)
    {
        g_host_motion_stable_t0_ms = 0ull;
        motion_control_force_all_motors_off();
        return;
    }

    if (g_host_motion_stable_t0_ms == 0ull)
    {
        g_host_motion_stable_t0_ms = now_ms;
        motion_control_force_all_motors_off();
        return;
    }

    if ((now_ms - g_host_motion_stable_t0_ms) < HOST_MOTION_SETTLE_MS)
    {
        motion_control_force_all_motors_off();
        return;
    }

    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        auto_unload_reset(ch, 1u);
#if BMCU_DM_TWO_MICROSWITCH
        dm_auto_state[ch] = DM_AUTO_IDLE;
        dm_auto_try[ch] = 0u;
        dm_auto_t0_ms[ch] = 0ull;
        dm_auto_remain_m[ch] = 0.0f;
        dm_auto_last_m[ch] = 0.0f;
        const uint8_t key = MC_ONLINE_key_stu[ch];
        const bool loaded = filament_channel_inserted[ch] && key == 1u;

        dm_loaded[ch] = loaded ? 1u : 0u;
        dm_loaded_drop_t0_ms[ch] = 0ull;
        dm_autoload_gate[ch] = (key != 0u) ? 1u : 0u;
#endif
    }
    g_host_motion_enabled = true;
}

void Motion_control_set_PWM(uint8_t CHx, int PWM)
{
    if (!g_host_motion_enabled && PWM != 0) PWM = 0;
    uint16_t set1 = 0, set2 = 0;

    if (PWM > 0)       set1 = (uint16_t)PWM;
    else if (PWM < 0)  set2 = (uint16_t)(-PWM);
    else { set1 = 1000; set2 = 1000; }

    switch (CHx)
    {
    case 3:
        TIM_SetCompare1(TIM2, set1);
        TIM_SetCompare2(TIM2, set2);
        break;
    case 2:
        TIM_SetCompare1(TIM3, set1);
        TIM_SetCompare2(TIM3, set2);
        break;
    case 1:
        TIM_SetCompare1(TIM4, set1);
        TIM_SetCompare2(TIM4, set2);
        break;
    case 0:
        TIM_SetCompare3(TIM4, set1);
        TIM_SetCompare4(TIM4, set2);
        break;
    default:
        break;
    }
}

int32_t as5600_distance_save[4] = {0,0,0,0};

static inline int calibration_angle_delta(int16_t angle1, int16_t angle2)
{
    int delta = (int)angle1 - (int)angle2;
    if (delta > 2048) delta -= 4096;
    if (delta < -2048) delta += 4096;
    return delta;
}

bool Motion_control_calibrate_motor_encoder(uint8_t selected_mask,
                                            int8_t directions[4])
{
    selected_mask &= 0x0Fu;
    if (!selected_mask || !directions) return false;

    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const int saved = Motion_control_data_save.Motion_control_dir[ch];
        directions[ch] = saved < 0 ? -1 : (saved > 0 ? 1 : 0);
        Motion_control_set_PWM(ch, 0);
    }

    MC_AS5600.updata_stu();
    MC_AS5600.updata_angle();

    static const int TEST_PWM = 650;
    static const int TEST_COUNTS = 64;
    static const int RETURN_COUNTS = 20;
    static const uint16_t TEST_STEPS = 40u;
    static const uint16_t RETURN_STEPS = 40u;

    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const uint8_t bit = (uint8_t)(1u << ch);
        if (!(selected_mask & bit) || !filament_channel_inserted[ch]) continue;

        MC_AS5600.updata_stu();
        MC_AS5600.updata_angle();
        if (!MC_AS5600.online[ch] ||
            MC_AS5600.magnet_stu[ch] == AS5600_soft_IIC_many::offline)
            return false;

        const int16_t start = (int16_t)MC_AS5600.raw_angle[ch];
        int movement = 0;
        Motion_control_set_PWM(ch, TEST_PWM);
        for (uint16_t step = 0u; step < TEST_STEPS; step++)
        {
            delay(10);
            MC_AS5600.updata_stu();
            MC_AS5600.updata_angle();
            if (!MC_AS5600.online[ch] ||
                MC_AS5600.magnet_stu[ch] == AS5600_soft_IIC_many::offline)
                break;
            movement = calibration_angle_delta(
                (int16_t)MC_AS5600.raw_angle[ch], start);
            if (movement >= TEST_COUNTS || movement <= -TEST_COUNTS) break;
        }
        Motion_control_set_PWM(ch, 0);

        if (movement < TEST_COUNTS && movement > -TEST_COUNTS)
        {
            for (uint8_t stop = 0u; stop < kChCount; stop++)
                Motion_control_set_PWM(stop, 0);
            return false;
        }
        directions[ch] = movement > 0 ? 1 : -1;

        Motion_control_set_PWM(ch, -TEST_PWM);
        for (uint16_t step = 0u; step < RETURN_STEPS; step++)
        {
            delay(10);
            MC_AS5600.updata_angle();
            const int remaining = calibration_angle_delta(
                (int16_t)MC_AS5600.raw_angle[ch], start);
            if (remaining <= RETURN_COUNTS && remaining >= -RETURN_COUNTS) break;
        }
        Motion_control_set_PWM(ch, 0);
        delay(20);
        MC_AS5600.updata_stu();
        MC_AS5600.updata_angle();
        if (!MC_AS5600.online[ch] ||
            MC_AS5600.magnet_stu[ch] == AS5600_soft_IIC_many::offline)
            return false;

        g_as5600_good[ch] = 1u;
        g_as5600_fail[ch] = 0u;
        g_as5600_okstreak[ch] = kAS5600_OK_RECOVER;
        as5600_distance_save[ch] = MC_AS5600.raw_angle[ch];
        speed_as5600[ch] = 0.0f;
    }
    return true;
}

bool Motion_control_commit_hardware_calibration(
    uint8_t selected_mask, const uint8_t detector_none_cv[4],
    const int8_t directions[4])
{
    selected_mask &= 0x0Fu;
    if (!selected_mask || !detector_none_cv || !directions) return false;

    const BmcuMotionNvm previous = g_bmcu_nvm;
    float previous_thresholds[4];
    for (uint8_t ch = 0u; ch < kChCount; ch++)
        previous_thresholds[ch] = MC_DM_KEY_NONE_THRESH[ch];

    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const uint8_t bit = (uint8_t)(1u << ch);
        if (!(selected_mask & bit) || !filament_channel_inserted[ch]) continue;
        if ((directions[ch] != -1 && directions[ch] != 1) ||
            detector_none_cv[ch] < 60u || detector_none_cv[ch] > 139u)
            return false;
    }
    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const uint8_t bit = (uint8_t)(1u << ch);
        if (!(selected_mask & bit) || !filament_channel_inserted[ch]) continue;
        g_bmcu_nvm.Motion_control_dir[ch] = directions[ch];
        g_bmcu_nvm.dm_key_none_cv[ch] = detector_none_cv[ch];
        MC_DM_KEY_NONE_THRESH[ch] = 0.01f * (float)detector_none_cv[ch];
    }
    g_bmcu_nvm.check = 0x40614061u;

    if (!bmcu_config_save())
    {
        g_bmcu_nvm = previous;
        for (uint8_t ch = 0u; ch < kChCount; ch++)
            MC_DM_KEY_NONE_THRESH[ch] = previous_thresholds[ch];
        return false;
    }

    for (uint8_t ch = 0u; ch < kChCount; ch++)
    {
        const uint8_t bit = (uint8_t)(1u << ch);
        if (!(selected_mask & bit) || !filament_channel_inserted[ch]) continue;
        MOTOR_CONTROL[ch].dir = (float)directions[ch];
    }
    return true;
}

void AS5600_distance_updata(uint32_t now_ticks)
{
    static uint32_t last_ticks = 0u;
    static uint32_t last_poll_ticks = 0u;
    static uint8_t  have_last_ticks = 0u;
    static uint8_t  was_ok[4] = {0,0,0,0};
    static uint32_t last_stu_ticks = 0u;

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    uint32_t tpus = time_hw_tpus;
    if (!tpus) tpus = 1u;

    uint32_t min_poll_ticks = tpm;
    if ((uint32_t)(now_ticks - last_poll_ticks) < min_poll_ticks)
        return;

    last_poll_ticks = now_ticks;

    if ((uint32_t)(now_ticks - last_stu_ticks) >= (200u * tpm))
    {
        last_stu_ticks = now_ticks;
        MC_AS5600.updata_stu();
    }

    if (!have_last_ticks)
    {
        last_ticks = now_ticks;
        have_last_ticks = 1u;
        return;
    }

    const uint32_t dt_ticks = (uint32_t)(now_ticks - last_ticks);
    if (dt_ticks == 0u) return;
    last_ticks = now_ticks;

    static uint32_t inv_dt_ticks_cache = 0u;
    static uint32_t inv_dt_tpus_cache = 0u;
    static float inv_dt_cache = 0.0f;
    if (dt_ticks != inv_dt_ticks_cache || tpus != inv_dt_tpus_cache)
    {
        inv_dt_ticks_cache = dt_ticks;
        inv_dt_tpus_cache = tpus;
        inv_dt_cache = (1000000.0f * (float)tpus) / (float)dt_ticks;
    }
    const float inv_dt = inv_dt_cache;

    MC_AS5600.updata_angle();
    auto &A = ams[motion_control_ams_num];

    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ok_now = MC_AS5600.online[i] && (MC_AS5600.magnet_stu[i] != AS5600_soft_IIC_many::offline);

        if (ok_now)
        {
            g_as5600_fail[i] = 0;
            if (g_as5600_okstreak[i] < 255u) g_as5600_okstreak[i]++;
            if (g_as5600_okstreak[i] >= kAS5600_OK_RECOVER) g_as5600_good[i] = 1u;
        }
        else
        {
            g_as5600_okstreak[i] = 0u;
            if (g_as5600_fail[i] < 255u) g_as5600_fail[i]++;
            if (g_as5600_fail[i] >= kAS5600_FAIL_TRIP) g_as5600_good[i] = 0u;
        }

        if (!AS5600_is_good(i))
        {
            was_ok[i] = 0u;
            speed_as5600[i] = 0.0f;
            continue;
        }

        if (!was_ok[i])
        {
            as5600_distance_save[i] = MC_AS5600.raw_angle[i];
            speed_as5600[i] = 0.0f;
            was_ok[i] = 1u;
            continue;
        }

        const int32_t last = as5600_distance_save[i];
        const int32_t now  = MC_AS5600.raw_angle[i];

        int32_t diff = now - last;
        if (diff > 2048) diff -= 4096;
        if (diff < -2048) diff += 4096;

        as5600_distance_save[i] = now;

        const float dist_mm = (float)diff * kAS5600_MM_PER_CNT;
        speed_as5600[i] = dist_mm * inv_dt;
        A.filament[i].meters += dist_mm * 0.001f;
    }
}

enum filament_now_position_enum
{
    filament_idle,
    filament_sending_out,
    filament_using,
    filament_before_pull_back,
    filament_pulling_back,
    filament_redetect,
};

static filament_now_position_enum filament_now_position[4];
static float filament_pull_back_meters[4];

static float filament_pull_back_target[4] = {0.200f, 0.200f, 0.200f, 0.200f};

static bool motor_motion_filamnet_pull_back_to_online_key(uint64_t time_now)
{
    bool wait = false;
    auto &A = ams[motion_control_ams_num];

    for (uint8_t i = 0; i < kChCount; i++)
    {
        switch (filament_now_position[i])
        {
        case filament_pulling_back:
        {
            MC_STU_STATUS_set_latch(i, BMCU_LED_PULLBACK, time_now, 1u);

            const float target = filament_pull_back_target[i];
            const float d = filament_pull_back_meters[i] - A.filament[i].meters;

            if (target <= 0.0f || d >= target)
            {
                g_pull_remain_m[i]  = 0.0f;
                g_pull_speed_set[i] = -PULL_V_FAST;
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_pull_back_target[i] = motion_control_pull_back_distance(i);
                filament_now_position[i] = filament_redetect;
            }
            else if (MC_ONLINE_key_stu[i] == 0)
            {
                g_pull_remain_m[i]  = 0.0f;
                g_pull_speed_set[i] = -PULL_V_FAST;
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_pull_back_target[i] = motion_control_pull_back_distance(i);
                filament_now_position[i] = filament_redetect;
            }
            else
            {
                const float remain = target - d;
                g_pull_remain_m[i] = (remain > 0.0f) ? remain : 0.0f;

                float k = g_pull_remain_m[i] / PULL_RAMP_M;
                k = clampf(k, 0.0f, 1.0f);

                const float v = PULL_V_END + (PULL_V_FAST - PULL_V_END) * k;
                g_pull_speed_set[i] = -v;

                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_pull, 100, time_now);
            }

            wait = true;
            break;
        }

        case filament_redetect:
        {
            MC_STU_STATUS_set_latch(i, BMCU_LED_REDETECT, time_now, 0u);

            if (MC_ONLINE_key_stu[i] == 0)
            {
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_redetect, 100, time_now);
            }
            else
            {
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_now_position[i] = filament_idle;

                A.filament_use_flag = 0x00;
                A.filament[i].motion = _filament_motion::idle;
            }

            wait = true;
            break;
        }

        default:
            break;
        }
    }

    return wait;
}

static void motor_motion_switch(uint64_t time_now)
{
    auto &A = ams[motion_control_ams_num];

    const uint8_t num = A.now_filament_num;
    const _filament_motion motion = (num < kChCount) ? A.filament[num].motion : _filament_motion::idle;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (i != num)
        {
            filament_now_position[i] = filament_idle;

            if (filament_channel_inserted[i] && (MC_ONLINE_key_stu[i] != 0 || g_last_on_use_exit_ms[i] != 0))
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 1000, time_now);
            else
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 1000, time_now);

#if BMCU_DM_TWO_MICROSWITCH
            if (dm_fail_latch[i])
                MC_STU_STATUS_set_latch(
                    i, BMCU_LED_ERROR, time_now, 0u);
            else if (filament_channel_inserted[i] && dm_loaded[i])
                MC_STU_STATUS_set_latch(
                    i, BMCU_LED_IDLE, time_now, 0u);
            else
                MC_STU_STATUS_set_latch(
                    i, BMCU_LED_EMPTY, time_now, 0u);
#else
            MC_STU_STATUS_set_latch(
                i,
                MC_ONLINE_key_stu[i] != 0u
                    ? BMCU_LED_IDLE : BMCU_LED_EMPTY,
                time_now, 0u);
#endif
            continue;
        }

        if (num >= kChCount) continue;

        if (MC_ONLINE_key_stu[num] != 0)
        {
            switch (motion)
            {
            case _filament_motion::before_on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_before_on_use, 300, time_now);
                MC_STU_STATUS_set_latch(num, BMCU_LED_BEFORE_LOAD, time_now, 0u);
                break;
            }

            case _filament_motion::stop_on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop_on_use, 300, time_now);
                MC_STU_STATUS_set_latch(num, BMCU_LED_ERROR, time_now, 0u);
                break;
            }

            case _filament_motion::send_out:
            {
                if (g_on_use_jam_latch[num])
                {
                    if (MC_PULL_pct_f[num] > 85.0f)
                    {
                        g_on_use_low_latch[num] = 0u;
                        g_on_use_jam_latch[num] = 0u;
                        g_on_use_hi_pwm_us[num] = 0u;
                    }
                    else
                    {
                        MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                        MC_STU_STATUS_set_latch(num, BMCU_LED_LOADING, time_now, 0u);
                        break;
                    }
                }

                MC_STU_STATUS_set_latch(num, BMCU_LED_LOADING, time_now, 0u);
                filament_now_position[num] = filament_sending_out;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_send, 100, time_now);
                break;
            }

            case _filament_motion::pull_back:
            {
                MC_STU_STATUS_set_latch(num, BMCU_LED_UNLOADING, time_now, 1u);

                if (filament_now_position[num] != filament_pulling_back)
                {
                    const bool from_before_pull_back =
                        filament_now_position[num] == filament_before_pull_back;
                    filament_now_position[num] = filament_pulling_back;

                    float target;
                    if (g_on_use_jam_latch[num])
                    {
                        filament_pull_back_meters[num] = A.filament[num].meters;
                        target = 0.100f;
                    }
                    else
                    {
                        if (!from_before_pull_back)
                            filament_pull_back_meters[num] = A.filament[num].meters;
                        target = motion_control_pull_back_distance(num);
                    }

                    filament_pull_back_target[num] = target;

                    const float d = filament_pull_back_meters[num] - A.filament[num].meters;
                    const float remain = target - d;
                    g_pull_remain_m[num]  = (remain > 0.0f) ? remain : 0.0f;
                    g_pull_speed_set[num] = -PULL_V_FAST;
                }

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pull, 100, time_now);
                break;
            }

            case _filament_motion::before_pull_back:
            {
                MC_STU_STATUS_set_latch(num, BMCU_LED_BEFORE_UNLOAD, time_now, 1u);

                if (filament_now_position[num] != filament_before_pull_back)
                {
                    filament_now_position[num] = filament_before_pull_back;
                    filament_pull_back_meters[num] = A.filament[num].meters;
                }

                const float target = motion_control_pull_back_distance(num);
                const float retracted = filament_pull_back_meters[num] - A.filament[num].meters;
                if (retracted >= target)
                {
                    MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                    break;
                }

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_before_pull_back, 300, time_now);
                break;
            }

            case _filament_motion::on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_on_use, 300, time_now);
                MC_STU_STATUS_set_latch(num, BMCU_LED_ACTIVE, time_now, 0u);
                break;
            }

            case _filament_motion::idle:
            default:
            {
                filament_now_position[num] = filament_idle;

                if (g_on_use_jam_latch[num])
                {
                    MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                    MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
                    break;
                }

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 100, time_now);

#if BMCU_DM_TWO_MICROSWITCH
                if (dm_fail_latch[num])      MC_STU_STATUS_set_latch(num, BMCU_LED_ERROR, time_now, 0u);
                else if (dm_loaded[num])     MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
                else                         MC_STU_RGB_set_latch(num, 0x00u, 0x00u, 0x00u, time_now, 0u);
#else
                MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
#endif
                break;
            }
            }
        }
        else
        {
            filament_now_position[num] = filament_idle;
            MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 100, time_now);
            MC_STU_RGB_set_latch(num, 0x00u, 0x00u, 0x00u, time_now, 0u);
        }
    }
}

static inline void stu_apply_baseline(int error, uint64_t now_ms)
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (g_on_use_low_latch[i])
        {
            MC_STU_RGB_set(i, 0xFFu, 0x00u, 0x00u);
            continue;
        }

#if BMCU_DM_TWO_MICROSWITCH
        if (dm_fail_latch[i])
        {
            MC_STU_RGB_set(i, 0xFFu, 0x00u, 0x00u);
            continue;
        }

        const bool ins_ok = error ? true : filament_channel_inserted[i];
        const bool show_loaded =
            (dm_loaded[i] != 0u) &&
            (MC_ONLINE_key_stu[i] != 0u) &&
            ins_ok;

        if (show_loaded) MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
        else             MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
#else
        if (error)
        {
            if (MC_ONLINE_key_stu[i] != 0) MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
            else                           MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
        }
        else
        {
            if (MC_ONLINE_key_stu[i] != 0 && filament_channel_inserted[i])
                MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
            else
                MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
        }
#endif
    }
}

static void motor_motion_run(int error, uint64_t time_now, uint32_t now_ticks,
                             bool calibration_inhibit)
{
    g_calibration_motion_inhibit = calibration_inhibit;
#if BMCU_DM_TWO_MICROSWITCH
    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        if (error || calibration_inhibit || !g_host_motion_enabled)
        {
            dm_loaded[ch]            = 0u;
            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0ull;
            dm_auto_remain_m[ch]     = 0.0f;
            dm_auto_last_m[ch]       = 0.0f;
            dm_loaded_drop_t0_ms[ch] = 0ull;
            dm_autoload_gate[ch]     = 1u;
            auto_unload_reset(ch, 1u);
            continue;
        }

        if (!filament_channel_inserted[ch])
        {
            dm_loaded[ch]            = 1u;
            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0ull;
            dm_auto_remain_m[ch]     = 0.0f;
            dm_auto_last_m[ch]       = 0.0f;
            dm_loaded_drop_t0_ms[ch] = 0ull;
            dm_autoload_gate[ch]     = 0u;
            continue;
        }

        const uint8_t ks = MC_ONLINE_key_stu[ch];

        if (ks == 0u)
        {
            if (filament_now_position[ch] == filament_idle)
                dm_autoload_gate[ch] = 0u;

            dm_loaded[ch]            = 0u;
            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0ull;
            dm_auto_remain_m[ch]     = 0.0f;
            dm_auto_last_m[ch]       = 0.0f;
            dm_loaded_drop_t0_ms[ch] = 0ull;
            continue;
        }

        if (dm_loaded[ch] && (ks != 1u))
        {
            uint64_t t0 = dm_loaded_drop_t0_ms[ch];
            if (t0 == 0ull) dm_loaded_drop_t0_ms[ch] = time_now;
            else if ((time_now - t0) >= 100ull)
            {
                dm_loaded[ch]            = 0u;
                dm_loaded_drop_t0_ms[ch] = 0ull;

                dm_auto_state[ch]    = DM_AUTO_IDLE;
                dm_auto_try[ch]      = 0u;
                dm_auto_t0_ms[ch]    = 0ull;
                dm_auto_remain_m[ch] = 0.0f;
                dm_auto_last_m[ch]   = 0.0f;
            }
        }
        else
        {
            dm_loaded_drop_t0_ms[ch] = 0ull;
        }
    }
#endif

    static uint32_t last_ticks = 0u;
    static uint8_t  have_last_ticks = 0u;

    uint32_t dt_ticks = 0u;
    if (!have_last_ticks)
    {
        have_last_ticks = 1u;
    }
    else
    {
        dt_ticks = (uint32_t)(now_ticks - last_ticks);
    }
    last_ticks = now_ticks;

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    const uint32_t max_dt_ticks = 200u * tpm;
    if (dt_ticks > max_dt_ticks) dt_ticks = max_dt_ticks;

    uint32_t tpus = time_hw_tpus;
    if (!tpus) tpus = 1u;

    static uint32_t seconds_tpus_cache = 0u;
    static float seconds_per_tick = 0.0f;
    if (tpus != seconds_tpus_cache)
    {
        seconds_tpus_cache = tpus;
        seconds_per_tick = 1.0f / ((float)tpus * 1000000.0f);
    }
    const bool have_time_step = (dt_ticks != 0u);
    const float time_E = have_time_step ? (float)dt_ticks * seconds_per_tick : 0.0f;

    stu_apply_baseline(error, time_now);

#if BMCU_ONLINE_LED_FILAMENT_RGB
    auto &Acol = ams[motion_control_ams_num];
#endif

    if (!error && !calibration_inhibit)
    {
        if (!motor_motion_filamnet_pull_back_to_online_key(time_now))
            motor_motion_switch(time_now);
    }
    else
    {
        for (uint8_t i = 0; i < kChCount; i++)
            MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
    }

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (error || calibration_inhibit || !g_host_motion_enabled)
        {
            if (auto_unload_active[i])
                Motion_control_cancel_channel_retract(i);
            else
                auto_unload_reset(i, 1u);
            MOTOR_CONTROL[i].set_motion(
                filament_motion_enum::filament_motion_stop, 100, time_now);
            Motion_control_set_PWM(i, 0);
            continue;
        }

        if (!MC_PULL_calibration_is_valid(i))
        {
            if (auto_unload_active[i])
                Motion_control_cancel_channel_retract(i);
            else
                auto_unload_reset(i, 1u);
            MOTOR_CONTROL[i].set_motion(
                filament_motion_enum::filament_motion_stop, 100, time_now);
            Motion_control_set_PWM(i, 0);
            const uint8_t brightness = calibration_breathe(
                time_now, 1800u, 2u, 0x18u);
            MC_PULL_ONLINE_RGB_set(
                i, brightness, (uint8_t)((brightness * 2u) / 3u),
                0u, false);
            continue;
        }

        if (!AS5600_is_good(i))
        {
            if (auto_unload_active[i])
                Motion_control_cancel_channel_retract(i);
            else
                auto_unload_reset(i, 1u);
            MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
            Motion_control_set_PWM(i, 0);
            continue;
        }

        const bool auto_unload_route_safe =
            ams_state_get_route_state(i) == AMS_ROUTE_EMPTY;
        const bool auto_unload_idle =
            MOTOR_CONTROL[i].motion ==
                filament_motion_enum::filament_motion_pressure_ctrl_idle;
        const float pct = MC_PULL_pct_f[i];
        const bool neutral =
            pct > AUTO_UNLOAD_NEUTRAL_LO_PCT &&
            pct < AUTO_UNLOAD_NEUTRAL_HI_PCT;

        const bool controller_busy =
            bmcu_protocol_busy_for_local_calibration();
        const bool explicit_retract =
            auto_unload_active[i] && auto_unload_explicit[i];
        const bool gesture_policy_ok =
            bmcu_policy_autonomous_unload();

        if (!filament_channel_inserted[i] || !auto_unload_route_safe ||
            (auto_unload_active[i] && !explicit_retract &&
             !gesture_policy_ok) ||
            (!auto_unload_active[i] &&
             (!gesture_policy_ok || !auto_unload_idle || controller_busy)))
        {
            if (auto_unload_active[i])
                Motion_control_cancel_channel_retract(i);
            else
                auto_unload_reset(i, 1u);
        }
        else
        {

            if (!auto_unload_active[i] && auto_unload_blocked[i])
            {
                if (!neutral)
                {
                    auto_unload_rearm_t0_ms[i] = 0ull;
                }
                else if (auto_unload_rearm_t0_ms[i] == 0ull)
                {
                    auto_unload_rearm_t0_ms[i] = time_now;
                }
                else if ((time_now - auto_unload_rearm_t0_ms[i]) >=
                         AUTO_UNLOAD_REARM_NEUTRAL_MS)
                {
                    auto_unload_blocked[i] = 0u;
                    auto_unload_rearm_t0_ms[i] = 0ull;
                    auto_unload_arm[i] = 0u;
                    auto_unload_arm_t0_ms[i] = 0ull;
                }
            }

            if (!auto_unload_blocked[i] &&
                pct >= AUTO_UNLOAD_START_PCT)
            {
                if (!auto_unload_arm[i] && !auto_unload_active[i])
                {
                    auto_unload_arm[i] = 1u;
                    auto_unload_arm_t0_ms[i] = time_now;
                }
            }

            if (auto_unload_arm[i] && !auto_unload_active[i])
            {
                const uint64_t dt = time_now - auto_unload_arm_t0_ms[i];
                if (neutral)
                {
                    if (!auto_unload_blocked[i] && dt <= AUTO_UNLOAD_ARM_MS)
                    {
                        auto_unload_active[i] = 1u;
                        auto_unload_explicit[i] = 0u;
                        auto_unload_active_t0_ms[i] = time_now;
                        auto_unload_empty_t0_ms[i] = 0ull;
                        auto_unload_start_m[i] =
                            ams[motion_control_ams_num].filament[i].meters;
                        auto_unload_blocked[i] = 1u;
                    }
                    auto_unload_arm[i] = 0u;
                    auto_unload_arm_t0_ms[i] = 0ull;
                }
                else if (dt > AUTO_UNLOAD_ARM_MS)
                {
                    auto_unload_arm[i] = 0u;
                    auto_unload_arm_t0_ms[i] = 0ull;
                }
            }

            if (auto_unload_active[i])
            {
                const float current_m =
                    ams[motion_control_ams_num].filament[i].meters;
                const float total_retracted_m =
                    absf(current_m - auto_unload_start_m[i]);
                const bool hard_limit =
                    total_retracted_m >= AUTO_UNLOAD_MAX_M ||
                    (time_now - auto_unload_active_t0_ms[i]) >=
                        AUTO_UNLOAD_MAX_MS;

                if (hard_limit || pct < AUTO_UNLOAD_ABORT_PCT ||
                    ams_state_get_route_state(i) != AMS_ROUTE_EMPTY)
                {
                    Motion_control_cancel_channel_retract(i);
                }
                else if (!Motion_control_channel_retract_target_reached(i))
                {
                    auto_unload_empty_t0_ms[i] = 0ull;
                }
                else if (auto_unload_empty_t0_ms[i] == 0ull)
                {

                    auto_unload_empty_t0_ms[i] = time_now;
                }
                else if ((time_now - auto_unload_empty_t0_ms[i]) >=
                         AUTO_UNLOAD_EMPTY_MS)
                {
                    Motion_control_cancel_channel_retract(i);
                }
            }
        }

        const bool manual_empty_pull =
            bmcu_policy_autonomous_unload() &&
            filament_channel_inserted[i] &&
            ams_state_get_route_state(i) == AMS_ROUTE_EMPTY &&
            auto_unload_idle &&
            !controller_busy &&
            !auto_unload_blocked[i] &&
            (MC_ONLINE_key_stu[i] == 0u) &&
            (MC_PULL_pct_f[i] > 80.0f) &&
            (auto_unload_active[i] == 0u);

        if (auto_unload_active[i])
        {
            float x = MOTOR_CONTROL[i].dir * AUTO_UNLOAD_PWM_PULL;
            if (x * MOTOR_CONTROL[i].dir < 0.0f) x = 0.0f;

            MOTOR_CONTROL[i].PID_speed.clear();
            MOTOR_CONTROL[i].PID_pressure.clear();
            MOTOR_CONTROL[i].pwm_zeroed = (x == 0.0f) ? 1u : 0u;
            _MOTOR_CONTROL::x_prev[i] = x;

            Motion_control_set_PWM(i, (int)x);
            MC_STU_STATUS_set_latch(i, BMCU_LED_UNLOADING, time_now, 1u);
        }
        else if (manual_empty_pull)
        {
            float x = MOTOR_CONTROL[i].dir * 700.0f;
            if (x * MOTOR_CONTROL[i].dir < 0.0f) x = 0.0f;

            MOTOR_CONTROL[i].PID_speed.clear();
            MOTOR_CONTROL[i].PID_pressure.clear();
            MOTOR_CONTROL[i].pwm_zeroed = (x == 0.0f) ? 1u : 0u;
            _MOTOR_CONTROL::x_prev[i] = x;

            Motion_control_set_PWM(i, (int)x);
        }
        else if (have_time_step)
        {
            MOTOR_CONTROL[i].run(time_E, time_now);
        }

        uint8_t r = 0u, g = 0u, b = 0u;
        bool is_filament_rgb = false;

        const uint8_t pct_u8 = MC_PULL_pct[i];

        int hi_thr = MC_PULL_DEADBAND_PCT_HIGH;

        const filament_motion_enum m = MOTOR_CONTROL[i].motion;

        bool hi_hold =
            (m == filament_motion_enum::filament_motion_send) ||
            (m == filament_motion_enum::filament_motion_before_on_use) ||
            (m == filament_motion_enum::filament_motion_stop_on_use);

        if (!hi_hold && (m == filament_motion_enum::filament_motion_pressure_ctrl_on_use))
        {
            const uint64_t t0 = MOTOR_CONTROL[i].on_use_handoff_t0_ms;
            if (t0 != 0ull && (time_now - t0) < ON_USE_HANDOFF_MS) hi_hold = true;
        }

        if (hi_hold)
        {
            hi_thr = (int)MC_LOAD_S2_HOLD_TARGET_PCT + 3;
            if (hi_thr > 100) hi_thr = 100;
            if (hi_thr < 0) hi_thr = 0;
        }

        if (!(m == filament_motion_enum::filament_motion_before_on_use) && (int)pct_u8 >= hi_thr)
        {
            lighting_buffer_rgb(BMCU_BUFFER_MAXIMUM, &r, &g, &b);
        }
        else if (pct_u8 <= 30u)
        {
            lighting_buffer_rgb(BMCU_BUFFER_MINIMUM, &r, &g, &b);
        }
        else
        {
            const uint8_t key = MC_ONLINE_key_stu[i];

#if BMCU_ONLINE_LED_FILAMENT_RGB
    #if BMCU_DM_TWO_MICROSWITCH
            const bool show_filament_rgb = (key == 1u) && dm_loaded[i] && !dm_fail_latch[i];
    #else
            const bool show_filament_rgb = (key != 0u);
    #endif
            if (show_filament_rgb)
            {
                r = Acol.filament[i].color_R;
                g = Acol.filament[i].color_G;
                b = Acol.filament[i].color_B;
                is_filament_rgb = true;
            }
            else
#endif
            {
                if (key == 0u && (uint8_t)(pct_u8 - 49u) <= 2u)
                    lighting_buffer_rgb(BMCU_BUFFER_NEUTRAL, &r, &g, &b);
            }
        }

        MC_PULL_ONLINE_RGB_set(i, r, g, b, is_filament_rgb);

    }
}

void Motion_control_run(int error)
{
    const uint64_t now_ticks64 = time_ticks64();
    const uint32_t now_ticks   = (uint32_t)now_ticks64;
    const uint64_t now_ms      = time_ms_fast_from_ticks64(now_ticks64);

    const bool adc_sample_ready = MC_PULL_ONLINE_read(now_ticks);
    motion_control_service_host_motion_gate(now_ms);
    if (!adc_sample_ready)
    {

        for (uint8_t ch = 0; ch < kChCount; ch++)
        {
            MOTOR_CONTROL[ch].set_motion(
                filament_motion_enum::filament_motion_stop, 100, now_ms);
            Motion_control_set_PWM(ch, 0);
        }
        return;
    }
    bool calibration_inhibit = MC_PULL_calibration_motion_inhibited();
    const bool host_motion_inhibit = !g_host_motion_enabled;

    auto &A = ams[motion_control_ams_num];

    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        const uint8_t ks = MC_ONLINE_key_stu[ch];
        if (ks == 0u)
        {
            if (!error)
            {
                if (A.now_filament_num == ch)
                {
                    if (A.filament[ch].motion == _filament_motion::send_out)
                        MOTOR_CONTROL[ch].set_motion(filament_motion_enum::filament_motion_stop, 100, now_ms);
                }
            }

            if (g_on_use_jam_latch[ch])
            {
                g_on_use_low_latch[ch] = 0u;
                g_on_use_jam_latch[ch] = 0u;
            }

            g_on_use_hi_pwm_us[ch] = 0u;
        }
    }

    if ((error <= 0) && all_no_filament() &&
        !calibration_inhibit && !host_motion_inhibit &&
        !bmcu_protocol_busy_for_local_calibration())
    {
        int pressed = -1;

        for (uint8_t ch = 0; ch < kChCount; ch++)
        {
            if (!filament_channel_inserted[ch]) continue;

            const int   pct = (int)MC_PULL_pct[ch];
            const float v   = MC_PULL_stu_raw[ch];

            const bool hard_blue =
                (pct <= CAL_START_PCT_THRESH) ||
                (v <= (1.65f - CAL_START_V_DELTA)) ||
                (v <= (MC_PULL_V_MIN[ch] + CAL_START_NEAR_MIN));

            if (hard_blue) { pressed = (int)ch; break; }
        }

        uint32_t tpm = time_hw_tpms;
        if (!tpm) tpm = 1u;

        if (pressed >= 0)
        {
            if (g_hold_ch != pressed)
            {
                g_hold_ch = pressed;
                g_hold_t0_ticks = now_ticks;
            }
            else if (!g_cal_start_latched &&
                     (uint32_t)(now_ticks - g_hold_t0_ticks) >=
                         (uint32_t)CAL_START_HOLD_MS * tpm)
            {
                calibration_start_from_buffer_button();
                calibration_inhibit = true;

                g_cal_start_latched = true;
            }
        }
        else
        {
            g_hold_ch = -1;
            g_hold_t0_ticks = 0u;
            g_cal_start_latched = false;
        }
    }
    else
    {
        g_hold_ch = -1;
        g_hold_t0_ticks = 0u;

        if (!calibration_inhibit)
            g_cal_start_latched = false;
    }

    AS5600_distance_updata(now_ticks);

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (MC_ONLINE_key_stu[i] != 0u) A.filament[i].online = true;
        else if ((filament_now_position[i] == filament_redetect) || (filament_now_position[i] == filament_pulling_back))
            A.filament[i].online = true;
        else
            A.filament[i].online = false;
    }

    motor_motion_run(
        error, now_ms, now_ticks, calibration_inhibit || host_motion_inhibit);

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if ((MC_AS5600.online[i] == false) || (MC_AS5600.magnet_stu[i] == -1))
            MC_STU_RGB_set(i, 0xFF, 0x00, 0x00);
    }
}

void MC_PWM_init()
{
    GPIO_InitTypeDef GPIO_InitStructure;
    const uint16_t motor_gpio_b = GPIO_Pin_3 | GPIO_Pin_4 | GPIO_Pin_5 |
                                  GPIO_Pin_6 | GPIO_Pin_7 | GPIO_Pin_8 |
                                  GPIO_Pin_9;

    RCC_APB2PeriphClockCmd(
        RCC_APB2Periph_GPIOA | RCC_APB2Periph_GPIOB |
        RCC_APB2Periph_AFIO, ENABLE);

    GPIO_InitStructure.GPIO_Pin   = motor_gpio_b;
    GPIO_InitStructure.GPIO_Mode  = GPIO_Mode_Out_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOB, &GPIO_InitStructure);
    GPIO_SetBits(GPIOB, motor_gpio_b);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_15;
    GPIO_Init(GPIOA, &GPIO_InitStructure);
    GPIO_SetBits(GPIOA, GPIO_Pin_15);

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM3, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;
    TIM_OCInitTypeDef TIM_OCInitStructure;

    TIM_TimeBaseStructure.TIM_Period        = 999;
    TIM_TimeBaseStructure.TIM_Prescaler     = 1;
    TIM_TimeBaseStructure.TIM_ClockDivision = 0;
    TIM_TimeBaseStructure.TIM_CounterMode   = TIM_CounterMode_Up;

    TIM_TimeBaseInit(TIM2, &TIM_TimeBaseStructure);
    TIM_TimeBaseInit(TIM3, &TIM_TimeBaseStructure);
    TIM_TimeBaseInit(TIM4, &TIM_TimeBaseStructure);

    TIM_OCInitStructure.TIM_OCMode      = TIM_OCMode_PWM1;
    TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;
    TIM_OCInitStructure.TIM_Pulse       = 1000;
    TIM_OCInitStructure.TIM_OCPolarity  = TIM_OCPolarity_High;

    TIM_OC1Init(TIM2, &TIM_OCInitStructure);
    TIM_OC2Init(TIM2, &TIM_OCInitStructure);
    TIM_OC1Init(TIM3, &TIM_OCInitStructure);
    TIM_OC2Init(TIM3, &TIM_OCInitStructure);
    TIM_OC1Init(TIM4, &TIM_OCInitStructure);
    TIM_OC2Init(TIM4, &TIM_OCInitStructure);
    TIM_OC3Init(TIM4, &TIM_OCInitStructure);
    TIM_OC4Init(TIM4, &TIM_OCInitStructure);

    TIM_OC1PreloadConfig(TIM2, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM2, TIM_OCPreload_Enable);
    TIM_OC1PreloadConfig(TIM3, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM3, TIM_OCPreload_Enable);
    TIM_OC1PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC3PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC4PreloadConfig(TIM4, TIM_OCPreload_Enable);

    GPIO_PinRemapConfig(GPIO_FullRemap_TIM2, ENABLE);
    GPIO_PinRemapConfig(GPIO_PartialRemap_TIM3, ENABLE);
    GPIO_PinRemapConfig(GPIO_Remap_TIM4, DISABLE);

    TIM_CtrlPWMOutputs(TIM2, ENABLE);
    TIM_ARRPreloadConfig(TIM2, ENABLE);
    TIM_Cmd(TIM2, ENABLE);
    TIM_CtrlPWMOutputs(TIM3, ENABLE);
    TIM_ARRPreloadConfig(TIM3, ENABLE);
    TIM_Cmd(TIM3, ENABLE);
    TIM_CtrlPWMOutputs(TIM4, ENABLE);
    TIM_ARRPreloadConfig(TIM4, ENABLE);
    TIM_Cmd(TIM4, ENABLE);

    GPIO_InitStructure.GPIO_Pin   = motor_gpio_b;
    GPIO_InitStructure.GPIO_Mode  = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOB, &GPIO_InitStructure);
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_15;
    GPIO_Init(GPIOA, &GPIO_InitStructure);
}

void Motion_control_boot_safe_init()
{

    g_host_motion_requested = false;
    g_host_motion_enabled = false;
    g_host_motion_sample_valid = false;
    g_host_motion_stable_t0_ms = 0ull;
    MC_PWM_init();
    for (uint8_t ch = 0u; ch < kChCount; ch++)
        Motion_control_set_PWM(ch, 0);
}

static void MOTOR_init()
{
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOC | RCC_APB2Periph_GPIOD, ENABLE);

    for (uint8_t i = 0; i < kChCount; i++)
    {
        Motion_control_set_PWM(i, 0);
        MOTOR_CONTROL[i].set_pwm_zero(500);
        MOTOR_CONTROL[i].dir = (float)Motion_control_data_save.Motion_control_dir[i];
    }
}

void Motion_control_init()
{
    auto &A = ams[motion_control_ams_num];
    A.online   = true;
    A.ams_type = 0x03;

    (void)Motion_control_read();

    MC_PULL_ONLINE_init();
    (void)MC_PULL_ONLINE_read(time_ticks32());

    #if BMCU_DM_TWO_MICROSWITCH
        for (uint8_t ch = 0; ch < kChCount; ch++)
        {
            if (!filament_channel_inserted[ch])
            {
                dm_loaded[ch]            = 1u;
                dm_fail_latch[ch]        = 0u;
                dm_auto_state[ch]        = DM_AUTO_IDLE;
                dm_auto_try[ch]          = 0u;
                dm_auto_t0_ms[ch]        = 0ull;
                dm_auto_remain_m[ch]     = 0.0f;
                dm_auto_last_m[ch]       = 0.0f;
                dm_loaded_drop_t0_ms[ch] = 0ull;
                dm_autoload_gate[ch]     = 0u;
                continue;
            }

            const uint8_t ks = MC_ONLINE_key_stu[ch];

            dm_autoload_gate[ch] = (ks != 0u) ? 1u : 0u;
            dm_loaded[ch] = (ks == 1u) ? 1u : 0u;

            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0ull;
            dm_auto_remain_m[ch]     = 0.0f;
            dm_auto_last_m[ch]       = 0.0f;
            dm_loaded_drop_t0_ms[ch] = 0ull;
        }
    #endif

    MC_AS5600.init(AS5600_SCL_PORT, AS5600_SCL_PIN,
               AS5600_SDA_PORT, AS5600_SDA_PIN,
               4);
    MC_AS5600.updata_angle();
    MC_AS5600.updata_stu();

    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ok = MC_AS5600.online[i] && (MC_AS5600.magnet_stu[i] != AS5600_soft_IIC_many::offline);
        g_as5600_good[i]     = ok ? 1u : 0u;
        g_as5600_fail[i]     = ok ? 0u : kAS5600_FAIL_TRIP;
        g_as5600_okstreak[i] = ok ? kAS5600_OK_RECOVER : 0u;
    }

    for (uint8_t i = 0; i < kChCount; i++)
    {
        as5600_distance_save[i] = MC_AS5600.raw_angle[i];
        filament_now_position[i] = filament_idle;
    }

    MOTOR_init();
}
