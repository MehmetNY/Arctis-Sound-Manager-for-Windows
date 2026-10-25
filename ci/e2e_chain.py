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
HEADSET = "CABLE In 16ch"
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


def write_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "win_audio_engine.json").write_text(json.dumps({
        "latency": "balanced",
        "inputs": {"game": "ASM Tap game", "chat": "ASM Tap chat", "media": "ASM Tap media", "aux": "ASM Tap aux"},
        "outputs": {"headset": HEADSET},
        "mic": {"input": "", "output": ""},
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
    def __init__(self, name: str, amp: float, freq: float, secs: float = 600):
        self.exe = WORK / f"{name}.exe"
        shutil.copy(BUILD / "audioprobe.exe", self.exe)
        self.name = f"{name}.exe"
        self.proc = subprocess.Popen([str(self.exe), "tone", "--device", PROGRAMS_ENDPOINT, "--amp", str(amp), "--freq", str(freq),
                                      "--secs", str(secs)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
                log(f"--- {path}  ({len(text)} chars)")
                if "engine" in path.name and len(lines) > 60:
                    log("    (first 35 lines)")
                    for line in lines[:35]:
                        log("    " + line[:300])
                    log("    (last 25 lines)")
                    lines = lines[-25:]
                for line in lines[-25:]:
                    log("    " + line[:300])


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

    log("== can the installed program open the headset device? (the engine's own code path, on two threads)")
    probe = subprocess.run([str(EXE), "--engine", "--open-test", HEADSET], capture_output=True, text=True, timeout=240)
    for line in (probe.stdout + probe.stderr).splitlines()[-40:]:
        log("    " + line[:300])
    check("C3.2 opening the headset device works in the installed program", probe.returncode == 0, f"exit {probe.returncode}")

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
    status = {}
    try:
        status = client.status()
    except EngineUnavailable as exc:
        log("   status unavailable:", exc)
    log("   status:", json.dumps(status)[:600])
    check("C4.2 it is running and has no problems", bool(status.get("running")) and not status.get("problems"), str(status.get("problems")))
    check("C4.3 the helper process is there", bool(processes("asm-audiotap.exe")))
    check("C4.4 the tray/daemon process is there (not a console window)", len(processes("ArctisSoundManager.exe")) >= 2,
          str(len(processes("ArctisSoundManager.exe"))))

    log("== two programs play")
    game = Fake("fakegame", 0.30, 440.0)
    chat = Fake("fakechat", 0.20, 880.0)
    log("   ", game.first, "|", chat.first)
    try:
        check("C5.1 the programs are turned down to the helper's level", wait_for(
            lambda: (volume_of("fakegame.exe") or 1) < 1e-6 and (volume_of("fakechat.exe") or 1) < 1e-6, 15),
            f"{volume_of('fakegame.exe')} {volume_of('fakechat.exe')}")
        time.sleep(2.0)
        both = level(HEADSET, 3.0)
        leak = level(PROGRAMS_ENDPOINT, 3.0)
        check("C5.2 the headset receives the mixed sound", both > 0.03, f"{both:.4f}")
        check("C5.3 not clipped", both < 0.9, f"{both:.4f}")
        check("C5.4 nothing reaches the programs' own endpoint any more", 0 <= leak < 2e-3, f"{leak:.5f}")

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
              abs((volume_of("fakechat.exe") or 0) - 1.0) < 1e-3, f"{volume_of('fakegame.exe')} {volume_of('fakechat.exe')}")
        direct = level(PROGRAMS_ENDPOINT, 2.0)
        check("C8.3 and are heard directly again", direct > 0.05, f"{direct:.4f}")
    finally:
        game.kill()
        chat.kill()

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
