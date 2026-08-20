#!/usr/bin/env python3
"""
Stage C analysis -- compare labelled CSI recordings and report separation.

Deliberately written BEFORE any recordings exist. The comparisons, the metric,
and the reporting format are fixed in advance so that the analysis cannot be
reshaped after seeing the data to make the detector look better than it is
(ESP32_PHASE3_VALIDATION.md section 5).

There is no threshold anywhere in this file. The output is ROC AUC -- a
threshold-free measure of how separable two distributions are. A detector is
built from a threshold only after the separation is known to exist.

What it computes, per recording:

  * per-subcarrier amplitude variance in a sliding window
  * a scalar activity metric: summed variance over the top-K most variable
    subcarriers, which is what edge_processing's presence heuristic keys on
  * amplitude mean and drift over the run
  * which subcarriers carry the variance (are they consistent between runs?)

Bins the hardware flagged via first_word_invalid are excluded -- csi-capture.py
records them as null, and this tool skips them rather than substituting a zero.

Usage:
    # Summarise every recording in a directory
    python tools/csi-analyse.py data/phase3

    # The pre-registered comparisons
    python tools/csi-analyse.py data/phase3 --compare

    # Machine-readable
    python tools/csi-analyse.py data/phase3 --compare --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

CSI_MAGIC = 0xC5110001
CSI_HEADER_SIZE = 20
FLAG_FIRST_WORD_INVALID = 1 << 5
FIRST_WORD_INVALID_BINS = 2

RVCSI_MAGIC = b"RVCSI\x00"
RECORD_HEADER = struct.Struct("<QH")

#: Sliding window for variance, in seconds. Two scales: a short one that
#: responds to movement and a long one that responds to occupancy.
WINDOWS_S = (2.0, 10.0)

#: How many subcarriers the activity metric sums over. Matches the firmware's
#: default top_k_count so the offline metric and the on-device one agree.
TOP_K = 8

#: The comparisons that must be reported, in this order. The two negative
#: controls are not optional and are marked so they cannot be quietly dropped
#: from a summary.
PRE_REGISTERED = [
    ("empty-baseline", "walking-continuous", "Does walking separate from empty?", False),
    ("empty-baseline", "stationary-seated", "Does a SEATED STILL person separate? (the real test)", False),
    ("empty-baseline", "stationary-standing", "Does a standing still person separate?", False),
    ("empty-baseline", "enter-exit", "Are transitions detectable?", False),
    ("walking-continuous", "two-people", "Does one person differ from two?", False),
    ("empty-baseline", "empty-with-fan", "NEGATIVE CONTROL: does a fan look like a person?", True),
    ("empty-baseline", "empty-door-open", "NEGATIVE CONTROL: does a door look like a person?", True),
]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

@dataclass
class Frame:
    t_s: float
    amps: list[float | None]
    rssi: int
    fwi: bool


def read_recording(path: Path) -> tuple[dict, list[Frame]]:
    frames: list[Frame] = []
    with open(path, "rb") as fh:
        if fh.read(len(RVCSI_MAGIC)) != RVCSI_MAGIC:
            raise ValueError(f"{path.name}: not an .rvcsi file")
        _ver, blob_len = struct.unpack("<HI", fh.read(6))
        meta = json.loads(fh.read(blob_len).decode())

        t0 = None
        while True:
            head = fh.read(RECORD_HEADER.size)
            if len(head) < RECORD_HEADER.size:
                break
            t_ns, length = RECORD_HEADER.unpack(head)
            payload = fh.read(length)
            if len(payload) < length:
                break
            if len(payload) < CSI_HEADER_SIZE:
                continue
            if struct.unpack_from("<I", payload, 0)[0] != CSI_MAGIC:
                continue  # diag or sibling packet
            flags = payload[19]
            rssi = struct.unpack_from("b", payload, 16)[0]
            fwi = bool(flags & FLAG_FIRST_WORD_INVALID)
            iq = payload[CSI_HEADER_SIZE:]
            skip = FIRST_WORD_INVALID_BINS if fwi else 0
            amps: list[float | None] = []
            for i in range(0, len(iq) - 1, 2):
                if (i // 2) < skip:
                    amps.append(None)
                    continue
                imag = struct.unpack_from("b", iq, i)[0]
                real = struct.unpack_from("b", iq, i + 1)[0]
                amps.append(math.hypot(real, imag))
            if t0 is None:
                t0 = t_ns
            frames.append(Frame((t_ns - t0) / 1e9, amps, rssi, fwi))
    return meta, frames


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def activity_series(frames: list[Frame], window_s: float) -> list[float]:
    """
    Summed variance over the TOP_K most variable subcarriers, per window.

    This is the offline twin of what edge_processing computes on-device. One
    value per non-overlapping window, so the values are independent samples --
    which matters, because ROC AUC on autocorrelated overlapping windows would
    overstate confidence.
    """
    if not frames:
        return []
    n_sc = max(len(f.amps) for f in frames)
    out: list[float] = []
    start = 0
    while start < len(frames):
        t_end = frames[start].t_s + window_s
        end = start
        while end < len(frames) and frames[end].t_s < t_end:
            end += 1
        chunk = frames[start:end]
        start = end if end > start else start + 1
        if len(chunk) < 4:            # too few frames to estimate variance
            continue
        per_sc: list[float] = []
        for sc in range(n_sc):
            vals = [f.amps[sc] for f in chunk
                    if sc < len(f.amps) and f.amps[sc] is not None]
            if len(vals) >= 4:
                per_sc.append(statistics.variance(vals))
        if not per_sc:
            continue
        per_sc.sort(reverse=True)
        out.append(sum(per_sc[:TOP_K]))
    return out


def variance_by_subcarrier(frames: list[Frame]) -> list[float | None]:
    if not frames:
        return []
    n_sc = max(len(f.amps) for f in frames)
    res: list[float | None] = []
    for sc in range(n_sc):
        vals = [f.amps[sc] for f in frames
                if sc < len(f.amps) and f.amps[sc] is not None]
        res.append(statistics.variance(vals) if len(vals) >= 4 else None)
    return res


def roc_auc(neg: list[float], pos: list[float]) -> float | None:
    """
    Threshold-free separation, via the Mann-Whitney U identity. Ties count half.

    0.5 = indistinguishable. 1.0 = perfectly separable with pos above neg.
    Below 0.5 means pos scores LOWER than neg, which for an activity metric
    is itself a finding worth seeing rather than hiding behind abs().
    """
    if not neg or not pos:
        return None
    merged = sorted([(v, 0) for v in neg] + [(v, 1) for v in pos])
    # average ranks over ties
    ranks: list[float] = [0.0] * len(merged)
    i = 0
    while i < len(merged):
        j = i
        while j + 1 < len(merged) and merged[j + 1][0] == merged[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    r_pos = sum(r for r, (_, lab) in zip(ranks, merged) if lab == 1)
    n_pos, n_neg = len(pos), len(neg)
    u = r_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def interpret_auc(auc: float | None) -> str:
    if auc is None:
        return "no data"
    d = abs(auc - 0.5) * 2
    if d < 0.10:
        return "indistinguishable"
    if d < 0.30:
        return "weak"
    if d < 0.60:
        return "moderate"
    if d < 0.85:
        return "strong"
    return "near-perfect"


# --------------------------------------------------------------------------

def label_of(meta: dict, path: Path) -> str:
    return meta.get("label") or path.stem.rsplit("-", 1)[0]


def summarise(path: Path) -> dict:
    meta, frames = read_recording(path)
    label = label_of(meta, path)
    if not frames:
        return {"file": path.name, "label": label, "csi_frames": 0,
                "error": "no CSI frames in recording"}

    span = frames[-1].t_s - frames[0].t_s
    fwi_n = sum(1 for f in frames if f.fwi)
    lengths: dict[int, int] = {}
    for f in frames:
        lengths[len(f.amps)] = lengths.get(len(f.amps), 0) + 1

    var_sc = variance_by_subcarrier(frames)
    ranked = sorted(((v, i) for i, v in enumerate(var_sc) if v is not None),
                    reverse=True)

    # Drift: does mean amplitude wander over the run? Environmental change
    # rather than activity.
    half = len(frames) // 2
    def mean_amp(fs):
        vals = [a for f in fs for a in f.amps if a is not None]
        return statistics.fmean(vals) if vals else float("nan")
    amp_first, amp_second = mean_amp(frames[:half]), mean_amp(frames[half:])

    out = {
        "file": path.name,
        "label": label,
        "note": meta.get("note", ""),
        "started_utc": meta.get("started_utc"),
        "csi_frames": len(frames),
        "duration_s": round(span, 1),
        "observed_csi_rate_hz": round(len(frames) / span, 2) if span > 0 else None,
        "subcarrier_counts": dict(sorted(lengths.items())),
        "first_word_invalid_frames": fwi_n,
        "first_word_invalid_fraction": round(fwi_n / len(frames), 4),
        "rssi_mean": round(statistics.fmean(f.rssi for f in frames), 1),
        "rssi_stdev": round(statistics.stdev([f.rssi for f in frames]), 2)
                      if len(frames) > 1 else 0.0,
        "amp_mean_first_half": round(amp_first, 3),
        "amp_mean_second_half": round(amp_second, 3),
        "amp_drift_pct": round((amp_second - amp_first) / amp_first * 100, 2)
                         if amp_first else None,
        "top_variance_subcarriers": [i for _, i in ranked[:TOP_K]],
        "activity": {},
    }
    for w in WINDOWS_S:
        series = activity_series(frames, w)
        out["activity"][f"{w:g}s"] = {
            "n_windows": len(series),
            "median": round(statistics.median(series), 4) if series else None,
            "p10": round(sorted(series)[int(0.1 * (len(series) - 1))], 4) if series else None,
            "p90": round(sorted(series)[int(0.9 * (len(series) - 1))], 4) if series else None,
            "_series": series,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage C: compare labelled CSI recordings.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("directory", help="directory of .rvcsi recordings")
    ap.add_argument("--compare", action="store_true",
                    help="run the pre-registered comparisons")
    ap.add_argument("--window", type=float, default=2.0,
                    help=f"window for comparisons, one of {WINDOWS_S} (default: 2.0)")
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args()

    files = sorted(Path(args.directory).glob("*.rvcsi"))
    if not files:
        print(f"no .rvcsi recordings in {args.directory}", file=sys.stderr)
        return 2

    summaries = []
    print(f"{'label':<22}{'frames':>8}{'dur_s':>8}{'rate':>7}{'fwi%':>7}"
          f"{'rssi':>7}{'drift%':>8}{'act_med(2s)':>13}")
    print("-" * 82)
    for f in files:
        try:
            s = summarise(f)
        except Exception as exc:                          # noqa: BLE001
            print(f"{f.name}: {exc}", file=sys.stderr)
            continue
        summaries.append(s)
        if s.get("csi_frames"):
            act = s["activity"].get("2s", {}).get("median")
            print(f"{s['label']:<22}{s['csi_frames']:>8}{s['duration_s']:>8.0f}"
                  f"{s['observed_csi_rate_hz'] or 0:>7.1f}"
                  f"{s['first_word_invalid_fraction'] * 100:>7.1f}"
                  f"{s['rssi_mean']:>7.0f}"
                  f"{s['amp_drift_pct'] if s['amp_drift_pct'] is not None else 0:>8.1f}"
                  f"{act if act is not None else 0:>13.3f}")
        else:
            print(f"{s['label']:<22}{'--':>8}  {s.get('error', '')}")

    report = {"tool": "tools/csi-analyse.py", "evidence": "MEASURED",
              "window_s": args.window, "top_k": TOP_K,
              "recordings": summaries, "comparisons": []}

    if args.compare:
        key = f"{args.window:g}s"
        by_label: dict[str, list[float]] = {}
        for s in summaries:
            if s.get("csi_frames"):
                by_label.setdefault(s["label"], []).extend(
                    s["activity"].get(key, {}).get("_series", []))

        print(f"\nPre-registered comparisons  (activity metric, {key} windows)")
        print("-" * 82)
        for neg_l, pos_l, question, is_control in PRE_REGISTERED:
            neg, pos = by_label.get(neg_l), by_label.get(pos_l)
            if not neg or not pos:
                missing = [n for n, v in ((neg_l, neg), (pos_l, pos)) if not v]
                print(f"  [ MISSING  ] {question}")
                print(f"               no data for: {', '.join(missing)}")
                report["comparisons"].append({
                    "negative": neg_l, "positive": pos_l, "question": question,
                    "is_negative_control": is_control, "auc": None,
                    "status": "MISSING", "missing": missing})
                continue
            auc = roc_auc(neg, pos)
            tag = "CONTROL" if is_control else "        "
            print(f"  [{tag}  ] {question}")
            print(f"               {neg_l} (n={len(neg)}) vs {pos_l} (n={len(pos)})"
                  f"  AUC={auc:.3f}  -> {interpret_auc(auc)}")
            report["comparisons"].append({
                "negative": neg_l, "positive": pos_l, "question": question,
                "is_negative_control": is_control,
                "n_negative": len(neg), "n_positive": len(pos),
                "auc": round(auc, 4), "interpretation": interpret_auc(auc),
                "status": "OK"})

        # The honesty check the whole phase exists for.
        walking = next((c for c in report["comparisons"]
                        if c["positive"] == "walking-continuous"
                        and c["status"] == "OK"), None)
        controls = [c for c in report["comparisons"]
                    if c["is_negative_control"] and c["status"] == "OK"]
        print("\nInterpretation")
        print("-" * 82)
        if walking and controls:
            w = abs(walking["auc"] - 0.5)
            worst = max(controls, key=lambda c: abs(c["auc"] - 0.5))
            c = abs(worst["auc"] - 0.5)
            if w < 0.10:
                verdict = ("Outcome E/B: walking barely separates from empty. "
                           "Check the Stage A rate before drawing sensing conclusions.")
            elif c >= 0.7 * w:
                verdict = (f"Outcome C/D: the negative control '{worst['positive']}' "
                           f"separates about as strongly as walking "
                           f"({worst['auc']:.3f} vs {walking['auc']:.3f}). This is an "
                           f"RF-CHANGE detector, not a presence detector. Label it "
                           f"'activity', not 'person'.")
            else:
                verdict = (f"Walking separates ({walking['auc']:.3f}) more strongly "
                           f"than the strongest negative control "
                           f"({worst['auc']:.3f}). Necessary but not sufficient -- "
                           f"check the seated-still comparison before claiming "
                           f"presence rather than motion.")
            print("  " + verdict)
            report["verdict"] = verdict
        else:
            print("  Not enough runs to interpret. The negative controls "
                  "(fan, door) are mandatory.")
            report["verdict"] = "incomplete: mandatory runs missing"

    if args.json:
        for s in report["recordings"]:
            for w in s.get("activity", {}).values():
                w.pop("_series", None)
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
