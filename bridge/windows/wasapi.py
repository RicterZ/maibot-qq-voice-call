"""Shared-mode WASAPI streams addressed by IMMDevice ID, never system defaults."""
from __future__ import annotations

import ctypes
import struct
import time

import numpy as np

from virtual_audio import Guid, checked, com_method


class EndpointStream:
    def __init__(self, identity, *, capture):
        self.identity, self.capture = identity, capture
        self.ole = None
        self.enumerator = self.device = self.client = self.service = ctypes.c_void_p()
        self.started = False

    def __enter__(self):
        self.ole = ctypes.OleDLL("ole32")
        checked(self.ole.CoInitializeEx(None, 0))
        try:
            self.enumerator, self.device, self.client, self.service = (ctypes.c_void_p() for _ in range(4))
            checked(self.ole.CoCreateInstance(ctypes.byref(Guid("bcde0395-e52f-467c-8e3d-c4579291692e")), None, 23,
                ctypes.byref(Guid("a95664d2-9614-4f35-a746-de8db63617e6")), ctypes.byref(self.enumerator)))
            checked(com_method(self.enumerator, 5, ctypes.c_long, ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_void_p))(self.enumerator, self.identity, ctypes.byref(self.device)))
            checked(com_method(self.device, 3, ctypes.c_long, ctypes.POINTER(Guid), ctypes.c_ulong,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(self.device,
                ctypes.byref(Guid("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")), 23, None, ctypes.byref(self.client)))
            pointer = ctypes.c_void_p()
            checked(com_method(self.client, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(self.client, ctypes.byref(pointer)))
            try:
                header = ctypes.string_at(pointer, 18)
                self.tag, self.channels, self.rate, _, self.align, self.bits, extra = struct.unpack("<HHIIHHH", header)
                self.format = ctypes.string_at(pointer, 18 + extra)
            finally:
                self.ole.CoTaskMemFree(pointer)
            if self.tag == 65534:
                if len(self.format) < 40:
                    raise RuntimeError("Invalid extensible audio format")
                subtype = self.format[24:40]
                if subtype == bytes(Guid("00000003-0000-0010-8000-00aa00389b71").bytes): self.tag = 3
                elif subtype == bytes(Guid("00000001-0000-0010-8000-00aa00389b71").bytes): self.tag = 1
                else: raise RuntimeError("Unsupported audio mix subtype")
            if self.channels < 1 or self.align != self.channels * (self.bits // 8) or self.rate < 8000:
                raise RuntimeError("Unsupported audio mix layout")
            if (self.tag, self.bits) not in ((3, 32), (1, 16), (1, 24), (1, 32)):
                raise RuntimeError(f"Unsupported audio mix format {self.tag}/{self.bits}")
            blob = ctypes.create_string_buffer(self.format)
            checked(com_method(self.client, 3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)(self.client, 0,
                0x20000 if self.capture else 0, 1000000, 0, blob, None))
            iid = "c8adbd64-e71e-48a0-a4de-185c395cd317" if self.capture else "f294acfc-3146-4483-a7bf-addca7c260e2"
            checked(com_method(self.client, 14, ctypes.c_long, ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p))(
                self.client, ctypes.byref(Guid(iid)), ctypes.byref(self.service)))
            size = ctypes.c_uint()
            checked(com_method(self.client, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(self.client, ctypes.byref(size)))
            self.buffer_size = size.value
            checked(com_method(self.client, 10, ctypes.c_long)(self.client))
            self.started = True
            return self
        except BaseException:
            self.__exit__()
            raise

    def read(self):
        frames = ctypes.c_uint()
        checked(com_method(self.service, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(self.service, ctypes.byref(frames)))
        if not frames.value: return None
        pointer, count, flags = ctypes.c_void_p(), ctypes.c_uint(), ctypes.c_ulong()
        checked(com_method(self.service, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_void_p)(
            self.service, ctypes.byref(pointer), ctypes.byref(count), ctypes.byref(flags), None, None))
        try:
            if flags.value & 2: return np.zeros(count.value, dtype=np.float64)
            raw = ctypes.string_at(pointer, count.value * self.align)
            if self.tag == 3: values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
            elif self.bits == 16: values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
            elif self.bits == 32: values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648
            else:
                b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
                packed = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
                values = ((packed ^ 0x800000) - 0x800000).astype(np.float64) / 8388608
            return values.reshape(-1, self.channels).mean(axis=1)
        finally:
            checked(com_method(self.service, 4, ctypes.c_long, ctypes.c_uint)(self.service, count.value))

    def write(self, mono):
        values = np.clip(np.repeat(mono[:, None], self.channels, axis=1), -1, 1)
        if self.tag == 3: raw = values.astype("<f4").tobytes()
        elif self.bits == 16: raw = (values * 32767).astype("<i2").tobytes()
        elif self.bits == 32: raw = (values * 2147483647).astype("<i4").tobytes()
        else:
            packed = (values * 8388607).astype("<i4").reshape(-1).view(np.uint8).reshape(-1, 4)
            raw = packed[:, :3].copy().tobytes()
        offset = 0
        while offset < len(mono):
            padding = ctypes.c_uint()
            checked(com_method(self.client, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(self.client, ctypes.byref(padding)))
            count = min(self.buffer_size - padding.value, len(mono) - offset)
            if count <= 0:
                time.sleep(.003)
                continue
            pointer = ctypes.c_void_p()
            checked(com_method(self.service, 3, ctypes.c_long, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p))(self.service, count, ctypes.byref(pointer)))
            ctypes.memmove(pointer, raw[offset * self.align:(offset + count) * self.align], count * self.align)
            checked(com_method(self.service, 4, ctypes.c_long, ctypes.c_uint, ctypes.c_ulong)(self.service, count, 0))
            offset += count

    def drain(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            padding = ctypes.c_uint()
            checked(com_method(self.client, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(self.client, ctypes.byref(padding)))
            if not padding.value: return
            time.sleep(.003)
        raise RuntimeError("Audio render buffer did not drain")

    def __exit__(self, *_):
        if self.started:
            com_method(self.client, 11, ctypes.c_long)(self.client)
            self.started = False
        for value in (self.service, self.client, self.device, self.enumerator):
            if value: com_method(value, 2, ctypes.c_ulong)(value)
        self.enumerator = self.device = self.client = self.service = ctypes.c_void_p()
        if self.ole:
            self.ole.CoUninitialize()
            self.ole = None
