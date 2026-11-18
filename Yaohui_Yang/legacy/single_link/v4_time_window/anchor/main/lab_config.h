#pragma once
// Keep the two lab_config.h files identical.
#define LAB_SSID          "RTT_CIR_LAB"
#define LAB_PASSWORD      "CHANGE_ME_LAB"
#define LAB_CHANNEL       6
#define LAB_UDP_PORT      3333
// 0 = HT20 baseline; 1 = experimental HT40 (must verify actual received cwb).
#define LAB_USE_HT40      0
// Keep zero initially; do not apply an unverified offset.
#define LAB_FTM_OFFSET_CM 0
