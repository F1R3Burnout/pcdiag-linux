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
| `sysdiag` | CPU/RAM, Datenträger, fehlgeschlagene Dienste und Kernel-Fehler |
| `netdiag` | Schnittstellen, Gateway, DNS, Internet, Linkgeschwindigkeit und WLAN |
| `displaydiag` | DRM-Anschlüsse, EDID, Display-Fehler und HDMI/DP-Audio |
| `stability` | CPU-, RAM-, GPU-/VRAM- und Speichertests mit Ergebnisvergleich, Temperaturüberwachung und Kernel-Korrelation |

## Installation

Voraussetzungen sind Linux mit Python 3, für die vollständigen Systemabfragen ein systemd-System und passende Zugriffsrechte.

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
sudo python3 -m pcdiag sysdiag
sudo python3 -m pcdiag stability --dry-run
sudo python3 -m pcdiag stability --profile quick
```

Die Profile heißen `quick`, `standard` und `extended`. Ein Stabilitätstest erzeugt aktive Last und kann temporäre Testdateien schreiben. `PASS` bezieht sich auf den tatsächlich geprüften Bereich. GPU- und RAM-Abdeckung hängen von Treibern, Werkzeugen und verfügbarem Userspace-Speicher ab.

HTML- und JSON-Berichte liegen standardmäßig unter `~/.local/state/pcdiag/<tool>_<zeit>/`, jeweils im Benutzerkontext des gestarteten Prozesses. Vor Weitergabe die enthaltenen Systeminformationen prüfen.

## Konfiguration

Die Bedienung erfolgt über CLI-Optionen. `--output` setzt den Berichtspfad, `--workdir` das Verzeichnis für den Speichertest, `--components` die Komponenten des Stabilitätstests.

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
