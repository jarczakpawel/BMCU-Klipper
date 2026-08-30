#pragma once
#include <stdint.h>

static inline void bmcu_uid_get(uint8_t out[12])
{
    const volatile uint32_t *p = (const volatile uint32_t*)0x1FFFF7E8u;
    for (int i = 0; i < 3; i++)
    {
        const uint32_t w = p[i];
        out[i * 4 + 0] = (uint8_t)(w);
        out[i * 4 + 1] = (uint8_t)(w >> 8);
        out[i * 4 + 2] = (uint8_t)(w >> 16);
        out[i * 4 + 3] = (uint8_t)(w >> 24);
    }
}

static inline void bmcu_uid_hex(const uint8_t uid[12], char out[25])
{
    static const char h[] = "0123456789ABCDEF";
    for (int i = 0; i < 12; i++)
    {
        out[i * 2 + 0] = h[(uid[i] >> 4) & 0x0F];
        out[i * 2 + 1] = h[uid[i] & 0x0F];
    }
    out[24] = 0;
}
