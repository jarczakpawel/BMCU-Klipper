#include "bmcu_uart.h"
#include "ch32v20x.h"
#include "ch32v20x_rcc.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_usart.h"
#include "ch32v20x_dma.h"
#include "hal/time_hw.h"
#include <string.h>

#define BMCU_UART_RX_SIZE 1024u
#define BMCU_UART_RX_MASK (BMCU_UART_RX_SIZE - 1u)
#define BMCU_UART_TX_FRAME_MAX 256u
#define BMCU_UART_TX_CRITICAL_SLOTS 4u
#define BMCU_UART_TX_NORMAL_SLOTS 2u

struct TxSlot
{
    uint16_t len;
    uint8_t data[BMCU_UART_TX_FRAME_MAX];
};

static uint8_t rx_dma_buf[BMCU_UART_RX_SIZE] __attribute__((aligned(4)));
static volatile uint16_t rx_tail = 0;

static TxSlot tx_critical[BMCU_UART_TX_CRITICAL_SLOTS];
static TxSlot tx_normal[BMCU_UART_TX_NORMAL_SLOTS];
static uint8_t tx_critical_head = 0u;
static uint8_t tx_critical_tail = 0u;
static uint8_t tx_critical_count = 0u;
static uint8_t tx_normal_head = 0u;
static uint8_t tx_normal_tail = 0u;
static uint8_t tx_normal_count = 0u;
static uint8_t tx_busy = 0u;
static uint8_t tx_wait_tc = 0u;
static uint8_t tx_active_critical = 0u;
static uint32_t tx_dropped = 0u;
static uint32_t tx_errors = 0u;
static uint32_t tx_started_tick = 0u;

static inline uint16_t rx_head(void)
{
    return (uint16_t)((BMCU_UART_RX_SIZE - DMA1_Channel5->CNTR) & BMCU_UART_RX_MASK);
}

static inline void uart_rx_dma_restart(void)
{
    DMA1_Channel5->CFGR &= (uint16_t)(~DMA_CFGR1_EN);
    DMA1_Channel5->MADDR = (uint32_t)(uintptr_t)rx_dma_buf;
    DMA1_Channel5->CNTR = BMCU_UART_RX_SIZE;
    DMA1_Channel5->CFGR |= DMA_CFGR1_EN;
    rx_tail = 0;
}

static inline void uart_clear_error_flags(void)
{
    const uint32_t sr = USART1->STATR;
    if (sr & (USART_FLAG_ORE | USART_FLAG_NE | USART_FLAG_FE | USART_FLAG_PE))
    {
        (void)USART1->DATAR;
    }
}

static void tx_pop_active(void)
{
    if (tx_active_critical)
    {
        if (tx_critical_count)
        {
            tx_critical_tail = (uint8_t)((tx_critical_tail + 1u) % BMCU_UART_TX_CRITICAL_SLOTS);
            tx_critical_count--;
        }
    }
    else if (tx_normal_count)
    {
        tx_normal_tail = (uint8_t)((tx_normal_tail + 1u) % BMCU_UART_TX_NORMAL_SLOTS);
        tx_normal_count--;
    }
}

static void tx_start_next(void)
{
    if (tx_busy || tx_wait_tc) return;

    TxSlot *slot = 0;
    if (tx_critical_count)
    {
        tx_active_critical = 1u;
        slot = &tx_critical[tx_critical_tail];
    }
    else if (tx_normal_count)
    {
        tx_active_critical = 0u;
        slot = &tx_normal[tx_normal_tail];
    }
    if (!slot || !slot->len) return;

    DMA_Cmd(DMA1_Channel4, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4);
    DMA1_Channel4->MADDR = (uint32_t)(uintptr_t)slot->data;
    DMA1_Channel4->CNTR = slot->len;
    USART_ClearFlag(USART1, USART_FLAG_TC);
    GPIOA->BCR = GPIO_Pin_12;
    tx_busy = 1u;
    tx_started_tick = time_ticks32();
    DMA_Cmd(DMA1_Channel4, ENABLE);
}

void bmcu_uart_init(uint32_t baud)
{
    GPIO_InitTypeDef gpio = {0};
    USART_InitTypeDef us = {0};
    DMA_InitTypeDef dma = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA, ENABLE);
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_DMA1, ENABLE);

    gpio.GPIO_Pin = GPIO_Pin_9;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    gpio.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOA, &gpio);

    gpio.GPIO_Pin = GPIO_Pin_10;
    gpio.GPIO_Mode = GPIO_Mode_IPU;
    GPIO_Init(GPIOA, &gpio);

    gpio.GPIO_Pin = GPIO_Pin_12;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    gpio.GPIO_Mode = GPIO_Mode_Out_PP;
    GPIO_Init(GPIOA, &gpio);
    GPIOA->BCR = GPIO_Pin_12;

    USART_DeInit(USART1);
    us.USART_BaudRate = baud;
    us.USART_WordLength = USART_WordLength_8b;
    us.USART_StopBits = USART_StopBits_1;
    us.USART_Parity = USART_Parity_No;
    us.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    us.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART1, &us);

    DMA_DeInit(DMA1_Channel5);
    dma.DMA_PeripheralBaseAddr = (uint32_t)(uintptr_t)&USART1->DATAR;
    dma.DMA_MemoryBaseAddr = (uint32_t)(uintptr_t)rx_dma_buf;
    dma.DMA_DIR = DMA_DIR_PeripheralSRC;
    dma.DMA_BufferSize = BMCU_UART_RX_SIZE;
    dma.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    dma.DMA_MemoryInc = DMA_MemoryInc_Enable;
    dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    dma.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    dma.DMA_Mode = DMA_Mode_Circular;
    dma.DMA_Priority = DMA_Priority_VeryHigh;
    dma.DMA_M2M = DMA_M2M_Disable;
    DMA_Init(DMA1_Channel5, &dma);

    DMA_DeInit(DMA1_Channel4);
    dma.DMA_PeripheralBaseAddr = (uint32_t)(uintptr_t)&USART1->DATAR;
    dma.DMA_MemoryBaseAddr = (uint32_t)(uintptr_t)tx_critical[0].data;
    dma.DMA_DIR = DMA_DIR_PeripheralDST;
    dma.DMA_BufferSize = 1u;
    dma.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    dma.DMA_MemoryInc = DMA_MemoryInc_Enable;
    dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    dma.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    dma.DMA_Mode = DMA_Mode_Normal;
    dma.DMA_Priority = DMA_Priority_High;
    dma.DMA_M2M = DMA_M2M_Disable;
    DMA_Init(DMA1_Channel4, &dma);
    DMA_Cmd(DMA1_Channel4, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4);

    USART1->CTLR3 |= USART_DMAReq_Rx | USART_DMAReq_Tx;
    USART_Cmd(USART1, ENABLE);
    uart_rx_dma_restart();

    tx_critical_head = tx_critical_tail = tx_critical_count = 0u;
    tx_normal_head = tx_normal_tail = tx_normal_count = 0u;
    tx_busy = tx_wait_tc = 0u;
    tx_started_tick = 0u;
    tx_dropped = tx_errors = 0u;
}

bool bmcu_uart_read_byte(uint8_t *out)
{
    const uint16_t head = rx_head();
    if (rx_tail == head)
    {
        uart_clear_error_flags();
        return false;
    }
    *out = rx_dma_buf[rx_tail];
    rx_tail = (uint16_t)((rx_tail + 1u) & BMCU_UART_RX_MASK);
    return true;
}

bool bmcu_uart_write(const uint8_t *data, uint16_t len, bool critical)
{
    if ((!data && len) || !len || len > BMCU_UART_TX_FRAME_MAX) return false;

    TxSlot *slot = 0;
    if (critical)
    {
        if (tx_critical_count >= BMCU_UART_TX_CRITICAL_SLOTS)
        {
            tx_dropped++;
            return false;
        }
        slot = &tx_critical[tx_critical_head];
        tx_critical_head = (uint8_t)((tx_critical_head + 1u) % BMCU_UART_TX_CRITICAL_SLOTS);
        tx_critical_count++;
    }
    else
    {
        if (tx_normal_count >= BMCU_UART_TX_NORMAL_SLOTS)
        {
            uint8_t replace = (uint8_t)((tx_normal_head + BMCU_UART_TX_NORMAL_SLOTS - 1u) %
                                        BMCU_UART_TX_NORMAL_SLOTS);
            if ((tx_busy || tx_wait_tc) && !tx_active_critical)
                replace = (uint8_t)((tx_normal_tail + 1u) % BMCU_UART_TX_NORMAL_SLOTS);
            slot = &tx_normal[replace];
            tx_dropped++;
        }
        else
        {
            slot = &tx_normal[tx_normal_head];
            tx_normal_head = (uint8_t)((tx_normal_head + 1u) % BMCU_UART_TX_NORMAL_SLOTS);
            tx_normal_count++;
        }
    }

    slot->len = len;
    memcpy(slot->data, data, len);
    tx_start_next();
    return true;
}

void bmcu_uart_discard_pending(void)
{
    const bool active = tx_busy || tx_wait_tc;
    if (!active)
    {
        tx_critical_head = tx_critical_tail = tx_critical_count = 0u;
        tx_normal_head = tx_normal_tail = tx_normal_count = 0u;
        return;
    }

    if (tx_active_critical)
    {
        tx_critical_count = tx_critical_count ? 1u : 0u;
        tx_critical_head = (uint8_t)((tx_critical_tail + tx_critical_count) %
                                     BMCU_UART_TX_CRITICAL_SLOTS);
        tx_normal_head = tx_normal_tail = tx_normal_count = 0u;
    }
    else
    {
        tx_normal_count = tx_normal_count ? 1u : 0u;
        tx_normal_head = (uint8_t)((tx_normal_tail + tx_normal_count) %
                                   BMCU_UART_TX_NORMAL_SLOTS);
        tx_critical_head = tx_critical_tail = tx_critical_count = 0u;
    }
}

void bmcu_uart_poll(void)
{
    uart_clear_error_flags();

    const uint32_t flags = DMA1->INTFR;
    if (tx_busy && (flags & DMA1_FLAG_TE4))
    {
        DMA_Cmd(DMA1_Channel4, DISABLE);
        DMA_ClearFlag(DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4);
        tx_busy = 0u;
        tx_wait_tc = 0u;
        tx_errors++;
        tx_pop_active();
        GPIOA->BCR = GPIO_Pin_12;
    }
    else if (tx_busy && (flags & DMA1_FLAG_TC4))
    {
        DMA_Cmd(DMA1_Channel4, DISABLE);
        DMA_ClearFlag(DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4);
        tx_busy = 0u;
        tx_wait_tc = 1u;
    }

    if (tx_wait_tc && USART_GetFlagStatus(USART1, USART_FLAG_TC) != RESET)
    {
        tx_wait_tc = 0u;
        tx_pop_active();
        GPIOA->BCR = GPIO_Pin_12;
    }

    uint32_t ticks_per_ms = time_hw_tpms;
    if (!ticks_per_ms) ticks_per_ms = 1u;
    if ((tx_busy || tx_wait_tc) &&
        (uint32_t)(time_ticks32() - tx_started_tick) > ticks_per_ms * 100u)
    {
        DMA_Cmd(DMA1_Channel4, DISABLE);
        DMA_ClearFlag(DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4);
        tx_busy = 0u;
        tx_wait_tc = 0u;
        tx_errors++;
        tx_pop_active();
        GPIOA->BCR = GPIO_Pin_12;
    }

    tx_start_next();
}

bool bmcu_uart_tx_idle(void)
{
    return !tx_busy && !tx_wait_tc && !tx_critical_count && !tx_normal_count;
}

uint8_t bmcu_uart_critical_free(void)
{
    return (uint8_t)(BMCU_UART_TX_CRITICAL_SLOTS - tx_critical_count);
}

uint32_t bmcu_uart_tx_drop_count(void)
{
    return tx_dropped;
}

uint32_t bmcu_uart_tx_error_count(void)
{
    return tx_errors;
}
