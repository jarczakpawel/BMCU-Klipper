#pragma once
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

size_t bmcu_cobs_encode(const uint8_t *in, size_t len, uint8_t *out);
int bmcu_cobs_decode(const uint8_t *in, size_t len, uint8_t *out);

#ifdef __cplusplus
}
#endif
