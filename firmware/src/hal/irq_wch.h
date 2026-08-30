#pragma once
#include <stdint.h>

#ifdef BMCU_HOST_TEST

static inline uint32_t irq_save_wch(void) { return 0u; }
static inline void irq_restore_wch(uint32_t) {}

#else

static inline __attribute__((always_inline)) uint32_t irq_save_wch(void)
{
    uint32_t s;
    __asm volatile("csrr %0, 0x800" : "=r"(s) :: "memory");
    __asm volatile("csrc 0x800, %0" :: "r"(0x88u) : "memory");
    return (s & 0x88u);
}

static inline __attribute__((always_inline)) void irq_restore_wch(uint32_t s88)
{
    const uint32_t m = 0x88u;
    __asm volatile("csrc 0x800, %0" :: "r"(m)   : "memory");
    __asm volatile("csrs 0x800, %0" :: "r"(s88) : "memory");
}

#endif
