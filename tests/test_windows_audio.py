"""Platform-independent contract tests; real ports are verified on Windows."""
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

WINDOWS = Path(__file__).resolve().parents[1] / "bridge" / "windows"
spec = importlib.util.spec_from_file_location("virtual_audio", WINDOWS / "virtual_audio.py")
virtual_audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(virtual_audio)
sys.modules["virtual_audio"] = virtual_audio
spec = importlib.util.spec_from_file_location("audio_backend", WINDOWS / "audio_backend.py")
audio_backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio_backend)


def test_virtual_format_is_stereo_48k_32bit():
    value = virtual_audio.device_format()
    assert len(value) == 40
    assert struct.unpack_from("<HHIIHHHHI", value) == (65534, 2, 48000, 384000, 8, 32, 22, 32, 3)


def test_native_selection_follows_names_not_default_or_portaudio_indices():
    devices = {
        "microphone": {"result": 0, "names": ["physical", "another", "Steam Streaming Microphone"]},
        "speaker": {"result": 0, "names": ["Steam Streaming Speakers", "physical"]},
    }
    assert audio_backend.virtual_selectors(devices) == [3, 1]
    devices["speaker"]["names"] = ["physical"]
    with pytest.raises(RuntimeError, match="Expected one native"):
        audio_backend.virtual_selectors(devices)
    devices["speaker"]["result"] = -1
    with pytest.raises(RuntimeError, match="not ready"):
        audio_backend.virtual_selectors(devices)


def test_ambiguous_virtual_devices_are_rejected():
    with pytest.raises(RuntimeError, match="Expected one native"):
        audio_backend.virtual_selectors({"microphone": {"result": 0, "names": [
            "Steam Streaming Microphone", "Steam Streaming Microphone"]}})


def test_failed_restoration_retains_recovery_document(tmp_path):
    session = virtual_audio.VirtualAudioSession(tmp_path / "recovery.json")
    session.originals = [{"id": "virtual", "format": "00", "hidden": True}]
    session._persist()
    session._set_format = lambda *_args: (_ for _ in ()).throw(OSError("device unavailable"))
    with pytest.raises(RuntimeError, match="restoration failed"):
        session._restore()
    assert session.path.exists()
    assert session.originals


def test_successful_restoration_returns_formats_and_visibility(tmp_path):
    session = virtual_audio.VirtualAudioSession(tmp_path / "recovery.json")
    session.originals = [{"id": "virtual", "format": "0001", "hidden": True}]
    session._persist()
    operations = []
    session._set_format = lambda *args: operations.append(("format", *args))
    session._visibility = lambda *args: operations.append(("visibility", *args))
    session._restore()
    assert operations == [("format", "virtual", b"\x00\x01"), ("visibility", "virtual", False)]
    assert not session.path.exists()
    assert session.originals == []


def test_real_windows_enumeration_does_not_select_previous_physical_endpoint():
    devices = {
        "microphone": {"result": 0, "names": ["AUX (Steam Streaming Speakers)", "Mic (Steam Streaming Microphone)", "Physical mic"]},
        "speaker": {"result": 0, "names": ["Physical speaker", "Physical display", "Speaker (Steam Streaming Speakers)"]},
    }
    assert audio_backend.virtual_selectors(devices) == [2, 3]
