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


def test_hardware_selector_uses_endpoint_identity_and_one_based_mmdevice_index():
    assert virtual_audio.endpoint_selector("STEAM-MIC", ["aux", "steam-mic", "physical"]) == 2
    assert virtual_audio.endpoint_selector("steam-speakers", ["display", "steam-speakers", "physical"]) == 2
    with pytest.raises(RuntimeError, match="absent or ambiguous"):
        virtual_audio.endpoint_selector("missing", ["physical"])
    with pytest.raises(RuntimeError, match="absent or ambiguous"):
        virtual_audio.endpoint_selector("same", ["same", "SAME"])


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


def test_real_windows_legacy_list_order_must_not_select_physical_speaker():
    # Observed on Windows: legacy 65 names = EDIFIER, display, Steam, display, SteamMic.
    # Command 102 uses MMDevice collection = display, Steam, EDIFIER, display, SteamMic.
    # Using legacy index + 1 (=3) opens EDIFIER; identity maps correctly to selector 2.
    identities = ["display-1", "steam-speakers", "edifier", "display-2", "steam-mic"]
    assert virtual_audio.endpoint_selector("steam-speakers", identities) == 2
    assert identities[3 - 1] == "edifier"


def test_prepare_uses_hardware_id_without_initializing_legacy_audio(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    class Stream:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        InputStream=Stream, OutputStream=Stream, WasapiSettings=lambda: None))
    monkeypatch.setitem(sys.modules, "audio_stream", SimpleNamespace(endpoint=lambda *args: 1))
    monkeypatch.setattr(audio_backend, "audio_device_catalog", lambda: {
        "inputs": [{"id": "physical", "name": "USB Microphone"}, {"id": "steam-mic", "name": "Steam Streaming Microphone"}],
        "outputs": [{"id": "edifier", "name": "EDIFIER"}, {"id": "steam-speakers", "name": "Steam Streaming Speakers"}]})
    monkeypatch.setattr(audio_backend, "active_endpoint_ids", lambda flow:
        ["aux", "steam-mic", "physical"] if flow else
        ["display", "steam-speakers", "edifier"])
    calls = []

    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def json(self): return {"data": {"audioDevices": {"selection": {"result": 1}}}}

    class Http:
        def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            return Response()
        def get(self, *args, **kwargs): return Response()

    backend = audio_backend.WindowsAudioBackend(tmp_path)
    asyncio.run(backend.prepare(Http(), "http://localhost", "test"))
    assert calls == [{"command": 102, "params": [2, 2]}]
    assert backend.selected
    backend = audio_backend.WindowsAudioBackend(tmp_path, device_ids={"input_device": "missing"})
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(backend.prepare(Http(), "http://localhost", "test"))
    assert len(calls) == 1

    backend = audio_backend.WindowsAudioBackend(tmp_path, device_ids={"input_device": "physical", "output_device": "edifier"})
    asyncio.run(backend.prepare(Http(), "http://localhost", "test"))
    assert calls[-1] == {"command": 102, "params": [3, 3]}
    assert backend.device_selection["input_device"]["id"] == "physical"
