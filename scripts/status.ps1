<#
.SYNOPSIS
Read-only verification of this user's WorkTime autostart.
.DESCRIPTION
Keep this repository and its .venv at a fixed path after installation.
This file also contains shared, independently testable deployment functions.
#>
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)

function Get-WorkTimeUserSid {
    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Resolve-WorkTimeUserSid {
    param([string]$Identity)
    if ([string]::IsNullOrWhiteSpace($Identity)) {
        throw 'Leere Benutzeridentitaet kann nicht verifiziert werden.'
    }
    if ($Identity -match '^S-') {
        return [Security.Principal.SecurityIdentifier]::new($Identity).Value
    }
    $account = [Security.Principal.NTAccount]::new($Identity)
    # Translation failures must propagate; an unknown account is never a match.
    return $account.Translate([Security.Principal.SecurityIdentifier]).Value
}

function Get-WorkTimeConfig {
    param([string]$RepositoryRoot, [string]$LocalAppData)
    if ([string]::IsNullOrWhiteSpace($LocalAppData) -or
        $LocalAppData -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') {
        throw 'LOCALAPPDATA muss ein absoluter Benutzerpfad sein.'
    }
    $root = [IO.Path]::GetFullPath($RepositoryRoot)
    $sid = Get-WorkTimeUserSid
    if ($sid -notmatch '^S-\d+(-\d+)+$' -or $sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) {
        throw 'Installation nur fuer ein interaktiv angemeldetes Benutzerkonto.'
    }
    [pscustomobject]@{
        RepositoryRoot = $root
        Python = Join-Path $root '.venv\Scripts\python.exe'
        Pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
        DataDirectory = Join-Path $LocalAppData 'WorkTime\data'
        Database = Join-Path $LocalAppData 'WorkTime\data\worktime.db'
        UserSid = $sid
        TaskName = "WorkTime-$sid"
        Source = 'WorkTime.MVP.Autostart.v1'
        Uri = "\WorkTime-$sid"
    }
}

function New-WorkTimeTaskXml {
    param($Config)
    $sid = [Security.SecurityElement]::Escape($Config.UserSid)
    $exe = [Security.SecurityElement]::Escape($Config.Pythonw)
    $root = [Security.SecurityElement]::Escape($Config.RepositoryRoot)
    $source = [Security.SecurityElement]::Escape($Config.Source)
    $uri = [Security.SecurityElement]::Escape($Config.Uri)
    return @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$sid</Author>
    <Description>WorkTime: lokaler, interaktiver Benutzer-Autostart.</Description>
    <Source>$source</Source>
    <URI>$uri</URI>
  </RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="WorkTime">
    <UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="WorkTime"><Exec>
    <Command>$exe</Command><Arguments>-m worktime</Arguments><WorkingDirectory>$root</WorkingDirectory>
  </Exec></Actions>
</Task>
"@
}

function Get-WorkTimeSchedulerFolder {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    return $service.GetFolder('\')
}

function Get-WorkTimeTask {
    param($Folder, [string]$TaskName)
    try {
        return $Folder.GetTask($TaskName)
    } catch {
        $errorObject = $_.Exception
        while ($null -ne $errorObject.InnerException) {
            $errorObject = $errorObject.InnerException
        }
        # Only ERROR_FILE_NOT_FOUND means "not installed"; access errors propagate.
        if ($errorObject.HResult -eq -2147024894) { return $null }
        throw
    }
}

function Read-WorkTimeTaskXml {
    param($Task)
    $document = New-Object System.Xml.XmlDocument
    $document.XmlResolver = $null
    $document.LoadXml($Task.Xml)
    return ,$document
}

function Get-WorkTimeXmlValue {
    param([xml]$Document, [string]$Path)
    $namespaces = New-Object System.Xml.XmlNamespaceManager($Document.NameTable)
    $namespaces.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $node = $Document.SelectSingleNode($Path, $namespaces)
    if ($null -eq $node) { return '' }
    return $node.InnerText
}

function Assert-WorkTimeOwnedTask {
    param($Task, $Config)
    $document = Read-WorkTimeTaskXml $Task
    if ((Get-WorkTimeXmlValue $document '/t:Task/t:RegistrationInfo/t:Source') -cne $Config.Source -or
        (Get-WorkTimeXmlValue $document '/t:Task/t:RegistrationInfo/t:URI') -cne $Config.Uri -or
        (Resolve-WorkTimeUserSid (Get-WorkTimeXmlValue $document '/t:Task/t:Principals/t:Principal/t:UserId')) -cne
        (Resolve-WorkTimeUserSid $Config.UserSid)) {
        throw "Namenskollision: Task '$($Config.TaskName)' gehoert nicht dieser WorkTime-Installation. Keine Aenderung."
    }
}

function Test-WorkTimeTaskConfiguration {
    param($Task, $Config)
    Assert-WorkTimeOwnedTask $Task $Config
    $document = Read-WorkTimeTaskXml $Task
    if ((Resolve-WorkTimeUserSid (Get-WorkTimeXmlValue $document '/t:Task/t:Triggers/t:LogonTrigger/t:UserId')) -cne
        (Resolve-WorkTimeUserSid $Config.UserSid)) { return $false }
    # The scheduler may omit default XML nodes. Read the actual COM values,
    # never infer safe privileges/settings merely from a missing XML element.
    $definition = $Task.Definition
    $runLevel = $definition.Principal.RunLevel
    $enabled = $definition.Settings.Enabled
    $allowDemandStart = $definition.Settings.AllowDemandStart
    if ($null -eq $runLevel -or $runLevel -ne 0 -or # TASK_RUNLEVEL_LUA
        $enabled -isnot [bool] -or -not $enabled -or
        $allowDemandStart -isnot [bool] -or -not $allowDemandStart) { return $false }
    $triggers = $definition.Triggers
    if ($null -eq $triggers -or $triggers.Count -ne 1) { return $false }
    # Task Scheduler's COM TriggerCollection uses one-based Item indexes.
    $trigger = $triggers.Item(1)
    $triggerEnabled = $trigger.Enabled
    if ($triggerEnabled -isnot [bool] -or -not $triggerEnabled) { return $false }
    $expected = @{
        '/t:Task/t:Actions/t:Exec/t:Command' = $Config.Pythonw
        '/t:Task/t:Actions/t:Exec/t:Arguments' = '-m worktime'
        '/t:Task/t:Actions/t:Exec/t:WorkingDirectory' = $Config.RepositoryRoot
        '/t:Task/t:Principals/t:Principal/t:LogonType' = 'InteractiveToken'
        '/t:Task/t:Settings/t:MultipleInstancesPolicy' = 'IgnoreNew'
        '/t:Task/t:Settings/t:ExecutionTimeLimit' = 'PT0S'
        '/t:Task/t:Settings/t:DisallowStartIfOnBatteries' = 'false'
        '/t:Task/t:Settings/t:StopIfGoingOnBatteries' = 'false'
        '/t:Task/t:Settings/t:AllowHardTerminate' = 'false'
        '/t:Task/t:Settings/t:StartWhenAvailable' = 'true'
    }
    foreach ($key in $expected.Keys) {
        if ((Get-WorkTimeXmlValue $document $key) -cne $expected[$key]) { return $false }
    }
    # Reject additional actions/triggers/principals as well as changed values.
    return ($document.DocumentElement.Actions.ChildNodes.Count -eq 1 -and
            $document.DocumentElement.Triggers.ChildNodes.Count -eq 1 -and
            $document.DocumentElement.Principals.ChildNodes.Count -eq 1 -and
            $document.DocumentElement.Actions.Context -ceq 'WorkTime' -and
            $document.DocumentElement.Principals.Principal.id -ceq 'WorkTime')
}

function Test-WorkTimePrerequisites {
    param($Config)
    foreach ($file in @($Config.Python, $Config.Pythonw,
                        (Join-Path $Config.RepositoryRoot 'pyproject.toml'),
                        (Join-Path $Config.RepositoryRoot 'src\worktime\__main__.py'))) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Voraussetzung fehlt: $file" }
    }
    $check = "import pathlib,sys,tkinter,pystray; import worktime.app,worktime.paths,worktime.single_instance; assert pathlib.Path(worktime.app.__file__).resolve() == (pathlib.Path(sys.argv[1])/'src/worktime/app.py').resolve(), 'venv verweist auf ein anderes Repository'"
    & $Config.Python -B -c $check $Config.RepositoryRoot
    if ($LASTEXITCODE -ne 0) { throw 'venv nicht bereit. Zuerst python.exe -m pip install -e ".[dev]" ausfuehren.' }
}

function Get-WorkTimeInstallationStatus {
    param($Config, $Folder)
    $task = Get-WorkTimeTask $Folder $Config.TaskName
    $verified = $false
    if ($null -ne $task) { $verified = Test-WorkTimeTaskConfiguration $task $Config }
    [pscustomobject]@{
        TaskName = $Config.TaskName
        Installed = ($null -ne $task)
        ConfigurationVerified = $verified
        Pythonw = $Config.Pythonw
        PythonwExists = (Test-Path -LiteralPath $Config.Pythonw -PathType Leaf)
        WorkingDirectory = $Config.RepositoryRoot
        Database = $Config.Database
        DatabaseExists = (Test-Path -LiteralPath $Config.Database -PathType Leaf)
        UserSid = $Config.UserSid
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $config = Get-WorkTimeConfig $RepositoryRoot $env:LOCALAPPDATA
    Get-WorkTimeInstallationStatus $config (Get-WorkTimeSchedulerFolder)
}
