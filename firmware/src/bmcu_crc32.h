#pragma once
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

uint32_t bmcu_crc32(const void *data, size_t len);

#ifdef __cplusplus
}
#endif
