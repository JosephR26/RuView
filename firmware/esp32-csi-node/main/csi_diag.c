/**
 * @file csi_diag.c
 * @brief Periodic measured-only diagnostics for CSI capture and heap.
 *
 * See csi_diag.h for what this is for and why it carries no inferences.
 */

#include "csi_diag.h"

#if CONFIG_CSI_DIAG_ENABLE

#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_wifi.h"

#include "csi_collector.h"
#include "edge_processing.h"
#include "stream_sender.h"

static const char *TAG = "csi_diag";

static esp_timer_handle_t s_timer = NULL;

/* Previous cumulative counters, for differencing into rates. */
static uint32_t s_prev_cb    = 0;
static int64_t  s_prev_us    = 0;

/** Clamp a u32 counter into a u16 wire field without wrapping. */
static inline uint16_t sat_u16(uint32_t v)
{
    return (v > UINT16_MAX) ? UINT16_MAX : (uint16_t)v;
}

void csi_diag_sample(csi_diag_pkt_t *out)
{
    if (out == NULL) return;

    csi_stats_t st;
    csi_collector_get_stats(&st, true /* reset the RSSI/noise window */);

    const int64_t now_us = esp_timer_get_time();

    /* Rate over the actual elapsed time, not the nominal interval — the timer
     * can be late under load, and a rate computed against the nominal window
     * would quietly overstate yield exactly when the device is struggling. */
    uint16_t rate_x10 = 0;
    if (s_prev_us != 0 && now_us > s_prev_us) {
        const uint32_t d_cb   = st.cb_count - s_prev_cb;   /* u32 wrap is fine */
        const int64_t  d_us   = now_us - s_prev_us;
        const int64_t  scaled = ((int64_t)d_cb * 10 * 1000000) / d_us;
        rate_x10 = (scaled > UINT16_MAX) ? UINT16_MAX : (uint16_t)scaled;
    }
    s_prev_cb = st.cb_count;
    s_prev_us = now_us;

    memset(out, 0, sizeof(*out));
    out->magic            = CSI_DIAG_MAGIC;
    out->node_id          = csi_collector_get_node_id();
    out->version          = CSI_DIAG_PKT_VERSION;
    out->interval_ms      = (uint16_t)CONFIG_CSI_DIAG_INTERVAL_MS;

    out->uptime_s         = (uint32_t)(now_us / 1000000);
    out->cb_total         = st.cb_count;
    out->early_drop_total = st.early_drop;
    out->send_ok_total    = st.send_ok;
    out->send_fail_total  = st.send_fail;

    out->csi_rate_hz_x10  = rate_x10;
    out->last_len         = st.last_len;
    out->last_subcarriers = st.last_subcarriers;
    out->fwi_total        = sat_u16(st.fwi_count);

    out->skipped_subcarriers = (uint8_t)edge_get_skipped_subcarriers();
    out->phy_flags        = (uint8_t)((st.last_bw40 ? CSI_DIAG_PHY_BW40 : 0u) |
                                      (st.last_stbc ? CSI_DIAG_PHY_STBC : 0u));
    out->channel          = st.last_channel;
    out->sig_mode         = st.last_sig_mode;

    out->rssi_mean        = st.rssi_mean;
    out->rssi_min         = st.rssi_min;
    out->rssi_max         = st.rssi_max;
    out->noise_floor_mean = st.noise_floor_mean;

    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        out->flags |= CSI_DIAG_FLAG_WIFI_CONNECTED;
    }
    if (rate_x10 > 0) {
        out->flags |= CSI_DIAG_FLAG_CSI_ACTIVE;
    }

    out->free_heap          = (uint32_t)esp_get_free_heap_size();
    out->min_free_heap      = (uint32_t)esp_get_minimum_free_heap_size();
    out->largest_free_block =
        (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
}

static void csi_diag_timer_cb(void *arg)
{
    (void)arg;

    csi_diag_pkt_t pkt;
    csi_diag_sample(&pkt);

    /* Serial line: the bring-up view. Deliberately one line, fixed field
     * order, greppable — this is what gets pasted into a witness log. */
    ESP_LOGI(TAG,
             "up=%lus rate=%u.%uHz cb=%lu drop=%lu tx=%lu/%lu len=%u sc=%u "
             "skip=%u fwi=%u ch=%u sig=%u bw40=%u rssi=%d[%d..%d] nf=%d "
             "heap=%lu min=%lu blk=%lu",
             (unsigned long)pkt.uptime_s,
             (unsigned)(pkt.csi_rate_hz_x10 / 10),
             (unsigned)(pkt.csi_rate_hz_x10 % 10),
             (unsigned long)pkt.cb_total,
             (unsigned long)pkt.early_drop_total,
             (unsigned long)pkt.send_ok_total,
             (unsigned long)(pkt.send_ok_total + pkt.send_fail_total),
             (unsigned)pkt.last_len,
             (unsigned)pkt.last_subcarriers,
             (unsigned)pkt.skipped_subcarriers,
             (unsigned)pkt.fwi_total,
             (unsigned)pkt.channel,
             (unsigned)pkt.sig_mode,
             (unsigned)((pkt.phy_flags & CSI_DIAG_PHY_BW40) ? 1 : 0),
             (int)pkt.rssi_mean, (int)pkt.rssi_min, (int)pkt.rssi_max,
             (int)pkt.noise_floor_mean,
             (unsigned long)pkt.free_heap,
             (unsigned long)pkt.min_free_heap,
             (unsigned long)pkt.largest_free_block);

    /* UDP: the recording view. Uses the priority path so the diagnostic is
     * not starved by the CSI stream's global ENOMEM backoff (#1183) — a
     * diagnostic that disappears exactly when the device is under pressure
     * would be worse than no diagnostic at all. */
    (void)stream_sender_send_priority((const uint8_t *)&pkt, sizeof(pkt));
}

esp_err_t csi_diag_init(void)
{
    if (s_timer != NULL) {
        return ESP_OK;  /* Already running. */
    }

    const esp_timer_create_args_t args = {
        .callback = &csi_diag_timer_cb,
        .arg      = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name     = "csi_diag",
    };

    esp_err_t err = esp_timer_create(&args, &s_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_timer_create failed: %s", esp_err_to_name(err));
        s_timer = NULL;
        return err;
    }

    /* Prime the differencing state from the counters as they stand right now,
     * not from zero. csi_diag_init() runs after csi_collector_init(), so some
     * callbacks may already have landed; differencing against 0 would report a
     * huge first-window rate. Also resets the RSSI/noise accumulators so the
     * first window's means cover only that window. */
    {
        csi_stats_t st0;
        csi_collector_get_stats(&st0, true);
        s_prev_cb = st0.cb_count;
    }
    s_prev_us = esp_timer_get_time();

    err = esp_timer_start_periodic(s_timer,
                                   (uint64_t)CONFIG_CSI_DIAG_INTERVAL_MS * 1000ULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_timer_start_periodic failed: %s", esp_err_to_name(err));
        esp_timer_delete(s_timer);
        s_timer = NULL;
        return err;
    }

    ESP_LOGI(TAG, "diagnostics every %d ms -> serial + UDP magic 0x%08X (%d B)",
             (int)CONFIG_CSI_DIAG_INTERVAL_MS,
             (unsigned int)CSI_DIAG_MAGIC, (int)CSI_DIAG_PKT_SIZE);
    return ESP_OK;
}

void csi_diag_deinit(void)
{
    if (s_timer == NULL) return;
    esp_timer_stop(s_timer);
    esp_timer_delete(s_timer);
    s_timer = NULL;
}

#endif /* CONFIG_CSI_DIAG_ENABLE */
