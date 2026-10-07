"use strict";

const { timingSafeEqual } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);
const { validInvocation } = require("./commands.cjs");

function port(value, fallback, name) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer TCP port`);
  }
  return parsed;
}

function loadSettings(env = process.env) {
  const bridgeDir = path.resolve(env.MAIBOT_QQ_CALL_BRIDGE_DIR || path.join(__dirname, ".."));
  const runtimeDir = path.resolve(env.MAIBOT_QQ_CALL_RUNTIME_DIR || path.join(bridgeDir, "runtime"));
  const qqDir = path.resolve(env.MAIBOT_QQ_CALL_QQ_DIR || path.join(bridgeDir, "QQ"));
  const host = env.MAIBOT_QQ_CALL_AV_HOST_HOST || "127.0.0.1";
  const bridgeHost = env.MAIBOT_QQ_CALL_BRIDGE_HOST || "127.0.0.1";
  if (!LOOPBACK_HOSTS.has(host) || !LOOPBACK_HOSTS.has(bridgeHost)) {
    throw new Error("AV host endpoints must use loopback addresses");
  }
  return {
    bridgeDir,
    runtimeDir,
    qqDir,
    host,
    listenPort: port(env.MAIBOT_QQ_CALL_AV_HOST_PORT, 6111, "AV host port"),
    bridgeHost,
    bridgePort: port(env.MAIBOT_QQ_CALL_BRIDGE_PORT, 6110, "bridge port"),
    token: (env.MAIBOT_QQ_CALL_BRIDGE_TOKEN || "").trim(),
    tokenFile: path.resolve(
      env.MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE || path.join(runtimeDir, "control.token"),
    ),
    avsdkPath: path.resolve(
      env.MAIBOT_QQ_CALL_AVSDK_PATH ||
        path.join(qqDir, "resources", "app", "avsdk", process.platform === "win32" ? "AVSDKPlugin.dll" : "libAVSDKPlugin.so"),
    ),
  };
}

const settings = loadSettings();
let controlToken = null;
let avWindow = null;
let controlServer = null;
let nextInvocationId = 1;
let rendererState = {
  ready: false,
  pluginFound: false,
  methods: [],
  messageCount: 0,
  forwardedCount: 0,
  lastForwardedCommand: null,
  forwardError: null,
  invocationCount: 0,
  lastInvocationCommand: null,
  lastInvocationAt: null,
  error: null,
  audioDevices: { microphone: null, speaker: null, selection: null },
};

function loadControlToken() {
  const token = settings.token || fs.readFileSync(settings.tokenFile, "utf8").trim();
  if (Buffer.byteLength(token, "utf8") < 32) {
    throw new Error("bridge token is missing or shorter than 32 bytes");
  }
  return token;
}

function hasValidControlToken(req) {
  const header = req.headers.authorization ?? "";
  if (!header.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(controlToken, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function sendJson(res, statusCode, body) {
  const encoded = Buffer.from(JSON.stringify(body));
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": encoded.byteLength,
    "Cache-Control": "no-store",
  });
  res.end(encoded);
}

async function readJsonBody(req, limit = 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request body is too large");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

function startControlServer() {
  controlServer = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", `http://${settings.host}:${settings.listenPort}`);
    if (req.method === "GET" && url.pathname === "/healthz") {
      return sendJson(res, 200, { ok: true });
    }
    if (!hasValidControlToken(req)) {
      return sendJson(res, 401, { code: -1, message: "Unauthorized" });
    }
    if (req.method === "GET" && url.pathname === "/v1/status") {
      return sendJson(res, 200, {
        code: 0,
        data: {
          electron: process.versions.electron ?? null,
          chrome: process.versions.chrome ?? null,
          audioProcesses: app.getAppMetrics().map(({ pid, type }) => ({ pid, type })),
          ...rendererState,
        },
      });
    }
    if (req.method === "POST" && url.pathname === "/v1/invoke") {
      try {
        const body = await readJsonBody(req);
        const command = Number(body?.command);
        const params = body?.params;
        if (!validInvocation(command, params)) {
          return sendJson(res, 400, { code: -1, message: "invalid command or params" });
        }
        if ([64, 65, 102].includes(command)) {
          rendererState.audioDevices[{ 64: "microphone", 65: "speaker", 102: "selection" }[command]] = null;
        }
        const invocationId = nextInvocationId++;
        const result = await avWindow?.webContents.executeJavaScript(
          `window.maibotQQCallAVSDKInvoke(${JSON.stringify(command)},` +
            `${JSON.stringify(invocationId)},${JSON.stringify(params)})`,
          true,
        );
        rendererState = {
          ...rendererState,
          invocationCount: rendererState.invocationCount + 1,
          lastInvocationCommand: command,
          lastInvocationAt: new Date().toISOString(),
        };
        return sendJson(res, 200, { code: 0, data: result ?? null });
      } catch (_error) {
        return sendJson(res, 500, { code: -1, message: "AVSDK invocation failed" });
      }
    }
    return sendJson(res, 404, { code: -1, message: "Not Found" });
  });
  controlServer.on("clientError", (_error, socket) => socket.destroy());
  controlServer.listen(settings.listenPort, settings.host, () => {
    console.log(`[MaiBotQQCallAVHost] listening on ${settings.host}:${settings.listenPort}`);
  });
}

ipcMain.on("maibot-qq-call-avsdk-state", (_event, incoming) => {
  rendererState = {
    ...rendererState,
    ready: Boolean(incoming?.ready),
    pluginFound: Boolean(incoming?.pluginFound),
    methods: Array.isArray(incoming?.methods)
      ? incoming.methods.filter((item) => typeof item === "string").slice(0, 100)
      : [],
    error: typeof incoming?.error === "string" ? incoming.error.slice(0, 500) : null,
  };
});

ipcMain.on("maibot-qq-call-avsdk-message", () => {
  rendererState = { ...rendererState, messageCount: rendererState.messageCount + 1 };
});

async function forwardPluginMessage(message) {
  if (
    !message ||
    typeof message !== "object" ||
    !Number.isInteger(message.cmd) ||
    !Object.hasOwn(message, "value")
  ) {
    return;
  }
  if ([64, 65, 102].includes(message.cmd) && Array.isArray(message.value)) {
    const key = { 64: "microphone", 65: "speaker", 102: "selection" }[message.cmd];
    rendererState.audioDevices[key] = message.cmd === 102
      ? { result: message.value[0] }
      : { result: message.value[0], names: Array.isArray(message.value[2])
          ? message.value[2].map((name) => typeof name === "string" ? name : "").slice(0, 128) : [] };
  }
  try {
    const response = await fetch(
      `http://${settings.bridgeHost}:${settings.bridgePort}/v1/avsdk/output`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${controlToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          command: message.cmd,
          id: Number.isInteger(message.id) ? message.id : 0,
          value: message.value,
        }),
      },
    );
    if (!response.ok) throw new Error(`bridge returned HTTP ${response.status}`);
    rendererState = {
      ...rendererState,
      forwardedCount: rendererState.forwardedCount + 1,
      lastForwardedCommand: message.cmd,
      forwardError: null,
    };
  } catch (error) {
    rendererState = { ...rendererState, forwardError: error?.message ?? String(error) };
  }
}

ipcMain.on("maibot-qq-call-avsdk-raw-message", (_event, message) => {
  void forwardPluginMessage(message);
});

if (!fs.existsSync(settings.avsdkPath)) {
  throw new Error(`QQ AVSDK library was not found: ${settings.avsdkPath}`);
}
controlToken = loadControlToken();
app.commandLine.appendSwitch(
  "register-pepper-plugins",
  `${settings.avsdkPath};application/x-ppapi-avSDK`,
);
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");
const profileDir = path.join(settings.runtimeDir, "av-host-profile");
fs.mkdirSync(profileDir, { recursive: true });
app.setPath("userData", profileDir);

app.whenReady()
  .then(async () => {
    avWindow = new BrowserWindow({
      width: 320,
      height: 240,
      show: false,
      webPreferences: {
        contextIsolation: false,
        nodeIntegration: true,
        plugins: true,
        sandbox: false,
      },
    });
    avWindow.webContents.on("render-process-gone", (_event, details) => {
      rendererState = {
        ...rendererState,
        ready: false,
        error: `renderer process gone: ${details.reason}`,
      };
    });
    await avWindow.loadFile(path.join(__dirname, "host.html"));
    startControlServer();
  })
  .catch((error) => {
    console.error(`[MaiBotQQCallAVHost] startup failed: ${error?.message ?? String(error)}`);
    process.exitCode = 1;
  });

app.on("window-all-closed", () => app.quit());
