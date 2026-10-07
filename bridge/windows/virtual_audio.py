"""Scope format changes to existing Steam virtual endpoints and restore on exit.

Never sets a default endpoint. Drivers must already be installed by their owner.
"""
from __future__ import annotations

import ctypes
import json
import re
import struct
import sys
import uuid
from pathlib import Path

PAIRS = ("Steam Streaming Microphone", "Steam Streaming Speakers")


def discover_endpoints(pair: str):
    import winreg

    if pair not in PAIRS:
        raise ValueError("Unsupported virtual endpoint pair")
    result = []
    for flow, number in (("Render", 0), ("Capture", 1)):
        registry_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio" + "\\" + flow
        matches = []
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry_path) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                with winreg.OpenKey(root, name) as endpoint:
                    state = winreg.QueryValueEx(endpoint, "DeviceState")[0]
                    if state & 0xF != 1:
                        continue
                    with winreg.OpenKey(endpoint, "Properties") as properties:
                        values = [winreg.EnumValue(properties, i)[1]
                                  for i in range(winreg.QueryInfoKey(properties)[1])]
                    if any(isinstance(value, str) and pair in value for value in values):
                        matches.append((f"{{0.0.{number}.00000000}}.{name}", state))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one active {pair} {flow} endpoint; got {len(matches)}")
        result.append(matches[0])
    return result


def virtual_device_catalog():
    """Stable endpoint IDs for the supported, physically isolated Steam pair."""
    result = {"inputs": [], "outputs": []}
    for role, pair, flow in (("inputs", PAIRS[0], 1), ("outputs", PAIRS[1], 0)):
        try:
            identity, _ = discover_endpoints(pair)[flow]
            result[role].append({"id": identity, "name": pair})
        except (OSError, RuntimeError) as error:
            result.setdefault("errors", []).append(str(error))
    return result


def endpoint_records():
    """Driver identity and endpoint association, independent of renamed labels."""
    import winreg

    result = []
    for flow, number in (("Capture", 1), ("Render", 0)):
        path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio" + "\\" + flow
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                key = winreg.EnumKey(root, index)
                try:
                    with winreg.OpenKey(root, key) as endpoint:
                        state = winreg.QueryValueEx(endpoint, "DeviceState")[0]
                    if state & 0xF != 1: continue
                    with winreg.OpenKey(root, key + r"\Properties") as properties:
                        values = {winreg.EnumValue(properties, i)[0].casefold(): winreg.EnumValue(properties, i)[1]
                                  for i in range(winreg.QueryInfoKey(properties)[1])}
                    adapter = str(values.get("{b3f8fa53-0004-438e-9003-51a46e139bfc},6", ""))
                    label = values.get("{a45c254e-df1c-4efd-8020-67d146a850e0},14")
                    if not label:
                        label = str(values.get("{a45c254e-df1c-4efd-8020-67d146a850e0},2", "Audio device"))
                        if adapter: label += " (" + adapter + ")"
                    associations = sorted(value.casefold() for value in values.values()
                        if isinstance(value, str) and (value.startswith("{2}.") or value.startswith("{1}.")))
                    driver = " ".join(str(v) for k, v in values.items()
                                      if k != "{a45c254e-df1c-4efd-8020-67d146a850e0},14").casefold()
                    virtual = any(name in driver for name in ("steamstreaming", "steam streaming", "vb-audio", "vbaudio", "virtual audio cable"))
                    result.append({"id": f"{{0.0.{number}.00000000}}.{key}", "name": str(label),
                        "flow": number, "state": state, "virtual": virtual, "association": associations})
                except OSError:
                    continue
    return result


def audio_device_catalog():
    """All active devices plus recoverable hidden virtual ports."""
    records = endpoint_records()
    result = {"inputs": [], "outputs": [], "errors": []}
    for group, flow in (("inputs", 1), ("outputs", 0)):
        active = {item.casefold() for item in active_endpoint_ids(flow)}
        for item in records:
            if item["flow"] != flow: continue
            if item["id"].casefold() not in active and not (item["virtual"] and item["state"] & 0x10000000): continue
            result[group].append({"id": item["id"], "name": item["name"], "virtual": item["virtual"]})
        result[group].sort(key=lambda item: item["name"].casefold())
    return result


def cable_family(entry):
    # VB-CABLE uses different pin/device associations for recording and playback.
    # Preserve the A/B/C/D or numbered cable identity; do not join every VB endpoint.
    if not entry.get("virtual"): return None
    match = re.search(r"\b(cable(?:[- ][a-z]|[- ]\d+)?)\s+(?:input|output)\b",
                      entry["name"], re.IGNORECASE)
    return match.group(1).casefold().replace(" ", "-") if match else None


def select_transport(device_ids, records=None):
    records = endpoint_records() if records is None else records
    selected = {}
    for role, flow, automatic in (("input_device", 1, "steamstreamingmicrophone"),
                                  ("output_device", 0, "steamstreamingspeakers")):
        requested = device_ids.get(role, "")
        matches = [item for item in records if item["flow"] == flow and
                   (not requested or item["id"].casefold() == requested.casefold())]
        if not matches or (requested and len(matches) != 1):
            raise RuntimeError("所选音频设备不可用，请刷新设备列表并重新选择。")
        def priority(item):
            if cable_family(item) == "cable": return 0
            if any(automatic in value.replace(" ", "") for value in item["association"]): return 1
            return 2
        selected[role] = sorted(matches, key=lambda item: (priority(item), item["name"].casefold(), item["id"]))[0]
    source = selected["input_device"]
    warnings = []
    paired = [item for item in records if item["flow"] == 0 and
              set(source["association"]) & set(item["association"])]
    if source["virtual"]:
        family = cable_family(source)
        if family:
            paired = [item for item in records if item["flow"] == 0 and cable_family(item) == family]
        if len(paired) != 1:
            raise RuntimeError("所选虚拟麦克风的对应播放端口缺失或不唯一，请检查虚拟音频驱动。")
        injection = paired[0]
    else:
        # Respect an explicit physical-device choice; never switch it to Steam.
        injection = paired[0] if len(paired) == 1 else selected["output_device"]
        warnings.append("实体麦克风的语音回送取决于设备线路，可能外放或回声。")
    shared = injection["id"].casefold() == selected["output_device"]["id"].casefold()
    if shared and source["virtual"]:
        warnings.append("共用一条虚拟线路，可能回声。")
    if not selected["output_device"]["virtual"]:
        warnings.append("实体输出设备可能外放。")
    return {"input_device": source, "output_device": selected["output_device"],
            "injection_device": injection, "warnings": warnings,
            "half_duplex": shared or not source["virtual"]}


class Guid(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    def __init__(self, text):
        super().__init__((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le))


def com_method(obj, index, result_type, *args):
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *args)(table[index])


def checked(result):
    if result < 0:
        raise OSError(f"Audio policy HRESULT 0x{result & 0xFFFFFFFF:08x}")


def active_endpoint_ids(flow: int):
    """Enumerate the exact MMDevice active collection used by AVSDK hardware selection.

    Registry order and the legacy QQ name list are not MMDevice collection indexes.
    This query opens no audio streams and changes no device settings.
    """
    if flow not in (0, 1):
        raise ValueError("Expected render (0) or capture (1)")
    ole = ctypes.OleDLL("ole32")
    checked(ole.CoInitializeEx(None, 0))
    enumerator = ctypes.c_void_p()
    collection = ctypes.c_void_p()
    try:
        checked(ole.CoCreateInstance(
            ctypes.byref(Guid("bcde0395-e52f-467c-8e3d-c4579291692e")), None, 23,
            ctypes.byref(Guid("a95664d2-9614-4f35-a746-de8db63617e6")),
            ctypes.byref(enumerator)))
        checked(com_method(enumerator, 3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                           ctypes.POINTER(ctypes.c_void_p))(
                               enumerator, flow, 1, ctypes.byref(collection)))
        count = ctypes.c_uint()
        checked(com_method(collection, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(
            collection, ctypes.byref(count)))
        identities = []
        for index in range(count.value):
            device = ctypes.c_void_p()
            text = ctypes.c_void_p()
            try:
                checked(com_method(collection, 4, ctypes.c_long, ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p))(
                                       collection, index, ctypes.byref(device)))
                checked(com_method(device, 5, ctypes.c_long,
                                   ctypes.POINTER(ctypes.c_void_p))(
                                       device, ctypes.byref(text)))
                identities.append(ctypes.wstring_at(text))
            finally:
                if text:
                    ole.CoTaskMemFree(text)
                if device:
                    com_method(device, 2, ctypes.c_ulong)(device)
        return identities
    finally:
        if collection:
            com_method(collection, 2, ctypes.c_ulong)(collection)
        if enumerator:
            com_method(enumerator, 2, ctypes.c_ulong)(enumerator)
        ole.CoUninitialize()


def endpoint_selector(identity: str, identities):
    matches = [index for index, candidate in enumerate(identities)
               if candidate.casefold() == identity.casefold()]
    if len(matches) != 1:
        raise RuntimeError("Selected virtual endpoint is absent or ambiguous in MMDevice collection")
    return matches[0] + 1


def device_format():
    return (struct.pack("<HHIIHHHHI", 65534, 2, 48000, 384000, 8, 32, 22, 32, 3)
            + uuid.UUID("00000001-0000-0010-8000-00aa00389b71").bytes_le)


class VirtualAudioSession:
    def __init__(self, recovery_path: Path, *, endpoint_ids=None):
        self.path = recovery_path
        self.endpoint_ids = endpoint_ids
        self.policy = ctypes.c_void_p()
        self.originals = []
        self.ole = None
        self.ready = False

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.originals), encoding="utf-8")
        temporary.replace(self.path)

    def _set_format(self, endpoint_id, data):
        setter = com_method(self.policy, 6, ctypes.c_long, ctypes.c_wchar_p,
                            ctypes.c_void_p, ctypes.c_void_p)
        desired = ctypes.create_string_buffer(data)
        previous = ctypes.create_string_buffer(40)
        checked(setter(self.policy, endpoint_id, desired, previous))

    def _visibility(self, endpoint_id, visible):
        # IUnknown 0..2, IPolicyConfig SetEndpointVisibility at 14.
        setter = com_method(self.policy, 14, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)
        checked(setter(self.policy, endpoint_id, int(visible)))

    def _restore(self):
        errors = []
        for entry in reversed(self.originals):
            try:
                if "format" in entry:
                    self._set_format(entry["id"], bytes.fromhex(entry["format"]))
                if entry["hidden"]:
                    self._visibility(entry["id"], False)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("Virtual audio restoration failed: " + "; ".join(errors))
        self.originals = []
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows virtual audio requires Windows")
        self.ole = ctypes.OleDLL("ole32")
        checked(self.ole.CoInitializeEx(None, 0))
        try:
            checked(self.ole.CoCreateInstance(
                ctypes.byref(Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")), None, 23,
                ctypes.byref(Guid("f8679f50-850a-41cf-9c72-430f290290c8")),
                ctypes.byref(self.policy)))
            if self.path.exists():
                self.originals = json.loads(self.path.read_text(encoding="utf-8"))
                allowed = {entry["id"] for entry in endpoint_records() if entry["virtual"]}
                if any(entry["id"] not in allowed for entry in self.originals):
                    raise RuntimeError("Recovery file contains an unknown virtual endpoint")
                self._restore()
            if self.endpoint_ids is not None:
                records = {entry["id"].casefold(): entry for entry in endpoint_records()}
                for identity in dict.fromkeys(self.endpoint_ids):
                    entry = records.get(identity.casefold())
                    if entry is None:
                        raise RuntimeError("所选音频端口已离线，请刷新设备列表。")
                    if entry["state"] & 0x10000000:
                        if not entry["virtual"]: raise RuntimeError("不能自动启用隐藏的实体音频设备。")
                        self.originals.append({"id": entry["id"], "hidden": True})
                if self.originals:
                    self._persist()
                    for entry in self.originals: self._visibility(entry["id"], True)
                self.ready = True
                return self
            getter = com_method(self.policy, 4, ctypes.c_long, ctypes.c_wchar_p,
                                ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            # Snapshot all four ports before modifying any of them.
            for pair in PAIRS:
                for endpoint_id, state in discover_endpoints(pair):
                    pointer = ctypes.c_void_p()
                    checked(getter(self.policy, endpoint_id, 0, ctypes.byref(pointer)))
                    try:
                        header = ctypes.string_at(pointer, 18)
                        extra = struct.unpack_from("<H", header, 16)[0]
                        original = ctypes.string_at(pointer, 18 + extra)
                    finally:
                        self.ole.CoTaskMemFree(pointer)
                    self.originals.append({"id": endpoint_id, "format": original.hex(),
                                           "hidden": bool(state & 0x10000000)})
            self._persist()
            for entry in self.originals:
                if entry["hidden"]:
                    self._visibility(entry["id"], True)
                self._set_format(entry["id"], device_format())
            self.ready = True
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *_args):
        self.ready = False
        try:
            if self.policy and self.originals:
                self._restore()
        finally:
            if self.policy:
                com_method(self.policy, 2, ctypes.c_ulong)(self.policy)
                self.policy = ctypes.c_void_p()
            if self.ole:
                self.ole.CoUninitialize()
                self.ole = None
