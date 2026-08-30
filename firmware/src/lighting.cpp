#include "lighting.h"
#include <string.h>

struct LightingConfig
{
    uint8_t system_rgb[3];
    uint8_t system_brightness;
    uint8_t filament_cap;
    uint8_t buffer_rgb[BMCU_BUFFER_LED_COUNT][3];
    uint8_t status_rgb[BMCU_LED_STATUS_COUNT][3];
};

static LightingConfig g_lighting;

static inline __attribute__((always_inline)) uint8_t scale_q8(uint8_t value, uint8_t scale)
{

    return (uint8_t)(((uint16_t)value * ((uint16_t)scale + 1u)) >> 8);
}

static inline __attribute__((always_inline)) void scale_rgb_q8(
    const uint8_t input[3], uint8_t scale,
    uint8_t *red, uint8_t *green, uint8_t *blue)
{
    *red = scale_q8(input[0], scale);
    *green = scale_q8(input[1], scale);
    *blue = scale_q8(input[2], scale);
}

void lighting_init(void)
{
    static const LightingConfig defaults = {
        {0xFFu, 0xFFu, 0xFFu}, 0xFFu, 96u,
        {
            {0x00u, 0x00u, 0xFFu},
            {0xFFu, 0x80u, 0x00u},
            {0xFFu, 0x00u, 0x00u},
        },
        {
            {0x38u, 0x35u, 0x32u},
            {0xFFu, 0xFFu, 0x00u},
            {0x00u, 0xD5u, 0x2Au},
            {0x00u, 0xB0u, 0xFFu},
            {0xFFu, 0xA0u, 0x00u},
            {0xA0u, 0x2Du, 0xFFu},
            {0xFFu, 0xFFu, 0x00u},
            {0xFFu, 0x00u, 0x00u},
            {0x00u, 0x00u, 0x00u},

            {0xA0u, 0x2Du, 0xFFu},
        },
    };
    g_lighting = defaults;
}

bool lighting_apply_payload(const uint8_t *payload, uint16_t len)
{
    if (!payload || len != BMCU_LIGHTING_PAYLOAD_SIZE ||
            payload[0] != BMCU_LIGHTING_VERSION)
        return false;
    LightingConfig next = g_lighting;
    next.system_rgb[0] = payload[1];
    next.system_rgb[1] = payload[2];
    next.system_rgb[2] = payload[3];
    next.system_brightness = payload[4];
    next.filament_cap = payload[5];

    uint16_t offset = 6u;
    for (uint8_t index = 0u; index < BMCU_BUFFER_LED_COUNT; index++)
    {
        memcpy(next.buffer_rgb[index], payload + offset, 3u);
        offset += 3u;
    }
    for (uint8_t index = 0u; index < BMCU_LED_STATUS_COUNT; index++)
    {
        if (index == BMCU_LED_REDETECT)
        {
            next.status_rgb[index][0] = 0xFFu;
            next.status_rgb[index][1] = 0xFFu;
            next.status_rgb[index][2] = 0x00u;
        }
        else
        {
            memcpy(next.status_rgb[index], payload + offset, 3u);
        }
        offset += 3u;
    }
    if (offset != BMCU_LIGHTING_PAYLOAD_SIZE)
        return false;

    g_lighting = next;
    return true;
}

void lighting_set_system_color(uint8_t red, uint8_t green, uint8_t blue)
{
    g_lighting.system_rgb[0] = red;
    g_lighting.system_rgb[1] = green;
    g_lighting.system_rgb[2] = blue;
}

void lighting_system_rgb(uint8_t *red, uint8_t *green, uint8_t *blue)
{
    scale_rgb_q8(g_lighting.system_rgb, g_lighting.system_brightness,
                 red, green, blue);
}

void lighting_scale_system_effect(uint8_t *red, uint8_t *green, uint8_t *blue)
{
    *red = scale_q8(*red, g_lighting.system_brightness);
    *green = scale_q8(*green, g_lighting.system_brightness);
    *blue = scale_q8(*blue, g_lighting.system_brightness);
}

void lighting_status_rgb(uint8_t status, uint8_t *red, uint8_t *green, uint8_t *blue)
{
    if (status >= BMCU_LED_STATUS_COUNT) status = BMCU_LED_ERROR;
    const uint8_t *color = g_lighting.status_rgb[status];

    scale_rgb_q8(color, 128u, red, green, blue);
}

void lighting_buffer_rgb(uint8_t state, uint8_t *red, uint8_t *green, uint8_t *blue)
{
    if (state >= BMCU_BUFFER_LED_COUNT)
    {
        *red = *green = *blue = 0u;
        return;
    }

    scale_rgb_q8(g_lighting.buffer_rgb[state], 16u, red, green, blue);
}

uint8_t lighting_filament_cap(void)
{
    return g_lighting.filament_cap;
}
