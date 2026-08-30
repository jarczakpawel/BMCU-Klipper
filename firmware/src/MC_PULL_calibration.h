#pragma once
#include <stdint.h>
#include <stdbool.h>

enum BmcuBufferCalPoint : uint8_t
{
    BMCU_BUFFER_CAL_MIN = 0,
    BMCU_BUFFER_CAL_NEUTRAL = 1,
    BMCU_BUFFER_CAL_MAX = 2
};

enum BmcuAutoCalStage : uint8_t
{
    BMCU_AUTO_CAL_IDLE = 0,
    BMCU_AUTO_CAL_BASELINE = 1,
    BMCU_AUTO_CAL_FIRST_MOVE = 2,
    BMCU_AUTO_CAL_FIRST_RELEASE = 3,
    BMCU_AUTO_CAL_SECOND_MOVE = 4,
    BMCU_AUTO_CAL_SECOND_RELEASE = 5,
    BMCU_AUTO_CAL_SAVING = 6
};

enum BmcuAutoCalState : uint8_t
{
    BMCU_AUTO_CAL_STATE_IDLE = 0,
    BMCU_AUTO_CAL_STATE_RUNNING = 1,
    BMCU_AUTO_CAL_STATE_DONE = 2,
    BMCU_AUTO_CAL_STATE_FAILED = 3,
    BMCU_AUTO_CAL_STATE_ABORTED = 4
};

void MC_PULL_calibration_boot();
bool MC_PULL_calibration_clear();

bool MC_PULL_calibration_capture(uint8_t ch, uint8_t point, float* raw_out);
bool MC_PULL_calibration_commit(uint8_t ch);

bool MC_PULL_calibration_auto_start(uint8_t selected_mask, uint32_t op_id,
                                    bool wait_for_release = false,
                                    uint8_t trigger_channel = 0xFFu);
void MC_PULL_calibration_auto_run();
bool MC_PULL_calibration_auto_abort();
bool MC_PULL_calibration_auto_active();

bool MC_PULL_calibration_motion_inhibited();
uint32_t MC_PULL_calibration_auto_op_id();
uint8_t MC_PULL_calibration_auto_stage();
uint8_t MC_PULL_calibration_auto_state();
uint8_t MC_PULL_calibration_auto_reason();
uint8_t MC_PULL_calibration_auto_channel();
uint8_t MC_PULL_calibration_auto_progress();
uint8_t MC_PULL_calibration_auto_selected_mask();
uint8_t MC_PULL_calibration_auto_done_mask();
bool MC_PULL_calibration_auto_take_result(uint32_t* op_id, uint8_t* state,
                                          uint8_t* reason, uint8_t* channel,
                                          uint32_t* duration_ms);

uint8_t MC_PULL_calibration_capture_mask(uint8_t ch);
uint8_t MC_PULL_calibration_valid_mask();
bool MC_PULL_calibration_active();
bool MC_PULL_calibration_is_valid(uint8_t ch);
float MC_PULL_calibration_raw(uint8_t ch);
