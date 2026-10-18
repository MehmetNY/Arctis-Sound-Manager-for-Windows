# Reverse-engineering a SteelSeries headset's control protocol

Some Arctis devices use HID DAC opcodes that differ from the ones already mapped
in `src/arctis_sound_manager/devices/*.yaml`. When a new device is recognised
but its controls have no effect (UI shows up, nothing happens), the opcodes need
to be captured from **SteelSeries GG**, which is the only software that speaks
the real protocol — and it is **Windows-only**.

This folder has the helpers used for that (first applied to the Arctis Nova
Pro Omni, issue #70), plus discovery scripts for problems that need no Windows
step at all — see §3.

## 1. Capture on Windows — `capture-gg-windows.ps1`

For the user with the device (needs Windows, or a Windows dual-boot). Works for
every Arctis headset, GameDAC and base station.

1. Install **SteelSeries GG**, and **Wireshark** with **"Install USBPcap"**
   ticked in its installer, then reboot:
   <https://www.wireshark.org/download.html>
2. Plug the headset (or its base station / DAC) into USB.
3. Open **Windows PowerShell as administrator** and run:
   ```powershell
   irm https://raw.githubusercontent.com/loteran/Arctis-Sound-Manager/develop/scripts/reverse-engineering/capture-gg-windows.ps1 -OutFile $env:TEMP\asm-capture.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\asm-capture.ps1
   ```
   It finds the SteelSeries device, starts USBPcap, then walks you through
   changing **one setting at a time** in GG and timestamps each change. The
   list of settings is the model's own, taken from GG's description of it;
   anything GG does not show is skipped with `s`.
4. Attach `Desktop\asm-gg-capture.zip` to the GitHub issue.

The zip holds `capture.pcapng` (USB capture), `actions.txt` (what was changed
and when, in UTC) and `device.txt` (the devices plugged in, GG's version).

The per-model lists are generated from the decrypted GG specs by
`gg_capture_plan.py --write` in the private research repo (setting names and
ranges only — no opcode). Rerun it when a profile or GG changes.

## 2. Decode on Linux — `parse_steelseries_capture.py`

For the maintainer, on the `.pcapng`:

```bash
sudo pacman -S wireshark-cli            # provides tshark (Arch/CachyOS)
python3 parse_steelseries_capture.py capture.pcapng actions.txt
```

USBPcap records the whole USB tree, so it picks the device that received the
most commands and the report id most of them start with (`--device N`,
`--report-id XX` to override). For each change it lists the commands sent
right after it (minus the background polling seen at baseline) and the frames
the device sent back that differ from the baseline — where a read-back offset
is checked. A log written in local time (older captures) is shifted to the
capture's clock automatically. The differential summary at the end gives, per
setting, the opcode and the byte that changes: the `update_sequence` for the
device YAML.

## 3. Find a missing status field without Windows — `gamebuds_battery_probe.py`

Some families have no `status.request` at all — nobody has ever captured what
they push, so battery and connection state are simply unavailable (the
GameBuds, issue #202: `devices/gamebuds.yaml` says so at the top). This
family, and any other in the same position, does not need Windows, Wireshark,
or an understanding of HID to investigate: on Linux, whatever the device
sends on its own can be listened to directly, and a byte that only changes
alongside a real state (in the case vs. out, muted vs. not) tends to stand
out from the rest once you have it side by side with what you were doing at
the time.

```bash
python3 gamebuds_battery_probe.py                       # passive capture only (writes nothing)
python3 gamebuds_battery_probe.py --send-status-opcodes  # also try opcodes borrowed from other families (writes to the device)
```

Two phases, and only the first runs unless asked:

1. **Listen.** It walks you through a short scripted sequence — case in, case
   out, mute the mic, idle — pausing at each step so you can do the thing,
   and records every frame the buds send with a timestamp and the action that
   was happening. A byte that stays put while one action holds and lands on a
   different, still-steady value once you switch actions is worth a second
   look; that pattern is exactly what a case-state or battery byte produces
   and what a counter byte does not.
2. **Ask, opt-in only (`--send-status-opcodes`).** Sends the status-request
   opcodes already known from other Arctis families and records which ones
   get a reply. This **writes to the device** — it is not known to be
   harmless on an unprobed family, only unlikely to be harmful, and the
   script says so before doing it. A reply proves an opcode works; it proves
   nothing about what any byte in that reply means. Do not carry a
   `response_mapping` over from the family the opcode came from — see
   `nova_7_discrete_battery.yaml`'s comment for what that mistake cost once
   already.

The ASM daemon holds the same USB interface whenever it runs, so the script
checks for it first, tells you the one command to stop it, and offers to stop
and restart it for you — or refuses to run rather than fight it for the
interface, the same failure mode discussion #203's boot-race analysis is
about.

Needs only pyusb, and runs from a checkout with ASM not installed — see the
script's own header for the exact command. Every run is saved to a
timestamped file in the working directory and ends with a short summary
meant to be pasted straight into the issue.

## 4. Watch a value the device only answers on request — `arctis7_chatmix_probe.py`

Not every state a headset knows arrives on its own. The Arctis 7 dongle's spec
declares its ChatMix dial as a read — out `[0x06, 0x24]`, back
`[0x06, 0x24, game, chat]` — and pushes nothing when the dial turns, so a
passive listen (§3) sees nothing however long it runs and however far you turn
it. ASM asked once at startup and never again, which is why the mix it showed
was frozen wherever the dial happened to be when the daemon started (#220).

```bash
systemctl --user stop arctis-manager     # it holds the interface
python3 arctis7_chatmix_probe.py         # sweep the dial slowly, then Ctrl-C
systemctl --user start arctis-manager
```

It asks once per 250 ms and prints a line only when the bytes change, so a
full sweep of the dial is a handful of lines rather than hundreds of identical
ones — short enough to paste straight into the issue. It tries the interface
the profile names first, then any other HID interface with an IN endpoint,
because the USB layout is in no SteelSeries specification and a profile shared
by several products can name an interface only some of them have (#213).

Read-only: one query per pass, no setting written. Needs only pyusb, and is a
single self-contained file — it can be downloaded on its own rather than
cloning the repository. Adapting it to another opcode is a matter of changing
`QUERY` and `PRODUCTS`.

### Find a screen's pixel layout — `oled_packing_probe.py`

The Arctis Pro Wireless base draws with the Siberia 840 protocol (its spec
`include`s siberia-840): the commands are in the spec, the pixel layout is
only the name of a GG builtin, `convert-to-column-packed-byte-format` (#305).
The probe draws a border, a block in the top-left corner and a number in each
candidate layout and asks which one looked right; set that as `packing:` in
the profile's `oled:` section.

```bash
systemctl --user stop arctis-manager
python3 oled_packing_probe.py
systemctl --user start arctis-manager
```

Draws only — no setting written — and hands the screen back at the end. Unlike
the probes above it imports ASM (protocol and font), so it needs ASM installed.

## 5. Automated SteelSeries GG watcher — `gg-watch.py`

This script is the **automation backbone** for new GG releases. It runs on a
GitHub Actions schedule (weekly, Monday 09:23 UTC) and:

1. **Detects** a new GG version via a single HEAD request (no download).
2. **Downloads** the installer (~430 MB) and extracts `deviceSpecifications/`,
   `SteelSeriesEngine.exe`, and `apps/sonar/db-migrations/`.
3. **Recovers** the OpenPGP passphrase from `SteelSeriesEngine.exe`
   (`recover-key.py` — pure stdlib PE/Go pclntab parser, no Wine).
4. **Decrypts** Arctis `.edevice` files (`decode-arctis-specs.sh` — gpg
   loopback, deterministic).
5. **Compares** against a committed fingerprint index in
   `.github/gg-watch-state.json` (no decrypted specs ever leave the runner):
   - new `.edevice` names → potential new hardware
   - changed `.device` SHA-256 hashes → protocol changes
   - missing Sonar presets → mechanical JSON commits
6. **Acts**:
   - Presets → writes JSON to `gui/presets/`, updates `presets_provenance.json`,
     regenerates `presets_manifest.json`, commits to `main`. The back-merge
     workflow carries it to `develop` — no cherry-pick needed.
   - New/changed specs → opens a GitHub issue for human review.

### Local usage (analysis only)

```bash
# Dry run against current main (needs the decoded-118.0.0/ directory locally)
GG_WORKDIR=/tmp/ggw ASM_ROOT=/tmp/asm-clone \
  python3 scripts/reverse-engineering/gg-watch.py --dry-run --since=117.0.0

# With --no-push: writes files locally, regenerates manifest, leaves git alone
GG_WORKDIR=~/steelseries-research ASM_ROOT=. \
  python3 scripts/reverse-engineering/gg-watch.py --no-push --since=117.0.0
```

Environment variables:
- `GG_WORKDIR` — temp workspace for downloads/extraction/decoding (default
  `~/steelseries-research`).
- `ASM_ROOT` — repo root (default `~/Arctis-Sound-Manager`).
- `GH_TOKEN` — GitHub token for `gh` and git push (in CI: `secrets.GITHUB_TOKEN`).

Flags:
- `--dry-run` — all reads/comparisons, no writes to git or issues.
- `--since=VERSION` — force the "known" version to replay a delta.
- `--no-push` — write files and manifest locally, skip commit/push.

### What is committed

- `scripts/reverse-engineering/gg-watch.py` (the watcher)
- `scripts/reverse-engineering/recover-key.py` (passphrase recovery)
- `scripts/reverse-engineering/decode-arctis-specs.sh` (deterministic decryption)
- `.github/workflows/gg-watch.yaml` (CI schedule + dispatch)
- `.github/gg-watch-state.json` (fingerprint index, written by the first real run)

### What is NOT committed

- Decrypted `.device` files — SteelSeries' proprietary material, kept only in
  the runner's temp dir and discarded.
- The passphrase — written to a file read by the shell script, never logged.
- The extracted installer — in `$GG_WORKDIR`, cleaned by the runner.
