#pragma once
#include <stdint.h>
#include <stdbool.h>

#define BMCU_LIGHTING_VERSION 1u
#define BMCU_LIGHTING_PAYLOAD_SIZE 45u

enum BmcuStatusLed : uint8_t
{
    BMCU_LED_IDLE = 0u,
    BMCU_LED_BEFORE_LOAD = 1u,
    BMCU_LED_LOADING = 2u,
    BMCU_LED_ACTIVE = 3u,
    BMCU_LED_BEFORE_UNLOAD = 4u,
    BMCU_LED_UNLOADING = 5u,
    BMCU_LED_REDETECT = 6u,
    BMCU_LED_ERROR = 7u,
    BMCU_LED_EMPTY = 8u,
    BMCU_LED_PULLBACK = 9u,
    BMCU_LED_STATUS_COUNT = 10u,
};

enum BmcuBufferLed : uint8_t
{
    BMCU_BUFFER_MINIMUM = 0u,
    BMCU_BUFFER_NEUTRAL = 1u,
    BMCU_BUFFER_MAXIMUM = 2u,
    BMCU_BUFFER_LED_COUNT = 3u,
};

void lighting_init(void);
bool lighting_apply_payload(const uint8_t *payload, uint16_t len);
void lighting_set_system_color(uint8_t red, uint8_t green, uint8_t blue);
void lighting_system_rgb(uint8_t *red, uint8_t *green, uint8_t *blue);
void lighting_scale_system_effect(uint8_t *red, uint8_t *green, uint8_t *blue);
void lighting_status_rgb(uint8_t status, uint8_t *red, uint8_t *green, uint8_t *blue);
void lighting_buffer_rgb(uint8_t state, uint8_t *red, uint8_t *green, uint8_t *blue);
uint8_t lighting_filament_cap(void);
