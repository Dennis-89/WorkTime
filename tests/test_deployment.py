"""Deployment tests never connect to the real Task Scheduler."""
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SID = "S-1-5-21-100-200-300-1001"


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(script):
    preamble = "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", preamble + script],
        capture_output=True, timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    # Windows startup diagnostics can use the OEM codepage before our UTF-8
    # preamble executes. Decode only after communicate has collected the bytes.
    output = result.stdout.decode("utf-8")
    errors = result.stderr.decode("oem", errors="backslashreplace")
    assert result.returncode == 0, output + errors
    return output.strip()


def load_scripts():
    return "\n".join(". " + quote(ROOT / "scripts" / name) for name in
                     ("install.ps1", "uninstall.ps1")) + "\n" + TASK_DOUBLE


TASK_DOUBLE = r"""
function New-TestTask {
    param([xml]$Xml)
    # Model the typed COM definition independently of XML node presence.
    $triggers = [pscustomobject]@{
        Count = $Xml.Task.Triggers.ChildNodes.Count
        Trigger = [pscustomobject]@{Enabled = ($Xml.Task.Triggers.LogonTrigger.Enabled -cne 'false')}
    }
    $triggers | Add-Member ScriptMethod Item {
        param([int]$Index)
        if ($Index -ne 1) { throw 'COM TriggerCollection indexes start at 1' }
        return $this.Trigger
    }
    [pscustomobject]@{
        Xml = $Xml.OuterXml
        Definition = [pscustomobject]@{
            Triggers = $triggers
            Principal = [pscustomobject]@{
                RunLevel = $(if ($Xml.Task.Principals.Principal.RunLevel -eq 'HighestAvailable') { 1 } else { 0 })
            }
            Settings = [pscustomobject]@{
                Enabled = ($Xml.Task.Settings.Enabled -cne 'false')
                AllowDemandStart = ($Xml.Task.Settings.AllowStartOnDemand -cne 'false')
            }
        }
    }
}
"""


def config_script(tmp_path):
    # Special characters exercise XML escaping and argument/path handling.
    repo = tmp_path / "repo & user's worktime"
    local = tmp_path / "user profile"
    scripts = repo / ".venv/Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"placeholder, never executed")
    return (load_scripts() +
            "function Get-WorkTimeUserSid { '" + SID + "' }\n" +
            "$config = Get-WorkTimeConfig " + quote(repo) + " " + quote(local) + "\n")


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1", "status.ps1"])
def test_powershell_syntax(name):
    source = quote(ROOT / "scripts" / name)
    output = powershell(
        "$tokens=$null; $errors=$null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile(" +
        source + ", [ref]$tokens, [ref]$errors)\n"
        "if ($errors.Count -gt 0) { throw ($errors | Out-String) }\n'valid'")
    assert output == "valid"


def test_task_xml_and_paths_are_explicit_safe_and_windowless(tmp_path):
    result = json.loads(powershell(config_script(tmp_path) +
                                  "@{Config=$config; Xml=(New-WorkTimeTaskXml $config)} | ConvertTo-Json -Depth 5"))
    config = result["Config"]
    xml = ET.fromstring(result["Xml"])
    def value(path):
        return xml.find(path, NS).text
    assert config["TaskName"] == "WorkTime-" + SID
    assert value("t:Actions/t:Exec/t:Command") == config["Pythonw"]
    assert config["Pythonw"].endswith(r".venv\Scripts\pythonw.exe")
    assert value("t:Actions/t:Exec/t:WorkingDirectory") == config["RepositoryRoot"]
    assert value("t:Actions/t:Exec/t:Arguments") == "-m worktime"
    assert value("t:Principals/t:Principal/t:UserId") == SID
    assert value("t:Triggers/t:LogonTrigger/t:UserId") == SID
    assert value("t:Triggers/t:LogonTrigger/t:Enabled") == "true"
    assert value("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert value("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert value("t:Settings/t:Enabled") == "true"
    assert value("t:Settings/t:AllowStartOnDemand") == "true"
    assert value("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert value("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert value("t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert value("t:Settings/t:StopIfGoingOnBatteries") == "false"
    assert value("t:Settings/t:AllowHardTerminate") == "false"
    assert config["Database"] == str(tmp_path / "user profile/WorkTime/data/worktime.db")
    assert not Path(config["DataDirectory"]).exists()


MOCK_SCHEDULER = r"""
$script:tasks = @{}
$script:registrations = @()
$script:deletions = @()
$script:folder = New-Object PSObject
$script:folder | Add-Member ScriptMethod GetTask {
    param($name)
    if (-not $script:tasks.ContainsKey($name)) {
        throw [Runtime.InteropServices.COMException]::new('missing', -2147024894)
    }
    return $script:tasks[$name]
}
$script:folder | Add-Member ScriptMethod RegisterTask {
    param($name, $xml, $flags, $user, $password, $logon, $sddl)
    if ($name -cne $config.TaskName -or $user -cne $config.UserSid -or
        $null -ne $password -or $null -ne $sddl -or $logon -ne 3 -or $flags -notin @(2,4)) {
        throw 'unsafe registration'
    }
    $script:registrations += $flags
    $script:tasks[$name] = New-TestTask ([xml]$xml)
    return $script:tasks[$name]
}
$script:folder | Add-Member ScriptMethod DeleteTask {
    param($name, $flags)
    if ($name -cne $config.TaskName -or $flags -ne 0) { throw 'foreign deletion' }
    $script:deletions += $name
    $script:tasks.Remove($name)
}
function Get-WorkTimeSchedulerFolder { return $script:folder }
function Test-WorkTimePrerequisites { param($Config) }
function Write-Host { param($Object) }
"""


def test_install_twice_verify_and_uninstall_preserve_data_and_foreign_tasks(tmp_path):
    script = config_script(tmp_path) + MOCK_SCHEDULER + r"""
$script:tasks['ForeignTask'] = [pscustomobject]@{Xml='foreign bytes'}
$one = Install-WorkTime $config
[IO.File]::WriteAllText($config.Database, 'keep these bytes')
$two = Install-WorkTime $config
$verified = Get-WorkTimeInstallationStatus $config $script:folder
Uninstall-WorkTime $config
Uninstall-WorkTime $config
$removed = Get-WorkTimeInstallationStatus $config $script:folder
@{
    First=$one.ConfigurationVerified; Second=$two.ConfigurationVerified
    Verified=$verified.ConfigurationVerified; Removed=(-not $removed.Installed)
    Registrations=$script:registrations; Deletions=$script:deletions
    Data=[IO.File]::ReadAllText($config.Database); Foreign=$script:tasks['ForeignTask'].Xml
} | ConvertTo-Json -Depth 5
"""
    result = json.loads(powershell(script))
    assert result["First"] and result["Second"] and result["Verified"] and result["Removed"]
    assert result["Registrations"] == [2, 4]
    assert result["Deletions"] == ["WorkTime-" + SID]
    assert result["Data"] == "keep these bytes"
    assert result["Foreign"] == "foreign bytes"


@pytest.mark.parametrize("operation", ["Install-WorkTime", "Uninstall-WorkTime"])
@pytest.mark.parametrize("field", ["Source", "URI", "UserId"])
def test_collision_never_changes_task(tmp_path, operation, field):
    edits = {"Source": "$xml.Task.RegistrationInfo.Source = 'foreign'",
             "URI": "$xml.Task.RegistrationInfo.URI = 'foreign'",
             "UserId": "$xml.Task.Principals.Principal.UserId = 'S-1-5-18'"}
    script = config_script(tmp_path) + MOCK_SCHEDULER + """
[xml]$xml = New-WorkTimeTaskXml $config
""" + edits[field] + """
$script:tasks[$config.TaskName] = [pscustomobject]@{Xml=$xml.OuterXml}
$before = $script:tasks[$config.TaskName].Xml
$rejected=$false
try { """ + operation + """ $config } catch { $rejected=$true }
@{Rejected=$rejected; Registers=$script:registrations.Count; Deletes=$script:deletions.Count;
  Unchanged=($before -ceq $script:tasks[$config.TaskName].Xml)} | ConvertTo-Json
"""
    assert json.loads(powershell(script)) == {
        "Rejected": True, "Registers": 0, "Deletes": 0, "Unchanged": True}


@pytest.mark.parametrize("change", [
    "$xml.Task.Actions.Exec.Command = 'cmd.exe'",
    "$xml.Task.Actions.Exec.Arguments = 'wrong'",
    "$xml.Task.Principals.Principal.RunLevel = 'HighestAvailable'",
    "$xml.Task.Principals.Principal.LogonType = 'Password'",
    "$xml.Task.Settings.Enabled = 'false'",
    "$xml.Task.Settings.ExecutionTimeLimit = 'PT72H'",
    "$xml.Task.Triggers.LogonTrigger.UserId = 'S-1-5-18'",
    "[void]$xml.Task.Actions.AppendChild($xml.Task.Actions.Exec.CloneNode($true))",
])
def test_status_detects_configuration_drift(tmp_path, change):
    script = config_script(tmp_path) + "\n[xml]$xml = New-WorkTimeTaskXml $config\n" + change + "\n" + """
Test-WorkTimeTaskConfiguration (New-TestTask $xml) $config
"""
    assert powershell(script) == "False"


def test_access_denied_is_not_mistaken_for_missing_task(tmp_path):
    script = config_script(tmp_path) + """
$folder = New-Object PSObject
$folder | Add-Member ScriptMethod GetTask {
    param($name)
    throw [Runtime.InteropServices.COMException]::new('denied', -2147024891)
}
$rejected=$false
try { Get-WorkTimeTask $folder $config.TaskName } catch { $rejected=$true }
if (-not $rejected) { throw 'access error swallowed' }
'blocked'
"""
    assert powershell(script) == "blocked"


@pytest.mark.parametrize("value", ["", "relative", "C:relative", r"\relative-to-drive"])
def test_script_rejects_unstable_localappdata(value):
    script = load_scripts() + """
function Get-WorkTimeUserSid { '""" + SID + """' }
$rejected=$false
try { Get-WorkTimeConfig 'C:\\WorkTime' """ + quote(value) + """ } catch { $rejected=$true }
if (-not $rejected) { throw 'unstable path accepted' }
'blocked'
"""
    assert powershell(script) == "blocked"


def test_missing_pythonw_is_rejected_before_task_access(tmp_path):
    script = config_script(tmp_path)
    root = tmp_path / "repo & user's worktime"
    (root / ".venv/Scripts/pythonw.exe").unlink()
    (root / ".venv/Scripts/python.exe").write_bytes(b"placeholder, never executed")
    script += """
$rejected=$false
try { Test-WorkTimePrerequisites $config } catch {
    $rejected=($_.Exception.Message -like '*pythonw.exe*')
}
if (-not $rejected) { throw 'missing venv accepted' }
'blocked'
"""
    assert powershell(script) == "blocked"


def test_actual_venv_prerequisite_check_imports_without_starting_app(tmp_path):
    # Imports only: no factory, mutex, tray, data directory or scheduler access.
    script = load_scripts() + "\n$config = Get-WorkTimeConfig " + quote(ROOT) + " " + quote(tmp_path) + "\n"
    script += "Test-WorkTimePrerequisites $config\n'imports ready'"
    assert powershell(script) == "imports ready"
    assert not (tmp_path / "WorkTime").exists()


IDENTITY_DOUBLE = r"""
$script:realResolver = ${function:Resolve-WorkTimeUserSid}
function Resolve-WorkTimeUserSid {
    param([string]$Identity)
    switch ($Identity) {
        'MEHRER\straub' { return $config.UserSid }
        'OTHER\user' { return 'S-1-5-21-999-888-777-1002' }
        'UNKNOWN\user' { throw 'IdentityNotMappedException (test double)' }
        default { return & $script:realResolver $Identity }
    }
}
"""


@pytest.mark.parametrize("principal_account", [False, True])
def test_real_windows_normalization_verifies_same_identity_and_com_defaults(tmp_path, principal_account):
    script = config_script(tmp_path) + MOCK_SCHEDULER + IDENTITY_DOUBLE + r"""
[xml]$xml = New-WorkTimeTaskXml $config
$xml.Task.Triggers.LogonTrigger.UserId = 'MEHRER\straub'
[void]$xml.Task.Principals.Principal.RemoveChild($xml.Task.Principals.Principal.SelectSingleNode('*[local-name()="RunLevel"]'))
[void]$xml.Task.Settings.RemoveChild($xml.Task.Settings.SelectSingleNode('*[local-name()="Enabled"]'))
[void]$xml.Task.Settings.RemoveChild($xml.Task.Settings.SelectSingleNode('*[local-name()="AllowStartOnDemand"]'))
[void]$xml.Task.Triggers.LogonTrigger.RemoveChild($xml.Task.Triggers.LogonTrigger.SelectSingleNode('*[local-name()="Enabled"]'))
"""
    if principal_account:
        script += "$xml.Task.Principals.Principal.UserId = 'MEHRER\\straub'\n"
    script += r"""
$task = New-TestTask $xml
$task.Definition.Principal.RunLevel = 0
$task.Definition.Settings.Enabled = $true
$task.Definition.Settings.AllowDemandStart = $true
$task.Definition.Triggers.Trigger.Enabled = $true
$script:tasks[$config.TaskName] = $task
(Get-WorkTimeInstallationStatus $config $script:folder).ConfigurationVerified
if ($script:registrations.Count -ne 0 -or $script:deletions.Count -ne 0) { throw 'status wrote task' }
"""
    assert powershell(script) == "True"


@pytest.mark.parametrize("node", ["Principal", "Trigger"])
@pytest.mark.parametrize("identity", ["OTHER\\user", "S-1-5-21-999-888-777-1002", "UNKNOWN\\user"])
def test_foreign_and_unresolvable_identities_fail_closed(tmp_path, node, identity):
    member = "Principals.Principal" if node == "Principal" else "Triggers.LogonTrigger"
    script = config_script(tmp_path) + IDENTITY_DOUBLE + "\n[xml]$xml = New-WorkTimeTaskXml $config\n"
    script += "$xml.Task." + member + ".UserId = " + quote(identity) + "\n"
    script += """
$verified = $false
try { $verified = Test-WorkTimeTaskConfiguration (New-TestTask $xml) $config } catch { }
if ($verified) { throw 'foreign or unknown identity accepted' }
'rejected'
"""
    assert powershell(script) == "rejected"


@pytest.mark.parametrize("field,xml_path,good,bad", [
    ("Principal.RunLevel", "Principals/Principal/RunLevel", "0", "1"),
    ("Settings.Enabled", "Settings/Enabled", "$true", "$false"),
    ("Settings.AllowDemandStart", "Settings/AllowStartOnDemand", "$true", "$false"),
])
def test_omitted_xml_defaults_require_positive_com_evidence(tmp_path, field, xml_path, good, bad):
    xpath = "/".join("*[local-name()='" + name + "']" for name in xml_path.split("/"))
    script = config_script(tmp_path) + "\n[xml]$xml = New-WorkTimeTaskXml $config\n"
    script += "$node = $xml.Task.SelectSingleNode(" + quote(xpath) + ")\n[void]$node.ParentNode.RemoveChild($node)\n"
    script += "$task = New-TestTask $xml\n$task.Definition." + field + " = " + good + "\n"
    script += "if (-not (Test-WorkTimeTaskConfiguration $task $config)) { throw 'valid COM default rejected' }\n"
    for value in (bad, "$null"):
        script += "$task.Definition." + field + " = " + value + "\n"
        script += "if (Test-WorkTimeTaskConfiguration $task $config) { throw 'unsafe/missing COM value accepted' }\n"
    script += "'verified good, rejected bad and missing'"
    assert powershell(script) == "verified good, rejected bad and missing"


def test_com_definition_failure_is_not_assumed_safe(tmp_path):
    script = config_script(tmp_path) + """
$task = [pscustomobject]@{Xml=(New-WorkTimeTaskXml $config)}
if (Test-WorkTimeTaskConfiguration $task $config) { throw 'missing definition accepted' }
$task | Add-Member ScriptProperty Definition { throw 'COM access error' }
# PowerShell can turn a failing ScriptProperty getter into a null value.
# Either an exception or False is a safe rejection; True must never result.
$verified = $false
try { $verified = Test-WorkTimeTaskConfiguration $task $config } catch { }
if ($verified) { throw 'COM failure accepted' }
'rejected'
"""
    assert powershell(script) == "rejected"


def test_sid_resolver_canonicalizes_and_rejects_invalid_values():
    script = load_scripts() + "\nif ((Resolve-WorkTimeUserSid '" + SID + "') -cne '" + SID + "') { throw 'SID changed' }\n"
    script += """
foreach ($bad in @('', ' ', 'S-invalid', 'S-1-5-not-a-number')) {
    $rejected = $false
    try { Resolve-WorkTimeUserSid $bad } catch { $rejected = $true }
    if (-not $rejected) { throw 'invalid identity accepted' }
}
'canonical, invalid rejected'
"""
    assert powershell(script) == "canonical, invalid rejected"


def test_sid_resolver_translates_current_ntaccount_using_dotnet():
    # Read-only identity resolution; no scheduler or session changes.
    script = load_scripts() + """
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ((Resolve-WorkTimeUserSid $identity.Name) -cne $identity.User.Value) { throw 'wrong translated SID' }
'translated'
"""
    assert powershell(script) == "translated"


def test_remaining_xml_constraints_are_not_relaxed(tmp_path):
    mutations = [
        "$xml.Task.RegistrationInfo.Source = 'foreign'",
        "$xml.Task.RegistrationInfo.URI = 'foreign'",
        "$xml.Task.Actions.Exec.Command = 'wrong.exe'",
        "$xml.Task.Actions.Exec.Arguments = 'wrong'",
        "$xml.Task.Actions.Exec.WorkingDirectory = 'C:\\wrong'",
        "$xml.Task.Principals.Principal.LogonType = 'Password'",
        "$xml.Task.Settings.MultipleInstancesPolicy = 'Parallel'",
        "$xml.Task.Settings.ExecutionTimeLimit = 'PT1H'",
        "$xml.Task.Settings.DisallowStartIfOnBatteries = 'true'",
        "$xml.Task.Settings.StopIfGoingOnBatteries = 'true'",
        "$xml.Task.Settings.AllowHardTerminate = 'true'",
        "$xml.Task.Settings.StartWhenAvailable = 'false'",
        "$xml.Task.Triggers.LogonTrigger.Enabled = 'false'",
        "$xml.Task.Actions.Context = 'foreign'",
        "$xml.Task.Principals.Principal.id = 'foreign'",
        "[void]$xml.Task.Actions.AppendChild($xml.Task.Actions.Exec.CloneNode($true))",
        "[void]$xml.Task.Triggers.AppendChild($xml.Task.Triggers.LogonTrigger.CloneNode($true))",
        "[void]$xml.Task.Principals.AppendChild($xml.Task.Principals.Principal.CloneNode($true))",
    ]
    script = config_script(tmp_path)
    for number, mutation in enumerate(mutations):
        script += "\n[xml]$xml = New-WorkTimeTaskXml $config\n" + mutation + "\n"
        script += "$verified = $false\ntry { $verified = Test-WorkTimeTaskConfiguration (New-TestTask $xml) $config } catch { }\n"
        script += "if ($verified) { throw 'drift accepted: " + str(number) + "' }\n"
    script += "'all strict guards preserved'"
    assert powershell(script) == "all strict guards preserved"


@pytest.mark.parametrize("mutation,expected", [
    ("$task.Definition.Triggers.Trigger.Enabled = $true", "True"),
    ("$task.Definition.Triggers.Trigger.Enabled = $false", "False"),
    ("$task.Definition.Triggers.Trigger.Enabled = $null", "False"),
    ("$task.Definition.Triggers.Trigger.PSObject.Properties.Remove('Enabled')", "False"),
    ("$task.Definition.Triggers.Trigger.Enabled = 'true'", "False"),
    ("$task.Definition.Triggers.Trigger = $null", "False"),
    ("$task.Definition.Triggers = $null", "False"),
    ("$task.Definition.Triggers.Count = 0", "False"),
    ("$task.Definition.Triggers.Count = 2", "False"),
    ("$task.Definition.Triggers.Count = $null", "False"),
    ("$task.Definition = $null", "False"),
    ("$task.Definition | Add-Member ScriptProperty Triggers { throw 'COM collection error' } -Force", "False"),
    ("$task.Definition.Triggers | Add-Member ScriptMethod Item { param($Index) throw 'COM Item error' } -Force", "False"),
    ("$task.Definition.Triggers.Trigger | Add-Member ScriptProperty Enabled { throw 'COM Enabled error' } -Force", "False"),
])
def test_omitted_trigger_enabled_requires_positive_com_evidence(tmp_path, mutation, expected):
    script = config_script(tmp_path) + MOCK_SCHEDULER + r"""
[xml]$xml = New-WorkTimeTaskXml $config
$node = $xml.Task.Triggers.LogonTrigger.SelectSingleNode('*[local-name()="Enabled"]')
[void]$node.ParentNode.RemoveChild($node)
$task = New-TestTask $xml
""" + mutation + r"""
$script:tasks[$config.TaskName] = $task
$verified = $false
try { $verified = (Get-WorkTimeInstallationStatus $config $script:folder).ConfigurationVerified } catch { }
$verified
if ($script:registrations.Count -ne 0 -or $script:deletions.Count -ne 0) { throw 'status changed a task' }
"""
    assert powershell(script) == expected


def test_extra_xml_trigger_rejected_even_with_one_com_trigger(tmp_path):
    script = config_script(tmp_path) + r"""
[xml]$xml = New-WorkTimeTaskXml $config
$task = New-TestTask $xml
[void]$xml.Task.Triggers.AppendChild($xml.Task.Triggers.LogonTrigger.CloneNode($true))
$task.Xml = $xml.OuterXml
Test-WorkTimeTaskConfiguration $task $config
"""
    assert powershell(script) == "False"
