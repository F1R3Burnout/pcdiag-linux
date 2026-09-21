# pcdiag-linux

Linux-Gegenstück zu [PC-Diagnose](https://github.com/F1R3Burnout/PC-Diagnose). Reines Python 3 (nur Standardbibliothek), getestet für Arch/CachyOS, funktioniert auf jeder systemd-Distribution.

## Start

```bash
curl -fsSL https://raw.githubusercontent.com/F1R3Burnout/pcdiag-linux/main/install.sh | bash
```

Direkt mit Tool: `... | bash -s -- stability --profile quick`

Oder aus einem Klon: `sudo python3 -m pcdiag` (interaktives Menü).

## Tools

| Tool | Inhalt |
|---|---|
| `sysdiag` | CPU/RAM, Datenträger, fehlgeschlagene Units, Kernel-Fehler (MCE, EDAC, AER, NVMe, GPU), PCI-GPUs |
| `netdiag` | Schnittstellen, Gateway, Internet, DNS, Linkgeschwindigkeit, WLAN-Signal |
| `displaydiag` | DRM-Anschlüsse, EDID-Prüfung, Kernel-Display-Fehler, HDMI/DP-Audio |
| `stability` | Verifikations-Stabilitätstest, siehe unten |

Reports landen unter `~/.local/state/pcdiag/<tool>_<zeit>/` als `report.html` und `report.json`.

## Hardware-Stabilitätstest

Prinzip wie unter Windows: bekannte Daten bzw. Berechnungen laufen durch die Hardware und werden gegen einen bekannten Sollwert geprüft. **PASS heißt "keine Fehler im getesteten Bereich", nicht "Hardware gesund".**

* **CPU** - SHA-256-Ketten auf allen Threads gegen Referenzwert, jede Abweichung ist ein Fehler.
* **RAM** - Stuck-Bit-Muster (00/FF/AA/55) plus Zufallsdaten mit Seed, Rücklesen und Vergleich; zusätzlich `memtester`, falls installiert.
* **Storage** - SMART über `smartctl` (SATA + NVMe), Schreib-/Rücklese-Verifikation mit fsync in einer normalen Testdatei, optionaler Oberflächen-Lesetest (nur `O_RDONLY`, kein Schreibpfad auf Blockgeräte - per Test abgesichert).
* **Thermal Guard** - hwmon/thermal_zone, Warnung ab 90 °C, Abbruch ab 97 °C.
* **Kernel-Korrelation** - neue MCE-/EDAC-/AER-/GPU-/NVMe-Meldungen während des Laufs machen den Lauf zu FAIL.
* **Reset-Erkennung** - vor jeder Stage wird ein Marker (mit `boot_id`) geschrieben. Findet der nächste Start einen Marker aus einem anderen Boot, meldet er `SYSTEM_RESET` (typisch bei Netzteil-/Spannungs-/Temperaturproblemen, aber kein Beweis).

Profile: `quick` (~1-2 min), `standard`, `extended`. Optionen: `--components cpu,memory,storage,kernel`, `--workdir`, `--dry-run`.

Optional installieren für mehr Abdeckung: `smartmontools`, `memtester`, `pciutils`, `ethtool`, `iw`, `alsa-utils`. Arch/CachyOS: `sudo pacman -S smartmontools memtester pciutils ethtool iw alsa-utils`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

CI läuft auf Ubuntu (Python 3.9 und 3.12) inklusive echtem Quick-Lauf.

## Bekannte Grenzen

* Keine GPU-Compute-/VRAM-Tests (unter Windows via D3D11/memtest_vulkan); kommt bei Bedarf über Vulkan-Tools nach.
* Der Python-RAM-Test deckt nur einen Teil des RAM ab (Linux-Userspace, kein physischer Zugriff). Für ganzen RAM: MemTest86+ vom Boot-Stick.
