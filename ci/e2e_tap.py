#!/usr/bin/env python3
"""End-to-end test of asm-audiotap.exe on a real Windows audio stack.

The CI machine has no sound card; VB-CABLE's "CABLE Input" plays the part of the headset. The "programs" are
copies of audioprobe.exe under other names (so every one has its own executable and its own session), each
playing a sine of known amplitude into that endpoint. Through the real Python TapHelper we then check
what the helper hands to the engine (levels, separation of channels) and what still reaches the headset
(it must be silence), what happens when volumes are changed, programs come and go, the helper dies, ...
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from arctis_sound_manager.win.audio.tap import AppRules, TapHelper, restore_volumes  # noqa: E402

BUILD = ROOT / "build"
WORK = ROOT / "e2e-work"
WORK.mkdir(exist_ok=True)
LOG = open(ROOT / "e2e.log", "w", encoding="utf-8")
HEADSET = "CABLE Input"
RATE = 48000
FAILS: list[str] = []


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


def ap(*args: str, timeout: float = 30.0) -> str:
    out = subprocess.run([str(BUILD / "audioprobe.exe"), *args], capture_output=True, text=True, timeout=timeout)
    return (out.stdout + out.stderr).strip()


def sessions() -> list[dict]:
    rows = []
    for line in ap("sessions").splitlines():
        m = re.search(r"pid=(\d+) exe=(\S+) state=(\d+) volume=(\S+) mute=(\d+)", line)
        if m:
            rows.append({"pid": int(m[1]), "exe": m[2], "state": int(m[3]), "volume": float(m[4]), "mute": int(m[5]),
                         "device": line.split("'")[1] if "'" in line else ""})
    return rows


def volume_of(exe: str) -> float | None:
    vols = [s["volume"] for s in sessions() if s["exe"].lower() == exe.lower() and "CABLE Input" in s["device"]]
    return vols[0] if vols else None


class Fake:
    """A 'program': audioprobe.exe under another name playing a sine into the headset endpoint."""

    def __init__(self, name: str, amp: float, freq: float, secs: float):
        self.exe = WORK / f"{name}.exe"
        shutil.copy(BUILD / "audioprobe.exe", self.exe)
        self.name = f"{name}.exe"
        self.proc = subprocess.Popen([str(self.exe), "tone", "--device", HEADSET, "--amp", str(amp), "--freq", str(freq),
                                      "--secs", str(secs)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.pid = self.proc.pid
        self.first = self.proc.stdout.readline().strip()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class Recorder:
    """What really reaches the headset endpoint: a loopback recording of it, with a 10 ms level trace."""

    def __init__(self, secs: float):
        self.proc = subprocess.Popen([str(BUILD / "audioprobe.exe"), "record", "--device", HEADSET, "--loopback",
                                      "--secs", str(secs), "--trace"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def result(self) -> tuple[float, list[float]]:
        out, _ = self.proc.communicate(timeout=60)
        rms = 0.0
        m = re.search(r"ch1_rms=([0-9.eE+-]+)", out)
        if m:
            rms = float(m[1])
        t = re.search(r"TRACE record 10ms-rms:(.*)", out)
        trace = [float(x) for x in t[1].split()] if t else []
        if not m:
            log("   recorder said:", out[:300])
        return rms, trace


class Sinks:
    def __init__(self, helper: TapHelper):
        self.data = {c: [] for c in ("game", "chat", "media", "aux")}
        self.lock = threading.Lock()
        for c in self.data:
            helper.attach(c, lambda block, c=c: self._add(c, block))

    def _add(self, channel, block):
        with self.lock:
            self.data[channel].append(block.copy())

    def window(self, seconds: float) -> dict[str, np.ndarray]:
        with self.lock:
            for v in self.data.values():
                v.clear()
        time.sleep(seconds)
        with self.lock:
            return {c: (np.concatenate(v) if v else np.zeros((0, 2), dtype=np.float32)) for c, v in self.data.items()}


def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if a.size else 0.0


def tone_ratio(a: np.ndarray, hz_a: float, hz_b: float) -> float:
    """Energy near hz_a over energy near hz_b in the left channel."""
    x = a[:, 0].astype(np.float64)
    if x.size < 4096:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1 / RATE)

    def band(hz):
        return float(spec[(freqs > hz * 0.97) & (freqs < hz * 1.03)].sum()) + 1e-30
    return band(hz_a) / band(hz_b)


def wait_for(cond, timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def rel_ok(value: float, expected: float, tol: float) -> bool:
    return abs(value - expected) <= tol * expected


def make_helper(rules: AppRules) -> TapHelper:
    helper = TapHelper(exe=str(BUILD / "asm-audiotap.exe"), state=WORK / "tap_state.txt")
    helper.configure(endpoint=HEADSET, rules=rules, engaged=True)
    return helper


def main() -> int:
    log("== devices"); log(ap("list"))
    log("== sessions before"); log(ap("sessions"))
    state = WORK / "tap_state.txt"
    state.unlink(missing_ok=True)
    rules = AppRules(user={"fakechat.exe": "chat", "fakemedia.exe": "media", "fakeexit.exe": "game"})
    helper = make_helper(rules)
    sinks = Sinks(helper)
    apps: list[Fake] = []
    try:
        # ------------------------------------------------------------------------------------------- S1
        log("\n== S1 two programs playing; the helper joins them already running")
        game = Fake("fakegame", 0.30, 440, 120); chat = Fake("fakechat", 0.20, 880, 120); apps += [game, chat]
        log("   ", game.first); log("   ", chat.first)
        pre = Recorder(2.0)                                  # what reaches the headset before the helper takes over
        pre_rms, _ = pre.result()
        log(f"   headset before engaging: rms {pre_rms:.4f} (expect about {0.3/2**.5:.3f}+{0.2/2**.5:.3f} mixed)")
        check("S1.0 programs are audible at the headset before the helper takes over", pre_rms > 0.1, f"{pre_rms:.4f}")
        check("S1.1 the helper starts", helper.ensure_running(), helper.last_error)
        check("S1.2 both programs are found and turned down", wait_for(lambda: sum(1 for a in helper.apps() if a.get("managed")) >= 2, 10),
              json.dumps(helper.apps()))
        time.sleep(1.0)
        rec = Recorder(3.0)
        time.sleep(0.6)
        w = sinks.window(2.0)
        head_rms, head_trace = rec.result()
        g, c = rms(w["game"]), rms(w["chat"])
        check("S1.3 game level through the helper is what the program plays", rel_ok(g, 0.30 / 2 ** .5, 0.02), f"{g:.5f} vs {0.30/2**.5:.5f}")
        check("S1.4 chat level likewise", rel_ok(c, 0.20 / 2 ** .5, 0.02), f"{c:.5f} vs {0.20/2**.5:.5f}")
        check("S1.5 media and aux carry nothing", rms(w["media"]) < 1e-4 and rms(w["aux"]) < 1e-4)
        check("S1.6 the game channel holds only the game", tone_ratio(w["game"], 440, 880) > 1000, f"{tone_ratio(w['game'], 440, 880):.1f}")
        check("S1.7 the chat channel holds only the chat", tone_ratio(w["chat"], 880, 440) > 1000, f"{tone_ratio(w['chat'], 880, 440):.1f}")
        check("S1.8 left and right keep their level", abs(rms(w["game"][:, 0]) - rms(w["game"][:, 1])) < 1e-4)
        check("S1.9 nothing reaches the headset any more", head_rms < 1e-5 and max(head_trace or [0]) < 1e-4, f"rms {head_rms:.2e} max10ms {max(head_trace or [0]):.2e}")
        check("S1.10 the programs' sessions are at the helper's level", all(v is not None and v < 1e-6 for v in (volume_of("fakegame.exe"), volume_of("fakechat.exe"))),
              f"{volume_of('fakegame.exe')} {volume_of('fakechat.exe')}")
        log("   apps:", json.dumps(helper.apps()))

        # ------------------------------------------------------------------------------------------- S2
        log("\n== S2 the user moves a program's slider in the Windows volume mixer")
        for target in (0.5, 0.05, 1.0):
            ap("setvol", "--pid", str(game.pid), "--vol", str(target))
            time.sleep(1.5)
            w = sinks.window(1.5)
            check(f"S2 slider {target}: the helper follows", rel_ok(rms(w["game"]), 0.30 / 2 ** .5 * target, 0.03), f"{rms(w['game']):.5f} vs {0.30/2**.5*target:.5f}")
            check(f"S2 slider {target}: the program is turned down again", (volume_of("fakegame.exe") or 1) < 1e-6, str(volume_of("fakegame.exe")))

        # ------------------------------------------------------------------------------------------- S3
        log("\n== S3 mute in the mixer")
        ap("mute", "--pid", str(game.pid), "--on")
        time.sleep(1.0)
        check("S3.1 muted program is silent", rms(sinks.window(1.0)["game"]) < 1e-4)
        ap("mute", "--pid", str(game.pid))
        time.sleep(1.0)
        check("S3.2 unmuted program is back", rel_ok(rms(sinks.window(1.0)["game"]), 0.30 / 2 ** .5, 0.03))

        # ------------------------------------------------------------------------------------------- S4
        log("\n== S4 a program that goes straight to the headset (bypass)")
        helper.set_rule("fakechat.exe", "bypass")
        time.sleep(2.0)
        rec = Recorder(3.0); time.sleep(0.6)
        w = sinks.window(2.0)
        head_rms, _ = rec.result()
        check("S4.1 bypassed program gives the channel nothing", rms(w["chat"]) < 1e-3, f"{rms(w['chat']):.5f}")
        check("S4.2 its volume is back", abs((volume_of("fakechat.exe") or 0) - 1.0) < 1e-3, str(volume_of("fakechat.exe")))
        check("S4.3 it is audible at the headset", rel_ok(head_rms, 0.20 / 2 ** .5, 0.05), f"{head_rms:.4f}")
        helper.set_rule("fakechat.exe", "chat")
        time.sleep(2.0)
        rec = Recorder(3.0); time.sleep(0.6)
        w = sinks.window(2.0)
        head_rms, _ = rec.result()
        check("S4.4 back on its channel", rel_ok(rms(w["chat"]), 0.20 / 2 ** .5, 0.03) and head_rms < 1e-5, f"chat {rms(w['chat']):.5f} headset {head_rms:.2e}")

        # ------------------------------------------------------------------------------------------- S5
        log("\n== S5 a program that starts while the helper runs (what leaks before it is turned down?)")
        rec = Recorder(4.0)
        time.sleep(1.0)
        media = Fake("fakemedia", 0.25, 660, 6); apps.append(media)
        _, trace = rec.result()
        first = next((i for i, v in enumerate(trace) if v > 1e-4), None)
        leak = [v for v in trace[first:first + 100]] if first is not None else []
        log(f"   first launch: leak starts at bucket {first}, 10 ms levels: {['%.3f' % v for v in leak[:30]]}")
        leak_ms = sum(1 for v in leak if v > 0.01) * 10
        log(f"   MEASURE first-launch audible leak = {leak_ms} ms (peak {max(leak or [0]):.3f})")
        check("S5.1 a first launch leaks for no longer than 60 ms", leak_ms <= 60, f"{leak_ms} ms")
        check("S5.2 and the program then reaches its channel", wait_for(lambda: any(a.get("exe") == "fakemedia.exe" and a.get("managed") for a in helper.apps()), 5))
        media.kill(); time.sleep(1.0)
        rec = Recorder(3.0); time.sleep(0.6)
        media = Fake("fakemedia", 0.25, 660, 4); apps.append(media)
        _, trace = rec.result()
        peak = max(trace or [0])
        log(f"   MEASURE second launch (volume remembered by Windows): peak at headset {peak:.5f}")
        check("S5.3 a second launch of the same program does not leak at all", peak < 1e-3, f"{peak:.5f}")
        media.kill()

        # ------------------------------------------------------------------------------------------- S6
        log("\n== S6 a program that ends while turned down: what volume does it have the next time?")
        for how in ("ends by itself", "is killed"):
            helper.set_rule("fakeexit.exe", "game")
            ex = Fake("fakeexit", 0.2, 500, 3 if how == "ends by itself" else 30); apps.append(ex)
            wait_for(lambda: any(a.get("exe") == "fakeexit.exe" and a.get("managed") for a in helper.apps()), 5)
            time.sleep(0.5)
            if how == "is killed":
                ex.kill()
            else:
                wait_for(lambda: not ex.alive, 10)
            time.sleep(1.5)
            helper.stop()                                           # ASM is closed...
            ex2 = Fake("fakeexit", 0.2, 500, 5); apps.append(ex2)   # ...and the program is started again
            time.sleep(0.8)
            v = volume_of("fakeexit.exe")
            log(f"   MEASURE program {how}: volume of the next session with the helper not running = {v}")
            check(f"S6 program that {how} gets its volume back for the next launch", v is not None and v > 0.99, str(v))
            ex2.kill()
            log("   state file after:", (state.read_text() if state.exists() else "(none)").strip())
            helper = make_helper(rules); sinks = Sinks(helper); helper.ensure_running()
            time.sleep(1.0)

        # ------------------------------------------------------------------------------------------- S7
        log("\n== S7 the helper is killed (crash): programs stay turned down until restore")
        check("S7.0 helper is managing the game", wait_for(lambda: any(a.get("exe") == "fakegame.exe" and a.get("managed") for a in helper.apps()), 6))
        ap("setvol", "--pid", str(game.pid), "--vol", "0.4"); time.sleep(1.0)
        helper._proc.kill(); helper._proc.wait(); time.sleep(0.8)
        v = volume_of("fakegame.exe")
        log(f"   after the crash the game's volume is {v}; state file: {(state.read_text() if state.exists() else '(none)').strip()}")
        check("S7.1 (expected) a crashed helper leaves the program turned down", v is not None and v < 1e-6, str(v))
        check("S7.2 the restore command finds it and puts the volume back", restore_volumes(str(BUILD / "asm-audiotap.exe")))
        time.sleep(0.5)
        v = volume_of("fakegame.exe")
        check("S7.3 the volume it had before is back", v is not None and abs(v - 0.4) < 1e-3, str(v))
        ap("setvol", "--pid", str(game.pid), "--vol", "1.0")

        # ------------------------------------------------------------------------------------------- S8
        log("\n== S8 engaged / disengaged (the headset is switched off, the engine stops)")
        helper = make_helper(rules); sinks = Sinks(helper); helper.ensure_running()
        check("S8.0 managing again", wait_for(lambda: (volume_of("fakegame.exe") or 1) < 1e-6, 6))
        helper.engage(False)
        check("S8.1 disengaging puts the volume back within 2 s", wait_for(lambda: abs((volume_of("fakegame.exe") or 0) - 1.0) < 1e-3, 2), str(volume_of("fakegame.exe")))
        helper.engage(True)
        check("S8.2 engaging turns it down again", wait_for(lambda: (volume_of("fakegame.exe") or 1) < 1e-6, 4))
        w = sinks.window(1.5)
        check("S8.3 and the sound is back in the helper", rel_ok(rms(w["game"]), 0.30 / 2 ** .5, 0.03), f"{rms(w['game']):.5f}")

        # ------------------------------------------------------------------------------------------- S9
        log("\n== S9 stopping the helper puts every volume back")
        helper.stop()
        time.sleep(0.5)
        check("S9.1 game volume restored", abs((volume_of("fakegame.exe") or 0) - 1.0) < 1e-3, str(volume_of("fakegame.exe")))
        check("S9.2 chat volume restored", abs((volume_of("fakechat.exe") or 0) - 1.0) < 1e-3, str(volume_of("fakechat.exe")))
        log("   state file:", (state.read_text() if state.exists() else "(none)").strip())
    finally:
        log("\n== helper log"); [log("   ", x) for x in helper.stderr_log[-40:]]
        log("   info:", json.dumps({k: v for k, v in helper.info().items() if k != "apps"}))
        helper.stop()
        for a in apps:
            a.kill()
    log("\n== RESULT: " + ("all checks passed" if not FAILS else f"{len(FAILS)} check(s) failed: " + "; ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:                                       # noqa: BLE001
        import traceback
        log("EXCEPTION", traceback.format_exc())
        rc = 2
    LOG.close()
    sys.exit(rc)
