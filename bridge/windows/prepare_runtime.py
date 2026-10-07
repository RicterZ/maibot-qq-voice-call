"""Stage the pinned Windows QQ host and loader independently of bridge code."""

import argparse
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

COMPONENTS = Path(__file__).with_name("components.json")


def download(component, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".partial")
        try:
            with (
                urllib.request.urlopen(component["url"], timeout=120) as source,
                temporary.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    with destination.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != component["sha256"]:
        raise ValueError("SHA256 mismatch: " + str(destination))
    return destination


def copy_qq_runtime(source: Path, destination: Path):
    """Keep the Electron/AVSDK host, excluding QQ business modules at its app boundary."""
    version = json.loads(COMPONENTS.read_text(encoding="utf-8"))["qq"]["version"]
    app = source / "versions" / version / "resources/app"

    def excluded(directory, names):
        excluded_names = {"wmpfsdk", "miniapp", "QQScreenShot", "major.node", "wrapper.node", "application.asar"}
        return excluded_names.intersection(names) if Path(directory) == app else set()

    shutil.copytree(source, destination, ignore=excluded)


def prepare(output: Path, cache: Path, sevenzip="7z"):
    components = json.loads(COMPONENTS.read_text(encoding="utf-8"))
    installer = download(components["qq"], cache / ("QQ-" + components["qq"]["version"] + ".exe"))
    extracted = cache / ("QQ-" + components["qq"]["version"])
    if extracted.exists():
        shutil.rmtree(extracted)
    subprocess.run(
        [sevenzip, "x", "-y", "-o" + str(extracted), str(installer)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    # Preserve the native Electron/AVSDK host closure. The custom bootstrap does
    # not load QQ business modules; NapCat ships its own separate wrapper.node.
    # The Electron QQNT.dll cannot be replaced by the NapCat Node QQNT.dll.
    qq = output / "qq/Files"
    if qq.exists():
        shutil.rmtree(qq)
    qq.parent.mkdir(parents=True, exist_ok=True)
    copy_qq_runtime(extracted / "Files", qq)
    for name, expected in components["loader"]["files"].items():
        source = download(
            {"url": components["loader"]["base_url"] + name, "sha256": expected}, cache / name
        )
        destination = output / "loader" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    app = qq / "versions" / components["qq"]["version"] / "resources/app"
    for file in (qq / "QQ.exe", app / "avsdk/AVSDKPlugin.dll", app / "package.json"):
        if not file.is_file():
            raise FileNotFoundError(file)
    (output / "components.json").write_text(json.dumps(components, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--sevenzip", default="7z")
    args = parser.parse_args()
    print(prepare(args.output, args.cache, args.sevenzip))
