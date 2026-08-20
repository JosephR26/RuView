/**
 * @file csi_diag.h
 * @brief Periodic measured-only diagnostics for CSI capture and heap.
 *
 * Fork-local module (see ESP32_CSI_PORT.md). It exists to answer, from the
 * device itself, the questions the original-ESP32 port could only estimate on
 * paper:
 *
 *   - Does the DSP pipeline plus WiFi/lwIP actually fit in the WROOM-32's
 *     ~320 KB of DRAM, and how much headroom is left after an hour?
 *   - What CSI frame rate does the radio really sustain under the MGMT-only
 *     promiscuous filter?
 *   - What frame lengths (128 / 256 / 384 bytes) does the link deliver, i.e.
 *     which LTF fields are actually present?
 *   - How often does the hardware set first_word_invalid on this silicon?
 *
 * Everything emitted here is a MEASUREMENT. There is no threshold, no
 * inference, no presence or motion value — those live in edge_processing and
 * are deliberately kept out of this packet so that a diagnostic capture can be
 * trusted as ground truth about the hardware rather than about the algorithm.
 *
 * Compiles to nothing when CONFIG_CSI_DIAG_ENABLE is unset, so S3 and C6
 * images are byte-identical to before this module existed.
 */

#pragma once

#include <stdint.h>
#include "esp_err.h"
#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Fork-local diagnostic packet magic.
 *
 * Deliberately outside upstream's 0xC51100xx series. That series already
 * contains a live collision — 0xC5110007 is claimed by both ADR-040 WASM
 * output (firmware) and ADR-095 temporal classification (the Rust host
 * parser) — because the firmware-side and host-side allocation lists are
 * maintained separately. Taking the "next free" slot would be betting that no
 * third list exists. 0xC5111xxx cannot collide with upstream and makes
 * fork-local packets self-identifying on the wire.
 */
#define CSI_DIAG_MAGIC 0xC5111001u

/** Wire size of csi_diag_pkt_t. */
#define CSI_DIAG_PKT_SIZE 60

/** Packet layout version, byte 5. Bump on any field change. */
#define CSI_DIAG_PKT_VERSION 1

/* ---- Bits in csi_diag_pkt_t.flags ---- */
#define CSI_DIAG_FLAG_WIFI_CONNECTED (1u << 0)
#define CSI_DIAG_FLAG_CSI_ACTIVE     (1u << 1)  /**< >=1 callback this window. */

/* ---- Bits in csi_diag_pkt_t.phy_flags ---- */
#define CSI_DIAG_PHY_BW40            (1u << 0)
#define CSI_DIAG_PHY_STBC            (1u << 1)

/**
 * Diagnostic packet, 60 bytes, little-endian, packed.
 *
 * Not the 48 bytes most upstream sibling packets use: the heap triple
 * (free / minimum-free / largest-block) is the whole point of this packet for
 * the port and would not fit. 60 bytes is the same size as the ADR-081
 * feature-state packet, so it is not an unusual shape for this wire. The magic
 * and the version byte make the layout unambiguous to a reader regardless.
 *
 * Cumulative counters are since boot; rate fields cover the last interval.
 */
typedef struct __attribute__((packed)) {
    uint32_t magic;            /**< CSI_DIAG_MAGIC. */
    uint8_t  node_id;          /**< csi_collector_get_node_id(). */
    uint8_t  version;          /**< CSI_DIAG_PKT_VERSION. */
    uint16_t interval_ms;      /**< Window length these rates cover. */

    uint32_t uptime_s;         /**< Seconds since boot. */
    uint32_t cb_total;         /**< CSI callbacks accepted, since boot. */
    uint32_t early_drop_total; /**< Callbacks dropped by the rate gate. */
    uint32_t send_ok_total;    /**< UDP CSI frames sent OK, since boot. */
    uint32_t send_fail_total;  /**< UDP CSI send failures, since boot. */

    uint16_t csi_rate_hz_x10;  /**< Accepted callback rate this window, Hz*10. */
    uint16_t last_len;         /**< Most recent info->len, bytes. */
    uint16_t last_subcarriers; /**< Most recent subcarrier count. */
    uint16_t fwi_total;        /**< first_word_invalid frames (saturating). */

    uint8_t  skipped_subcarriers; /**< Bins the DSP is excluding. */
    uint8_t  phy_flags;        /**< CSI_DIAG_PHY_*. */
    uint8_t  channel;          /**< Most recent rx_ctrl.channel. */
    uint8_t  sig_mode;         /**< Legacy sig_mode, or HE baseband format. */

    int8_t   rssi_mean;        /**< dBm, mean over the window. */
    int8_t   rssi_min;         /**< dBm. */
    int8_t   rssi_max;         /**< dBm. */
    int8_t   noise_floor_mean; /**< dBm, mean over the window. */

    uint8_t  flags;            /**< CSI_DIAG_FLAG_*. */
    uint8_t  reserved[3];      /**< Zero. Pads the byte group to a word. */

    uint32_t free_heap;        /**< Bytes free now. */
    uint32_t min_free_heap;    /**< Low-water mark since boot. */
    uint32_t largest_free_block; /**< Largest allocatable block (fragmentation). */
} csi_diag_pkt_t;

_Static_assert(sizeof(csi_diag_pkt_t) == CSI_DIAG_PKT_SIZE,
               "csi_diag packet must be exactly 60 bytes");

#if CONFIG_CSI_DIAG_ENABLE

/**
 * Start periodic diagnostics.
 *
 * Call after csi_collector_init() and stream_sender_init_with(). Creates one
 * esp_timer firing every CONFIG_CSI_DIAG_INTERVAL_MS. Safe to call twice; the
 * second call is a no-op.
 *
 * @return ESP_OK, or the esp_timer error that prevented startup.
 */
esp_err_t csi_diag_init(void);

/** Stop and free the diagnostic timer. */
void csi_diag_deinit(void);

/**
 * Fill @p out with the current snapshot without emitting it.
 * Exposed for tests and for on-demand queries.
 */
void csi_diag_sample(csi_diag_pkt_t *out);

#else  /* !CONFIG_CSI_DIAG_ENABLE */

static inline esp_err_t csi_diag_init(void) { return ESP_OK; }
static inline void csi_diag_deinit(void) { }
static inline void csi_diag_sample(csi_diag_pkt_t *out) { (void)out; }

#endif /* CONFIG_CSI_DIAG_ENABLE */

#ifdef __cplusplus
}
#endif
