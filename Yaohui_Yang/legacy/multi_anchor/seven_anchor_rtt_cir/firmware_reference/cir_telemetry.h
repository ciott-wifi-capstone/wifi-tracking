#pragma once
#include <stdint.h>
#include "esp_err.h"

// Call after esp_wifi_init() and esp_wifi_start() from the existing Tag firmware.
esp_err_t cir_telemetry_init(void);
// Call around EACH Anchor's FTM window. Use the BSSID measured for the same Anchor.
void cir_begin_slot(uint32_t round_id, uint8_t anchor_id, const uint8_t bssid[6]);
void cir_end_slot(void);
// Call BEFORE printing MROUND_META so CSIRAW lines for this epoch are flushed.
void cir_wait_drain(void);
