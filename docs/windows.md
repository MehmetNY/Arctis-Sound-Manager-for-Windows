# Arctis Sound Manager on Windows

This is the Windows port of Arctis Sound Manager: the same window, the same headset and base-station control, the same
Sonar-style channels, equaliser, spatial audio, noise cancelling and OLED screen - with an audio engine of its own in
place of PipeWire.

## Install

1. Download `ASM-Setup-<version>.exe` from the
   [`windows-latest` pre-release](https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows/releases/tag/windows-latest).
2. Run it. The installer is not code-signed yet, so Windows SmartScreen says "unknown publisher": choose
   **More info → Run anyway**.
3. Leave **Start Arctis Sound Manager with Windows** ticked if you want it. It then starts by itself, quietly, in the
   background (a tray icon, no window) every time you sign in.

Published releases (the ones that are not marked "pre-release") are on the
[Releases page](https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows/releases); the `windows-latest` pre-release is
the newest build of the branch and is rebuilt on every change.

That is all. The setup installs everything the program needs, including the one virtual audio device it uses for the
processed microphone (VB-CABLE, see below), and nothing has to be installed or configured by hand afterwards. A silent
installation works too: `ASM-Setup-<version>.exe /VERYSILENT /TASKS=autostart`.

To remove it, use *Apps → Installed apps*. The uninstaller stops every ASM process, gives every program its volume back
and removes the Start-with-Windows entry; it asks whether the virtual microphone driver should go as well, because other
programs may use it.

## Updates

ASM checks this repository for a newly **published** release (a pre-release or a draft never counts, so the
continuously rebuilt `windows-latest` build does not trigger anything) a minute and a half after it starts and then every six hours. When there is
one, a notification appears once and **Settings → Check for updates** (and the banner on the Channels page) offers
**Update now**: ASM downloads `ASM-Setup-<version>.exe` from that release, checks it against the SHA-256 and size GitHub
states for it, and runs it. Windows asks for the administrator's permission once (the program lives in Program Files);
the installer then ends every ASM process, replaces the files and opens the program again. Nothing is installed
without that click.

To publish an update, push a tag such as `v1.7.0` (the number must be higher than the one shown in the window's corner):
the `windows-release` workflow builds the installer with that version and attaches it to a new release. Until the
installer is attached, the release is not announced to anybody. The version number of builds that were not made from a
tag is only a label; releases are what people are told about.

The update check is the only thing the program ever asks the internet for. There is no telemetry, no account and no
usage statistics in the Windows build.

## How the sound is handled

ASM does not need a driver of its own, and it does not ask you to point every program at a virtual device.

* While ASM runs, each program that plays sound has its own volume in Windows' mixer turned (practically) to zero, and
  ASM listens to that program's audio directly (WASAPI "process loopback"), puts it back at the volume you chose, sorts
  it into **Game**, **Chat**, **Media** or **Aux** (voice-chat programs go to Chat, players and browsers to Media,
  everything else to Game; change it per program on the Channels page), applies the equaliser, spatial audio, volume
  boost and Smart Volume of that channel, mixes the channels and plays the result to your headset.
* The slider of a program in Windows' volume mixer still works: ASM follows it.
* When ASM exits - or crashes - the volumes are put back (a crash is repaired at the next start, and by the uninstaller).
* The **processed microphone** (noise cancelling, noise reduction, gate, compressor, equaliser) is played into a virtual
  microphone. Windows can only offer a new microphone through a driver, so ASM installs **VB-CABLE** for this, silently, as
  part of its setup. In Discord, OBS, games and other programs choose **CABLE Output** as the microphone.

VB-CABLE is © VB-Audio Software and is donationware: if it is useful to you, please support its author at
<https://vb-audio.com/Cable/>.

## What is where

| Page | What it holds |
|---|---|
| Channels | Game / Chat / Media / Aux volumes, ChatMix, which program is on which channel |
| Equalizer | The equaliser of every channel, the presets (Sonar's and your own), the microphone's equaliser |
| Headset | The headset's own settings, and next to them everything that shapes its sound: spatial audio, surround, volume boost, Smart Volume, and the microphone's AI noise cancelling, noise reduction, gate and compressor |
| DAC | The base-station screen: what it shows, fonts, and the media line (the track playing in your browser or player, read from Windows' media controls). The screen goes dark after the **screen timeout** (counted from the last turn of the wheel or change of a setting; 0 = never), and, with **turn the screen off while locked or idle**, while the PC is locked (lock screen, screen saver) or has had no keyboard or mouse input for five minutes |
| Help, About | How the program works; who made it, where its source and bug reports live, the credits, and the donation button |
| Settings | Start with Windows, theme, language, the **Audio engine** switch and **Devices...** (headset, microphone and where the processed microphone goes - ASM chooses them by itself, change them only if it chose wrongly) |

## Good to know

* **Close SteelSeries GG / Sonar** while ASM is running: both talk to the same base station, and Sonar's own virtual
  devices would sit in front of ASM's.
* **Programs that open the headset in "exclusive mode"** (some audio production tools, a few games) bypass Windows' mixer,
  and with it ASM; they play to the headset directly.
* **Recorders and streaming tools** (OBS, Discord's screen capture, Xbox Game Bar, ...) are never turned down, so what they
  record stays what they record. A program ASM has never heard before is only *listened to* until it plays; the first
  tenth of a second of its very first sound can be heard unprocessed, once.
* **VB-CABLE has two playing ends**, "CABLE Input" and "CABLE In 16ch". Windows lets only one of them be open at a time, and ASM uses "CABLE Input" for the processed microphone: do not point other programs at "CABLE In 16ch" while ASM runs.
* With the audio engine switched off (Settings → Audio engine), ASM does not touch any sound.
* The program is unsigned, so Windows warns when you run the installer. Signing is possible later.

## Memory and CPU

ASM runs as a few small processes (the tray with its window, the daemon, the audio engine and the audio helper); the
tray gives its hidden window back to Windows after ten minutes without use, so what is left is a few hundred MB while
the sound is processed. Each process writes a line like this about itself every five minutes:

```
health: tray pid 4120, private 212.4 MB, ws 96.0 MB, handles 410, gdi 61, user 33, threads 24, python-objects 188201, cpu 0.4%, up 3h05m, window closed
```

They are in `%LOCALAPPDATA%\arctis_manager\logs\tray.log` and `daemon.log`, and in
`%USERPROFILE%\.config\arctis_manager\asm-audio-engine.log` (the engine, which also reports the helper). "private" is
the number that matters (Task Manager's "Commit size"); the working set shrinks and grows by itself. A process whose
private memory keeps growing by more than 150 MB an hour logs a `health: ... grows by` warning, and one that passes
1.2 GB (tray) or 1.5 GB (engine) starts again by itself without interrupting the daemon or the sound.

If memory use is still too high on your PC, create a text file `%LOCALAPPDATA%\arctis_manager\diag.txt` containing
`1` and restart ASM: the same lines then also say which kinds of Python objects grew and which lines of code
allocated the memory (`diag:` lines). Send those logs with the report. Setting `ASM_NO_HEALTH=1` turns all of this off.

`busiest threads:` at the end of a line says which thread of the process is using the CPU. For the audio engine, a file
`%LOCALAPPDATA%\arctis_manager\profile-playback.txt` containing a number of seconds (default 20) makes the playback
thread profile itself once and write the result (`profile:` lines) to the engine log.

**What the program does when no headset is there.** It asks Windows for the *names* of the HID interfaces (one call, nothing
is opened) and only when a SteelSeries name appears or changes does it enumerate for real; a PC with no headset on it
enumerates nothing, however long it stays on. (Before, the headset was searched for with a full enumeration of every
HID interface about 140 times every three seconds, and the two-second hot-plug poll added more; a log from a seven-hour
session without a headset showed 8365 such rounds.)

## Building it yourself

```powershell
pip install -r installer\requirements.txt
.\installer\build.ps1        # PyInstaller folder + Inno Setup installer in build\
```

The native helper that takes each program's sound is `native/audiotap/audiotap.cpp` (MSVC, `ci/build_native.ps1`). GitHub
Actions (`windows-e2e`, `windows-installer`) build it all on real Windows machines, run the helper against a real audio
stack, install the result silently, play sound through it and uninstall it again.
