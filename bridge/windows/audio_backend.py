"""Windows adapter for the Momoi media protocol; no conversation state here."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from virtual_audio import VirtualAudioSession


def virtual_selectors(devices):
    result = []
    for role, pair in (("microphone", "Steam Streaming Microphone"),
                       ("speaker", "Steam Streaming Speakers")):
        entry = devices.get(role) or {}
        if entry.get("result") != 0:
            raise RuntimeError("Native audio enumeration is not ready")
        matches = [index for index, name in enumerate(entry.get("names") or []) if pair in name]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one native {pair} endpoint")
        result.append(matches[0])
    return result


class WindowsAudioBackend:
    def __init__(self, runtime: Path, *, python=None):
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
        for command in (64, 65):
            await invoke(command, [])
        selectors = None
        for _ in range(50):
            async with http.get(host_url + "/v1/status", headers=headers) as response:
                response.raise_for_status()
                data = (await response.json())["data"]
            try:
                selectors = virtual_selectors(data.get("audioDevices") or {})
                break
            except RuntimeError:
                await asyncio.sleep(0.1)
        if selectors is None:
            raise RuntimeError("QQ cannot enumerate the prepared virtual audio pair")
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
