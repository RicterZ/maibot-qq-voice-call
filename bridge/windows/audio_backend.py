"""Windows adapter for the Momoi media protocol; no conversation state here."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from virtual_audio import (VirtualAudioSession, audio_device_catalog, PAIRS,
                           active_endpoint_ids, endpoint_selector)



class WindowsAudioBackend:
    def __init__(self, runtime: Path, *, python=None, device_ids=None):
        self.device_ids = device_ids or {}
        self.device_selection = {}
        self.session = VirtualAudioSession(runtime / "audio-recovery.json")
        executable = python or sys.executable
        script = str(Path(__file__).with_name("audio_stream.py"))
        self.capture_command = (executable, "-B", "-X", "utf8", script, "capture")
        self.playback_command = (executable, "-B", "-X", "utf8", script, "playback")
        self.selected = False

    @property
    def ready(self):
        return self.session.ready and self.selected

    async def prepare(self, http, host_url, token):
        catalog = audio_device_catalog()
        for role, group in (("input_device", "inputs"), ("output_device", "outputs")):
            requested = self.device_ids.get(role, "")
            choices = catalog[group]
            automatic = PAIRS[0] if role == "input_device" else PAIRS[1]
            matches = [item for item in choices if
                       (item["id"] == requested if requested else automatic in item["name"])]
            if len(matches) != 1:
                raise RuntimeError(f"Selected {role} is unavailable; no default-device fallback")
            self.device_selection[role] = matches[0]
        import sounddevice as sd
        from audio_stream import endpoint

        # Explicit virtual ports only; fail before arming if WASAPI cannot open either.
        settings = dict(samplerate=48000, channels=2, dtype="float32", blocksize=960,
                        latency="low", extra_settings=sd.WasapiSettings())
        with sd.InputStream(device=endpoint(sd, "Steam Streaming Speakers", "input"), **settings):
            pass
        with sd.OutputStream(
                device=endpoint(sd, "Steam Streaming Microphone", "output"), **settings):
            pass
        headers = {"Authorization": "Bearer " + token}
        async def invoke(command, params):
            async with http.post(host_url + "/v1/invoke", headers=headers,
                                 json={"command": command, "params": params}) as response:
                response.raise_for_status()
        # Commands 64/65 enumerate the legacy record/playout core. Command 102
        # selects through QRTC hardware, which uses IMMDevice active collection
        # indexes. Those lists can have different orders on the same machine.
        selectors = [endpoint_selector(self.device_selection[role]["id"],
                                       active_endpoint_ids(flow))
                     for role, flow in (("input_device", 1), ("output_device", 0))]
        print(__import__("json").dumps({"event": "qq_call_devices_selected",
              "microphone_selector": selectors[0], "speaker_selector": selectors[1],
              "devices": self.device_selection}),
              file=sys.stderr, flush=True)
        await invoke(102, selectors)
        for _ in range(50):
            async with http.get(host_url + "/v1/status", headers=headers) as response:
                response.raise_for_status()
                data = (await response.json())["data"]
            selection = (data.get("audioDevices") or {}).get("selection") or {}
            if selection.get("result") == 1:
                self.selected = True
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("QQ virtual audio selection was not acknowledged")

    def __enter__(self):
        self.session.__enter__()
        return self

    def __exit__(self, *args):
        self.selected = False
        return self.session.__exit__(*args)
