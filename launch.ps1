#requires -Version 7.0
<#
.SYNOPSIS
  Starts the local Guanye PPT review server and reuses only the matching instance.

.DESCRIPTION
  The no-argument path preserves the original launcher behavior: bind 127.0.0.1:8765,
  write launcher state under data/, and open the browser. Optional parameters allow
  isolated launcher tests and future local instance features.

  Instance JSON schema (all optional):
  {
    "localPro": true,
    "python": "C:/path/to/python.exe",
    "port": 8765,
    "source": "C:/path/to/images",
    "stateDirectory": "C:/path/to/launcher-state",
    "environment": { "NAME": "value" },
    "serverArguments": ["--future-option", "value"]
  }

  localPro only validates the independent instance config and passes bootstrap
  environment variables. It does not start or verify a professional worker.

.NOTES
  Implementation references:
  - CPython ThreadingHTTPServer (PSF-2.0):
    https://github.com/python/cpython/blob/main/Lib/http/server.py
  - .NET named Mutex (MIT):
    https://github.com/dotnet/runtime/blob/main/src/libraries/System.Private.CoreLib/src/System/Threading/Mutex.Windows.cs
  - PowerShell Start-Process -Environment:
    https://learn.microsoft.com/powershell/module/microsoft.powershell.management/start-process
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$Source,
    [string]$Python,
    [string]$StateDirectory,
    [string]$InstanceConfig,
    [string]$ProfessionalConfig,
    [string[]]$EnvironmentVariable = @(),
    [string[]]$ServerArgument = @(),
    [switch]$LocalPro,
    [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or later is required. Start this script with pwsh.exe.'
}

$script:ProjectPath = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$script:ServerPath = Join-Path $script:ProjectPath 'server.py'
if (-not (Test-Path -LiteralPath $script:ServerPath -PathType Leaf)) {
    throw "server.py was not found: $script:ServerPath"
}

function Resolve-LauncherPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $BaseDirectory $Path))
}

function Read-InstanceConfiguration {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = Resolve-LauncherPath -Path $Path -BaseDirectory $script:ProjectPath
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Instance config was not found: $fullPath"
    }

    $raw = Get-Content -LiteralPath $fullPath -Raw -Encoding utf8
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "Instance config is empty: $fullPath"
    }
    $data = $raw | ConvertFrom-Json -AsHashtable
    if ($data -isnot [System.Collections.IDictionary]) {
        throw 'Instance config must be a JSON object.'
    }

    $allowed = @('localPro', 'python', 'port', 'source', 'stateDirectory', 'environment', 'serverArguments')
    foreach ($key in $data.Keys) {
        if ($key -notin $allowed) {
            throw "Unknown instance config key: $key"
        }
    }
    return [pscustomobject]@{ Path = $fullPath; Data = $data; Directory = (Split-Path -Parent $fullPath) }
}

function Set-EnvironmentEntry {
    param(
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Map,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $false)][object]$Value
    )
    if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "Invalid environment variable name: $Name"
    }
    if ($null -eq $Value -or $Value -is [array]) {
        throw "Environment variable $Name must have a scalar value."
    }
    if ($Value -is [bool]) {
        $Map[$Name] = if ($Value) { '1' } else { '0' }
    }
    elseif ($Value -is [string]) {
        $Map[$Name] = $Value
    }
    elseif ($Value -is [ValueType]) {
        $Map[$Name] = [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    else {
        throw "Environment variable $Name must be a string, number, or boolean."
    }
}

function Resolve-PythonInterpreter {
    param([string]$Requested)

    $candidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        if ($Requested.IndexOfAny([char[]]@('/', '\')) -ge 0) {
            $candidates.Add((Resolve-LauncherPath -Path $Requested -BaseDirectory $script:ProjectPath))
        }
        else {
            $command = Get-Command $Requested -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($command) { $candidates.Add($command.Source) }
        }
    }
    else {
        if (-not [string]::IsNullOrWhiteSpace($env:GUANYE_PYTHON)) {
            $candidates.Add($env:GUANYE_PYTHON)
        }
        foreach ($name in @('python.exe', 'python3.exe', 'python')) {
            foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
                $candidates.Add($command.Source)
            }
        }
    }

    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        try {
            $candidatePath = [System.IO.Path]::GetFullPath($candidate)
        }
        catch {
            $failures.Add('invalid path')
            continue
        }
        if (-not $seen.Add($candidatePath)) { continue }
        if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
            $failures.Add("not found: $candidatePath")
            continue
        }
        try {
            $probe = & $candidatePath -c 'import json, sys; print(json.dumps({"major": sys.version_info.major, "minor": sys.version_info.minor, "micro": sys.version_info.micro, "version": sys.version.split()[0]}))' 2>&1
            if ($LASTEXITCODE -ne 0) { throw "version probe exited with $LASTEXITCODE" }
            $info = (($probe | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json
            $major = [int]$info.major
            $minor = [int]$info.minor
            if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
                $failures.Add("version is older than 3.10: $candidatePath")
                continue
            }
            return [pscustomobject]@{
                Path = $candidatePath
                Version = [string]$info.version
                Major = $major
                Minor = $minor
            }
        }
        catch {
            $failures.Add("probe failed: $candidatePath")
        }
    }

    $detail = if ($failures.Count) { ' ' + ($failures -join '; ') } else { '' }
    throw "Python 3.10 or later was not found.$detail"
}

function ConvertTo-WindowsCommandLineArgument {
    param([AllowEmptyString()][string]$Argument)

    if ($null -eq $Argument) { $Argument = '' }
    if ($Argument.Length -eq 0) { return '""' }
    if ($Argument -notmatch '[\s"]') { return $Argument }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]'\') {
            $backslashes++
            continue
        }
        if ($character -eq [char]'"') {
            if ($backslashes -gt 0) {
                [void]$builder.Append([char]'\', ($backslashes * 2 + 1))
                $backslashes = 0
            }
            else {
                [void]$builder.Append('\')
            }
            [void]$builder.Append('"')
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append([char]'\', $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append([char]'\', ($backslashes * 2))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-PortListeners {
    param([Parameter(Mandatory = $true)][int]$LocalPort)

    $getNetTcp = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($getNetTcp) {
        return @(
            Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
                ForEach-Object {
                    [pscustomobject]@{
                        LocalAddress = [string]$_.LocalAddress
                        LocalPort = [int]$_.LocalPort
                        OwningProcess = [int]$_.OwningProcess
                    }
                }
        )
    }

    $listeners = [System.Collections.Generic.List[object]]::new()
    foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
        if ($line -match '^\s*TCP\s+(.+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$' -and [int]$matches[2] -eq $LocalPort) {
            $address = [string]$matches[1]
            if ($address.StartsWith('[') -and $address.EndsWith(']')) {
                $address = $address.Substring(1, $address.Length - 2)
            }
            $listeners.Add([pscustomobject]@{
                LocalAddress = $address
                LocalPort = $LocalPort
                OwningProcess = [int]$matches[3]
            })
        }
    }
    return @($listeners)
}

function Get-AppHealth {
    param([Parameter(Mandatory = $true)][string]$Address)
    try {
        return Invoke-RestMethod -Method Get -Uri ($Address + '/api/health') -TimeoutSec 1 -NoProxy -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Test-HealthIdentity {
    param(
        [Parameter(Mandatory = $false)][object]$Health,
        [Parameter(Mandatory = $true)][int]$ListenerPid,
        [Parameter(Mandatory = $true)][string]$Mode
    )

    if (-not $Health -or [string]$Health.app -ne 'ppt-review') {
        return [pscustomobject]@{ Matches = $false; Reason = 'health endpoint does not identify PPT Review' }
    }
    $propertyNames = @($Health.PSObject.Properties.Name)
    $hasMode = $propertyNames -contains 'mode'
    $hasPid = $propertyNames -contains 'pid'
    $healthPid = 0

    if ($Mode -eq 'single-judge') {
        if (-not $hasMode -or [string]$Health.mode -ne 'single-judge') {
            return [pscustomobject]@{ Matches = $false; Reason = 'server is not running in single-judge mode' }
        }
        if (-not $hasPid -or -not [int]::TryParse([string]$Health.pid, [ref]$healthPid) -or $healthPid -ne $ListenerPid) {
            return [pscustomobject]@{ Matches = $false; Reason = 'health PID does not match the listening process' }
        }
    }
    else {
        if ($hasMode -and -not [string]::IsNullOrWhiteSpace([string]$Health.mode) -and [string]$Health.mode -ne 'legacy') {
            return [pscustomobject]@{ Matches = $false; Reason = 'server is not running in legacy mode' }
        }
        if ($hasPid -and (-not [int]::TryParse([string]$Health.pid, [ref]$healthPid) -or $healthPid -ne $ListenerPid)) {
            return [pscustomobject]@{ Matches = $false; Reason = 'health PID does not match the listening process' }
        }
    }
    return [pscustomobject]@{ Matches = $true; Reason = '' }
}
function Get-ProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Test-ServerCommandLine {
    param(
        [AllowEmptyString()][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ServerPath,
        [Parameter(Mandatory = $true)][int]$LocalPort
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    if ($CommandLine.IndexOf($ServerPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    $portPattern = '(?i)(?:^|\s)--port(?:\s+|=)("?)' + [regex]::Escape([string]$LocalPort) + '\1(?:\s|$)'
    return $CommandLine -match $portPattern
}

function Test-ServerProcessOwnership {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ServerPath,
        [Parameter(Mandatory = $true)][int]$LocalPort,
        [int]$ExpectedLauncherProcessId = 0,
        [string]$ProfessionalConfigPath = ''
    )

    $listenerSnapshot = Get-ProcessSnapshot -ProcessId $ProcessId
    if (-not $listenerSnapshot) {
        return [pscustomobject]@{ Matches = $false; Reason = 'process is not running'; Snapshot = $null; InterpreterLauncherPid = 0; ServerExecutablePath = $null }
    }
    $listenerExecutable = [string]$listenerSnapshot.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($listenerExecutable)) {
        return [pscustomobject]@{ Matches = $false; Reason = 'process executable path is unavailable'; Snapshot = $listenerSnapshot; InterpreterLauncherPid = 0; ServerExecutablePath = $null }
    }
    if (-not (Test-ServerCommandLine -CommandLine ([string]$listenerSnapshot.CommandLine) -ServerPath $ServerPath -LocalPort $LocalPort)) {
        return [pscustomobject]@{ Matches = $false; Reason = 'listener command line does not identify this server and port'; Snapshot = $listenerSnapshot; InterpreterLauncherPid = 0; ServerExecutablePath = $listenerExecutable }
    }
    $listenerCommandLine = [string]$listenerSnapshot.CommandLine
    $hasProfessionalConfig = $listenerCommandLine -match '(?i)(?:^|\s)--professional-config(?:\s+|=)'
    if ($ProfessionalConfigPath) {
        if (-not $hasProfessionalConfig -or $listenerCommandLine.IndexOf($ProfessionalConfigPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return [pscustomobject]@{ Matches = $false; Reason = 'running mode or professional config does not match this launch'; Snapshot = $listenerSnapshot; InterpreterLauncherPid = 0; ServerExecutablePath = $listenerExecutable }
        }
    }
    elseif ($hasProfessionalConfig) {
        return [pscustomobject]@{ Matches = $false; Reason = 'running server is in single-judge mode, not legacy mode'; Snapshot = $listenerSnapshot; InterpreterLauncherPid = 0; ServerExecutablePath = $listenerExecutable }
    }

    $selectedPython = [System.IO.Path]::GetFullPath($PythonPath)
    $current = $listenerSnapshot
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    for ($depth = 0; $depth -lt 10; $depth++) {
        if (-not $current) { break }
        $currentProcessId = [int]$current.ProcessId
        if (-not $seen.Add($currentProcessId)) { break }

        $currentExecutable = [string]$current.ExecutablePath
        if (-not [string]::IsNullOrWhiteSpace($currentExecutable)) {
            try {
                $currentExecutable = [System.IO.Path]::GetFullPath($currentExecutable)
                if ([string]::Equals($currentExecutable, $selectedPython, [StringComparison]::OrdinalIgnoreCase) -and
                    (Test-ServerCommandLine -CommandLine ([string]$current.CommandLine) -ServerPath $ServerPath -LocalPort $LocalPort)) {
                    if ($ExpectedLauncherProcessId -gt 0 -and $currentProcessId -ne $ExpectedLauncherProcessId) {
                        return [pscustomobject]@{ Matches = $false; Reason = 'interpreter launcher does not match this launch'; Snapshot = $listenerSnapshot; InterpreterLauncherPid = $currentProcessId; ServerExecutablePath = $listenerExecutable }
                    }
                    return [pscustomobject]@{
                        Matches = $true
                        Reason = ''
                        Snapshot = $listenerSnapshot
                        InterpreterLauncherPid = $currentProcessId
                        ServerExecutablePath = $listenerExecutable
                    }
                }
            }
            catch { }
        }

        $parentProcessId = [int]$current.ParentProcessId
        if ($parentProcessId -le 0 -or $parentProcessId -eq $currentProcessId) { break }
        $current = Get-ProcessSnapshot -ProcessId $parentProcessId
    }

    return [pscustomobject]@{
        Matches = $false
        Reason = 'listener process is not owned by the selected Python interpreter'
        Snapshot = $listenerSnapshot
        InterpreterLauncherPid = 0
        ServerExecutablePath = $listenerExecutable
    }
}

function Stop-OwnedServerProcess {
    param(
        [Parameter(Mandatory = $true)][int]$LauncherProcessId,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ServerPath,
        [Parameter(Mandatory = $true)][int]$LocalPort
    )

    $stopped = [System.Collections.Generic.List[int]]::new()
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $targets = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $queue.Enqueue($LauncherProcessId)

    $selectedPython = [System.IO.Path]::GetFullPath($PythonPath)
    while ($queue.Count -gt 0) {
        $parentProcessId = $queue.Dequeue()
        if (-not $seen.Add($parentProcessId)) { continue }
        foreach ($child in $all) {
            if ([int]$child.ParentProcessId -ne $parentProcessId) { continue }
            $childProcessId = [int]$child.ProcessId
            $queue.Enqueue($childProcessId)
            if (Test-ServerCommandLine -CommandLine ([string]$child.CommandLine) -ServerPath $ServerPath -LocalPort $LocalPort) {
                [void]$targets.Add($childProcessId)
            }
        }
        if ($parentProcessId -eq $LauncherProcessId) {
            $launcher = $all | Where-Object { [int]$_.ProcessId -eq $LauncherProcessId } | Select-Object -First 1
            if ($launcher -and (Test-ServerCommandLine -CommandLine ([string]$launcher.CommandLine) -ServerPath $ServerPath -LocalPort $LocalPort)) {
                try {
                    $launcherPath = [System.IO.Path]::GetFullPath([string]$launcher.ExecutablePath)
                    if ([string]::Equals($launcherPath, $selectedPython, [StringComparison]::OrdinalIgnoreCase)) {
                        [void]$targets.Add($LauncherProcessId)
                    }
                }
                catch { }
            }
        }
    }

    foreach ($targetProcessId in $targets) {
        try {
            Stop-Process -Id $targetProcessId -Force -ErrorAction SilentlyContinue
            [void]$stopped.Add($targetProcessId)
        }
        catch { }
    }
    return @($stopped)
}
function Read-LauncherStateFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
        $value = $raw | ConvertFrom-Json -AsHashtable
        if ($value -is [System.Collections.IDictionary] -and [string]$value['app'] -eq 'ppt-review') {
            return $value
        }
    }
    catch { }
    return $null
}
function Write-LauncherState {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$StateDirectoryPath,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$Address,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $false)][string]$SourcePath,
        [Parameter(Mandatory = $false)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][bool]$LocalProRequested,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$EnvironmentMap,
        [Parameter(Mandatory = $true)][int]$ExtraArgumentCount,
        [Parameter(Mandatory = $true)][bool]$Reused,
        [Parameter(Mandatory = $true)][string]$StartedAt,
        [Parameter(Mandatory = $true)][int]$LocalPort,
        [Parameter(Mandatory = $false)][int]$InterpreterLauncherPid = 0,
        [Parameter(Mandatory = $false)][string]$ServerExecutablePath = '',
        [Parameter(Mandatory = $true)][string]$Mode,
        [Parameter(Mandatory = $false)][string]$ProfessionalConfigPath = '',
        [Parameter(Mandatory = $false)][string]$ProfessionalConfigSha256 = ''
    )

    $workerStatus = if ($LocalProRequested) { 'not-verified' } else { 'not-requested' }
    $launcherPidValue = if ($InterpreterLauncherPid -gt 0) { $InterpreterLauncherPid } else { $null }
    $state = [ordered]@{
        app = 'ppt-review'
        launcher = 'launch.ps1'
        powershellPath = (Get-Process -Id $PID).Path
        powershellVersion = $PSVersionTable.PSVersion.ToString()
        serverPath = $script:ServerPath
        pythonPath = $PythonPath
        serverPythonPath = if ($ServerExecutablePath) { $ServerExecutablePath } else { $null }
        interpreterLauncherPid = $launcherPidValue
        mode = $Mode
        professionalConfig = if ($ProfessionalConfigPath) { $ProfessionalConfigPath } else { $null }
        professionalConfigSha256 = if ($ProfessionalConfigSha256) { $ProfessionalConfigSha256 } else { $null }
        port = $LocalPort
        bindAddress = '127.0.0.1'
        address = $Address
        stateDirectory = $StateDirectoryPath
        source = $SourcePath
        instanceConfig = $ConfigPath
        localProRequested = $LocalProRequested
        professionalWorkerStatus = $workerStatus
        environmentNames = @($EnvironmentMap.Keys | Sort-Object)
        extraServerArgumentCount = $ExtraArgumentCount
        processId = $ProcessId
        reused = $Reused
        startedAt = $StartedAt
    }
    $json = $state | ConvertTo-Json -Depth 8
    $temporaryPath = Join-Path $StateDirectoryPath ('.launcher-state-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [System.IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $utf8NoBom)
    Move-Item -LiteralPath $temporaryPath -Destination $StatePath -Force
}

$exitCode = 0
$mutex = $null
$mutexAcquired = $false
$service = $null
$startedNew = $false
$stateDirectoryPath = $null
$statePidPath = $null

try {
    $config = $null
    if (-not [string]::IsNullOrWhiteSpace($InstanceConfig)) {
        $config = Read-InstanceConfiguration -Path $InstanceConfig
    }

    if ($config -and -not $PSBoundParameters.ContainsKey('Port') -and $config.Data.ContainsKey('port')) {
        $configPort = [int]$config.Data['port']
        if ($configPort -lt 1 -or $configPort -gt 65535) {
            throw 'Instance config port must be between 1 and 65535.'
        }
        $Port = $configPort
    }

    $localProRequested = $LocalPro.IsPresent
    if (-not $PSBoundParameters.ContainsKey('LocalPro') -and $config -and $config.Data.ContainsKey('localPro')) {
        if ($config.Data['localPro'] -isnot [bool]) {
            throw 'Instance config localPro must be a boolean.'
        }
        $localProRequested = [bool]$config.Data['localPro']
    }
    if ($localProRequested -and -not $config) {
        throw 'LocalPro requires an independent instance config supplied with -InstanceConfig.'
    }

    $sourcePath = $null
    if ($PSBoundParameters.ContainsKey('Source') -and -not [string]::IsNullOrWhiteSpace($Source)) {
        $sourcePath = Resolve-LauncherPath -Path $Source -BaseDirectory (Get-Location).Path
    }
    elseif ($config -and $config.Data.ContainsKey('source') -and -not [string]::IsNullOrWhiteSpace([string]$config.Data['source'])) {
        $sourcePath = Resolve-LauncherPath -Path ([string]$config.Data['source']) -BaseDirectory $config.Directory
    }
    if ($sourcePath -and -not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
        throw "Source directory was not found: $sourcePath"
    }

    if (-not $PSBoundParameters.ContainsKey('StateDirectory') -and $config -and $config.Data.ContainsKey('stateDirectory')) {
        $StateDirectory = [string]$config.Data['stateDirectory']
        if (-not [string]::IsNullOrWhiteSpace($StateDirectory)) {
            $StateDirectory = Resolve-LauncherPath -Path $StateDirectory -BaseDirectory $config.Directory
        }
    }
    if ([string]::IsNullOrWhiteSpace($StateDirectory)) {
        $stateDirectoryPath = Join-Path $script:ProjectPath 'data'
    }
    else {
        $base = if ($config) { $config.Directory } else { (Get-Location).Path }
        $stateDirectoryPath = Resolve-LauncherPath -Path $StateDirectory -BaseDirectory $base
    }
    if (Test-Path -LiteralPath $stateDirectoryPath -PathType Leaf) {
        throw "State directory points to a file: $stateDirectoryPath"
    }
    $statePidPath = Join-Path $stateDirectoryPath 'server.pid'
    $stateMetadataPath = Join-Path $stateDirectoryPath 'launcher-state.json'

    $pythonRequest = $null
    if ($PSBoundParameters.ContainsKey('Python') -and -not [string]::IsNullOrWhiteSpace($Python)) {
        $pythonRequest = $Python
    }
    elseif ($config -and $config.Data.ContainsKey('python') -and -not [string]::IsNullOrWhiteSpace([string]$config.Data['python'])) {
        $pythonRequest = Resolve-LauncherPath -Path ([string]$config.Data['python']) -BaseDirectory $config.Directory
    }
    $pythonInfo = Resolve-PythonInterpreter -Requested $pythonRequest

    $environment = @{}
    if ($config -and $config.Data.ContainsKey('environment')) {
        $configEnvironment = $config.Data['environment']
        if ($configEnvironment -isnot [System.Collections.IDictionary]) {
            throw 'Instance config environment must be a JSON object.'
        }
        foreach ($name in $configEnvironment.Keys) {
            Set-EnvironmentEntry -Map $environment -Name ([string]$name) -Value $configEnvironment[$name]
        }
    }
    foreach ($entry in $EnvironmentVariable) {
        $separator = $entry.IndexOf('=')
        if ($separator -lt 1) {
            throw 'Environment variables must use NAME=VALUE syntax.'
        }
        Set-EnvironmentEntry -Map $environment -Name $entry.Substring(0, $separator) -Value $entry.Substring($separator + 1)
    }
    $environment['PYTHONUTF8'] = '1'
    $environment['PYTHONIOENCODING'] = 'utf-8'
    if ($config) {
        $environment['GUANYE_INSTANCE_CONFIG'] = $config.Path
        $localProValue = if ($localProRequested) { '1' } else { '0' }
        $environment['GUANYE_LOCAL_PRO_ENABLED'] = $localProValue
    }

    $serverArguments = @()
    if ($config -and $config.Data.ContainsKey('serverArguments')) {
        $configuredArguments = $config.Data['serverArguments']
        if ($configuredArguments -is [string] -or $configuredArguments -isnot [System.Collections.IEnumerable]) {
            throw 'Instance config serverArguments must be an array of strings.'
        }
        foreach ($argument in $configuredArguments) {
            if ($argument -isnot [string]) {
                throw 'Instance config serverArguments must contain only strings.'
            }
            $serverArguments += $argument
        }
    }
    $serverArguments += @($ServerArgument)
    foreach ($argument in $serverArguments) {
        if ([string]::IsNullOrWhiteSpace($argument)) {
            throw 'Server arguments cannot be empty.'
        }
        if ($argument -in @('--port', '--source', '--professional-config', '-u') -or $argument -match '^--(port|source|professional-config)=') {
            throw "Server argument is managed by the launcher: $argument"
        }
    }

    $professionalConfigPath = $null
    $professionalConfigSha256 = $null
    if ($PSBoundParameters.ContainsKey('ProfessionalConfig')) {
        if ([string]::IsNullOrWhiteSpace($ProfessionalConfig)) {
            throw 'ProfessionalConfig cannot be empty.'
        }
        $professionalConfigPath = Resolve-LauncherPath -Path $ProfessionalConfig -BaseDirectory (Get-Location).Path
        if (-not (Test-Path -LiteralPath $professionalConfigPath -PathType Leaf)) {
            throw "Professional config was not found: $professionalConfigPath"
        }
        $configFile = Get-Item -LiteralPath $professionalConfigPath
        if ($configFile.Length -gt 8192) {
            throw 'Professional config exceeds the server limit of 8192 bytes.'
        }
        $configHashBytes = [Security.Cryptography.SHA256]::HashData([System.IO.File]::ReadAllBytes($professionalConfigPath))
        $professionalConfigSha256 = ([Convert]::ToHexString($configHashBytes)).ToLowerInvariant()
    }
    $launchMode = if ($professionalConfigPath) { 'single-judge' } else { 'legacy' }
    $address = 'http://127.0.0.1:' + $Port
    $professionalWorkerStatus = if ($localProRequested) { 'not-verified' } else { 'not-requested' }
    $instanceConfigPath = if ($config) { $config.Path } else { $null }

    if ($Check) {
        $plan = [ordered]@{
            status = 'validated'
            powershellPath = (Get-Process -Id $PID).Path
            powershellVersion = $PSVersionTable.PSVersion.ToString()
            projectPath = $script:ProjectPath
            serverPath = $script:ServerPath
            pythonPath = $pythonInfo.Path
            pythonVersion = $pythonInfo.Version
            port = $Port
            bindAddress = '127.0.0.1'
            address = $address
            mode = $launchMode
            professionalConfig = $professionalConfigPath
            professionalConfigSha256 = $professionalConfigSha256
            source = $sourcePath
            stateDirectory = $stateDirectoryPath
            instanceConfig = $instanceConfigPath
            localProRequested = $localProRequested
            professionalWorkerStatus = $professionalWorkerStatus
            environmentNames = @($environment.Keys | Sort-Object)
            extraServerArgumentCount = $serverArguments.Count
        }
        $plan | ConvertTo-Json -Depth 6
        exit 0
    }

    $hashInput = $script:ServerPath + '|' + [string]$Port
    $hashBytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($hashInput))
    $mutexHash = [Convert]::ToHexString($hashBytes).Substring(0, 24)
    $mutex = [Threading.Mutex]::new($false, 'Local\GuanyePptReviewLauncher-' + $mutexHash)
    try {
        $mutexAcquired = $mutex.WaitOne(20000)
    }
    catch [Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw 'Another launcher is still starting or checking this port.'
    }

    $listeners = @(Get-PortListeners -LocalPort $Port)
    $reusedPid = $null
    $reusedOwnership = $null
    if ($listeners.Count -gt 0) {
        foreach ($listener in $listeners) {
            if (-not [string]::Equals($listener.LocalAddress, '127.0.0.1', [StringComparison]::OrdinalIgnoreCase)) {
                throw "Port $Port is bound outside 127.0.0.1 by PID $($listener.OwningProcess); refusing to reuse it."
            }
        }
        $owners = @($listeners | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)
        if ($owners.Count -ne 1) {
            throw "Port $Port has multiple listener owners; refusing to reuse it."
        }
        $ownerProcessId = [int]$owners[0]
        $health = Get-AppHealth -Address $address
        if (-not $health -or [string]$health.app -ne 'ppt-review') {
            throw "Port $Port is occupied by another application (PID $ownerProcessId)."
        }
        $healthIdentity = Test-HealthIdentity -Health $health -ListenerPid $ownerProcessId -Mode $launchMode
        if (-not $healthIdentity.Matches) {
            throw "Port $Port is running PPT Review in a different mode: $($healthIdentity.Reason)."
        }
        $ownership = Test-ServerProcessOwnership -ProcessId $ownerProcessId -PythonPath $pythonInfo.Path -ServerPath $script:ServerPath -LocalPort $Port -ProfessionalConfigPath $professionalConfigPath
        if (-not $ownership.Matches) {
            throw "Port $Port has a PPT Review response but is not the expected process: $($ownership.Reason)."
        }
        if ($launchMode -eq 'single-judge') {
            $existingState = Read-LauncherStateFile -Path $stateMetadataPath
            if (-not $existingState) {
                throw "Port $Port is running single-judge mode, but no launcher state is available to verify its professional config."
            }
            $stateProcessId = 0
            if (-not [int]::TryParse([string]$existingState['processId'], [ref]$stateProcessId) -or $stateProcessId -ne $ownerProcessId) {
                throw "Port $Port launcher state does not belong to listener PID $ownerProcessId."
            }
            if ([string]$existingState['mode'] -ne 'single-judge') {
                throw "Port $Port is running a different mode than its launcher state."
            }
            if (-not [string]::Equals([string]$existingState['professionalConfig'], $professionalConfigPath, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Port $Port is running a different professional config path."
            }
            if (-not [string]::Equals([string]$existingState['professionalConfigSha256'], $professionalConfigSha256, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Port $Port is running a different professional config fingerprint."
            }
        }
        $reusedPid = $ownerProcessId
        $reusedOwnership = $ownership
    }

    if ($null -ne $reusedPid) {
        New-Item -ItemType Directory -Force -Path $stateDirectoryPath | Out-Null
        [System.IO.File]::WriteAllText($statePidPath, [string]$reusedPid, $utf8NoBom)
        $snapshot = Get-ProcessSnapshot -ProcessId $reusedPid
        $startedAt = if ($snapshot -and $snapshot.CreationDate) {
            ([datetime]$snapshot.CreationDate).ToUniversalTime().ToString('o')
        }
        else {
            [datetime]::UtcNow.ToString('o')
        }
        Write-LauncherState -StatePath $stateMetadataPath -StateDirectoryPath $stateDirectoryPath -ProcessId $reusedPid -Address $address -PythonPath $pythonInfo.Path -SourcePath $sourcePath -ConfigPath $instanceConfigPath -LocalProRequested $localProRequested -EnvironmentMap $environment -ExtraArgumentCount $serverArguments.Count -Reused $true -StartedAt $startedAt -LocalPort $Port -InterpreterLauncherPid $reusedOwnership.InterpreterLauncherPid -ServerExecutablePath $reusedOwnership.ServerExecutablePath -Mode $launchMode -ProfessionalConfigPath $professionalConfigPath -ProfessionalConfigSha256 $professionalConfigSha256
        Write-Output "PPT Review is already running; reused PID $reusedPid at $address"
    }
    else {
        New-Item -ItemType Directory -Force -Path $stateDirectoryPath | Out-Null
        $stdoutPath = Join-Path $stateDirectoryPath 'server.log'
        $stderrPath = Join-Path $stateDirectoryPath 'server-error.log'
        $argumentTokens = @('-u', $script:ServerPath, '--port', [string]$Port)
        if ($sourcePath) {
            $argumentTokens += @('--source', $sourcePath)
        }
        if ($professionalConfigPath) {
            $argumentTokens += @('--professional-config', $professionalConfigPath)
        }
        $argumentTokens += $serverArguments
        $argumentLine = ($argumentTokens | ForEach-Object { ConvertTo-WindowsCommandLineArgument -Argument ([string]$_) }) -join ' '

        $service = Start-Process -FilePath $pythonInfo.Path -ArgumentList $argumentLine -WorkingDirectory $script:ProjectPath -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -Environment $environment -PassThru
        $startedNew = $true
        $ready = $false
        $lastProblem = ''
        $startedListenerPid = 0
        $startedOwnership = $null
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            if ($service.HasExited) {
                $lastProblem = "Python exited with code $($service.ExitCode)."
                break
            }
            Start-Sleep -Milliseconds 250
            $health = Get-AppHealth -Address $address
            if (-not $health) { continue }
            if ([string]$health.app -ne 'ppt-review') {
                $lastProblem = 'health endpoint belongs to another application'
                break
            }
            $listeners = @(Get-PortListeners -LocalPort $Port)
            $badBinding = @($listeners | Where-Object { -not [string]::Equals($_.LocalAddress, '127.0.0.1', [StringComparison]::OrdinalIgnoreCase) })
            if ($badBinding.Count -gt 0) {
                $lastProblem = 'server bound outside 127.0.0.1'
                break
            }
            $owners = @($listeners | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)
            if ($owners.Count -ne 1) {
                $lastProblem = 'listener owner is not unique'
                break
            }
            $listenerProcessId = [int]$owners[0]
            $healthIdentity = Test-HealthIdentity -Health $health -ListenerPid $listenerProcessId -Mode $launchMode
            if (-not $healthIdentity.Matches) {
                $lastProblem = $healthIdentity.Reason
                break
            }
            $ownership = Test-ServerProcessOwnership -ProcessId $listenerProcessId -PythonPath $pythonInfo.Path -ServerPath $script:ServerPath -LocalPort $Port -ExpectedLauncherProcessId $service.Id -ProfessionalConfigPath $professionalConfigPath
            if ($ownership.Matches) {
                $ready = $true
                $startedListenerPid = $listenerProcessId
                $startedOwnership = $ownership
                break
            }
            $lastProblem = $ownership.Reason
        }
        if (-not $ready) {
            $detail = if ($lastProblem) { " $lastProblem" } else { '' }
            throw "Could not start PPT Review.$detail Check logs in: $stateDirectoryPath"
        }

        [System.IO.File]::WriteAllText($statePidPath, [string]$startedListenerPid, $utf8NoBom)
        Write-LauncherState -StatePath $stateMetadataPath -StateDirectoryPath $stateDirectoryPath -ProcessId $startedListenerPid -Address $address -PythonPath $pythonInfo.Path -SourcePath $sourcePath -ConfigPath $instanceConfigPath -LocalProRequested $localProRequested -EnvironmentMap $environment -ExtraArgumentCount $serverArguments.Count -Reused $false -StartedAt $service.StartTime.ToUniversalTime().ToString('o') -LocalPort $Port -InterpreterLauncherPid $startedOwnership.InterpreterLauncherPid -ServerExecutablePath $startedOwnership.ServerExecutablePath -Mode $launchMode -ProfessionalConfigPath $professionalConfigPath -ProfessionalConfigSha256 $professionalConfigSha256
        Write-Output "PPT Review started at $address (PID $startedListenerPid; interpreter PID $($startedOwnership.InterpreterLauncherPid))"
        Write-Output "Logs: $stdoutPath ; $stderrPath"
    }

    if ($launchMode -eq 'single-judge') {
        Write-Output 'Professional mode verified: health mode single-judge and health PID matched the listener PID.'
    }
    if ($localProRequested) {
        Write-Output 'Local Pro bootstrap: instance config validated and environment passed to server; worker status is not verified.'
    }
    if (-not $NoBrowser) {
        try {
            Start-Process -FilePath $address | Out-Null
        }
        catch {
            Write-Warning "PPT Review is running, but the browser could not be opened: $($_.Exception.Message)"
        }
    }
}
catch {
    [Console]::Error.WriteLine('[ERROR] ' + $_.Exception.Message)
    if ($startedNew -and $service) {
        $stoppedPids = @()
        try {
            $stoppedPids = @(Stop-OwnedServerProcess -LauncherProcessId $service.Id -PythonPath $pythonInfo.Path -ServerPath $script:ServerPath -LocalPort $Port)
        }
        catch { }
        try {
            if (Test-Path -LiteralPath $statePidPath -PathType Leaf) {
                $pidText = (Get-Content -LiteralPath $statePidPath -Raw -Encoding utf8).Trim()
                $pidValue = 0
                if ([int]::TryParse($pidText, [ref]$pidValue) -and ($pidValue -eq $service.Id -or $stoppedPids -contains $pidValue)) {
                    Remove-Item -LiteralPath $statePidPath -Force
                }
            }
        }
        catch { }
    }
    $exitCode = 1
}
finally {
    if ($mutexAcquired -and $mutex) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}

exit $exitCode