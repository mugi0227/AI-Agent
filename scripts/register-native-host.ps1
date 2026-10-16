param(
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-p]{32}$')][string]$ExtensionId,
  [Parameter(Mandatory=$true)][string]$PythonExe,
  [string]$DataHome = "$env:LOCALAPPDATA\WorkAssistant",
  [ValidateSet('Edge', 'Chrome')][string]$Browser = 'Edge'
)
$ErrorActionPreference = 'Stop'
# Run only after company approval. Never changes execution policy, EDR, HKLM or browser policy.
$PythonExe = (Resolve-Path $PythonExe).Path
$DataHome = [IO.Path]::GetFullPath($DataHome)
$configPath = Join-Path $DataHome 'config.json'
if (!(Test-Path $configPath)) { throw 'Run python -m work_assistant init first.' }
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.extension_ids -notcontains $ExtensionId) { throw 'Add this exact extension ID to config.json extension_ids after approval, then retry.' }
$compiler = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (!(Test-Path $compiler)) { $compiler = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (!(Test-Path $compiler)) { throw 'Approved .NET Framework compiler unavailable. See COMPANY_CODEX_PROMPT; do not bypass policy.' }
# Keep each browser's launcher and origin allowlist independent.
$target = Join-Path (Join-Path $DataHome 'native') $Browser.ToLowerInvariant()
New-Item -ItemType Directory -Path $target -Force | Out-Null
$exe = Join-Path $target 'WorkAssistant.Native.exe'
$source = Join-Path $PSScriptRoot '..\native\Launcher.cs'
& $compiler /nologo /target:exe "/out:$exe" /reference:System.Runtime.Serialization.dll $source
if ($LASTEXITCODE -ne 0) { throw 'Native launcher compile failed.' }
[IO.File]::WriteAllText((Join-Path $target 'native-launcher.json'), (@{python=$PythonExe; home=$DataHome} | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
$manifest = Join-Path $target 'com.workassistant.capture.json'
[IO.File]::WriteAllText($manifest, (@{name='com.workassistant.capture'; description='Explicit user-selected capture'; path=$exe; type='stdio'; allowed_origins=@("chrome-extension://$ExtensionId/")} | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
$key = switch ($Browser) {
  'Edge' { 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.workassistant.capture' }
  'Chrome' { 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.workassistant.capture' }
}
New-Item -Path $key -Force | Out-Null
Set-Item -Path $key -Value $manifest
Write-Output "Registered for current user in $Browser. Test extension Ping, then a synthetic capture."
