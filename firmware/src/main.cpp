#include "MC_PULL_calibration.h"
#include "ws2812.h"

#include "Flash_saves.h"
#include "Motion_control.h"
#include "ams.h"
#include "ADC_DMA.h"
#include "Debug_log.h"
#include "bmcu_uart.h"
#include "bmcu_protocol.h"
#include "bmcu_config.h"
#include "system_led.h"
#include "lighting.h"
#include <string.h>
#include "ch32v20x.h"
#include "ch32v20x_rcc.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_misc.h"

#ifndef BMCU_UART_BAUD
#define BMCU_UART_BAUD 115200
#endif

WS2812_class SYS_RGB;
WS2812_class RGBOUT[4];

static uint8_t g_rgb_preview_active = 0u;
static uint8_t g_rgb_preview_target = 0u;
static uint32_t g_rgb_preview_deadline = 0u;

static void RGB_preview_restore(void)
{
    if (g_rgb_preview_target == 2u)
        SYS_RGB.restore();
    else
        for (uint8_t i = 0u; i < 4u; i++) RGBOUT[i].restore();
}

bool RGB_preview(uint8_t target, uint8_t red, uint8_t green, uint8_t blue)
{
    if (target > 3u) return false;
    if (g_rgb_preview_active && target != g_rgb_preview_target)
        RGB_preview_restore();

    if (target == 2u)
        SYS_RGB.preview_RGB(red, green, blue, 0u);
    else if (target == 3u)
    {
        static const uint8_t reference[4][3] = {
            {255u, 0u, 0u}, {255u, 255u, 0u},
            {0u, 255u, 0u}, {0u, 0u, 255u}
        };
        for (uint8_t i = 0u; i < 4u; i++)
            RGBOUT[i].preview_filament_RGB(
                reference[i][0], reference[i][1], reference[i][2], 1u, red);
    }
    else
        for (uint8_t i = 0u; i < 4u; i++)
            RGBOUT[i].preview_RGB(red, green, blue, target);

    uint32_t ticks_per_ms = time_hw_tpms;
    if (!ticks_per_ms) ticks_per_ms = 1u;
    g_rgb_preview_target = target;
    g_rgb_preview_deadline = time_ticks32() + 3000u * ticks_per_ms;
    g_rgb_preview_active = 1u;
    return true;
}

void RGB_init()
{
    SYS_RGB.init(1, GPIOD, GPIO_Pin_1);
    RGBOUT[0].init(2, GPIOA, GPIO_Pin_11);
    RGBOUT[1].init(2, GPIOA, GPIO_Pin_8);
    RGBOUT[2].init(2, GPIOB, GPIO_Pin_1);
    RGBOUT[3].init(2, GPIOB, GPIO_Pin_0);
}

void RGB_update()
{
    uint8_t preview_board = 0u;
    uint8_t preview_channels = 0u;
    if (g_rgb_preview_active)
    {
        if ((int32_t)(time_ticks32() - g_rgb_preview_deadline) >= 0)
        {
            RGB_preview_restore();
            g_rgb_preview_active = 0u;
        }
        else
        {
            preview_board = g_rgb_preview_target == 2u;
            preview_channels = g_rgb_preview_target != 2u;
        }
    }

    const bool board_dirty = !preview_board && SYS_RGB.is_dirty();
    const bool channels_dirty = !preview_channels &&
        (RGBOUT[0].is_dirty() || RGBOUT[1].is_dirty() ||
         RGBOUT[2].is_dirty() || RGBOUT[3].is_dirty());
    if (!board_dirty && !channels_dirty) return;

    static uint32_t last = 0u;

    uint32_t min_gap = time_hw_tpms;
    if (!min_gap) min_gap = 1u;

    const uint32_t now = time_ticks32();
    if (last != 0u && (uint32_t)(now - last) < min_gap)
        return;

    last = now;

    if (!preview_board) SYS_RGB.updata();
    if (!preview_channels)
    {
        RGBOUT[0].updata();
        RGBOUT[1].updata();
        RGBOUT[2].updata();
        RGBOUT[3].updata();
    }
}

static uint8_t g_route_state[4] = {
    AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY
};
static uint8_t g_state_dirty = 0u;

static bool route_state_value_valid(uint8_t value)
{
    return value == AMS_ROUTE_EMPTY || value == AMS_ROUTE_LOADED ||
           value == AMS_ROUTE_UNCERTAIN;
}

static bool route_state_all_valid(void)
{
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (!route_state_value_valid(g_route_state[ch])) return false;
    return true;
}

bool ams_state_begin_change(uint8_t filament_ch)
{
    if (filament_ch >= 4u || Flash_saves_faulted()) return false;
    g_route_state[filament_ch] = AMS_ROUTE_UNCERTAIN;
    g_state_dirty = 1u;

    return ams_state_save_run();
}

bool ams_state_set_loaded(uint8_t filament_ch)
{
    if (filament_ch >= 4u) return false;
    const uint8_t previous = g_route_state[filament_ch];
    const uint8_t previous_dirty = g_state_dirty;
    g_route_state[filament_ch] = AMS_ROUTE_LOADED;
    g_state_dirty = 1u;
    if (ams_state_save_run()) return true;
    g_route_state[filament_ch] = previous;
    g_state_dirty = previous_dirty;
    return false;
}

bool ams_state_set_unloaded(uint8_t filament_ch)
{
    if (filament_ch >= 4u) return false;
    const uint8_t previous = g_route_state[filament_ch];
    const uint8_t previous_dirty = g_state_dirty;
    g_route_state[filament_ch] = AMS_ROUTE_EMPTY;
    g_state_dirty = 1u;
    if (ams_state_save_run()) return true;
    g_route_state[filament_ch] = previous;
    g_state_dirty = previous_dirty;
    return false;
}

uint8_t ams_state_get_route_state(uint8_t filament_ch)
{
    return filament_ch < 4u ? g_route_state[filament_ch] : (uint8_t)AMS_ROUTE_UNCERTAIN;
}

uint8_t ams_state_get_loaded_mask(void)
{
    uint8_t mask = 0u;
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (g_route_state[ch] == AMS_ROUTE_LOADED) mask |= (uint8_t)(1u << ch);
    return mask;
}

uint8_t ams_state_get_uncertain_mask(void)
{
    uint8_t mask = 0u;
    for (uint8_t ch = 0u; ch < 4u; ch++)
        if (g_route_state[ch] == AMS_ROUTE_UNCERTAIN) mask |= (uint8_t)(1u << ch);
    return mask;
}

bool ams_state_save_run()
{
    if (!g_state_dirty) return true;
    if (!route_state_all_valid()) return false;
    if (!Flash_AMS_state_write(g_route_state)) return false;
    g_state_dirty = 0u;
    return true;
}

int main(void)
{
    SystemInit();
    SystemCoreClockUpdate();
    Motion_control_boot_safe_init();
    time_hw_init();

    __enable_irq();

    WWDG_DeInit();
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_WWDG, DISABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_AFIO, ENABLE);

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    GPIO_PinRemapConfig(GPIO_Remap_PD01, ENABLE);

    RGB_init();
    delay(10);

    lighting_init();
    system_led_init();
    for (int i = 0; i < 4; i++) RGBOUT[i].set_RGB(0, 0, 0, 0);
    RGB_update();
    delay(50);

    DEBUG_init();
    ams_init();
    Flash_saves_init();

    ADC_DMA_init();
    ADC_DMA_wait_full();

    MC_PULL_calibration_boot();

    {
        uint8_t route_state[4] = {
            AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY
        };
        if (Flash_AMS_state_read(route_state))
        {
            memcpy(g_route_state, route_state, sizeof(g_route_state));

            _ams* a = &ams[0];
            a->now_filament_num = 0xFFu;
            a->filament_use_flag = 0u;
            a->pressure = 0u;
            for (uint8_t i = 0u; i < 4u; i++)
                a->filament[i].motion = _filament_motion::idle;
        }
    }

    bmcu_config_load();
    Motion_control_init();

    bmcu_uart_init(BMCU_UART_BAUD);
    bmcu_protocol_init();

    DEBUG("BMCU KLIPPER START\n");

    while (1)
    {
        Motion_control_run(bmcu_protocol_error_state());
        bmcu_protocol_run();
        ams_state_save_run();
        RGB_update();
    }
}
