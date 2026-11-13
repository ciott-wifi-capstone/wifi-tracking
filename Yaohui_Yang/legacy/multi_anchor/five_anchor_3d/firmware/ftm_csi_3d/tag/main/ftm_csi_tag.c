#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#define ANCHOR_PREFIX "FTM_A"
#define MEDIAN_WINDOW 5
#define MAX_RANGE_CM 30000
#define MFUSED_SCHEMA 1

typedef struct {
    uint32_t packets;
    uint32_t first_invalid;
    int64_t last_us;
    double rssi_sum, rssi_sq_sum;
    double power_sum, power_sq_sum;
    double roughness_sum;
} csi_stats_t;

typedef struct {
    uint32_t raw_data_frames;
    uint32_t raw_probe_frames;
    uint32_t csi_callbacks;
    uint32_t csi_valid_buffers;
    uint32_t csi_payloads;
    uint32_t csi_probe_frames;
    uint32_t csi_bad_ids;
    uint32_t csi_mac_misses;
    uint32_t matched[CONFIG_FTM_MAX_ANCHORS];
    int8_t last_raw_rssi;
    uint8_t last_raw_sig_mode;
    uint8_t last_raw_mcs;
    uint8_t last_csi_id;
    int8_t last_csi_rssi;
    uint8_t last_csi_sig_mode;
    uint8_t last_csi_mcs;
    uint16_t last_csi_len;
    uint16_t last_payload_len;
    uint8_t last_csi_mac[6];
} csi_diag_t;

typedef struct {
    bool found;
    uint8_t id, bssid[6], channel;
    int8_t scan_rssi;
    float scale, bias_cm;
    uint32_t history[MEDIAN_WINDOW];
    uint8_t history_count, history_pos;
    uint32_t ok_count, fail_count;
    csi_stats_t csi;
} anchor_t;

static const char *TAG = "FUSED_TAG";

#ifdef CONFIG_TAG_BW40
#define TAG_BANDWIDTH WIFI_BW40
#define TAG_BANDWIDTH_MHZ 40
#else
#define TAG_BANDWIDTH WIFI_BW20
#define TAG_BANDWIDTH_MHZ 20
#endif
static anchor_t s_anchors[CONFIG_FTM_MAX_ANCHORS];
static SemaphoreHandle_t s_ftm_done;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static volatile int s_active_index = -1;
static uint32_t s_round_id;
static csi_diag_t s_csi_diag;

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

static bool find_probe_magic(const uint8_t *data, size_t length, uint8_t *anchor_id)
{
    if (!data || length < 6) return false;
    size_t limit = length < 64 ? length : 64;
    for (size_t i = 0; i + 6 <= limit; ++i) {
        if (memcmp(data + i, "CSIP", 4) == 0 && data[i + 4] == 1) {
            *anchor_id = data[i + 5];
            return true;
        }
    }
    return false;
}

static bool is_probe(const wifi_csi_info_t *info, uint8_t *anchor_id)
{
    return find_probe_magic(info->payload, info->payload_len, anchor_id);
}

static void promiscuous_rx_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    if (!buf || type != WIFI_PKT_DATA) return;
    const wifi_promiscuous_pkt_t *packet = buf;
    uint8_t id = 0;
    bool probe = find_probe_magic(packet->payload, packet->rx_ctrl.sig_len, &id);

    portENTER_CRITICAL(&s_lock);
    s_csi_diag.raw_data_frames++;
    if (probe) s_csi_diag.raw_probe_frames++;
    s_csi_diag.last_raw_rssi = packet->rx_ctrl.rssi;
    s_csi_diag.last_raw_sig_mode = packet->rx_ctrl.sig_mode;
    s_csi_diag.last_raw_mcs = packet->rx_ctrl.mcs;
    portEXIT_CRITICAL(&s_lock);
}

static void csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info) return;

    portENTER_CRITICAL(&s_lock);
    s_csi_diag.csi_callbacks++;
    s_csi_diag.last_csi_rssi = info->rx_ctrl.rssi;
    s_csi_diag.last_csi_sig_mode = info->rx_ctrl.sig_mode;
    s_csi_diag.last_csi_mcs = info->rx_ctrl.mcs;
    s_csi_diag.last_csi_len = info->len;
    s_csi_diag.last_payload_len = info->payload_len;
    memcpy(s_csi_diag.last_csi_mac, info->mac, sizeof(s_csi_diag.last_csi_mac));
    if (info->buf && info->len >= 4) s_csi_diag.csi_valid_buffers++;
    if (info->payload && info->payload_len) s_csi_diag.csi_payloads++;
    portEXIT_CRITICAL(&s_lock);

    if (!info->buf || info->len < 4) return;
    uint8_t id = 0;
    if (!is_probe(info, &id)) return;

    portENTER_CRITICAL(&s_lock);
    s_csi_diag.csi_probe_frames++;
    s_csi_diag.last_csi_id = id;
    if (id < 1 || id > CONFIG_FTM_MAX_ANCHORS) s_csi_diag.csi_bad_ids++;
    portEXIT_CRITICAL(&s_lock);
    if (id < 1 || id > CONFIG_FTM_MAX_ANCHORS) return;

    int start = info->first_word_invalid ? 4 : 0;
    double power_sum = 0.0, diff_sum = 0.0;
    int pairs = 0, differences = 0;
    int previous_real = 0, previous_imag = 0;
    for (int i = start; i + 1 < info->len; i += 2) {
        int imag = info->buf[i];
        int real = info->buf[i + 1];
        power_sum += (double)(real * real + imag * imag);
        if (pairs > 0) {
            int dr = real - previous_real, di = imag - previous_imag;
            diff_sum += (double)(dr * dr + di * di);
            differences++;
        }
        previous_real = real;
        previous_imag = imag;
        pairs++;
    }
    if (!pairs) return;
    double mean_power = power_sum / pairs;
    double roughness = differences ? (diff_sum / differences) / (mean_power + 1.0) : 0.0;

    portENTER_CRITICAL(&s_lock);
    anchor_t *a = &s_anchors[id - 1];
    if (a->found && memcmp(a->bssid, info->mac, 6) == 0) {
        s_csi_diag.matched[id - 1]++;
        csi_stats_t *s = &a->csi;
        s->packets++;
        if (info->first_word_invalid) s->first_invalid++;
        s->last_us = esp_timer_get_time();
        s->rssi_sum += info->rx_ctrl.rssi;
        s->rssi_sq_sum += (double)info->rx_ctrl.rssi * info->rx_ctrl.rssi;
        s->power_sum += mean_power;
        s->power_sq_sum += mean_power * mean_power;
        s->roughness_sum += roughness;
    } else {
        s_csi_diag.csi_mac_misses++;
    }
    portEXIT_CRITICAL(&s_lock);
}

static void csi_diag_task(void *arg)
{
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        csi_diag_t d;
        portENTER_CRITICAL(&s_lock);
        d = s_csi_diag;
        portEXIT_CRITICAL(&s_lock);
        printf("MCSI_DIAG,raw=%" PRIu32 ",raw_probe=%" PRIu32
               ",callbacks=%" PRIu32 ",valid=%" PRIu32
               ",payload=%" PRIu32 ",probe=%" PRIu32
               ",bad_id=%" PRIu32 ",mac_miss=%" PRIu32
               ",match=%" PRIu32 ":%" PRIu32 ":%" PRIu32 ":%" PRIu32 ":%" PRIu32
               ",last_id=%u,last_rssi=%d,sig=%u,mcs=%u,csi_len=%u,payload_len=%u,mac=" MACSTR
               ",raw_rssi=%d,raw_sig=%u,raw_mcs=%u\n",
               d.raw_data_frames, d.raw_probe_frames,
               d.csi_callbacks, d.csi_valid_buffers, d.csi_payloads,
               d.csi_probe_frames, d.csi_bad_ids, d.csi_mac_misses,
               d.matched[0], d.matched[1], d.matched[2], d.matched[3], d.matched[4],
               d.last_csi_id, d.last_csi_rssi, d.last_csi_sig_mode,
               d.last_csi_mcs, d.last_csi_len, d.last_payload_len,
               MAC2STR(d.last_csi_mac), d.last_raw_rssi,
               d.last_raw_sig_mode, d.last_raw_mcs);
    }
}

static void scan_anchors(void)
{
    wifi_scan_config_t scan = {.show_hidden = true};
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan failed: %s", esp_err_to_name(err));
        return;
    }
    uint16_t count = 0;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_num(&count));
    wifi_ap_record_t *records = calloc(count, sizeof(*records));
    if (!records) return;
    ESP_ERROR_CHECK(esp_wifi_scan_get_ap_records(&count, records));

    portENTER_CRITICAL(&s_lock);
    for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) s_anchors[i].found = false;
    for (int i = 0; i < count; ++i) {
        int id = anchor_id_from_ssid(records[i].ssid);
        if (id < 1) continue;
        anchor_t *a = &s_anchors[id - 1];
        if (!a->found || records[i].rssi > a->scan_rssi) {
            a->found = true;
            a->id = id;
            memcpy(a->bssid, records[i].bssid, 6);
            a->channel = records[i].primary;
            a->scan_rssi = records[i].rssi;
            if (a->scale == 0.0f) a->scale = 1.0f;
            memset(&a->csi, 0, sizeof(a->csi));
        }
    }
    portEXIT_CRITICAL(&s_lock);
    free(records);

    printf("MSCAN,%" PRIu32, s_round_id);
    for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) {
        anchor_t *a = &s_anchors[i];
        if (a->found) printf(",A%d:" MACSTR ":ch%d:rssi%d",
                             a->id, MAC2STR(a->bssid), a->channel, a->scan_rssi);
    }
    printf("\n");
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_COMMON_CHANNEL, WIFI_SECOND_CHAN_NONE));
}

static void on_ftm_report(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base != WIFI_EVENT || id != WIFI_EVENT_FTM_REPORT) return;
    wifi_event_ftm_report_t *report = data;
    int index;
    portENTER_CRITICAL(&s_lock);
    index = s_active_index;
    portEXIT_CRITICAL(&s_lock);

    int8_t avg_rssi = 0;
    uint8_t valid = 0;
    if (report->ftm_report_num_entries) {
        wifi_ftm_report_entry_t *entries = calloc(report->ftm_report_num_entries, sizeof(*entries));
        if (entries && esp_wifi_ftm_get_report(entries, report->ftm_report_num_entries) == ESP_OK) {
            int sum = 0;
            for (int i = 0; i < report->ftm_report_num_entries; ++i) {
                if (entries[i].rtt != UINT32_MAX) {
                    sum += entries[i].rssi;
                    valid++;
                }
            }
            if (valid) avg_rssi = sum / valid;
        }
        free(entries);
    } else {
        esp_wifi_ftm_get_report(NULL, 0);
    }

    if (index >= 0 && index < CONFIG_FTM_MAX_ANCHORS) {
        anchor_t *a = &s_anchors[index];
        uint32_t raw_cm = report->rtt_raw > 0 ? report->rtt_raw * 15U : 0;
        float corrected = a->scale * raw_cm + a->bias_cm;
        uint32_t corrected_cm = corrected > 0 && corrected <= MAX_RANGE_CM
                                    ? (uint32_t)(corrected + 0.5f) : 0;
        uint32_t filtered_cm = corrected_cm ? median(a, corrected_cm) : 0;
        bool ok = report->status == FTM_STATUS_SUCCESS && corrected_cm > 0;
        if (ok) a->ok_count++; else a->fail_count++;

        csi_stats_t csi;
        portENTER_CRITICAL(&s_lock);
        csi = a->csi;
        memset(&a->csi, 0, sizeof(a->csi));
        portEXIT_CRITICAL(&s_lock);

        double n = csi.packets ? csi.packets : 1.0;
        double csi_rssi = csi.packets ? csi.rssi_sum / n : -127.0;
        double csi_rssi_var = csi.packets ? csi.rssi_sq_sum / n - csi_rssi * csi_rssi : 0.0;
        double csi_power = csi.packets ? csi.power_sum / n : 0.0;
        double csi_power_var = csi.packets ? csi.power_sq_sum / n - csi_power * csi_power : 0.0;
        double csi_roughness = csi.packets ? csi.roughness_sum / n : 0.0;
        int64_t age_ms = csi.last_us ? (esp_timer_get_time() - csi.last_us) / 1000 : -1;
        if (csi_rssi_var < 0) csi_rssi_var = 0;
        if (csi_power_var < 0) csi_power_var = 0;

        printf("MFUSED,%d,%" PRIu32 ",%" PRIi64 ",%d,%u,%" PRIu32 ",%" PRIu32
               ",%" PRIu32 ",%" PRIu32 ",%d,%u,%" PRIu32
               ",%.3f,%.3f,%.3f,%.3f,%.6f,%" PRIu32 ",%" PRIi64
               ",%" PRIu32 ",%" PRIu32 "," MACSTR ",%d\n",
               MFUSED_SCHEMA, s_round_id, esp_timer_get_time(), a->id, report->status,
               report->rtt_raw, report->dist_est, corrected_cm, filtered_cm,
               avg_rssi, valid, csi.packets, csi_rssi, sqrt(csi_rssi_var),
               csi_power, sqrt(csi_power_var), csi_roughness, csi.first_invalid,
               age_ms, a->ok_count, a->fail_count, MAC2STR(a->bssid), a->scan_rssi);
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
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_FTM_REPORT,
                                                on_ftm_report, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, TAG_BANDWIDTH));

    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA,
    };
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_rx_cb(promiscuous_rx_cb));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = false,
        .ltf_merge_en = true,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = 0,
        .dump_ack_en = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

static void ranging_task(void *arg)
{
    int64_t last_scan = 0;
    while (true) {
        s_round_id++;
        int64_t now = esp_timer_get_time();
        if (!last_scan || now - last_scan >= CONFIG_FTM_RESCAN_PERIOD_MS * 1000LL) {
            scan_anchors();
            last_scan = esp_timer_get_time();
        }
        int measured = 0;
        for (int i = 0; i < CONFIG_FTM_MAX_ANCHORS; ++i) {
            anchor_t *a = &s_anchors[i];
            if (!a->found) continue;
            while (xSemaphoreTake(s_ftm_done, 0) == pdTRUE) {}
            portENTER_CRITICAL(&s_lock);
            s_active_index = i;
            portEXIT_CRITICAL(&s_lock);
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
                    a->fail_count++;
                    esp_wifi_ftm_end_session();
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
        portENTER_CRITICAL(&s_lock);
        s_active_index = -1;
        portEXIT_CRITICAL(&s_lock);
        printf("MROUND,%" PRIu32 ",%" PRIi64 ",%d\n",
               s_round_id, esp_timer_get_time(), measured);
        if (!measured) vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    s_ftm_done = xSemaphoreCreateBinary();
    ESP_ERROR_CHECK(s_ftm_done ? ESP_OK : ESP_ERR_NO_MEM);
    wifi_init();
    ESP_LOGI(TAG, "TREADY,anchors=%d,frames=%d,channel=%d,bw=%d",
             CONFIG_FTM_MAX_ANCHORS, CONFIG_FTM_FRAME_COUNT, CONFIG_COMMON_CHANNEL,
             TAG_BANDWIDTH_MHZ);
    xTaskCreate(csi_diag_task, "csi_diag", 4096, NULL, 4, NULL);
    xTaskCreate(ranging_task, "ranging", 7168, NULL, 5, NULL);
}
