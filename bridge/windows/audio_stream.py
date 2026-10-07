"""PCM pipes bound to explicit endpoint IDs; receive uses render loopback."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np


class Resampler:
    """Continuous sample positions across chunks, with anti-aliasing on downsample."""
    def __init__(self, source_rate, target_rate):
        self.step = source_rate / target_rate
        self.position = 0.0
        self.total = 0
        self.last = 0.0
        self.taps = None
        if target_rate < source_rate:
            cutoff = .45 * target_rate / source_rate
            x = np.arange(63) - 31
            self.taps = 2 * cutoff * np.sinc(2 * cutoff * x) * np.hamming(63)
            self.taps /= self.taps.sum()
            self.history = np.zeros(62)

    def feed(self, values):
        if not len(values): return np.empty(0)
        values = np.asarray(values, dtype=np.float64)
        if self.taps is not None:
            extended = np.concatenate((self.history, values))
            values = np.convolve(extended, self.taps, mode="valid")
            self.history = extended[-62:]
        samples = np.concatenate(([self.last], values))
        end = self.total + len(values) - 1
        count = max(0, int(np.floor((end - self.position) / self.step + 1e-9)) + 1)
        positions = self.position + np.arange(count) * self.step
        result = np.interp(positions - (self.total - 1), np.arange(len(samples)), samples)
        self.position += count * self.step
        self.total += len(values)
        self.last = float(values[-1])
        return result


def run(mode, identity):
    from wasapi import EndpointStream

    with EndpointStream(identity, capture=mode == "capture") as device:
        if mode == "capture":
            convert = Resampler(device.rate, 16000)
            pending = b""
            idle_at = time.monotonic()
            while True:
                values = device.read()
                if values is None:
                    # Keep protocol frames moving even when no render stream is active.
                    if time.monotonic() - idle_at < .02:
                        time.sleep(.003)
                        continue
                    pending += b"\0" * 640
                    idle_at = time.monotonic()
                else:
                    idle_at = time.monotonic()
                    mono = convert.feed(values)
                    pending += (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
                count = len(pending) // 640 * 640
                if count:
                    sys.stdout.buffer.write(pending[:count])
                    sys.stdout.buffer.flush()
                    pending = pending[count:]
        else:
            convert = Resampler(24000, device.rate)
            pending = b""
            while True:
                chunk = sys.stdin.buffer.read(640)
                if not chunk: break
                pending += chunk
                count = len(pending) // 2 * 2
                if not count: continue
                mono = np.frombuffer(pending[:count], dtype="<i2").astype(np.float64) / 32768
                pending = pending[count:]
                device.write(convert.feed(mono))
            if pending: raise RuntimeError("Incomplete PCM sample")
            device.write(convert.feed(np.array([convert.last])))
            device.drain()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "playback"))
    parser.add_argument("--endpoint-id", required=True)
    args = parser.parse_args()
    run(args.mode, args.endpoint_id)
