#!/usr/bin/env python3
"""
Stage A gate -- collect real runtime diagnostics from an ESP32 DevKitC-32 and
decide, against fixed criteria, whether the node is fit for sensing experiments.

Stage A is a gate, not a warm-up (ESP32_PHASE3_VALIDATION.md section 3). If the
node does not hold heap, does not sustain a usable CSI rate, or reboots, then any
presence result collected afterwards is noise dressed up as a finding.

The criteria are fixed HERE, before any data exists, so the gate cannot be
quietly relaxed to make a marginal board pass. Change them only with a stated
reason, in a commit.

Sources (pick one):
    --serial COM7      parse the per-second `csi_diag:` console line
    --udp              parse csi_diag UDP packets (needs provisioning first)

Serial works before the node has network config, so it is the right source for
the first boot. UDP is the right source for a long unattended soak.

Usage:
    # First boot, 10 minutes, watch the console
    python tools/stage-a-gate.py --serial COM7 --duration 600 \\
        --out data/phase3/stageA-first-boot.json

    # One-hour soak over the network once provisioned
    python tools/stage-a-gate.py --udp --duration 3600 \\
        --out data/phase3/stageA-soak.json

Exit status: 0 if the gate passes, 1 if it fails, 2 if inconclusive
(not enough samples to decide). Suitable for scripting.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------
# Gate criteria -- fixed in advance. See module docstring.
# --------------------------------------------------------------------------

#: Minimum samples before the gate will return anything but INCONCLUSIVE.
MIN_SAMPLES = 30

#: A2. Free heap below this is treated as no headroom for sensing work.
#: 40 KB is roughly the static DSP state measured from edge_processing.h
#: (~38 KB) -- if the low-water mark is under that, one more allocation of the
#: size the pipeline already holds would not fit.
MIN_FREE_HEAP_BYTES = 40 * 1024

#: A3. Fragmentation. If the largest allocatable block falls below this
#: fraction of free heap, the heap is fragmented enough that a large
#: contiguous allocation would fail even though "free heap" looks fine.
MIN_LARGEST_BLOCK_RATIO = 0.25

#: A3. Sustained downward slope in largest-free-block, in bytes per second,
#: that counts as progressive degradation rather than noise.
FRAG_SLOPE_FAIL_BPS = -20.0

#: A4. Below this the CSI rate cannot support the 20 Hz pipeline design.
#: Not a pass/fail on its own -- it sets the Nyquist ceiling that Stage B's
#: expectations must be revised against. Reported loudly either way.
RATE_USABLE_HZ = 8.0

#: A4. Rate stability: interquartile range as a fraction of the median.
RATE_JITTER_WARN = 0.5

#: Sentinel: uptime going backwards means the node rebooted mid-run.
#: Any reboot fails A7 outright.

SERIAL_RE = re.compile(
    r"up=(?P<up>\d+)s\s+"
    r"rate=(?P<rate>[\d.]+)Hz\s+"
    r"cb=(?P<cb>\d+)\s+"
    r"drop=(?P<drop>\d+)\s+"
    r"tx=(?P<tx_ok>\d+)/(?P<tx_total>\d+)\s+"
    r"len=(?P<len>\d+)\s+"
    r"sc=(?P<sc>\d+)\s+"
    r"skip=(?P<skip>\d+)\s+"
    r"fwi=(?P<fwi>\d+)\s+"
    r"ch=(?P<ch>\d+)\s+"
    r"sig=(?P<sig>\d+)\s+"
    r"bw40=(?P<bw40>\d+)\s+"
    r"rssi=(?P<rssi>-?\d+)\[(?P<rssi_min>-?\d+)\.\.(?P<rssi_max>-?\d+)\]\s+"
    r"nf=(?P<nf>-?\d+)\s+"
    r"heap=(?P<heap>\d+)\s+"
    r"min=(?P<minheap>\d+)\s+"
    r"blk=(?P<blk>\d+)"
)

DIAG_MAGIC = 0xC5111001
DIAG_PKT_SIZE = 60
DIAG_STRUCT = struct.Struct("<IBBH IIIII HHHH BBBB bbbb B3x III")


@dataclass
class Sample:
    """One diagnostic snapshot. Every field is measured on the device."""
    t_host: float
    uptime_s: int
    rate_hz: float
    cb_total: int
    drop_total: int
    tx_ok: int
    tx_total: int
    last_len: int
    subcarriers: int
    skip: int
    fwi_total: int
    channel: int
    sig_mode: int
    bw40: int
    rssi: int
    noise_floor: int
    free_heap: int
    min_free_heap: int
    largest_block: int
    wifi_connected: bool | None = None


def parse_serial_line(line: str, t: float) -> Sample | None:
    m = SERIAL_RE.search(line)
    if not m:
        return None
    g = m.groupdict()
    return Sample(
        t_host=t, uptime_s=int(g["up"]), rate_hz=float(g["rate"]),
        cb_total=int(g["cb"]), drop_total=int(g["drop"]),
        tx_ok=int(g["tx_ok"]), tx_total=int(g["tx_total"]),
        last_len=int(g["len"]), subcarriers=int(g["sc"]), skip=int(g["skip"]),
        fwi_total=int(g["fwi"]), channel=int(g["ch"]), sig_mode=int(g["sig"]),
        bw40=int(g["bw40"]), rssi=int(g["rssi"]), noise_floor=int(g["nf"]),
        free_heap=int(g["heap"]), min_free_heap=int(g["minheap"]),
        largest_block=int(g["blk"]),
    )


def parse_diag_packet(data: bytes, t: float) -> Sample | None:
    if len(data) < DIAG_PKT_SIZE:
        return None
    if struct.unpack_from("<I", data, 0)[0] != DIAG_MAGIC:
        return None
    f = DIAG_STRUCT.unpack_from(data, 0)
    return Sample(
        t_host=t, uptime_s=f[4], rate_hz=f[9] / 10.0,
        cb_total=f[5], drop_total=f[6], tx_ok=f[7], tx_total=f[7] + f[8],
        last_len=f[10], subcarriers=f[11], skip=f[13], fwi_total=f[12],
        channel=f[15], sig_mode=f[16], bw40=1 if (f[14] & 0x1) else 0,
        rssi=f[17], noise_floor=f[20],
        free_heap=f[22], min_free_heap=f[23], largest_block=f[24],
        wifi_connected=bool(f[21] & 0x1),
    )


# --------------------------------------------------------------------------
# Pure-stdlib statistics (numpy is not available on this machine)
# --------------------------------------------------------------------------

def quantile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def lsq_slope(ts: list[float], ys: list[float]) -> float:
    """Least-squares slope in y-units per second. 0.0 if undetermined."""
    n = len(ts)
    if n < 2:
        return 0.0
    mt = sum(ts) / n
    my = sum(ys) / n
    num = sum((t - mt) * (y - my) for t, y in zip(ts, ys))
    den = sum((t - mt) ** 2 for t in ts)
    return num / den if den else 0.0


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

@dataclass
class Check:
    id: str
    question: str
    verdict: str          # PASS | FAIL | WARN | INCONCLUSIVE
    detail: str
    values: dict = field(default_factory=dict)


def evaluate(samples: list[Sample], serial_saw_ip: bool | None) -> tuple[str, list[Check]]:
    checks: list[Check] = []

    if len(samples) < MIN_SAMPLES:
        checks.append(Check(
            "A0", "Enough samples to decide?", "INCONCLUSIVE",
            f"only {len(samples)} diagnostic samples; need >= {MIN_SAMPLES}. "
            "Run longer, or check that CONFIG_CSI_DIAG_ENABLE is set in the image."))
        return "INCONCLUSIVE", checks

    ts = [s.t_host - samples[0].t_host for s in samples]
    span_s = ts[-1]

    # ---- A7 first: a reboot invalidates every other trend ----
    reboots = sum(1 for a, b in zip(samples, samples[1:]) if b.uptime_s < a.uptime_s)
    checks.append(Check(
        "A7", "Does it stay up?",
        "PASS" if reboots == 0 else "FAIL",
        f"{reboots} reboot(s) detected over {span_s:.0f}s "
        f"(uptime {samples[0].uptime_s}s -> {samples[-1].uptime_s}s)"
        + ("" if reboots == 0 else ". Trends below are not meaningful across a reboot."),
        {"reboots": reboots, "span_s": round(span_s, 1),
         "uptime_first_s": samples[0].uptime_s, "uptime_last_s": samples[-1].uptime_s}))

    # ---- A1 associated ----
    udp_flags = [s.wifi_connected for s in samples if s.wifi_connected is not None]
    if udp_flags:
        frac = sum(1 for f in udp_flags if f) / len(udp_flags)
        checks.append(Check(
            "A1", "Associated to the AP?",
            "PASS" if frac > 0.95 else ("WARN" if frac > 0.5 else "FAIL"),
            f"wifi_connected true in {frac * 100:.1f}% of samples",
            {"connected_fraction": round(frac, 4)}))
    elif serial_saw_ip is not None:
        # Over serial there is no flag; receiving diag lines at all plus an
        # observed IP_EVENT_STA_GOT_IP is the available evidence.
        checks.append(Check(
            "A1", "Associated to the AP?",
            "PASS" if serial_saw_ip else "WARN",
            "'Got IP' seen in console output" if serial_saw_ip
            else "no 'Got IP' line seen; node may be streaming diagnostics "
                 "without network association",
            {"got_ip_line_seen": serial_saw_ip}))

    # ---- A4 sustained CSI rate ----
    rates = [s.rate_hz for s in samples[1:]]  # first window is priming
    med = quantile(rates, 0.5)
    p10, p90 = quantile(rates, 0.10), quantile(rates, 0.90)
    iqr = quantile(rates, 0.75) - quantile(rates, 0.25)
    jitter = (iqr / med) if med > 0 else float("inf")
    if med <= 0:
        v, d = "FAIL", "no CSI callbacks at all -- the radio is not delivering frames"
    elif med < RATE_USABLE_HZ:
        v = "FAIL"
        d = (f"sustained {med:.2f} Hz is below the {RATE_USABLE_HZ:.0f} Hz floor; "
             f"Nyquist ceiling {med / 2:.2f} Hz")
    elif jitter > RATE_JITTER_WARN:
        v = "WARN"
        d = (f"sustained {med:.2f} Hz but jittery (IQR/median {jitter:.2f}); "
             f"Nyquist ceiling {med / 2:.2f} Hz")
    else:
        v = "PASS"
        d = f"sustained {med:.2f} Hz; Nyquist ceiling {med / 2:.2f} Hz"
    checks.append(Check("A4", "What CSI rate does the radio sustain?", v, d, {
        "median_hz": round(med, 3), "p10_hz": round(p10, 3), "p90_hz": round(p90, 3),
        "iqr_over_median": round(jitter, 3) if med > 0 else None,
        "nyquist_ceiling_hz": round(med / 2, 3),
        "cb_total_delta": samples[-1].cb_total - samples[0].cb_total,
        "drop_total_delta": samples[-1].drop_total - samples[0].drop_total,
    }))

    # ---- A2 heap headroom ----
    min_free = min(s.min_free_heap for s in samples)
    free_slope = lsq_slope(ts, [float(s.free_heap) for s in samples])
    # min_free_heap is monotonic non-increasing by construction; "settled"
    # means it stopped falling during the second half of the run.
    half = len(samples) // 2
    settled = samples[half].min_free_heap == samples[-1].min_free_heap
    if min_free < MIN_FREE_HEAP_BYTES:
        v = "FAIL"
        d = (f"low-water mark {min_free} B is under the {MIN_FREE_HEAP_BYTES} B "
             f"floor -- no headroom for the DSP working set")
    elif not settled:
        v = "WARN"
        d = (f"low-water mark {min_free} B but still falling in the second half "
             f"of the run -- run longer before trusting it")
    else:
        v = "PASS"
        d = f"low-water mark {min_free} B, settled; free-heap slope {free_slope:+.1f} B/s"
    checks.append(Check("A2", "Does the working set fit in DRAM?", v, d, {
        "startup_free_heap": samples[0].free_heap,
        "final_free_heap": samples[-1].free_heap,
        "min_free_heap": min_free,
        "free_heap_slope_bytes_per_s": round(free_slope, 3),
        "min_free_settled": settled,
    }))

    # ---- A3 fragmentation ----
    blk_slope = lsq_slope(ts, [float(s.largest_block) for s in samples])
    last = samples[-1]
    ratio = (last.largest_block / last.free_heap) if last.free_heap else 0.0
    if ratio < MIN_LARGEST_BLOCK_RATIO:
        v = "FAIL"
        d = (f"largest block is only {ratio * 100:.1f}% of free heap -- "
             f"heap is fragmented")
    elif blk_slope < FRAG_SLOPE_FAIL_BPS:
        v = "FAIL"
        d = f"largest block shrinking at {blk_slope:.1f} B/s -- progressive fragmentation"
    elif blk_slope < 0:
        v = "WARN"
        d = f"largest block drifting down at {blk_slope:.1f} B/s -- watch over a longer soak"
    else:
        v = "PASS"
        d = (f"largest block {last.largest_block} B "
             f"({ratio * 100:.1f}% of free heap), slope {blk_slope:+.1f} B/s")
    checks.append(Check("A3", "Is the heap fragmenting?", v, d, {
        "largest_block_final": last.largest_block,
        "largest_block_slope_bytes_per_s": round(blk_slope, 3),
        "largest_block_over_free_heap": round(ratio, 4),
    }))

    # ---- A5 frame lengths ----
    lengths: dict[int, int] = {}
    for s in samples:
        if s.last_len:
            lengths[s.last_len] = lengths.get(s.last_len, 0) + 1
    # These are wifi_csi_info_t.len -- the raw CSI payload straight from the
    # driver, per the ESP-IDF Wi-Fi guide's LTF table. NOT the ADR-018 UDP
    # frame size, which is this plus the 20-byte header (so 128 -> 148 on the
    # wire). A "non-standard" verdict here usually means something is reading
    # the wrong field rather than that the radio did something exotic.
    known = {128: "LLTF only (non-HT)", 256: "LLTF+HT-LTF",
             384: "LLTF+HT-LTF+STBC-HT-LTF"}
    if not lengths:
        v, d = "FAIL", "no frame lengths observed"
    else:
        dominant = max(lengths, key=lambda k: lengths[k])
        if dominant in known:
            v = "PASS"
            d = (f"dominant raw CSI length {dominant} B ({known[dominant]}), "
                 f"{len(lengths)} distinct length(s)")
        else:
            v = "WARN"
            d = (f"dominant raw CSI length {dominant} B is not a documented LTF "
                 f"combination (expected 128/256/384). If it is 20 more than one "
                 f"of those, an ADR-018 frame size is being reported instead of "
                 f"wifi_csi_info_t.len")
    checks.append(Check("A5", "Which LTF fields does the link deliver?", v, d, {
        "length_histogram": dict(sorted(lengths.items())),
        "subcarriers_last": last.subcarriers,
    }))

    # ---- A6 first_word_invalid ----
    fwi_delta = samples[-1].fwi_total - samples[0].fwi_total
    cb_delta = samples[-1].cb_total - samples[0].cb_total
    frac = (fwi_delta / cb_delta) if cb_delta > 0 else 0.0
    skip = samples[-1].skip
    consistent = (skip == 2) if fwi_delta > 0 else (skip == 0)
    checks.append(Check(
        "A6", "Does the hardware flag invalid leading bins?",
        "PASS" if consistent else "FAIL",
        f"first_word_invalid on {frac * 100:.1f}% of frames "
        f"({fwi_delta}/{cb_delta}); DSP excluding {skip} bin(s)"
        + ("" if consistent else
           " -- INCONSISTENT: skip should be 2 when the flag is ever seen, else 0"),
        {"fwi_delta": fwi_delta, "cb_delta": cb_delta,
         "fwi_fraction": round(frac, 4), "skipped_subcarriers": skip}))

    # ---- overall ----
    if any(c.verdict == "FAIL" for c in checks):
        overall = "FAIL"
    elif any(c.verdict == "INCONCLUSIVE" for c in checks):
        overall = "INCONCLUSIVE"
    else:
        overall = "PASS"
    return overall, checks


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def collect_serial(port: str, baud: int, duration: float,
                   raw_log) -> tuple[list[Sample], bool]:
    import serial  # pyserial; ships with esptool
    samples: list[Sample] = []
    saw_ip = False
    print(f"reading {port} at {baud} for {duration:.0f}s -- Ctrl-C to stop early\n")
    with serial.Serial(port, baud, timeout=1.0) as ser:
        end = time.time() + duration
        while time.time() < end:
            try:
                raw = ser.readline()
            except Exception as exc:                      # noqa: BLE001
                print(f"serial read error: {exc}", file=sys.stderr)
                break
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").rstrip()
            if raw_log:
                raw_log.write(line + "\n")
                raw_log.flush()
            if "Got IP" in line:
                saw_ip = True
                print(f"  {line}")
            s = parse_serial_line(line, time.time())
            if s is not None:
                samples.append(s)
                if len(samples) % 10 == 1:
                    print(f"  [{len(samples):4d}] rate={s.rate_hz:5.1f}Hz "
                          f"len={s.last_len} fwi={s.fwi_total} "
                          f"heap={s.free_heap} min={s.min_free_heap} "
                          f"blk={s.largest_block}")
            elif any(k in line for k in ("E (", "abort", "Guru Meditation",
                                         "rst:", "panic")):
                print(f"  ! {line}")
    return samples, saw_ip


def collect_udp(port: int, bind: str, duration: float,
                raw_log) -> tuple[list[Sample], None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.settimeout(1.0)
    samples: list[Sample] = []
    print(f"listening on {bind}:{port} for {duration:.0f}s -- Ctrl-C to stop early\n")
    end = time.time() + duration
    try:
        while time.time() < end:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            s = parse_diag_packet(data, time.time())
            if s is None:
                continue
            samples.append(s)
            if raw_log:
                raw_log.write(json.dumps(asdict(s)) + "\n")
                raw_log.flush()
            if len(samples) % 10 == 1:
                print(f"  [{len(samples):4d}] rate={s.rate_hz:5.1f}Hz "
                      f"len={s.last_len} fwi={s.fwi_total} "
                      f"heap={s.free_heap} min={s.min_free_heap} "
                      f"blk={s.largest_block}")
    finally:
        sock.close()
    return samples, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage A gate: measure ESP32 runtime diagnostics and decide "
                    "whether the node is fit for sensing experiments.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="PORT",
                     help="serial port to read the csi_diag console line from")
    src.add_argument("--udp", action="store_true",
                     help="listen for csi_diag UDP packets instead")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--port", type=int, default=5005, help="UDP port (--udp)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--duration", type=float, default=600,
                    help="seconds to collect (default: 600)")
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--raw", help="also write every raw line/sample here")
    args = ap.parse_args()

    raw_log = None
    if args.raw:
        Path(args.raw).parent.mkdir(parents=True, exist_ok=True)
        raw_log = open(args.raw, "w", encoding="utf-8")

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    try:
        if args.serial:
            samples, saw_ip = collect_serial(args.serial, args.baud,
                                             args.duration, raw_log)
        else:
            samples, saw_ip = collect_udp(args.port, args.bind,
                                          args.duration, raw_log)
    except KeyboardInterrupt:
        print("\ninterrupted -- evaluating what was collected")
        samples, saw_ip = [], None
    finally:
        if raw_log:
            raw_log.close()

    overall, checks = evaluate(samples, saw_ip)

    print("\n" + "=" * 72)
    print(f"STAGE A GATE: {overall}")
    print("=" * 72)
    for c in checks:
        print(f"  [{c.verdict:<12}] {c.id}  {c.question}")
        print(f"                 {c.detail}")
    print("=" * 72)
    if overall == "PASS":
        print("Proceed to Stage B (ESP32_PHASE3_VALIDATION.md section 4).")
    elif overall == "FAIL":
        print("Do NOT proceed to sensing runs. Diagnose the failing check first;\n"
              "presence results collected on a node in this state are not evidence.")
    else:
        print("Collect more samples before deciding.")

    report = {
        "tool": "tools/stage-a-gate.py",
        "started_utc": started,
        "elapsed_s": round(time.time() - t0, 1),
        "source": f"serial:{args.serial}" if args.serial else f"udp:{args.port}",
        "sample_count": len(samples),
        "overall": overall,
        "evidence": "MEASURED" if samples else "NONE",
        "criteria": {
            "min_samples": MIN_SAMPLES,
            "min_free_heap_bytes": MIN_FREE_HEAP_BYTES,
            "min_largest_block_ratio": MIN_LARGEST_BLOCK_RATIO,
            "frag_slope_fail_bytes_per_s": FRAG_SLOPE_FAIL_BPS,
            "rate_usable_hz": RATE_USABLE_HZ,
            "rate_jitter_warn": RATE_JITTER_WARN,
        },
        "checks": [asdict(c) for c in checks],
        "first_sample": asdict(samples[0]) if samples else None,
        "last_sample": asdict(samples[-1]) if samples else None,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.out}")

    return {"PASS": 0, "FAIL": 1}.get(overall, 2)


if __name__ == "__main__":
    sys.exit(main())
