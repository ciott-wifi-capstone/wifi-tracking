/* ESP32-S3 Anchor (SoftAP + FTM responder + UDP echo)
 * Tested by static review against ESP-IDF 5.5 and 6.x APIs; hardware build still required.
 */
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_idf_version.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "lwip/sockets.h"

#define WIFI_SSID "RTT_CIR_LAB"
#define WIFI_PASS "CHANGE_ME_LAB"
#define WIFI_CHANNEL 6
#define UDP_PORT 3333
static const char *TAG = "ANCHOR";

static void udp_echo_server(void *arg)
{
    (void)arg;
    while (1) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) { ESP_LOGE(TAG, "socket errno=%d", errno); vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        struct sockaddr_in local = {0};
        local.sin_family = AF_INET;
        local.sin_port = htons(UDP_PORT);
        local.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
            ESP_LOGE(TAG, "bind errno=%d", errno); close(sock); vTaskDelay(pdMS_TO_TICKS(1000)); continue;
        }
        ESP_LOGI(TAG, "UDP echo listening on 192.168.4.1:%d", UDP_PORT);
        while (1) {
            char buf[128];
            struct sockaddr_in peer = {0};
            socklen_t plen = sizeof(peer);
            int n = recvfrom(sock, buf, sizeof(buf), 0, (struct sockaddr *)&peer, &plen);
            if (n < 0) { ESP_LOGW(TAG, "recvfrom errno=%d", errno); break; }
            if (sendto(sock, buf, n, 0, (struct sockaddr *)&peer, plen) < 0) {
                ESP_LOGW(TAG, "sendto errno=%d", errno);
            }
        }
        close(sock);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    wifi_config_t wifi_cfg = {0};
    memcpy(wifi_cfg.ap.ssid, WIFI_SSID, sizeof(WIFI_SSID));
    memcpy(wifi_cfg.ap.password, WIFI_PASS, sizeof(WIFI_PASS));
    wifi_cfg.ap.ssid_len = strlen(WIFI_SSID);
    wifi_cfg.ap.channel = WIFI_CHANNEL;
    wifi_cfg.ap.max_connection = 1;
    wifi_cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_cfg.ap.ftm_responder = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
#if ESP_IDF_VERSION_MAJOR >= 6
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW20));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW_HT20));
#endif
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_AP, mac));
    printf("ANCHOR_READY,ssid=%s,channel=%d,bssid=%02X:%02X:%02X:%02X:%02X:%02X,ip=192.168.4.1\n",
           WIFI_SSID, WIFI_CHANNEL, mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
    fflush(stdout);
    xTaskCreate(udp_echo_server, "udp_echo", 4096, NULL, 5, NULL);
}
