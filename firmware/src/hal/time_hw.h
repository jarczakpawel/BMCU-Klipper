#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TIME_HW_STK_BASE (0xE000F000u)

#define STK_CTLR  (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x00u))
#define STK_SR    (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x04u))
#define STK_CNTL  (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x08u))
#define STK_CNTH  (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x0Cu))
#define STK_CMPLR (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x10u))
#define STK_CMPHR (*(volatile uint32_t *)(TIME_HW_STK_BASE + 0x14u))

void     time_hw_init(void);
uint32_t time_hw_ticks_per_us(void);
uint32_t time_hw_ticks_per_ms(void);

extern uint32_t time_hw_tpus;
extern uint32_t time_hw_tpms;

static inline __attribute__((always_inline)) uint32_t time_diff_u32(uint32_t a, uint32_t b)
{
    return (uint32_t)(a - b);
}

static inline __attribute__((always_inline)) int time_reached32(uint32_t now, uint32_t deadline)
{
    return ((time_diff_u32(now, deadline) >> 31) == 0u);
}

static inline __attribute__((always_inline)) int32_t time_diff32(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b);
}

static inline __attribute__((always_inline)) uint32_t ms_to_ticks32(uint32_t ms)
{
    const uint32_t tpm = time_hw_tpms;
    if (!ms || !tpm) return 0u;

    const uint32_t max_ms = 0xFFFFFFFFu / tpm;
    if (ms > max_ms) return 0xFFFFFFFFu;

    return ms * tpm;
}

static inline __attribute__((always_inline)) uint32_t us_to_ticks32(uint32_t us)
{
    const uint32_t tpu = time_hw_tpus;
    if (!us || !tpu) return 0u;

    const uint32_t max_us = 0xFFFFFFFFu / tpu;
    if (us > max_us) return 0xFFFFFFFFu;

    return us * tpu;
}

#ifdef BMCU_HOST_TEST
extern uint32_t bmcu_host_fake_ticks;
static inline __attribute__((always_inline)) uint32_t time_ticks32(void)
{
    return bmcu_host_fake_ticks++;
}
#else
static inline __attribute__((always_inline)) uint32_t time_ticks32(void)
{
    return STK_CNTL;
}
#endif

uint64_t time_ticks64(void);

uint64_t time_us64(void);
uint64_t time_ms64(void);

static inline __attribute__((always_inline)) void delayTicks32(uint32_t ticks)
{
    if (!ticks) return;
    const uint32_t t0 = STK_CNTL;
    while ((uint32_t)(STK_CNTL - t0) < ticks) {
        __asm__ volatile ("nop");
    }
}

void delay_us(uint32_t us);
void delay(uint32_t ms);

#ifdef __cplusplus
}
#endif
