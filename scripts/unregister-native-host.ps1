$ErrorActionPreference = 'Stop'
$key = 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.workassistant.capture'
if (Test-Path $key) { Remove-Item $key }
Write-Output 'Native host registration removed. Local data and executable were not deleted.'
