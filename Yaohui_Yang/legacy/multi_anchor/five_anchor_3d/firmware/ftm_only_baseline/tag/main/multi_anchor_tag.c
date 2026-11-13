#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_netif.h"

#define ANCHOR_PREFIX "FTM_A"
#define MEDIAN_WINDOW 5
#define MAX_RANGE_CM 30000

typedef struct {
    bool found;
    uint8_t id, bssid[6], channel;
    int8_t scan_rssi;
    float scale, bias_cm;
    uint32_t history[MEDIAN_WINDOW];
    uint8_t history_count, history_pos;
    uint32_t ok_count, fail_count;
} anchor_t;

static const char *TAG = "MULTI_TAG";
static anchor_t s_anchors[CONFIG_FTM_MAX_ANCHORS];
static SemaphoreHandle_t s_ftm_done;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static volatile int s_active_index = -1;
static uint32_t s_round_id;

static uint32_t median(anchor_t *a, uint32_t value)
{
    a->history[a->history_pos++] = value;
    a->history_pos %= MEDIAN_WINDOW;
    if (a->history_count < MEDIAN_WINDOW) a->history_count++;
    uint32_t copy[MEDIAN_WINDOW];
    memcpy(copy, a->history, a->history_count * sizeof(uint32_t));
    for (int i = 0; i < a->history_count; ++i)
        for (int j = i + 1; j < a->history_count; ++j)
            if (copy[j] < copy[i]) {
                uint32_t t = copy[i]; copy[i] = copy[j]; copy[j] = t;
            }
    return copy[a->history_count / 2];
}

static int anchor_id_from_ssid(const uint8_t *ssid)
{
    size_t n = strlen(ANCHOR_PREFIX);
    if (strncmp((const char *)ssid, ANCHOR_PREFIX, n) != 0) return -1;
    char *end = NULL;
    long id = strtol((const char *)ssid + n, &end, 10);
    return (end && *end == '\0' && id >= 1 && id <= CONFIG_FTM_MAX_ANCHORS)
               ? (int)id : -1;
}

static void scan_anchors(void)
{
    wifi_scan_config_t scan = { .show_hidden = true };
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    if (err != ESP_OK) { ESP_LOGW(TAG, "scan failed: %s", esp_err_to_name(err)); return; }
    uint16_t count = 0;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_num(&count));
    wifi_ap_record_t *records = calloc(count, sizeof(*records));
    if (!records) return;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&count, records));
    for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) s_anchors[i].found = false;
    for (int i = 0; i < count; ++i) {
        int id = anchor_id_from_ssid(records[i].ssid);
        if (id < 1) continue;
        anchor_t *a = &s_anchors[id - 1];
        if (!a->found || records[i].rssi > a->scan_rssi) {
            a->found = true; a->id = id;
            memcpy(a->bssid, records[i].bssid, 6);
            a->channel = records[i].primary; a->scan_rssi = records[i].rssi;
            if (a->scale == 0.0f) a->scale = 1.0f;
        }
    }
    free(records);
    printf("MSCAN,%" PRIu32, s_round_id);
    for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) {
        anchor_t *a = &s_anchors[i];
        if (a->found) printf(",A%d:" MACSTR ":ch%d:rssi%d",
                             a->id, MAC2STR(a->bssid), a->channel, a->scan_rssi);
    }
    printf("\n");
}

static void on_ftm_report(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base != WIFI_EVENT || id != WIFI_EVENT_FTM_REPORT) return;
    wifi_event_ftm_report_t *report = data;
    int index;
    portENTER_CRITICAL(&s_lock); index = s_active_index; portEXIT_CRITICAL(&s_lock);
    int8_t avg_rssi = 0; uint8_t valid = 0;
    if (report->ftm_report_num_entries) {
        wifi_ftm_report_entry_t *entries =
            calloc(report->ftm_report_num_entries, sizeof(*entries));
        if (entries && esp_wifi_ftm_get_report(
                entries, report->ftm_report_num_entries) == ESP_OK) {
            int sum = 0;
            for (int i = 0; i < report->ftm_report_num_entries; ++i)
                if (entries[i].rtt != UINT32_MAX) { sum += entries[i].rssi; valid++; }
            if (valid) avg_rssi = sum / valid;
        }
        free(entries);
    } else esp_wifi_ftm_get_report(NULL, 0);

    if (index >= 0 && index < CONFIG_FTM_MAX_ANCHORS) {
        anchor_t *a = &s_anchors[index];
        uint32_t raw_cm = report->rtt_raw > 0 ? report->rtt_raw * 15U : 0;
        float corrected = a->scale * raw_cm + a->bias_cm;
        uint32_t corrected_cm = corrected > 0 && corrected <= MAX_RANGE_CM
                                    ? (uint32_t)(corrected + 0.5f) : 0;
        uint32_t filtered_cm = corrected_cm ? median(a, corrected_cm) : 0;
        bool ok = report->status == FTM_STATUS_SUCCESS && corrected_cm > 0;
        if (ok) a->ok_count++; else a->fail_count++;
        printf("MRANGE,%" PRIu32 ",%" PRIi64 ",%d,%u,%" PRIu32 ",%" PRIu32
               ",%" PRIu32 ",%" PRIu32 ",%d,%u,%" PRIu32 ",%" PRIu32 "\n",
               s_round_id, esp_timer_get_time(), a->id, report->status,
               report->rtt_raw, report->dist_est, corrected_cm, filtered_cm,
               avg_rssi, valid, a->ok_count, a->fail_count);
    }
    xSemaphoreGive(s_ftm_done);
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(
        WIFI_EVENT, WIFI_EVENT_FTM_REPORT, on_ftm_report, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
}

static void ranging_task(void *arg)
{
    int64_t last_scan = 0;
    while (true) {
        s_round_id++;
        int64_t now = esp_timer_get_time();
        if (!last_scan || now - last_scan >= CONFIG_FTM_RESCAN_PERIOD_MS * 1000LL) {
            scan_anchors(); last_scan = esp_timer_get_time();
        }
        int measured = 0;
        for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) {
            anchor_t *a = &s_anchors[i];
            if (!a->found) continue;
            while (xSemaphoreTake(s_ftm_done, 0) == pdTRUE) {}
            portENTER_CRITICAL(&s_lock); s_active_index = i; portEXIT_CRITICAL(&s_lock);
            wifi_ftm_initiator_cfg_t req = {
                .channel = a->channel,
                .frm_count = CONFIG_FTM_FRAME_COUNT,
                .burst_period = CONFIG_FTM_BURST_PERIOD,
            };
            memcpy(req.resp_mac, a->bssid, 6);
            esp_err_t err = esp_wifi_ftm_initiate_session(&req);
            if (err == ESP_OK) {
                measured++;
                if (xSemaphoreTake(s_ftm_done,
                        pdMS_TO_TICKS(CONFIG_FTM_SESSION_TIMEOUT_MS)) != pdTRUE) {
                    a->fail_count++; esp_wifi_ftm_end_session();
                    printf("MTIMEOUT,%" PRIu32 ",%d,%" PRIu32 "\n",
                           s_round_id, a->id, a->fail_count);
                }
            } else {
                a->fail_count++;
                printf("MSTARTFAIL,%" PRIu32 ",%d,%s\n",
                       s_round_id, a->id, esp_err_to_name(err));
            }
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        portENTER_CRITICAL(&s_lock); s_active_index = -1; portEXIT_CRITICAL(&s_lock);
        printf("MROUND,%" PRIu32 ",%" PRIi64 ",%d\n",
               s_round_id, esp_timer_get_time(), measured);
        if (!measured) vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    s_ftm_done = xSemaphoreCreateBinary();
    ESP_ERROR_CHECK(s_ftm_done ? ESP_OK : ESP_ERR_NO_MEM);
    wifi_init();
    ESP_LOGI(TAG, "tag ready: anchors=%d frames=%d timeout=%dms",
             CONFIG_FTM_MAX_ANCHORS, CONFIG_FTM_FRAME_COUNT,
             CONFIG_FTM_SESSION_TIMEOUT_MS);
    xTaskCreate(ranging_task, "ranging", 6144, NULL, 5, NULL);
}
