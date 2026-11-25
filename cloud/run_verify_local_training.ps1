#Requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$secretPath=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Guanye\CoverPilot\pilot-reviewer-password.dpapi'
$secure=ConvertTo-SecureString ([IO.File]::ReadAllText($secretPath,[Text.Encoding]::UTF8)); $ptr=[IntPtr]::Zero; $process=$null
try { $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure); $password=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr); $si=[Diagnostics.ProcessStartInfo]::new(); $si.FileName=(Get-Command node.exe -CommandType Application | Select-Object -First 1).Source; $si.UseShellExecute=$false; $si.WorkingDirectory=$PSScriptRoot; $si.ArgumentList.Add((Join-Path $PSScriptRoot 'verify_local_training.mjs')); $si.Environment['TARGET_PASSWORD']=$password; $si.Environment['TARGET_EMAIL']='reviewer5@guanye.local'; $process=[Diagnostics.Process]::Start($si) } finally { if($ptr -ne [IntPtr]::Zero){[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}; $secure.Dispose(); $password=$null }
try {
  $process.WaitForExit()
  $exitCode = $process.ExitCode
  if ($exitCode -ne 0) { throw "验证失败（退出码 $exitCode）" }
} finally {
  if ($process) { $process.Dispose() }
}
