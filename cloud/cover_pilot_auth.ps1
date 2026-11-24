#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Authorize', 'Plan', 'Login', 'Clear')]
    [string] $Action,
    [ValidatePattern('^[a-z0-9]{20}$')]
    [string] $ProjectRef
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw '此入口只支持 Windows 用户级 DPAPI。' }
$credentialDir = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Guanye/CoverPilot'
$credentialPath = Join-Path $credentialDir 'supabase-access-token.dpapi'

switch ($Action) {
    'Authorize' {
        $secret = Read-Host '粘贴临时 Supabase Personal Access Token（不回显）' -AsSecureString
        try {
            if ($secret.Length -eq 0) { throw '没有输入令牌。' }
            $null = [IO.Directory]::CreateDirectory($credentialDir)
            [IO.File]::WriteAllText($credentialPath, (ConvertFrom-SecureString -SecureString $secret), [Text.UTF8Encoding]::new($false))
        } finally {
            $secret.Dispose()
        }
        Write-Output '已保存本机加密授权，尚未连接或修改数据库。请把测试项目的 Project Ref 发到聊天中，不要发送令牌。'
    }
    'Plan' {
        $productionRef = 'aytnpevbqkdhwzkbhkvw'
        if (-not $ProjectRef -or $ProjectRef -eq $productionRef) { throw '必须指定独立测试项目，不能使用正式项目。' }
        if (-not [IO.File]::Exists($credentialPath)) { throw '尚未在当前 Windows 用户下运行 Authorize。' }

        $python = (Get-Command python.exe -CommandType Application -ErrorAction Stop).Source
        $start = [Diagnostics.ProcessStartInfo]::new($python)
        $start.UseShellExecute = $false
        $start.WorkingDirectory = Split-Path -Parent $PSScriptRoot
        foreach ($arg in @('-B', (Join-Path $PSScriptRoot 'manage.py'), 'migration-plan', '--project-ref', $ProjectRef, '--cover-pilot', '--production-project-ref', $productionRef)) {
            $start.ArgumentList.Add($arg)
        }
        $secret = ConvertTo-SecureString ([IO.File]::ReadAllText($credentialPath, [Text.Encoding]::UTF8))
        $ptr = [IntPtr]::Zero
        try {
            $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
            $start.Environment['SUPABASE_ACCESS_TOKEN'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
            $process = [Diagnostics.Process]::Start($start)
        } finally {
            if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
            $secret.Dispose()
            $null = $start.Environment.Remove('SUPABASE_ACCESS_TOKEN')
        }
        try {
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) { throw "只读迁移预检失败（退出码 $($process.ExitCode)）。" }
        } finally {
            $process.Dispose()
        }
    }
    'Login' {
        $loginPath = Join-Path $credentialDir 'pilot-reviewer-password.dpapi'
        if (-not [IO.File]::Exists($loginPath)) { throw '当前 Windows 用户下没有训练站评分密码。' }
        Add-Type -AssemblyName System.Windows.Forms
        $secret = ConvertTo-SecureString ([IO.File]::ReadAllText($loginPath, [Text.Encoding]::UTF8))
        $form = [System.Windows.Forms.Form]::new()
        $form.Text = '观页训练站 · 本机评分账号'
        $form.StartPosition = 'CenterScreen'
        $form.ClientSize = [Drawing.Size]::new(420, 155)
        $form.FormBorderStyle = 'FixedDialog'
        $form.MinimizeBox = $false
        $form.MaximizeBox = $false
        $label = [System.Windows.Forms.Label]::new()
        $label.Text = '使用原观页评分邮箱和下面的独立训练密码登录。'
        $label.SetBounds(14, 18, 390, 25)
        $passwordBox = [System.Windows.Forms.TextBox]::new()
        $passwordBox.SetBounds(14, 49, 390, 25)
        $passwordBox.ReadOnly = $true
        $passwordBox.UseSystemPasswordChar = $true
        $show = [System.Windows.Forms.CheckBox]::new()
        $show.Text = '显示密码（不要发到聊天）'
        $show.SetBounds(14, 82, 220, 27)
        $show.add_CheckedChanged({ $passwordBox.UseSystemPasswordChar = -not $show.Checked })
        $close = [System.Windows.Forms.Button]::new()
        $close.Text = '关闭'
        $close.DialogResult = 'Cancel'
        $close.SetBounds(329, 110, 75, 28)
        $form.CancelButton = $close
        $form.Controls.AddRange(@($label, $passwordBox, $show, $close))
        try {
            $passwordBox.Text = [Net.NetworkCredential]::new('', $secret).Password
            $null = $form.ShowDialog()
        } finally {
            $passwordBox.Clear()
            $secret.Dispose()
            $form.Dispose()
        }
    }
    'Clear' {
        [IO.File]::Delete($credentialPath)
        Write-Output '已清除本机授权副本。请在 Supabase Dashboard 单独撤销临时令牌。'
    }
}
