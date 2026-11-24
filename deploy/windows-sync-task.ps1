<#
.SYNOPSIS
Schedule `dineoncampus-mcp sync` on Windows so a cloud-hosted server keeps getting menus.

.DESCRIPTION
Installs (or upgrades) dineoncampus-mcp with uv, stores the sync settings and ingest token in
%APPDATA%\dineoncampus-mcp\sync.json (readable only by you), and registers a Task Scheduler
task that runs the sync at the given times. A run missed while the PC was off happens when it
next starts. Running the script again updates the settings and replaces the task.

.EXAMPLE
.\windows-sync-task.ps1 -Server https://3-145-27-189.sslip.io -Token <ingest token> `
    -LocationId 615f4f93a9f13a32678e5feb -SiteId 5c86b28a8e45bf0ccb9ec92b -Tz America/New_York
#>
param(
    [Parameter(Mandatory = $true)] [string]$Server,
    [Parameter(Mandatory = $true)] [string]$Token,
    [Parameter(Mandatory = $true)] [string]$LocationId,
    [string]$SiteId = "",
    [string]$Tz = "America/New_York",
    [int]$Days = 7,
    [string[]]$Times = @("06:00", "11:00", "16:00", "21:00"),
    [string]$TaskName = "DineOnCampus MCP Sync",
    [string]$Source = "git+https://github.com/Pixel2075/dineoncampus-mcp"
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun."
}

Write-Host "Installing dineoncampus-mcp from $Source"
uv tool install --force $Source
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }

$toolDir = (uv tool dir).Trim()
$pythonw = Join-Path $toolDir "dineoncampus-mcp\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Could not find $pythonw" }

$confDir = Join-Path $env:APPDATA "dineoncampus-mcp"
New-Item -ItemType Directory -Force -Path $confDir | Out-Null
$config = Join-Path $confDir "sync.json"
$log = Join-Path $confDir "sync.log"

$settingsJson = [ordered]@{
    server      = $Server
    token       = $Token
    location_id = $LocationId
    site_id     = $SiteId
    tz          = $Tz
    days        = $Days
} | ConvertTo-Json
[System.IO.File]::WriteAllText($config, $settingsJson, (New-Object System.Text.UTF8Encoding($false)))
icacls $config /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m dineoncampus_mcp sync --config `"$config`" --log `"$log`"" -WorkingDirectory $confDir
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $taskSettings `
    -Description "Fetch DineOnCampus menus and push them to $Server" -Force | Out-Null

Write-Host "Registered task '$TaskName' at $($Times -join ', '). Running it once now..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Watch progress in $log"
