#include <stdio.h>
#include <string.h>
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"

static const char *TAG = "MULTI_ANCHOR";

static void init_ftm_anchor(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    char ssid[16];
    snprintf(ssid, sizeof(ssid), "FTM_A%d", CONFIG_FTM_ANCHOR_ID);
    wifi_config_t config = {0};
    strlcpy((char *)config.ap.ssid, ssid, sizeof(config.ap.ssid));
    config.ap.ssid_len = strlen(ssid);
    config.ap.channel = CONFIG_FTM_ANCHOR_CHANNEL;
    config.ap.max_connection = 1;
    config.ap.authmode = WIFI_AUTH_OPEN;
    config.ap.pmf_cfg.required = false;
    config.ap.ftm_responder = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &config));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(
        WIFI_IF_AP, CONFIG_FTM_ANCHOR_BW40 ? WIFI_BW40 : WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
#if CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3
    ESP_ERROR_CHECK(esp_wifi_ftm_resp_set_offset(CONFIG_FTM_RESP_OFFSET_CM));
#endif

    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_AP, mac));
    ESP_LOGI(TAG, "READY,id=%d,ssid=%s,channel=%d,bw=%d,offset_cm=%d,mac=" MACSTR,
             CONFIG_FTM_ANCHOR_ID, ssid, CONFIG_FTM_ANCHOR_CHANNEL,
             CONFIG_FTM_ANCHOR_BW40 ? 40 : 20, CONFIG_FTM_RESP_OFFSET_CM,
             MAC2STR(mac));
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    init_ftm_anchor();
}
