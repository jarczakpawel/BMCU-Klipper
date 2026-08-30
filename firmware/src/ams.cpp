#include "ams.h"
_ams ams[ams_max_number];
uint8_t bus_now_ams_num=0;

void ams_init()
{
    for(uint8_t i=0;i<ams_max_number;i++)
    {
        ams[i].init();
    }
}
