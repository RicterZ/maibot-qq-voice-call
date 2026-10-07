"""Scope format changes to existing Steam virtual endpoints and restore on exit.

Never sets a default endpoint. Drivers must already be installed by their owner.
"""
from __future__ import annotations

import ctypes
import json
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


def audio_device_catalog():
    """All active Windows recording/playback endpoints, keyed by stable IDs.

    QQ routing may target any device. The bridge's internal virtual transport
    is separate and continues to use the prepared Steam ports.
    """
    import winreg

    result = {"inputs": [], "outputs": [], "errors": []}
    for group, flow, number in (("inputs", "Capture", 1), ("outputs", "Render", 0)):
        try:
            identities = {item.casefold() for item in active_endpoint_ids(number)}
            path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio" + "\\" + flow
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    key = winreg.EnumKey(root, index)
                    identity = f"{{0.0.{number}.00000000}}.{key}"
                    if identity.casefold() not in identities:
                        continue
                    try:
                        with winreg.OpenKey(root, key + r"\Properties") as properties:
                            try:
                                label = winreg.QueryValueEx(properties, "{a45c254e-df1c-4efd-8020-67d146a850e0},14")[0]
                            except OSError:
                                label = winreg.QueryValueEx(properties, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")[0]
                                try:
                                    adapter = winreg.QueryValueEx(properties, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")[0]
                                    label = f"{label} ({adapter})"
                                except OSError:
                                    pass
                        result[group].append({"id": identity, "name": str(label)})
                    except OSError as error:
                        result["errors"].append(str(error))
            result[group].sort(key=lambda item: item["name"].casefold())
        except (OSError, RuntimeError) as error:
            result["errors"].append(str(error))
    return result


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
    def __init__(self, recovery_path: Path):
        self.path = recovery_path
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
                allowed = {endpoint for pair in PAIRS for endpoint, _ in discover_endpoints(pair)}
                if any(entry["id"] not in allowed for entry in self.originals):
                    raise RuntimeError("Recovery file contains an unknown virtual endpoint")
                self._restore()
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
