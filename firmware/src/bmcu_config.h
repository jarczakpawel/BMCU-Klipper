#pragma once
#include <stdint.h>
#include <stdbool.h>

#define BMCU_LOAD_PRESSURE_MIN_PCT     75u
#define BMCU_LOAD_PRESSURE_MAX_PCT     95u
#define BMCU_LOAD_PRESSURE_DEFAULT_PCT 82u

struct __attribute__((packed, aligned(4))) BmcuMotionNvm
{
    int Motion_control_dir[4];
    uint32_t check;
    uint8_t dm_key_none_cv[4];
    uint8_t load_pressure_pct;
    uint8_t reserved0[3];
    float load_speed_mms;
    float pull_speed_mms;
    float pull_speed_end_mms;
    uint32_t jam_timeout_ms;
    float before_pullback_target_pct;
    uint32_t config_check;
};

extern BmcuMotionNvm g_bmcu_nvm;

void bmcu_config_defaults(void);
bool bmcu_config_load(void);
bool bmcu_config_save(void);

float bmcu_config_retract_len(uint8_t channel);
bool bmcu_runtime_set_retract_lengths(const float values_m[4]);
float bmcu_config_autoload_len(uint8_t channel);
bool bmcu_runtime_set_autoload_lengths(const float values_m[4]);
float bmcu_config_load_speed(void);
float bmcu_config_pull_speed(void);
float bmcu_config_pull_speed_end(void);
uint8_t bmcu_config_load_pressure_pct(void);
uint32_t bmcu_config_jam_timeout_ms(void);
float bmcu_config_before_pullback_target_pct(void);

bool bmcu_config_get(uint16_t key, float *value);
bool bmcu_config_set(uint16_t key, float value);

void bmcu_policy_set(bool standalone, bool autonomous_assist,
                     bool autonomous_unload);
void bmcu_policy_disable(void);
bool bmcu_policy_standalone(void);
bool bmcu_policy_autonomous_assist(void);
bool bmcu_policy_autonomous_unload(void);
