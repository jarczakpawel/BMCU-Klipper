#pragma once
#include <stdint.h>
#include <stdbool.h>

#ifndef BAMBU_BUS_AMS_NUM
#define BAMBU_BUS_AMS_NUM 0
#endif

#define FLASH_NVM_BASE_ADDR       ((uint32_t)0x0800F000)
#define FLASH_NVM256_PAGE_SIZE    (256u)
#define FLASH_NVM_TOTAL_SIZE      (4096u)
#define FLASH_NVM_PAGE_COUNT      (FLASH_NVM_TOTAL_SIZE / FLASH_NVM256_PAGE_SIZE)

#define FLASH_NVM_CAL_PAGE_FIRST      (0u)
#define FLASH_NVM_CAL_PAGE_COUNT      (4u)
#define FLASH_NVM_MOTION_PAGE_FIRST   (4u)
#define FLASH_NVM_MOTION_PAGE_COUNT   (2u)
#define FLASH_NVM_STATE_PAGE_FIRST    (6u)
#define FLASH_NVM_STATE_PAGE_COUNT    (8u)
#define FLASH_NVM_RESERVED_PAGE_FIRST (14u)
#define FLASH_NVM_RESERVED_PAGE_COUNT (2u)

static constexpr uint32_t MAGIC_CAL = 0x324C4143u;
static constexpr uint32_t MAGIC_MOT = 0x31544F4Du;
static constexpr uint32_t MAGIC_STA = 0x32415453u;
static constexpr uint16_t NVM_FORMAT_VERSION = 1u;

void Flash_saves_init(void);

bool Flash_saves_faulted(void);
uint16_t Flash_saves_bad_page_mask(void);

bool Flash_AMS_state_read(uint8_t route_state[4]);
bool Flash_AMS_state_write(const uint8_t route_state[4]);

bool Flash_MC_PULL_cal_read(float offs[4], float vmin[4], float vmax[4], int8_t pol[4], uint8_t* valid_mask);
bool Flash_MC_PULL_cal_write_all(const float offs[4], const float vmin[4], const float vmax[4], const int8_t pol[4], uint8_t valid_mask);
bool Flash_MC_PULL_cal_clear(void);

bool Flash_Motion_read(void* out, uint16_t bytes);
bool Flash_Motion_write(const void* in, uint16_t bytes);
bool Flash_Motion_clear(void);

bool Flash_NVM_full_clear(void);
bool Flash_NVM_read_raw(uint16_t offset, void* out, uint16_t bytes);
