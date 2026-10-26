"""Does asking Windows "what is playing?" (the DAC's media line) leak? Run on a real Windows machine.

The daemon asks the system media controls every OLED tick (about every 5 s, for as long as the headset is connected;
``oled_show_media`` is on by default). Nothing else in the test suite runs that code, because the runners have no
headset and no media player. This script makes both ends:

    python ci/media_soak.py            the consumer: starts the publisher, polls ``win/media.py`` the way the
                                       daemon does (one private event loop on a thread), prints memory per call
    python ci/media_soak.py publish    the publisher: a media session that is "playing" with a title, an artist,
                                       a changing track and a 190 KB thumbnail (as Spotify or a browser publish)

3000 polls at 20 ms stand for about four hours of ticks. Memory is the process's private bytes. Exit code 0
unless the polling itself leaks (more than ``LIMIT_KB_PER_CALL`` per call over the second half of a phase).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEDIA_PY = HERE.parent / "src" / "arctis_sound_manager" / "win" / "media.py"
LIMIT_KB_PER_CALL = 20.0
CALLS_WITH_SESSION = int(os.environ.get("MEDIA_SOAK_CALLS", "3000"))
CALLS_WITHOUT = 1000
INTERVAL_S = 0.02
EVERY = 250


def log(*parts) -> None:
    print(*parts, flush=True)


# ── the publisher ────────────────────────────────────────────────────────────

def png_noise(size: int = 256) -> bytes:
    """A valid PNG of random pixels (does not compress: about 190 KB)."""
    raw = b"".join(b"\x00" + os.urandom(size * 3) for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def publish() -> int:
    from winrt.windows.media import MediaPlaybackStatus, MediaPlaybackType
    from winrt.windows.media.playback import MediaPlayer

    player = MediaPlayer()
    player.command_manager.is_enabled = False                 # SMTC is driven by hand
    smtc = player.system_media_transport_controls
    smtc.is_enabled = True
    smtc.is_play_enabled = True
    smtc.is_pause_enabled = True
    smtc.playback_status = MediaPlaybackStatus.PLAYING
    thumbnail = None
    try:
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream, RandomAccessStreamReference
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_noise())
        asyncio.run(_await(writer.store_async()))
        stream.seek(0)
        thumbnail = RandomAccessStreamReference.create_from_stream(stream)
    except Exception as exc:                                  # noqa: BLE001 - the thumbnail is a bonus
        log("publisher: no thumbnail:", repr(exc))
    n = 0
    while True:
        updater = smtc.display_updater
        updater.type = MediaPlaybackType.MUSIC
        updater.music_properties.title = f"Soak Track {n}"
        updater.music_properties.artist = "Soak Artist"
        if thumbnail is not None:
            updater.thumbnail = thumbnail
        updater.update()
        smtc.playback_status = MediaPlaybackStatus.PLAYING
        n += 1
        time.sleep(15)


async def _await(op):
    return await op


# ── the consumer ─────────────────────────────────────────────────────────────

def load_media():
    spec = importlib.util.spec_from_file_location("asm_media", MEDIA_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_mb() -> float:
    import psutil
    info = psutil.Process().memory_info()
    return getattr(info, "private", info.vms) / 2 ** 20


def slope_kb_per_call(points: list[tuple[int, float]]) -> float:
    half = points[len(points) // 2:]
    if len(half) < 2:
        return 0.0
    n = len(half)
    mx = sum(x for x, _ in half) / n
    my = sum(y for _, y in half) / n
    var = sum((x - mx) ** 2 for x, _ in half)
    return 0.0 if var == 0 else sum((x - mx) * (y - my) for x, y in half) / var * 1024.0


class Poller:
    """What ``DesktopSession`` does for the daemon: one event loop on a daemon thread, blocking calls with a timeout."""

    def __init__(self, media):
        self.media = media
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="DesktopSession", daemon=True).start()

    def now_playing(self) -> str:
        try:
            return asyncio.run_coroutine_threadsafe(self.media.now_playing(), self.loop).result(timeout=5)
        except Exception as exc:                              # noqa: BLE001
            return f"<{exc!r}>"


def phase(title: str, poller: Poller, calls: int) -> tuple[float, str]:
    log(f"== {title}: {calls} polls, one every {INTERVAL_S * 1000:.0f} ms")
    points: list[tuple[int, float]] = []
    last = ""
    for i in range(calls):
        last = poller.now_playing()
        if i % EVERY == 0:
            mb = private_mb()
            points.append((i, mb))
            log(f"   call {i:5d}: private {mb:7.1f} MB, threads {threading.active_count()}, now playing: {last!r}")
        time.sleep(INTERVAL_S)
    points.append((calls, private_mb()))
    slope = slope_kb_per_call(points)
    log(f"   {title}: {points[0][1]:.1f} MB -> {points[-1][1]:.1f} MB; growth {slope:+.1f} KB per call (second half)")
    return slope, last


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "publish":
        return publish()
    media = load_media()
    publisher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "publish"])
    poller = Poller(media)
    try:
        deadline = time.time() + 90
        text = ""
        while time.time() < deadline and not text.startswith("Soak"):
            text = poller.now_playing()
            time.sleep(1)
        log(f"== the media session: {text!r}")
        if not text.startswith("Soak"):
            log("SKIP no media session could be created on this machine; nothing measured")
            return 0
        problems = []
        slope, last = phase("a player is playing (title, artist, thumbnail; the track changes every 15 s)", poller,
                            CALLS_WITH_SESSION)
        if slope > LIMIT_KB_PER_CALL:
            problems.append(f"polling a playing session grows by {slope:.1f} KB per call")
        publisher.kill()
        publisher.wait(timeout=20)
        time.sleep(3)
        slope, last = phase("nothing is playing any more", poller, CALLS_WITHOUT)
        if slope > LIMIT_KB_PER_CALL:
            problems.append(f"polling with no session grows by {slope:.1f} KB per call")
        log("== " + ("FAIL " + "; ".join(problems) if problems else "no leak in the media line"))
        return 1 if problems else 0
    finally:
        if publisher.poll() is None:
            publisher.kill()


if __name__ == "__main__":
    sys.exit(main())
