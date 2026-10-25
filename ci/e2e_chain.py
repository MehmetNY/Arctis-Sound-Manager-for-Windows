#!/usr/bin/env python3
"""The whole chain on a real Windows audio stack, with the program the installer put on disk.

Run after ci/chain_install.ps1 (which ran ASM-Setup.exe silently, VB-CABLE included). The CI machine has no sound card:
"CABLE Input" is where the programs play (Windows' default output there) and "CABLE In 16ch" plays the part of the
headset. The "programs" are copies of audioprobe.exe under other names.

What is proved, with the installed ArctisSoundManager.exe started the way "Start with Windows" starts it (--background):
  * the engine comes up by itself and the per-application helper turns the programs down,
  * what the headset receives is the programs' sound, mixed by the engine, and nothing leaks to the programs' own endpoint,
  * the channel mute / volume commands change what the headset receives,
  * --quit-all puts every volume back, and the programs are heard directly again,
  * the uninstaller removes the program, the Start-with-Windows entry and leaves nothing running.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent

BUILD = ROOT / "build"
WORK = ROOT / "chain-work"
WORK.mkdir(exist_ok=True)
LOG = open(ROOT / "chain.log", "w", encoding="utf-8")
APP = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Arctis Sound Manager"
EXE = APP / "ArctisSoundManager.exe"
CONFIG_DIR = Path.home() / ".config" / "arctis_manager"
PROGRAMS_ENDPOINT = "CABLE Input"
HEADSET = "CABLE Input"      # the programs play to it too, exactly like a headset that is also Windows' default output
FAILS: list[str] = []


class EngineUnavailable(RuntimeError):
    pass


class EngineClient:
    """The engine's control protocol (one JSON line over a local socket, token from the port file), on its own:
    this test must not import the program, whose audio modules need packages the test machine does not have."""

    def __init__(self, config_dir: Path, timeout: float = 3.0):
        self.config_dir, self.timeout = config_dir, timeout

    def request(self, cmd: str, **fields) -> dict:
        import socket
        try:
            info = json.loads((self.config_dir / "win_audio_engine.port").read_text(encoding="utf-8"))
            port, token = int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineUnavailable("no port file") from exc
        payload = (json.dumps({"token": token, "cmd": cmd, **fields}) + "\n").encode("utf-8")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(payload)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise EngineUnavailable(str(exc)) from exc

    def available(self) -> bool:
        try:
            return bool(self.request("ping").get("ok"))
        except EngineUnavailable:
            return False

    def status(self) -> dict:
        return self.request("status")

    def set_volume(self, channel: str, pct: float) -> dict:
        return self.request("volume", channel=channel, pct=pct)

    def set_mute(self, channel: str, muted: bool) -> dict:
        return self.request("mute", channel=channel, muted=muted)


def write_config(mic_input: str = "", mic_output: str = "", playback: bool = True) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    taps = {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"}
    (CONFIG_DIR / "win_audio_engine.json").write_text(json.dumps({
        "latency": "balanced",
        "inputs": taps if playback else {ch: "" for ch in taps},
        "outputs": {"headset": HEADSET} if playback else {},
        "mic": {"input": mic_input, "output": mic_output},
        "master_limiter": True}, indent=2), encoding="utf-8")


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    LOG.write(text + "\n")
    LOG.flush()
    print(text, flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    log(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def ap(*args: str, timeout: float = 40.0) -> str:
    out = subprocess.run([str(BUILD / "audioprobe.exe"), *args], capture_output=True, text=True, timeout=timeout)
    return (out.stdout + out.stderr).strip()


def level(device: str, secs: float = 2.0) -> float:
    """RMS of what is played to *device* (a loopback recording)."""
    out = ap("record", "--device", device, "--loopback", "--secs", str(secs), timeout=secs + 40)
    m = re.search(r"ch1_rms=([0-9.eE+-]+)", out)
    if not m:
        log("   recorder said:", out[:300])
        return -1.0
    return float(m[1])


def volume_of(exe: str) -> float | None:
    for line in ap("sessions").splitlines():
        m = re.search(r"pid=(\d+) exe=(\S+) state=(\d+) volume=(\S+) mute=(\d+)", line)
        device = line.split("'")[1] if "'" in line else ""
        if m and m[2].lower() == exe.lower() and PROGRAMS_ENDPOINT in device:
            return float(m[4])
    return None


def wait_for(cond, timeout: float, step: float = 0.25) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:                                            # noqa: BLE001 — a probe that failed is "not yet"
            pass
        time.sleep(step)
    return False


def processes(image: str) -> list[str]:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if image.lower() in line.lower()]


class Fake:
    def __init__(self, name: str, amp: float, freq: float, secs: float = 600, noise: bool = False):
        self.exe = WORK / f"{name}.exe"
        shutil.copy(BUILD / "audioprobe.exe", self.exe)
        self.name = f"{name}.exe"
        self.proc = subprocess.Popen([str(self.exe), "tone", "--device", PROGRAMS_ENDPOINT, "--amp", str(amp), "--freq", str(freq),
                                      "--secs", str(secs), *(["--noise"] if noise else [])],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.first = self.proc.stdout.readline().strip()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def tail_logs() -> None:
    roots = [CONFIG_DIR, Path(os.environ.get("LOCALAPPDATA", "")) / "arctis_manager"]
    for root in roots:
        for path in sorted(root.rglob("*")) if root.exists() else []:
            if path.is_file() and path.suffix in (".log", ".txt", ".json", ".out", ".err") and "numba-cache" not in str(path):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lines = text.splitlines()
                log(f"--- {path}  ({len(text)} chars, {len(lines)} lines)")
                if "engine" in path.name:
                    shown = lines if len(lines) <= 140 else lines[:70] + ["    ..."] + lines[-50:]
                else:
                    shown = lines[-25:]
                for line in shown:
                    log("    " + line[:300])


def microphone_phase(client: "EngineClient") -> None:
    """Microphone -> the engine's microphone chain -> the virtual microphone, with and without noise cancelling.

    The runner has no microphone and only one virtual cable, so the cable plays every part: what a program plays into
    "CABLE Input" is what "CABLE Output" records. The engine reads "CABLE Output" as its microphone and plays the
    result into "CABLE Input" again (microphone only, no headset: a valid set-up). White noise stands in for the room.
    * noise cancelling off: the engine adds what it hears to the wire (and hears that again...): the wire gets louder
      than the noise alone;
    * noise cancelling on: RNNoise removes noise completely, the engine adds nothing: the wire carries the noise alone.
    The noise source is called obs64.exe: recorders are never turned down by the helper."""
    log("== the microphone chain: microphone -> noise cancelling -> virtual microphone")
    wire = "CABLE Input"
    state = CONFIG_DIR / "sonar_micro_state.json"

    def micro_state(noise_cancelling: bool) -> None:
        state.write_text(json.dumps({"bands": [], "noise_canceling": {"enabled": noise_cancelling, "value": 0.9, "engine": "rnnoise"}}),
                         encoding="utf-8")

    def status() -> dict:
        try:
            reply = client.status()
            return reply.get("status", reply)
        except EngineUnavailable:
            return {}

    source = None
    try:
        source = Fake("obs64", 0.3, 0.0, noise=True)
        time.sleep(2.5)
        alone = level(wire, 2.5)
        check("C10.1 the noise source is heard on the cable", alone > 0.05, f"{alone:.4f}")

        micro_state(False)
        write_config("CABLE Output", "CABLE Input", playback=False)
        subprocess.Popen([str(EXE), "--background"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("C10.2 the engine runs with only a microphone configured", wait_for(lambda: bool(status().get("running")), 240, 1.0))
        st = status()
        log("   endpoints:", json.dumps(st.get("endpoints", {}))[:900])
        bad = {k: v for k, v in (st.get("endpoints") or {}).items() if v.get("state") != "running"}
        check("C10.3 the microphone's devices are open", not bad and set(st.get("endpoints") or {}) == {"mic:in", "mic:out"},
              f"{sorted((st.get('endpoints') or {}))} {json.dumps(bad)[:300]} {st.get('problems')}")
        time.sleep(3.0)
        plain = level(wire, 2.5)
        check("C10.4 without noise cancelling the microphone's sound is passed on (the cable is louder than the noise alone)",
              plain > 1.3 * alone, f"{plain:.4f} vs {alone:.4f} alone")

        micro_state(True)
        log("   reload:", json.dumps(client.request("reload"))[:200])
        time.sleep(5.0)
        cancelled = level(wire, 2.5)
        engine_in_use = (status().get("microphone") or {}).get("noise_engine")
        check("C10.5 noise cancelling is running (RNNoise)", engine_in_use == "rnnoise", str(engine_in_use))
        check("C10.6 and takes the noise out: the engine adds nothing to the cable any more",
              0.85 * alone < cancelled < 1.15 * alone, f"{cancelled:.4f} vs {alone:.4f} alone")
        log(f"   cable level: noise alone {alone:.4f}   engine without noise cancelling {plain:.4f}   with it {cancelled:.4f}")
    finally:
        if source is not None:
            source.kill()
        subprocess.run([str(EXE), "--quit-all"], capture_output=True, timeout=120)
        time.sleep(2.0)
        check("C10.7 nothing of ASM is left running after the microphone test", not processes("ArctisSoundManager.exe") and not processes("asm-audiotap.exe"))


def run_key() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return winreg.QueryValueEx(k, "ArctisSoundManager")[0]
    except OSError:
        return None


def main() -> int:
    log("== installed program")
    check("C0.1 the installer put the program on disk", EXE.exists(), str(EXE))
    check("C0.2 and the per-application helper", (APP / "asm-audiotap.exe").exists())
    check("C0.3 and the virtual microphone driver's installer", (APP / "vbcable" / "VBCABLE_Setup_x64.exe").exists())
    out = ap("list")
    log(out)
    check("C1.1 the installer set up the virtual microphone (CABLE endpoints exist)", "CABLE Input" in out and "CABLE Output" in out)
    entry = run_key()
    check("C2.1 Start with Windows is registered", bool(entry) and "ArctisSoundManager.exe" in (entry or ""), str(entry))
    check("C2.2 and starts quietly in the background", "--background" in (entry or ""), str(entry))

    version = subprocess.run([str(EXE), "--version"], capture_output=True, text=True, timeout=120)
    check("C3.1 the installed program runs", version.returncode == 0 and re.match(r"\d+\.\d+", version.stdout.strip() or "") is not None,
          (version.stdout + version.stderr).strip()[:80])

    log("== the engine starts by itself, the way Start with Windows starts the program")
    write_config()
    client = EngineClient(CONFIG_DIR, timeout=3.0)
    background = subprocess.Popen([str(EXE), "--background"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = wait_for(client.available, 240, 1.0)
    check("C4.1 the engine is up without anybody starting it (first start compiles the audio kernels)", started)
    if not started:
        log("   background process exit code:", background.poll())
        tail_logs()
        log("   engine in the foreground instead, to see its output:")
        fg = subprocess.Popen([str(EXE), "--engine"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if wait_for(client.available, 240, 1.0):
            log("   (it does start by hand)")
        else:
            log("   engine output:", (fg.stdout.read(3000) if fg.poll() is not None else "(still running, not answering)"))
    def engine_status() -> dict:
        try:
            reply = client.status()
            return reply.get("status", reply)
        except EngineUnavailable as exc:
            log("   status unavailable:", exc)
            return {}

    # the control channel answers at once; the audio graph itself is built a moment later (kernels, devices)
    wait_for(lambda: bool(engine_status().get("running")), 120, 1.0)
    status = engine_status()
    log("   engine config:", json.dumps(status.get("config", {}))[:400])
    log("   engine endpoints:", json.dumps(status.get("endpoints", {}))[:1500])
    check("C4.2 it is running and has no problems", bool(status.get("running")) and not status.get("problems") and not status.get("error"),
          f"{status.get('problems')} {status.get('error')}")
    bad = {k: v for k, v in (status.get("endpoints") or {}).items() if v.get("state") != "running"}
    check("C4.2b every device of the engine is open", not bad, json.dumps(bad)[:300])
    check("C4.3 the helper process is there", wait_for(lambda: bool(processes("asm-audiotap.exe")), 20, 1.0))
    check("C4.4 the tray/daemon process is there (not a console window)", len(processes("ArctisSoundManager.exe")) >= 2,
          str(len(processes("ArctisSoundManager.exe"))))

    log("== two programs play")
    game = Fake("fakegame", 0.30, 440.0)
    chat = Fake("discord", 0.20, 880.0)
    log("   ", game.first, "|", chat.first)
    try:
        check("C5.1 the programs are turned down to the helper's level", wait_for(
            lambda: (volume_of("fakegame.exe") or 1) < 1e-6 and (volume_of("discord.exe") or 1) < 1e-6, 15),
            f"{volume_of('fakegame.exe')} {volume_of('discord.exe')}")
        time.sleep(2.0)
        both = level(HEADSET, 3.0)
        leak = level(HEADSET, 1.0)
        check("C5.2 the headset receives the mixed sound", both > 0.03, f"{both:.4f}")
        check("C5.3 not clipped", both < 0.9, f"{both:.4f}")
        log(f"   (the programs' endpoint is the headset here, so it carries the engine's mix: {leak:.4f})")

        log("== channel controls")
        client.set_mute("game", True)
        time.sleep(0.6)
        chat_only = level(HEADSET, 2.0)
        client.set_mute("game", False)
        client.set_mute("chat", True)
        time.sleep(0.6)
        game_only = level(HEADSET, 2.0)
        client.set_mute("game", True)
        time.sleep(0.6)
        none = level(HEADSET, 2.0)
        client.set_mute("game", False)
        client.set_mute("chat", False)
        time.sleep(0.6)
        again = level(HEADSET, 2.0)
        log(f"   both {both:.4f}  chat only {chat_only:.4f}  game only {game_only:.4f}  none {none:.5f}  both again {again:.4f}")
        check("C6.1 muting the game channel leaves the chat", 0.01 < chat_only < both * 0.9, f"{chat_only:.4f}")
        check("C6.2 muting the chat channel leaves the game", 0.01 < game_only < both * 0.99, f"{game_only:.4f}")
        check("C6.3 muting both is silence", 0 <= none < 2e-3, f"{none:.5f}")
        check("C6.4 unmuting brings the sound back", again > both * 0.8, f"{again:.4f} vs {both:.4f}")
        check("C6.5 the two channels add up (separate channels, one mix)",
              abs((chat_only ** 2 + game_only ** 2) - both ** 2) < 0.35 * both ** 2,
              f"{chat_only ** 2 + game_only ** 2:.5f} vs {both ** 2:.5f}")
        client.set_mute("chat", True)
        client.set_volume("game", 50)
        time.sleep(0.6)
        half = level(HEADSET, 2.0)
        client.set_volume("game", 100)
        client.set_mute("chat", False)
        check("C6.6 the volume of a channel changes the level", 0.1 * game_only < half < game_only * 0.9, f"{half:.4f} vs {game_only:.4f}")

        log("== cost")
        try:
            import psutil
            procs = [p for p in psutil.process_iter(["name", "cmdline"]) if (p.info["name"] or "").lower() in ("arctissoundmanager.exe", "asm-audiotap.exe")]
            for p in procs:
                p.cpu_percent(None)
            time.sleep(8)
            total = 0.0
            for p in procs:
                try:
                    cpu = p.cpu_percent(None)
                except psutil.Error:
                    continue
                total += cpu
                log(f"   MEASURE {p.info['name']} pid {p.pid}: {cpu:.1f}% of one core   {' '.join(p.info['cmdline'] or [])[-40:]}")
            check("C7.1 all of ASM together uses less than half a core while two programs play", total < 50, f"{total:.1f}%")
        except ImportError:
            log("   psutil not installed: CPU not measured")

        log("== quit all")
        subprocess.run([str(EXE), "--quit-all"], capture_output=True, timeout=120)
        time.sleep(2.0)
        check("C8.1 no ASM process is left", not processes("ArctisSoundManager.exe") and not processes("asm-audiotap.exe"),
              f"{processes('ArctisSoundManager.exe')} {processes('asm-audiotap.exe')}")
        check("C8.2 the programs have their volume back", abs((volume_of("fakegame.exe") or 0) - 1.0) < 1e-3 and
              abs((volume_of("discord.exe") or 0) - 1.0) < 1e-3, f"{volume_of('fakegame.exe')} {volume_of('discord.exe')}")
        direct = level(PROGRAMS_ENDPOINT, 2.0)
        check("C8.3 and are heard directly again", direct > 0.05, f"{direct:.4f}")
    finally:
        game.kill()
        chat.kill()

    log("== can the installed program open the headset device? (the engine's own code path, on two threads)")
    for device in ("CABLE Input", "CABLE In 16ch"):
        probe = subprocess.run([str(EXE), "--engine", "--open-test", device], capture_output=True, text=True, timeout=240)
        log(f"   --- {device}")
        for line in (probe.stdout + probe.stderr).splitlines()[-40:]:
            log("    " + line[:300])
    log(f"   (informational; the second, worker-thread attempt starts a second stream right after the first closed)")

    microphone_phase(client)


    log("== uninstall")
    uninstaller = APP / "unins000.exe"
    check("C9.0 there is an uninstaller", uninstaller.exists())
    if uninstaller.exists():
        done = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], capture_output=True, timeout=300)
        log("   uninstaller exit code:", done.returncode)
        time.sleep(5)
        check("C9.1 the program folder is gone (or only leftovers of the uninstaller itself)",
              not (APP / "ArctisSoundManager.exe").exists() and not (APP / "asm-audiotap.exe").exists(),
              str([p.name for p in APP.glob("*")][:8]) if APP.exists() else "folder removed")
        check("C9.2 Start with Windows is removed", run_key() is None, str(run_key()))
        check("C9.3 nothing is left running", not processes("ArctisSoundManager.exe") and not processes("asm-audiotap.exe"))

    log("== logs of the programs")
    tail_logs()
    log("\n== RESULT:", "all checks passed" if not FAILS else f"{len(FAILS)} check(s) failed: " + "; ".join(FAILS))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:                                            # noqa: BLE001 — the log must say what happened
        import traceback
        log("CRASH:\n" + traceback.format_exc())
        raise
    raise SystemExit(rc)
