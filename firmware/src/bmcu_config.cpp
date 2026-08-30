#include "bmcu_config.h"
#include "Flash_saves.h"
#include <math.h>

#define BMCU_CFG_CHECK 0x4B4C4243u

#define KEY_LOAD_PRESSURE_PCT  0x0002u
#define KEY_LOAD_SPEED_MMS     0x0003u
#define KEY_PULL_SPEED_MMS     0x0004u
#define KEY_PULL_SPEED_END_MMS 0x0005u
#define KEY_JAM_TIMEOUT_MS     0x0006u
#define KEY_BEFORE_PULLBACK_TARGET_PCT 0x0009u

BmcuMotionNvm g_bmcu_nvm;

static uint8_t g_policy_standalone = 0u;
static uint8_t g_policy_autonomous_assist = 0u;
static uint8_t g_policy_autonomous_unload = 0u;

static float g_channel_retract_len_m[4] = {0.200f, 0.200f, 0.200f, 0.200f};
static float g_channel_autoload_len_m[4] = {0.120f, 0.120f, 0.120f, 0.120f};

void bmcu_policy_set(bool standalone, bool autonomous_assist,
                     bool autonomous_unload)
{
    g_policy_standalone = standalone ? 1u : 0u;
    g_policy_autonomous_assist =
        (g_policy_standalone && autonomous_assist) ? 1u : 0u;
    g_policy_autonomous_unload =
        (g_policy_standalone && autonomous_unload) ? 1u : 0u;
}

void bmcu_policy_disable(void)
{
    g_policy_standalone = 0u;
    g_policy_autonomous_assist = 0u;
    g_policy_autonomous_unload = 0u;
}

bool bmcu_policy_standalone(void) { return g_policy_standalone != 0u; }
bool bmcu_policy_autonomous_assist(void)
{
    return g_policy_autonomous_assist != 0u;
}
bool bmcu_policy_autonomous_unload(void)
{
    return g_policy_autonomous_unload != 0u;
}

static float clampf_local(float v, float lo, float hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

void bmcu_config_defaults(void)
{
    for (uint8_t i = 0; i < 4; i++)
    {
        g_bmcu_nvm.Motion_control_dir[i] = 0;
        g_bmcu_nvm.dm_key_none_cv[i] = 60u;
        g_channel_retract_len_m[i] = 0.200f;
        g_channel_autoload_len_m[i] = 0.120f;
    }

    g_bmcu_nvm.check = 0x40614061u;
    g_bmcu_nvm.load_pressure_pct = BMCU_LOAD_PRESSURE_DEFAULT_PCT;
    g_bmcu_nvm.reserved0[0] = 0u;
    g_bmcu_nvm.reserved0[1] = 0u;
    g_bmcu_nvm.reserved0[2] = 0u;
    g_bmcu_nvm.load_speed_mms = 60.0f;
    g_bmcu_nvm.pull_speed_mms = 60.0f;
    g_bmcu_nvm.pull_speed_end_mms = 12.0f;
    g_bmcu_nvm.jam_timeout_ms = 20000u;
    g_bmcu_nvm.before_pullback_target_pct = 40.0f;
    g_bmcu_nvm.config_check = BMCU_CFG_CHECK;
}

bool bmcu_config_load(void)
{
    bmcu_config_defaults();

    BmcuMotionNvm tmp = g_bmcu_nvm;
    if (!Flash_Motion_read(&tmp, (uint16_t)sizeof(tmp)))
        return false;

    if (tmp.check != 0x40614061u)
        return false;

    for (uint8_t i = 0; i < 4; i++)
    {
        g_bmcu_nvm.Motion_control_dir[i] = tmp.Motion_control_dir[i];
        g_bmcu_nvm.dm_key_none_cv[i] = tmp.dm_key_none_cv[i];
    }
    g_bmcu_nvm.check = tmp.check;

    return bmcu_config_save();
}

struct __attribute__((packed, aligned(4))) BmcuHardwareNvm
{
    int32_t Motion_control_dir[4];
    uint32_t check;
    uint8_t dm_key_none_cv[4];
};

static_assert(sizeof(BmcuHardwareNvm) == 24u, "hardware NVM layout changed");

bool bmcu_config_save(void)
{

    BmcuHardwareNvm hw{};
    for (uint8_t i = 0u; i < 4u; i++)
    {
        hw.Motion_control_dir[i] = g_bmcu_nvm.Motion_control_dir[i];
        hw.dm_key_none_cv[i] = g_bmcu_nvm.dm_key_none_cv[i];
    }
    hw.check = 0x40614061u;
    return Flash_Motion_write(&hw, (uint16_t)sizeof(hw));
}

float bmcu_config_retract_len(uint8_t channel)
{
    return channel < 4u ? g_channel_retract_len_m[channel] : 0.200f;
}

bool bmcu_runtime_set_retract_lengths(const float values_m[4])
{
    if (!values_m) return false;
    for (uint8_t channel = 0u; channel < 4u; channel++)
    {
        if (!isfinite(values_m[channel]) ||
                values_m[channel] < 0.010f || values_m[channel] > 2.000f)
            return false;
    }
    for (uint8_t channel = 0u; channel < 4u; channel++)
        g_channel_retract_len_m[channel] = values_m[channel];
    return true;
}

float bmcu_config_autoload_len(uint8_t channel)
{
    return channel < 4u ? g_channel_autoload_len_m[channel] : 0.120f;
}

bool bmcu_runtime_set_autoload_lengths(const float values_m[4])
{
    if (!values_m) return false;
    for (uint8_t channel = 0u; channel < 4u; channel++)
    {
        if (!isfinite(values_m[channel]) ||
                values_m[channel] < 0.010f || values_m[channel] > 1.000f)
            return false;
    }
    for (uint8_t channel = 0u; channel < 4u; channel++)
        g_channel_autoload_len_m[channel] = values_m[channel];
    return true;
}

float bmcu_config_load_speed(void) { return g_bmcu_nvm.load_speed_mms; }
float bmcu_config_pull_speed(void)
{
    return (g_bmcu_nvm.pull_speed_mms >= 10.0f && g_bmcu_nvm.pull_speed_mms <= 120.0f) ? g_bmcu_nvm.pull_speed_mms : 60.0f;
}

float bmcu_config_pull_speed_end(void)
{
    return (g_bmcu_nvm.pull_speed_end_mms >= 4.0f && g_bmcu_nvm.pull_speed_end_mms <= 40.0f) ? g_bmcu_nvm.pull_speed_end_mms : 12.0f;
}
uint8_t bmcu_config_load_pressure_pct(void)
{
    const uint8_t value = g_bmcu_nvm.load_pressure_pct;
    return (value >= BMCU_LOAD_PRESSURE_MIN_PCT &&
            value <= BMCU_LOAD_PRESSURE_MAX_PCT)
        ? value : BMCU_LOAD_PRESSURE_DEFAULT_PCT;
}
uint32_t bmcu_config_jam_timeout_ms(void) { return g_bmcu_nvm.jam_timeout_ms; }
float bmcu_config_before_pullback_target_pct(void) { return clampf_local(g_bmcu_nvm.before_pullback_target_pct, 20.0f, 60.0f); }

bool bmcu_config_get(uint16_t key, float *value)
{
    if (!value) return false;
    switch (key)
    {
    case KEY_LOAD_PRESSURE_PCT:  *value = (float)bmcu_config_load_pressure_pct(); return true;
    case KEY_LOAD_SPEED_MMS:     *value = g_bmcu_nvm.load_speed_mms; return true;
    case KEY_PULL_SPEED_MMS:     *value = g_bmcu_nvm.pull_speed_mms; return true;
    case KEY_PULL_SPEED_END_MMS: *value = g_bmcu_nvm.pull_speed_end_mms; return true;
    case KEY_JAM_TIMEOUT_MS:     *value = (float)g_bmcu_nvm.jam_timeout_ms; return true;
    case KEY_BEFORE_PULLBACK_TARGET_PCT: *value = bmcu_config_before_pullback_target_pct(); return true;
    default: return false;
    }
}

bool bmcu_config_set(uint16_t key, float value)
{
    if (!isfinite(value)) return false;
    switch (key)
    {
    case KEY_LOAD_PRESSURE_PCT:

        if (value == 0.0f)
            g_bmcu_nvm.load_pressure_pct = BMCU_LOAD_PRESSURE_DEFAULT_PCT;
        else if (value == 1.0f)
            g_bmcu_nvm.load_pressure_pct = BMCU_LOAD_PRESSURE_MAX_PCT;
        else if (value == 2.0f)
            g_bmcu_nvm.load_pressure_pct = BMCU_LOAD_PRESSURE_MIN_PCT;
        else
            g_bmcu_nvm.load_pressure_pct = (uint8_t)(clampf_local(
                value, (float)BMCU_LOAD_PRESSURE_MIN_PCT,
                (float)BMCU_LOAD_PRESSURE_MAX_PCT) + 0.5f);
        return true;
    case KEY_LOAD_SPEED_MMS:     g_bmcu_nvm.load_speed_mms = clampf_local(value, 10.0f, 120.0f); return true;
    case KEY_PULL_SPEED_MMS:     g_bmcu_nvm.pull_speed_mms = clampf_local(value, 10.0f, 120.0f); return true;
    case KEY_PULL_SPEED_END_MMS: g_bmcu_nvm.pull_speed_end_mms = clampf_local(value, 4.0f, 40.0f); return true;
    case KEY_JAM_TIMEOUT_MS:     g_bmcu_nvm.jam_timeout_ms = (uint32_t)clampf_local(value, 1000.0f, 120000.0f); return true;
    case KEY_BEFORE_PULLBACK_TARGET_PCT: g_bmcu_nvm.before_pullback_target_pct = clampf_local(value, 20.0f, 60.0f); return true;
    default: return false;
    }
}
