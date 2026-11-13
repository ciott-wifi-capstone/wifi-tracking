/* ESP32-S3 Target (STA + FTM initiator + raw LLTF CSI acquisition)
 * Single link experiment: capture AP->STA CSI before each FTM session.
 * This CSI is channel data for downlink traffic, NOT FTM frame-specific CSI.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"

#define WIFI_SSID "RTT_CIR_LAB"
#define WIFI_PASS "CHANGE_ME_LAB"
#define ANCHOR_IP "192.168.4.1"
#define UDP_PORT 3333
#define CSI_MAX_BYTES 256
#define CSI_QUEUE_ITEMS 48
#define PROBES_PER_ROUND 16
#define PROBE_INTERVAL_MS 70
#define FTM_TIMEOUT_MS 8000
#define ROUND_GAP_MS 1000
#define CONNECTED_BIT BIT0

static const char *TAG = "TARGET";
static EventGroupHandle_t s_events;
static QueueHandle_t s_csi_queue;
static SemaphoreHandle_t s_ftm_done;
static volatile bool s_capture_csi = false;
static volatile uint32_t s_current_round = 0;
static volatile uint32_t s_csi_dropped = 0;
static uint8_t s_anchor_bssid[6] = {0};
static uint8_t s_anchor_channel = 0;
static wifi_event_ftm_report_t s_ftm_result;

typedef struct {
    uint32_t round_id;
    int64_t t_us;
    uint8_t mac[6];
    int8_t rssi;
    int8_t noise_floor;
    uint8_t channel;
    uint8_t secondary_channel;
    uint8_t sig_mode;
    uint8_t cwb;
    uint16_t len;
    bool invalid_first_word;
    uint8_t raw[CSI_MAX_BYTES];
} csi_msg_t;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *event)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        wifi_event_sta_connected_t *e = (wifi_event_sta_connected_t *)event;
        memcpy(s_anchor_bssid, e->bssid, 6);
        s_anchor_channel = e->channel;
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_FTM_REPORT) {
        memcpy(&s_ftm_result, event, sizeof(s_ftm_result));
        xSemaphoreGive(s_ftm_done);
    }
}

/* Wi-Fi driver's callback: NEVER print or do FFT here. The buffer is transient. */
static void csi_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    if (!s_capture_csi || !info || !info->buf) return;
    if (memcmp(info->mac, s_anchor_bssid, 6) != 0) return;
    if (info->len == 0 || info->len > CSI_MAX_BYTES) return;
    /* Accept only 20 MHz frames: LLTF sequence represents 64 complex tones. */
    if (info->rx_ctrl.cwb != 0) return;
    csi_msg_t msg = {0};
    msg.round_id = s_current_round;
    msg.t_us = esp_timer_get_time();
    memcpy(msg.mac, info->mac, 6);
    msg.rssi = info->rx_ctrl.rssi;
    msg.noise_floor = info->rx_ctrl.noise_floor;
    msg.channel = info->rx_ctrl.channel;
    msg.secondary_channel = info->rx_ctrl.secondary_channel;
    msg.sig_mode = info->rx_ctrl.sig_mode;
    msg.cwb = info->rx_ctrl.cwb;
    msg.len = info->len;
    msg.invalid_first_word = info->first_word_invalid;
    memcpy(msg.raw, info->buf, info->len);
    if (xQueueSend(s_csi_queue, &msg, 0) != pdTRUE) s_csi_dropped++;
}

static void print_csi(const csi_msg_t *m)
{
    printf("CSI,%lu,%lld,%02X:%02X:%02X:%02X:%02X:%02X,%d,%d,%u,%u,%u,%u,%u,%u,",
           (unsigned long)m->round_id, (long long)m->t_us,
           m->mac[0],m->mac[1],m->mac[2],m->mac[3],m->mac[4],m->mac[5],
           m->rssi, m->noise_floor, m->channel, m->secondary_channel,
           m->sig_mode, m->cwb, (unsigned)m->len, m->invalid_first_word ? 1U : 0U);
    for (int i = 0; i < m->len; ++i) printf("%02X", m->raw[i]);
    putchar('\n');
}

static void init_csi(void)
{
    wifi_csi_config_t cfg = {0};
    cfg.lltf_en = true;
    cfg.htltf_en = false;
    cfg.stbc_htltf2_en = false;
    cfg.ltf_merge_en = false;
    cfg.channel_filter_en = false;
    cfg.manu_scale = false;
    cfg.shift = 0;
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

static int create_probe_socket(void)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) return -1;
    struct sockaddr_in peer = {0};
    peer.sin_family = AF_INET;
    peer.sin_port = htons(UDP_PORT);
    peer.sin_addr.s_addr = inet_addr(ANCHOR_IP);
    if (connect(sock, (struct sockaddr *)&peer, sizeof(peer)) < 0) { close(sock); return -1; }
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 250000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    return sock;
}

static void run_round(uint32_t round_id, int sock)
{
    s_current_round = round_id;
    csi_msg_t msg;
    xQueueReset(s_csi_queue);
    s_csi_dropped = 0;
    int64_t begin = esp_timer_get_time();
    printf("ROUND_START,%lu,%lld\n", (unsigned long)round_id, (long long)begin);
    fflush(stdout);

    int udp_received = 0;
    s_capture_csi = true;
    for (int p = 0; p < PROBES_PER_ROUND; ++p) {
        if (!(xEventGroupGetBits(s_events) & CONNECTED_BIT)) break;
        char ping[40];
        int n = snprintf(ping, sizeof(ping), "SLAB,%lu,%d", (unsigned long)round_id, p);
        if (send(sock, ping, n, 0) >= 0) {
            char rx[96];
            int got = recv(sock, rx, sizeof(rx), 0);
            if (got == n && memcmp(rx, ping, n) == 0) udp_received++;
        }
        vTaskDelay(pdMS_TO_TICKS(PROBE_INTERVAL_MS));
    }
    s_capture_csi = false;
    vTaskDelay(pdMS_TO_TICKS(30)); // Allow last driver callback to finish.
    int csi_rows = 0;
    while (xQueueReceive(s_csi_queue, &msg, 0) == pdTRUE) {
        print_csi(&msg);
        csi_rows++;
    }
    printf("PROBE_SUMMARY,%lu,%d,%d,%d,%lu\n", (unsigned long)round_id,
           PROBES_PER_ROUND, udp_received, csi_rows, (unsigned long)s_csi_dropped);
    fflush(stdout);

    /* Run FTM AFTER the CSI probe window, on the same AP connection. */
    while (xSemaphoreTake(s_ftm_done, 0) == pdTRUE) { }
    wifi_ftm_initiator_cfg_t ftm = {0};
    memcpy(ftm.resp_mac, s_anchor_bssid, 6);
    ftm.channel = s_anchor_channel;
    ftm.frm_count = 16;
    ftm.burst_period = 2;
#if ESP_IDF_VERSION_MAJOR < 6
    ftm.use_get_report_api = true;
#endif
    int64_t ftm_start = esp_timer_get_time();
    esp_err_t ret = esp_wifi_ftm_initiate_session(&ftm);
    if (ret != ESP_OK) {
        printf("FTM,%lu,%lld,%d,0,0,0,0,nan\n", (unsigned long)round_id,
               (long long)ftm_start, -((int)ret));
    } else if (xSemaphoreTake(s_ftm_done, pdMS_TO_TICKS(FTM_TIMEOUT_MS)) != pdTRUE) {
        printf("FTM,%lu,%lld,-10000,0,0,0,0,nan\n", (unsigned long)round_id,
               (long long)esp_timer_get_time());
        esp_wifi_ftm_end_session();
    } else {
        wifi_event_ftm_report_t r = s_ftm_result;
        int n = r.ftm_report_num_entries;
        float rssi_average = 0.0f;
        int valid = 0;
        if (n > 0 && n <= 64) {
            wifi_ftm_report_entry_t *items = calloc((size_t)n, sizeof(*items));
            if (items && esp_wifi_ftm_get_report(items, (uint8_t)n) == ESP_OK) {
                for (int i = 0; i < n; ++i) {
                    rssi_average += items[i].rssi;
                    valid++;
                    printf("FTM_ENTRY,%lu,%d,%lu,%d\n", (unsigned long)round_id, i,
                           (unsigned long)items[i].rtt, (int)items[i].rssi);
                }
            }
            free(items);
        }
        if (valid) rssi_average /= valid;
        printf("FTM,%lu,%lld,%d,%lu,%lu,%lu,%d,",
               (unsigned long)round_id, (long long)esp_timer_get_time(), (int)r.status,
               (unsigned long)r.rtt_raw, (unsigned long)r.rtt_est,
               (unsigned long)r.dist_est, n);
        if (valid) printf("%.2f\n", (double)rssi_average);
        else printf("nan\n");
    }
    printf("ROUND_END,%lu,%lld\n", (unsigned long)round_id,
           (long long)esp_timer_get_time());
    fflush(stdout);
}

void app_main(void)
{
    s_events = xEventGroupCreate();
    s_csi_queue = xQueueCreate(CSI_QUEUE_ITEMS, sizeof(csi_msg_t));
    s_ftm_done = xSemaphoreCreateBinary();
    configASSERT(s_events && s_csi_queue && s_ftm_done);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));
    wifi_config_t wifi_cfg = {0};
    memcpy(wifi_cfg.sta.ssid, WIFI_SSID, sizeof(WIFI_SSID));
    memcpy(wifi_cfg.sta.password, WIFI_PASS, sizeof(WIFI_PASS));
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_LOGI(TAG, "Waiting for Anchor SSID %s", WIFI_SSID);
    xEventGroupWaitBits(s_events, CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    printf("TARGET_READY,bssid=%02X:%02X:%02X:%02X:%02X:%02X,channel=%u\n",
           s_anchor_bssid[0],s_anchor_bssid[1],s_anchor_bssid[2],
           s_anchor_bssid[3],s_anchor_bssid[4],s_anchor_bssid[5],s_anchor_channel);
    init_csi();
    uint32_t round_id = 1;
    while (1) {
        xEventGroupWaitBits(s_events, CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
        int sock = create_probe_socket();
        if (sock < 0) { ESP_LOGW(TAG, "UDP socket unavailable"); vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        while (xEventGroupGetBits(s_events) & CONNECTED_BIT) {
            run_round(round_id++, sock);
            vTaskDelay(pdMS_TO_TICKS(ROUND_GAP_MS));
        }
        close(sock);
    }
}
