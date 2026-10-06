"""Export only the selected platform's runtime code (no native binaries/tests)."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bundle_files(platform: str, root: Path = ROOT) -> dict[str, bytes]:
    if platform not in ("windows", "linux"):
        raise ValueError("unsupported platform: " + platform)
    files = {"LICENSE": (root / "LICENSE").read_bytes()}
    common = (
        "av-host/host.cjs",
        "av-host/commands.cjs",
        "av-host/host.html",
        "napcat-plugin/index.mjs",
        "napcat-plugin/package.json",
    )
    for name in common:
        files[name] = (root / "bridge" / name).read_bytes()
    if platform == "windows":
        names = ("windows/start-av-host.ps1", "windows/components.json",
                 "windows/virtual_audio.py", "windows/audio_stream.py", "windows/audio_backend.py")
    else:
        names = tuple(
            p.relative_to(root / "bridge").as_posix()
            for p in sorted((root / "bridge/scripts").glob("*.sh"))
        )
    for name in names:
        files[name] = (root / "bridge" / name).read_bytes()
    files["bundle.json"] = json.dumps(
        {
            "format_version": 1,
            "platform": platform,
            "files": {
                name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())
            },
        },
        sort_keys=True,
        indent=2,
    ).encode()
    return files


def build_bundle(platform: str, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(bundle_files(platform).items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows", "linux"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(build_bundle(args.platform, args.output))
