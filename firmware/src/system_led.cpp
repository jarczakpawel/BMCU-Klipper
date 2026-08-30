#include "system_led.h"
#include "ws2812.h"
#include "lighting.h"
#include "hal/time_hw.h"

extern WS2812_class SYS_RGB;

static uint8_t g_update_mode = 0u;
static uint32_t g_last_update = 0u;
static uint32_t g_last_rgb = 0xFFFFFFFFu;

static uint8_t smooth_breathe(uint32_t milliseconds, uint32_t period_ms,
                              uint8_t minimum, uint8_t maximum)
{
    if (period_ms < 2u || maximum <= minimum) return maximum;
    const uint32_t half = period_ms / 2u;
    uint32_t phase = milliseconds % period_ms;
    uint32_t x = phase <= half ? phase : period_ms - phase;
    x = (x * 255u) / half;
    const uint32_t smooth = (x * x * (765u - 2u * x) + 32512u) / 65025u;
    return (uint8_t)(minimum + ((uint32_t)(maximum - minimum) * smooth + 127u) / 255u);
}

static void apply_rgb(uint8_t red, uint8_t green, uint8_t blue)
{
    const uint32_t packed = ((uint32_t)red << 16) | ((uint32_t)green << 8) | blue;
    if (packed == g_last_rgb) return;
    g_last_rgb = packed;
    SYS_RGB.set_RGB(red, green, blue, 0u);
}

static void apply_effect(uint8_t red, uint8_t green, uint8_t blue)
{
    lighting_scale_system_effect(&red, &green, &blue);
    apply_rgb(red, green, blue);
}

void system_led_init(void)
{
    g_last_update = 0u;
    g_last_rgb = 0xFFFFFFFFu;
    apply_effect(0x10u, 0x00u, 0x00u);
}

void system_led_set_normal_rgb(uint8_t red, uint8_t green, uint8_t blue)
{
    lighting_set_system_color(red, green, blue);
    g_last_rgb = 0xFFFFFFFFu;
}

void system_led_set_update_mode(bool active)
{
    g_update_mode = active ? 1u : 0u;
}

void system_led_run(bool host_online, bool calibrating, bool setup_required)
{
    uint32_t ticks_per_ms = time_hw_tpms;
    if (!ticks_per_ms) ticks_per_ms = 1u;
    const uint32_t now = time_ticks32();
    if (g_last_update && (uint32_t)(now - g_last_update) < ticks_per_ms * 20u) return;
    g_last_update = now;
    const uint32_t milliseconds = now / ticks_per_ms;

    if (g_update_mode)
    {
        const uint8_t brightness = smooth_breathe(milliseconds, 1000u, 4u, 48u);
        apply_effect(0u, (uint8_t)(brightness / 2u), brightness);
        return;
    }
    if (!host_online)
    {
        const uint8_t brightness = smooth_breathe(milliseconds, 1800u, 3u, 40u);
        apply_effect(brightness, 0u, 0u);
        return;
    }
    if (calibrating)
    {
        const uint8_t brightness = smooth_breathe(milliseconds, 1400u, 5u, 48u);
        apply_effect(brightness, brightness, 0u);
        return;
    }
    if (setup_required)
    {

        const uint8_t brightness = smooth_breathe(
            milliseconds, 1400u, 5u, 48u);
        apply_effect(brightness, brightness, 0u);
        return;
    }
    uint8_t red, green, blue;
    lighting_system_rgb(&red, &green, &blue);
    apply_rgb(red, green, blue);
}
