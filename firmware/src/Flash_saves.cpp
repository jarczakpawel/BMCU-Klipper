#include "Flash_saves.h"
#include "ams.h"
#include "hal/irq_wch.h"
#include <string.h>

#include "ch32v20x_rcc.h"
#include "ch32v20x_crc.h"
#include "ch32v20x_flash.h"

static uint8_t g_nvm_fault = 0u;
static uint16_t g_bad_page_mask = 0u;

static uint32_t crc32_hw_words(const void* data, uint32_t bytes)
{
#ifdef BMCU_HOST_TEST
    const uint8_t* p = (const uint8_t*)data;
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0u; i < bytes; i++)
    {
        crc ^= p[i];
        for (uint8_t bit = 0u; bit < 8u; bit++)
            crc = (crc >> 1) ^ (0xEDB88320u & (uint32_t)-(int32_t)(crc & 1u));
    }
    return ~crc;
#else
    const uint32_t* p = (const uint32_t*)data;
    CRC->CTLR = 1u;
    for (uint32_t i = 0u; i < (bytes >> 2); i++) CRC->DATAR = p[i];
    return CRC->DATAR;
#endif
}

static constexpr uint32_t FLASH_ERASED_WORD = 0xE339E339u;

static inline bool flash_word_is_blank(uint32_t value)
{
    return value == FLASH_ERASED_WORD || value == 0xFFFFFFFFu;
}

static inline uint32_t flash_page_addr(uint32_t page)
{
    return FLASH_NVM_BASE_ADDR + page * FLASH_NVM256_PAGE_SIZE;
}

static inline uint32_t flash_page_index(uint32_t address)
{
    return (address - FLASH_NVM_BASE_ADDR) / FLASH_NVM256_PAGE_SIZE;
}

static void mark_bad_page(uint32_t page)
{
    if (page < FLASH_NVM_PAGE_COUNT)
        g_bad_page_mask |= (uint16_t)(1u << page);
}

static bool flash_range_is_erased(uint32_t base_addr, uint32_t bytes)
{
    const uint32_t* p = (const uint32_t*)base_addr;
    for (uint32_t i = 0u; i < (bytes >> 2); i++)
        if (!flash_word_is_blank(p[i])) return false;
    return true;
}

static bool flash256_prog(uint32_t page_addr, const uint32_t words[64])
{
    if (page_addr & (FLASH_NVM256_PAGE_SIZE - 1u)) return false;
    if (memcmp((const void*)page_addr, words, FLASH_NVM256_PAGE_SIZE) == 0)
        return true;

    const uint32_t irq = irq_save_wch();
    FLASH_Unlock_Fast();
    FLASH_ClearFlag(FLASH_FLAG_BSY | FLASH_FLAG_EOP | FLASH_FLAG_WRPRTERR);
    FLASH_ErasePage_Fast(page_addr);
    FLASH_ProgramPage_Fast(page_addr, (uint32_t*)words);
    FLASH_Lock_Fast();
    FLASH_Lock();
    irq_restore_wch(irq);

    return memcmp((const void*)page_addr, words, FLASH_NVM256_PAGE_SIZE) == 0;
}

static bool flash256_erase(uint32_t page_addr)
{
    if (page_addr & (FLASH_NVM256_PAGE_SIZE - 1u)) return false;

    const uint32_t irq = irq_save_wch();
    FLASH_Unlock_Fast();
    FLASH_ClearFlag(FLASH_FLAG_BSY | FLASH_FLAG_EOP | FLASH_FLAG_WRPRTERR);
    FLASH_ErasePage_Fast(page_addr);
    FLASH_Lock_Fast();
    FLASH_Lock();
    irq_restore_wch(irq);

    return flash_range_is_erased(page_addr, FLASH_NVM256_PAGE_SIZE);
}

static bool flash_word_prog_std(uint32_t addr, uint32_t data)
{
    if (addr & 3u) return false;
    const uint32_t current = *(const volatile uint32_t*)addr;
    if (current == data) return true;

    const uint32_t irq = irq_save_wch();
    FLASH_Unlock();
    FLASH_ClearFlag(FLASH_FLAG_BSY | FLASH_FLAG_EOP | FLASH_FLAG_WRPRTERR);
    const FLASH_Status status = FLASH_ProgramWord(addr, data);
    FLASH_Lock();
    irq_restore_wch(irq);

    return status == FLASH_COMPLETE && *(const volatile uint32_t*)addr == data;
}

static bool flash_prog_words(uint32_t base_addr, const uint32_t* words, uint32_t count)
{
    if (base_addr & 3u) return false;
    for (uint32_t i = 0u; i < count; i++)
        if (!flash_word_prog_std(base_addr + (i << 2), words[i])) return false;
    return true;
}

static constexpr uint32_t NVM_RECORD_BODY_BYTES = 240u;
static constexpr uint32_t NVM_RECORD_CRC_OFF = 240u;
static constexpr uint32_t NVM_RECORD_CRC_INV_OFF = 244u;
static constexpr uint32_t NVM_RECORD_COMMIT_OFF = 248u;
static constexpr uint32_t NVM_RECORD_COMMIT_INV_OFF = 252u;
static constexpr uint32_t NVM_RECORD_COMMIT = 0x324D5443u;
static constexpr uint32_t NVM_RECORD_FLAG_TOMBSTONE = 1u;

struct __attribute__((packed, aligned(4))) NVMRecordHeader
{
    uint32_t magic;
    uint16_t format;
    uint16_t length;
    uint32_t sequence;
    uint32_t metadata;
    uint32_t flags;
};

static_assert(sizeof(NVMRecordHeader) == 20u, "NVM record header layout changed");

struct NVMRecordLatest
{
    uint8_t found;
    uint8_t local_page;
    NVMRecordHeader header;
};

enum NVMRecordReadResult : uint8_t
{
    NVM_RECORD_NONE = 0u,
    NVM_RECORD_DATA = 1u,
    NVM_RECORD_TOMBSTONE = 2u
};

static inline bool seq32_newer(uint32_t candidate, uint32_t current)
{
    return (int32_t)(candidate - current) > 0;
}

static bool nvm_record_validate(uint32_t page_addr, uint32_t expected_magic, NVMRecordHeader* out)
{
    const uint8_t* bytes = (const uint8_t*)page_addr;
    const uint32_t commit = *(const volatile uint32_t*)(page_addr + NVM_RECORD_COMMIT_OFF);
    const uint32_t commit_inv = *(const volatile uint32_t*)(page_addr + NVM_RECORD_COMMIT_INV_OFF);
    if (commit != NVM_RECORD_COMMIT || commit_inv != ~NVM_RECORD_COMMIT) return false;

    const uint32_t stored_crc = *(const volatile uint32_t*)(page_addr + NVM_RECORD_CRC_OFF);
    const uint32_t stored_crc_inv = *(const volatile uint32_t*)(page_addr + NVM_RECORD_CRC_INV_OFF);
    if (stored_crc_inv != ~stored_crc) return false;
    if (crc32_hw_words(bytes, NVM_RECORD_BODY_BYTES) != stored_crc) return false;

    NVMRecordHeader header{};
    memcpy(&header, bytes, sizeof(header));
    const bool format_supported =
        header.format == NVM_FORMAT_VERSION ||
        header.format == (uint16_t)(NVM_FORMAT_VERSION + 1u);
    if (header.magic != expected_magic || !format_supported) return false;
    if (header.length > (uint16_t)(NVM_RECORD_BODY_BYTES - sizeof(NVMRecordHeader))) return false;
    if (out) *out = header;
    return true;
}

static NVMRecordLatest nvm_record_find_latest(uint32_t first_page, uint32_t page_count, uint32_t magic)
{
    NVMRecordLatest latest{};
    for (uint32_t local = 0u; local < page_count; local++)
    {
        NVMRecordHeader header{};
        if (!nvm_record_validate(flash_page_addr(first_page + local), magic, &header)) continue;
        if (!latest.found || seq32_newer(header.sequence, latest.header.sequence))
        {
            latest.found = 1u;
            latest.local_page = (uint8_t)local;
            latest.header = header;
        }
    }
    return latest;
}

static bool nvm_record_program_page(uint32_t page, uint32_t magic, uint32_t sequence,
                              uint32_t metadata, uint32_t flags,
                              const void* payload, uint16_t length)
{
    if (page >= FLASH_NVM_PAGE_COUNT) return false;
    if (length > (uint16_t)(NVM_RECORD_BODY_BYTES - sizeof(NVMRecordHeader))) return false;

    alignas(4) uint32_t words[64];
    memset(words, 0xFF, sizeof(words));
    uint8_t* bytes = (uint8_t*)words;

    NVMRecordHeader header{};
    header.magic = magic;
    header.format = NVM_FORMAT_VERSION;
    header.length = length;
    header.sequence = sequence;
    header.metadata = metadata;
    header.flags = flags;
    memcpy(bytes, &header, sizeof(header));
    if (length && payload) memcpy(bytes + sizeof(header), payload, length);

    const uint32_t crc = crc32_hw_words(bytes, NVM_RECORD_BODY_BYTES);
    memcpy(bytes + NVM_RECORD_CRC_OFF, &crc, sizeof(crc));
    const uint32_t crc_inv = ~crc;
    memcpy(bytes + NVM_RECORD_CRC_INV_OFF, &crc_inv, sizeof(crc_inv));

    const uint32_t address = flash_page_addr(page);
    if (!flash256_prog(address, words)) return false;
    if (memcmp((const void*)address, words, NVM_RECORD_COMMIT_OFF) != 0) return false;

    if (!flash_word_prog_std(address + NVM_RECORD_COMMIT_OFF, NVM_RECORD_COMMIT)) return false;
    if (!flash_word_prog_std(address + NVM_RECORD_COMMIT_INV_OFF, ~NVM_RECORD_COMMIT)) return false;

    NVMRecordHeader verified{};
    return nvm_record_validate(address, magic, &verified) &&
           verified.sequence == sequence && verified.length == length &&
           verified.metadata == metadata && verified.flags == flags;
}

static bool nvm_record_same(uint32_t first_page, const NVMRecordLatest& latest,
                             uint32_t metadata, uint32_t flags,
                             const void* payload, uint16_t length)
{
    if (!latest.found || latest.header.format != NVM_FORMAT_VERSION ||
        latest.header.metadata != metadata || latest.header.flags != flags ||
        latest.header.length != length)
        return false;
    if (!length) return true;
    const uint32_t address = flash_page_addr(first_page + latest.local_page);
    return memcmp((const void*)(address + sizeof(NVMRecordHeader)), payload, length) == 0;
}

static bool nvm_record_write_group(uint32_t first_page, uint32_t page_count, uint32_t magic,
                             uint32_t metadata, uint32_t flags,
                             const void* payload, uint16_t length,
                             uint32_t preferred_start, uint16_t protected_pages)
{
    NVMRecordLatest latest = nvm_record_find_latest(first_page, page_count, magic);
    if (nvm_record_same(first_page, latest, metadata, flags, payload, length)) return true;

    const uint32_t sequence = latest.found ? latest.header.sequence + 1u : 1u;
    uint32_t start = latest.found ? ((uint32_t)latest.local_page + 1u) % page_count
                                  : preferred_start % page_count;

    for (uint32_t attempt = 0u; attempt < page_count; attempt++)
    {
        const uint32_t local = (start + attempt) % page_count;
        const uint32_t page = first_page + local;
        if (latest.found && local == latest.local_page) continue;
        if (protected_pages & (uint16_t)(1u << page)) continue;
        if (g_bad_page_mask & (uint16_t)(1u << page)) continue;

        if (nvm_record_program_page(page, magic, sequence, metadata, flags, payload, length))
            return true;
        mark_bad_page(page);
    }

    g_nvm_fault = 1u;
    return false;
}

static NVMRecordReadResult nvm_record_read_group(uint32_t first_page, uint32_t page_count,
                                      uint32_t magic, void* out, uint16_t max_length,
                                      uint16_t* got_length, uint32_t* metadata)
{
    const NVMRecordLatest latest = nvm_record_find_latest(first_page, page_count, magic);
    if (!latest.found) return NVM_RECORD_NONE;
    if (metadata) *metadata = latest.header.metadata;
    if (got_length) *got_length = latest.header.length;
    if (latest.header.flags & NVM_RECORD_FLAG_TOMBSTONE) return NVM_RECORD_TOMBSTONE;
    if (latest.header.length > max_length || (latest.header.length && !out)) return NVM_RECORD_NONE;

    if (latest.header.length)
    {
        const uint32_t address = flash_page_addr(first_page + latest.local_page);
        memcpy(out, (const void*)(address + sizeof(NVMRecordHeader)), latest.header.length);
    }
    return NVM_RECORD_DATA;
}

static constexpr uint32_t STA_TAG = 0xA6u;
static constexpr uint32_t STA_SLOT_BYTES = 8u;
static constexpr uint32_t STA_SLOTS_PER_PAGE = FLASH_NVM256_PAGE_SIZE / STA_SLOT_BYTES;
static constexpr uint32_t STA_ACTIVE_SLOTS = FLASH_NVM_STATE_PAGE_COUNT * STA_SLOTS_PER_PAGE;

static uint16_t g_sta_seq = 0u;
static uint16_t g_sta_latest_slot = 0xFFFFu;
static uint8_t g_sta_have_saved = 0u;
static uint8_t g_sta_saved_active = 0u;
static uint8_t g_sta_saved_raw = 0xFFu;

static void flash_runtime_cache_clear(void)
{
    g_sta_seq = 0u;
    g_sta_latest_slot = 0xFFFFu;
    g_sta_have_saved = 0u;
    g_sta_saved_active = 0u;
    g_sta_saved_raw = 0xFFu;
}

static inline uint32_t sta_page_addr(uint32_t page_local)
{
    return flash_page_addr(FLASH_NVM_STATE_PAGE_FIRST + page_local);
}

static inline uint32_t sta_slot_addr(uint32_t slot)
{
    const uint32_t page_local = slot / STA_SLOTS_PER_PAGE;
    const uint32_t slot_local = slot % STA_SLOTS_PER_PAGE;
    return sta_page_addr(page_local) + slot_local * STA_SLOT_BYTES;
}

static bool sta_slot_blank(uint32_t slot)
{
    const uint32_t address = sta_slot_addr(slot);
    return flash_word_is_blank(*(const volatile uint32_t*)(address + 0u)) &&
           flash_word_is_blank(*(const volatile uint32_t*)(address + 4u));
}

static uint8_t state_encode(const uint8_t route_state[4])
{
    if (!route_state) return 0xFFu;
    uint8_t raw = 0u;
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        const uint8_t value = route_state[ch];
        if (value != AMS_ROUTE_EMPTY && value != AMS_ROUTE_LOADED &&
            value != AMS_ROUTE_UNCERTAIN)
            return 0xFFu;
        raw |= (uint8_t)(value << (ch * 2u));
    }
    return raw;
}

static bool state_decode(uint8_t raw, uint8_t route_state[4])
{
    if (!route_state) return false;
    for (uint8_t ch = 0u; ch < 4u; ch++)
    {
        const uint8_t value = (uint8_t)((raw >> (ch * 2u)) & 0x03u);
        if (value > AMS_ROUTE_UNCERTAIN) return false;
        route_state[ch] = value;
    }
    return true;
}

static bool sta_select_target(uint32_t* target_slot)
{
    if (!target_slot) return false;
    const uint32_t start = (g_sta_saved_active && g_sta_latest_slot < STA_ACTIVE_SLOTS)
                         ? ((uint32_t)g_sta_latest_slot + 1u) % STA_ACTIVE_SLOTS : 0u;

    for (uint32_t offset = 0u; offset < STA_ACTIVE_SLOTS; offset++)
    {
        const uint32_t slot = (start + offset) % STA_ACTIVE_SLOTS;
        const uint32_t page_local = slot / STA_SLOTS_PER_PAGE;
        const uint32_t page = FLASH_NVM_STATE_PAGE_FIRST + page_local;
        if (g_bad_page_mask & (uint16_t)(1u << page)) continue;
        if (sta_slot_blank(slot))
        {
            *target_slot = slot;
            return true;
        }
    }

    const uint32_t latest_page_local = (g_sta_saved_active && g_sta_latest_slot < STA_ACTIVE_SLOTS)
                                     ? g_sta_latest_slot / STA_SLOTS_PER_PAGE : 0xFFFFFFFFu;
    const uint32_t first_page = latest_page_local < FLASH_NVM_STATE_PAGE_COUNT
                              ? (latest_page_local + 1u) % FLASH_NVM_STATE_PAGE_COUNT : 0u;

    for (uint32_t offset = 0u; offset < FLASH_NVM_STATE_PAGE_COUNT; offset++)
    {
        const uint32_t page_local = (first_page + offset) % FLASH_NVM_STATE_PAGE_COUNT;
        const uint32_t page = FLASH_NVM_STATE_PAGE_FIRST + page_local;
        if (page_local == latest_page_local) continue;
        if (g_bad_page_mask & (uint16_t)(1u << page)) continue;
        if (!flash256_erase(sta_page_addr(page_local)))
        {
            mark_bad_page(page);
            continue;
        }
        *target_slot = page_local * STA_SLOTS_PER_PAGE;
        return true;
    }

    g_nvm_fault = 1u;
    return false;
}

void Flash_saves_init(void)
{
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_CRC, ENABLE);
    g_nvm_fault = 0u;
    g_bad_page_mask = 0u;
    flash_runtime_cache_clear();
}

bool Flash_saves_faulted(void)
{
    return g_nvm_fault != 0u;
}

uint16_t Flash_saves_bad_page_mask(void)
{
    return g_bad_page_mask;
}

bool Flash_NVM_read_raw(uint16_t offset, void* out, uint16_t bytes)
{
    if ((!out && bytes) || (uint32_t)offset + (uint32_t)bytes > FLASH_NVM_TOTAL_SIZE)
        return false;
    if (bytes) memcpy(out, (const void*)(FLASH_NVM_BASE_ADDR + (uint32_t)offset), bytes);
    return true;
}

bool Flash_NVM_full_clear(void)
{
    bool ok = true;
    for (uint32_t page = 0u; page < FLASH_NVM_PAGE_COUNT; page++)
    {
        if (!flash256_erase(flash_page_addr(page)))
        {
            mark_bad_page(page);
            ok = false;
        }
    }
    if (!flash_range_is_erased(FLASH_NVM_BASE_ADDR, FLASH_NVM_TOTAL_SIZE))
    {
        g_nvm_fault = 1u;
        ok = false;
    }
    if (!ok) g_nvm_fault = 1u;
    flash_runtime_cache_clear();
    return ok;
}

bool Flash_AMS_state_read(uint8_t route_state[4])
{
    if (!route_state) return false;

    uint8_t best_raw = 0u;
    uint8_t best_states[4] = {
        AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY, AMS_ROUTE_EMPTY
    };
    uint16_t best_seq = 0u;
    uint32_t best_slot = 0u;
    uint8_t have = 0u;

    for (uint32_t slot = 0u; slot < STA_ACTIVE_SLOTS; slot++)
    {
        const uint32_t address = sta_slot_addr(slot);
        const uint32_t word0 = *(const volatile uint32_t*)(address + 0u);
        const uint32_t word1 = *(const volatile uint32_t*)(address + 4u);
        if (flash_word_is_blank(word0) && flash_word_is_blank(word1)) continue;
        if ((word0 >> 24) != STA_TAG || (word0 ^ word1) != MAGIC_STA) continue;

        const uint16_t sequence = (uint16_t)((word0 >> 8) & 0xFFFFu);
        const uint8_t raw = (uint8_t)(word0 & 0xFFu);
        uint8_t decoded[4];
        if (!state_decode(raw, decoded)) continue;

        if (!have || (int16_t)(sequence - best_seq) > 0)
        {
            have = 1u;
            best_seq = sequence;
            best_raw = raw;
            memcpy(best_states, decoded, sizeof(best_states));
            best_slot = slot;
        }
    }

    if (have)
    {
        g_sta_seq = (uint16_t)(best_seq + 1u);
        g_sta_latest_slot = (uint16_t)best_slot;
        g_sta_have_saved = 1u;
        g_sta_saved_active = 1u;
        g_sta_saved_raw = best_raw;
    }
    else
    {

        flash_runtime_cache_clear();
    }

    memcpy(route_state, best_states, sizeof(best_states));
    return true;
}

bool Flash_AMS_state_write(const uint8_t route_state[4])
{
    const uint8_t raw = state_encode(route_state);
    if (raw == 0xFFu) return false;
    if (g_sta_have_saved && g_sta_saved_active && g_sta_saved_raw == raw)
        return true;

    for (uint32_t attempt = 0u; attempt < FLASH_NVM_STATE_PAGE_COUNT; attempt++)
    {
        uint32_t slot = 0u;
        if (!sta_select_target(&slot)) return false;
        const uint32_t page = FLASH_NVM_STATE_PAGE_FIRST + slot / STA_SLOTS_PER_PAGE;
        const uint32_t address = sta_slot_addr(slot);
        const uint16_t sequence = g_sta_seq;
        const uint32_t word0 = ((uint32_t)STA_TAG << 24) |
                               ((uint32_t)sequence << 8) | (uint32_t)raw;
        const uint32_t words[2] = { word0, word0 ^ MAGIC_STA };

        if (flash_prog_words(address, words, 2u) &&
            *(const volatile uint32_t*)(address + 0u) == words[0] &&
            *(const volatile uint32_t*)(address + 4u) == words[1])
        {
            g_sta_seq = (uint16_t)(sequence + 1u);
            g_sta_latest_slot = (uint16_t)slot;
            g_sta_have_saved = 1u;
            g_sta_saved_active = 1u;
            g_sta_saved_raw = raw;
            return true;
        }
        mark_bad_page(page);
    }
    g_nvm_fault = 1u;
    return false;
}

struct alignas(4) FlashCalPayload
{
    float offsets[4];
    float minimums[4];
    float maximums[4];
};

bool Flash_MC_PULL_cal_write_all(const float offs[4], const float vmin[4],
                                 const float vmax[4], const int8_t pol[4],
                                 uint8_t valid_mask)
{
    if (!offs || !vmin || !vmax) return false;
    FlashCalPayload payload{};
    memcpy(payload.offsets, offs, sizeof(payload.offsets));
    memcpy(payload.minimums, vmin, sizeof(payload.minimums));
    memcpy(payload.maximums, vmax, sizeof(payload.maximums));

    uint32_t metadata = ((uint32_t)(valid_mask & 0x0Fu) << 8);
    for (uint8_t channel = 0u; channel < 4u; channel++)
        if (pol && pol[channel] < 0) metadata |= (1u << channel);

    return nvm_record_write_group(FLASH_NVM_CAL_PAGE_FIRST, FLASH_NVM_CAL_PAGE_COUNT,
                            MAGIC_CAL, metadata, 0u, &payload,
                            (uint16_t)sizeof(payload), 2u, 0u);
}

bool Flash_MC_PULL_cal_read(float offs[4], float vmin[4], float vmax[4],
                            int8_t pol[4], uint8_t* valid_mask)
{
    if (!offs || !vmin || !vmax) return false;
    FlashCalPayload payload{};
    uint16_t got = 0u;
    uint32_t metadata = 0u;
    const NVMRecordReadResult result = nvm_record_read_group(
        FLASH_NVM_CAL_PAGE_FIRST, FLASH_NVM_CAL_PAGE_COUNT, MAGIC_CAL,
        &payload, (uint16_t)sizeof(payload), &got, &metadata);
    if (result == NVM_RECORD_TOMBSTONE) return false;

    if (result != NVM_RECORD_DATA) return false;
    if (got != sizeof(payload)) return false;

    memcpy(offs, payload.offsets, sizeof(payload.offsets));
    memcpy(vmin, payload.minimums, sizeof(payload.minimums));
    memcpy(vmax, payload.maximums, sizeof(payload.maximums));
    if (pol)
        for (uint8_t channel = 0u; channel < 4u; channel++)
            pol[channel] = (metadata & (1u << channel)) ? -1 : 1;

    uint8_t mask = (uint8_t)((metadata >> 8) & 0x0Fu);
    if (mask == 0u)
    {
        bool sane = true;
        for (uint8_t channel = 0u; channel < 4u; channel++)
            sane = sane && ((vmax[channel] - vmin[channel]) >= 0.10f);
        if (sane) mask = 0x0Fu;
    }
    if (valid_mask) *valid_mask = mask;

    return true;
}

bool Flash_MC_PULL_cal_clear(void)
{
    return nvm_record_write_group(FLASH_NVM_CAL_PAGE_FIRST, FLASH_NVM_CAL_PAGE_COUNT,
                            MAGIC_CAL, 0u, NVM_RECORD_FLAG_TOMBSTONE,
                            nullptr, 0u, 2u, 0u);
}

bool Flash_Motion_write(const void* in, uint16_t bytes)
{
    if (!in || bytes == 0u) return false;
    return nvm_record_write_group(FLASH_NVM_MOTION_PAGE_FIRST, FLASH_NVM_MOTION_PAGE_COUNT,
                            MAGIC_MOT, 0u, 0u, in, bytes, 0u, 0u);
}

bool Flash_Motion_read(void* out, uint16_t bytes)
{
    if (!out || bytes == 0u) return false;
    uint16_t got = 0u;
    const NVMRecordReadResult result = nvm_record_read_group(
        FLASH_NVM_MOTION_PAGE_FIRST, FLASH_NVM_MOTION_PAGE_COUNT, MAGIC_MOT,
        out, bytes, &got, nullptr);
    if (result != NVM_RECORD_DATA) return false;
    return got != 0u && got <= bytes;
}

bool Flash_Motion_clear(void)
{
    return nvm_record_write_group(FLASH_NVM_MOTION_PAGE_FIRST, FLASH_NVM_MOTION_PAGE_COUNT,
                            MAGIC_MOT, 0u, NVM_RECORD_FLAG_TOMBSTONE,
                            nullptr, 0u, 0u, 0u);
}
