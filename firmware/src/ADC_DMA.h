#pragma once

#include <stdbool.h>

void  ADC_DMA_init(void);
bool  ADC_DMA_is_inited(void);

void  ADC_DMA_gpio_analog(void);

void  ADC_DMA_poll(void);
const float* ADC_DMA_get_value(void);

void  ADC_DMA_filter_reset(void);
bool  ADC_DMA_ready(void);
bool  ADC_DMA_sample_ready(void);
void  ADC_DMA_wait_full(void);
