# Phase 3 — validating what the DevKitC-32 can actually sense

**Status:** Stage A **PASSED** on real hardware (2026-08-20). Stage B not yet
run -- a transmitter-mixing confound was measured first and must be resolved
before the run set begins. See section 4b.
**Prerequisites:** [`ESP32_ORIGINAL_COMPATIBILITY.md`](ESP32_ORIGINAL_COMPATIBILITY.md),
[`ESP32_CSI_PORT.md`](ESP32_CSI_PORT.md)
**Evidence status:** Stage A is `MEASURED` (board `34:5f:45:aa:6f:8c`,
`data/phase3/stageA-soak-10min.json`). Everything about *sensing* remains
`CLAIMED` -- no presence result exists.

---

## 0. What this phase is for

The firmware builds and the wire formats are verified. Nothing about **sensing**
has been demonstrated. This document is the procedure for finding out what the
hardware can and cannot do — including the outcomes where the answer is "it
can't".

Two framing points that shape the whole design:

1. **Stage A comes before any sensing question.** If the node does not hold heap,
   does not sustain a usable frame rate, or crashes overnight, then any presence
   result is noise dressed up as a finding. Stage A is not a warm-up; it is a gate.

2. **The negative controls are mandatory and are not optional extras.** A moving
   fan and an opening door both change the RF channel. If those score like a
   walking person, then what has been built is a *motion-sensitive RF change
   detector*, not a presence detector — and the documentation must say so. Runs H
   and I exist to make that outcome findable rather than avoidable.

---

## 1. Equipment and setup

| Item | Notes |
|---|---|
| ESP32 DevKitC-32 | The sensing node. |
| Micro-USB data cable | Many are charge-only; a charge-only cable presents no COM port. |
| A host on the same LAN | Runs `tools/csi-capture.py`. No Internet needed. |
| A 2.4 GHz AP | The node associates to it. CSI comes from received frames. |
| An oscillating fan | Run H. Any fan that moves air across the room. |
| A door in the monitored volume | Run I. |

Fix these before the first run and **do not change them between runs** — every
comparison below assumes they are constant:

- node position and orientation (mark the spot; tape works)
- antenna orientation
- AP and Wi-Fi channel
- room furniture layout
- time of day (RF environments differ morning vs evening)

Record anything that does change in `--note`.

---

## 2. Flashing and provisioning

Full commands are in the handover section of the session report; summarised here
so this document stands alone.

```bash
# 1. Flash. NOTE: bootloader at 0x1000 on the original ESP32 (0x0 on S3/C6).
python -m esptool --chip esp32 --port COM<N> --baud 460800 \
  write-flash --flash_mode dio --flash_size 4MB --flash_freq 40m \
  0x1000  bootloader.bin \
  0x8000  partition-table-esp32.bin \
  0xf000  ota_data_initial.bin \
  0x20000 esp32-csi-node-esp32.bin

# 2. Provision WiFi + where to stream. No reflash needed to change these.
python firmware/esp32-csi-node/provision.py --port COM<N> --chip esp32 \
  --ssid "<SSID>" --password "<PASSWORD>" \
  --target-ip <HOST_IP> --target-port 5005 --node-id 1
```

Credentials go into NVS via `provision.py`. Do not put them in `sdkconfig`, in
source, or in a shell history you will later paste into an issue.

---

## 3. Stage A — instrument first (the gate)

Nothing about presence is meaningful until these pass. Watch the node with:

```bash
python tools/csi-capture.py monitor
```

The firmware prints a `csi_diag` line every second over serial as well:

```
csi_diag: up=61s rate=19.4Hz cb=1183 drop=402 tx=1180/1183 len=148 sc=64
          skip=2 fwi=1183 ch=6 sig=1 bw40=0 rssi=-54[-61..-48] nf=-93
          heap=181240 min=176008 blk=110580
```

| # | Question | Where to read it | Pass condition |
|---|---|---|---|
| A1 | Does it boot and associate? | serial, `flags` bit 0 | `wifi_connected` true |
| A2 | Does the DSP fit in DRAM? | `free_heap`, `min_free_heap` | `min_free_heap` stable over 1 h, comfortably above zero |
| A3 | Is the heap fragmenting? | `largest_free_block` | not trending down over 1 h |
| A4 | What CSI rate does the radio sustain? | `csi_rate_hz` | steady; **record the number, whatever it is** |
| A5 | Which LTFs does the link deliver? | `last_len` | note the distribution of 128/256/384 |
| A6 | Does the hardware flag invalid bins? | `fwi_total`, `skipped_subcarriers` | record the fraction; `skip` should be 2 if ever flagged |
| A7 | Does it survive a soak? | 24 h run | no reboot, no `wDev_ProcessFiq` crash |

**A4 sets the ceiling for everything after it.** A sustained rate of R Hz means
motion faster than R/2 Hz is not observable, full stop. If R comes out at 5 Hz
rather than the ~20 Hz the S3 achieves, that is the finding, and Stage B's
expectations must be revised down rather than the number talked up.

Capture Stage A as a recording so the numbers are re-checkable:

```bash
python tools/csi-capture.py record --label stageA-soak --duration 3600 \
  --out data/phase3 --note "node on shelf N wall, AP ch6, empty flat"
```

---

## 4. Stage B — the controlled protocol

Nine runs. **All nine, including H and I.** Each is one recording with an
operator-asserted label.

```bash
python tools/csi-capture.py record --label <LABEL> --duration <SECONDS> \
  --out data/phase3 --note "<what you actually did>"
```

| Run | `--label` | Duration | What you do | Why |
|---|---|---:|---|---|
| A | `empty-baseline` | 30 min | Leave. Nobody in the room. | The reference distribution. |
| B | `empty-overnight` | 8 h | Leave. Nobody enters. | Environmental drift, thermal, neighbours' Wi-Fi. Sets the false-positive floor. |
| C | `enter-exit` | 10 min | Enter, wait 15 s, leave, wait 15 s. ×20. Note times. | Transition detectability and latency. |
| D | `walking-continuous` | 10 min | Walk around continuously. | Upper bound on signal strength. |
| E | `stationary-seated` | 20 min | Sit still, normal breathing. | **The hard case.** Micro-motion only. |
| F | `stationary-standing` | 10 min | Stand still. | Compare against E. |
| G | `two-people` | 10 min | Two people moving. | Does the metric separate or saturate? |
| **H** | `empty-with-fan` | 10 min | **Nobody present. Oscillating fan running.** | **Negative control — mandatory.** |
| **I** | `empty-door-open` | 10 min | **Nobody present. Open/close the door every 30 s from outside the room.** | **Negative control — mandatory.** |

Notes on the two controls, because they are the runs most likely to get skipped:

- **H (fan)** tests whether the detector distinguishes *human* motion from *any*
  motion. Physically it should not — a single-antenna variance metric has no
  mechanism to tell a person from a fan. Running it turns a known limitation into
  a measured one.
- **I (door)** tests sensitivity to *geometry change without a person in the
  volume*. A door swinging alters multipath substantially. Operate it from
  outside the room so your own body is not in the monitored volume.

Log ground truth by hand alongside each run (a phone timestamp note is fine).
The label is operator-asserted, not measured — `csi-capture.py` records it as
such in the file header and does not treat it as evidence.

---

## 4b. MEASURED CONFOUND: transmitter mixing (found 2026-08-20)

**Read this before recording any Stage B run.** The first `empty-baseline`
capture (17 994 frames, 900 s, house empty) contains a confound large enough to
invalidate naive comparisons.

### What was measured

RSSI over the run is cleanly **bimodal**, with almost nothing between -63 and
-45 dBm:

| Cluster | RSSI | Frames | Mean amplitude |
|---|---|---:|---:|
| Near | -24 to -36 dBm | 8 503 (51 %) | 13.05 (sd 4.59) |
| Far | ~-69 dBm | 8 136 (49 %) | 20.66 (sd 2.52) |

Both on channel 6. Two distinct transmitters, split almost evenly -- consistent
with the hub plus an extender, the extender being the one the node associates
to (`bssid = 0a:3c:c5:9b:82:ae`, locally-administered bit set).

The problem is the interleaving:

```
consecutive frames switching source     : 94.3 % (15 686 of 16 638)
mean |amplitude jump| when source SWITCHES : 8.05
mean |amplitude jump| when source is SAME  : 2.24
                                     ratio : 3.6x
```

**The activity metric is therefore dominated by which transmitter sent each
frame, not by whether anything in the room moved.** A presence threshold built
on this would be keying off the traffic mix. It is precisely the failure that
produces a confident, meaningless result.

### Why it cannot be fixed in post-processing

**ADR-018 carries no source MAC.** `csi_collector` has `info->mac` in the
callback and does not serialise it, so a recorded frame cannot be attributed to
a transmitter after the fact.

RSSI clustering rescued this particular recording only because the two sources
happen to sit ~36 dB apart. With three transmitters at comparable distances --
the router-plus-two-extenders configuration in section 9 of the project brief --
that proxy stops working entirely.

### Options

| | Effect | Cost |
|---|---|---|
| Turn extenders off | Single source | Degrades household Wi-Fi; forces the node onto the distant hub at ~-69 dBm, changing the dominant path, so the baseline needs retaking anyway |
| **MAC filter (ADR-060 `--filter-mac`)** | Single source, geometry unchanged | Rate roughly halves; no code change |
| **Source MAC in ADR-018** | All transmitters retained and separable offline | Firmware change and reflash |

The MAC filter is the quick unblock. Adding the source MAC (or a 2-byte hash) to
the frame is the correct fix, and it is a *prerequisite* for the multi-transmitter
experiment rather than an optimisation of it: attributed, extra transmitters are
extra viewpoints; blended, they are noise.

### Consequence for the run set

`empty-baseline-20260820T211716Z` was recorded unfiltered and is retained as
evidence of the confound, not as a usable reference distribution. Every Stage B
run must share one transmitter configuration, and the baseline must be re-taken
under whichever is chosen.

---

## 5. Stage C — analysis

Offline, over the raw `.rvcsi` logs, so it re-runs unchanged as the feature
extraction evolves:

```bash
python tools/csi-capture.py info data/phase3/empty-baseline-*.rvcsi
python tools/csi-capture.py replay data/phase3/empty-baseline-*.rvcsi \
  --host 127.0.0.1 --port 5005 --speed 10
```

### 5.1 What to compute

For each run, from the per-subcarrier amplitudes in the JSONL sidecar:

1. Per-subcarrier variance over a sliding window (2 s and 10 s).
2. A scalar activity metric — total variance across the top-K subcarriers.
3. The distribution of that metric per run.

Exclude bins flagged by `first_word_invalid`. The sidecar already reports them as
`null` rather than a number, so an analysis that naively averages will produce a
`TypeError` rather than a plausible wrong answer. That is deliberate.

### 5.2 What counts as a result

| Comparison | Question | Reported as |
|---|---|---|
| A vs D | Does walking separate from empty? | ROC AUC, not a hand-picked threshold |
| **A vs E** | Does a seated still person separate from an empty room? | ROC AUC. **The real test.** |
| A vs C | How fast, and how reliably, is a transition detected? | latency distribution, missed transitions |
| D vs G | Does the metric distinguish one person from two? | overlap of distributions |
| **A vs H** | Does a fan look like a person? | ROC AUC. **If ≈ A-vs-D, say so.** |
| **A vs I** | Does a door look like a person? | ROC AUC |
| B alone | What does 8 h of nothing look like? | drift magnitude → false-positive floor |

Any threshold must be justified against **run B**, not against a 30-minute
sample. A threshold tuned on 30 minutes of quiet and then run overnight is how
a presence sensor that fires at 3 a.m. gets built.

### 5.3 Outcomes to be prepared to report

State plainly whichever of these the data shows:

- **A vs E shows no separation.** Likely. Single-antenna HT20 CSI at ~20 Hz may
  simply not resolve a still person. Report it as a limitation of the approach on
  this hardware, not as a tuning problem.
- **H scores like D.** Then the honest name for the artefact is "RF change
  detector", the UI must say "activity" rather than "presence", and the
  compatibility report's §6.3 caution extends to presence as well as vitals.
- **B drifts enough to swamp C.** Then the useful output is relative change over
  minutes, not an absolute occupancy state.
- **It works.** Then it is `MEASURED`, with the recordings and the analysis script
  committed so the number is reproducible.

---

## 6. Promotion rules

Per `CLAUDE.md`, accuracy statements carry `MEASURED` / `CLAIMED` / `SYNTHETIC`,
and hardware validation needs evidence from real silicon.

Nothing from this phase may be described as a **validated detection** until all
of the following hold:

1. Stage A passed, with the soak log committed.
2. All nine Stage B runs exist, **including H and I**, as raw `.rvcsi`.
3. The analysis is a committed script, runnable against the recordings.
4. Separation is reported as ROC AUC against run B's false-positive floor.
5. The negative-control results are reported **next to** the positive results,
   not in a footnote.

Until then, everything the firmware emits is a *measured signal* or a *derived
feature*, and the presence value is an *experimental inference*.

Do not label any of it "human detection", "occupancy", or "through-wall". What is
measured is a change in the Wi-Fi channel response consistent with movement in
the monitored volume. A person produces that. So does a fan.

---

## 7. Data handling

The recordings are RF observations of a room in a home. They are not video, but
they are not nothing either.

- Keep them local. Nothing in this pipeline needs the Internet.
- `--max-size-mb` and `--retain-days` exist on `csi-capture.py`; use them rather
  than accumulating months of captures by default.
- Do not paste raw recordings into public issues. Summary statistics are fine.
- If anyone other than you is in the monitored space during runs C-G, tell them
  what is being recorded first.
