# ESP32 DevKitC-32 CSI port — implementation

**Status:** Implemented. All six commits landed and CI-verified.
**Prerequisite:** [`ESP32_ORIGINAL_COMPATIBILITY.md`](ESP32_ORIGINAL_COMPATIBILITY.md)
**Next phase:** [`ESP32_PHASE3_VALIDATION.md`](ESP32_PHASE3_VALIDATION.md)
**Branch:** `feat/esp32-original-csi-port`

---

## 1. Design principles for this port

1. **Additive only.** Every change is a new file, a new `#elif` branch, or a new CI
   matrix row. No existing S3 or C6 code path changes behaviour. The test for this:
   after the port, the `esp32s3/8mb`, `esp32s3/4mb` and `esp32c6/c6-4mb` CI jobs must
   still produce working images.
2. **Measure before optimising.** The first firmware's job is to tell us what the
   hardware does, not to be clever. Diagnostics are a feature, not debug scaffolding.
3. **Separate the four claim levels.** Wire format, log output, and UI must keep
   *measured signal*, *derived feature*, *experimental inference*, and *validated
   detection* visibly distinct. See §5.
4. **Prefer reusing RuView's structures.** ADR-018 frames and `edge_feature_pkt_t`
   already exist and already have host-side parsers. Inventing a parallel format
   would be the wrong kind of divergence.

---

## 2. Changes, in commit order

Each of these landed as one logical commit. Snippets below are the shipped
versions.

### Commit 1 — `sdkconfig.defaults.esp32` (new file)

An ESP-IDF target overlay, auto-applied when `CONFIG_IDF_TARGET=esp32`, layered on
`sdkconfig.defaults`. Deliberately conservative:

```
CONFIG_IDF_TARGET="esp32"

# 4 MB flash, dual OTA — reuse the existing table
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions_4mb.csv"
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESPTOOLPY_FLASHSIZE="4MB"

# CSI — the whole point
CONFIG_ESP_WIFI_CSI_ENABLED=y

# No PSRAM on a WROOM-32: both of these MUST stay off
# CONFIG_DISPLAY_ENABLE is not set
# CONFIG_WASM_ENABLE is not set

# Full clock for DSP headroom
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ=240

# Size optimisation — IRAM/flash are tighter here than on the S3
CONFIG_COMPILER_OPTIMIZATION_SIZE=y
```

**Open risk:** `CONFIG_ESP_WIFI_EXTRA_IRAM_OPT=y` is inherited from
`sdkconfig.defaults`. If the first build overflows IRAM, this overlay is where we
turn it off (`CONFIG_ESP_WIFI_EXTRA_IRAM_OPT=n`) and re-measure. I will not
pre-emptively disable it — that would be optimising before measuring.

Naming note: this file is `sdkconfig.defaults.esp32`, deliberately *not* anything
containing "devkitc", because `sdkconfig.defaults.devkitc` already means
ESP32-**S3**-DevKitC-1 (see compatibility report §3.3).

### Commit 2 — fix the LED GPIO fallthrough

`main/main.c`. Turn the two-way branch into three-way:

```c
#if defined(CONFIG_IDF_TARGET_ESP32C6)
    const int led_gpio = 8;
#elif defined(CONFIG_IDF_TARGET_ESP32)
    const int led_gpio = -1;   /* DevKitC-32 has no addressable LED */
#else
    const int led_gpio = 48;   /* S3 DevKitC-1 v1.1 / N16R8 */
#endif
```

…and skip `led_strip_new_rmt_device()` entirely when `led_gpio < 0`. This is a
strict improvement on the S3 and C6 paths (unchanged) and removes a guaranteed
boot-time error on ESP32.

### Commit 3 — handle `first_word_invalid`

`main/csi_collector.c`. This is the correctness fix from compatibility report §7.1
and it matters on every target, so it is written target-neutrally.

Proposal: rather than silently dropping bytes (which would desynchronise the
sub-carrier indexing that host parsers depend on), **surface it on the wire and
neutralise it in the DSP**:

- Set a new bit in ADR-018 byte 19 — `bit 5 = first_word_invalid` — so host-side
  consumers can mask the affected bins themselves. Byte 19 bits 0/2/4 are already
  used for bw40 / STBC / sync-valid; bit 5 is free, and readers that don't know
  about it are unaffected (backward-compatible, same pattern as the existing
  ADR-110 extension).
- In `edge_enqueue_csi()`, exclude sub-carrier index 0 and 1 from the variance and
  phase computation when the flag is set, instead of feeding hardware garbage into
  the presence threshold.

This needs the ADR-018 field table updating in the same commit.

### Commit 4 — `csi_diag`: the measurement module (new files)

`main/csi_diag.c` / `.h`. This is the module that answers the open questions in
compatibility report §8. It is a new, self-contained file — no existing module
changes — and it is what makes the first firmware genuinely useful.

Emits one line per interval over the serial console *and* one 60-byte UDP packet
(magic `0xC5111001`), containing only measured quantities:

| Field | Type | Level |
|---|---|---|
| `uptime_ms` | u32 | measured |
| `csi_cb_count` | u32 | measured — raw callback count this second |
| `csi_rate_hz` | f32 | measured |
| `frames_dropped_rate_gate` | u32 | measured |
| `frames_sent_ok` / `send_fail` | u32 | measured |
| `last_len_bytes` | u16 | measured — tells us 128 / 256 / 384 |
| `n_subcarriers` | u16 | measured |
| `sig_mode` / `cwb` / `stbc` / `secondary_channel` | u8 | measured |
| `first_word_invalid_count` | u32 | measured |
| `rssi_mean` / `rssi_min` / `rssi_max` | i8 | measured |
| `noise_floor_mean` | i8 | measured |
| `free_heap_bytes` / `min_free_heap_bytes` | u32 | measured |
| `largest_free_block` | u32 | measured |

That last group is deliberately included: it directly answers open question §8.1
(does it fit in DRAM) from the device itself, on the first boot, rather than by
estimation.

### Commit 5 — CI matrix row

`.github/workflows/firmware-ci.yml`: add

```yaml
- variant: esp32-4mb
  target: esp32
  sdkconfig: sdkconfig.defaults
  partition_table_name: partitions_4mb.csv
  size_warn_kb: 1000
  size_limit_kb: 1152
  artifact_app: esp32-csi-node-esp32.bin
  artifact_pt: partition-table-esp32.bin
```

`sdkconfig.defaults.esp32` is picked up automatically from the target name, so
the build step itself needed no change. Two supporting edits were required:
staging `bootloader.bin` and `ota_data_initial.bin` for this variant (the
bootloader is chip-specific, and **on the original ESP32 it flashes to 0x1000,
not 0x0**), and running `test_fwi` on this job.

Purely additive — the three existing rows are untouched, and `fail-fast: false` is
already set, so an ESP32 build failure cannot mask an S3/C6 result.

**This is also our build machine** (compatibility report §9): no local ESP-IDF or
Docker is required. Push the branch, download the artefact, flash with the esptool
already installed.

### Commit 6 — `tools/csi-capture.py` (new file)

A correct replacement for `scripts/record-csi-udp.py`, which decodes the wrong
header layout (compatibility report §7.2). I propose a *new* file rather than
editing the existing script, so the fix is isolated and the upstream script can be
fixed separately as its own PR.

Responsibilities:
- Bind UDP, demultiplex by magic (`0xC5110001` CSI / `0xC5110003` feature /
  `0xC5110002` vitals / the new diag magic) — the existing Rust parser already
  models this multiplexing, so the Python must too.
- Decode the real 20-byte header.
- Write **raw frames verbatim** to a `.rvcsi` binary log (length-prefixed), plus a
  human-readable JSONL sidecar. Raw-first matters: it makes the recording
  re-analysable when the feature extraction changes, which is the whole point of
  Phase 3 replay.
- `--replay <file>` mode that re-emits a recording to a UDP port at original
  timing, so the same analysis path serves live and recorded data.
- `--label <text>` written into the sidecar header, for marking ground-truth
  segments ("empty", "one person walking", …).

---

## 3. CSI acquisition architecture on the DevKitC-32

Unchanged from RuView's existing design, which is already sound — documenting it
here so the port is reviewable without reading the whole source.

```
 802.11 frames (beacons, probe responses, self-ping)
        │
        ▼
 Wi-Fi driver task ──► wifi_csi_callback()          [Core 0, driver context]
        │                  ├── rate gate ~50 Hz     (SPI-cache-race mitigation)
        │                  ├── optional MAC filter
        │                  ├── csi_serialize_frame() → ADR-018 20 B header + I/Q
        │                  ├── stream_sender_send()  (UDP, rate-gated below 50 Hz)
        │                  └── edge_enqueue_csi()    → SPSC ring, 16 slots
        ▼
 edge task ─────────► amplitude/phase per sub-carrier   [Core 1]
                      Welford running variance
                      top-K sub-carrier selection
                      biquad bandpass + zero-crossing
                      presence hysteresis + 60 s ambient calibration
                            │
                            ▼
                      edge_feature_pkt_t (48 B) ── UDP ──► host / CYD
```

Three properties worth naming because they are what make this workable on a
WROOM-32:

- **The callback does almost nothing.** ESP-IDF explicitly warns that the CSI
  callback runs on the Wi-Fi task; RuView already defers real work to the ring
  buffer. Correct as-is.
- **The rate gate is a safety mechanism, not a tuning knob.** It exists because
  unbounded CSI callbacks crashed Core 0 in the Wi-Fi blob (RuView#396). Do not
  raise it without a soak test.
- **The pipeline is dual-core.** Capture on Core 0, DSP on Core 1. The C6 does this
  single-core; the DevKitC-32 does not have to.

### 3.1 Frame yield strategy

CSI only exists when frames arrive. Three existing mechanisms combine:

| Source | Rate | Mechanism |
|---|---|---|
| Beacons | ~10 Hz | MGMT-only promiscuous filter |
| Probe responses | ~10 Hz | 10 Hz probe-request injection |
| Self-ping | configurable | `csi_start_self_ping()` — unicast to the gateway |

Expected combined: **~20 Hz**, matching the DSP's designed sample rate and giving a
~10 Hz Nyquist ceiling. Phase 3 must *measure* this on the DevKitC-32 rather than
assume the S3 figure carries over (open question §8.3).

---

## 4. Data structures

### 4.1 ADR-018 CSI frame — unchanged, plus one bit

20-byte header (see compatibility report §7.2 for the full table), followed by the
raw I/Q payload copied from `info->buf`: pairs of `int8_t`, **imaginary first, then
real**, in the sub-carrier order the ESP-IDF Wi-Fi guide specifies.

Only proposed change: **byte 19, bit 5 = `first_word_invalid`** (commit 3).

### 4.2 `edge_feature_pkt_t` — unchanged, 48 bytes

Magic `0xC5110003`, `_Static_assert`-ed to 48 bytes. This is the packet the CYD
should consume in Phase 4 — low rate, fixed size, already has host-side parsers.

### 4.3 Diagnostic packet — new, 48 bytes

Fields per §2, commit 4. Fixed 48 bytes to match the existing sibling-packet
convention, so `ruview_sibling_packet_name()` can be taught about it with a one-line
addition.

**Magic: `0xC5111001`, from a deliberately separate fork-local range.**

The obvious choice would have been the next slot in the upstream series, but that
series is already inconsistent. Current allocation:

| Magic | Owner |
|---|---|
| `0xC5110001` | ADR-018 raw CSI frame |
| `0xC5110002` | ADR-039 edge vitals |
| `0xC5110003` | ADR-069 feature vector |
| `0xC5110004` | ADR-063 fused vitals |
| `0xC5110005` | ADR-039 compressed CSI |
| `0xC5110006` | ADR-081 feature state |
| `0xC5110007` | **ADR-040 WASM output *and* ADR-095 temporal classification** |

That last row is a live upstream collision (compatibility report §7.3) — and it was
introduced by the fix for an identical earlier collision, because the firmware-side
registry comment and the Rust-side constant list are maintained separately.

Taking `0xC5110008` would mean betting that upstream has not already claimed it in a
file I have not read. Using `0xC5111xxx` costs nothing, cannot collide with the
upstream series, and makes fork-local packets self-identifying on the wire. If this
work is ever upstreamed, the magic can be renumbered at that point as a deliberate
decision rather than an accident.

---

## 5. Keeping the four claim levels separate

Per engineering principle 4, this is a structural requirement, not a documentation
style. Concretely:

| Level | Example | Where it may appear |
|---|---|---|
| **Measured signal** | `rssi`, `noise_floor`, `len`, `csi_rate_hz`, raw I/Q, `free_heap` | Diagnostics, raw log, CYD "signal" view |
| **Derived feature** | per-sub-carrier amplitude, phase, running variance, top-K variance energy | Feature packet, CYD live view |
| **Experimental inference** | presence confidence, motion score, "environment changed" | CYD dashboard — **must be labelled experimental** |
| **Validated detection** | *(nothing yet)* | Nothing may be promoted here until Phase 3 produces a labelled dataset showing separation |

Two rules that follow from this:

- **Nothing is labelled "human detected".** The honest label for what this measures
  is *"RF channel change consistent with movement in the monitored volume"* — a metal
  door, a fan, or a neighbour's microwave produces the same signature. The UI should
  say "activity" and "presence confidence", never "person".
- **Breathing and heart rate stay off the Phase 4 dashboard.** The code produces
  numbers; those numbers are not validated on this hardware (compatibility report
  §6.3). They may be logged, but must not be displayed as measurements.

This also has to satisfy the repository's own rule (`CLAUDE.md`): accuracy and
performance statements must carry a `MEASURED` / `CLAIMED` / `SYNTHETIC` tag, and
hardware validation requires evidence from real silicon — a build or a simulator
does not count. The two taxonomies map cleanly:

| This document | `CLAUDE.md` tag |
|---|---|
| Measured signal, derived feature (from a real board) | `MEASURED` + reproducer |
| Experimental inference | `CLAIMED` until Phase 3 produces a labelled dataset |
| Anything from `mock_csi.c` or replay of synthesised data | `SYNTHETIC` |

Every claim in this port carries `CLAIMED` until a captured boot/runtime log from a
DevKitC-32 exists in the repository.

---

## 6. Testing methodology (Phase 3 design)

### 6.1 Instrument first

Before any presence claim, establish from `csi_diag`:

1. Boot succeeds; free heap and minimum free heap over a 1-hour soak.
2. Sustained `csi_rate_hz` and its variance.
3. Distribution of `last_len_bytes` (128 / 256 / 384) — tells us which LTFs the
   link actually delivers.
4. `first_word_invalid_count` as a fraction of frames.
5. No `wDev_ProcessFiq` crash over 24 hours.

If any of these fail, the sensing questions are premature.

### 6.2 The controlled protocol

Fixed node position, fixed AP, same room, same time of day. Each run is a labelled
recording made with `tools/csi-capture.py --label`:

| Run | Label | Duration | Purpose |
|---|---|---:|---|
| A | `empty-baseline` | 30 min | Establish ambient variance distribution |
| B | `empty-overnight` | 8 h | Environmental drift, thermal, neighbour Wi-Fi |
| C | `enter-exit` × 20 | 10 min | Transition detectability + latency |
| D | `walking-continuous` | 10 min | Upper bound on signal strength |
| E | `stationary-seated` | 20 min | The hard case — micro-motion only |
| F | `stationary-standing` | 10 min | Compare to E |
| G | `two-people` | 10 min | Does the metric saturate or separate? |
| H | `empty-with-fan` | 10 min | **Negative control** — non-human motion |
| I | `empty-door-open` | 10 min | **Negative control** — geometry change, no person |

Runs H and I matter as much as the positive runs. A detector that fires on a fan is
a motion detector, not a presence detector, and we should know which one we built.

### 6.3 What counts as success

Analysis is offline, over the raw `.rvcsi` logs, so it is re-runnable as the feature
extraction changes:

- Plot the variance metric distribution for A vs D. **Success = visible separation**,
  quantified as ROC AUC — not a threshold that happens to work once.
- A vs E is the real test. If seated-stationary is indistinguishable from empty, say
  so plainly; that is a genuine and expected limitation of single-antenna HT20 CSI,
  not a bug to be tuned away.
- H and I must be reported alongside — if the fan run scores like the walking run,
  the honest headline is "motion-sensitive RF change detector", and the
  documentation must say that.
- B sets the false-positive floor. Any threshold must be justified against 8 hours
  of empty-room drift, not against a 30-minute sample.

### 6.4 Replay

Every conclusion above must be reproducible by
`tools/csi-capture.py --replay <recording>` feeding the same analysis. That is the
difference between "we measured this" and "it worked when we tried it".

---

## 7. Security foundations (from the start, not retrofitted)

Scoped to what is proportionate for a local prototype:

- **No hard-coded credentials.** Wi-Fi credentials already go through
  `provision.py` into NVS. Keep it that way; nothing goes in `sdkconfig` or source.
- **Authenticated local telemetry.** UDP is currently unauthenticated. Proposal for
  Phase 2: a pre-shared key in NVS and an HMAC-SHA256 truncated to 8 bytes appended
  to feature and diagnostic packets, with a monotonic counter for replay resistance.
  Cheap (mbedTLS is already a dependency), and it stops another device on the LAN
  spoofing presence events to the CYD. **Not** applied to raw CSI frames — those are
  high-rate and opt-in-only.
- **Minimise what is transmitted.** Raw I/Q streaming should default to **off**,
  enabled explicitly for capture sessions. Steady-state traffic is the 48-byte
  feature packet.
- **Local logs, configurable retention.** Recordings live on the operator's machine.
  `tools/csi-capture.py` should take `--max-size` / `--retain-days` and enforce them.
- **No cloud, no accounts, no Internet.** Nothing in this plan calls out of the LAN.
  Worth stating because RuView upstream carries OTA-over-HTTP and telemetry paths
  that we simply do not enable.

A note on what CSI sensing *is*, for the record: a device that reports when a room is
occupied is a surveillance sensor, however benign the intent. Keeping it offline,
authenticated, and honestly labelled is the minimum bar, and is why the retention and
"no cloud" points above are requirements rather than nice-to-haves.

---

## 8. Phase 4 sketch — ESP32-2432S028 (not yet designed in detail)

Deliberately brief; this gets a proper design document once the sensing node is
demonstrated. Constraints already established:

- CYD is a WROOM-32, no PSRAM, ILI9341 320×240 SPI + XPT2046 resistive touch.
  `display_hal.c` (SH8601 QSPI AMOLED) is **not reusable** — a new HAL is required.
- LVGL 8.3 is already a managed component and carries over.
- The CYD consumes **`edge_feature_pkt_t` over UDP**, not raw CSI.
- Screens: dashboard, live signal view, event log, config — as specified in the
  project brief.
- Event types (`PRESENCE_STARTED`, `PRESENCE_ENDED`, `MOTION_DETECTED`,
  `ENVIRONMENT_CHANGE`, `SENSOR_CONNECTED`, `SENSOR_DISCONNECTED`) should be derived
  **on the CYD** from the feature stream, so the sensing node stays a pure sensor
  and event policy lives with the UI that displays it.

---

## 9. What this plan explicitly does not do

- Does not touch S3 or C6 code paths beyond adding `#elif` branches.
- Does not port the display stack, WASM runtime, or C6 radio extensions.
- Does not attempt vital-sign validation.
- Does not add ML inference on-device.
- Does not integrate mmWave or any second sensing modality.
- Does not modify `scripts/record-csi-udp.py` in place (a separate upstream fix).

---

## 10. What was built, and what CI proved

All six commits landed. Firmware CI run
[32387105968](https://github.com/JosephR26/RuView/actions/runs/32387105968)
is green on all four targets.

| Target | Variant | Image | Partition slack |
|---|---|---:|---:|
| esp32s3 | 8mb | 1 127 440 B | — |
| esp32s3 | 4mb | 911 504 B | — |
| esp32c6 | c6-4mb | 1 051 248 B | — |
| **esp32** | **esp32-4mb** | **893 856 B** | **53 % free** |

What that does and does not establish:

- **Establishes** that the original-ESP32 target compiles, links and produces a
  valid image (esptool reports `Chip ID: 0 (ESP32)`, validation hash valid), that
  it fits with wide margin, that IRAM did **not** overflow — the risk flagged in
  the compatibility report §4.3 — and that the S3 and C6 targets still build.
  The S3 8 MB image grew by 832 bytes; it was already over the 1100 KB soft
  budget on upstream `main` (1 126 608 B) and remains under the 1152 KB hard gate.
- **Establishes** that the `first_word_invalid` encoding and DSP exclusion behave
  as specified — `test_first_word_invalid` runs in CI and passes.
- **Does not establish anything about sensing, or about RAM at runtime.** A build
  is not hardware evidence. Every runtime number in this document remains an
  estimate until a boot log from real silicon exists. That is what
  [`ESP32_PHASE3_VALIDATION.md`](ESP32_PHASE3_VALIDATION.md) Stage A is for.

### 10.1 Decisions taken

1. **Build path** — the fork's GitHub Actions CI, as recommended. No local
   ESP-IDF or Docker was needed at any point.
2. **`first_word_invalid`** — fixed properly, with the wire-format extension.
   Byte 19 bit 5, payload untouched, subcarrier indexing unchanged.
3. **Diagnostic magic** — `0xC5111001`, fork-local range.

### 10.2 Two things found while implementing

- **`provision.py --help` crashed on this machine.** The file contains em dashes
  and arrows; a Windows console defaulting to cp1252 raises `UnicodeEncodeError`
  before argparse finishes printing, and a provisioning run could fail *after*
  writing to the device. Fixed with a two-line stream guard rather than rewriting
  upstream text. `tools/csi-capture.py` hit the identical bug during testing and
  is now pure ASCII with the same guard.

- **The default UDP port is 5005**, not 5500 — `CONFIG_CSI_TARGET_PORT` and
  `provision.py --target-port` agree on 5005; the fuzz harness's 5500 is a test
  fixture. `csi-capture.py` defaults to 5005 to match the firmware.

### 10.3 Not done, deliberately

The CYD display HAL. Deferred until the sensing node has been demonstrated,
per the phase ordering. §8 remains a sketch.

