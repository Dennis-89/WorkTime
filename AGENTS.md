# Arbeitsregeln für Coding-Agenten

## Projektziel

WorkTime ist eine lokale Windows-11-Anwendung zur persönlichen Arbeitszeiterfassung.

Maßgeblich für Umfang, Reihenfolge und fachliche Anforderungen ist:
`docs/project-management/PROJECT_PLAN.md`

## Architekturgrenzen

- Fachlogik bleibt unabhängig von GUI, SQLite und Windows-spezifischem Code.
- Persistenz greift auf die Fachlogik zu, nicht umgekehrt.
- Windows-Integration und GUI enthalten keine eigene Zeitberechnungslogik.
- Keine Netzwerk- oder Cloud-Abhängigkeiten im MVP.
- Keine Aktivitätsüberwachung von Maus, Tastatur, Programmen oder Fenstern.
- Keine zusätzlichen Funktionen außerhalb des aktiven Arbeitspakets.

## Arbeitsweise

- Bearbeite ausschließlich das ausdrücklich zugewiesene Arbeitspaket.
- Bearbeite ausschließlich die dort erlaubten Pfade.
- Erfinde keine Anforderungen, Standardwerte oder Schnittstellen.
- Ändere keine bereits vorhandenen Nutzeränderungen.
- Lege ohne ausdrückliche Anweisung keinen Branch an.
- Erzeuge keinen Commit.
- Stoppe bei Widersprüchen, fehlenden Voraussetzungen oder notwendiger Umfangserweiterung.
- Verwende für Tests die vorhandene Windows-venv.

## Verbindliche Befehle

Setup:
`.venv\Scripts\python.exe -m pip install -e ".[dev]"`

Tests:
`.venv\Scripts\python.exe -m pytest -q`

Compile:
`.venv\Scripts\python.exe -m compileall -q src tests`

Diff-Prüfung:
`git diff --check`

## Nachweisvertrag

- Erzeuge nach jedem Agentenlauf `.project-control/evidence/<WP-ID>/REPORT.md`.
- Sichere Ausgangszustand, Endzustand, Diff und vollständige Testausgaben.
- Erfolgsaussagen ohne rohe Testausgaben gelten nicht als Nachweis.
- Nachweisdateien dürfen nicht committed oder gestaged werden.
- REPORT.md ist das primäre Prüfartefakt für den Nutzer und muss ohne zusätzliche Terminalausgaben eine vollständige Code- und Nachweisprüfung ermöglichen.
- Separate ZIP-, Patch-, Log- oder Hash-Dateien dürfen zusätzlich erzeugt werden, dürfen aber für die Prüfung notwendige Inhalte im REPORT.md nicht ersetzen.
