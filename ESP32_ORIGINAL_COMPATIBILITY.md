# RuView on the original ESP32 — compatibility assessment

**Status:** Port implemented, CI-green, and **validated on real silicon**.
Stage A gate PASSED on board `34:5f:45:aa:6f:8c` (ESP32-D0WD-V3 rev v3.1) —
see §8 for the measurements.
**Audited commit:** `a3b6e1d5` (fork of `ruvnet/RuView`, branch `main`)
**Target hardware:** ESP32 DevKitC-32 (ESP32-WROOM-32, ESP32-D0WD, 4 MB flash, no PSRAM)
**Secondary target:** ESP32-2432S028 "CYD" (ESP32-WROOM-32 + ILI9341 + XPT2046)
**Reference toolchain:** ESP-IDF v5.4 (the version upstream CI pins)

---

## 0. Bottom line

Porting RuView's **CSI acquisition** to the original ESP32 is feasible, and it is
feasible for a specific and slightly surprising reason: **the original ESP32 and the
ESP32-S3 share the same Wi-Fi CSI API surface.** RuView's `csi_collector.c` already
contains a `#else` branch — written for the S3 — that is byte-for-byte the correct
code path for the classic ESP32. The port is therefore mostly *build plumbing*, not
a rewrite of the sensing core.

What is **not** portable is a well-bounded set: the AMOLED display stack, the WASM
runtime, and the ESP32-C6 radio extensions. All three are already behind compile-time
guards, so excluding them costs nothing and breaks nothing upstream.

The honest caveats are in §6 (limitations). §8 now carries **measured** answers to
the questions the audit could not close on paper: the DSP fits with 137 KB of heap
to spare, the build links within IRAM, and the radio sustains **11.9 Hz** rather
than the S3's ~20 Hz. Three genuine defects that affect *every* target were found
along the way — see §7 — and one of them, `first_word_invalid`, turns out to fire
on **100 % of frames** on this hardware.

---

## 1. Firmware inventory

The repository contains three distinct firmware trees. Only the first is relevant.

| Tree | Purpose | Relevance |
|---|---|---|
| `firmware/esp32-csi-node/` | The real product. CSI capture → DSP → UDP stream. ~60 source files. | **This is the port target.** |
| `firmware/esp32-hello-world/` | 4-file smoke test used to validate the Docker/QEMU build path. | Useful as a toolchain sanity check. |
| `firmware/privshield/` | "Counter-sensing" / RF privacy work (nexmon patches, OpenWrt daemon, two ESP-IDF components). Separate concern. | Out of scope. |

### `firmware/esp32-csi-node/main/` — per-module portability

Classification key: **P** = portable as-is · **B** = needs a small fix · **X** = exclude on ESP32.

| Module | Size | Class | Notes |
|---|---:|:--:|---|
| `csi_collector.c/.h` | 33 KB | **P** | The core. Uses only `esp_wifi_set_csi_config/_rx_cb/_csi`, promiscuous mode, `wifi_csi_info_t`. Already dual-branched on `CONFIG_SOC_WIFI_HE_SUPPORT`; the non-HE branch is exactly right for ESP32. |
| `stream_sender.c/.h` | 6 KB | **P** | Plain BSD UDP sockets over lwIP. |
| `nvs_config.c/.h` | 12 KB | **P** | NVS only. |
| `edge_processing.c/.h` | 56 KB | **P** | Pure C float/double DSP. No FFT, no ESP-DSP, no chip intrinsics. See §4 for the compute budget. |
| `rv_radio_ops_esp32.c` | 6 KB | **P** | Thin `esp_wifi_*` wrapper (`set_channel`, `set_csi`, `sta_get_ap_info`). |
| `rv_feature_state.c`, `rv_mesh.c`, `adaptive_controller.c` | 26 KB | **P** | Target-agnostic logic; host unit tests already exist under `tests/host/`. |
| `rvf_parser.c` | 8 KB | **P** | Byte parsing. |
| `power_mgmt.c` | 3 KB | **P** | `esp_pm` / `esp_wifi_set_ps`. Available on ESP32. |
| `ota_update.c` | 11 KB | **P** | `esp_http_server` + `app_update`. Works on ESP32 given a two-OTA-slot partition table. |
| `c6_sync_espnow.c/.h` | 10 KB | **P** | ESP-NOW. Despite the `c6_` prefix its own header says it works on every ESP32 family. Confirmed: no C6-only API used. |
| `mock_csi.c` | 22 KB | **P** | QEMU/synthetic CSI. Only compiled when `CONFIG_CSI_MOCK_ENABLED`. |
| `mmwave_sensor.c` | 20 KB | **P** | UART probe, default pins TX=17/RX=18 (valid on ESP32). Times out harmlessly with no sensor attached. |
| `main.c` | 22 KB | **B** | One hard defect: the onboard-LED GPIO falls through to `48` for any non-C6 target. GPIO 48 does not exist on the original ESP32. See §3.1. |
| `swarm_bridge.c` | 12 KB | **B** | Compiles fine; comments assume PSRAM headroom. Needs a heap check, not a port. |
| `c6_twt.c`, `c6_timesync.c`, `c6_lp_core.c`, `c6_softap_he.c` | 25 KB | **X** | Already `#if defined(CONFIG_IDF_TARGET_ESP32C6)`-guarded end-to-end. Compile to nothing on ESP32. No action needed. |
| `wasm_runtime.c`, `wasm_upload.c` | 45 KB | **X** | Guarded by `CONFIG_WASM_ENABLE && WASM3_AVAILABLE`. Allocates `MALLOC_CAP_SPIRAM` arenas — impossible on a PSRAM-less WROOM-32. |
| `display_hal.c`, `display_ui.c`, `display_task.c`, `lv_conf.h` | 32 KB | **X** | Guarded by `CONFIG_DISPLAY_ENABLE`. Hard-coded for an **SH8601 368×448 QSPI AMOLED** on GPIO 4–7/11/12 with an FT3168 touch controller and TCA9554 expander. Nothing about this maps to the CYD. |

**Net result: nothing in the sensing path requires an S3 or a C6.**

---

## 2. Why the CSI path actually ports — the evidence

This is the load-bearing claim, so here is the primary source rather than an assertion.

### 2.1 The SoC capability flags

From `components/soc/<target>/include/soc/soc_caps.h` at ESP-IDF v5.4:

| Flag | esp32 | esp32s3 | esp32c6 |
|---|:--:|:--:|:--:|
| `SOC_WIFI_CSI_SUPPORT` | **1** | 1 | 1 |
| `SOC_WIFI_HE_SUPPORT` | — | — | 1 |
| `SOC_CPU_HAS_FPU` | **1** | 1 | **—** |
| `SOC_CPU_CORES_NUM` | **2** | 2 | 1 |

Two things worth pausing on:

1. The original ESP32 supports CSI natively. It is in fact the chip Espressif's
   original ESP32-CSI-Tool was written against.
2. **The original ESP32 is a closer relative of the S3 than the C6 is.** It has the
   same two Xtensa cores and the same hardware single-precision FPU. The C6 —
   which RuView already supports — is single-core with *no FPU at all*, so every
   float in `edge_processing.c` is software-emulated there. If the DSP pipeline
   is viable on a C6, it is comfortably viable on a DevKitC-32.

### 2.2 The struct that decides everything

From `components/esp_wifi/include/local/esp_wifi_types_native.h` at v5.4:

```c
#if CONFIG_SOC_WIFI_HE_SUPPORT
typedef wifi_csi_acquire_config_t wifi_csi_config_t;   /* C6 / C5 */
#else
typedef struct {
    bool lltf_en; bool htltf_en; bool stbc_htltf2_en; bool ltf_merge_en;
    bool channel_filter_en; bool manu_scale; uint8_t shift; bool dump_ack_en;
} wifi_csi_config_t;                                   /* esp32, esp32s2, esp32s3, esp32c3 … */
#endif
```

The selector is `CONFIG_SOC_WIFI_HE_SUPPORT`, **not** the specific chip. The original
ESP32 lands in the same branch as the S3. Now compare `csi_collector.c:571-579`:

```c
#else
    wifi_csi_config_t csi_config = {
        .lltf_en = true,  .htltf_en = true, .stbc_htltf2_en = true,
        .ltf_merge_en = true, .channel_filter_en = false,
        .manu_scale = false, .shift = false,
    };
#endif
```

That code already compiles and runs correctly on the original ESP32 today. Same for
the `rx_ctrl` metadata branch at `csi_collector.c:213-222`, which reads
`sig_mode` / `cwb` / `stbc` — all present in the non-HE `wifi_pkt_rx_ctrl_t`.

`noise_floor` (used at `csi_collector.c:177`) is present on the original ESP32 too;
it sits at a different bit offset than on the S3, but the field name is identical, so
the existing source is correct on both.

### 2.3 What the radio will actually give us

Per the ESP-IDF v5.4 Wi-Fi guide (§ *Wi-Fi Channel State Information*), CSI is
returned as pairs of `int8_t` — **imaginary first, then real** — for up to three
LTF fields (LLTF, HT-LTF, STBC-HT-LTF):

| Received packet | `len` (bytes) | Sub-carriers |
|---|---:|---|
| non-HT, 20 MHz, no secondary | 128 | LLTF `0..31, -32..-1` |
| HT, 20 MHz, no secondary | 256 | LLTF + HT-LTF |
| HT, 20 MHz, STBC | 384 | LLTF + HT-LTF + STBC-HT-LTF |
| HT, 40 MHz (secondary below/above) | 380–612 | wider index range |

Useful sub-carrier content is narrower than the raw count: 802.11n HT20 carries
**56 sub-carriers, 52 of them data**, spanning −28…+28. The remaining bins in the
128-byte LLTF block are guard/null and carry no information.

This is *identical* to what the S3 produces on the same link. RuView's field
measurements (`docs/WITNESS-LOG-110.md` §B1) recorded **148-byte / 64-sub-carrier
HT frames** against an 11n AP — that is the regime the DevKitC-32 will operate in
permanently.

---

## 3. What must change to build for `esp32`

Small, enumerable, and none of it touches shared logic.

### 3.1 Defect — LED GPIO falls through to a pin that does not exist

`main/main.c:236-240`:

```c
#if defined(CONFIG_IDF_TARGET_ESP32C6)
    const int led_gpio = 8;
#else
    const int led_gpio = 48;      /* <-- ESP32 classic has GPIO 0..39 only */
#endif
```

The original ESP32's GPIO range is 0–39 (34–39 input-only), and the DevKitC-32 has
no addressable WS2812 at all — just a plain LED on GPIO 2. `led_strip_new_rmt_device()`
will reject GPIO 48 and the return value *is* checked, so this is not a boot failure —
but it logs an error every boot and the intent is wrong. Needs a third branch.

### 3.2 No `esp32` sdkconfig overlay exists

`sdkconfig.defaults` hard-codes `CONFIG_IDF_TARGET="esp32s3"` and an 8 MB flash
layout. A new `sdkconfig.defaults.esp32` overlay is required (4 MB, display off,
WASM off, `partitions_4mb.csv`).

### 3.3 Naming trap — `sdkconfig.defaults.devkitc` is **not** for your board

This one is worth calling out explicitly because the filename is actively misleading
for this project. Reading its header:

> *"The stock **ESP32-S3-DevKitC-1** has no AMOLED panel, but the ADR-045 runtime
> probe false-positives on it…"*

It is an **ESP32-S3** overlay, and its documented build command runs
`idf.py set-target esp32s3`. It has nothing to do with the ESP32 DevKitC-32.
The new overlay must use a distinct name to avoid a very easy mis-flash.

### 3.4 Flash and partitions

DevKitC-32 / WROOM-32 is 4 MB. `partitions_4mb.csv` already exists and gives two
1.875 MB OTA slots — ample for a ~900 KB image. Reusable unchanged (its header
comment says "ESP32-S3" but the layout is target-neutral).

### 3.5 Build-system touch points

`main/CMakeLists.txt` needs no structural change — the C6 block is already
`if(IDF_TARGET STREQUAL "esp32c6")`. Only the source list for a lean ESP32 profile
is worth trimming, and that can be done additively.

`.github/workflows/firmware-ci.yml` currently builds a 3-way matrix
(`esp32s3/8mb`, `esp32s3/4mb`, `esp32c6/c6-4mb`). Adding an `esp32/4mb` row is a
purely additive change and gives us a build path that does not depend on my local
machine (see §9).

---

## 4. Compute and memory budget

### 4.1 CPU

`edge_processing.c` contains **no FFT and no ESP-DSP dependency** — it is
biquad IIR filtering, Welford running variance, and zero-crossing rate estimation.
Per CSI frame at the designed 20 Hz:

- `atan2f` × N sub-carriers (phase extraction) — hardware FPU on ESP32.
- `sqrtf` × N (amplitude) — hardware FPU.
- Welford update × up to 128 sub-carriers — **uses `double`** (`edge_welford_t`
  is `{double mean; double m2; uint32_t count;}`). The ESP32 LX6 FPU is
  *single*-precision, so these are software-emulated. Same on the S3; worse on
  the C6. Not a blocker at 20 Hz × 64 sub-carriers, but it is the single
  hottest avoidable cost in the pipeline and worth measuring.
- Two biquad passes over a 256-sample history for the vitals band.

At 240 MHz dual-core with the CSI callback already rate-gated to ~50 Hz
(`CSI_MIN_PROCESS_INTERVAL_US`) and UDP emission gated below that, this is not
close to saturating the chip. **CPU is not the constraint.**

### 4.2 RAM — this *is* the constraint

Original ESP32: 520 KB SRAM total, of which roughly **320 KB is usable DRAM**.
The S3 has 512 KB SRAM with a more favourable split, plus (on RuView's boards)
8 MB PSRAM. The DevKitC-32 has **no PSRAM at all**.

Static `.bss` in `edge_processing.c`, computed from the header:

| Buffer | Size |
|---|---:|
| SPSC ring (16 × 1032 B slots) | ~16.5 KB |
| `s_person_br_filt` + `s_person_hr_filt` (4 × 256 floats × 2) | 8.0 KB |
| `s_persons[4]` (each holds a 256-float history) | ~4.2 KB |
| `s_subcarrier_var[128]` (Welford, doubles) | ~3.0 KB |
| `s_scratch_br` + `s_scratch_hr` | 2.0 KB |
| `s_breathing_filtered` + `s_heartrate_filtered` | 2.0 KB |
| `s_phase_history[256]` | 1.0 KB |
| `s_prev_iq[1024]` | 1.0 KB |
| `s_prev_phase[128]` | 0.5 KB |
| **Total static DSP state** | **~38 KB** |

Against that, `sdkconfig.defaults` deliberately inflates the network pools to fix a
documented ENOMEM problem:

```
CONFIG_LWIP_UDP_RECVMBOX_SIZE=32          (stock: 6)
CONFIG_LWIP_TCPIP_RECVMBOX_SIZE=64        (stock: 32)
CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER_NUM=64  (stock: 32)
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH=8192
```

The comment in that file measures the cost at "~3 KB extra heap" on the S3 —
that figure is for the mbox bumps; the Wi-Fi TX buffer bump is the larger item and
scales with the driver's dynamic pool.

**Assessment:** ~38 KB DSP + Wi-Fi/lwIP stack + OTA HTTP server against ~320 KB
DRAM should fit with meaningful headroom, but the margin is materially thinner than
on the S3 and I have **not measured it**. This is the number to establish first on
real hardware.

### 4.3 IRAM — the underrated risk

The original ESP32 has a tighter IRAM budget than the S3, and
`sdkconfig.defaults` sets `CONFIG_ESP_WIFI_EXTRA_IRAM_OPT=y` (added as
defence-in-depth for an SPI-cache race, RuView#396). Combined with the Wi-Fi
blob, IRAM overflow at link time is a plausible first-build failure on `esp32`.
It is a config-level fix if it happens, not a design problem — but expect it.

### 4.4 Flash

Upstream reports ~943 KB for the full S3 image. Dropping display + WASM + LVGL
should land the ESP32 build meaningfully below that, well inside a 1.875 MB slot.

---

## 5. Recommended split — what runs where

| Layer | Where | Why |
|---|---|---|
| CSI capture, timestamping, framing | **DevKitC-32** | Must be at the radio. |
| Per-sub-carrier amplitude/phase, running variance | **DevKitC-32** | Cheap; hugely reduces bandwidth vs raw I/Q. |
| Motion/activity metric + presence confidence | **DevKitC-32** | Cheap, and keeps the node useful standalone. |
| Raw I/Q streaming | **DevKitC-32, opt-in only** | Needed for Phase 3 offline analysis; too heavy to leave on permanently. |
| Baseline learning, thresholds, event derivation | **Either** — start on-node | Already implemented on-node with 60 s ambient calibration. |
| Vital-sign estimation (breathing/HR) | **Off-node, later** | See §6.3. Not a Phase-2 goal. |
| Multi-person separation, ML inference | **Off-node** | Not credible on this hardware. |
| Dashboard, event log, config UI | **CYD** | See §5.1. |

### 5.1 The CYD is *also* a classic ESP32 — design accordingly

The ESP32-2432S028 is an ESP32-WROOM-32 with a 320×240 ILI9341 over SPI and an
XPT2046 resistive touch controller, typically 4 MB flash and **no PSRAM**. It is not
a more capable device than the sensing node — it is the *same* device with a screen.

Two consequences:

1. **The CYD must consume a derived, low-rate feature stream, not raw CSI.** Pushing
   20 Hz × 128-byte I/Q frames into an LVGL app on a WROOM-32 is not a good use of
   the part. RuView already defines exactly the right packet for this: the 48-byte
   `edge_feature_pkt_t` (`EDGE_FEATURE_MAGIC = 0xC5110003`).
2. **`display_hal.c` cannot be reused.** It is SH8601/QSPI/FT3168/TCA9554. The CYD
   needs a fresh `esp_lcd_panel_ili9341` + XPT2046 HAL. LVGL 8.3 (already a managed
   dependency) carries over; nothing else does.

---

## 6. Hardware limitations — stated plainly

### 6.1 What the original ESP32 gives up versus the S3/C6

| | ESP32 (DevKitC-32) | ESP32-S3 | ESP32-C6 |
|---|---|---|---|
| Wi-Fi | 802.11 b/g/n, HT20/HT40 | same | **+ 802.11ax (HE)** |
| CSI sub-carriers | 64 bins / 56 useful (HT20) | same | **242 useful (HE20, IDF ≥ 5.5)** |
| Antennas | 1 | 1 | 1 |
| PSRAM on target board | **none** | 8 MB | n/a |
| Usable DRAM | ~320 KB | ~400 KB+ | ~320 KB |
| FPU | yes (single-precision) | yes | **none** |
| TWT / deterministic cadence | no | no | yes |
| 802.15.4 side-channel | no | no | yes |

The meaningful loss is **spectral resolution**: 56 useful sub-carriers versus 242
on a C6 talking to an 11ax AP under IDF 5.5+. That is a ~4× reduction in the width
of the feature vector. It does not prevent presence/motion sensing — the original
CSI sensing literature was built on 30–56 sub-carrier Intel 5300 and ESP32 data —
but it does cap the ceiling for anything finer-grained.

Note this loss is **partly theoretical in your environment**: RuView's own logs
(`WITNESS-LOG-110` §B1) record that against an 11n-only AP, even a C6 receives
64-sub-carrier HT frames. Unless you have an 802.11ax AP, the C6 advantage
largely evaporates. **On an 11n network, a DevKitC-32 and a C6 see nearly the
same thing.**

### 6.2 Single antenna, single radio

One antenna means no spatial diversity and no angle-of-arrival. Everything is
derived from temporal variation of a single channel response. Direction, position,
and person-count are not directly observable from one such node.

### 6.3 On the vital-signs claims

RuView advertises breathing (0.1–0.5 Hz bandpass) and heart rate (0.8–2.0 Hz
bandpass) via zero-crossing BPM. That code will *compile and produce numbers* on a
DevKitC-32. Per engineering principle 4, I want to be explicit that this is a
**derived feature, not a validated detection**, on this hardware — for reasons that
are structural, not implementation quality:

- A zero-crossing estimator on a narrow bandpass will emit a plausible BPM from
  filtered noise. It has no intrinsic "no signal present" state.
- Heart-rate extraction at 0.8–2.0 Hz from 20 Hz single-antenna HT20 CSI is at the
  edge of what the physics supports, and demands a stationary subject at close range.
- Validating it needs ground truth (chest strap / pulse oximeter) that this project
  does not currently have.

I would not put breathing or heart rate on the CYD dashboard in Phase 4. Presence
and motion are defensible; vitals are a research question.

### 6.4 CSI yield depends on network traffic, not on us

CSI is produced only when frames arrive. RuView already works around this with a
MGMT-only promiscuous filter (~10 Hz from beacons), 10 Hz probe-request injection,
and a self-ping traffic source. Expect **~20 Hz** in practice on a quiet network,
which sets a **~10 Hz Nyquist ceiling** on observable motion. That is fine for
presence and walking; it is marginal for anything faster.

### 6.5 Practical board notes

- No native USB — the DevKitC-32 uses a CP2102/CH340 bridge, so no USB-CDC console
  and no USB-JTAG. Serial only.
- No ADC2 while Wi-Fi is active (an ESP32 erratum). Irrelevant now; relevant if
  analog sensors get added later.
- GPIO 34–39 are input-only.

---

## 7. Defects found during the audit (affecting all targets)

These are pre-existing upstream issues, found while tracing the data path. The
first two matter more for the ESP32 port than for the S3; the third is latent for
us but constrains how the fork allocates wire formats.

### 7.1 `first_word_invalid` is never checked — anywhere

`wifi_csi_info_t` carries:

```c
bool first_word_invalid;  /* first four bytes of the CSI data is invalid
                             due to a hardware limitation */
```

A repository-wide search for `first_word_invalid` across `.c`, `.h`, `.py` and `.rs`
returns **zero hits**. `csi_serialize_frame()` copies `info->buf` wholesale from
byte 0 (`csi_collector.c:241`).

When the flag is set, the first two I/Q pairs are hardware garbage being fed
straight into amplitude, phase, and variance computation. Because the affected bins
are at a fixed index, they contribute a *persistent* bias — exactly the kind of
artefact that corrupts a variance-threshold presence detector. This is a documented
limitation on the original ESP32 and needs handling in the port; the fix belongs
upstream too.

### 7.2 `scripts/record-csi-udp.py` decodes the wrong header layout

The recorder — the natural starting point for Phase 3 logging — assumes:

```python
# ADR-018 header: [magic(2), len(2), node_id(1), seq(1), rssi(1), channel(1), iq_data...]
rssi     = ... data[6]
channel  = data[7]
iq_data  = data[8:]
```

The actual wire format, per `csi_collector.h` (`CSI_HEADER_SIZE 20`) and the
authoritative Rust parser (`v2/crates/wifi-densepose-hardware/src/esp32_parser.rs`,
`const HEADER_SIZE: usize = 20`), is:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic `0xC5110001` (LE) |
| 4 | 1 | node_id |
| 5 | 1 | n_antennas |
| 6 | 2 | n_subcarriers (LE) |
| 8 | 4 | freq_mhz (LE) |
| 12 | 4 | sequence (LE) |
| 16 | 1 | rssi (i8) |
| 17 | 1 | noise_floor (i8) |
| 18 | 1 | ppdu_type |
| 19 | 1 | flags |
| 20 | … | I/Q payload (imag, real) |

So the script reads `rssi` from the low byte of `n_subcarriers`, invents a `channel`
field that does not exist on the wire, and includes 12 bytes of header in the I/Q
payload — shifting every sub-carrier by six positions. **Any analysis produced with
this script is wrong.** It needs replacing before Phase 3, not adapting.

(Note also there is no `channel` field in ADR-018 at all — only `freq_mhz`. For
channel-hopping experiments the channel has to be derived from frequency.)

### 7.3 Magic number `0xC5110007` is allocated twice

Firmware, `main/wasm_runtime.h:55`:

```c
#define WASM_OUTPUT_MAGIC    0xC5110007    /**< WASM output packet magic (post-#928). */
```

Host parser, `v2/crates/wifi-densepose-hardware/src/esp32_parser.rs:60`:

```rust
/// ADR-095 / #513 on-device temporal-classification packet.
pub const RUVIEW_TEMPORAL_MAGIC: u32 = 0xC5110007;
```

Both are transmitted on the same UDP port and demultiplexed by magic. The Rust
`ruview_sibling_packet_name()` will label a WASM output packet as
"ADR-095 temporal classification".

This is notable because it is *the same defect issue #928 was raised to fix*. The
comment above `WASM_OUTPUT_MAGIC` explains that WASM output was moved off
`0xC5110004` precisely because it collided with ADR-063 fused vitals, and describes
`0xC5110007` as "next free slot in the registry" — but the registry comment block in
`rv_feature_state.h` lists only firmware-side allocations, and ADR-095's claim on
`…07` lives in the Rust crate. The two allocation lists were never reconciled.

Neither packet is enabled in the ESP32 port (WASM needs PSRAM; temporal
classification is host-side), so this does not block us. It does mean **the fork
should not allocate new packet magics from the `0xC51100xx` series** — see
`ESP32_CSI_PORT.md` §4.3.

---

## 8. Open questions — now answered on real hardware

**Status: MEASURED.** Board `34:5f:45:aa:6f:8c`, ESP32-D0WD-V3 rev v3.1, 4 MB
flash, flashed from CI run 32388159744 and run for a 600-second soak on
2026-08-20. Evidence: `data/phase3/stageA-soak-10min.json` (committed); raw
console logs stay local per §7 of the Phase 3 procedure. Reproduce with
`python tools/stage-a-gate.py --udp --duration 600`.

| # | Question asked in the audit | Answer | Verdict |
|---|---|---|---|
| 1 | Free DRAM after Wi-Fi + CSI + DSP init? | **136 824 B low-water**, 142 456 B free, slope −0.3 B/s over 600 s | Comfortable. The §4.2 estimate held. |
| 2 | Does it link within IRAM with `EXTRA_IRAM_OPT=y`? | Yes — no overflow, image 893 856 B, 53 % partition slack | The flagged risk did not materialise. |
| 3 | Achievable CSI frame rate? | **11.90 Hz sustained** (9.9–13.0 Hz), Nyquist ceiling **5.95 Hz** | Lower than the S3's ~20 Hz. See below. |
| 4 | Does the `wDev_ProcessFiq` crash reproduce? | 0 reboots in 600 s | Provisionally clear; needs the 24 h soak. |
| 5 | How often is `first_word_invalid` set? | **100.0 % of frames (7047/7047)** | Far worse than assumed. See below. |
| 6 | Does variance separate occupied from empty? | Still open | Stage B. |

### 8.1 The rate is ~12 Hz, and roughly half of it is thrown away by us

The radio delivers about **23 callbacks/second**; the firmware's early rate gate
(`CSI_MIN_PROCESS_INTERVAL_US`) discards roughly half — measured `cb=7047` against
`drop=8534` over the same window. So 11.9 Hz is a *firmware policy* number, not a
hardware ceiling.

That gate exists to prevent the RuView#396 SPI-cache crash, so it is not free to
raise. But it means the Nyquist ceiling of 5.95 Hz is self-imposed and there is
probably headroom if a soak test shows the original ESP32 does not reproduce #396.
That is a measurement to run, not an assumption to act on.

### 8.2 `first_word_invalid` is set on every single frame

Not "sometimes", not "on some silicon" — **100.0 %**, 7047 of 7047 frames, with the
DSP correctly excluding 2 bins (`skip=2`) on all of them.

This makes §7.1 considerably more serious than it read as a code review. Upstream
discards this flag everywhere, so on the original ESP32 upstream feeds
hardware-invalid I/Q into amplitude, phase and Welford variance **on every frame**,
at a fixed subcarrier index — precisely the bias profile that corrupts a
variance-threshold presence detector. The fix was not tidying.

Whether the S3 also sets it this often is untested and worth checking, since the
same defect would apply there.

### 8.3 All three LTF combinations appear

`len=128 / sc=64 / sig=0` dominates (LLTF only, non-HT), with `len=256 / sc=128 /
sig=1` (LLTF+HT-LTF) and `len=384 / sc=192 / sig=1 / bw40=1` (all three LTFs,
40 MHz) also observed. That is exactly the ESP-IDF documented table in §2.3,
confirmed on silicon.

### 8.4 Still open

- The 24-hour A7 soak (§4 of the runbook).
- Everything in Stage B — whether any of this separates a person from an empty room.

---

## 9. Build path — a real constraint on this machine

Worth stating early because it shapes how Phase 2 should be sequenced.

This laptop is **Windows 11 on ARM64 (Qualcomm Snapdragon)**. Current state:

| Tool | Status |
|---|---|
| ESP-IDF | **not installed** (no `IDF_PATH`, no `~/.espressif`) |
| Docker | **not installed** — so upstream's "only reliable method" is unavailable |
| PlatformIO | installed, `espressif32@6.13.0` + `toolchain-xtensa-esp32` |
| esptool | installed (v5.3.1) — **flashing works today** |
| ESP32 boards attached | none detected (only Qualcomm ACPI UARTs on COM1/COM2) |

Three viable options, in my order of preference:

1. **Use the fork's GitHub Actions CI to build.** `.github/workflows/firmware-ci.yml`
   already runs in the `espressif/idf:v5.4` container on every branch push touching
   `firmware/**`. Adding an `esp32/4mb` matrix row gives a reproducible, correctly
   versioned build with downloadable artefacts — **no local toolchain needed**, and
   it matches upstream exactly. Flash the artefact with the esptool you already have.
2. **Install ESP-IDF v5.4 locally.** Works on Windows-on-ARM under x64 emulation;
   slower, and the official installer is not ARM64-native.
3. **Install Docker Desktop** and use the upstream command verbatim.

PlatformIO is *not* a good fit here: its `espressif32@6.x` platform pins ESP-IDF
5.1.x, below the v5.4 this project targets, and it would fork the build system away
from upstream for no benefit.

**Recommendation: option 1 for Phase 2.** It removes the local toolchain from the
critical path entirely and keeps the fork's builds byte-comparable with upstream's.

---

## 10. Verdict

| Question | Answer |
|---|---|
| Can RuView's CSI acquisition run on an ESP32 DevKitC-32? | **Yes.** Same API, same struct, same code path as the S3. |
| Does any part of the sensing core require S3 or C6? | **No.** |
| What genuinely requires an S3? | The AMOLED display stack (specific panel + PSRAM). |
| What genuinely requires a C6? | TWT, 802.15.4, LP-core, HE/242-sub-carrier CSI. All already guarded. |
| What genuinely requires PSRAM? | The WASM runtime and the LVGL framebuffers. |
| Is the DSP pipeline computationally feasible? | **Yes**, with room to spare — the ESP32 has an FPU the C6 lacks. |
| Is RAM sufficient? | **Probably**, ~38 KB static DSP against ~320 KB DRAM — but unmeasured. |
| Biggest technical risk? | IRAM overflow at link time, then DRAM headroom, then CSI yield. |
| Biggest *scientific* risk? | That variance-based presence does not separate cleanly from environmental drift in a real room. Phase 3 exists to find out. |

**Recommendation: proceed to Phase 2**, with the implementation plan in
[`ESP32_CSI_PORT.md`](ESP32_CSI_PORT.md).

---

*Audit performed by reading source at commit `a3b6e1d5`, cross-checked against
ESP-IDF v5.4 headers and documentation fetched from `espressif/esp-idf`. No claim
in this document is based on measurements taken on original-ESP32 hardware; every
number attributed to real hardware is either from ESP-IDF documentation or from
RuView's own witness logs, and is labelled as such.*
