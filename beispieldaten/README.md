# Beispieldaten

Exporte aus zwei echten, kurzen Testläufen (je 3 Minuten) auf einer FRITZ!Box 5690 XGS
mit FRITZ!OS 295.08.25. Sie zeigen, wie die Ausgabe von `fbtest export` aussieht, ohne
dass man dafür eine FRITZ!Box braucht.

| Lauf | Name | Dauer | Inhalt |
|---|---|---|---|
| 3 | Test Wlan1 | 3 min | Messung über WLAN |
| 4 | Test lan1 | 3 min | Messung über LAN |

Je Lauf liegen vor:

* `run000X.json` — vollständiger Export in einer Datei
* `run000X_measurements.csv` — alle Messwerte (Latenz, Jitter, Verlust, WLAN, Traffic)
* `run000X_events.csv` — Ereignisse mit Zeitstempel und Schweregrad
* `run000X_router_status.csv` — TR-064-Telemetrie: Laufzeit, Sync-Raten, Byte-Zähler
* `run000X_wlan_status.csv` — WLAN aus Router- und Clientsicht
* `run000X_speedtests.csv`, `run000X_outages.csv`, `run000X_testrun.csv`

## Anonymisiert

Diese Dateien sind **nicht** die Rohexporte. Ersetzt wurden:

* die selbst vergebenen WLAN-Namen durch `TestWLAN-2G` und `TestWLAN-5G`
* die MAC-Adressen der angemeldeten Geräte durch `AA:BB:CC:00:00:01` und `AA:BB:CC:00:00:02`

Alles andere ist unverändert: Messwerte, Zeitstempel, Signalstärken, Firmware und Modell.
Die Adressen `192.168.178.x` sind die Werkseinstellung einer FRITZ!Box und zeigen auf kein
bestimmtes Netz.

## Selbst erzeugen

```powershell
fbtest export 3 --format both
```
