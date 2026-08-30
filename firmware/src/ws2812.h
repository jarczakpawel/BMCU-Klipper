#pragma once
#include <stdint.h>
#include "ch32v20x.h"

#ifndef BMCU_ONLINE_LED_FILAMENT_RGB
#define BMCU_ONLINE_LED_FILAMENT_RGB 0
#endif

class WS2812_class
{
public:
    static constexpr uint8_t MAX_NUM = 4;

    void init(uint8_t num, GPIO_TypeDef* port, uint16_t pin);

    void clear(void);
    void RST(void);
    void updata(void);

    void set_RGB(uint8_t R, uint8_t G, uint8_t B, uint8_t index);
    void preview_RGB(uint8_t R, uint8_t G, uint8_t B, uint8_t index);
    void preview_filament_RGB(uint8_t R, uint8_t G, uint8_t B, uint8_t index, uint8_t cap);
    void restore(void);

    void set_RGB_online(uint8_t R, uint8_t G, uint8_t B, uint8_t index, bool filament = false);

    inline bool is_dirty() const { return dirty; }

private:
    GPIO_TypeDef* port = nullptr;
    uint16_t      pin  = 0;
    uint8_t       num  = 0;

    uint32_t last_grb[MAX_NUM] = {0u, 0u, 0u, 0u};

    uint32_t last_online_raw_rgb[MAX_NUM]   = {0u, 0u, 0u, 0u};
    uint8_t  last_online_is_filament[MAX_NUM] = {0u, 0u, 0u, 0u};
    uint32_t last_online_filament_cap[MAX_NUM] = {0u, 0u, 0u, 0u};

    void write_frame(uint8_t override_index, uint32_t override_grb);

    bool dirty = false;
};
