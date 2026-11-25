#Requires -Version 7.0
[CmdletBinding()]
param(
  [string]$LibraryRoot = 'F:\ppt素材库\ppt-material-library-public',
  [string]$RunId = 'local-ppt-material-library-v1',
  [int]$Concurrency = 4
)
$ErrorActionPreference = 'Stop'
$pwsh = (Get-Command pwsh.exe -CommandType Application -ErrorAction Stop).Source
if ($PSVersionTable.PSVersion.Major -lt 7) { throw '必须使用 PowerShell 7' }
$repo = Split-Path -Parent $PSScriptRoot
$secretPath = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Guanye\CoverPilot\pilot-reviewer-password.dpapi'
if (-not (Test-Path -LiteralPath $secretPath)) { throw '找不到本机 DPAPI 测试账号密码文件' }
$secure = ConvertTo-SecureString ([IO.File]::ReadAllText($secretPath, [Text.Encoding]::UTF8))
$ptr = [IntPtr]::Zero
$process = $null
try {
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $start = [Diagnostics.ProcessStartInfo]::new()
  $start.FileName = (Get-Command node.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
  $start.UseShellExecute = $false
  $start.WorkingDirectory = $repo
  foreach ($arg in @((Join-Path $repo 'cloud\import_local_material_library.mjs'))) { $start.ArgumentList.Add($arg) }
  $start.Environment['TARGET_PASSWORD'] = $password
  $start.Environment['TARGET_EMAIL'] = 'reviewer5@guanye.local'
  $start.Environment['LOCAL_LIBRARY_ROOT'] = $LibraryRoot
  $start.Environment['LOCAL_IMPORT_RUN_ID'] = $RunId
  $start.Environment['LOCAL_IMPORT_CONCURRENCY'] = [string]$Concurrency
  $process = [Diagnostics.Process]::Start($start)
} finally {
  if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
  $secure.Dispose()
  $password = $null
}
try {
  $process.WaitForExit()
  if ($process.ExitCode -ne 0) { throw "本地素材导入失败（退出码 $($process.ExitCode)）" }
} finally { if ($process) { $process.Dispose() } }

