<#
.SYNOPSIS
Install/update only the current user's WorkTime logon task. No elevation required.
.DESCRIPTION
Prepare the fixed repository once:
  .venv\Scripts\python.exe -m pip install -e ".[dev]"
Then run this script in a normal PowerShell, not "as administrator".
Keep this repository and its .venv at this path. Rerun install.ps1 after moving it.
No app start or user logoff is forced by this script.
.EXAMPLE
.\scripts\install.ps1
#>
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)
. (Join-Path $PSScriptRoot 'status.ps1') -RepositoryRoot $RepositoryRoot

function Install-WorkTime {
    param($Config)
    Test-WorkTimePrerequisites $Config
    $folder = Get-WorkTimeSchedulerFolder
    $existing = Get-WorkTimeTask $folder $Config.TaskName
    $flags = 2 # TASK_CREATE: never silently replace a task created after our check.
    if ($null -ne $existing) {
        Assert-WorkTimeOwnedTask $existing $Config
        $flags = 4 # TASK_UPDATE: only after checking SID and WorkTime markers.
    }
    [IO.Directory]::CreateDirectory($Config.DataDirectory) | Out-Null
    $xml = New-WorkTimeTaskXml $Config
    # TASK_LOGON_INTERACTIVE_TOKEN=3; no password, no SYSTEM, no elevation.
    $null = $folder.RegisterTask($Config.TaskName, $xml, $flags, $Config.UserSid, $null, 3, $null)
    $status = Get-WorkTimeInstallationStatus $Config $folder
    if (-not $status.ConfigurationVerified -or -not $status.PythonwExists) {
        throw 'Registrierter WorkTime-Task konnte nicht vollstaendig verifiziert werden.'
    }
    Write-Host "WorkTime-Autostart eingerichtet: $($Config.TaskName)"
    Write-Host "Produktive Datenbank: $($Config.Database)"
    Write-Host "Fester Programmstart: $($Config.Pythonw) -m worktime"
    Write-Host 'Einmalig: WorkTime-Symbol aus ^ in den sichtbaren Infobereich ziehen.'
    Write-Host 'Alternativ: Einstellungen > Personalisierung > Taskleiste > Andere Symbole im Infobereich.'
    Write-Host 'Dort WorkTime bzw. den zugeordneten Python-Eintrag aktivieren.'
    Write-Host 'Danach WorkTime-Neustart und Windows-Neuanmeldung pruefen. Windows steuert die Sichtbarkeit.'
    Write-Host 'Kein Explorer-/Registry-Eingriff; die vorhandene pystray-API bietet keine feste Icon-GUID.'
    $legacy = Join-Path $Config.RepositoryRoot 'data\worktime.db'
    if (Test-Path -LiteralPath $legacy -PathType Leaf) {
        Write-Warning "Alte Repository-Datenbank vorhanden: $legacy. Keine automatische Migration!"
        Write-Host 'Falls diese Daten uebernommen werden sollen: WorkTime vollstaendig beenden, beide Datenbanken'
        Write-Host 'sichern und die alte Datenbank nur bei noch unbenutztem Ziel manuell uebernehmen.'
        Write-Host 'Vorhandene produktive Daten nicht ueberschreiben oder Datenbanken ungeprueft zusammenfuehren.'
    }
    return $status
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    Install-WorkTime (Get-WorkTimeConfig $RepositoryRoot $env:LOCALAPPDATA)
}
