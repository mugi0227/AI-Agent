param(
  [ValidateSet('Edge', 'Chrome')][string]$Browser = 'Edge'
)
$ErrorActionPreference = 'Stop'
$key = switch ($Browser) {
  'Edge' { 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.workassistant.capture' }
  'Chrome' { 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.workassistant.capture' }
}
if (Test-Path $key) { Remove-Item $key }
Write-Output "$Browser native host registration removed. Local data and executable were not deleted."
