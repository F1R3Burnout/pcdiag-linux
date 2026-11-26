# PC-Diagnose für Linux

**System, Netzwerk, Bildschirme und Hardware-Stabilität mit lokal gespeicherten Berichten prüfen.**

Das Linux-Werkzeug ergänzt [PC-Diagnose für Windows](https://github.com/F1R3Burnout/PC-Diagnose). Es verwendet Python 3 und die Standardbibliothek. Optionale Systemwerkzeuge erweitern einzelne Prüfungen.

## Inhalt

- [Funktionen](#funktionen)
- [Installation](#installation)
- [Bedienung](#bedienung)
- [Konfiguration](#konfiguration)
- [Entwicklung](#entwicklung)
- [Weitere Dokumentation](#weitere-dokumentation)

## Funktionen

| Werkzeug | Prüfbereich |
| --- | --- |
| `sysdiag` | Kompakte Hardwareübersicht, SMART, fehlgeschlagene Dienste, Neustart, Kernel-Ereignisse und Core-Dump-Nachweise mit Quellen |
| `netdiag` | Adapter und Gateways, DNS/TCP, LAN/WLAN, Tunnel getrennt, auf Wunsch Speedtest mit Server |
| `displaydiag` | DRM/EDID, aktiver Modus unter KDE-Wayland oder X11, HDMI/DP-Audio und Vergleich mit einem vorherigen Bericht |
| `stability` | CPU-, RAM-, GPU-/VRAM- und Speichertests mit Ergebnisvergleich, Temperaturüberwachung und Kernel-Korrelation |

## Installation

Zielsysteme sind Debian, Linux Mint und CachyOS. Voraussetzung ist Python 3.9 oder neuer;
für die vollständigen Systemabfragen werden systemd und je nach Abfrage passende Rechte benötigt.

Der Starter lädt den Quellcode und startet das Menü, bei verfügbarem `sudo` mit einer Rechteabfrage:

```bash
curl -fsSL https://kiwus-it.de/l | bash
```

Der Kurzlink verweist auf [install.sh](install.sh). Alternativ das Repository klonen und lokal starten:

```bash
git clone https://github.com/F1R3Burnout/pcdiag-linux.git
cd pcdiag-linux
sudo python3 -m pcdiag
```

Fehlende optionale Pakete werden nach Rückfrage über den erkannten Paketmanager installiert. `--no-install` unterbindet diese Installation, `--no-download` zusätzliche Werkzeugdownloads.

## Bedienung

Im Menü einen Prüfbereich wählen oder direkt angeben:

```bash
sudo python3 -m pcdiag sysdiag --no-install --zip --output "$HOME/pcdiag-system"
python3 -m pcdiag netdiag --lan-only --no-install
python3 -m pcdiag displaydiag --no-install
python3 -m pcdiag displaydiag --compare /pfad/zum/alten/report.json --no-install
sudo python3 -m pcdiag stability --dry-run
sudo python3 -m pcdiag stability --profile quick --components cpu,memory,kernel --load-seconds 5 --no-install --no-download
```

Die Profile heißen `quick`, `standard` und `extended`. Ein Stabilitätstest erzeugt aktive Last und kann temporäre Testdateien schreiben. `PASS` bezieht sich auf den tatsächlich geprüften Bereich. GPU- und RAM-Abdeckung hängen von Treibern, Werkzeugen und verfügbarem Userspace-Speicher ab.

HTML- und JSON-Berichte liegen standardmäßig unter `~/.local/state/pcdiag/<tool>_<zeit>/`, jeweils im Benutzerkontext des gestarteten Prozesses. `--zip` erzeugt zusätzlich ein lokales Archiv. Für `--output` bei jedem Lauf einen neuen Ordner wählen. Vor Weitergabe die enthaltenen Systeminformationen prüfen; das Tool lädt keine Berichte oder Dumps hoch.

## Konfiguration

Die Bedienung erfolgt über CLI-Optionen. `--output` setzt den Berichtspfad, `--workdir` das Verzeichnis für den Schreib-/Lesetest, `--components` die Komponenten des Stabilitätstests. `netdiag --speedtest` nutzt nur einen vorhandenen Speedtest-Client und erzeugt Internetlast. Für den aktiven Displaymodus als angemeldeter Desktop-Benutzer starten, nicht mit `sudo`.

`sysdiag --fix-xrdp-polkit` ist eine ausdrücklich gewählte Systemänderung mit Bestätigung. Sie richtet eine benutzerbezogene PolicyKit-Regel für NetworkManager ein und startet PolicyKit neu. Details und Plattformgrenzen stehen in [docs/TECHNIK.md](docs/TECHNIK.md).

## Entwicklung

`pcdiag/cli.py` enthält Menü und Optionen. Die Module in `pcdiag/` führen die Prüfungen aus, `tests/` enthält die Regressionstests.

```bash
python3 -m unittest discover -s tests -v
```

Die CI-Konfiguration liegt in [.github/workflows/test.yml](.github/workflows/test.yml).

## Weitere Dokumentation

- [Technische Details, Distributionen und Testgrenzen](docs/TECHNIK.md)
- [Windows-Werkzeug](https://github.com/F1R3Burnout/PC-Diagnose)
- [Lizenz](LICENSE)
