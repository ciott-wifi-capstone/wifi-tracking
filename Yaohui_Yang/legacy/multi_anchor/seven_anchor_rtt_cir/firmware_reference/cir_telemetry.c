/* LLTF/20-MHz CSI exporter for ESP32-S3 and ESP-IDF 5.x (reference module).
 * CMakeLists.txt must compile this .c in the component which owns FTM code.
 * No FTM changes are made here: existing MFUSED lines must still be emitted.
 * Output: CSIRAW,1,round,esp_us,anchor,source_mac,channel,20,LLTF,first_invalid,base64
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <inttypes.h>
#include <string.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_wifi_types.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "cir_telemetry.h"

#define RAW_LEN 128
#define CSI_QUEUE_LEN 24
#define MAX_SAMPLES_PER_SLOT 12

typedef struct {
    uint32_t round_id;
    int64_t esp_us;
    uint8_t anchor;
    uint8_t mac[6];
    uint8_t channel;
    uint8_t first_invalid;
    uint8_t raw[RAW_LEN];
} raw_msg_t;

static QueueHandle_t s_queue;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_active;
static uint32_t s_round;
static uint8_t s_anchor;
static uint8_t s_bssid[6];
static uint32_t s_sample_count;
static volatile uint32_t s_task_busy;

void cir_begin_slot(uint32_t round_id, uint8_t anchor_id, const uint8_t bssid[6]) {
    portENTER_CRITICAL(&s_lock);
    s_round=round_id; s_anchor=anchor_id;
    memcpy(s_bssid,bssid,6); s_sample_count=0; s_active=true;
    portEXIT_CRITICAL(&s_lock);
}
void cir_end_slot(void) {
    portENTER_CRITICAL(&s_lock);
    s_active=false;
    portEXIT_CRITICAL(&s_lock);
}

static void csi_rx_cb(void *ctx, wifi_csi_info_t *info) {
    (void)ctx;
    if (!s_queue || !info || !info->buf || info->len != RAW_LEN || info->rx_ctrl.cwb != 0) return;
    raw_msg_t msg;
    bool accept=false;
    portENTER_CRITICAL(&s_lock);
    // Must match the CURRENT AP's actual source MAC. This is essential.
    if (s_active && s_sample_count < MAX_SAMPLES_PER_SLOT &&
        memcmp(info->mac,s_bssid,6)==0) {
        msg.round_id=s_round;
        msg.anchor=s_anchor;
        s_sample_count++;
        accept=true;
    }
    portEXIT_CRITICAL(&s_lock);
    if (!accept) return;
    msg.esp_us=esp_timer_get_time(); // same clock as MFUSED esp_us MUST be used
    memcpy(msg.mac,info->mac,6);
    msg.channel=info->rx_ctrl.channel;
    msg.first_invalid=info->first_word_invalid ? 1 : 0;
    memcpy(msg.raw,info->buf,RAW_LEN); // CSI callback buffer expires on return
    // Wi-Fi task must never block on serial output.
    (void)xQueueSend(s_queue,&msg,0); // count dropped packets in final production code
}

static void csi_writer_task(void *arg) {
    (void)arg;
    raw_msg_t msg;
    unsigned char out[256];
    size_t out_len;
    for (;;) {
        if (xQueueReceive(s_queue,&msg,portMAX_DELAY)!=pdTRUE) continue;
        s_task_busy=1;
        if (mbedtls_base64_encode(out,sizeof(out)-1,&out_len,msg.raw,RAW_LEN)==0) {
            out[out_len]=0;
            printf("CSIRAW,1,%" PRIu32 ",%" PRId64 ",%u,"
                   "%02x:%02x:%02x:%02x:%02x:%02x,%u,20,LLTF,%u,%s\n",
                   msg.round_id,msg.esp_us,msg.anchor,
                   msg.mac[0],msg.mac[1],msg.mac[2],msg.mac[3],msg.mac[4],msg.mac[5],
                   msg.channel,msg.first_invalid,(char *)out);
        }
        s_task_busy=0;
    }
}

void cir_wait_drain(void) {
    // Don't use inside the Wi-Fi callback or ISR. Drain before MROUND_META.
    for (int i=0;i<100;i++) {
        if (uxQueueMessagesWaiting(s_queue)==0 && !s_task_busy) {
            vTaskDelay(pdMS_TO_TICKS(10));
            if (uxQueueMessagesWaiting(s_queue)==0 && !s_task_busy) break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

esp_err_t cir_telemetry_init(void) {
    if (!s_queue) s_queue=xQueueCreate(CSI_QUEUE_LEN,sizeof(raw_msg_t));
    if (!s_queue) return ESP_ERR_NO_MEM;
    wifi_csi_config_t cfg = {
        .lltf_en=true, .htltf_en=false, .stbc_htltf2_en=false,
        .ltf_merge_en=false, .channel_filter_en=false,
        .manu_scale=false, .shift=0,
    };
    esp_err_t err=esp_wifi_set_csi_config(&cfg);
    if (err!=ESP_OK) return err;
    err=esp_wifi_set_csi_rx_cb(csi_rx_cb,NULL);
    if (err!=ESP_OK) return err;
    err=esp_wifi_set_csi(true);
    if (err!=ESP_OK) return err;
    xTaskCreate(csi_writer_task,"csi_uart",4096,NULL,4,NULL);
    return ESP_OK;
}
