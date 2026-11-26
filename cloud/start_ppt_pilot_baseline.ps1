#Requires -Version 7.0
[CmdletBinding()]
param(
  [switch]$Submit,
  [string]$RuntimeRoot = (Join-Path $HOME '.cache\codex-runtimes\codex-primary-runtime\dependencies')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root '.test-output\pilot-community-reading-page-v4-key-default'
$specPath = Join-Path $runDir 'baseline.spec.json'
$runner = Join-Path $PSScriptRoot 'run_ppt_training_page.mjs'
$node = (Resolve-Path -LiteralPath (Join-Path $RuntimeRoot 'node\bin\node.exe')).Path
$modules = (Resolve-Path -LiteralPath (Join-Path $RuntimeRoot 'node\node_modules')).Path
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$spec = [IO.File]::ReadAllText($specPath, $utf8) | ConvertFrom-Json
if ($spec.scope -cne 'page' -or $spec.arm -cne 'baseline' -or $spec.position -ne 1 -or $spec.pageCount -ne 1 -or
    $spec.model -cne 'gpt-image-2.6-sunburst' -or $spec.size -cne '1536x864' -or $spec.quality -cne 'medium' -or
    $spec.responseFormat -cne 'key-default' -or $spec.PSObject.Properties['reference'] -or $spec.PSObject.Properties['feedback']) {
  throw 'Unexpected trial scope or settings; no request was sent.'
}
$promptHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($utf8.GetBytes([string]$spec.prompt)))
if ($promptHash -cne '3A22FF735DFCC91A2B5871BEDF71D29F9B2B435A0B876A91FF3602DD41A27BC4') { throw 'Registered baseline prompt changed; no request was sent.' }
$configText = [IO.File]::ReadAllText((Join-Path $root 'dist\cloud-config.js'), $utf8)
$m = [regex]::Match($configText, '(?s)window\.CLOUD_CONFIG\s*=\s*(\{.*\});?\s*$')
if (!$m.Success) { throw 'Cannot read isolated pilot configuration.' }
$config = $m.Groups[1].Value | ConvertFrom-Json
if ($config.workspaceId -cne 'dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7' -or
    $config.url -cne 'https://etorcisrfcbtagnxrpau.supabase.co' -or $config.coverTrainingSite -cne $true -or
    [string]::IsNullOrWhiteSpace($config.anonKey)) { throw 'Local config does not point to the isolated pilot.' }
$stem = 'page-baseline-01'
foreach ($suffix in @('attempt.json','dispatch.json','result.json','png')) {
  if (Test-Path -LiteralPath (Join-Path $runDir "$stem.$suffix")) {
    throw 'This trial already has local evidence. Check its receipts and the provider log before any separate attempt.'
  }
}
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $node
$start.WorkingDirectory = $root
$start.UseShellExecute = $false
foreach ($name in @('GUANYE_PILOT_IMAGE_KEY','GUANYE_PILOT_REVIEWER_PASSWORD','NODE_OPTIONS')) {
  [void]$start.Environment.Remove($name)
}
$start.Environment['NODE_PATH'] = $modules
$start.ArgumentList.Add('-e')
$start.ArgumentList.Add('require("sharp")')
$preflight = [Diagnostics.Process]::Start($start)
try {
  $preflight.WaitForExit()
  if ($preflight.ExitCode -ne 0) { throw 'Node or image verification dependency unavailable.' }
} finally { $preflight.Dispose() }
Write-Output 'Pilot: one text-only body slide, gpt-image-2.6-sunburst, 16:9, Medium; catalog estimate: 0.013 credits.'
if (!$Submit) { Write-Output 'Preflight only. No credential read. No request sent.'; return }
$secure = Read-Host 'Enter the rotated site API key (hidden); Enter on empty input cancels' -AsSecureString
$ptr = [IntPtr]::Zero
$process = $null
try {
  if ($secure.Length -eq 0) { Write-Output 'Canceled; no request sent.'; return }
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  if ($key -cnotmatch '^sk-site-[A-Za-z0-9_-]{20,}$') { throw 'Unexpected site API key format; no request sent.' }
  $start.ArgumentList.Clear()
  foreach ($arg in @($runner, $specPath, $runDir, '--submit-one-pilot-image')) { $start.ArgumentList.Add($arg) }
  $start.Environment['GUANYE_PILOT_IMAGE_KEY'] = $key
  $process = [Diagnostics.Process]::Start($start)
} finally {
  [void]$start.Environment.Remove('GUANYE_PILOT_IMAGE_KEY')
  $key = $null
  if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
  $secure.Dispose()
}
try {
  $process.WaitForExit()
  if ($process.ExitCode -ne 0) { throw 'No verified image. Read the retained receipts and provider log; do not resubmit blindly.' }
} finally { $process.Dispose() }
