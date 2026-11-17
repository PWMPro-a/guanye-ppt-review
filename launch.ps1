param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$address = 'http://127.0.0.1:8765'
$ready = $false
try {
    $health = Invoke-RestMethod -Uri ($address + '/api/health') -TimeoutSec 2
    if ($health.app -eq 'ppt-review') { $ready = $true }
    else { throw 'Port 8765 is occupied by another application.' }
} catch {
    if ($_.Exception.Message -eq 'Port 8765 is occupied by another application.') { throw }
}
if (-not $ready) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Python 3.10 or later is required. Please install Python and try again.' }
    $logFolder = Join-Path $project 'data'
    New-Item -ItemType Directory -Force -Path $logFolder | Out-Null
    $serverScript = Join-Path $project 'server.py'
    $service = Start-Process -FilePath $pythonCommand.Source -ArgumentList @('-u', ('"' + $serverScript + '"')) -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logFolder 'server.log') -RedirectStandardError (Join-Path $logFolder 'server-error.log') -PassThru
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $health = Invoke-RestMethod -Uri ($address + '/api/health') -TimeoutSec 1
            if ($health.app -eq 'ppt-review') { $ready = $true; break }
        } catch { }
        if ($service.HasExited) { break }
    }
    if (-not $ready) { throw ('Could not start PPT Review. Check logs in: ' + $logFolder) }
    $service.Id | Set-Content -LiteralPath (Join-Path $logFolder 'server.pid')
}
if (-not $NoBrowser) { Start-Process $address }
