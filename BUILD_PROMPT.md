# Build-Prompt für Claude Code — FRITZ!Box Langzeit-Testsystem

> **Anwendung:** Diese Datei nach `C:\Users\Dino\Desktop\Fritzbox_Test_app\BUILD_PROMPT.md` kopieren,
> dort ein Terminal öffnen, `claude` starten und eingeben:
> `Lies BUILD_PROMPT.md und setze Phase 1 um.`

---

## Rolle & Kontext

Du bist ein erfahrener Python-Entwickler und baust mit mir ein Software-Projekt für eine
schweizerische Berufsfachschul-Projektarbeit (Informatiker EFZ, Fachrichtung Plattform-
entwicklung). Bewertet werden Codequalität, Architektur, Nachvollziehbarkeit und Dokumentation
— nicht nur die Funktion. Kommentare, Docstrings, Logmeldungen und CLI-Ausgaben auf **Deutsch**,
Bezeichner (Variablen, Funktionen, Klassen) auf **Englisch**.

**Projektziel:** Ein automatisiertes Testsystem, das die Stabilität einer FRITZ!Box über Stunden
bis Tage unbeaufsichtigt prüft: Es erzeugt LAN- und WLAN-Datenverkehr, überwacht permanent
Erreichbarkeit und Internetverbindung, erkennt Ausfälle sowie ungeplante Router-Neustarts,
protokolliert alles mit Zeitstempel und erstellt am Ende einen Testbericht. Damit sollen
verschiedene Firmware-Versionen objektiv und reproduzierbar verglichen werden.

**Zielumgebung:** Windows 11 Laptop, Projektordner `C:\Users\Dino\Desktop\Fritzbox_Test_app`
(aktuell leer). Der Code muss zusätzlich unter Linux laufen (für den Agent-Modus auf Raspberry Pi).

---

## Technologie-Stack (verbindlich)

| Bereich | Wahl | Begründung |
|---|---|---|
| Sprache | Python 3.12 | Netzwerk-Ökosystem, plattformübergreifend |
| Umgebung | `venv` + `requirements.txt` | einfach nachvollziehbar für Prüfungsexperten |
| Nebenläufigkeit | `asyncio` | viele I/O-Tasks parallel, geringe Last |
| Router-Zugriff | `fritzconnection` (TR-064) | Uptime, WAN-Status, WLAN-Status direkt aus der Box |
| ICMP | `icmplib` (Fallback: `ping`-Subprocess) | präzise Latenz-/Verlustmessung |
| HTTP-Traffic | `httpx` (async) | Download-/Upload-/Web-Simulation |
| Datenhaltung | `sqlite3` (WAL-Modus) | Millionen Messpunkte bei Langzeittests |
| Konfiguration | `PyYAML` + `pydantic` v2 | validiertes, kommentierbares Config-File |
| CLI | `typer` + `rich` | saubere Subcommands, Live-Statusanzeige |
| Dashboard | `FastAPI` + `uvicorn` + Chart.js (CDN-frei, lokal) | optional, Phase 6 |
| Bericht | `Jinja2` + `matplotlib` → HTML, optional PDF via `weasyprint` | druckbarer Abschlussbericht |
| Tests | `pytest` + `pytest-asyncio` | Unit-Tests für Parser und Auswertelogik |
| Qualität | `ruff`, `mypy` (strict für `core/`) | nachweisbare Codequalität |

Keine Cloud-Dienste, keine Docker-Pflicht, keine Admin-Rechte zwingend erforderlich
(ICMP ohne Adminrechte → automatischer Fallback auf Subprocess-Ping).

---

## Projektstruktur

```
Fritzbox_Test_app/
├─ README.md
├─ requirements.txt
├─ config.example.yaml
├─ config.yaml                  (gitignored)
├─ pyproject.toml               (ruff/mypy/pytest-Konfiguration)
├─ .gitignore
├─ src/fbtest/
│  ├─ __main__.py               CLI-Einstiegspunkt (typer)
│  ├─ config.py                 pydantic-Modelle, YAML laden/validieren
│  ├─ core/
│  │  ├─ scheduler.py           Supervisor: startet/überwacht/restartet Tasks
│  │  ├─ events.py              interner Event-Bus (asyncio.Queue)
│  │  └─ models.py              Dataclasses: Measurement, Event, TestRun
│  ├─ storage/
│  │  ├─ database.py            SQLite-Schema, Migration, WAL, Batch-Insert
│  │  └─ exporter.py            CSV-/JSON-Export
│  ├─ modules/
│  │  ├─ ping_monitor.py        Latenz, Paketverlust, Jitter
│  │  ├─ uptime_monitor.py      TR-064-Polling, Reboot-/Reconnect-Erkennung
│  │  ├─ traffic_generator.py   Web-/Streaming-/Download-/Upload-Profile
│  │  ├─ speedtest.py           Bandbreitenmessung WAN + optional iperf3 LAN
│  │  ├─ wlan_monitor.py        Band, RSSI, Linkspeed, Disconnect-Erkennung
│  │  └─ base.py                abstrakte Basisklasse MonitorModule
│  ├─ router/
│  │  └─ fritzbox.py            fritzconnection-Wrapper, defensiv gekapselt
│  ├─ agent/
│  │  ├─ client.py              Agent-Modus: misst und sendet an Master
│  │  └─ server.py              Master: nimmt Agent-Daten entgegen
│  ├─ report/
│  │  ├─ generator.py           Auswertung + Kennzahlen
│  │  ├─ charts.py              matplotlib-Diagramme
│  │  └─ templates/report.html.j2
│  └─ dashboard/
│     ├─ app.py                 FastAPI
│     └─ static/
├─ tests/
├─ data/                        SQLite-Datenbanken pro Testlauf
├─ exports/                     CSV/JSON
├─ reports/                     generierte Berichte
└─ logs/                        rotierende Logfiles
```

---

## Modul-Spezifikationen

### 1. `router/fritzbox.py` — TR-064-Anbindung
Kapselt `fritzconnection`. **Jeder Aufruf muss fehlertolerant sein** (Box nicht erreichbar =
Normalfall während eines Reboots, kein Grund für einen Crash → `None` zurückgeben + Event feuern).

Auszulesen:
- `DeviceInfo:GetInfo` → `NewUpTime` (Router-Laufzeit in s), Firmware-Version (`NewSoftwareVersion`), Modell
- `WANIPConnection:GetStatusInfo` → `NewConnectionStatus`, `NewUptime`, `NewLastConnectionError`
- `WANCommonInterfaceConfig:GetCommonLinkProperties` → Sync-Raten Up/Down
- `WANCommonInterfaceConfig:GetTotalBytesSent/Received` → Byte-Zähler für reale Durchsatzberechnung
- `WLANConfiguration1/2/3:GetInfo` → SSID, Kanal, Status je Band (2,4 / 5 / 6 GHz)
- `WLANConfiguration*:GetTotalAssociations` + `GetGenericAssociatedDeviceInfo` → verbundene Clients, RSSI, Linkspeed
- `Hosts:GetHostNumberOfEntries` → aktive Geräte im Netz

**Kernlogik Reboot-Erkennung:** Sinkt `NewUpTime` gegenüber dem letzten Poll, hat die Box neu
gestartet → Event `ROUTER_REBOOT` mit geschätztem Zeitpunkt. Analog für WAN-Uptime → `WAN_RECONNECT`.
Diese Unterscheidung (Router-Neustart vs. reiner Verbindungsabbruch) ist ein zentrales
Alleinstellungsmerkmal des Systems — sauber implementieren und dokumentieren.

Zugangsdaten aus `config.yaml` oder Umgebungsvariablen (`FRITZ_PASSWORD`), **niemals hartcodiert**.
Falls TR-064 deaktiviert ist: klare deutschsprachige Fehlermeldung mit Hinweis auf
*Heimnetz → Netzwerk → Netzwerkeinstellungen → Zugriff für Anwendungen zulassen*, danach
Degraded-Betrieb ohne Router-Telemetrie (Ping/Traffic laufen weiter).

### 2. `modules/ping_monitor.py`
- Parallele Ziel-Gruppen: **Gateway** (FRITZ!Box, z.B. 192.168.178.1), **externe IPs**
  (1.1.1.1, 8.8.8.8), **DNS-Namen** (getrennte DNS-Auflösungszeit messen!)
- Intervall konfigurierbar (Default 1 s), pro Messung: RTT, Verlust ja/nein
- Aggregation in Fenstern (Default 60 s): min/avg/max/p95 RTT, Jitter, Paketverlust in %
- **Outage-Erkennung:** N aufeinanderfolgende Fehlschläge (Default 3) → Event `OUTAGE_START`;
  erster Erfolg → `OUTAGE_END` mit exakter Dauer
- Wichtige Diagnose-Trennung: Gateway erreichbar + Internet weg = WAN-Problem;
  Gateway weg = Router/LAN/WLAN-Problem. Diese Klassifikation als Feld `outage_scope` speichern.
- `icmplib` bevorzugt; bei `PermissionError` automatisch auf Subprocess-Ping umschalten und
  das im Log vermerken

### 3. `modules/uptime_monitor.py`
- Pollt `router/fritzbox.py` in konfigurierbarem Intervall (Default 10 s)
- Führt Buch über: Router-Uptime, WAN-Uptime, ConnectionStatus, letzter Fehlercode
- Berechnet über den gesamten Testlauf: **tatsächliche Verfügbarkeit in %**, Anzahl Ausfälle,
  längster Ausfall, MTBF (mittlere Zeit zwischen Ausfällen), Anzahl ungeplanter Neustarts
- Unterscheidet geplante (durch das Tool selbst ausgelöste) von ungeplanten Neustarts

### 4. `modules/traffic_generator.py`
Profilgesteuert, mehrere Profile parallel als eigene asyncio-Tasks. Jedes Profil ist ein
eigener „virtueller Client" mit eigener Statistik.

Profile:
- **`web`** — periodisch mehrere HTTP-Requests auf konfigurierbare URLs, misst TTFB und
  Gesamtantwortzeit (simuliert Surfen)
- **`streaming`** — kontinuierlicher Download mit gedrosselter, konstanter Rate; erkennt
  „Stalls" (Rate bricht unter Schwellwert ein) → Event `STREAM_STALL`
- **`download`** — Dauerdownload grosser Testdateien mit maximaler Rate
- **`upload`** — HTTP-POST von generierten Zufallsdaten
- **`lan_iperf`** *(optional)* — `iperf3`-Client gegen einen Server im LAN, falls konfiguriert;
  misst reinen LAN-Durchsatz ohne WAN-Einfluss

Pflicht-Features: **einstellbare Ziel-Datenrate pro Profil** (Token-Bucket-Drosselung, keine
Busy-Loops), **einstellbare Anzahl paralleler Clients pro Profil**, **einstellbare Gesamt-
Testdauer**, sauberes Beenden bei Abbruch. Bei Netzwerkfehler: exponentielles Backoff und
Weiterlaufen, niemals Task-Abbruch.

⚠️ Nur öffentliche, dafür vorgesehene Testendpunkte oder eigene LAN-Server verwenden
(z.B. `speed.hetzner.de`, `proof.ovh.net`, eigener nginx). Als Default-Konfiguration
Testserver eintragen, die für Bandbreitentests gedacht sind.

### 5. `modules/speedtest.py`
- Periodische WAN-Bandbreitenmessung in konfigurierbarem Intervall (Default 30 min)
- Eigene Implementierung: paralleler HTTP-Download bekannter Testdateien über N Verbindungen,
  Messfenster nach Slow-Start-Phase; Upload analog per POST
- Ergebnis: Down/Up in Mbit/s, Latenz unter Last (Bufferbloat-Indikator!) — der Vergleich
  Idle-Latenz vs. Latenz unter Last ist für Firmware-Vergleiche sehr aussagekräftig
- Muss sich mit dem Traffic-Generator abstimmen: während einer Speedtest-Messung
  Traffic-Profile kurz pausieren (über den Event-Bus), sonst sind die Werte wertlos.
  Dieses Verhalten konfigurierbar machen.

### 6. `modules/wlan_monitor.py`
Zweistufig:
- **Router-Sicht (immer verfügbar):** über TR-064 pro Band Status, Kanal, Anzahl assoziierter
  Geräte, RSSI/Linkspeed je Client. Verschwindet ein bekannter Client aus der Liste →
  Event `WLAN_CLIENT_LOST`.
- **Client-Sicht (nur wo das Gerät per WLAN hängt):** aktuelle SSID, Band, Signalstärke,
  Linkspeed. Windows via `netsh wlan show interfaces` parsen, Linux via `iw dev <if> link`.
  Beide Parser in eigene, per pytest testbare Funktionen auslagern (mit Beispiel-Fixtures).
- **Bandwechsel-Test:** Konfiguration erlaubt eine Liste von WLAN-Profilen (SSID pro Band).
  Das Tool verbindet zyklisch auf ein anderes Band (`netsh wlan connect name=<profil>` bzw.
  `nmcli`), misst Verbindungsaufbauzeit und Stabilität, wechselt zurück.
  → In der Doku klar festhalten: Das setzt **getrennte SSIDs pro Band** voraus
  (Band-Steering in der FRITZ!Box deaktivieren), sonst ist keine gezielte Bandwahl möglich.
  Diese Einschränkung offen dokumentieren, nicht kaschieren.

### 7. Agent-Modus (`agent/`) — Lösung für „mehrere WLAN-Clients"
Ein Laptop hat nur einen WLAN-Adapter. Deshalb:
- **Master** (`fbtest run`) startet zusätzlich einen kleinen HTTP-Endpunkt.
- **Agent** (`fbtest agent --master http://<ip>:8080 --name pi-wohnzimmer`) läuft auf
  Raspberry Pi, Zweitlaptop oder Handy-Hotspot-Gerät, führt Ping- und Traffic-Module lokal aus
  und sendet Messwerte im Batch an den Master.
- Master schreibt alle Agent-Daten mit `source`-Feld in dieselbe Datenbank.
- Fällt ein Agent aus, läuft der Master weiter; Agent-Ausfälle werden als eigenes Event geloggt.
- Agent puffert bei Verbindungsverlust zum Master lokal und sendet nach (wichtig: bei
  Router-Ausfall ist auch der Master nicht erreichbar!).

### 8. `storage/database.py`
SQLite mit WAL-Modus, Batch-Inserts (alle ~5 s), Indizes auf `(test_run_id, timestamp)`.

Schema (mindestens):
```sql
test_runs        (id, started_at, ended_at, firmware_version, router_model,
                  config_snapshot_json, notes)
measurements     (id, test_run_id, timestamp, source, module, metric, value, unit, meta_json)
events           (id, test_run_id, timestamp, source, severity, type, message, meta_json)
router_status    (id, test_run_id, timestamp, router_uptime_s, wan_uptime_s,
                  connection_status, last_error, sync_down_kbps, sync_up_kbps,
                  bytes_sent, bytes_received)
wlan_status      (id, test_run_id, timestamp, band, ssid, channel, client_count,
                  rssi_dbm, link_speed_mbps)
speedtests       (id, test_run_id, timestamp, down_mbps, up_mbps,
                  latency_idle_ms, latency_loaded_ms)
outages          (id, test_run_id, started_at, ended_at, duration_s, scope, cause_guess)
```
Alle Zeitstempel als UTC-ISO8601 **und** als Unix-Timestamp speichern.
`firmware_version` beim Start automatisch aus der Box lesen → Firmware-Vergleich wird trivial.

### 9. `report/generator.py`
Erzeugt aus einem Testlauf einen HTML-Bericht (optional PDF) mit:
- Kopf: Testlauf-ID, Zeitraum, Dauer, Router-Modell, **Firmware-Version**, Konfigurations-Zusammenfassung
- Management-Summary: Verfügbarkeit %, Anzahl Ausfälle, längster Ausfall, Anzahl Neustarts,
  Ø/p95-Latenz, Ø Paketverlust, Ø Down/Up, übertragenes Datenvolumen
- Diagramme: Latenz über Zeit (mit markierten Ausfällen), Paketverlust, Bandbreite über Zeit,
  Router-Uptime-Verlauf, WLAN-Signal über Zeit
- Ereignistabelle chronologisch (alle Events mit Zeitstempel und Schweregrad)
- **Vergleichsmodus:** `fbtest report --compare <run_id_a> <run_id_b>` stellt zwei Testläufe
  (= zwei Firmware-Versionen) in einer Tabelle und in Overlay-Diagrammen gegenüber.
  Das ist die Kernaussage des Projekts — besonders sorgfältig umsetzen.

### 10. Dashboard (`dashboard/app.py`, Phase 6, optional)
FastAPI auf `127.0.0.1:8080`: Live-Status aller Module, aktuelle Latenz/Bandbreite als
Chart.js-Graph (Polling alle 2 s auf einen JSON-Endpunkt), Ereignis-Ticker, Restlaufzeit,
Buttons für Start/Stop/Bericht. Bewusst schlicht, dunkles Theme, keine Build-Tools —
statisches HTML/CSS/JS, damit es ohne npm läuft.

---

## Nichtfunktionale Anforderungen

- **Wiederanlauf:** `core/scheduler.py` überwacht jeden Modul-Task. Absturz → Log-Eintrag +
  Event + automatischer Neustart mit exponentiellem Backoff (1 s → max. 60 s). Das
  Gesamtsystem darf nie wegen eines Moduls stoppen.
- **Crash-Recovery:** Beim Start prüfen, ob ein Testlauf offen ist (`ended_at IS NULL`)
  → auf Nachfrage fortsetzen statt neu beginnen. Lücke als Event `SYSTEM_GAP` protokollieren.
- **Langzeitfestigkeit:** Kein unbegrenztes Wachstum im RAM (keine Messwertlisten im Speicher
  halten, alles in die DB). Rotierende Logfiles (`RotatingFileHandler`, 10 MB × 5).
  Ziel: 7 Tage Dauerlauf ohne Eingriff, RAM-Verbrauch stabil.
- **Graceful Shutdown:** Strg+C → laufende Tasks sauber beenden, Puffer schreiben,
  `test_runs.ended_at` setzen, optional direkt Bericht generieren.
- **Zeitumstellung/Systemschlaf:** monotone Uhr (`time.monotonic()`) für Dauern verwenden,
  Wall-Clock nur für Zeitstempel. Erkennt das Tool eine grosse Zeitlücke (Laptop im Standby),
  wird das als Event markiert und **nicht** als Router-Ausfall gewertet.
  → Im README den Hinweis aufnehmen, Energiesparoptionen des Laptops für Langzeittests zu deaktivieren.

---

## CLI

```
fbtest init                      Beispielkonfiguration + Ordner anlegen
fbtest check                     Vorabprüfung: Box erreichbar? TR-064 aktiv? Zugangsdaten ok?
                                 Testserver erreichbar? WLAN-Profile vorhanden? → Klartext-Report
fbtest run [--duration 24h] [--config config.yaml] [--name "FW 8.02 Test"]
fbtest agent --master <url> --name <clientname>
fbtest list                      alle Testläufe mit Kennzahlen
fbtest export <run_id> --format csv|json
fbtest report <run_id> [--pdf]
fbtest report --compare <a> <b>
fbtest dashboard
```
`fbtest check` ist wichtig: vor einem 3-Tage-Lauf muss in 10 Sekunden klar sein, ob alles passt.

---

## Konfiguration (`config.example.yaml`)

Vollständig kommentiert, jede Option mit deutscher Erklärung. Mindestens:
Router (Host, Benutzer, Passwort/ENV-Variable, Poll-Intervall), Ping-Ziele und -Intervalle,
Outage-Schwellwerte, Traffic-Profile (Typ, Anzahl Clients, Zielrate, URLs), Speedtest
(Intervall, Server, Traffic-Pause ja/nein), WLAN (Interface, Profile je Band, Wechsel-Intervall),
Testdauer, Speicherorte, Loglevel, Dashboard-Port.

---

## Arbeitsweise — Phasen

Arbeite **eine Phase nach der anderen** ab. Nach jeder Phase: kurz zusammenfassen, was
funktioniert, wie ich es teste, und auf mein „OK" warten. Nicht alles auf einmal generieren.

1. **Grundgerüst** — Ordnerstruktur, venv, requirements, Config-Modelle, Logging, CLI-Skelett,
   SQLite-Schema, `fbtest init` + `fbtest check`
2. **Router-Anbindung** — `fritzbox.py`, `uptime_monitor.py`, Reboot-/Reconnect-Erkennung
3. **Monitoring** — `ping_monitor.py` inkl. Outage-Klassifikation, Speicherung, Scheduler
   mit Auto-Restart
4. **Traffic & Speedtest** — Traffic-Profile mit Ratenbegrenzung, Speedtest-Modul, Koordination
5. **WLAN & Agent** — WLAN-Monitoring beide Sichten, Bandwechsel, Agent-/Master-Modus
6. **Bericht & Export** — CSV/JSON-Export, HTML-Bericht mit Diagrammen, Vergleichsmodus
7. **Dashboard & Feinschliff** — FastAPI-Dashboard, README, Tests, ruff/mypy sauber

---

## Qualitätsvorgaben

- Type Hints überall, `mypy --strict` für `src/fbtest/core/` und `storage/` fehlerfrei
- `ruff check` fehlerfrei
- Docstrings (Google-Style, deutsch) für alle öffentlichen Funktionen und Klassen
- pytest-Tests mindestens für: `netsh`/`iw`-Parser, Outage-Erkennungslogik, Reboot-Erkennung
  (Uptime-Sprünge), Aggregationsfunktionen, Config-Validierung — jeweils mit Fixtures,
  **ohne echtes Netzwerk**
- Keine Geheimnisse im Code oder in Git. `.gitignore` für `config.yaml`, `data/`, `logs/`, `exports/`
- README.md: Zweck, Architekturübersicht (inkl. ASCII-Diagramm des Datenflusses), Installation,
  FRITZ!Box-Vorbereitung (TR-064 aktivieren, Benutzer anlegen, getrennte SSIDs), Bedienung,
  Grenzen des Systems

## Nicht tun

- Keine Passwörter, IPs oder SSIDs hartcodieren
- Keine externen Dienste ausser den konfigurierten Testservern
- Keine Funktion nur „vortäuschen" — was nicht sinnvoll umsetzbar ist, wird als bekannte
  Einschränkung im README dokumentiert statt mit Dummy-Werten kaschiert
- Keine `time.sleep()`-Blockaden in asyncio-Code
- Keine Änderungen an der FRITZ!Box-Konfiguration ohne explizite Konfigurationsfreigabe
  (das Tool ist ein **Mess**-, kein Steuerwerkzeug)

---

## Akzeptanzkriterien

Das Projekt gilt als fertig, wenn ich Folgendes nachweisen kann:

1. `fbtest check` meldet grün gegen meine FRITZ!Box
2. `fbtest run --duration 24h` läuft 24 h unbeaufsichtigt durch, ohne Absturz und mit
   stabilem Speicherverbrauch
3. Ein manuell ausgelöster Router-Neustart erscheint korrekt als Event `ROUTER_REBOOT`
   inkl. Ausfalldauer
4. Ein gezogenes WAN-Kabel erscheint als `OUTAGE` mit `scope=wan`, ein ausgeschaltetes
   WLAN als `scope=wlan`
5. `fbtest export` liefert vollständige CSV- und JSON-Dateien
6. `fbtest report` erzeugt einen druckbaren Bericht mit Kennzahlen und Diagrammen
7. `fbtest report --compare` stellt zwei Firmware-Läufe verständlich gegenüber
8. Mindestens zwei Geräte (Laptop + Agent) erzeugen gleichzeitig Last, beide erscheinen
   getrennt in der Auswertung

**Starte jetzt mit Phase 1.** Zeig mir vorher in Stichworten, wie du Phase 1 aufbauen willst.
