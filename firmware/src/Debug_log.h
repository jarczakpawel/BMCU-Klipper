#ifndef __DEBUG_H
#define __DEBUG_H

#include <stdint.h>
#include "hal/time_hw.h"

#ifdef __cplusplus
extern "C" {
#endif

#define Debug_log_baudrate 115200

static inline void Delay_Init(void) { }
static inline void Delay_Us(uint32_t n) { delay_us(n); }
static inline void Delay_Ms(uint32_t n) { delay(n); }

void Debug_log_init(void);
uint64_t Debug_log_count64(void);
void Debug_log_time(void);
void Debug_log_write(const void *data);
void Debug_log_write_num(const void *data, int num);

#define DEBUG_time_log()

#ifdef Debug_log_on
  #define DEBUG_init()        Debug_log_init()
  #define DEBUG(logs)         Debug_log_write(logs)
  #define DEBUG_num(logs,num) Debug_log_write_num((logs),(num))
  #define DEBUG_time()        Debug_log_time()
  #define DEBUG_get_time()    Debug_log_count64()
#else
  #define DEBUG_init()        do{}while(0)
  #define DEBUG(logs)         do{}while(0)
  #define DEBUG_num(logs,num) do{}while(0)
  #define DEBUG_time()        do{}while(0)
  #define DEBUG_get_time()    (0ULL)
#endif

#ifdef __cplusplus
}
#endif

#endif
