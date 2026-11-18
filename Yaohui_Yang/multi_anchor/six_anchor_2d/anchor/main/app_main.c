/* QAR-FTM Anchor: standard ESP-IDF v6.0.2 FTM responder and UDP echo server.
 * This is an independent implementation; no Wi-PRO code is used.
 */
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_idf_version.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "lab_config.h"

static const char *TAG = "QAR_ANCHOR";

static void udp_echo_task(void *arg)
{
    (void)arg;
    for (;;) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
        if (sock < 0) {
            ESP_LOGE(TAG, "socket errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        struct sockaddr_in bind_addr = {0};
        bind_addr.sin_family = AF_INET;
        bind_addr.sin_addr.s_addr = htonl(INADDR_ANY);
        bind_addr.sin_port = htons(LAB_UDP_PORT);
        if (bind(sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) != 0) {
            ESP_LOGE(TAG, "bind errno=%d", errno);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        char buf[100];
        for (;;) {
            struct sockaddr_in peer = {0};
            socklen_t npeer = sizeof(peer);
            int n = recvfrom(sock, buf, sizeof(buf), 0,
                             (struct sockaddr *)&peer, &npeer);
            if (n < 0) { ESP_LOGW(TAG, "recvfrom errno=%d", errno); break; }
            if (sendto(sock, buf, (size_t)n, 0,
                       (struct sockaddr *)&peer, npeer) < 0) {
                ESP_LOGW(TAG, "sendto errno=%d", errno);
            }
        }
        close(sock);
    }
}

static void nvs_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

void app_main(void)
{
    nvs_init();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    wifi_config_t ap = {0};
    memcpy(ap.ap.ssid, LAB_SSID, strlen(LAB_SSID));
    memcpy(ap.ap.password, LAB_PASSWORD, strlen(LAB_PASSWORD));
    ap.ap.ssid_len = strlen(LAB_SSID);
    ap.ap.channel = LAB_CHANNEL;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.max_connection = 2;
    ap.ap.ftm_responder = true;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_protocol(WIFI_IF_AP,
                    WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N));
    esp_err_t bw_ret = esp_wifi_set_bandwidth(WIFI_IF_AP,
                           LAB_USE_HT40 ? WIFI_BW40 : WIFI_BW20);
    if (bw_ret != ESP_OK) ESP_LOGW(TAG, "set_bandwidth: %s", esp_err_to_name(bw_ret));
    ESP_ERROR_CHECK(esp_wifi_ftm_resp_set_offset(LAB_FTM_OFFSET_CM));

    uint8_t mac[6] = {0};
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_AP, mac));
    wifi_bandwidth_t bw = WIFI_BW20;
    (void)esp_wifi_get_bandwidth(WIFI_IF_AP, &bw);
    uint8_t ch = 0;
    wifi_second_chan_t sec = WIFI_SECOND_CHAN_NONE;
    (void)esp_wifi_get_channel(&ch, &sec);
    printf("ANCHOR_READY,anchor_id=%s,ssid=%s,channel=%u,bssid=" MACSTR ",ip=192.168.4.1\n",
           LAB_ANCHOR_ID, LAB_SSID, (unsigned)ch, MAC2STR(mac));
    printf("RADIO_ANCHOR,requested_ht40=%d,reported_bw_mhz=%d,primary=%u,secondary=%d,ftm_offset_cm=%d\n",
           LAB_USE_HT40, bw == WIFI_BW40 ? 40 : 20, (unsigned)ch,
           (int)sec, LAB_FTM_OFFSET_CM);
    fflush(stdout);
    if (xTaskCreate(udp_echo_task, "udp_echo", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "UDP echo task failed");
    }
}
