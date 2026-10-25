"""A stand-in for asm-audiotap.exe that speaks its protocol, so the Python side can be tested on any system.

    fake_audiotap.py [--state FILE]

stdout: an ATP1 packet every 10 ms (480 frames, 8 channels: the *game* pair carries a tone, the *chat* pair
a different one when FAKE_CHAT=1); stderr: a status line four times a second; stdin: the helper's commands,
each appended to ``<state>.cmds`` so a test can see what the program was told. ``quit`` ends it.
"""
import json
import math
import os
import struct
import sys
import threading
import time

RATE, CHUNK, CHANNELS = 48000, 480, 8


def main() -> int:
    state = sys.argv[sys.argv.index("--state") + 1] if "--state" in sys.argv else "fake_state.txt"
    cmds = state + ".cmds"
    engaged = {"on": False}
    stop = threading.Event()

    def read_commands():
        for raw in sys.stdin:
            line = raw.rstrip("\r\n")
            with open(cmds, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if line.startswith("engage\t"):
                engaged["on"] = line.endswith("1")
            if line == "quit":
                stop.set()
                return
        stop.set()                                   # stdin closed: the engine is gone

    threading.Thread(target=read_commands, daemon=True).start()
    out, err = sys.stdout.buffer, sys.stderr
    n = 0
    chat = os.environ.get("FAKE_CHAT") == "1"
    next_status = 0.0
    t0 = time.perf_counter()
    while not stop.is_set():
        frames = bytearray()
        for i in range(CHUNK):
            k = n + i
            row = [0.0] * CHANNELS
            row[0] = 0.2 * math.sin(2 * math.pi * 1000 * k / RATE)
            row[1] = 0.1 * math.sin(2 * math.pi * 1000 * k / RATE)
            if chat:
                row[2] = row[3] = 0.15 * math.sin(2 * math.pi * 500 * k / RATE)
            frames += struct.pack("<8f", *row)
        n += CHUNK
        try:
            out.write(struct.pack("<4sIII", b"ATP1", CHUNK, CHANNELS, 0) + bytes(frames))
            out.flush()
        except (BrokenPipeError, OSError):
            return 0
        now = time.perf_counter()
        if now >= next_status:
            next_status = now + 0.25
            err.write(json.dumps({"t": "status", "engaged": engaged["on"], "endpoint": "Fake Headset", "eps": 2e-8,
                                  "apps": [{"exe": "game.exe", "pid": 4242, "channel": "game", "managed": True,
                                            "active": True, "level": 0.1, "leaks": 0},
                                           {"exe": "discord.exe", "pid": 4343, "channel": "chat", "managed": True,
                                            "active": False, "level": 0.0, "leaks": 0}]}) + "\n")
            err.flush()
        time.sleep(max(0.0, t0 + n / RATE - time.perf_counter()))
    with open(cmds, "a", encoding="utf-8") as f:
        f.write("exited\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
