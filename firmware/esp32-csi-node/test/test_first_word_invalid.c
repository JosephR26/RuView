/**
 * @file test_first_word_invalid.c
 * @brief Host-side unit tests for the ADR-018 first_word_invalid handling.
 *
 * ESP-IDF sets wifi_csi_info_t.first_word_invalid when the first four bytes
 * of the CSI payload are hardware-invalid — a documented limitation, called
 * out explicitly for the original ESP32. Before this change the flag was
 * dropped everywhere in the tree, so those two bins reached the DSP
 * indistinguishable from real channel data.
 *
 * Two behaviours are pinned here:
 *
 *   1. Wire encoding — byte 19 bit 5 reports the flag, and setting it must
 *      not disturb the ADR-110 bits that already live in that byte. This is
 *      what makes the format extension backward compatible.
 *   2. DSP exclusion — the excluded prefix must never be a top-K candidate,
 *      and the latch must be monotonic.
 *
 * The named constants come from ../main/csi_collector.h, so the test and the
 * firmware cannot disagree about which bit or how many bins. The two
 * predicates below are small reimplementations of firmware logic (same
 * approach as test_adr110_encoding.c) — if the firmware copy changes, this
 * test must be updated and re-run before that change merges.
 *
 * Build:
 *   cc -std=c99 -Wall -Wextra -Istubs -I../main -o test_fwi \
 *      test_first_word_invalid.c && ./test_fwi
 *
 * Exits 0 on all-pass; prints the failing assertion otherwise.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "csi_collector.h"   /* CSI_FLAG_*, CSI_FIRST_WORD_INVALID_BINS */

/* ──────────────────────────────────────────────────────────────────────
 *  System under test — mirrors firmware logic.
 * ────────────────────────────────────────────────────────────────────── */

/* From csi_collector.c: byte 19 is the ADR-110 flag set OR'd with the
 * first_word_invalid bit, which is computed outside the HE-tagging guard so
 * it is reported even in builds that leave PPDU tagging off. */
static uint8_t byte19(uint8_t adr110_flags, bool first_word_invalid)
{
    const uint8_t fwi_bit = first_word_invalid ? CSI_FLAG_FIRST_WORD_INVALID : 0u;
    return (uint8_t)(adr110_flags | fwi_bit);
}

/* From edge_processing.c process_frame(): the latch is monotonic — once a
 * bin has been seen invalid it stays excluded for the session. */
static uint16_t latch_skip(uint16_t current, bool first_word_invalid)
{
    if (first_word_invalid && current < CSI_FIRST_WORD_INVALID_BINS) {
        return CSI_FIRST_WORD_INVALID_BINS;
    }
    return current;
}

/* From edge_processing.c update_top_k(): candidates start at the skip
 * offset, so excluded bins are not merely deprioritised — they are never
 * examined. */
static bool is_topk_candidate(uint16_t sc, uint16_t skip, uint16_t n_subcarriers)
{
    return sc >= skip && sc < n_subcarriers;
}

/* ──────────────────────────────────────────────────────────────────────
 *  Test harness
 * ────────────────────────────────────────────────────────────────────── */

static int failures = 0;

#define CHECK(cond, msg)                                                     \
    do {                                                                     \
        if (!(cond)) {                                                       \
            printf("FAIL %s:%d — %s\n", __FILE__, __LINE__, (msg));           \
            failures++;                                                      \
        }                                                                    \
    } while (0)

static void test_flag_allocation(void)
{
    /* Bit 5. If this ever changes, every host parser must change with it. */
    CHECK(CSI_FLAG_FIRST_WORD_INVALID == 0x20u,
          "first_word_invalid must be byte-19 bit 5");

    /* Four bytes of invalid I/Q, two bytes per bin. */
    CHECK(CSI_FIRST_WORD_INVALID_BINS == 2,
          "four invalid bytes is exactly two subcarrier bins");

    /* Must not collide with the bits ADR-110 already defined. */
    CHECK((CSI_FLAG_FIRST_WORD_INVALID & CSI_FLAG_BW40) == 0,
          "collides with bw40");
    CHECK((CSI_FLAG_FIRST_WORD_INVALID & CSI_FLAG_STBC) == 0,
          "collides with STBC");
    CHECK((CSI_FLAG_FIRST_WORD_INVALID & CSI_FLAG_SYNC_VALID) == 0,
          "collides with sync-valid");
}

static void test_wire_encoding(void)
{
    /* Clean frame: bit 5 clear. A reader that predates the flag sees the
     * byte exactly as it saw it before — this is the compatibility claim. */
    CHECK(byte19(0u, false) == 0u,
          "no flags + valid first word must encode as 0");
    CHECK((byte19(0u, false) & CSI_FLAG_FIRST_WORD_INVALID) == 0,
          "bit 5 must be clear when the hardware did not flag the frame");

    /* Flagged frame. */
    CHECK((byte19(0u, true) & CSI_FLAG_FIRST_WORD_INVALID) != 0,
          "bit 5 must be set when the hardware flagged the frame");

    /* Coexistence: setting bit 5 must preserve every ADR-110 bit, and
     * clearing it must not disturb them either. */
    const uint8_t adr110 = CSI_FLAG_BW40 | CSI_FLAG_STBC | CSI_FLAG_SYNC_VALID;

    CHECK(byte19(adr110, false) == adr110,
          "valid first word must leave ADR-110 flags untouched");
    CHECK(byte19(adr110, true) == (uint8_t)(adr110 | CSI_FLAG_FIRST_WORD_INVALID),
          "bit 5 must be additive to the ADR-110 flags");

    /* The only difference between the two encodings is bit 5. */
    CHECK((uint8_t)(byte19(adr110, true) ^ byte19(adr110, false))
              == CSI_FLAG_FIRST_WORD_INVALID,
          "first_word_invalid must change exactly one bit");
}

static void test_latch_is_monotonic(void)
{
    uint16_t skip = 0;

    /* Clean frames leave the pipeline fully open. */
    skip = latch_skip(skip, false);
    CHECK(skip == 0, "no bins excluded before the first flagged frame");

    /* First flagged frame latches the exclusion. */
    skip = latch_skip(skip, true);
    CHECK(skip == CSI_FIRST_WORD_INVALID_BINS,
          "flagged frame must latch the exclusion");

    /* Subsequent clean frames must NOT reopen the bins. The flag is reported
     * per frame, but what it describes is a property of the capture path; if
     * the exclusion flapped, a bin could accumulate real variance on clean
     * frames and garbage on flagged ones and still win top-K. */
    skip = latch_skip(skip, false);
    CHECK(skip == CSI_FIRST_WORD_INVALID_BINS,
          "a clean frame must not un-exclude a previously invalid bin");

    /* Re-flagging is idempotent. */
    skip = latch_skip(skip, true);
    CHECK(skip == CSI_FIRST_WORD_INVALID_BINS, "latch must be idempotent");
}

static void test_topk_excludes_invalid_bins(void)
{
    const uint16_t n = 64;   /* HT20 on this hardware: 64 bins. */
    const uint16_t skip = CSI_FIRST_WORD_INVALID_BINS;

    for (uint16_t sc = 0; sc < skip; sc++) {
        CHECK(!is_topk_candidate(sc, skip, n),
              "hardware-invalid bin must never be a top-K candidate");
    }
    for (uint16_t sc = skip; sc < n; sc++) {
        CHECK(is_topk_candidate(sc, skip, n),
              "valid bin must remain a top-K candidate");
    }

    /* With no exclusion latched, every bin is a candidate — the unflagged
     * path must behave exactly as it did before this change. */
    for (uint16_t sc = 0; sc < n; sc++) {
        CHECK(is_topk_candidate(sc, 0, n),
              "unflagged capture must consider every bin");
    }

    /* Degenerate frame shorter than the excluded prefix: nothing to analyse,
     * and in particular no candidate that would index past the payload. */
    CHECK(!is_topk_candidate(0, skip, 1), "short frame must yield no candidate");
    CHECK(!is_topk_candidate(1, skip, 1), "short frame must yield no candidate");
}

int main(void)
{
    test_flag_allocation();
    test_wire_encoding();
    test_latch_is_monotonic();
    test_topk_excludes_invalid_bins();

    if (failures == 0) {
        printf("test_first_word_invalid: all assertions passed\n");
        return 0;
    }
    printf("test_first_word_invalid: %d assertion(s) failed\n", failures);
    return 1;
}
