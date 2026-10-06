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
