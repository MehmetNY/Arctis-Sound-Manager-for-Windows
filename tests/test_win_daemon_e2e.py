"""asm-daemon end to end, as it will run on Windows (clean-interpreter subprocess).

Covers what unit tests cannot: that the *real* CoreEngine finds a headset through the
hidapi shim, talks to it, parses the status frame captured on real hardware, serves it to a
client over the local IPC, and shuts down on Ctrl-C leaving nothing behind, with no PipeWire,
no pyudev, no pulsectl and no D-Bus session bus anywhere.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("win_daemon_harness.py")


@pytest.fixture(scope="module")
def run():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, str(HARNESS)], capture_output=True, text=True,
                          timeout=150, env=env)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT:")), None)
    assert line is not None, f"harness produced no result.\nstdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
    return json.loads(line[len("RESULT:"):]), proc


def test_status_parsed_from_the_frame_captured_on_real_hardware(run):
    result, _ = run
    headset = result["status"]["headset"]
    assert headset["headset_power_status"]["value"] == "online"
    assert headset["headset_battery_charge"]["value"] == 100
    assert headset["noise_cancelling"]["value"] == "on"
    assert headset["transparent_noise_cancelling_level"]["value"] == 80
    assert result["status"]["mic"]["mic_status"]["value"] == "unmuted"


def test_ipc_serves_the_real_dbus_service_classes(run):
    result, _ = run
    assert result["unknown_method_error"] == "org.freedesktop.DBus.Error.UnknownMethod"
    assert result["get_settings"] == "METHOD_RETURN" and result["get_settings_has_general"]


def test_commands_use_the_numbered_command_report_at_its_exact_length(run):
    result, _ = run
    assert result["writes_ffc0"] > 0
    assert result["ffc0_write_ids"] == [6]          # report id 6 = the command channel, never 0x00
    assert result["ffc0_write_lens"] == [64]        # Windows rejects a report of any other size


def test_ctrl_c_shuts_down_cleanly_and_fast(run):
    result, _ = run
    assert result["shutdown_clean"], result.get("shutdown_error")
    assert result["shutdown_s"] < 10


def test_no_errors_tracebacks_or_audio_graph_noise_in_the_log(run):
    _, proc = run
    log = proc.stdout + proc.stderr
    assert "Traceback" not in log
    assert "[  ERROR]" not in log
    for noise in ("pw-dump", "pactl", "wireplumber", "No physical ALSA sink"):
        assert noise not in log, f"Linux audio tooling leaked into the Windows run: {noise!r}"


def test_the_ipc_state_file_is_removed_when_the_daemon_exits(run):
    result, _ = run
    assert not (Path(result["tmp"]) / "run" / "ipc.json").exists()


def test_ctrl_c_hands_the_base_station_back_to_hardware_mode(run):
    """device_init switches software-ChatMix on (0x8d 0x01, 0x49 0x01); the YAML's shutdown block
    must undo it (0x49 0x00, 0x8d 0x00), or the dial is left driving nothing once ASM stops."""
    result, _ = run
    tail = [tuple(w) for w in result["shutdown_tail"]]
    assert (6, 0x49, 0) in tail and (6, 0x8d, 0) in tail, tail
    assert tail.index((6, 0x49, 0)) < tail.index((6, 0x8d, 0)), tail
