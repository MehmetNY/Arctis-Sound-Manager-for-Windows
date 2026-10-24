"""The Windows audio probe's parsers and report, against a fake registry.

Nothing here needs Windows: the probe only talks to ``winreg`` through the handful of calls the
fake below implements, and the WAVEFORMATEX blobs are built the way Windows writes them (a
PROPVARIANT-style header followed by the format structure).
"""
from __future__ import annotations

import json
import struct
import types

import pytest

from arctis_sound_manager.scripts import win_audio_probe as probe

KSDATAFORMAT_SUBTYPE_PCM = bytes.fromhex("0100000000001000800000aa00389b71")
KSDATAFORMAT_SUBTYPE_FLOAT = bytes.fromhex("0300000000001000800000aa00389b71")


def extensible(channels: int, rate: int, bits: int, mask: int, *, float_samples: bool, header: int = 16) -> bytes:
    align = channels * bits // 8
    # WAVEFORMATEX (18 bytes: tag, ch, rate, avg, align, bits, cbSize=22) + valid bits + mask + subformat GUID
    wfx = struct.pack("<HHIIHHH", 0xFFFE, channels, rate, rate * align, align, bits, 22)
    wfx += struct.pack("<HI", bits, mask) + (KSDATAFORMAT_SUBTYPE_FLOAT if float_samples else KSDATAFORMAT_SUBTYPE_PCM)
    assert len(wfx) == 40
    return b"\x41\x00\x00\x00\x00\x00\x00\x00" + struct.pack("<I", len(wfx)).ljust(header - 8, b"\x00") + wfx


def plain_pcm(channels: int, rate: int, bits: int, header: int = 16) -> bytes:
    align = channels * bits // 8
    wfx = struct.pack("<HHIIHH", 1, channels, rate, rate * align, align, bits)
    return b"\x00" * header + wfx


def test_parse_extensible_71_float():
    mask = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x200 | 0x400
    info = probe.parse_waveformat(extensible(8, 48000, 32, mask, float_samples=True))
    assert info["channels"] == 8 and info["rate"] == 48000 and info["bits"] == 32
    assert info["subformat"] == "float"
    assert info["speakers"] == ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"]


@pytest.mark.parametrize("header", [0, 8, 12, 16, 20, 24])
def test_parse_survives_header_length_differences(header):
    info = probe.parse_waveformat(extensible(2, 44100, 16, 0x3, float_samples=False, header=max(header, 12)) if header >= 12
                                  else plain_pcm(2, 44100, 16, header))
    assert (info["channels"], info["rate"], info["bits"]) == (2, 44100, 16)


def test_parse_plain_pcm_at_the_very_end_of_the_blob():
    # a 16-byte WAVEFORMAT with nothing after it: the last valid offset must still be tried
    info = probe.parse_waveformat(plain_pcm(2, 48000, 16))
    assert info == {"format_tag": 1, "channels": 2, "rate": 48000, "bits": 16, "subformat": "PCM"}


@pytest.mark.parametrize("blob", [b"", b"\x00" * 64, b"\xff" * 64, bytes(range(64)), b"\x41" * 5])
def test_parse_rejects_garbage_without_raising(blob):
    assert probe.parse_waveformat(blob) is None


def test_parse_rejects_inconsistent_fields():
    good = plain_pcm(2, 48000, 16)
    broken = bytearray(good)
    struct.pack_into("<I", broken, 16 + 8, 12345)  # average bytes/s no longer matches rate * align
    assert probe.parse_waveformat(bytes(broken)) is None


def test_channel_mask_names():
    assert probe.channel_mask_names(0x3) == ["FL", "FR"]
    assert probe.channel_mask_names(0) == []


@pytest.mark.parametrize("name,virtual,cable,steelseries", [
    ("CABLE Input (VB-Audio Virtual Cable)", True, True, False),
    ("Voicemeeter Input (VB-Audio Voicemeeter VAIO)", True, True, False),
    ("Headset Earphone (Arctis Nova Pro Wireless)", False, False, True),
    ("SteelSeries Sonar - Gaming (SteelSeries Sonar Virtual Audio Device)", True, False, True),
    ("Speakers (Realtek(R) Audio)", False, False, False),
])
def test_classify_endpoint(name, virtual, cable, steelseries):
    assert probe.classify_endpoint(name) == {"virtual": virtual, "cable": cable, "steelseries": steelseries}


class FakeWinreg:
    """The subset of winreg the probe uses, backed by nested dicts."""

    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_WOW64_64KEY = 0x100
    KEY_WOW64_32KEY = 0x200

    def __init__(self, tree: dict[str, dict]):
        self.tree = {path.lower(): values for path, values in tree.items()}
        self.opened = 0

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if root != self.HKEY_LOCAL_MACHINE or path.lower() not in self._all_paths():
            raise FileNotFoundError(path)
        self.opened += 1
        return path.lower()

    def _all_paths(self):
        paths = set()
        for full in self.tree:
            parts = full.split("\\")
            for i in range(1, len(parts) + 1):
                paths.add("\\".join(parts[:i]))
        return paths

    def CloseKey(self, key):  # noqa: N802
        self.opened -= 1

    def EnumValue(self, key, index):  # noqa: N802
        items = list(self.tree.get(key, {}).items())
        if index >= len(items):
            raise OSError("no more values")
        name, value = items[index]
        return name, value, 0

    def EnumKey(self, key, index):  # noqa: N802
        prefix = key + "\\"
        children = sorted({p[len(prefix):].split("\\")[0] for p in self._all_paths() if p.startswith(prefix)})
        if index >= len(children):
            raise OSError("no more keys")
        return children[index]

    def QueryValueEx(self, key, name):  # noqa: N802
        values = self.tree.get(key, {})
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1


def endpoint(flow: str, guid: str, name: str, adapter: str, state: int, fmt: bytes | None, form: int = 5) -> dict:
    base = rf"{probe.MMDEVICES}\{flow}\{guid}".lower()
    props = {probe._PK_NAME: name, probe._PK_ADAPTER: adapter, probe._PK_FORM_FACTOR: form}
    if fmt is not None:
        props[probe._PK_DEVICE_FORMAT] = fmt
    return {base: {"DeviceState": state}, base + r"\properties": props}


def build_tree() -> dict[str, dict]:
    tree: dict[str, dict] = {}
    stereo = extensible(2, 48000, 32, 0x3, float_samples=True)
    surround = extensible(8, 48000, 32, 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x200 | 0x400, float_samples=True)
    for part in (
        endpoint("Render", "{r-game}", "Headset Earphone", "Arctis Nova Pro Wireless", 1, stereo),
        endpoint("Render", "{r-71}", "Speakers", "Arctis Nova Pro Wireless Surround", 1, surround, form=1),
        endpoint("Render", "{r-cable}", "CABLE Input", "VB-Audio Virtual Cable", 1, stereo),
        endpoint("Render", "{r-old}", "Speakers", "Realtek(R) Audio", 4, None, form=1),
        endpoint("Capture", "{c-mic}", "Headset Microphone", "Arctis Nova Pro Wireless", 1,
                 extensible(1, 48000, 16, 0x4, float_samples=False), form=4),
    ):
        tree.update(part)
    return tree


def test_read_endpoints_reports_formats_states_and_tags():
    endpoints = probe.read_endpoints(FakeWinreg(build_tree()))
    by_id = {e["id"]: e for e in endpoints}
    assert set(by_id) == {"{r-game}", "{r-71}", "{r-cable}", "{r-old}", "{c-mic}"}
    assert by_id["{r-game}"]["friendly_name"] == "Headset Earphone (Arctis Nova Pro Wireless)"
    assert by_id["{r-game}"]["mix_format"]["channels"] == 2 and by_id["{r-game}"]["steelseries"]
    assert by_id["{r-71}"]["mix_format"]["channels"] == 8
    assert by_id["{r-cable}"]["cable"] and by_id["{r-cable}"]["virtual"]
    assert by_id["{r-old}"]["state"] == "not present" and by_id["{r-old}"]["mix_format"] is None
    assert by_id["{c-mic}"]["flow"] == "recording" and by_id["{c-mic}"]["form_factor"] == "microphone"
    assert by_id["{c-mic}"]["mix_format"]["channels"] == 1


def test_read_endpoints_closes_every_key_and_tolerates_a_missing_registry():
    reg = FakeWinreg(build_tree())
    probe.read_endpoints(reg)
    assert reg.opened == 0
    assert probe.read_endpoints(FakeWinreg({})) == []


def test_equalizer_apo_detection():
    absent = probe.read_equalizer_apo(FakeWinreg({}), exists=lambda p: False)
    assert absent == {"installed": False, "install_path": None, "config_dir": None, "config_writable": False}

    reg = FakeWinreg({r"software\equalizerapo": {"InstallPath": r"D:\Audio\EqualizerAPO"}})
    present = probe.read_equalizer_apo(reg, exists=lambda p: p.startswith(r"D:\Audio\EqualizerAPO"),
                                       writable=lambda p: p.endswith("config"))
    assert present["installed"] and present["install_path"] == r"D:\Audio\EqualizerAPO"
    assert present["config_dir"].replace("/", "\\") == r"D:\Audio\EqualizerAPO\config" and present["config_writable"]

    locked = probe.read_equalizer_apo(reg, exists=lambda p: True, writable=lambda p: False)
    assert locked["installed"] and not locked["config_writable"]


def test_steelseries_process_filter():
    names = ["explorer.exe", "SteelSeriesGG.exe", "SteelSeriesGGClient.exe", "SteelSeriesSonar.exe",
             "ggwp.exe", "chrome.exe", "SteelSeriesGG.exe"]
    assert probe.steelseries_processes(names) == ["SteelSeriesGG.exe", "SteelSeriesGGClient.exe", "SteelSeriesSonar.exe"]


def test_running_processes_parses_tasklist_csv_and_survives_failure():
    out = '"System","4","Services","0","1,000 K"\n"SteelSeriesGG.exe","123","Console","1","90,000 K"\n'
    run = lambda *a, **k: types.SimpleNamespace(stdout=out)  # noqa: E731
    assert probe.running_processes(run) == ["System", "SteelSeriesGG.exe"]

    def boom(*a, **k):
        raise FileNotFoundError("tasklist")
    assert probe.running_processes(boom) == []


def test_report_text_and_json_roundtrip():
    reg = FakeWinreg(build_tree())
    report = probe.build_report(
        reg, procs=["SteelSeriesGG.exe"], packages={"numpy": True, "sounddevice": False},
        portaudio=None, sessions=None,
        apo={"installed": False, "install_path": None, "config_dir": None, "config_writable": False})
    text = probe.format_report(report)
    assert "Headset Earphone (Arctis Nova Pro Wireless)" in text
    assert "8ch 48000Hz 32bit float [FL FR FC LFE BL BR SL SR]" in text or "8ch 48000Hz 32bit float" in text
    assert "CABLE Input (VB-Audio Virtual Cable)" in text and "KABLO" in text
    assert "Sanal kablo/ses surucusu (aktif): 1 uc nokta" in text
    assert "Equalizer APO: kurulu degil" in text
    assert "SteelSeriesGG.exe" in text
    assert "sounddevice=YOK" in text and "numpy=var" in text
    assert "kurulu degil" in text  # PortAudio line
    # the not-present device is listed after the active ones, not dropped
    assert text.index("Headset Earphone") < text.index("Realtek(R) Audio")
    json.dumps(report)  # the file written by main() must be serialisable


def test_report_without_any_cable_says_so():
    reg = FakeWinreg(endpoint("Render", "{r1}", "Speakers", "Realtek(R) Audio", 1,
                              extensible(2, 48000, 32, 0x3, float_samples=True), form=1))
    report = probe.build_report(reg, procs=[], packages={}, portaudio=None, sessions=None,
                                apo={"installed": False, "install_path": None, "config_dir": None, "config_writable": False})
    text = probe.format_report(report)
    assert "hic yok" in text and "SteelSeries sureclari: calismiyor" in text


def test_report_formats_wasapi_channel_support_and_sessions():
    reg = FakeWinreg({})
    portaudio = {"portaudio": "PortAudio V19", "default_output": "Speakers", "default_input": "Mic",
                 "wasapi_devices": [{"name": "Headset Earphone", "accepts_out": [1, 2], "accepts_in": []},
                                    {"name": "Surround", "accepts_out": [2, 6, 8], "accepts_in": []}]}
    report = probe.build_report(reg, procs=[], packages={}, portaudio=portaudio,
                                sessions=[{"process": "chrome.exe"}, {"process": "Discord.exe"}, {"process": "chrome.exe"}],
                                apo={"installed": False, "install_path": None, "config_dir": None, "config_writable": False})
    text = probe.format_report(report)
    assert "Headset Earphone: cikis [1, 2]" in text and "Surround: cikis [2, 6, 8]" in text
    assert "Discord.exe, chrome.exe" in text


def test_main_refuses_politely_off_windows(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    assert probe.main(["--out", str(tmp_path / "x.json")]) == 2
    assert "yalnizca Windows" in capsys.readouterr().out
    assert not (tmp_path / "x.json").exists()


def test_main_writes_report_with_fake_registry(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "win32")
    fake = FakeWinreg(build_tree())
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    monkeypatch.setattr(probe, "running_processes", lambda *a, **k: ["SteelSeriesGG.exe"])
    monkeypatch.setattr(probe, "package_status", lambda: {"numpy": True})
    monkeypatch.setattr(probe, "portaudio_report", lambda: None)
    monkeypatch.setattr(probe, "audio_sessions", lambda: None)
    out = tmp_path / "probe.json"
    assert probe.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert {e["id"] for e in data["endpoints"]} >= {"{r-game}", "{c-mic}"}
    assert data["steelseries_processes"] == ["SteelSeriesGG.exe"]
    assert "ASM ses probu" in capsys.readouterr().out
