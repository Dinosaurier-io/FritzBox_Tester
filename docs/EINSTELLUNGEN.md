<!-- Erzeugt aus den Modellen in src/fbtest/config.py.
     Nicht von Hand aendern: '.\.venv\Scripts\python.exe -m fbtest.config_doc'
     schreibt diese Datei neu, und test_config_doc.py prueft sie gegen die Modelle. -->

# Einstellungen

Vollstaendige Referenz aller Felder der `config.yaml`. Sie entsteht aus denselben
Modellen, aus denen auch das Einstellungsformular und die Pruefung beim Speichern
gebaut werden - sie kann also nicht veralten.

Drei Wege fuehren zu denselben Werten:

| Weg | Wofuer |
|---|---|
| **Dashboard → Einstellungen** | Formular mit Suche, Einheiten und Standardwerten; empfohlen |
| **`config.yaml` im Texteditor** | volle Kontrolle; `fbtest init` legt eine kommentierte an |
| **Rohansicht im Dashboard** | dieselbe Datei im Browser, mit Pruefung vor dem Speichern |

Zum Lesen der Tabellen:

- **Pflicht** in der Spalte *Standard* heisst: ohne diesen Wert startet das Programm nicht.
- Felder auf `_s` sind Sekunden. Im Formular sind sie mit Einheit dargestellt, in der Datei
  bleiben es Zahlen; `run.duration_s` nimmt zusaetzlich Angaben wie `24h` entgegen.
- `enabled: nein` schaltet einen ganzen Abschnitt ab, ohne seine Einstellungen zu verlieren.
- Ein ungueltiger Wert wird beim Speichern **abgelehnt**, die bisherige Datei bleibt stehen.
  Vor jedem Schreiben entsteht zusaetzlich eine `config.yaml.bak`.

## Inhalt

- [`router` – FRITZ!Box-Zugang](#router)
- [`ping` – Erreichbarkeit und Ausfallerkennung](#ping)
- [`traffic` – Kuenstlich erzeugte Last](#traffic)
- [`speedtest` – Bandbreitenmessung](#speedtest)
- [`wlan` – WLAN-Ueberwachung](#wlan)
- [`run` – Testlauf](#run)
- [`storage` – Speicherorte](#storage)
- [`logging` – Protokollierung](#logging)
- [`dashboard` – Dashboard](#dashboard)
- [`desktop` – Desktop-Anwendung](#desktop)


<a id="router"></a>

## `router` – FRITZ!Box-Zugang

Ohne diesen Zugang misst das Programm weiterhin Erreichbarkeit und Bandbreite, aber es kann keinen Router-Neustart erkennen und keine Betriebsdauer auslesen - und damit den wichtigsten Befund eines Langzeittests nicht belegen.

Das Passwort gehoert **nicht** in diese Datei. Die Rangfolge ist `FRITZ_PASSWORD` (Umgebungsvariable) vor dem Schluesselspeicher des Systems vor dem Klartexteintrag; die Umgebungsvariable gewinnt immer.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `host` | Text | `192.168.178.1` | IP oder Hostname der FRITZ!Box |
| `port` | Ganzzahl | keiner | TR-064-Port, None = Standard |
| `username` | Text | leer | Benutzername (leer = Standardbenutzer) |
| `password` | Text | leer | Passwort im Klartext - besser: password_env |
| `password_env` | Text | `FRITZ_PASSWORD` | Name der Umgebungsvariablen, aus der das Passwort gelesen wird |
| `use_tls` | ja/nein | nein | TR-064 ueber HTTPS (Port 49443) |
| `timeout_s` | Zahl > 0 | `5` s | Timeout je TR-064-Aufruf |
| `poll_interval_s` | Zahl > 0 | `10` s | Abstand zwischen zwei Router-Abfragen |

<a id="ping"></a>

## `ping` – Erreichbarkeit und Ausfallerkennung

Die Grundlage der Ausfallerkennung. Entscheidend ist die Mischung der Ziele: Antwortet das Gateway, das Internet aber nicht, liegt das Problem hinter dem Router; antwortet auch das Gateway nicht, davor. Ohne ein Ziel je `scope` laesst sich diese Unterscheidung nicht treffen.

Gespeichert wird nicht jeder einzelne Ping, sondern je `aggregate_window_s` ein verdichteter Wert. Das haelt die Datenbank auch nach Tagen handlich - der Zeitpunkt eines Ausfalls bleibt trotzdem sekundengenau, weil Ausfaelle als eigene Ereignisse gefuehrt werden.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | ja | Ping-Ueberwachung aktiv; ohne sie gibt es keine Ausfallerkennung |
| `interval_s` | Zahl > 0 | `1` s | Abstand zwischen zwei Pings je Ziel |
| `timeout_s` | Zahl > 0 | `1` s | Timeout je Ping |
| `aggregate_window_s` | Zahl > 0 | `60` s (1 min) | Fenstergroesse fuer min/avg/max/p95/Jitter |
| `outage_threshold` | Ganzzahl ab 1 | `3` | Anzahl aufeinanderfolgender Fehlschlaege bis OUTAGE_START |
| `prefer_icmplib` | ja/nein | ja | icmplib bevorzugen; ohne Rechte automatisch Fallback auf System-Ping |
| `store_raw_samples` | ja/nein | nein | Jeden einzelnen Ping speichern (sehr viele Zeilen) statt nur die Fenster |
| `targets` | Liste von Eintraegen | leer | Ueberwachte Ziele; mindestens eines, sinnvoll sind Gateway und Internet |
| `dns` | Unterabschnitt | leer | Getrennte Messung der Namensaufloesung |

### `ping.targets` – Eintrag

Mindestens ein Ziel ist Pflicht. Sinnvoll sind drei: der Router selbst und zwei voneinander unabhaengige Ziele im Internet - faellt nur eines davon aus, liegt es an diesem Ziel und nicht an der Leitung.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Anzeigename, z.B. 'fritzbox' oder 'cloudflare' |
| `host` | Text | **Pflicht** | IP-Adresse oder Hostname |
| `scope` | `gateway` / `internet` / `lan` | `internet` | gateway = Router selbst, internet = ausserhalb, lan = anderes Geraet im LAN |


### `ping.dns` – Felder

Eine langsame Namensaufloesung fuehlt sich an wie «das Internet ist langsam», hat mit der Leitung aber nichts zu tun. Deshalb wird sie getrennt gemessen.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | ja | DNS-Aufloesung getrennt vom Ping messen |
| `hostnames` | Liste von Text | `www.google.com`, `www.sbb.ch` | Namen, die aufgeloest werden; je Durchgang alle nacheinander |
| `interval_s` | Zahl > 0 | `30` s | Abstand zwischen zwei Durchgaengen |
| `timeout_s` | Zahl > 0 | `5` s | Timeout je Namensaufloesung |


<a id="traffic"></a>

## `traffic` – Kuenstlich erzeugte Last

Ein Stabilitaetstest ohne Last ist wenig wert: Viele Firmware-Probleme zeigen sich erst unter Dauerbetrieb mit mehreren gleichzeitigen Verbindungen. Jedes Profil laeuft als eigener virtueller Client mit eigener Statistik und wird nach einem Netzwerkfehler mit wachsender Wartezeit neu gestartet, nie abgebrochen.

**Zum Datenvolumen:** Eine Dauerlast von 1 Mbit/s ergibt rund 10,8 GB pro Tag. Die heruntergeladenen Daten werden nirgends gespeichert - sie laufen in 64-KB-Haeppchen durch den Arbeitsspeicher und werden verworfen. Beim Provider faellt das Volumen trotzdem an.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | ja | Kuenstliche Last erzeugen; false = nur beobachten |
| `profiles` | Liste von Eintraegen | leer | Virtuelle Clients; jedes Profil laeuft unabhaengig mit eigener Statistik |
| `backoff_start_s` | Zahl > 0 | `1` s | Startwert des Fehler-Backoffs |
| `backoff_max_s` | Zahl > 0 | `60` s (1 min) | Obergrenze des Fehler-Backoffs |

### `traffic.profiles` – Eintrag

Welche Felder ein Profil hat, entscheidet sein `type`. Die folgenden Tabellen zeigen je Typ die zusaetzlichen Felder; die gemeinsamen stehen darueber.


**`type: web`** – Simuliert Surfen: periodische HTTP-Requests, misst TTFB und Gesamtzeit.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Freier Name, erscheint so in der Auswertung |
| `enabled` | ja/nein | ja | Profil laeuft mit; false = bleibt untaetig |
| `clients` | Ganzzahl ab 1 bis 64 | `1` | Anzahl paralleler virtueller Clients |
| `target_rate_mbps` | Zahl ab 0 | `0` | Ziel-Datenrate je Client in Mbit/s; 0 = unbegrenzt (Token-Bucket) |
| `type` | `web` | `web` | Profiltyp - bestimmt die uebrigen Felder |
| `urls` | Liste von Text | leer | Aufgerufene Seiten; geladen wird nur das HTML, keine Bilder oder Skripte |
| `interval_s` | Zahl > 0 | `15` s | Pause zwischen zwei Surf-Runden |


**`type: streaming`** – Simuliert einen Videostream: konstante Rate, erkennt Einbrueche (Stalls).

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Freier Name, erscheint so in der Auswertung |
| `enabled` | ja/nein | ja | Profil laeuft mit; false = bleibt untaetig |
| `clients` | Ganzzahl ab 1 bis 64 | `1` | Anzahl paralleler virtueller Clients |
| `target_rate_mbps` | Zahl > 0 | `5` | Konstante Rate je Client - die Bitrate des 'Videos' |
| `type` | `streaming` | `streaming` | Profiltyp - bestimmt die uebrigen Felder |
| `url` | Text | **Pflicht** | Testdatei, die fortlaufend gelesen wird |
| `stall_threshold_pct` | Zahl > 0 bis 100 | `60` | Faellt die erreichte Rate unter diesen Anteil der Zielrate: STREAM_STALL |
| `stall_window_s` | Zahl > 0 | `5` s | Messfenster fuer die Stall-Pruefung |


**`type: download`** – Dauerdownload einer grossen Testdatei.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Freier Name, erscheint so in der Auswertung |
| `enabled` | ja/nein | ja | Profil laeuft mit; false = bleibt untaetig |
| `clients` | Ganzzahl ab 1 bis 64 | `1` | Anzahl paralleler virtueller Clients |
| `target_rate_mbps` | Zahl ab 0 | `0` | Ziel-Datenrate je Client in Mbit/s; 0 = unbegrenzt (Token-Bucket) |
| `type` | `download` | `download` | Profiltyp - bestimmt die uebrigen Felder |
| `url` | Text | **Pflicht** | Testdatei; wird endlos wiederholt geladen und nirgends gespeichert |


**`type: upload`** – HTTP-POST von generierten Zufallsdaten.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Freier Name, erscheint so in der Auswertung |
| `enabled` | ja/nein | ja | Profil laeuft mit; false = bleibt untaetig |
| `clients` | Ganzzahl ab 1 bis 64 | `1` | Anzahl paralleler virtueller Clients |
| `target_rate_mbps` | Zahl ab 0 | `0` | Ziel-Datenrate je Client in Mbit/s; 0 = unbegrenzt (Token-Bucket) |
| `type` | `upload` | `upload` | Profiltyp - bestimmt die uebrigen Felder |
| `url` | Text | **Pflicht** | Endpunkt, der HTTP-POST annimmt - moeglichst im eigenen LAN |
| `chunk_size_kb` | Ganzzahl ab 1 bis 8192 | `256` | Groesse des wiederholt gesendeten Zufallsblocks |


**`type: lan_iperf`** – Reiner LAN-Durchsatz gegen einen iperf3-Server (ohne WAN-Einfluss).

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `name` | Text | **Pflicht** | Freier Name, erscheint so in der Auswertung |
| `enabled` | ja/nein | ja | Profil laeuft mit; false = bleibt untaetig |
| `clients` | Ganzzahl ab 1 bis 64 | `1` | Anzahl paralleler virtueller Clients |
| `target_rate_mbps` | Zahl ab 0 | `0` | Ziel-Datenrate je Client in Mbit/s; 0 = unbegrenzt (Token-Bucket) |
| `type` | `lan_iperf` | `lan_iperf` | Profiltyp - bestimmt die uebrigen Felder |
| `server` | Text | **Pflicht** | IP des iperf3-Servers im LAN |
| `port` | Ganzzahl ab 1 bis 65535 | `5201` | Port des iperf3-Servers |
| `duration_s` | Zahl > 0 | `10` s | Dauer einer einzelnen Messung |
| `interval_s` | Zahl > 0 | `300` s (5 min) | Abstand zwischen zwei Messungen |
| `reverse` | ja/nein | nein | True = Download-Richtung messen |


<a id="speedtest"></a>

## `speedtest` – Bandbreitenmessung

Misst in Abstaenden die tatsaechliche Bandbreite und zugleich die Latenz *unter Last* - die Differenz zur Ruhelatenz ist der Bufferbloat-Indikator.

Diese Messung drosselt bewusst nicht und laeuft mit voller Leitungsgeschwindigkeit. Bei 500 Mbit/s und dem Standardabstand von 30 Minuten sind das rund 39 GB pro Tag. Waehrend der Messung pausieren die Traffic-Profile, damit die eigene Hintergrundlast das Ergebnis nicht verfaelscht.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | ja | Periodische Bandbreitenmessung durchfuehren |
| `interval_s` | Zahl > 0 | `1800` s (30 min) | Abstand zwischen zwei Messungen |
| `download_url` | Text | `https://speed.hetzner.de/100MB.bin` | Testdatei; ueber den ganzen Lauf dieselbe, sonst sind Werte unvergleichbar |
| `upload_url` | Text | leer | Leer = Upload-Messung ueberspringen |
| `connections` | Ganzzahl ab 1 bis 16 | `4` | Parallele Verbindungen |
| `measure_duration_s` | Zahl > 0 | `10` s | Netto-Messfenster |
| `warmup_s` | Zahl ab 0 | `3` s | Slow-Start-Phase, wird nicht gewertet |
| `upload_size_mb` | Ganzzahl ab 1 bis 1024 | `20` | Datenmenge je Upload-Messung |
| `latency_host` | Text | `1.1.1.1` | Ziel fuer Latenz unter Last |
| `pause_traffic` | ja/nein | ja | Traffic-Profile waehrend der Messung pausieren |

<a id="wlan"></a>

## `wlan` – WLAN-Ueberwachung

Zwei Blickwinkel auf dasselbe Funknetz: Die Routersicht (TR-064) kennt alle verbundenen Geraete und Baender, die Clientsicht (`netsh` unter Windows, `iw` unter Linux) kennt Signalstaerke und Verbindungsrate des messenden Rechners. Erst zusammen zeigen sie, ob ein Abriss am Router oder am Endgeraet lag.

Der zyklische Bandwechsel setzt **getrennte SSIDs je Band** voraus; in der FRITZ!Box muss dafuer das Band-Steering abgeschaltet sein. Sonst entscheidet der Router, in welchem Band der Rechner landet, und die Messung vergleicht nichts.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | ja | WLAN ueberwachen |
| `interval_s` | Zahl > 0 | `30` s | Abstand zwischen zwei WLAN-Abfragen |
| `router_view` | ja/nein | ja | TR-064-Sicht: Bands, Clients, RSSI |
| `client_view` | ja/nein | ja | Lokale Sicht via netsh/iw |
| `interface` | Text | leer | Interface-Name; leer = automatisch |
| `band_switch_enabled` | ja/nein | nein | Zyklischer Bandwechsel - setzt getrennte SSIDs je Band voraus! |
| `band_switch_interval_s` | Zahl > 0 | `900` s (15 min) | Verweildauer je Band vor dem Wechsel |
| `profiles` | Liste von Eintraegen | leer | WLAN-Profile des Betriebssystems, je Band eines |

### `wlan.profiles` – Eintrag

Nur fuer den Bandwechsel-Test noetig, und dann mindestens zwei Eintraege.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `band` | `2.4GHz` / `5GHz` / `6GHz` | **Pflicht** | Frequenzband dieses Profils |
| `profile_name` | Text | **Pflicht** | Windows: Profilname; Linux: SSID/Verbindungsname |
| `ssid` | Text | leer | Netzname, falls er vom Profilnamen abweicht |


<a id="run"></a>

## `run` – Testlauf

Rahmen eines einzelnen Laufs. `duration_s` nimmt auch Angaben mit Einheit entgegen (`90s`, `30m`, `24h`, `3d`).

`time_gap_threshold_s` trennt zwei Faelle, die in den Rohdaten gleich aussehen: Ein Rechner im Standby hat nicht gemessen, das ist kein Ausfall der Leitung. Deshalb wird der Energiesparmodus waehrend eines Laufs unterdrueckt - gelingt das nicht, laeuft der Test weiter, haelt es aber als `POWER_KEEPALIVE` fest.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `duration_s` | Zahl > 0 | `86400` s (1 d) | Gesamtdauer des Testlaufs |
| `name` | Text | leer | Freier Name, z.B. 'FRITZ!OS 8.02 - Wohnung' |
| `notes` | Text | leer | Freie Notiz zum Lauf; erscheint im Bericht unter dem Namen |
| `resume_open_run` | ja/nein | ja | Offenen Testlauf beim Start fortsetzen statt neu beginnen |
| `time_gap_threshold_s` | Zahl > 0 | `120` s (2 min) | Groessere Luecke der monotonen Uhr gilt als Standby, nicht als Ausfall |
| `prevent_standby` | ja/nein | ja | Waehrend des Testlaufs den Energiesparmodus des Rechners unterdruecken |

<a id="storage"></a>

## `storage` – Speicherorte

Alle Pfade duerfen relativ sein; die Basis ist das Arbeitsverzeichnis, das `paths.py` nach fuenf Regeln ermittelt (`--config`, `FBTEST_HOME`, `config.yaml` im aktuellen Ordner, Projektordner, `%LOCALAPPDATA%\fbtest`).

Messwerte werden gesammelt und gebuendelt geschrieben, nicht einzeln. Die Datenbank laeuft im WAL-Modus: Wer sie kopiert, muss `fbtest.sqlite-wal` und `-shm` mitkopieren, sonst fehlen genau die neuesten Zeilen.

**Waehrend eines laufenden Tests sind diese Felder gesperrt.** Ein Wechsel des Speicherorts mitten im Lauf wuerde die Messreihe zerreissen.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `data_dir` | Pfad | `data` | Ort der SQLite-Datenbank; relativ zum Arbeitsverzeichnis |
| `export_dir` | Pfad | `exports` | Ziel der CSV- und XLSX-Exporte |
| `report_dir` | Pfad | `reports` | Ziel der HTML-Berichte |
| `log_dir` | Pfad | `logs` | Ort der rotierenden Protokolldateien |
| `batch_interval_s` | Zahl > 0 | `5` s | Abstand der Sammel-Schreibvorgaenge in die SQLite-DB |
| `batch_max_rows` | Ganzzahl ab 1 | `500` | Maximale Zeilen je Schreibvorgang |

<a id="logging"></a>

## `logging` – Protokollierung

Das Protokoll ist die zweite Spur neben der Datenbank und beantwortet Fragen, die keine Messreihe beantwortet - etwa warum ein Modul neu gestartet wurde. `DEBUG` protokolliert jeden Einzelaufruf und laesst die Dateien schnell wachsen; fuer die Fehlersuche ist das richtig, fuer einen mehrtaegigen Lauf nicht.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` | Ab welcher Dringlichkeit protokolliert wird |
| `max_bytes` | Ganzzahl ab 1024 | `10485760` | Groesse einer Logdatei bis zur Rotation |
| `backup_count` | Ganzzahl ab 0 | `5` | Anzahl aufbewahrter aelterer Logdateien |

<a id="dashboard"></a>

## `dashboard` – Dashboard

Das Dashboard bleibt bewusst auf `127.0.0.1` und kennt keine Benutzeranmeldung. Gegen Zugriffe fremder Webseiten schuetzt ein Sitzungs-Token, das bei jedem Start neu vergeben wird - kein Cookie, denn ein Cookie wuerde der Browser auch fremden Seiten mitsenden.

Eine Freigabe ins Netz ist nicht vorgesehen. Wer von aussen zusehen will, nimmt einen SSH-Tunnel.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `enabled` | ja/nein | nein | Dashboard beim Start eines Laufs automatisch mitstarten |
| `host` | Text | `127.0.0.1` | Adresse des Dashboards; bewusst nur lokal, es gibt keine Anmeldung |
| `port` | Ganzzahl ab 1 bis 65535 | `8080` | Port des Dashboards |

<a id="desktop"></a>

## `desktop` – Desktop-Anwendung

Gilt nur fuer die gebuendelte Anwendung mit eigenem Fenster. Das Schliessen des Fensters beendet einen laufenden Test **nicht**, sondern versteckt es nur - ein abgewuergter Lauf ueber mehrere Stunden waere nicht wiederherstellbar.

| Feld | Typ | Standard | Bedeutung |
|---|---|---|---|
| `minimize_to_tray` | ja/nein | ja | Fenster schliessen minimiert in den Infobereich statt zu beenden |
| `notifications` | ja/nein | ja | Systemmeldungen bei Neustarts und laengeren Ausfaellen |
| `notify_outage_after_s` | Zahl > 0 | `60` s (1 min) | Ab dieser Ausfalldauer wird gemeldet - kurze Aussetzer nicht |
| `window_width` | Ganzzahl ab 800 | `1280` | Fensterbreite beim Start |
| `window_height` | Ganzzahl ab 600 | `860` | Fensterhoehe beim Start |

---

## Was hier nicht steht

**Zugangsdaten.** Das Passwort der FRITZ!Box wird nicht in der `config.yaml` gefuehrt,
sondern ueber `FRITZ_PASSWORD` oder den Schluesselspeicher des Systems - siehe
«Passwort setzen» in der [README](../README.md#3-passwort-setzen--nicht-in-der-datei).

**Der Speicherort der `config.yaml` selbst.** Welche Datei benutzt wird, entscheiden fuenf
Regeln in der Reihenfolge `--config`, `FBTEST_HOME`, `config.yaml` im aktuellen Ordner,
Projektordner, `%LOCALAPPDATA%\fbtest`. Die Diagnose im Dashboard zeigt an, welche Regel
gegriffen hat.

**Was die Werte bewirken.** Diese Referenz beschreibt die Felder; wie das Programm daraus
Ausfaelle, Neustarts und Kennzahlen ableitet, steht in der [README](../README.md).
