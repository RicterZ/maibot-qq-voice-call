"""QQ device selection and PCM transport share the same stable endpoint identities."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from virtual_audio import VirtualAudioSession, active_endpoint_ids, endpoint_selector, select_transport


class WindowsAudioBackend:
    def __init__(self, runtime: Path, *, python=None, device_ids=None):
        self.runtime = runtime
        self.python = python or sys.executable
        self.device_ids = device_ids or {}
        self.device_selection = {}
        self.warnings = []
        self.half_duplex = False
        self.session = None
        self.capture_command = self.playback_command = ()
        self.selected = False

    @property
    def ready(self):
        return bool(self.session and self.session.ready and self.selected)

    def __enter__(self):
        plan = select_transport(self.device_ids)
        self.device_selection = {key: {"id": plan[key]["id"], "name": plan[key]["name"]}
                                 for key in ("input_device", "output_device", "injection_device")}
        self.warnings, self.half_duplex = plan["warnings"], plan["half_duplex"]
        self.session = VirtualAudioSession(self.runtime / "audio-recovery.json",
            endpoint_ids=[item["id"] for item in self.device_selection.values()])
        self.session.__enter__()
        script = str(Path(__file__).with_name("audio_stream.py"))
        prefix = (self.python, "-B", "-X", "utf8", script)
        self.capture_command = (*prefix, "capture", "--endpoint-id", plan["output_device"]["id"])
        self.playback_command = (*prefix, "playback", "--endpoint-id", plan["injection_device"]["id"])
        return self

    async def prepare(self, http, host_url, token):
        from wasapi import EndpointStream

        # Open exact selected endpoints without emitting test sounds or changing formats.
        with EndpointStream(self.device_selection["output_device"]["id"], capture=True): pass
        with EndpointStream(self.device_selection["injection_device"]["id"], capture=False): pass
        selectors = [endpoint_selector(self.device_selection[role]["id"], active_endpoint_ids(flow))
                     for role, flow in (("input_device", 1), ("output_device", 0))]
        print(json.dumps({"event": "qq_call_devices_selected", "microphone_selector": selectors[0],
            "speaker_selector": selectors[1], "devices": self.device_selection,
            "warnings": self.warnings, "half_duplex": self.half_duplex}), file=sys.stderr, flush=True)
        headers = {"Authorization": "Bearer " + token}
        async with http.post(host_url + "/v1/invoke", headers=headers,
            json={"command": 102, "params": selectors}) as response:
            response.raise_for_status()
        for _ in range(50):
            async with http.get(host_url + "/v1/status", headers=headers) as response:
                response.raise_for_status()
                data = (await response.json())["data"]
            selection = (data.get("audioDevices") or {}).get("selection") or {}
            if selection.get("result") == 1:
                self.selected = True
                return
            await asyncio.sleep(.1)
        raise RuntimeError("QQ 音频设备选择未确认，请重新应用设备。")

    def __exit__(self, *args):
        self.selected = False
        if self.session:
            return self.session.__exit__(*args)
