# QQ AV Bridge

该目录提供 MaiBot QQ Voice Call 所需的公开桥接源码。它把 QQ 内部 AVSDK
事件限制在两个本地进程之间，只向 MaiBot 暴露最小通话状态和两个 PulseAudio
设备。

## 组件

- `napcat-plugin/`：按 NapCat 官方外部插件结构发布，监听 QQ 来电事件、转发
  AVSDK 网络数据、自动接听，并在 `127.0.0.1:6110` 提供最小状态接口。
- `av-host/`：由第二个 QQ/Electron 进程加载用户安装目录内的
  `libAVSDKPlugin.so`，默认监听 `127.0.0.1:6111`。
- `scripts/`：安装、卸载、诊断、隔离 PulseAudio 和两个进程的启动脚本。
- `tests/`：验证原生 Accept 参数映射、敏感字段脱敏和配置边界。

两个 HTTP 服务共享至少 32 字节的 Bearer Token，而且拒绝非回环监听地址。
未鉴权的 `/healthz` 只返回 `{ "ok": true }`，不包含运行状态。

## 安装

依赖命令：`pulseaudio`、`pactl`、`parec`、`pacat`、`xvfb-run`、`curl`。以
Debian/Ubuntu 为例：

```bash
sudo apt-get install pulseaudio pulseaudio-utils xvfb curl
```

定位 QQ 安装目录和它内部的 NapCat 目录，然后执行：

```bash
./scripts/install.sh \
  --qq-dir /opt/QQ \
  --napcat-dir /opt/QQ/resources/app/app_launcher/napcat \
  --check

./scripts/install.sh \
  --qq-dir /opt/QQ \
  --napcat-dir /opt/QQ/resources/app/app_launcher/napcat
```

可用 `--install-dir` 修改默认安装位置。对应环境变量为：

- `MAIBOT_QQ_CALL_BRIDGE_DIR`
- `MAIBOT_QQ_CALL_NAPCAT_DIR`
- `MAIBOT_QQ_CALL_QQ_DIR`
- `MAIBOT_QQ_CALL_AVSDK_PATH`
- `MAIBOT_QQ_CALL_BRIDGE_TOKEN` 或 `MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE`
- `MAIBOT_QQ_CALL_BRIDGE_HOST` / `MAIBOT_QQ_CALL_BRIDGE_PORT`
- `MAIBOT_QQ_CALL_AV_HOST_HOST` / `MAIBOT_QQ_CALL_AV_HOST_PORT`

Host 变量只接受 `127.0.0.1`、`::1` 或 `localhost`。

如果 QQ Loader 已被旧的 AV Host 集成改写，安装器会拒绝叠加 Hook。请先找到
升级前保存的干净 Loader，用 `--original-loader /path/to/clean-loader.js` 明确
指定；该文件会成为本插件卸载时恢复的基线。

## Loader Hook 与回滚

QQ 的打包 Electron 入口固定为 `resources/app/loadNapCat.js`。第二个进程要加载
AV Host，因此安装器会：

1. 校验 QQ 可执行文件、`package.json`、Loader 和 AVSDK 都位于指定目录；
2. 把原 Loader 原样备份为 `loadNapCat.maibot-qq-call.backup.cjs`；
3. 写入带 `MAIBOT_QQ_CALL_LOADER_HOOK_V1` 标记的最小分流代码；
4. 普通 QQ 进程继续加载备份的原入口，只有设置
   `MAIBOT_QQ_CALL_AV_HOST=1` 的第二个进程才加载 AV Host。

`uninstall.sh` 只在标记仍然匹配时恢复备份。如果 QQ 升级改写了 Loader，它会
停止并保留备份，不会覆盖新文件。该 Hook 不绕过 QQ 鉴权，也不复制登录态。

## 启动与音频设备

推荐由服务管理器分别监管 `run-av-host.sh` 与普通 NapCat 进程。快速验证可直接：

```bash
MAIBOT_QQ_CALL_BOT_UIN="机器人QQ号" /安装目录/scripts/run-napcat.sh
```

脚本创建仅当前用户可访问的 PulseAudio socket，并提供：

- `maibot_qq_speaker.monitor`：MaiBot 的 ASR 输入；
- `maibot_qq_mic`：MaiBot 的 TTS 输出；
- `maibot_qq_mic_source`：QQ 使用的默认麦克风 source。

MaiBot 配置示例：

```toml
[bridge]
base_url = "http://127.0.0.1:6110"
token_file = "/安装目录/runtime/control.token"

[audio]
pulse_server = "unix:/安装目录/runtime/pulse/native"
capture_device = "maibot_qq_speaker.monitor"
playback_device = "maibot_qq_mic"
```

## 诊断与升级

启动后运行：

```bash
/安装目录/scripts/doctor.sh
```

诊断会检查依赖、文件、Loader 标记、PulseAudio、AV Host 和带鉴权的 NapCat
Bridge。QQ 或 NapCat 升级后，重新运行安装器和诊断，并用测试账号完成一次来电、
接通、双向音频和挂断测试。不要未经验证直接替换正在运行的生产链路。

## 不包含的内容

本仓库不包含 QQ、NapCat、AVSDK 二进制、登录态、QQ 号或任何 API Key。
用户需自行遵守 QQ、NapCat、云模型供应商及当地法律的适用条款。


## Windows host adaptation

Windows uses QQ's matching Electron host and the pinned official NapCat
shell-loader. It does not invoke Bash, PulseAudio, Xvfb, or Linux installers.
The host/plugin/callback path has been verified on Windows 11. Account login,
call acceptance and bidirectional Windows audio are still under validation;
this bundle must not be presented as a completed Windows voice-call product.

Build platform-specific code bundles explicitly:

```bash
python bridge/build_bundle.py --platform windows --output dist/bridge-windows.zip
python bridge/build_bundle.py --platform linux --output dist/bridge-linux.zip
```

The Windows bundle excludes Linux scripts, probes and build tools. Stage the
native host separately on Windows with 7-Zip available:

```powershell
python bridge/windows/prepare_runtime.py --output dist/native --cache build/downloads
Expand-Archive dist/bridge-windows.zip dist/bridge -Force
& dist/bridge/windows/start-av-host.ps1 -NativeRoot dist/native -DataRoot data/qq-call
```

Run the host in the user's interactive desktop session. `DataRoot` holds the
control token, redirected bootstrap/manifest and isolated Electron profile;
bridge code and native components may remain read-only. The launcher redirects
QQ file reads through its hook and does not modify the user's installed QQ.
QQ's full Electron `QQNT.dll` is not interchangeable with NapCat Node's small
shim of the same filename.

`bridge/windows/probe/` contains the bounded, no-login probes. The manual
Windows QQ call research workflow runs these without placing calls or recording
audio. The existing Linux deployment remains unchanged.
