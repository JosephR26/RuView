# DevKitC-32 hardware runbook

Exact commands for flashing, provisioning and running Stage A / Stage B on the
ESP32 DevKitC-32, on this Windows-on-ARM machine.

**Status of measurements in this repository: none yet.** No ESP32 has been
attached to this machine at any point — the only serial devices present are two
Qualcomm ACPI UARTs on COM1/COM2. Everything below is prepared and, where it
could be, verified without hardware. Every runtime number stays `CLAIMED` until
a real board produces it.

**Firmware provenance.** CI run
[32388159744](https://github.com/JosephR26/RuView/actions/runs/32388159744),
artifact `esp32-csi-node-firmware-esp32-4mb`, downloaded to `D:\josep\esp32-fw\`.

| File | SHA-256 | Verified |
|---|---|---|
| `bootloader.bin` | `f35df118…c188` | Chip ID 0 (ESP32), hash valid |
| `esp32-csi-node-esp32.bin` | `f602a51f…08bd` | Chip ID 0 (ESP32), hash valid, IDF v5.4, v0.8.4 |
| `partition-table-esp32.bin` | `4c2cc4ff…81f0` | decoded, matches `partitions_4mb.csv` |
| `ota_data_initial.bin` | `7d2c7ac4…c62f` | — |

Decoded partition table, confirming the flash offsets below:

```
nvs       data 0x02  0x00009000    24 KB
otadata   data 0x00  0x0000f000     8 KB
phy_init  data 0x01  0x00011000     4 KB
ota_0     app  0x10  0x00020000  1856 KB
ota_1     app  0x11  0x001f0000  1856 KB
```

---

## Step 1 — find the board

Plug the DevKitC-32 in with a **data** USB cable. Charge-only micro-USB cables
are extremely common and present no COM port at all; if nothing appears, try a
different cable before debugging anything else.

```bash
python -m serial.tools.list_ports -v
```

or, equivalently, in PowerShell:

```powershell
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.Name -match 'COM\d+' } |
  Select-Object Name, DeviceID, Status | Format-List
```

Right now, with no board attached, that reports exactly two ports:

```
COM1    desc: Qualcomm(R) UART Bus Device (COM1)    hwid: ACPI\QCOM0A16\6
COM2    desc: Qualcomm(R) UART Bus Device (COM2)    hwid: ACPI\QCOM0A16\8
```

Ignore both of those — they are the laptop's own ACPI UARTs, not your board.
You are looking for a **new, third** entry naming a USB-serial bridge:

- **CP2102 / CP2104** — "Silicon Labs CP210x USB to UART Bridge"
- **CH340 / CH9102** — "USB-SERIAL CH340" or "USB-Enhanced-SERIAL CH9102"
- **FTDI** — "USB Serial Port"

Note the `COM<N>`.

**If a device appears but with a warning (`Status` not `OK`, or a
`ConfigManagerErrorCode` other than 0), the driver is missing.** This is a real
risk on Windows-on-ARM. Check with:

```powershell
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.ConfigManagerErrorCode -ne 0 } |
  Select-Object Name, ConfigManagerErrorCode, PNPDeviceID | Format-Table -AutoSize
```

Silicon Labs and WCH both ship ARM64 Windows drivers; install the ARM64 build,
not the x64 one.

---

## Step 2 — identify the chip before writing anything

Read-only. Confirms the board, the chip revision, the flash size and the MAC
before any flash write happens.

```bash
python -m esptool --chip esp32 --port COM<N> flash-id
python -m esptool --chip esp32 --port COM<N> read-mac
```

Expected: `Chip is ESP32-D0WD-V3 (revision v3.x)` (or `-D0WDQ6`), a MAC address,
and `Detected flash size: 4MB`.

Three things to check against expectations:

- **Chip is ESP32**, not ESP32-S3/C3/C6. The image will refuse to boot otherwise.
- **Flash size 4MB.** If it reports 8MB or 16MB the image still works (the
  partition table only spans 4 MB) but you are wasting half the flash; say so
  and we can add an 8 MB partition variant.
- **MAC address** — record it. It is the node's identity in later logs.

If `flash-id` fails with a sync error, hold **BOOT** (sometimes labelled IO0),
tap **EN/RST**, release BOOT, and retry. Most DevKitC-32 boards auto-reset
correctly and do not need this.

---

## Step 3 — flash

**The bootloader goes at `0x1000` on the original ESP32.** On S3 and C6 it is
`0x0`; using `0x0` here produces a board that does not boot. The flag spellings
below are the hyphenated form esptool 5.3.1 expects (ESP-IDF prints the
underscore form, which is its own older bundled esptool).

This exact command was verified to parse end-to-end — it reached the point of
opening the serial port and failed only because no port existed.

```bash
python -m esptool --chip esp32 --port COM<N> --baud 460800 write-flash \
  --flash-mode dio --flash-size 4MB --flash-freq 40m \
  0x1000  /d/josep/esp32-fw/bootloader.bin \
  0x8000  /d/josep/esp32-fw/partition-table-esp32.bin \
  0xf000  /d/josep/esp32-fw/ota_data_initial.bin \
  0x20000 /d/josep/esp32-fw/esp32-csi-node-esp32.bin
```

PowerShell equivalent (backtick continuations):

```powershell
python -m esptool --chip esp32 --port COM<N> --baud 460800 write-flash `
  --flash-mode dio --flash-size 4MB --flash-freq 40m `
  0x1000  D:\josep\esp32-fw\bootloader.bin `
  0x8000  D:\josep\esp32-fw\partition-table-esp32.bin `
  0xf000  D:\josep\esp32-fw\ota_data_initial.bin `
  0x20000 D:\josep\esp32-fw\esp32-csi-node-esp32.bin
```

If the board has been flashed with something else before, add `--erase-all` once
to clear stale NVS. Note that this also erases any provisioning.

Expect four `Hash of data verified.` lines and `Hard resetting via RTS pin...`.

### First boot

Watch the console at **115200 8N1**. pyserial ships with esptool, so its
terminal is already installed — nothing extra to fetch, and no ESP-IDF needed:

```bash
python -m serial.tools.miniterm COM<N> 115200
```

Quit with `Ctrl-]`. Note that the serial port is exclusive: close miniterm
before running `esptool` or `provision.py`, or they will fail to open the port.

Expected within a few seconds of reset:

```
ESP32 CSI Node (ADR-018 / ADR-110) - v0.8.4 - Node ID: 1
No addressable onboard LED on this target - skipping WS2812 init
WiFi STA initialized, connecting to SSID: ...
```

The LED line is the GPIO-48 fix confirming it took effect. Wi-Fi will fail until
Step 4 — that is expected on a freshly flashed board.

---

## Step 4 — provision

Credentials go into NVS over serial. **Do not** put them in a file in this
repository, and be aware that a shell history keeps them too.

```bash
python firmware/esp32-csi-node/provision.py --port COM<N> --chip esp32 \
  --ssid "<YOUR_SSID>" --password "<YOUR_PASSWORD>" \
  --target-ip <YOUR_PC_IP> --target-port 5005 --node-id 1
```

Find `<YOUR_PC_IP>` — the address the node streams to — with:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
  Select-Object IPAddress, InterfaceAlias
```

It must be the interface on the **same subnet as the AP the node joins**. The
node must be on 2.4 GHz — it has no 5 GHz radio.

Then let the firewall accept the stream. Run once, elevated:

```powershell
New-NetFirewallRule -DisplayName "RuView CSI UDP 5005" -Direction Inbound `
  -Protocol UDP -LocalPort 5005 -Action Allow -Profile Private
```

A blocked UDP port looks exactly like a dead node: the board's serial log shows
CSI flowing, and the host receives nothing. Check this before suspecting the
firmware.

After a reset, expect `Got IP: 192.168.x.y` and then per-second `csi_diag:`
lines.

---

## Step 5 — Stage A gate

This is a gate, not a warm-up. If it fails, sensing runs are not evidence.

First boot, over serial, 10 minutes:

```bash
python tools/stage-a-gate.py --serial COM<N> --duration 600 \
  --out data/phase3/stageA-first-boot.json \
  --raw data/phase3/stageA-first-boot.log
```

Then a one-hour soak over the network (the node keeps running; no reflash):

```bash
python tools/stage-a-gate.py --udp --duration 3600 \
  --out data/phase3/stageA-soak.json
```

And, before trusting anything overnight, the 24-hour A7 check:

```bash
python tools/stage-a-gate.py --udp --duration 86400 \
  --out data/phase3/stageA-24h.json
```

The tool prints a verdict per check and exits 0 (PASS) / 1 (FAIL) /
2 (INCONCLUSIVE). Criteria are fixed in the source, above the code, so they
cannot be quietly relaxed to let a marginal board through:

| Check | Fails when |
|---|---|
| A1 associated | `wifi_connected` false in >50% of samples |
| A2 heap | min-free-heap below 40 KB (roughly the DSP working set) |
| A3 fragmentation | largest block <25% of free heap, or shrinking >20 B/s |
| A4 CSI rate | sustained median below 8 Hz |
| A5 frame length | no frames, or a length that is not 128/256/384 |
| A6 `first_word_invalid` | flag seen but DSP not excluding 2 bins (or vice versa) |
| A7 uptime | any reboot detected |

**A4 is the number that constrains everything after it.** A sustained rate of
R Hz means motion above R/2 Hz is not observable. If it comes out at 5 Hz rather
than the S3's ~20 Hz, that is the finding, and Stage B expectations get revised
down rather than the number talked up.

---

## Step 6 — Stage B runs

Nine runs, per `ESP32_PHASE3_VALIDATION.md` section 4. Fix the node position, AP,
channel and room layout before the first run and change nothing between runs.

```bash
python tools/csi-capture.py record --label empty-baseline      --duration 1800 --out data/phase3 --note "node on shelf, N wall, AP ch6"
python tools/csi-capture.py record --label empty-overnight     --duration 28800 --out data/phase3
python tools/csi-capture.py record --label enter-exit          --duration 600  --out data/phase3 --note "20x enter/wait15s/leave/wait15s"
python tools/csi-capture.py record --label walking-continuous  --duration 600  --out data/phase3
python tools/csi-capture.py record --label stationary-seated   --duration 1200 --out data/phase3 --note "seated 2 m from node"
python tools/csi-capture.py record --label stationary-standing --duration 600  --out data/phase3
python tools/csi-capture.py record --label two-people          --duration 600  --out data/phase3
python tools/csi-capture.py record --label empty-with-fan      --duration 600  --out data/phase3 --note "MANDATORY CONTROL: oscillating fan, nobody present"
python tools/csi-capture.py record --label empty-door-open     --duration 600  --out data/phase3 --note "MANDATORY CONTROL: door cycled from OUTSIDE the room every 30 s"
```

Both controls are mandatory. The fan tests whether the detector separates
*human* motion from *any* motion; the door tests geometry change with nobody in
the volume. Operate the door from outside so your own body stays out of it.

Every run writes a raw `.rvcsi` (packets byte-for-byte) plus a derived `.jsonl`.
Per-run record-keeping — label, timestamp, duration, observed CSI rate, packet
count, invalid count — is captured automatically in the container header and the
summary record; `csi-analyse.py` prints it as a table.

---

## Step 7 — Stage C analysis

```bash
python tools/csi-analyse.py data/phase3
python tools/csi-analyse.py data/phase3 --compare --json data/phase3/stageC.json
```

The comparisons are pre-registered in the source. There is no threshold anywhere
in that file — the output is ROC AUC, which is threshold-free. A threshold is
chosen only after separation is known to exist, and only against the overnight
drift floor.

Verified against synthetic data with known properties: perfect separation reads
1.000, identical distributions read 0.500, and a deliberately fan-dominated
dataset is correctly reported as an RF-change detector rather than a presence
detector.

Replay makes the analysis re-runnable as the feature extraction changes:

```bash
python tools/csi-capture.py replay data/phase3/empty-baseline-*.rvcsi \
  --host 127.0.0.1 --port 5005 --speed 10
```

---

## Troubleshooting map

| Symptom | Most likely cause |
|---|---|
| No COM port at all | Charge-only USB cable, or missing ARM64 bridge driver |
| `Failed to connect ... Wrong boot mode` | Hold BOOT, tap EN, release BOOT |
| Boots but reboots repeatedly | Wrong bootloader offset — must be `0x1000`, not `0x0` |
| `csi_diag` lines but rate=0.0 Hz | Associated but no frames — quiet network, or wrong channel |
| Serial shows CSI, host receives nothing | Firewall on UDP 5005, or wrong `--target-ip` |
| Never gets an IP | 5 GHz SSID (no 5 GHz radio), or WPA3-only AP |
| A6 FAIL | `first_word_invalid` handling inconsistent — report it, it is a real finding |

---

## What is deliberately not here

- The router + two-extender triangle experiment. Single-node baseline first, per
  the phase ordering. Nothing in this runbook assumes more than one transmitter.
- The CYD display HAL. Not started.
- Any threshold tuning. Not until the signal is understood.
