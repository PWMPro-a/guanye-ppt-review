#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$ProfessionalConfig,
    [switch]$Install
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $PSVersionTable.PSEdition -ne 'Core') { throw '需要 Windows PowerShell 7。' }
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$pythonPath = (Resolve-Path -LiteralPath $Python).ProviderPath
$configPath = (Resolve-Path -LiteralPath $ProfessionalConfig).ProviderPath
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or [IO.Path]::GetFileName($pythonPath) -ine 'python.exe') {
    throw '请指定已安装解释器的 python.exe 绝对路径。'
}
$workerPath = Join-Path -Path ([IO.Path]::GetDirectoryName($pythonPath)) -ChildPath 'pythonw.exe'
if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf) -or -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw '未找到同一解释器的 pythonw.exe 或专业版公开配置文件。'
}
if ($configPath -match '[\x00-\x1f"]') { throw '配置文件路径包含无法传递的字符。' }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
try { $sid = $identity.User.Value } finally { $identity.Dispose() }
# Bind task identity to the Windows user and canonical project/workspace, not the name of a JSON file.
$check = 'import sys; from pathlib import Path; from cloud.local_professional import load_browser_config; from cloud.professional_worker_secrets import _fingerprint; print(_fingerprint(load_browser_config(Path(sys.argv[1]))).hex())'
Push-Location -LiteralPath $root
try {
    $fingerprint = & $pythonPath -B -c $check $configPath
    if ($LASTEXITCODE -ne 0 -or $fingerprint -isnot [string] -or $fingerprint -cnotmatch '^[0-9a-f]{64}$') {
        throw '无法验证解释器或专业版公开配置。'
    }
    if ($Install) {
        & $pythonPath -B -m cloud.professional_review_worker doctor --config $configPath | Write-Host
        if ($LASTEXITCODE -ne 0) { throw '请先为当前 Windows 用户配置并验证本机预评凭据。' }
    }
} finally { Pop-Location }

$name = "GuanyeProfessionalReview-$sid-$fingerprint"
$marker = "GuanyeProfessionalReview:v1:$sid`:$fingerprint"
# Task Scheduler ExecAction does not use a shell; the only spaced argument is a verified literal file path.
$arguments = '-B -m cloud.professional_review_worker run --config "' + $configPath + '"'
if (-not $Install) {
    [ordered]@{ installed = $false; taskName = $name; executable = $workerPath; arguments = $arguments;
        workingDirectory = $root; trigger = 'current-user-logon'; runLevel = 'least-privilege'; multipleInstances = 'ignore-new' } |
        ConvertTo-Json -Compress
    return
}

# Native COM from PowerShell 7, without importing Windows PowerShell or storing a task password.
function Get-TaskIdentitySid([string]$Name) {
    if ($Name -match '^S-1-') { return ([Security.Principal.SecurityIdentifier]::new($Name)).Value }
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    return ([Security.Principal.NTAccount]::new($Name)).Translate([Security.Principal.SecurityIdentifier]).Value
}
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$existing = $null
try { $existing = $folder.GetTask($name) } catch {
    # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND); never overwrite on access or transport errors.
    if ($_.Exception.HResult -ne -2147024894 -and $_.Exception.InnerException?.HResult -ne -2147024894) { throw }
}
if ($null -ne $existing) {
    $definition = $existing.Definition
    if ($definition.RegistrationInfo.Description -cne $marker -or $definition.Actions.Count -ne 1 -or
        $definition.Actions.Item(1).Path -ine $workerPath -or $definition.Actions.Item(1).Arguments -cne $arguments -or
        $definition.Actions.Item(1).WorkingDirectory -ine $root -or $definition.Principal.LogonType -ne 3 -or
        $definition.Principal.RunLevel -ne 0 -or (Get-TaskIdentitySid $definition.Principal.UserId) -ne $sid -or
        $definition.Triggers.Count -ne 1 -or $definition.Triggers.Item(1).Type -ne 9 -or
        (Get-TaskIdentitySid $definition.Triggers.Item(1).UserId) -ne $sid -or $definition.Settings.MultipleInstances -ne 2 -or
        $definition.Settings.ExecutionTimeLimit -ne 'PT0S') {
        throw '同名计划任务的命令、运行身份或配置不匹配，未覆盖；请先在任务计划程序中人工核对。'
    }
    if (-not $existing.Enabled) { throw '已安装的专业预评计划任务被禁用，请先人工核对。' }
    if ($existing.State -ne 4) { $null = $existing.Run($null) }
    Write-Output "已复用计划任务 $name；未验证模型推理。"
    return
}

$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = $marker
$definition.Principal.UserId = $sid
$definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN (current Windows user)
$definition.Principal.RunLevel = 0 # TASK_RUNLEVEL_LUA
$definition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.RestartCount = 3
$definition.Settings.RestartInterval = 'PT1M'
$trigger = $definition.Triggers.Create(9) # TASK_TRIGGER_LOGON
$trigger.UserId = $sid
$trigger.Delay = 'PT15S'
$action = $definition.Actions.Create(0) # TASK_ACTION_EXEC
$action.Path = $workerPath
$action.Arguments = $arguments
$action.WorkingDirectory = $root
# CREATE only: refuses a racing existing task, even when it shares our computed name.
$registered = $folder.RegisterTaskDefinition($name, $definition, 2, $sid, $null, 3, $null)
$null = $registered.Run($null)
Write-Output "已登记并请求启动 $name。评分师下次登录 Windows 后自动启动；未验证模型推理。"
