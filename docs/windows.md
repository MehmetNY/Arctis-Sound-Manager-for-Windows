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

That is all. The setup installs everything the program needs, including the one virtual audio device it uses for the
processed microphone (VB-CABLE, see below), and nothing has to be installed or configured by hand afterwards. A silent
installation works too: `ASM-Setup-<version>.exe /VERYSILENT /TASKS=autostart`.

To remove it, use *Apps → Installed apps*. The uninstaller stops every ASM process, gives every program its volume back
and removes the Start-with-Windows entry; it asks whether the virtual microphone driver should go as well, because other
programs may use it.

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
| DAC | The base-station screen: what it shows, fonts, and the media line (the track playing in your browser or player, read from Windows' media controls) |
| Settings | Start with Windows, theme, language, the **Audio engine** switch and **Devices...** (headset, microphone and where the processed microphone goes - ASM chooses them by itself, change them only if it chose wrongly) |

## Good to know

* **Close SteelSeries GG / Sonar** while ASM is running: both talk to the same base station, and Sonar's own virtual
  devices would sit in front of ASM's.
* **Programs that open the headset in "exclusive mode"** (some audio production tools, a few games) bypass Windows' mixer,
  and with it ASM; they play to the headset directly.
* **Recorders and streaming tools** (OBS, Discord's screen capture, Xbox Game Bar, ...) are never turned down, so what they
  record stays what they record. A program ASM has never heard before is only *listened to* until it plays; the first
  tenth of a second of its very first sound can be heard unprocessed, once.
* With the audio engine switched off (Settings → Audio engine), ASM does not touch any sound.
* The program is unsigned, so Windows warns when you run the installer. Signing is possible later.

## Building it yourself

```powershell
pip install -r installer\requirements.txt
.\installer\build.ps1        # PyInstaller folder + Inno Setup installer in build\
```

The native helper that takes each program's sound is `native/audiotap/audiotap.cpp` (MSVC, `ci/build_native.ps1`). GitHub
Actions (`windows-e2e`, `windows-installer`) build it all on real Windows machines, run the helper against a real audio
stack, install the result silently, play sound through it and uninstall it again.
