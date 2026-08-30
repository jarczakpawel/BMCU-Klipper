#pragma once
#include <stdint.h>
#include <stdbool.h>

void system_led_init(void);
void system_led_set_normal_rgb(uint8_t red, uint8_t green, uint8_t blue);
void system_led_set_update_mode(bool active);
void system_led_run(bool host_online, bool calibrating, bool setup_required);
