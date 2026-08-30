#pragma once
#include <stdint.h>
#include <stdbool.h>

void bmcu_uart_init(uint32_t baud);
bool bmcu_uart_read_byte(uint8_t *out);

bool bmcu_uart_write(const uint8_t *data, uint16_t len, bool critical);
void bmcu_uart_poll(void);
void bmcu_uart_discard_pending(void);
bool bmcu_uart_tx_idle(void);
uint8_t bmcu_uart_critical_free(void);
uint32_t bmcu_uart_tx_drop_count(void);
uint32_t bmcu_uart_tx_error_count(void);
