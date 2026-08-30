#include "bmcu_cobs.h"

size_t bmcu_cobs_encode(const uint8_t *in, size_t len, uint8_t *out)
{
    const uint8_t *end = in + len;
    uint8_t *start = out;
    uint8_t *code_ptr = out++;
    uint8_t code = 1;

    while (in < end)
    {
        if (*in == 0)
        {
            *code_ptr = code;
            code_ptr = out++;
            code = 1;
            in++;
        }
        else
        {
            *out++ = *in++;
            code++;
            if (code == 0xFF)
            {
                *code_ptr = code;
                code_ptr = out++;
                code = 1;
            }
        }
    }

    *code_ptr = code;
    return (size_t)(out - start);
}

int bmcu_cobs_decode(const uint8_t *in, size_t len, uint8_t *out)
{
    const uint8_t *end = in + len;
    uint8_t *start = out;

    while (in < end)
    {
        uint8_t code = *in++;
        if (code == 0) return -1;
        uint8_t copy = (uint8_t)(code - 1);
        if (in + copy > end) return -1;
        while (copy--) *out++ = *in++;
        if (code != 0xFF && in < end) *out++ = 0;
    }

    return (int)(out - start);
}
