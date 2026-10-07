"""Distribution must select the platform, rather than copy the entire bridge."""

import importlib.util
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge_bundle", ROOT / "bridge/build_bundle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_windows_bundle_excludes_linux_and_diagnostics(tmp_path):
    archive = module.build_bundle("windows", tmp_path / "windows.zip")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "windows/start-av-host.ps1" in names
        assert "av-host/host.cjs" in names
        assert "napcat-plugin/index.mjs" in names
        assert "LICENSE" in names
        assert not any(
            n.startswith("scripts/") or n.endswith(".sh") or "/probe/" in n for n in names
        )
        manifest = json.loads(bundle.read("bundle.json"))
        assert manifest["platform"] == "windows"
        assert set(manifest["files"]) == set(names) - {"bundle.json"}
    second = module.build_bundle("windows", tmp_path / "again.zip")
    assert archive.read_bytes() == second.read_bytes()


def test_linux_bundle_keeps_existing_launchers_without_windows(tmp_path):
    with zipfile.ZipFile(module.build_bundle("linux", tmp_path / "linux.zip")) as bundle:
        assert "scripts/run-av-host.sh" in bundle.namelist()
        assert "scripts/audio-control.sh" in bundle.namelist()
        assert not any(n.startswith("windows/") for n in bundle.namelist())


def test_native_runtime_excludes_miniapps_but_preserves_avsdk(tmp_path):
    native_spec = importlib.util.spec_from_file_location(
        "prepare_runtime", ROOT / "bridge/windows/prepare_runtime.py"
    )
    native = importlib.util.module_from_spec(native_spec)
    native_spec.loader.exec_module(native)
    version = json.loads(native.COMPONENTS.read_text())["qq"]["version"]
    source = tmp_path / "Files"
    app = source / "versions" / version / "resources/app"
    for name in ("wmpfsdk/runtime/flue.dll", "miniapp/module.js", "QQScreenShot/screenshot.dll",
                 "major.node", "wrapper.node", "application.asar", "avsdk/AVSDKPlugin.dll", "package.json"):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"native")
    (source / "QQ.exe").write_bytes(b"host")
    destination = tmp_path / "output"
    native.copy_qq_runtime(source, destination)
    relative = app.relative_to(source)
    for name in ("wmpfsdk", "miniapp", "QQScreenShot", "major.node", "wrapper.node", "application.asar"):
        assert not (destination / relative / name).exists()
    assert (destination / relative / "avsdk/AVSDKPlugin.dll").read_bytes() == b"native"
    assert (destination / "QQ.exe").read_bytes() == b"host"
