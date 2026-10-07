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


def records():
    def entry(identity, name, flow, association, virtual=True):
        return {"id": identity, "name": name, "flow": flow, "association": [association],
                "virtual": virtual, "state": 1}
    return [entry("steam-mic", "Renamed virtual mic", 1, "steamstreamingmicrophone"),
            entry("inject", "Renamed virtual render", 0, "steamstreamingmicrophone"),
            entry("steam-speakers", "Renamed virtual output", 0, "steamstreamingspeakers"),
            entry("physical", "USB Microphone", 1, "usb-device", False),
            entry("edifier", "EDIFIER", 0, "usb-device", False)]


def test_selected_transport_uses_id_and_driver_association_not_names():
    plan = virtual_audio.select_transport({}, records())
    assert plan["injection_device"]["id"] == "inject"
    assert plan["output_device"]["id"] == "steam-speakers"
    assert not plan["warnings"]
    plan = virtual_audio.select_transport({"input_device": "steam-mic", "output_device": "edifier"}, records())
    assert plan["injection_device"]["id"] == "inject"
    assert plan["output_device"]["id"] == "edifier"
    assert plan["warnings"]


def test_physical_devices_warn_but_are_never_rejected_or_replaced():
    plan = virtual_audio.select_transport({"input_device": "physical", "output_device": "edifier"}, records())
    assert plan["input_device"]["id"] == "physical"
    assert plan["injection_device"]["id"] == "edifier"
    assert plan["warnings"]
    assert plan["half_duplex"]


def test_shared_virtual_line_warns_instead_of_refusing():
    plan = virtual_audio.select_transport({"output_device": "inject"}, records())
    assert plan["output_device"]["id"] == "inject"
    assert plan["warnings"] and plan["half_duplex"]


def test_disconnected_device_and_ambiguous_pair_do_not_fall_back():
    with pytest.raises(RuntimeError):
        virtual_audio.select_transport({"input_device": "missing"}, records())
    duplicate = {**records()[1], "id": "duplicate"}
    with pytest.raises(RuntimeError):
        virtual_audio.select_transport({}, [*records(), duplicate])


def test_prepare_uses_selected_ids_for_both_pcm_and_qq(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    operations = []
    class Stream:
        def __init__(self, identity, *, capture): operations.append((identity, capture))
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Session:
        ready = True
        def __init__(self, path, *, endpoint_ids): operations.append(tuple(endpoint_ids))
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setitem(sys.modules, "wasapi", SimpleNamespace(EndpointStream=Stream))
    monkeypatch.setattr(audio_backend, "VirtualAudioSession", Session)
    monkeypatch.setattr(audio_backend, "select_transport", lambda value: virtual_audio.select_transport(value, records()))
    monkeypatch.setattr(audio_backend, "active_endpoint_ids", lambda flow:
        ["aux", "steam-mic", "physical"] if flow else ["display", "steam-speakers", "edifier"])
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
    backend = audio_backend.WindowsAudioBackend(tmp_path, device_ids={"output_device": "edifier"})
    with backend:
        asyncio.run(backend.prepare(Http(), "http://localhost", "test"))
        assert calls == [{"command": 102, "params": [2, 3]}]
        assert backend.capture_command[-1] == "edifier"
        assert backend.playback_command[-1] == "inject"
        assert ("edifier", True) in operations
        assert ("inject", False) in operations
        assert backend.ready and backend.warnings


def test_resampling_chunk_boundaries_are_continuous():
    import numpy as np
    spec = importlib.util.spec_from_file_location("audio_stream", WINDOWS / "audio_stream.py")
    stream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stream)
    for source, target in ((24000, 48000), (48000, 16000), (44100, 16000), (24000, 44100)):
        signal = np.sin(np.arange(source // 10) * 2 * np.pi * 440 / source)
        whole = stream.Resampler(source, target).feed(signal)
        converter = stream.Resampler(source, target)
        chunks = np.concatenate([converter.feed(part) for part in np.array_split(signal, 19)])
        assert np.allclose(whole, chunks)
        assert abs(len(whole) - target // 10) <= 2


def test_cable_input_output_pair_uses_cable_identity_not_pin_association():
    def port(identity, name, flow, association):
        return {"id": identity, "name": name, "flow": flow, "association": [association], "virtual": True, "state": 1}
    devices = [port("cable-out", "CABLE Output (VB-Audio Virtual Cable)", 1, "record-pin"),
               port("cable-in", "CABLE Input (VB-Audio Virtual Cable)", 0, "render-pin"),
               port("a-out", "CABLE-A Output (VB-Audio Cable A)", 1, "another-record"),
               port("a-in", "CABLE-A Input (VB-Audio Cable A)", 0, "another-render")]
    plan = virtual_audio.select_transport({"input_device": "cable-out", "output_device": "cable-in"}, devices)
    assert plan["injection_device"]["id"] == "cable-in"
    assert plan["half_duplex"]
    plan = virtual_audio.select_transport({"input_device": "a-out", "output_device": "cable-in"}, devices)
    assert plan["injection_device"]["id"] == "a-in"
    assert not plan["half_duplex"]
