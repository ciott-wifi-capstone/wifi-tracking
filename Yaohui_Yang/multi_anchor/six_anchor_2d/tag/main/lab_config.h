#pragma once
// Keep the two lab_config.h files identical.
// 0 keeps the previous single-link SSID. flash_numbered.py selects 1..6.
#ifndef LAB_ANCHOR_NUMBER
#define LAB_ANCHOR_NUMBER 0
#endif
#if LAB_ANCHOR_NUMBER < 0 || LAB_ANCHOR_NUMBER > 6
#error "LAB_ANCHOR_NUMBER must be 0 (legacy) or 1..6"
#endif
#define LAB_STRINGIFY_INNER(x) #x
#define LAB_STRINGIFY(x) LAB_STRINGIFY_INNER(x)
#if LAB_ANCHOR_NUMBER == 0
#define LAB_ANCHOR_ID     "LEGACY"
#define LAB_SSID          "RTT_CIR_LAB"
#else
#define LAB_ANCHOR_ID     "A" LAB_STRINGIFY(LAB_ANCHOR_NUMBER)
#define LAB_SSID          "RTT_CIR_A" LAB_STRINGIFY(LAB_ANCHOR_NUMBER)
#endif
#define LAB_PASSWORD      "CHANGE_ME_LAB"
#define LAB_CHANNEL       6
#define LAB_UDP_PORT      3333
// 0 = HT20 baseline; 1 = experimental HT40 (must verify actual received cwb).
#define LAB_USE_HT40      0
// Keep zero initially; do not apply an unverified offset.
#define LAB_FTM_OFFSET_CM 0
