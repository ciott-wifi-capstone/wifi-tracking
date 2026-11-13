/* Six-anchor FTM/CSI acquisition for ESP32-S3, ESP-IDF 6.0.2.
 * No association, DHCP or invented MAC addresses. One FTM session at a time.
 * UART0 TX/RX (board UART connector); host MARK tokens carry no coordinates.
 * Public CSI microsecond timestamps are not nanosecond-accurate ToF.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <inttypes.h>
#include <math.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_idf_version.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "nvs_flash.h"
#include "lab_config.h"

#define CSI_MAX_BYTES 512
#define CSI_QUEUE_DEPTH 64
#define FTM_DONE_BIT BIT1
#define FTM_FRAME_COUNT 32
#ifndef FTM_BURST_PERIOD
#define FTM_BURST_PERIOD 2
#endif
#define FTM_TIMEOUT_MS 5000
static const char *TAG = "MULTI_TAG";
static EventGroupHandle_t events;
static QueueHandle_t csi_queue, command_queue;
static SemaphoreHandle_t writer_drained;
static portMUX_TYPE capture_mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t ap_mac[6], ap_channel = LAB_CHANNEL;
static volatile uint32_t active_round, callbacks_inflight;
static volatile bool ftm_csi_window, awaiting_report;
static volatile uint32_t csi_received, csi_dropped;
static wifi_ftm_status_t ftm_status;
static uint32_t rtt_raw_ns, rtt_est_ns, dist_est_cm;
static uint8_t ftm_num_entries;
static int64_t rf_start_us, rf_report_us;
static uint32_t boot_id;

typedef struct {
    uint32_t round_id;
    bool barrier;
    int64_t time_us;
    uint64_t hw_rx_ns;
    uint8_t hw_rx_ns_precise;
    uint8_t mac[6];
    int rssi, noise, channel, secondary, sig_mode, cwb, invalid_first;
    uint16_t length;
    uint8_t frame_is_ftm, dialog_token, followup_token;
    uint16_t mac_seq_ctl, rx_seq;
    uint8_t data[CSI_MAX_BYTES];
} csi_rec_t;

static void event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    (void)arg;
    if (base != WIFI_EVENT || event_id != WIFI_EVENT_FTM_REPORT || !data) return;
    const wifi_event_ftm_report_t *r = data;
    portENTER_CRITICAL(&capture_mux);
    bool accept = awaiting_report && memcmp(r->peer_mac, ap_mac, 6) == 0;
    if (accept) {
        ftm_status = r->status;
        rtt_raw_ns = r->rtt_raw;
        rtt_est_ns = r->rtt_est;
        dist_est_cm = r->dist_est;
        ftm_num_entries = r->ftm_report_num_entries;
        rf_report_us = esp_timer_get_time();
        awaiting_report = false;
    }
    portEXIT_CRITICAL(&capture_mux);
    if (accept) xEventGroupSetBits(events, FTM_DONE_BIT);
}

/* Called from the Wi-Fi task; never print or run FFT inside this callback. */
static void csi_callback(void *context, wifi_csi_info_t *info)
{
    (void)context;
    if (!info || !info->buf || !csi_queue || !info->len || info->len > CSI_MAX_BYTES) return;
    /* Save only FTM responses, preserving the same CSI + dialog pairing format. */
    if (!info->hdr || !info->payload || info->payload_len < 4 ||
        (info->hdr[0] & 0xFC) != 0xD0 || info->payload[0] != 4 || info->payload[1] != 33) return;
    portENTER_CRITICAL(&capture_mux);
    bool accept = ftm_csi_window && active_round && memcmp(info->mac, ap_mac, 6) == 0;
    uint32_t rid = active_round;
    if (accept) callbacks_inflight++;
    portEXIT_CRITICAL(&capture_mux);
    if (!accept) return;
    csi_rec_t r = {0};
    r.round_id = rid;
    r.time_us = esp_timer_get_time();
#ifdef WIFI_PKT_RX_TIMESTAMP_NSEC
    /* OFDM rates are required for fine-grained RX start metadata. */
    if (info->rx_ctrl.sig_mode != 0 ||
        info->rx_ctrl.rate >= WIFI_PHY_RATE_48M) {
        r.hw_rx_ns = WIFI_PKT_RX_TIMESTAMP_NSEC(&info->rx_ctrl);
        r.hw_rx_ns_precise = 1;
    } else {
        r.hw_rx_ns = (uint64_t)info->rx_ctrl.timestamp * UINT64_C(1000);
        r.hw_rx_ns_precise = 0;
    }
#else
    /* Standard public API: RX timestamp is microseconds (NOT ns accurate). */
    r.hw_rx_ns = (uint64_t)info->rx_ctrl.timestamp * UINT64_C(1000);
    r.hw_rx_ns_precise = 0;
#endif
    memcpy(r.mac, info->mac, 6);
    r.rssi = info->rx_ctrl.rssi;
    r.noise = info->rx_ctrl.noise_floor;
    r.channel = info->rx_ctrl.channel;
    r.secondary = info->rx_ctrl.secondary_channel;
    r.sig_mode = info->rx_ctrl.sig_mode;
    r.cwb = info->rx_ctrl.cwb;
    r.invalid_first = info->first_word_invalid;
    r.length = info->len;
    r.rx_seq = info->rx_seq;
    /* FTM response is an 802.11 management action frame (Public 4, FTM 33).
     * Do not confuse FTM request action=32 or any UDP packet with this.
     */
    if (info->hdr && info->payload && info->payload_len >= 4 &&
        (info->hdr[0] & 0xFC) == 0xD0 &&
        info->payload[0] == 4 && info->payload[1] == 33) {
        r.frame_is_ftm = 1;
        r.dialog_token = info->payload[2];
        r.followup_token = info->payload[3];
        r.mac_seq_ctl = (uint16_t)info->hdr[22] | ((uint16_t)info->hdr[23] << 8);
    }
    memcpy(r.data, info->buf, info->len);
    if (xQueueSend(csi_queue, &r, 0) == pdPASS) csi_received++;
    else csi_dropped++;
    portENTER_CRITICAL(&capture_mux);
    callbacks_inflight--;
    portEXIT_CRITICAL(&capture_mux);
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
        if (r.barrier) { fflush(stdout); xSemaphoreGive(writer_drained); continue; }
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
        /* Emit self-contained frame identity + CSI. Existing CSI format preserved. */
        if (r.frame_is_ftm) {
            printf("CSI_FTM_FRAME,%" PRIu32 ",%" PRId64 ",%" PRIu64 ",%u," MACSTR ",%u,%u,%u,%u,%u,%d,%u,%s\n",
                   r.round_id, r.time_us, r.hw_rx_ns,
                   (unsigned)r.hw_rx_ns_precise, MAC2STR(r.mac),
                   (unsigned)r.dialog_token, (unsigned)r.followup_token,
                   (unsigned)r.mac_seq_ctl, (unsigned)r.rx_seq,
                   (unsigned)r.cwb, r.rssi, (unsigned)r.length,
                   strrchr(line, ',') ? strrchr(line, ',')+1 : "");
        }
        /* A matching metadata row carries the same round/time as CSI.
         * hw_rx_ns_precise=0 means coarse microsecond RX timestamp only.
         */
        printf("CSI_FTM_HW,%" PRIu32 ",%" PRId64 ",%" PRIu64 ",%u," MACSTR ",%d,%d,%u\n",
               r.round_id, r.time_us, r.hw_rx_ns,
               (unsigned)r.hw_rx_ns_precise, MAC2STR(r.mac),
               r.rssi, r.cwb, (unsigned)r.length);
    }
}

static void nvs_init(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); e = nvs_flash_init();
    }
    ESP_ERROR_CHECK(e);
}
static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_FTM_REPORT, event_handler, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_set_channel(LAB_CHANNEL, WIFI_SECOND_CHAN_NONE));
    /* Unassociated reception: CSI needs promiscuous receive enabled. */
    wifi_promiscuous_filter_t filter = {.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT};
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
}
static void enable_csi(void)
{
    wifi_csi_config_t cfg = {.lltf_en = true, .htltf_en = false,
        .stbc_htltf2_en = false, .ltf_merge_en = false, .channel_filter_en = false,
        .manu_scale = false, .shift = 0};
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
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
    // ESP-IDF v6.0 removes use_get_report_api: use esp_wifi_ftm_get_report().
#if ESP_IDF_VERSION_MAJOR < 6
    cfg.use_get_report_api = true;
#endif
    xEventGroupClearBits(events, FTM_DONE_BIT);
    portENTER_CRITICAL(&capture_mux);
    awaiting_report = true;
    rf_start_us = esp_timer_get_time();
    rf_report_us = rf_start_us;
    portEXIT_CRITICAL(&capture_mux);
    esp_err_t started = esp_wifi_ftm_initiate_session(&cfg);
    if (started != ESP_OK) {
        portENTER_CRITICAL(&capture_mux);
        awaiting_report = false;
        rf_report_us = esp_timer_get_time();
        portEXIT_CRITICAL(&capture_mux);
        ESP_LOGW(TAG, "start FTM failed: %s", esp_err_to_name(started));
        printf("FTM,%" PRIu32 ",%" PRId64 ",-1,0,0,0,0,0.0\n", rid, esp_timer_get_time());
        return;
    }
    EventBits_t bits = xEventGroupWaitBits(events, FTM_DONE_BIT, pdTRUE, pdFALSE,
                                           pdMS_TO_TICKS(FTM_TIMEOUT_MS));
    portENTER_CRITICAL(&capture_mux);
    awaiting_report = false;
    portEXIT_CRITICAL(&capture_mux);
    if (!(bits & FTM_DONE_BIT)) {
        rf_report_us = esp_timer_get_time();
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

typedef struct { uint8_t mac[6], channel; int rssi; unsigned count; } anchor_t;
static anchor_t anchors[6];
static void discover(void)
{
    memset(anchors, 0, sizeof(anchors));
    wifi_scan_config_t scan = {.channel = LAB_CHANNEL, .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE, .scan_time.active = {.min = 100, .max = 300}};
    esp_err_t err = esp_wifi_scan_start(&scan, true);
    if (err != ESP_OK) { printf("SCAN_ERROR,%s\n", esp_err_to_name(err)); return; }
    uint16_t count = 0;
    if (esp_wifi_scan_get_ap_num(&count) != ESP_OK || !count) {
        (void)esp_wifi_clear_ap_list(); return;
    }
    if (count > 128) count = 128;
    wifi_ap_record_t *records = calloc(count, sizeof(*records));
    if (!records) { (void)esp_wifi_clear_ap_list(); return; }
    err = esp_wifi_scan_get_ap_records(&count, records);
    if (err == ESP_OK) for (unsigned j = 0; j < count; j++) {
        for (unsigned i = 0; i < 6; i++) {
            char ssid[24]; snprintf(ssid, sizeof(ssid), "RTT_CIR_A%u", i+1);
            if (strcmp((const char *)records[j].ssid, ssid)) continue;
            anchors[i].count++;
            memcpy(anchors[i].mac, records[j].bssid, 6);
            anchors[i].channel = records[j].primary;
            anchors[i].rssi = records[j].rssi;
        }
    }
    free(records);
    if (err != ESP_OK) (void)esp_wifi_clear_ap_list();
    ESP_ERROR_CHECK(esp_wifi_set_channel(LAB_CHANNEL, WIFI_SECOND_CHAN_NONE));
    for (unsigned i = 0; i < 6; i++) printf(
        "DISCOVER,A%u,%u," MACSTR ",%u,%d\n", i+1, anchors[i].count,
        MAC2STR(anchors[i].mac), anchors[i].channel, anchors[i].rssi);
    fflush(stdout);
}

/* Serial commands are queued; only the acquisition task changes scan labels. */
typedef struct { bool cancel, rescan; char token[33]; unsigned scans; } command_t;
static bool token_ok(const char *s)
{
    size_t n = strlen(s);
    if (!n || n > 32) return false;
    for (size_t i = 0; i < n; i++) if (!isalnum((unsigned char)s[i]) && s[i] != '_' && s[i] != '-') return false;
    return true;
}
static void command_task(void *arg)
{
    (void)arg;
    char line[96]; size_t n = 0; bool overflow = false;
    for (;;) {
        int c = getchar();
        if (c == EOF) { clearerr(stdin); vTaskDelay(pdMS_TO_TICKS(10)); continue; }
        if (c == '\r') continue;
        if (c != '\n') {
            if (n + 1 < sizeof(line)) line[n++] = (char)c; else overflow = true;
            continue;
        }
        line[n] = 0; command_t cmd = {0}; bool valid = false;
        if (!overflow && strcmp(line, "RESCAN") == 0) { cmd.rescan = true; valid = true; }
        else if (!overflow && strncmp(line, "CANCEL,", 7) == 0 && token_ok(line+7)) {
            cmd.cancel = true; strcpy(cmd.token, line+7); valid = true;
        } else if (!overflow && strncmp(line, "MARK,", 5) == 0) {
            char *sep = strchr(line+5, ',');
            if (sep) {
                *sep = 0; char *end = NULL;
                unsigned long scans = strtoul(sep+1, &end, 10);
                if (token_ok(line+5) && end && !*end && scans >= 1 && scans <= 200) {
                    strcpy(cmd.token, line+5); cmd.scans = (unsigned)scans; valid = true;
                }
            }
        }
        if (!valid || xQueueSend(command_queue, &cmd, 0) != pdPASS) puts("COMMAND_ERROR,invalid_or_queue_full");
        n = 0; overflow = false;
    }
}
static void close_capture_and_drain(void)
{
    portENTER_CRITICAL(&capture_mux);
    ftm_csi_window = false; active_round = 0;
    portEXIT_CRITICAL(&capture_mux);
    for (;;) {
        portENTER_CRITICAL(&capture_mux);
        unsigned busy = callbacks_inflight;
        portEXIT_CRITICAL(&capture_mux);
        if (!busy) break;
        vTaskDelay(1);
    }
    /* No callback can enqueue after this FIFO barrier. */
    csi_rec_t barrier = {.barrier = true};
    if (xQueueSend(csi_queue, &barrier, pdMS_TO_TICKS(10000)) != pdPASS ||
        xSemaphoreTake(writer_drained, pdMS_TO_TICKS(10000)) != pdTRUE)
        ESP_ERROR_CHECK(ESP_ERR_TIMEOUT);
}
static void measurement_task(void *arg)
{
    (void)arg;
    uint32_t rid = 0, scan_id = 0;
    unsigned remaining = 0, collected = 0;
    char token[33] = "none";
    bool rescan = true;
    for (;;) {
        command_t cmd;
        while (xQueueReceive(command_queue, &cmd, 0) == pdTRUE) {
            if (cmd.rescan) { if (!remaining) rescan = true; }
            else if (cmd.cancel) {
                if (remaining && strcmp(cmd.token, token) == 0) {
                    printf("POINT_CANCELLED,%08" PRIx32 ",%s,%u\n", boot_id, token, collected);
                    remaining = 0; strcpy(token, "none");
                }
            } else if (remaining) printf("MARK_REJECTED,%s,BUSY\n", cmd.token);
            else {
                strcpy(token, cmd.token); remaining = cmd.scans; collected = 0;
                printf("MARK_ACK,%08" PRIx32 ",%s,%u\n", boot_id, token, remaining);
            }
        }
        bool missing = false;
        for (unsigned i = 0; i < 6; i++) if (anchors[i].count != 1) missing = true;
        if (!remaining && (rescan || scan_id % 20 == 0 || (missing && scan_id % 3 == 0))) {
            discover(); rescan = false;
        }
        ++scan_id;
        printf("SCAN_START,%08" PRIx32 ",%" PRIu32 ",%" PRId64 ",%s\n", boot_id, scan_id, esp_timer_get_time(), token);
        printf("PROFILE,%d,%d,%d,20\n", FTM_FRAME_COUNT, FTM_BURST_PERIOD, LAB_CHANNEL);
        for (unsigned i = 0; i < 6; i++) {
            ++rid;
            const anchor_t *a = &anchors[i];
            memcpy(ap_mac, a->mac, 6); ap_channel = a->channel ? a->channel : LAB_CHANNEL;
            printf("TARGET_READY,anchor_id=A%u,ssid=RTT_CIR_A%u,bssid=" MACSTR ",channel=%u\n", i+1, i+1, MAC2STR(ap_mac), ap_channel);
            printf("ROUND_START,%" PRIu32 ",%" PRId64 "\n", rid, esp_timer_get_time());
            printf("LINK_ID,%" PRIu32 ",%" PRId64 ",A%u," MACSTR ",%u\n", rid, esp_timer_get_time(), i+1, MAC2STR(ap_mac), ap_channel);
            const uint32_t before = csi_received, drops = csi_dropped;
            if (a->count != 1) {
                rf_start_us = rf_report_us = esp_timer_get_time();
                printf("LINK_UNAVAILABLE,%" PRIu32 ",A%u,%s\n", rid, i+1, a->count ? "DUPLICATE_SSID" : "NOT_FOUND");
                printf("FTM,%" PRIu32 ",%" PRId64 ",-3,0,0,0,0,0.0\n", rid, rf_start_us);
            } else {
                portENTER_CRITICAL(&capture_mux);
                active_round = rid; ftm_csi_window = true;
                portEXIT_CRITICAL(&capture_mux);
                do_ftm(rid);
                close_capture_and_drain();
            }
            printf("LINK_TIME,%" PRIu32 ",%" PRId64 ",%" PRId64 ",%" PRId64 "\n", rid, rf_start_us, rf_report_us, esp_timer_get_time());
            printf("PROBE_SUMMARY,%" PRIu32 ",0,0,%" PRIu32 ",%" PRIu32 "\n", rid, csi_received-before, csi_dropped-drops);
            printf("ROUND_END,%" PRIu32 ",%" PRId64 "\n", rid, esp_timer_get_time());
            fflush(stdout);
            vTaskDelay(pdMS_TO_TICKS(30));
        }
        printf("SCAN_END,%08" PRIx32 ",%" PRIu32 ",%" PRId64 ",%s\n", boot_id, scan_id, esp_timer_get_time(), token);
        if (remaining) {
            remaining--; collected++;
            if (!remaining) {
                printf("POINT_DONE,%08" PRIx32 ",%s,%u,%" PRId64 "\n", boot_id, token, collected, esp_timer_get_time());
                strcpy(token, "none");
            }
        }
        fflush(stdout);
        /* Avoid spinning if every Anchor is absent. */
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}
void app_main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    nvs_init(); boot_id = esp_random();
    events = xEventGroupCreate();
    csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_rec_t));
    command_queue = xQueueCreate(8, sizeof(command_t));
    writer_drained = xSemaphoreCreateBinary();
    configASSERT(events && csi_queue && command_queue && writer_drained);
    printf("MULTI_BOOT,%08" PRIx32 ",6,32,%d,UART0\n", boot_id, FTM_BURST_PERIOD);
    wifi_init(); enable_csi();
    configASSERT(xTaskCreate(csi_writer_task, "csi_writer", 4096, NULL, 5, NULL) == pdPASS);
    configASSERT(xTaskCreate(command_task, "commands", 4096, NULL, 3, NULL) == pdPASS);
    configASSERT(xTaskCreate(measurement_task, "multi_ftm", 12288, NULL, 4, NULL) == pdPASS);
}
