#!/usr/bin/env python3
"""
CSI capture, logging and replay for the original-ESP32 sensing node.

Replaces scripts/record-csi-udp.py for this work. That script decodes an
8-byte ADR-018 header where the format specifies 20 (see
ESP32_ORIGINAL_COMPATIBILITY.md section 7.2): it reads RSSI from the low byte
of the subcarrier count, invents a `channel` field that is not on the wire,
and treats 12 bytes of header as I/Q -- shifting every subcarrier by six
positions. Anything recorded with it is wrong, so this is a new file rather
than an edit, and the upstream script is left alone for a separate fix.

Design notes:

  * Raw first. Every packet is written to the .rvcsi log byte-for-byte as it
    arrived, length-prefixed and timestamped. The JSONL sidecar is derived and
    disposable. That ordering is what makes a recording re-analysable after the
    feature extraction changes, which is the whole point of Phase 3 replay.

  * Measurement and inference are kept apart. This tool decodes and records.
    It computes subcarrier amplitudes because that is arithmetic on the bytes,
    and it honours the first_word_invalid flag by marking the affected bins
    null rather than emitting a plausible-looking number for hardware garbage.
    It does not decide whether anyone is present.

Usage:
    # Record a labelled 30-minute baseline
    python tools/csi-capture.py record --duration 1800 \\
        --label empty-baseline --out data/phase3

    # Inspect the live stream without writing anything
    python tools/csi-capture.py monitor

    # Replay a recording at original timing to a local analysis process
    python tools/csi-capture.py replay data/phase3/empty-baseline-....rvcsi \\
        --host 127.0.0.1 --port 5500

    # Summarise a recording
    python tools/csi-capture.py info data/phase3/empty-baseline-....rvcsi
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

# Console output is kept pure ASCII, but a Windows console defaulting to cp1252
# will still raise UnicodeEncodeError on anything that slips through (an
# operator-supplied --label, for instance). A capture tool must not lose a
# recording to a print statement, so degrade instead of dying.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Wire formats
# ---------------------------------------------------------------------------

CSI_MAGIC = 0xC5110001
CSI_HEADER_SIZE = 20

# ADR-018 byte 19 flag bits -- must match main/csi_collector.h.
FLAG_BW40 = 1 << 0
FLAG_STBC = 1 << 2
FLAG_SYNC_VALID = 1 << 4
FLAG_FIRST_WORD_INVALID = 1 << 5
FIRST_WORD_INVALID_BINS = 2

# Fork-local diagnostics -- must match main/csi_diag.h.
DIAG_MAGIC = 0xC5111001
DIAG_PKT_SIZE = 60

# Upstream sibling packets shared on this UDP port. Recorded verbatim and
# counted, but not decoded here.
#
# NOTE: 0xC5110007 is allocated twice upstream -- ADR-040 WASM output in the
# firmware and ADR-095 temporal classification in the Rust host parser. Named
# to reflect the ambiguity rather than pretending it is resolved.
SIBLING_MAGICS = {
    0xC5110002: "ADR-039 edge vitals",
    0xC5110003: "ADR-069 feature vector",
    0xC5110004: "ADR-063 fused vitals",
    0xC5110005: "ADR-039 compressed CSI",
    0xC5110006: "ADR-081 feature state",
    0xC5110007: "ADR-040 WASM output / ADR-095 temporal (magic collision)",
}

# .rvcsi container: header, then repeating [u64 t_ns][u16 len][payload].
RVCSI_MAGIC = b"RVCSI\x00"
RVCSI_VERSION = 1
RECORD_HEADER = struct.Struct("<QH")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

@dataclass
class CsiFrame:
    """A decoded ADR-018 frame. Every field is read from the wire."""
    node_id: int
    n_antennas: int
    n_subcarriers: int
    freq_mhz: int
    sequence: int
    rssi: int
    noise_floor: int
    ppdu_type: int
    flags: int
    iq: bytes

    @property
    def first_word_invalid(self) -> bool:
        return bool(self.flags & FLAG_FIRST_WORD_INVALID)

    @property
    def bw40(self) -> bool:
        return bool(self.flags & FLAG_BW40)

    @property
    def channel(self) -> int | None:
        """Derive the 2.4 GHz channel. ADR-018 carries frequency, not channel."""
        f = self.freq_mhz
        if f == 2484:
            return 14
        if 2412 <= f <= 2472 and (f - 2412) % 5 == 0:
            return (f - 2412) // 5 + 1
        if 5000 <= f <= 5885 and f % 5 == 0:
            return (f - 5000) // 5
        return None

    def amplitudes(self) -> list[float | None]:
        """
        Per-subcarrier magnitude.

        ESP-IDF delivers pairs of int8 as (imaginary, real) -- magnitude is the
        same either way, but the ordering is documented here because the phase
        convention depends on it.

        Bins invalidated by first_word_invalid are None, not 0.0: a zero is a
        measurement ("no energy"), None is an absence of one. Collapsing the
        two would quietly feed hardware garbage into any downstream statistic.
        """
        out: list[float | None] = []
        skip = FIRST_WORD_INVALID_BINS if self.first_word_invalid else 0
        for i in range(0, len(self.iq) - 1, 2):
            bin_idx = i // 2
            if bin_idx < skip:
                out.append(None)
                continue
            imag = struct.unpack_from("b", self.iq, i)[0]
            real = struct.unpack_from("b", self.iq, i + 1)[0]
            out.append(round(math.hypot(real, imag), 3))
        return out


def decode_csi(data: bytes) -> CsiFrame | None:
    """Decode an ADR-018 frame, or None if this is not one."""
    if len(data) < CSI_HEADER_SIZE:
        return None
    (magic, node_id, n_ant, n_sub, freq, seq, rssi, noise, ppdu,
     flags) = struct.unpack_from("<IBBHIIbbBB", data, 0)
    if magic != CSI_MAGIC:
        return None
    return CsiFrame(
        node_id=node_id, n_antennas=n_ant, n_subcarriers=n_sub,
        freq_mhz=freq, sequence=seq, rssi=rssi, noise_floor=noise,
        ppdu_type=ppdu, flags=flags, iq=data[CSI_HEADER_SIZE:],
    )


DIAG_STRUCT = struct.Struct("<IBBH IIIII HHHH BBBB bbbb B3x III")


def decode_diag(data: bytes) -> dict | None:
    """Decode a fork-local csi_diag packet, or None if this is not one."""
    if len(data) < 4 or struct.unpack_from("<I", data, 0)[0] != DIAG_MAGIC:
        return None
    if len(data) < DIAG_PKT_SIZE:
        return {"type": "diag", "error": f"short packet: {len(data)}B"}
    f = DIAG_STRUCT.unpack_from(data, 0)
    return {
        "type": "diag",
        "node_id": f[1], "version": f[2], "interval_ms": f[3],
        "uptime_s": f[4], "cb_total": f[5], "early_drop_total": f[6],
        "send_ok_total": f[7], "send_fail_total": f[8],
        "csi_rate_hz": f[9] / 10.0,
        "last_len": f[10], "last_subcarriers": f[11], "fwi_total": f[12],
        "skipped_subcarriers": f[13], "phy_flags": f[14],
        "channel": f[15], "sig_mode": f[16],
        "rssi_mean": f[17], "rssi_min": f[18], "rssi_max": f[19],
        "noise_floor_mean": f[20],
        "wifi_connected": bool(f[21] & 0x1), "csi_active": bool(f[21] & 0x2),
        "free_heap": f[22], "min_free_heap": f[23],
        "largest_free_block": f[24],
    }


def classify(data: bytes) -> str:
    if len(data) < 4:
        return "runt"
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == CSI_MAGIC:
        return "csi"
    if magic == DIAG_MAGIC:
        return "diag"
    if magic in SIBLING_MAGICS:
        return "sibling"
    return "unknown"


# ---------------------------------------------------------------------------
# .rvcsi container
# ---------------------------------------------------------------------------

def write_rvcsi_header(fh: BinaryIO, meta: dict) -> None:
    blob = json.dumps(meta, separators=(",", ":")).encode()
    fh.write(RVCSI_MAGIC)
    fh.write(struct.pack("<HI", RVCSI_VERSION, len(blob)))
    fh.write(blob)


def read_rvcsi_header(fh: BinaryIO) -> dict:
    if fh.read(len(RVCSI_MAGIC)) != RVCSI_MAGIC:
        raise ValueError("not an .rvcsi file")
    version, blob_len = struct.unpack("<HI", fh.read(6))
    if version != RVCSI_VERSION:
        raise ValueError(f"unsupported .rvcsi version {version}")
    return json.loads(fh.read(blob_len).decode())


def iter_rvcsi(fh: BinaryIO) -> Iterator[tuple[int, bytes]]:
    """Yield (t_ns, payload). Stops cleanly on a truncated tail."""
    while True:
        head = fh.read(RECORD_HEADER.size)
        if len(head) < RECORD_HEADER.size:
            return
        t_ns, length = RECORD_HEADER.unpack(head)
        payload = fh.read(length)
        if len(payload) < length:
            print("warning: truncated final record (capture interrupted?)",
                  file=sys.stderr)
            return
        yield t_ns, payload


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class Counters:
    csi: int = 0
    diag: int = 0
    sibling: int = 0
    unknown: int = 0
    runt: int = 0
    bytes_total: int = 0
    fwi_frames: int = 0
    lengths: dict[int, int] = field(default_factory=dict)
    unknown_magics: dict[str, int] = field(default_factory=dict)

    def add(self, data: bytes) -> str:
        kind = classify(data)
        setattr(self, kind, getattr(self, kind) + 1)
        self.bytes_total += len(data)
        if kind == "csi":
            self.lengths[len(data)] = self.lengths.get(len(data), 0) + 1
            frame = decode_csi(data)
            if frame and frame.first_word_invalid:
                self.fwi_frames += 1
        elif kind == "unknown":
            magic = f"0x{struct.unpack_from('<I', data, 0)[0]:08X}"
            self.unknown_magics[magic] = self.unknown_magics.get(magic, 0) + 1
        return kind

    def summary(self) -> dict:
        return {
            "csi_frames": self.csi,
            "diag_packets": self.diag,
            "sibling_packets": self.sibling,
            "unknown_packets": self.unknown,
            "runt_packets": self.runt,
            "bytes_total": self.bytes_total,
            "csi_frames_first_word_invalid": self.fwi_frames,
            "csi_frame_sizes": dict(sorted(self.lengths.items())),
            "unknown_magics": self.unknown_magics,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def bind_socket(port: int, bind_addr: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    sock.bind((bind_addr, port))
    sock.settimeout(1.0)
    return sock


def enforce_retention(out_dir: Path, retain_days: float | None) -> None:
    """Delete recordings older than the retention window. Local logs stay local,
    and a capture rig that quietly accumulates months of RF observations of a
    home is not something to build by accident."""
    if not retain_days or retain_days <= 0:
        return
    cutoff = time.time() - retain_days * 86400
    for path in out_dir.glob("*.rvcsi"):
        try:
            if path.stat().st_mtime < cutoff:
                sidecar = path.with_suffix(".jsonl")
                path.unlink()
                if sidecar.exists():
                    sidecar.unlink()
                print(f"retention: removed {path.name}")
        except OSError as exc:
            print(f"retention: could not remove {path.name}: {exc}",
                  file=sys.stderr)


def cmd_record(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    enforce_retention(out_dir, args.retain_days)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-"
                         for c in args.label)
    base = out_dir / f"{safe_label}-{stamp}"
    raw_path, side_path = base.with_suffix(".rvcsi"), base.with_suffix(".jsonl")

    meta = {
        "label": args.label,
        "note": args.note,
        "started_utc": utc_now(),
        "port": args.port,
        "tool": "tools/csi-capture.py",
        "rvcsi_version": RVCSI_VERSION,
        # Ground truth is asserted by the operator, not measured by the tool.
        "evidence": "MEASURED (raw capture); label is operator-asserted",
    }

    sock = bind_socket(args.port, args.bind)
    counters = Counters()
    max_bytes = int(args.max_size_mb * 1024 * 1024) if args.max_size_mb else None
    deadline = time.time() + args.duration if args.duration else None
    stop_reason = "duration reached"

    print(f"recording  label={args.label!r}  port={args.port}  "
          f"duration={args.duration or 'unbounded'}s")
    print(f"  raw     {raw_path}")
    print(f"  sidecar {side_path}")
    print("  Ctrl-C to stop early.\n")

    t0 = time.time()
    last_report = t0
    try:
        with open(raw_path, "wb") as raw, open(side_path, "w",
                                               encoding="utf-8") as side:
            write_rvcsi_header(raw, meta)
            side.write(json.dumps({"type": "header", **meta}) + "\n")

            while True:
                if deadline and time.time() >= deadline:
                    break
                if max_bytes and counters.bytes_total >= max_bytes:
                    stop_reason = "size cap reached"
                    break
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue

                t_ns = time.time_ns()
                raw.write(RECORD_HEADER.pack(t_ns, len(data)))
                raw.write(data)

                kind = counters.add(data)
                if kind == "csi":
                    frame = decode_csi(data)
                    if frame is not None:
                        side.write(json.dumps({
                            "type": "csi", "ts_ns": t_ns, "ts_utc": utc_now(),
                            "node_id": frame.node_id,
                            "sequence": frame.sequence,
                            "n_subcarriers": frame.n_subcarriers,
                            "n_antennas": frame.n_antennas,
                            "freq_mhz": frame.freq_mhz,
                            "channel": frame.channel,
                            "rssi": frame.rssi,
                            "noise_floor": frame.noise_floor,
                            "ppdu_type": frame.ppdu_type,
                            "bw40": frame.bw40,
                            "first_word_invalid": frame.first_word_invalid,
                            "amplitudes": frame.amplitudes(),
                        }) + "\n")
                elif kind == "diag":
                    decoded = decode_diag(data)
                    if decoded is not None:
                        decoded.update({"ts_ns": t_ns, "ts_utc": utc_now()})
                        side.write(json.dumps(decoded) + "\n")

                now = time.time()
                if now - last_report >= 5.0:
                    rate = counters.csi / max(now - t0, 1e-9)
                    print(f"  t={now - t0:7.1f}s  csi={counters.csi:<7d} "
                          f"({rate:5.1f}/s)  diag={counters.diag:<4d} "
                          f"fwi={counters.fwi_frames:<6d} "
                          f"{counters.bytes_total / 1e6:6.2f} MB")
                    last_report = now
    except KeyboardInterrupt:
        stop_reason = "interrupted by operator"
    finally:
        sock.close()

    elapsed = time.time() - t0
    summary = counters.summary()
    summary.update({
        "label": args.label, "stop_reason": stop_reason,
        "elapsed_s": round(elapsed, 3),
        "csi_rate_hz": round(counters.csi / elapsed, 3) if elapsed > 0 else 0.0,
        "ended_utc": utc_now(),
    })
    with open(side_path, "a", encoding="utf-8") as side:
        side.write(json.dumps({"type": "summary", **summary}) + "\n")

    print(f"\nstopped: {stop_reason}")
    print(json.dumps(summary, indent=2))
    if counters.csi == 0:
        print("\nNo CSI frames received. Check that the node is powered, "
              "provisioned, and streaming to this host's IP on the right port.",
              file=sys.stderr)
        return 1
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    sock = bind_socket(args.port, args.bind)
    counters = Counters()
    print(f"monitoring port {args.port} -- Ctrl-C to stop\n")
    t0 = last = time.time()
    try:
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            kind = counters.add(data)
            if kind == "diag":
                d = decode_diag(data)
                if d and "error" not in d:
                    print(f"  [diag] up={d['uptime_s']}s "
                          f"rate={d['csi_rate_hz']:.1f}Hz "
                          f"len={d['last_len']}B sc={d['last_subcarriers']} "
                          f"skip={d['skipped_subcarriers']} "
                          f"fwi={d['fwi_total']} "
                          f"rssi={d['rssi_mean']} nf={d['noise_floor_mean']} "
                          f"heap={d['free_heap']} min={d['min_free_heap']} "
                          f"blk={d['largest_free_block']}")
            now = time.time()
            if now - last >= 2.0:
                print(f"  [rx] csi={counters.csi} ({counters.csi / max(now - t0, 1e-9):.1f}/s) "
                      f"diag={counters.diag} sibling={counters.sibling} "
                      f"unknown={counters.unknown} fwi={counters.fwi_frames}")
                last = now
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print("\n" + json.dumps(counters.summary(), indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.recording)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    sent = 0
    with open(path, "rb") as fh:
        meta = read_rvcsi_header(fh)
        print(f"replaying {path.name}  label={meta.get('label')!r} "
              f"-> {args.host}:{args.port}  speed={args.speed}x")
        prev_ns: int | None = None
        wall0 = time.perf_counter()
        first_ns: int | None = None
        for t_ns, payload in iter_rvcsi(fh):
            if first_ns is None:
                first_ns = t_ns
            if not args.fast and prev_ns is not None:
                # Pace against the recording's own clock rather than
                # accumulating per-packet sleep error.
                target_elapsed = (t_ns - first_ns) / 1e9 / args.speed
                drift = target_elapsed - (time.perf_counter() - wall0)
                if drift > 0:
                    time.sleep(drift)
            sock.sendto(payload, target)
            sent += 1
            prev_ns = t_ns
    sock.close()
    print(f"replayed {sent} packets")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    path = Path(args.recording)
    counters = Counters()
    first_ns = last_ns = None
    with open(path, "rb") as fh:
        meta = read_rvcsi_header(fh)
        for t_ns, payload in iter_rvcsi(fh):
            if first_ns is None:
                first_ns = t_ns
            last_ns = t_ns
            counters.add(payload)

    span = (last_ns - first_ns) / 1e9 if (first_ns and last_ns) else 0.0
    summary = counters.summary()
    summary["span_s"] = round(span, 3)
    summary["csi_rate_hz"] = round(counters.csi / span, 3) if span > 0 else 0.0
    print(json.dumps({"header": meta, "contents": summary}, indent=2))
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture, log and replay ESP32 CSI over UDP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=5500,
                       help="UDP port to listen on (default: 5500)")
        p.add_argument("--bind", default="0.0.0.0",
                       help="Address to bind (default: 0.0.0.0)")

    p_rec = sub.add_parser("record", help="record a labelled capture")
    add_common(p_rec)
    p_rec.add_argument("--label", required=True,
                       help="ground-truth label, e.g. 'empty-baseline'. "
                            "Operator-asserted, not measured.")
    p_rec.add_argument("--note", default="",
                       help="free-text note stored in the recording header")
    p_rec.add_argument("--duration", type=float, default=0,
                       help="seconds to record (0 = until Ctrl-C)")
    p_rec.add_argument("--out", default="data/csi-captures",
                       help="output directory")
    p_rec.add_argument("--max-size-mb", type=float, default=0,
                       help="stop after this many MB (0 = no cap)")
    p_rec.add_argument("--retain-days", type=float, default=0,
                       help="delete recordings in --out older than this "
                            "before starting (0 = keep everything)")
    p_rec.set_defaults(func=cmd_record)

    p_mon = sub.add_parser("monitor", help="watch the live stream, write nothing")
    add_common(p_mon)
    p_mon.set_defaults(func=cmd_monitor)

    p_rep = sub.add_parser("replay", help="re-emit a recording over UDP")
    p_rep.add_argument("recording")
    p_rep.add_argument("--host", default="127.0.0.1")
    p_rep.add_argument("--port", type=int, default=5500)
    p_rep.add_argument("--speed", type=float, default=1.0,
                       help="playback rate multiplier (default: 1.0)")
    p_rep.add_argument("--fast", action="store_true",
                       help="ignore timing, send as fast as possible")
    p_rep.set_defaults(func=cmd_replay)

    p_inf = sub.add_parser("info", help="summarise a recording")
    p_inf.add_argument("recording")
    p_inf.set_defaults(func=cmd_info)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
