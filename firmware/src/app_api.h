#pragma once
#include <stdint.h>
#include "ws2812.h"
#include "lighting.h"

extern WS2812_class RGBOUT[4];

static inline __attribute__((always_inline))
void MC_STU_STATUS_set(uint8_t ch, uint8_t status)
{
    if (ch >= 4u) return;
    uint8_t r, g, b;
    lighting_status_rgb(status, &r, &g, &b);
    RGBOUT[ch].set_RGB(r, g, b, 0u);
}

static inline __attribute__((always_inline))
void MC_STU_RGB_set(uint8_t ch, uint8_t r, uint8_t g, uint8_t b)
{
    if (ch >= 4u) return;
    if (r == 0xFFu && g == 0x00u && b == 0x00u)
        return MC_STU_STATUS_set(ch, BMCU_LED_ERROR);
    if (r == 0x38u && g == 0x35u && b == 0x32u)
        return MC_STU_STATUS_set(ch, BMCU_LED_IDLE);
    if (r == 0xFFu && g == 0xFFu && b == 0x00u)
        return MC_STU_STATUS_set(ch, BMCU_LED_BEFORE_LOAD);
    if (r == 0x00u && g == 0xD5u && b == 0x2Au)
        return MC_STU_STATUS_set(ch, BMCU_LED_LOADING);
    if (r == 0x00u && g == 0xB0u && b == 0xFFu)
        return MC_STU_STATUS_set(ch, BMCU_LED_ACTIVE);
    if (r == 0xFFu && g == 0xA0u && b == 0x00u)
        return MC_STU_STATUS_set(ch, BMCU_LED_BEFORE_UNLOAD);
    if (r == 0xA0u && g == 0x2Du && b == 0xFFu)
        return MC_STU_STATUS_set(ch, BMCU_LED_UNLOADING);
    if (r == 0xFFu && g == 0x00u && b == 0xFFu)
        return MC_STU_STATUS_set(ch, BMCU_LED_PULLBACK);
    if (r == 0x00u && g == 0x00u && b == 0x00u)
        return MC_STU_STATUS_set(ch, BMCU_LED_EMPTY);
    RGBOUT[ch].set_RGB(r, g, b, 0u);
}

static inline __attribute__((always_inline))
void MC_PULL_ONLINE_RGB_set(uint8_t ch, uint8_t r, uint8_t g, uint8_t b, bool filament = false)
{
    if (ch < 4u) RGBOUT[ch].set_RGB_online(r, g, b, 1u, filament);
}

bool ams_state_begin_change(uint8_t filament_ch);
bool ams_state_set_loaded(uint8_t filament_ch);
bool ams_state_set_unloaded(uint8_t filament_ch);
uint8_t ams_state_get_route_state(uint8_t filament_ch);
uint8_t ams_state_get_loaded_mask(void);
uint8_t ams_state_get_uncertain_mask(void);

bool bmcu_protocol_busy_for_local_calibration(void);
