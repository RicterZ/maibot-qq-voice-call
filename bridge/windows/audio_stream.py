"""Pipe PCM to/from explicitly selected virtual ports; no default devices."""
import argparse
import sys


def endpoint(sounddevice, pair, role):
    apis = sounddevice.query_hostapis()
    devices = sounddevice.query_devices()
    matches = [i for i, device in enumerate(devices)
               if apis[device["hostapi"]]["name"] == "Windows WASAPI"
               and pair in device["name"] and device[f"max_{role}_channels"] >= 2
               and device["default_samplerate"] == 48000]
    if len(matches) != 1:
        raise RuntimeError(f"No unique prepared {pair} {role} endpoint")
    return matches[0]


def run(mode):
    import numpy as np
    import sounddevice as sd

    pair = "Steam Streaming Speakers" if mode == "capture" else "Steam Streaming Microphone"
    role = "input" if mode == "capture" else "output"
    device = endpoint(sd, pair, role)
    settings = dict(device=device, samplerate=48000, channels=2, dtype="float32",
                    blocksize=960, latency="low", extra_settings=sd.WasapiSettings())
    if mode == "capture":
        # Low-pass before 48k->16k decimation. Keep FIR history across chunks.
        taps = np.sinc((np.arange(63) - 31) / 3) * np.hamming(63)
        taps /= taps.sum()
        history = np.zeros(62)
        with sd.InputStream(**settings) as stream:
            while True:
                data, overflow = stream.read(960)
                if overflow:
                    raise RuntimeError("Virtual audio capture overflow")
                mono = data.mean(axis=1)
                extended = np.concatenate((history, mono))
                filtered = np.convolve(extended, taps, mode="valid")[::3]
                history = extended[-62:]
                pcm = (np.clip(filtered, -1, 1) * 32767).astype("<i2")
                sys.stdout.buffer.write(pcm.tobytes())
                sys.stdout.buffer.flush()
    else:
        pending = b""
        last = 0.0
        with sd.OutputStream(**settings) as stream:
            while True:
                chunk = sys.stdin.buffer.read(960)
                if not chunk:
                    if pending:
                        raise RuntimeError("Incomplete PCM sample")
                    break
                pending += chunk
                count = len(pending) // 2 * 2
                if not count:
                    continue
                mono = np.frombuffer(pending[:count], dtype="<i2").astype(np.float32) / 32768
                pending = pending[count:]
                previous = np.concatenate(([last], mono[:-1]))
                doubled = np.empty(len(mono) * 2, dtype=np.float32)
                doubled[::2] = (previous + mono) / 2
                doubled[1::2] = mono
                last = float(mono[-1])
                stereo = np.repeat(doubled[:, None], 2, axis=1)
                if stream.write(stereo):
                    raise RuntimeError("Virtual audio playback underflow")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "playback"))
    run(parser.parse_args().mode)
