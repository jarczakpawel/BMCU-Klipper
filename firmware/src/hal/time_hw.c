#include "hal/time_hw.h"
#include "ch32v20x.h"

uint32_t time_hw_tpus = 1;
uint32_t time_hw_tpms = 1000;

static inline __attribute__((always_inline)) uint64_t ticks64_raw(void)
{
    uint32_t hi1, lo, hi2;
    do {
        hi1 = STK_CNTH;
        lo  = STK_CNTL;
        hi2 = STK_CNTH;
    } while (hi1 != hi2);
    return ((uint64_t)hi1 << 32) | (uint64_t)lo;
}

void time_hw_init(void)
{
    STK_CTLR = 0;
    STK_SR   = 0;
    STK_CNTL = 0;
    STK_CNTH = 0;

    STK_CMPLR = 0xFFFFFFFFu;
    STK_CMPHR = 0xFFFFFFFFu;

    STK_CTLR = (1u << 3) | (1u << 0);

    uint32_t tpus = (SystemCoreClock / 8u) / 1000000u;
    if (!tpus) tpus = 1u;
    time_hw_tpus = tpus;

    uint32_t tpms = tpus * 1000u;
    if (!tpms) tpms = 1u;
    time_hw_tpms = tpms;
}

uint32_t time_hw_ticks_per_us(void) { return time_hw_tpus; }
uint32_t time_hw_ticks_per_ms(void) { return time_hw_tpms; }

uint64_t time_ticks64(void)
{
    return ticks64_raw();
}

uint64_t time_us64(void)
{
    const uint64_t t = ticks64_raw();
    const uint32_t d = time_hw_tpus;
    if (__builtin_expect(d == 1u, 0)) return t;
    return t / (uint64_t)d;
}

uint64_t time_ms64(void)
{
    const uint64_t t = ticks64_raw();
    const uint32_t d = time_hw_tpms;
    if (__builtin_expect(d == 1u, 0)) return t;
    return t / (uint64_t)d;
}

void delay_us(uint32_t us)
{
    if (!us) return;
    uint64_t t = (uint64_t)us * (uint64_t)time_hw_tpus;
    if (t > 0xFFFFFFFFu) t = 0xFFFFFFFFu;
    delayTicks32((uint32_t)t);
}

void delay(uint32_t ms)
{
    if (!ms) return;
    uint64_t t = (uint64_t)ms * (uint64_t)time_hw_tpms;
    if (t > 0xFFFFFFFFu) t = 0xFFFFFFFFu;
    delayTicks32((uint32_t)t);
}
