#Requires -Version 7.0
[CmdletBinding(DefaultParameterSetName='Inventory')]
param(
  [Parameter(ParameterSetName='Inventory')][switch]$VerifyStorage,
  [Parameter(Mandatory,ParameterSetName='Pair')][ValidateSet('page','deck')][string]$Scope,
  [Parameter(Mandatory,ParameterSetName='Pair')][string]$CasePath,
  [Parameter(Mandatory,ParameterSetName='Pair')][string]$OutputPath
)
$ErrorActionPreference='Stop'
$secretPath=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Guanye\CoverPilot\pilot-reviewer-password.dpapi'
$secure=ConvertTo-SecureString ([IO.File]::ReadAllText($secretPath,[Text.Encoding]::UTF8)); $ptr=[IntPtr]::Zero; $process=$null
try { $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure); $password=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr); $si=[Diagnostics.ProcessStartInfo]::new(); $si.FileName=(Get-Command node.exe -CommandType Application | Select-Object -First 1).Source; $si.UseShellExecute=$false; $si.WorkingDirectory=$PSScriptRoot; if ($PSCmdlet.ParameterSetName -eq 'Pair') {
  $si.ArgumentList.Add((Join-Path $PSScriptRoot 'verify_ppt_training_pair.mjs'))
  $si.ArgumentList.Add($Scope)
  $si.ArgumentList.Add($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CasePath))
  $si.ArgumentList.Add($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath))
} else {
  $si.ArgumentList.Add((Join-Path $PSScriptRoot 'verify_local_training.mjs'))
  if ($VerifyStorage) { $si.ArgumentList.Add('--verify-storage') }
}; $si.Environment['TARGET_PASSWORD']=$password; $si.Environment['TARGET_EMAIL']='reviewer5@guanye.local'; $process=[Diagnostics.Process]::Start($si) } finally { if($ptr -ne [IntPtr]::Zero){[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}; $secure.Dispose(); $password=$null }
try {
  $process.WaitForExit()
  $exitCode = $process.ExitCode
  if ($exitCode -ne 0) { throw "验证失败（退出码 $exitCode）" }
} finally {
  if ($process) { $process.Dispose() }
}
