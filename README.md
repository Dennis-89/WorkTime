# WorkTime

WorkTime ist eine lokale Windows-Anwendung zur persönlichen Arbeitszeiterfassung.

Die Anwendung läuft im Windows-Infobereich (System Tray), speichert die Arbeitszeit lokal in SQLite und startet nach der Windows-Anmeldung automatisch im Benutzerkontext.

## Funktionen

- Arbeitsbeginn erfassen
- Pause starten und beenden
- Feierabend erfassen
- Tagesübersicht
- Wochenübersicht
- manuelle Korrektur von Arbeitszeiten
- automatischer Start bei Windows-Anmeldung
- automatische Fortführung eines bereits laufenden Arbeitstags
- Windows-Sperren (`Win+L`) wird **nicht** als Pause gewertet
- Schutz vor mehreren gleichzeitig laufenden WorkTime-Instanzen
- lokale Datenspeicherung ohne Cloud-Anbindung

## Systemvoraussetzungen

- Windows 11
- Python-Umgebung im Projektordner
- PowerShell
- Repository an einem festen Speicherort

WorkTime wird im aktuellen MVP **nicht** als klassische Anwendung nach `C:\Program Files` installiert.
Die Anwendung läuft direkt aus dem Repository und verwendet dessen virtuelle Python-Umgebung.

## Verzeichnisstruktur

Beispiel:

```text
WorkTime\
├─ .venv\
├─ scripts\
│  ├─ install.ps1
│  ├─ status.ps1
│  └─ uninstall.ps1
├─ src\
│  └─ worktime\
├─ tests\
└─ pyproject.toml
```

## Installation

### 1. Repository an einen festen Ort legen

Beispiel:

```text
C:\Users\<Benutzer>\Documents\Programming\WorkTime
```

Nach der Installation sollte dieser Ordner nicht mehr verschoben werden, weil der Windows-Autostart auf die dortige `pythonw.exe` verweist.

Wenn das Repository später verschoben wird, muss `install.ps1` am neuen Ort erneut ausgeführt werden.

### 2. Virtuelle Python-Umgebung vorbereiten

Falls `.venv` bereits vorhanden und eingerichtet ist:

```powershell
Set-Location "C:\Pfad\zu\WorkTime"

.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Falls noch keine `.venv` vorhanden ist:

```powershell
Set-Location "C:\Pfad\zu\WorkTime"

py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 3. Autostart installieren

In einer **normalen PowerShell**, nicht als Administrator:

```powershell
Set-Location "C:\Pfad\zu\WorkTime"

.\scripts\install.ps1
```

Das Skript:

- prüft die Python-Umgebung
- legt den produktiven Datenordner an
- richtet einen benutzerspezifischen Windows-Task ein
- verwendet `pythonw.exe`, damit kein Konsolenfenster erscheint
- prüft die registrierte Task-Konfiguration anschließend erneut

### 4. Installation prüfen

```powershell
.\scripts\status.ps1
```

Erwarteter Zustand:

```text
Installed             : True
ConfigurationVerified : True
PythonwExists         : True
```

`DatabaseExists` kann vor dem ersten Start noch `False` sein.

## Erster manueller Start

Das Installationsskript startet WorkTime nicht automatisch.

Für den ersten manuellen Start:

```powershell
Start-Process `
    -FilePath ".\.venv\Scripts\pythonw.exe" `
    -ArgumentList "-m","worktime" `
    -WorkingDirectory (Get-Location).Path `
    -WindowStyle Hidden
```

Danach erscheint das WorkTime-Symbol im Windows-Infobereich.

Falls es nur unter `^` angezeigt wird, das Symbol einmal in den sichtbaren Bereich ziehen.

Alternativ:

```text
Einstellungen
→ Personalisierung
→ Taskleiste
→ Andere Symbole im Infobereich
```

Dort WorkTime bzw. den zugeordneten Python-Eintrag aktivieren.

Windows verwaltet die Position des Tray-Symbols; WorkTime erzwingt diese Position nicht selbst.

## Bedienung

WorkTime wird über das Tray-Symbol bedient.

### Noch nicht gestartet

Typische Anzeige:

```text
Noch nicht gestartet
Arbeit starten
Tagesübersicht
Wochenübersicht
Anwendung beenden
```

Mit **Arbeit starten** beginnt der Arbeitstag.

Beim ersten relevanten Start bzw. bei einer Windows-Anmeldung oder Entsperrung kann WorkTime einen noch nicht gestarteten Arbeitstag automatisch beginnen.

### Arbeit läuft

Typische Anzeige:

```text
Arbeit läuft
Pause
Feierabend
Tagesübersicht
Wochenübersicht
Anwendung beenden
```

- **Pause** startet eine Pause.
- **Feierabend** beendet den Arbeitstag.
- **Anwendung beenden** beendet nur WorkTime, nicht den Arbeitstag.

### Pause

Typische Anzeige:

```text
Pause
Weiter
Feierabend
Tagesübersicht
Wochenübersicht
Anwendung beenden
```

- **Weiter** beendet die Pause.
- **Feierabend** beendet den Arbeitstag auch dann, wenn gerade eine Pause aktiv ist.

### Feierabend

Nach Feierabend kann derselbe Arbeitstag nicht erneut automatisch gestartet werden.

Typische Anzeige:

```text
Feierabend
Tagesübersicht
Wochenübersicht
Anwendung beenden
```

Am nächsten Kalendertag kann WorkTime wieder automatisch einen neuen Arbeitstag beginnen.

## Tagesübersicht

Die Tagesübersicht zeigt unter anderem:

- Datum
- Arbeitsbeginn
- Arbeitsende
- Bruttozeit
- Pausenzeit
- Nettoarbeitszeit
- Zustand
- vorhandene Pausen
- Hinweis auf unvollständige Daten

Unvollständige oder falsche Daten können über die Korrekturfunktion angepasst werden.

## Wochenübersicht

Die Wochenübersicht zeigt:

- Montag bis Sonntag
- Nettoarbeitszeit je Tag
- Kennzeichnung unvollständiger Tage
- gesamte Nettoarbeitszeit der Woche
- Navigation zur Vorwoche
- aktuelle Woche
- Folgewoche

## Korrekturen

Arbeitszeiten können manuell korrigiert werden.

Dabei können insbesondere angepasst werden:

- Arbeitsbeginn
- Arbeitsende
- Pausen

Die Eingabe von Uhrzeiten erfolgt im Format:

```text
HH:MM
```

Korrekturen werden nachvollziehbar in der lokalen Datenhaltung berücksichtigt.

## Verhalten bei Windows-Sperre

`Win+L` bzw. das Sperren des Rechners ist **keine Pause**.

Beispiel:

```text
08:00 Arbeitsbeginn
10:00 Windows sperren
11:00 Windows entsperren
```

WorkTime wertet die Zeit von 10:00 bis 11:00 weiterhin als Arbeitszeit, solange keine Pause manuell gestartet wurde.

## Verhalten bei Neustart

Wenn Windows während eines laufenden Arbeitstags neu gestartet wird:

- WorkTime startet nach der Anmeldung automatisch
- der bestehende Arbeitstag bleibt erhalten
- es wird kein doppelter Arbeitsbeginn erzeugt
- die bisherige Arbeitszeit wird fortgeführt

Ein Windows-Neustart ist daher kein Feierabend.

## Anwendung beenden

**Anwendung beenden** beendet nur das Tray-Programm.

Es wird dadurch **kein Feierabend-Ereignis** erzeugt.

Wird WorkTime anschließend am selben Tag erneut gestartet, wird der bestehende Arbeitstag fortgeführt.

Für das tatsächliche Ende des Arbeitstags immer **Feierabend** verwenden.

## Datenspeicherung

Die produktive SQLite-Datenbank liegt unter:

```text
%LOCALAPPDATA%\WorkTime\data\worktime.db
```

Typischer vollständiger Pfad:

```text
C:\Users\<Benutzer>\AppData\Local\WorkTime\data\worktime.db
```

Die Datenbank liegt bewusst **nicht** im Repository.

## Datensicherung

Für eine manuelle Sicherung:

1. WorkTime über **Anwendung beenden** schließen.
2. Folgende Datei kopieren:

```text
%LOCALAPPDATA%\WorkTime\data\worktime.db
```

3. Kopie an einem sicheren Ort aufbewahren.

Vorhandene produktive Datenbanken nicht ungeprüft überschreiben oder mit einer anderen Datenbank zusammenführen.

## Alte Repository-Datenbank

Ältere Entwicklungsstände können noch folgende Datei enthalten:

```text
data\worktime.db
```

Diese Datei wird **nicht automatisch** in die produktive Datenbank übernommen.

Die produktive Datenbank ist:

```text
%LOCALAPPDATA%\WorkTime\data\worktime.db
```

Eine alte Datenbank nur manuell übernehmen, wenn WorkTime vollständig beendet ist und das produktive Ziel noch nicht benutzt wurde. Vorher beide Dateien sichern.

## Installationsstatus prüfen

Jederzeit möglich:

```powershell
Set-Location "C:\Pfad\zu\WorkTime"

.\scripts\status.ps1
```

Wichtige Werte:

```text
Installed
ConfigurationVerified
PythonwExists
DatabaseExists
```

Für eine funktionierende Installation sollten mindestens gelten:

```text
Installed             : True
ConfigurationVerified : True
PythonwExists         : True
```

## Deinstallation des Autostarts

Autostart entfernen:

```powershell
Set-Location "C:\Pfad\zu\WorkTime"

.\scripts\uninstall.ps1
```

Das Skript:

- entfernt den WorkTime-Autostart aus der Windows-Aufgabenplanung
- löscht die Arbeitszeitdaten **nicht**
- löscht das Repository **nicht**
- beendet eine bereits laufende WorkTime-Instanz **nicht**

Die Datenbank bleibt erhalten:

```text
%LOCALAPPDATA%\WorkTime\data\worktime.db
```

Falls WorkTime gerade läuft, anschließend über das Tray-Menü **Anwendung beenden** wählen.

## Erneute Installation

Nach einer Deinstallation des Autostarts genügt:

```powershell
.\scripts\install.ps1
```

Danach prüfen:

```powershell
.\scripts\status.ps1
```

Die vorhandene Arbeitszeitdatenbank bleibt dabei erhalten.

## Repository verschieben

Wenn der WorkTime-Projektordner verschoben wurde:

1. WorkTime beenden.
2. Repository an den neuen Ort verschieben.
3. virtuelle Umgebung am neuen Ort prüfen bzw. neu erstellen.
4. Installation erneut ausführen:

```powershell
.\scripts\install.ps1
```

5. Status prüfen:

```powershell
.\scripts\status.ps1
```

Der Autostart verwendet absolute Pfade und muss deshalb nach einem Umzug aktualisiert werden.

## Technischer Überblick

WorkTime verwendet:

- Python
- SQLite
- pystray
- Tkinter
- Windows Task Scheduler
- Windows Session Events
- Windows Named Mutex für Single-Instance

Die Anwendung arbeitet lokal und benötigt für die normale Zeiterfassung keine Cloud-Anbindung.

## MVP-Status

Der WorkTime-MVP umfasst:

- lokale Arbeitszeiterfassung
- Pause / Weiter / Feierabend
- Tages- und Wochenübersicht
- manuelle Korrekturen
- Windows-Autostart
- Lock/Unlock-Verhalten
- Single-Instance
- lokale SQLite-Persistenz
- Installations-, Status- und Uninstall-Skripte

Nicht Bestandteil des MVP sind unter anderem:

- Sollzeit / Gleitzeitkonto
- Outlook-Integration
- Teams- oder SharePoint-Anbindung
- Projektzeiterfassung
- GPS- oder Aktivitätsüberwachung
- Multi-User-Betrieb
