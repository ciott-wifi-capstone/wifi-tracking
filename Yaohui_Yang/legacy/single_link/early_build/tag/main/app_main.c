/* QAR-FTM Tag: FTM per-frame diagnostics + AP-origin CSI from UDP echoes.
 * ESP-IDF 5.5.x ESP32-S3. This is NOT paired FTM-frame CSI and does NOT
 * perform an uncalibrated Wi-PRO leading-edge / timestamp correction.
 * Existing serial CSV line types remain compatible with previous logger.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>
#include <errno.h>
#include <sys/time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "lab_config.h"

#define PROBES_PER_ROUND 16
#define FTM_FRAME_COUNT 32
#define FTM_BURST_PERIOD 2
#define FTM_TIMEOUT_MS 8000
#define CSI_MAX_BYTES 512
#define CSI_QUEUE_DEPTH 64
#define LINK_UP_BIT BIT0
#define FTM_DONE_BIT BIT1

static const char *TAG = "QAR_TAG";
static EventGroupHandle_t events;
static QueueHandle_t csi_queue;
static uint8_t ap_mac[6];
static uint8_t ap_channel = LAB_CHANNEL;
static volatile uint32_t active_round = 0;
static volatile uint32_t csi_received = 0, csi_dropped = 0;
static wifi_ftm_status_t ftm_status;
static uint32_t rtt_raw_ns, rtt_est_ns, dist_est_cm;
static uint8_t ftm_num_entries;

typedef struct {
    uint32_t round_id;
    int64_t time_us;
    uint8_t mac[6];
    int rssi, noise, channel, secondary, sig_mode, cwb, invalid_first;
    uint16_t length;
    uint8_t data[CSI_MAX_BYTES];
} csi_rec_t;

static void event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        (void)esp_wifi_connect();
    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(events, LINK_UP_BIT);
        (void)esp_wifi_connect();
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(events, LINK_UP_BIT);
    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_FTM_REPORT) {
        const wifi_event_ftm_report_t *r = (const wifi_event_ftm_report_t *)data;
        ftm_status = r->status;
        rtt_raw_ns = r->rtt_raw;
        rtt_est_ns = r->rtt_est;
        dist_est_cm = r->dist_est;
        ftm_num_entries = r->ftm_report_num_entries;
        xEventGroupSetBits(events, FTM_DONE_BIT);
    }
}

/* Called from the Wi-Fi task; never print or run FFT inside this callback. */
static void csi_callback(void *context, wifi_csi_info_t *info)
{
    (void)context;
    if (!info || !info->buf || !active_round || !csi_queue) return;
    if (memcmp(info->mac, ap_mac, 6) != 0) return;
    if (!info->len || info->len > CSI_MAX_BYTES) return;
    csi_rec_t r = {0};
    r.round_id = active_round;
    r.time_us = esp_timer_get_time();
    memcpy(r.mac, info->mac, 6);
    r.rssi = info->rx_ctrl.rssi;
    r.noise = info->rx_ctrl.noise_floor;
    r.channel = info->rx_ctrl.channel;
    r.secondary = info->rx_ctrl.secondary_channel;
    r.sig_mode = info->rx_ctrl.sig_mode;
    r.cwb = info->rx_ctrl.cwb;
    r.invalid_first = info->first_word_invalid;
    r.length = info->len;
    memcpy(r.data, info->buf, info->len);
    if (xQueueSend(csi_queue, &r, 0) == pdPASS) csi_received++;
    else csi_dropped++;
}

static void csi_writer_task(void *arg)
{
    (void)arg;
    csi_rec_t r;
    static const char hexdigits[] = "0123456789abcdef";
    // A single puts() per CSI record avoids mixing partial HEX strings with FTM lines.
    char line[1200];
    while (true) {
        if (xQueueReceive(csi_queue, &r, portMAX_DELAY) != pdTRUE) continue;
        int n = snprintf(line, sizeof(line),
                 "CSI,%" PRIu32 ",%" PRId64 "," MACSTR ",%d,%d,%d,%d,%d,%d,%u,%d,",
                 r.round_id, r.time_us, MAC2STR(r.mac), r.rssi, r.noise,
                 r.channel, r.secondary, r.sig_mode, r.cwb,
                 (unsigned)r.length, r.invalid_first);
        if (n <= 0 || (size_t)n + 2u * r.length + 2u > sizeof(line)) continue;
        for (unsigned i = 0; i < r.length; ++i) {
            line[n++] = hexdigits[r.data[i] >> 4];
            line[n++] = hexdigits[r.data[i] & 15];
        }
        line[n] = 0;
        puts(line);
    }
}

static void nvs_init(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        e = nvs_flash_init();
    }
    ESP_ERROR_CHECK(e);
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t conf = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&conf));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               event_handler, NULL));
    wifi_config_t sta = {0};
    memcpy(sta.sta.ssid, LAB_SSID, strlen(LAB_SSID));
    memcpy(sta.sta.password, LAB_PASSWORD, strlen(LAB_PASSWORD));
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    esp_err_t ret = esp_wifi_set_bandwidth(WIFI_IF_STA,
                              LAB_USE_HT40 ? WIFI_BW_HT40 : WIFI_BW_HT20);
    if (ret != ESP_OK) ESP_LOGW(TAG, "set_bandwidth %s", esp_err_to_name(ret));
}

static void enable_csi(void)
{
    // LLTF only: 20 MHz HT20 LLTF commonly reports 128 bytes.
    // No blind CSI-to-distance conversion; raw bytes are stored for offline analysis.
    wifi_csi_config_t cfg = {
        .lltf_en = true,
        .htltf_en = LAB_USE_HT40 ? true : false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = false,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = 0,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

static void log_radio(void)
{
    wifi_bandwidth_t bw = WIFI_BW_HT20;
    uint8_t primary = 0;
    wifi_second_chan_t secondary = WIFI_SECOND_CHAN_NONE;
    (void)esp_wifi_get_bandwidth(WIFI_IF_STA, &bw);
    (void)esp_wifi_get_channel(&primary, &secondary);
    printf("RADIO_TAG,requested_ht40=%d,reported_bw_mhz=%d,primary=%u,secondary=%d\n",
           LAB_USE_HT40, bw == WIFI_BW_HT40 ? 40 : 20,
           (unsigned)primary, (int)secondary);
}

static int probe_round(uint32_t rid)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) { ESP_LOGW(TAG, "probe socket errno=%d", errno); return 0; }
    struct timeval tv = {.tv_sec = 0, .tv_usec = 160000};
    (void)setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(LAB_UDP_PORT);
    addr.sin_addr.s_addr = inet_addr("192.168.4.1");
    int echoes = 0;
    for (int i = 0; i < PROBES_PER_ROUND; i++) {
        char tx[40];
        snprintf(tx, sizeof(tx), "QAR:%" PRIu32 ":%d", rid, i);
        if (sendto(sock, tx, strlen(tx), 0, (struct sockaddr *)&addr, sizeof(addr)) >= 0) {
            char rx[64];
            int n = recvfrom(sock, rx, sizeof(rx), 0, NULL, NULL);
            if (n == (int)strlen(tx) && memcmp(tx, rx, (size_t)n) == 0) echoes++;
        }
        vTaskDelay(pdMS_TO_TICKS(15));
    }
    close(sock);
    return echoes;
}

static int compare_double(const void *a, const void *b)
{
    const double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static void do_ftm(uint32_t rid)
{
    wifi_ftm_initiator_cfg_t cfg = {0};
    memcpy(cfg.resp_mac, ap_mac, 6);
    cfg.channel = ap_channel;
    cfg.frm_count = FTM_FRAME_COUNT;
    cfg.burst_period = FTM_BURST_PERIOD;
    cfg.use_get_report_api = true;
    xEventGroupClearBits(events, FTM_DONE_BIT);
    esp_err_t started = esp_wifi_ftm_initiate_session(&cfg);
    if (started != ESP_OK) {
        ESP_LOGW(TAG, "start FTM failed: %s", esp_err_to_name(started));
        printf("FTM,%" PRIu32 ",%" PRId64 ",-1,0,0,0,0,0.0\n", rid, esp_timer_get_time());
        return;
    }
    EventBits_t bits = xEventGroupWaitBits(events, FTM_DONE_BIT, pdTRUE, pdFALSE,
                                           pdMS_TO_TICKS(FTM_TIMEOUT_MS));
    if (!(bits & FTM_DONE_BIT)) {
        ESP_LOGW(TAG, "FTM timeout on round %" PRIu32, rid);
        (void)esp_wifi_ftm_end_session();
        printf("FTM,%" PRIu32 ",%" PRId64 ",-2,0,0,0,0,0.0\n", rid, esp_timer_get_time());
        return;
    }
    uint8_t n = ftm_num_entries;
    wifi_ftm_report_entry_t *entries = NULL;
    if (n) {
        entries = calloc(n, sizeof(*entries));
        if (entries && esp_wifi_ftm_get_report(entries, n) != ESP_OK) {
            free(entries);
            entries = NULL;
        }
        if (!entries) (void)esp_wifi_ftm_get_report(NULL, 0);
    } else {
        (void)esp_wifi_ftm_get_report(NULL, 0);
    }
    double rssi_sum = 0.0;
    unsigned rssi_count = 0;
    double dvals[64];
    unsigned nvalid = 0;
    for (unsigned i = 0; entries && i < n; i++) {
        const wifi_ftm_report_entry_t *e = &entries[i];
        printf("FTM_ENTRY,%" PRIu32 ",%u,%" PRIu32 ",%d\n",
               rid, i, e->rtt, (int)e->rssi);
        printf("FTM_DETAIL,%" PRIu32 ",%u,%u,%" PRIu32 ",%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
               rid, i, (unsigned)e->dlog_token, e->rtt, (int)e->rssi,
               e->t1, e->t2, e->t3, e->t4);
        if (e->rtt != UINT32_MAX) {
            rssi_sum += e->rssi;
            rssi_count++;
            if (nvalid < 64) dvals[nvalid++] = 0.000149896229 * (double)e->rtt;
        }
    }
    double mean_rssi = rssi_count ? rssi_sum / (double)rssi_count : 0.0;
    printf("FTM,%" PRIu32 ",%" PRId64 ",%d,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%u,%.2f\n",
           rid, esp_timer_get_time(), (int)ftm_status,
           rtt_raw_ns, rtt_est_ns, dist_est_cm,
           (unsigned)n, mean_rssi);
    // Diagnostic estimators only. They remove occasional RTT outliers, NOT
    // common multipath/hardware bias. Do not label these "accurate" distances.
    if (nvalid) {
        qsort(dvals, nvalid, sizeof(dvals[0]), compare_double);
        double median = dvals[nvalid / 2];
        if ((nvalid & 1u) == 0) median = (dvals[nvalid/2 - 1] + dvals[nvalid/2]) * 0.5;
        const double p25 = dvals[(nvalid - 1) / 4];
        double dev[64];
        for (unsigned i = 0; i < nvalid; ++i) dev[i] = fabs(dvals[i] - median);
        qsort(dev, nvalid, sizeof(dev[0]), compare_double);
        double mad = dev[nvalid / 2];
        if ((nvalid & 1u) == 0) mad = (dev[nvalid/2 - 1] + dev[nvalid/2]) * 0.5;
        printf("RANGE_DIAG,%" PRIu32 ",%.3f,%.3f,%.3f,%.3f,%u\n",
               rid, dist_est_cm / 100.0, p25, median, mad, nvalid);
    }
    free(entries);
}

static void measurement_task(void *arg)
{
    (void)arg;
    uint32_t rid = 0;
    while (true) {
        EventBits_t bits = xEventGroupWaitBits(events, LINK_UP_BIT, pdFALSE, pdTRUE,
                                               pdMS_TO_TICKS(2000));
        if (!(bits & LINK_UP_BIT)) { ESP_LOGW(TAG, "waiting for AP"); continue; }
        wifi_ap_record_t record = {0};
        if (esp_wifi_sta_get_ap_info(&record) == ESP_OK) {
            if (memcmp(record.bssid, ap_mac, 6) != 0) {
                memcpy(ap_mac, record.bssid, 6);
                ap_channel = record.primary;
                printf("TARGET_READY,bssid=" MACSTR ",channel=%u\n",
                       MAC2STR(ap_mac), (unsigned)ap_channel);
                log_radio();
            }
        }
        ++rid;
        printf("ROUND_START,%" PRIu32 ",%" PRId64 "\n", rid, esp_timer_get_time());
        const uint32_t before = csi_received;
        const uint32_t before_drop = csi_dropped;
        active_round = rid;
        int echoes = probe_round(rid);
        active_round = 0;
        // Allow the serial writer to drain without blocking the Wi-Fi task.
        vTaskDelay(pdMS_TO_TICKS(150));
        printf("PROBE_SUMMARY,%" PRIu32 ",%d,%d,%" PRIu32 ",%" PRIu32 "\n",
               rid, PROBES_PER_ROUND, echoes,
               csi_received - before, csi_dropped - before_drop);
        do_ftm(rid);
        printf("ROUND_END,%" PRIu32 ",%" PRId64 "\n", rid, esp_timer_get_time());
        fflush(stdout);
        vTaskDelay(pdMS_TO_TICKS(300));
    }
}

void app_main(void)
{
    nvs_init();
    events = xEventGroupCreate();
    // Allocate queue on heap; callback stack cannot hold large arrays.
    csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_rec_t));
    configASSERT(events && csi_queue);
    wifi_init();
    EventBits_t bits = xEventGroupWaitBits(events, LINK_UP_BIT, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(20000));
    if (!(bits & LINK_UP_BIT)) ESP_LOGW(TAG, "AP not yet connected; will retry");
    enable_csi();
    if (xTaskCreate(csi_writer_task, "csi_writer", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "CSI writer task failed");
    }
    if (xTaskCreate(measurement_task, "ftm_task", 8192, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "measurement task failed");
    }
}
