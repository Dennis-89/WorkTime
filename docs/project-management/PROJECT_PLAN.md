# WorkTime - Projektplan

## Dokumentstatus
- Status: Plan freigegeben
- Version/Datum: 2026-10-01
- Basis-Commit: cb7c544
- Arbeitsbranch: feature/worktime-mvp
- Modus: Agent / Codex
- Letzte Freigabe durch Nutzer: Scope und Projektschritte freigegeben am 2026-10-01

## Ziel und Nutzen

WorkTime ist eine lokale Windows-11-Anwendung zur möglichst aufwandsarmen persönlichen Arbeitszeiterfassung.

Das Werkzeug soll Arbeitsbeginn automatisch erkennen, tatsächliche Pausen manuell erfassen und verhindern, dass PC-Sperrzeiten fälschlich als Pausen interpretiert werden.

Die erfassten Rohdaten bleiben lokal erhalten und bilden später die Grundlage für Erweiterungen wie Outlook-Integration.

## Funktionsumfang

### Funktionale Anforderungen

- F-001: Der erste relevante Windows-Login bzw. das erste Entsperren eines noch nicht begonnenen Kalendertags kann automatisch den Arbeitsbeginn erzeugen.
- F-002: Das Sperren des PCs beendet oder pausiert die Arbeitszeit nicht.
- F-003: Der Nutzer kann eine laufende Arbeitszeit manuell pausieren.
- F-004: Der Nutzer kann eine Pause manuell beenden und die Arbeitszeit fortsetzen.
- F-005: Der Nutzer kann den Arbeitstag manuell mit "Feierabend" beenden.
- F-006: Ein bereits als FINISHED abgeschlossener Kalendertag darf durch späteres Sperren, Entsperren oder erneute PC-Nutzung nicht automatisch erneut gestartet werden.
- F-007: Erfasste Start-, Pausen- und Endzeitpunkte können nachträglich korrigiert werden.
- F-008: Die Anwendung zeigt eine Tagesübersicht mit Arbeitsbeginn, Arbeitsende, Pausendauer, Bruttozeit und Nettoarbeitszeit.
- F-009: Die Anwendung zeigt eine Wochenübersicht mit Nettoarbeitszeiten je Tag und Wochensumme.
- F-010: Die Rohereignisse werden dauerhaft lokal in SQLite gespeichert.
- F-011: Für jeden Kalendertag existiert fachlich genau einer der Zustände NOT_STARTED, WORKING, PAUSED oder FINISHED.

### Nichtfunktionale Anforderungen

- NF-001: Zielplattform ist Windows 11.
- NF-002: Implementierung in Python 3.11 oder neuer.
- NF-003: Zeitberechnung und Zustandslogik müssen unabhängig von GUI und Windows-Integration testbar sein.
- NF-004: Die Anwendung benötigt für Version 1 keine Netzwerk- oder Cloud-Verbindung.
- NF-005: Ein Neustart der Anwendung darf bereits gespeicherte Arbeitszeitdaten nicht verlieren.
- NF-006: Ungültige Zustandsübergänge dürfen nicht stillschweigend akzeptiert werden.
- NF-007: Zeitberechnungen müssen deterministisch testbar sein; laufende Tage erhalten einen expliziten Auswertungszeitpunkt.
- NF-008: Zeitstempel werden timezone-aware verarbeitet und später mit Offset persistiert.
- NF-009: Für die Kernlogik werden keine unnötigen externen Abhängigkeiten eingeführt.

### Schnittstellen und Daten

- I-001: SQLite wird als lokale persistente Datenbank verwendet.
- I-002: Fachliche Rohereignisse sind mindestens WORK_START, PAUSE_START, PAUSE_END und WORK_END.
- I-003: Windows-Login/Unlock wird erst in PS-03 mit der Fachlogik verbunden.
- I-004: GUI und Tray greifen über eine Anwendungsschicht auf Fachlogik und Persistenz zu.
- I-005: Datenbankdateien liegen unter data/ und werden nicht in Git versioniert.

### Nicht im Umfang von Version 1

- OOS-001: Outlook-Integration.
- OOS-002: Teams-Integration.
- OOS-003: SharePoint- oder Cloud-Synchronisation.
- OOS-004: Projektzeiterfassung.
- OOS-005: Maus-, Tastatur- oder Programmaktivitätsüberwachung.
- OOS-006: GPS oder Standorterfassung.
- OOS-007: Mehrbenutzer- oder Mitarbeiterverwaltung.
- OOS-008: Sollzeit-, Gleitzeit- oder Überstundensaldo.
- OOS-009: Automatische Interpretation einer PC-Sperrzeit als Pause.

### Fachliche Zustände und Übergänge

Erlaubte Übergänge:

- NOT_STARTED + WORK_START -> WORKING
- WORKING + PAUSE_START -> PAUSED
- PAUSED + PAUSE_END -> WORKING
- WORKING + WORK_END -> FINISHED
- PAUSED + WORK_END -> FINISHED

Andere Übergänge sind fachlich ungültig.

Ein FINISHED-Tag bleibt für den betreffenden Kalendertag beendet.

### Projektweite Abnahme

- [ ] Arbeitsbeginn kann automatisch ausgelöst werden.
- [ ] PC-Sperren verändert die Arbeitszeit nicht.
- [ ] Pause, Weiter und Feierabend funktionieren manuell.
- [ ] Spätere PC-Nutzung startet einen beendeten Tag nicht neu.
- [ ] Zeiten können nachträglich korrigiert werden.
- [ ] Tagesberechnung ist korrekt.
- [ ] Wochenberechnung ist korrekt.
- [ ] Daten überstehen einen Programmneustart.
- [ ] Kernlogik und Persistenz sind automatisiert getestet.
- [ ] Anwendung kann unter Windows 11 ohne Entwicklungs-IDE gestartet werden.

## Projektschritte

### PS-01: Datenmodell und Zeitberechnung

- Ziel: Fachliche Grundlage unabhängig von Oberfläche und Windows schaffen.
- Ergebnis: Getestetes Zustandsmodell, Zeitberechnung und SQLite-Persistenz.
- Abhängigkeiten: keine.
- Eintrittskriterium: Projektplan freigegeben.
- Abschlusskriterium: WP-001 und WP-002 abgeschlossen.

### PS-02: Tray-Anwendung

- Ziel: Pause, Weiter und Feierabend mit minimalem Bedienaufwand ermöglichen.
- Ergebnis: Laufende Tray-Anwendung mit den fachlich zulässigen Aktionen.
- Abhängigkeiten: PS-01.
- Abschlusskriterium: WP-003 abgeschlossen.

### PS-03: Windows-Integration

- Ziel: Arbeitsbeginn über Windows-Anmeldung bzw. Unlock automatisch auslösen.
- Ergebnis: Windows-Ereignis ruft die Anwendung auf, ohne Sperrzeiten als Pause zu behandeln.
- Abhängigkeiten: PS-01 und PS-02.
- Abschlusskriterium: WP-004 abgeschlossen.

### PS-04: Übersicht und Korrektur

- Ziel: Erfasste Zeiten kontrollieren und korrigieren können.
- Ergebnis: Tagesansicht, Wochenansicht und Korrekturfunktion.
- Abhängigkeiten: PS-01 bis PS-03.
- Abschlusskriterium: WP-005 abgeschlossen.

### PS-05: Installation und Praxistest

- Ziel: Alltagstauglicher Betrieb unter Windows 11.
- Ergebnis: Startbare Anwendung, Autostart/Task-Konfiguration und dokumentierter Praxistest.
- Abhängigkeiten: PS-01 bis PS-04.
- Abschlusskriterium: WP-006 abgeschlossen.

## Arbeitspakete

### WP-001: Fachliches Zustandsmodell und Zeitberechnung implementieren

- Status: abgeschlossen
- Projektschritt: PS-01
- Ziel: Vollständig getestete, persistenz- und GUI-unabhängige Fachlogik für einen Arbeitstag.
- Voraussetzungen: Projektplan und Agentenregeln vorhanden.
- Abhängigkeiten: keine.

- Erlaubte Dateien/Pfade:
  - pyproject.toml
  - src/worktime/__init__.py
  - src/worktime/domain.py
  - tests/test_domain.py
  - optional tests/__init__.py
  - .project-control/evidence/WP-001/**

- Verbotene Dateien/Pfade:
  - AGENTS.md
  - docs/**
  - README.md
  - .gitignore
  - data/**
  - alle GUI-, SQLite- und Windows-Integrationsmodule

- Aufgaben:
  1. Python-Paketstruktur für WorkTime anlegen.
  2. Zustände NOT_STARTED, WORKING, PAUSED und FINISHED modellieren.
  3. Ereignisse WORK_START, PAUSE_START, PAUSE_END und WORK_END modellieren.
  4. Erlaubte Zustandsübergänge implementieren.
  5. Ungültige Übergänge mit einer projektspezifischen Domain-Exception ablehnen.
  6. Rückwärts laufende Ereigniszeitpunkte ablehnen.
  7. Bruttozeit, Pausenzeit und Nettoarbeitszeit berechnen.
  8. Laufende Tage über einen explizit übergebenen Auswertungszeitpunkt berechnen.
  9. Verhalten für Feierabend während einer laufenden Pause unterstützen.
  10. Fachlogik umfassend mit pytest testen.

- Erwartetes Verhalten:
  - Domain-Code hat keine Abhängigkeit von SQLite, GUI oder Windows.
  - Zeitstempel sind timezone-aware.
  - Berechnungen verwenden keine versteckte aktuelle Systemzeit.
  - WORK_END während PAUSED beendet den Tag und zählt die noch offene Pause bis WORK_END als Pause.
  - FINISHED akzeptiert keine weiteren fachlichen Ereignisse.

- Nicht enthalten:
  - SQLite
  - Tray
  - Windows Login/Unlock
  - Tages-/Wochen-GUI
  - Korrektur-Oberfläche

- Prüfkommandos:
  - .venv\Scripts\python.exe -m pip install -e ".[dev]"
  - .venv\Scripts\python.exe -m pytest tests/test_domain.py -q
  - .venv\Scripts\python.exe -m compileall -q src tests
  - git diff --check

- Definition of Done:
  - [ ] Alle vier Zustände existieren.
  - [ ] Alle vier Ereignistypen existieren.
  - [ ] Jeder erlaubte Zustandsübergang ist getestet.
  - [ ] Ungültige Übergänge sind getestet.
  - [ ] Nicht-monotone Ereigniszeiten werden abgelehnt und getestet.
  - [ ] Arbeits-, Brutto- und Pausenzeiten werden getestet.
  - [ ] Laufende Arbeits- und Pausenzeiten werden mit explizitem Auswertungszeitpunkt getestet.
  - [ ] WORK_END aus PAUSED ist getestet.
  - [ ] FINISHED kann nicht wieder gestartet werden.
  - [ ] pytest ist vollständig erfolgreich.
  - [ ] compileall ist erfolgreich.
  - [ ] git diff --check meldet keine Fehler.

- Nachweise:
  - Git-Baseline.
  - vollständiger Diff.
  - Testausgabe.
  - REPORT.md.

- Stop-Bedingungen:
  - Änderung außerhalb der erlaubten Pfade erforderlich.
  - fachliche Zustandsregeln widersprechen sich.
  - eine zusätzliche Laufzeitabhängigkeit für die Domain wäre erforderlich.
  - erwarteter Branch oder Basis-Commit stimmen nicht.

- Geplanter Commit:
  - feat(domain): add workday state and time calculation

### WP-002: SQLite-Persistenz implementieren

- Status: Prüfung
- Projektschritt: PS-01
- Ziel: Fachereignisse dauerhaft lokal speichern und wiederherstellen.
- Abhängigkeiten: WP-001.
- Geplanter Commit: feat(storage): add sqlite event persistence

### WP-003: Tray-Steuerung implementieren

- Status: geplant
- Projektschritt: PS-02
- Ziel: Pause, Weiter und Feierabend über ein Windows-Tray-Symbol bedienen.
- Abhängigkeiten: WP-002.
- Geplanter Commit: feat(tray): add worktime tray controls

### WP-004: Windows-Start und Unlock integrieren

- Status: geplant
- Projektschritt: PS-03
- Ziel: Noch nicht gestartete Arbeitstage durch Windows-Login/Unlock automatisch starten.
- Abhängigkeiten: WP-003.
- Geplanter Commit: feat(windows): add automatic work start integration

### WP-005: Tages-, Wochen- und Korrekturansicht implementieren

- Status: geplant
- Projektschritt: PS-04
- Ziel: Zeiten prüfen, zusammenfassen und manuell korrigieren.
- Abhängigkeiten: WP-004.
- Geplanter Commit: feat(ui): add summaries and time correction

### WP-006: Windows-Bereitstellung und Praxistest

- Status: geplant
- Projektschritt: PS-05
- Ziel: Anwendung ohne IDE starten und im realen Arbeitsalltag testen.
- Abhängigkeiten: WP-005.
- Geplanter Commit: build: add Windows deployment

## Entscheidungsprotokoll

- 2026-10-01 D-001: Python als Implementierungssprache gewählt.
- 2026-10-01 D-002: SQLite als lokale Persistenz gewählt.
- 2026-10-01 D-003: PC-Sperren gilt ausdrücklich nicht als Pause.
- 2026-10-01 D-004: Outlook und Cloud-Anbindung werden aus MVP ausgeschlossen.
- 2026-10-01 D-005: Codex wird als Coding-Agent eingesetzt.
- 2026-10-01 D-006: Fachlogik wird vor GUI und Windows-Integration isoliert entwickelt.

## Fortschritt

- Aktuelles Arbeitspaket: WP-002
- Letzter geprüfter Stand: Basis-Commit cb7c544
- Blocker: keine
- Nächster Schritt: WP-001 durch Codex implementieren und Nachweise prüfen.
