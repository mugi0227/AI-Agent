param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$testHome = Join-Path ([IO.Path]::GetTempPath()) ("Work Assistant native test " + [Guid]::NewGuid().ToString('N'))
$keys = @{
  Edge = 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.workassistant.capture'
  Chrome = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.workassistant.capture'
}
$ids = @{ Edge = ('a' * 32); Chrome = ('b' * 32) }
$backups = @{}
foreach ($browser in $keys.Keys) {
  $existed = Test-Path $keys[$browser]
  $backups[$browser] = @{ existed = $existed; value = $null }
  if ($existed) { $backups[$browser].value = (Get-Item $keys[$browser]).GetValue('') }
}

function Assert-True($condition, [string]$message) {
  if (!$condition) { throw $message }
}

function Invoke-Native([string]$exe, [string]$origin, [hashtable]$payload) {
  $start = New-Object Diagnostics.ProcessStartInfo
  $start.FileName = $exe
  $start.Arguments = $origin
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardInput = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $process = New-Object Diagnostics.Process
  $process.StartInfo = $start
  try {
    [void]$process.Start()
    $body = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress))
    $header = [BitConverter]::GetBytes([int]$body.Length)
    $process.StandardInput.BaseStream.Write($header, 0, $header.Length)
    $process.StandardInput.BaseStream.Write($body, 0, $body.Length)
    $process.StandardInput.Close()
    Assert-True ($process.WaitForExit(35000)) 'Native host timed out.'
    $output = New-Object IO.MemoryStream
    try {
      $process.StandardOutput.BaseStream.CopyTo($output)
      $bytes = $output.ToArray()
      Assert-True ($bytes.Length -ge 4) ('Native host returned no frame. ' + $process.StandardError.ReadToEnd())
      $length = [BitConverter]::ToInt32($bytes, 0)
      Assert-True ($length -eq $bytes.Length - 4) 'Native reply frame length mismatch.'
      return ([Text.Encoding]::UTF8.GetString($bytes, 4, $length) | ConvertFrom-Json)
    } finally { $output.Dispose() }
  } finally {
    if ($process.Id -and !$process.HasExited) { $process.Kill() }
    $process.Dispose()
  }
}

try {
  & $PythonExe -m work_assistant --home $testHome init | Out-Null
  Assert-True ($LASTEXITCODE -eq 0) 'Initialize failed.'
  $configPath = Join-Path $testHome 'config.json'
  $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $config.extension_ids = @($ids.Edge, $ids.Chrome)
  $config.allowed_web_hosts = @('example.invalid')
  $config.local_data_approved = $true
  [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))

  $manifests = @{}
  foreach ($browser in @('Edge', 'Chrome')) {
    & (Join-Path $repo 'scripts\register-native-host.ps1') -Browser $browser -ExtensionId $ids[$browser] -PythonExe $PythonExe -DataHome $testHome
    $manifestPath = (Get-Item $keys[$browser]).GetValue('')
    $manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifests[$browser] = $manifest
    Assert-True ($manifest.allowed_origins.Count -eq 1) 'Unexpected native origins.'
    Assert-True ($manifest.allowed_origins[0] -eq "chrome-extension://$($ids[$browser])/") 'Browser origin mismatch.'
    $reply = Invoke-Native $manifest.path "chrome-extension://$($ids[$browser])/" @{operation='ping'}
    Assert-True ($reply.status -eq 'ok') "$browser native ping failed."
    # Build Japanese text without relying on the PowerShell 5 script-file codepage.
    $text = ([string][char]0x65E5) + [char]0x672C + [char]0x8A9E + "`n" + $browser
    $reply = Invoke-Native $manifest.path "chrome-extension://$($ids[$browser])/" @{
      operation='capture'; url='https://example.invalid/article?token=discard#fragment';
      title='Synthetic capture'; text=$text; scope='selection'
    }
    Assert-True ($reply.status -eq 'saved_locally') "$browser native capture failed."
    $saved = Get-Content (Join-Path $testHome "captures\$($reply.event_id).json") -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True ($saved.content -eq $text) 'Japanese native frame did not roundtrip.'
    Assert-True ($saved.source_url -eq 'https://example.invalid/article') 'Capture URL was not redacted.'
    $reply = Invoke-Native $manifest.path ("chrome-extension://" + ('c' * 32) + '/') @{operation='ping'}
    Assert-True ($reply.error -eq 'NATIVE_ORIGIN_BLOCKED') 'Unapproved extension ID was accepted.'
  }
  Assert-True ($manifests.Edge.path -ne $manifests.Chrome.path) 'Browser registrations share a launcher.'
  & (Join-Path $repo 'scripts\unregister-native-host.ps1') -Browser Edge
  Assert-True (!(Test-Path $keys.Edge)) 'Edge registration was not removed.'
  Assert-True (Test-Path $keys.Chrome) 'Removing Edge also removed Chrome.'
  $reply = Invoke-Native $manifests.Chrome.path "chrome-extension://$($ids.Chrome)/" @{operation='ping'}
  Assert-True ($reply.status -eq 'ok') 'Chrome stopped working after removing Edge.'
  & (Join-Path $repo 'scripts\unregister-native-host.ps1') -Browser Chrome
  Assert-True (!(Test-Path $keys.Chrome)) 'Chrome registration was not removed.'
  Write-Output 'PASS: Chrome/Edge registration, native relay, Japanese capture, origin checks, independent removal.'
} finally {
  foreach ($browser in $keys.Keys) {
    if ($backups[$browser].existed) {
      New-Item -Path $keys[$browser] -Force | Out-Null
      if ($null -ne $backups[$browser].value) { Set-Item -Path $keys[$browser] -Value $backups[$browser].value }
      else { (Get-Item $keys[$browser]).DeleteValue('', $false) }
    } elseif (Test-Path $keys[$browser]) { Remove-Item $keys[$browser] }
  }
  if (Test-Path $testHome) { Remove-Item $testHome -Recurse -Force }
}
