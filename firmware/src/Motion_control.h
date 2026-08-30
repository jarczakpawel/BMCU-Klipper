#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "bmcu_config.h"

void Motion_control_boot_safe_init();
void Motion_control_init();
void Motion_control_set_PWM(uint8_t CHx, int PWM);
void Motion_control_set_host_motion_enabled(bool enabled);
int16_t Motion_control_get_pwm(uint8_t CHx);
uint8_t Motion_control_filament_present(uint8_t CHx);
uint8_t Motion_control_channel_retract_target_reached(uint8_t CHx);
uint8_t Motion_control_channel_connected(uint8_t CHx);
uint8_t Motion_control_encoder_io_ok(uint8_t CHx);
float Motion_control_encoder_meters(uint8_t CHx);
void Motion_control_run(int error);
void Motion_control_clear_faults(void);
bool Motion_control_start_channel_retract(uint8_t channel);
bool Motion_control_channel_retract_active(uint8_t channel);
void Motion_control_cancel_channel_retract(uint8_t channel);
void Motion_control_cancel_all_channel_retracts(void);
void Motion_control_prepare_calibration(void);
bool Motion_control_save_dm_key_none_thresholds(void);

bool Motion_control_calibrate_motor_encoder(uint8_t selected_mask,
                                            int8_t directions[4]);

bool Motion_control_commit_hardware_calibration(
    uint8_t selected_mask, const uint8_t detector_none_cv[4],
    const int8_t directions[4]);

void MC_PULL_detect_channels_inserted();

extern float   MC_PULL_V_OFFSET[4];
extern float   MC_PULL_V_MIN[4];
extern float   MC_PULL_V_MAX[4];
extern uint8_t MC_PULL_pct[4];
extern int8_t  MC_PULL_POLARITY[4];
extern float   MC_DM_KEY_NONE_THRESH[4];
extern bool    filament_channel_inserted[4];

#ifndef BAMBU_BUS_AMS_NUM
#define BAMBU_BUS_AMS_NUM 0
#endif

#ifndef BMCU_DM_TWO_MICROSWITCH
#define BMCU_DM_TWO_MICROSWITCH 0
#endif

#ifndef BMCU_ONLINE_LED_FILAMENT_RGB
#define BMCU_ONLINE_LED_FILAMENT_RGB 0
#endif

#ifndef motion_control_ams_num
#define motion_control_ams_num BAMBU_BUS_AMS_NUM
#endif

#ifndef motion_control_pull_back_distance
#define motion_control_pull_back_distance(CHANNEL) (bmcu_config_retract_len((CHANNEL)))
#endif

#if (BAMBU_BUS_AMS_NUM < 0) || (BAMBU_BUS_AMS_NUM > 3)
#error "BAMBU_BUS_AMS_NUM must be in range 0..3"
#endif

#ifndef BMCU_KLIPPER_HOST_JAM
#define BMCU_KLIPPER_HOST_JAM 1
#endif
