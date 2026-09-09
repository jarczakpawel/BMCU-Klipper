#pragma once
#include <stdint.h>
#include <stdbool.h>

#define BMCU_PROTO_VER 1u
#define BMCU_FW_MAJOR 1u
#define BMCU_FW_MINOR 0u
#define BMCU_FW_PATCH 1u

#define MSG_HELLO               0x01u
#define MSG_PING                0x02u
#define MSG_GET_STATUS          0x03u
#define MSG_GET_CAPS            0x04u
#define MSG_GET_CALIBRATION     0x05u
#define MSG_GET_OP_RESULT       0x06u
#define MSG_GET_SNAPSHOT        0x07u
#define MSG_SESSION_CONFIRM     0x08u

#define MSG_SET_MOTION          0x10u
#define MSG_SET_ACTIVE_CH       0x11u
#define MSG_CAL_CAPTURE         0x12u
#define MSG_CAL_COMMIT          0x13u
#define MSG_TEST_ENCODER        0x14u
#define MSG_CHANNEL_AUTOLOAD    0x15u
#define MSG_ABORT_OP            0x16u
#define MSG_CLEAR_CALIBRATION   0x17u
#define MSG_FEED_TO_CONTACT     0x18u
#define MSG_FEED_DISTANCE       0x19u
#define MSG_MARK_UNLOADED       0x1Au
#define MSG_MARK_LOADED         0x1Bu
#define MSG_CAL_AUTO_START      0x1Cu

#define MSG_SET_SLOT_INFO       0x20u
#define MSG_GET_SLOT_INFO       0x21u
#define MSG_SET_SLOTS           0x22u
#define MSG_CHANNEL_RETRACT      0x23u
#define MSG_CONFIG_GET          0x30u
#define MSG_CONFIG_SET          0x31u
#define MSG_CONFIG_SAVE         0x32u
#define MSG_SET_SYSTEM_LED      0x33u
#define MSG_RUNTIME_SYNC        0x34u
#define MSG_SET_LIGHTING        0x35u
#define MSG_LED_PREVIEW         0x36u
#define MSG_STOP_ALL            0x50u
#define MSG_RESET_ERROR         0x51u
#define MSG_REBOOT              0x53u
#define MSG_UPDATE_PREPARE      0x60u
#define MSG_NVM_READ            0x61u
#define MSG_UPDATE_CANCEL       0x62u

#define MSG_HELLO_ACK           0x81u
#define MSG_PONG                0x82u
#define MSG_STATUS              0x90u
#define MSG_CAPS                0x91u
#define MSG_SLOT_INFO           0x92u
#define MSG_CONFIG_VAL          0x93u
#define MSG_CALIBRATION         0x94u
#define MSG_OP_RESULT           0x95u
#define MSG_NVM_DATA            0x96u
#define MSG_SNAPSHOT            0x97u
#define MSG_STATE_CHANGED       0xA0u
#define MSG_MOTION_DONE         0xA1u
#define MSG_JAM                 0xA2u
#define MSG_ERROR               0xA4u
#define MSG_ACK                 0xB0u

#define BMCU_CAP_BUFFER_CAL_3PT  (1u << 0)
#define BMCU_CAP_ENCODER_TEST    (1u << 1)
#define BMCU_CAP_CHANNEL_AUTOLOAD (1u << 2)
#define BMCU_CAP_SLOT_METADATA   (1u << 3)
#define BMCU_CAP_ASYNC_OPS       (1u << 4)
#define BMCU_CAP_LOCAL_CONTACT    (1u << 5)
#define BMCU_CAP_LOCAL_DISTANCE   (1u << 6)
#define BMCU_CAP_OP_REPLAY         (1u << 7)
#define BMCU_CAP_ROUTE_STATE       (1u << 8)
#define BMCU_CAP_VOLATILE_SLOTS    (1u << 9)
#define BMCU_CAP_ROUTE_CONFIRM     (1u << 10)
#define BMCU_CAP_HOST_RUNTIME_CONFIG (1u << 11)
#define BMCU_CAP_SYSTEM_LED_RUNTIME  (1u << 12)
#define BMCU_CAP_NVM_EXPORT          (1u << 13)
#define BMCU_CAP_UPDATE_GUARD        (1u << 14)
#define BMCU_CAP_SCALABLE_SYNC       (1u << 15)
#define BMCU_CAP_SESSION_CONFIRM     (1u << 16)
#define BMCU_CAP_AUTO_CALIBRATION    (1u << 17)
#define BMCU_CAP_INDEPENDENT_OUTPUTS  (1u << 18)
#define BMCU_CAP_CHANNEL_AUTOLOAD_RUNTIME (1u << 19)
#define BMCU_CAP_RUNTIME_LIGHTING       (1u << 20)
#define BMCU_CAP_CHANNEL_RETRACT         (1u << 21)
#define BMCU_CAP_LOAD_PRESSURE_PCT        (1u << 22)
#define BMCU_CAP_LED_PREVIEW               (1u << 23)
#define BMCU_CAP_LED_FILAMENT_PREVIEW      (1u << 24)

#define BMCU_MOTION_IDLE             0u
#define BMCU_MOTION_SEND_OUT         1u
#define BMCU_MOTION_BEFORE_ON_USE    2u
#define BMCU_MOTION_ON_USE           3u
#define BMCU_MOTION_BEFORE_PULL_BACK 4u
#define BMCU_MOTION_PULL_BACK        5u
#define BMCU_MOTION_STOP_ON_USE      6u

void bmcu_protocol_init(void);
void bmcu_protocol_run(void);
int bmcu_protocol_error_state(void);
bool bmcu_protocol_update_mode(void);
void bmcu_protocol_send_hello(void);
