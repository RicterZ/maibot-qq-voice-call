# Launch in the user's interactive session; SSH/session 0 is unsuitable for audio tests.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$NativeRoot,
    [Parameter(Mandatory)][string]$DataRoot,
    [string]$BridgeRoot = "",
    [int]$HostPort = 6111,
    [int]$BridgePort = 6110
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$NativeRoot = [System.IO.Path]::GetFullPath($NativeRoot)
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
if (-not $BridgeRoot) { $BridgeRoot = Split-Path $PSScriptRoot -Parent }
$BridgeRoot = [System.IO.Path]::GetFullPath($BridgeRoot)
if ([System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'AV host requires an interactive desktop session' }
if ($HostPort -lt 1 -or $HostPort -gt 65535 -or $BridgePort -lt 1 -or $BridgePort -gt 65535) { throw 'Invalid TCP port' }
$packages = @(Get-ChildItem (Join-Path $NativeRoot 'qq\Files\versions\*\resources\app\package.json'))
if ($packages.Count -ne 1) { throw 'Expected one isolated QQ runtime version' }
$appDir = $packages[0].DirectoryName
$loader = Join-Path $NativeRoot 'loader\NapCatWinBootMain.exe'
$hook = Join-Path $NativeRoot 'loader\NapCatWinBootHook.dll'
$exe = Join-Path $NativeRoot 'qq\Files\QQ.exe'
$hostEntry = Join-Path $BridgeRoot 'av-host\host.cjs'
foreach ($file in @($loader, $hook, $exe, $hostEntry)) { if (-not (Test-Path $file -PathType Leaf)) { throw "Missing runtime file: $file" } }
New-Item -ItemType Directory -Force $DataRoot | Out-Null
$utf8 = [System.Text.UTF8Encoding]::new($false)
$tokenFile = Join-Path $DataRoot 'control.token'
if (-not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [System.IO.File]::WriteAllText($tokenFile, [Convert]::ToBase64String($bytes), $utf8)
}
if ([System.Text.Encoding]::UTF8.GetByteCount([System.IO.File]::ReadAllText($tokenFile).Trim()) -lt 32) { throw 'Invalid control token' }
# Hook redirects manifest/bootstrap reads in memory. Installed QQ stays unchanged.
$manifest = Get-Content $packages[0].FullName -Raw | ConvertFrom-Json
$manifest.main = './loadNapCat.js'
$env:NAPCAT_PATCH_PACKAGE = Join-Path $DataRoot 'qqnt.json'
$env:NAPCAT_LOAD_PATH = Join-Path $DataRoot 'loadNapCat.js'
[System.IO.File]::WriteAllText($env:NAPCAT_PATCH_PACKAGE, ($manifest | ConvertTo-Json -Depth 10), $utf8)
[System.IO.File]::WriteAllText($env:NAPCAT_LOAD_PATH, ('require(' + ($hostEntry.Replace('\', '/') | ConvertTo-Json -Compress) + ');'), $utf8)
$env:MAIBOT_QQ_CALL_BRIDGE_DIR = $BridgeRoot
$env:MAIBOT_QQ_CALL_RUNTIME_DIR = $DataRoot
$env:MAIBOT_QQ_CALL_AVSDK_PATH = Join-Path $appDir 'avsdk\AVSDKPlugin.dll'
$env:MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE = $tokenFile
$env:MAIBOT_QQ_CALL_AV_HOST_PORT = [string]$HostPort
$env:MAIBOT_QQ_CALL_BRIDGE_PORT = [string]$BridgePort
$env:MAIBOT_QQ_CALL_AV_HOST_HOST = '127.0.0.1'
$env:MAIBOT_QQ_CALL_BRIDGE_HOST = '127.0.0.1'
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
& $loader $exe $hook
exit $LASTEXITCODE
