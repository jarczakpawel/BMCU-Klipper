#pragma once
#include <stdint.h>
#define ams_max_number 4

enum class _filament_motion : uint8_t
{
    idle            = 0,
    send_out        = 1,
    on_use          = 2,
    before_pull_back= 3,
    pull_back       = 4,
    before_on_use   = 5,
    stop_on_use     = 6
};

struct _filament
{

    char bambubus_filament_id[8] = "GFG00";
    uint8_t color_R = 0xFF;
    uint8_t color_G = 0xFF;
    uint8_t color_B = 0xFF;
    uint8_t color_A = 0xFF;
    int16_t temperature_min = 220;
    int16_t temperature_max = 240;
    char name[20] = "PETG";
    uint64_t xhub_unique_id = 0;

    uint8_t dryer_power = 0;
    int8_t dryer_temperature = 0;
    uint16_t dryer_time_left = 0;

    bool online = true;
    _filament_motion motion = _filament_motion::idle;
    uint8_t seal_status = 0;
    int8_t compartment_temperature = 22;
    uint8_t compartment_humidity = 20;

    float meters __attribute__((aligned(4))) = 0.0f;
    float meters_virtual_count __attribute__((aligned(4))) = 0.0f;

    void init()
    {
        xhub_unique_id = 0;
        bambubus_filament_id[0] = 'G';
        bambubus_filament_id[1] = 'F';
        bambubus_filament_id[2] = 'G';
        bambubus_filament_id[3] = '0';
        bambubus_filament_id[4] = '0';
        bambubus_filament_id[5] = '\0';
        color_R = 0xFF;
        color_G = 0xFF;
        color_B = 0xFF;
        color_A = 0xFF;
        temperature_min = 220;
        temperature_max = 240;
        name[0] = 'P';
        name[1] = 'E';
        name[2] = 'T';
        name[3] = 'G';
        name[4] = '\0';
        meters = 0;
        meters_virtual_count = 0;
        online = true;
        motion = _filament_motion::idle;
        compartment_temperature = 22;
        compartment_humidity = 20;
    }

} __attribute__((packed, aligned(4)));

static_assert((__builtin_offsetof(_filament, meters) & 3u) == 0u, "meters misaligned");
static_assert((__builtin_offsetof(_filament, meters_virtual_count) & 3u) == 0u, "meters_virtual_count misaligned");

struct _ams
{
    uint8_t ams_type = 0;
    _filament filament[4];
    uint8_t now_filament_num = 0xFF;
    char name[8];
    uint8_t filament_use_flag = 0;
    uint16_t pressure = 0xFFFF;
    bool online = false;
    void init()
    {
        now_filament_num = 0xFF;
        filament_use_flag = 0;
        pressure = 0xFFFF;
        online = false;
        for (uint8_t i = 0; i < 4; i++)
        {
            filament[i].init();
        }
    }
} __attribute__((aligned(4)));

extern _ams ams[ams_max_number];
extern void ams_init();
extern uint8_t bus_now_ams_num;

enum : uint8_t
{
    AMS_ROUTE_EMPTY = 0u,
    AMS_ROUTE_LOADED = 1u,
    AMS_ROUTE_UNCERTAIN = 2u,
};

bool ams_state_begin_change(uint8_t filament_ch);
bool ams_state_set_loaded(uint8_t filament_ch);
bool ams_state_set_unloaded(uint8_t filament_ch);
uint8_t ams_state_get_route_state(uint8_t filament_ch);
uint8_t ams_state_get_loaded_mask(void);
uint8_t ams_state_get_uncertain_mask(void);
bool ams_state_save_run();
