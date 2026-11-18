#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"

static const char *TAG = "FUSED_ANCHOR";

#ifdef CONFIG_ANCHOR_BW40
#define ANCHOR_BANDWIDTH WIFI_BW40
#define ANCHOR_BANDWIDTH_MHZ 40
#else
#define ANCHOR_BANDWIDTH WIFI_BW20
#define ANCHOR_BANDWIDTH_MHZ 20
#endif

typedef struct __attribute__((packed)) {
    uint8_t frame_control[2];
    uint8_t duration[2];
    uint8_t destination[6];
    uint8_t source[6];
    uint8_t bssid[6];
    uint8_t sequence[2];
    uint8_t llc_snap[8];
    char magic[4];
    uint8_t version;
    uint8_t anchor_id;
    uint16_t reserved;
    uint32_t sequence_number;
    int64_t timestamp_us;
} probe_frame_t;

static uint8_t s_ap_mac[6];
static volatile uint32_t s_tx_done_count;
static volatile uint32_t s_tx_ok_count;
static volatile uint8_t s_last_tx_rate;
static esp_err_t s_probe_rate_config_result = ESP_FAIL;

static void probe_tx_done_cb(const esp_80211_tx_info_t *tx_info)
{
    if (tx_info == NULL) {
        return;
    }
    s_tx_done_count++;
    if (tx_info->tx_status == WIFI_SEND_SUCCESS) {
        s_tx_ok_count++;
    }
    s_last_tx_rate = (uint8_t)tx_info->rate;
}

static void probe_task(void *arg)
{
    probe_frame_t frame = {0};
    frame.frame_control[0] = 0x08; /* non-QoS data */
    frame.frame_control[1] = 0x02; /* From DS */
    memset(frame.destination, 0xff, sizeof(frame.destination));
    memcpy(frame.source, s_ap_mac, sizeof(frame.source));
    memcpy(frame.bssid, s_ap_mac, sizeof(frame.bssid));
    const uint8_t llc[8] = {0xaa, 0xaa, 0x03, 0x00, 0x00, 0x00, 0x88, 0xb5};
    memcpy(frame.llc_snap, llc, sizeof(llc));
    memcpy(frame.magic, "CSIP", 4);
    frame.version = 1;
    frame.anchor_id = CONFIG_ANCHOR_ID;

    /* Different startup phases reduce collisions between identical anchor boards. */
    vTaskDelay(pdMS_TO_TICKS(300 + CONFIG_ANCHOR_ID * 13));
    uint32_t sent = 0, failed = 0;
    while (true) {
        frame.sequence_number = sent + failed;
        frame.timestamp_us = esp_timer_get_time();
        esp_err_t err = esp_wifi_80211_tx(WIFI_IF_AP, &frame, sizeof(frame), true);
        if (err == ESP_OK) {
            sent++;
        } else {
            failed++;
        }
        if ((sent + failed) % 250 == 0) {
            ESP_LOGI(TAG,
                     "APROBE,id=%d,sent=%" PRIu32 ",failed=%" PRIu32
                     ",tx_done=%" PRIu32 ",tx_ok=%" PRIu32 ",phy_rate=0x%02x",
                     CONFIG_ANCHOR_ID, sent, failed,
                     s_tx_done_count, s_tx_ok_count, s_last_tx_rate);
        }
        vTaskDelay(pdMS_TO_TICKS(CONFIG_CSI_PROBE_PERIOD_MS));
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_register_80211_tx_cb(probe_tx_done_cb));

    char ssid[16];
    snprintf(ssid, sizeof(ssid), "FTM_A%d", CONFIG_ANCHOR_ID);
    wifi_config_t config = {0};
    strlcpy((char *)config.ap.ssid, ssid, sizeof(config.ap.ssid));
    config.ap.ssid_len = strlen(ssid);
    config.ap.channel = CONFIG_ANCHOR_CHANNEL;
    config.ap.max_connection = 1;
    config.ap.authmode = WIFI_AUTH_OPEN;
    config.ap.pmf_cfg.required = false;
    config.ap.ftm_responder = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &config));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_AP, ANCHOR_BANDWIDTH));
    /*
     * CSI requires an OFDM/HT long-training field. Broadcast frames otherwise
     * tend to use a legacy basic rate. Configure the injected probe as HT MCS0
     * after the AP interface exists but before Wi-Fi starts. A rejected rate
     * configuration is reported and allowed to fall back instead of rebooting.
     */
    s_probe_rate_config_result = esp_wifi_config_80211_tx_rate(
        WIFI_IF_AP, WIFI_PHY_RATE_MCS0_LGI);
    if (s_probe_rate_config_result != ESP_OK) {
        ESP_LOGW(TAG, "APROBE_RATE_CONFIG_FAILED,err=%s",
                 esp_err_to_name(s_probe_rate_config_result));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3
    ESP_ERROR_CHECK(esp_wifi_ftm_resp_set_offset(CONFIG_FTM_RESP_OFFSET_CM));
#endif
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_AP, s_ap_mac));

    ESP_LOGI(TAG,
             "AREADY,id=%d,ssid=%s,channel=%d,bw=%d,offset_cm=%d,probe_ms=%d,"
             "probe_phy=HT20-MCS0-LGI,rate_cfg=%s,mac=" MACSTR,
             CONFIG_ANCHOR_ID, ssid, CONFIG_ANCHOR_CHANNEL,
             ANCHOR_BANDWIDTH_MHZ, CONFIG_FTM_RESP_OFFSET_CM,
             CONFIG_CSI_PROBE_PERIOD_MS,
             esp_err_to_name(s_probe_rate_config_result), MAC2STR(s_ap_mac));
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    wifi_init();
    xTaskCreate(probe_task, "csi_probe", 4096, NULL, 4, NULL);
}
