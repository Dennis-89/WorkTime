<#
.SYNOPSIS
Remove only this user's owned WorkTime task; keep every database and program file.
.DESCRIPTION
Close WorkTime manually via "Anwendung beenden" when desired.
This script removes autostart only; it does not kill a running application.
#>
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)
. (Join-Path $PSScriptRoot 'status.ps1') -RepositoryRoot $RepositoryRoot

function Uninstall-WorkTime {
    param($Config)
    $folder = Get-WorkTimeSchedulerFolder
    $task = Get-WorkTimeTask $folder $Config.TaskName
    if ($null -ne $task) {
        Assert-WorkTimeOwnedTask $task $Config
        $folder.DeleteTask($Config.TaskName, 0)
    }
    if ($null -ne (Get-WorkTimeTask $folder $Config.TaskName)) {
        throw 'WorkTime-Autostart ist nach der Deinstallation noch vorhanden.'
    }
    Write-Host "WorkTime-Autostart entfernt/nicht vorhanden: $($Config.TaskName)"
    Write-Host "Arbeitszeitdaten bleiben erhalten: $($Config.Database)"
    Write-Host 'Programmdateien bleiben erhalten. Eine laufende Anwendung bitte im Tray beenden.'
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    Uninstall-WorkTime (Get-WorkTimeConfig $RepositoryRoot $env:LOCALAPPDATA)
}
