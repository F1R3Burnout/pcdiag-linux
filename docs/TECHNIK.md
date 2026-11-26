# PC-Diagnose für Linux · Technische Details

Ergänzung zur [README](../README.md). Hier stehen Parameter, Abläufe und technische Einzelheiten für Einrichtung, Betrieb und Fehlersuche.

## Hardware-Stabilitätstest

Prinzip wie unter Windows: bekannte Daten bzw. Berechnungen laufen durch die Hardware und werden gegen einen bekannten Sollwert geprüft. **PASS heißt "keine Fehler im getesteten Bereich", nicht "Hardware gesund".**

* **CPU** - SHA-256-Ketten auf allen Threads gegen Referenzwert, jede Abweichung ist ein Fehler.
* **RAM** - Stuck-Bit-Muster (00/FF/AA/55) plus Zufallsdaten mit Seed, Rücklesen und Vergleich; zusätzlich `memtester`, falls installiert.
* **GPU/VRAM** - `memtest_vulkan` (Vulkan-Compute-Shader schreiben Muster in den VRAM, lesen zurück, vergleichen). Das Werkzeug wird beim ersten Lauf von GitHub geladen, per gepinntem SHA-256 verifiziert und unter `~/.cache/pcdiag/tools` abgelegt (`--no-download` verhindert das). Ohne echten Vulkan-Treiber (nur llvmpipe) meldet der Test `UNSUPPORTED` und nennt den passenden Treiber für deine Distro.
* **Storage** - SMART über `smartctl` (SATA + NVMe), Schreib-/Rücklese-Verifikation mit fsync in einer normalen Testdatei, optionaler Oberflächen-Lesetest (nur `O_RDONLY`, kein Schreibpfad auf Blockgeräte - per Test abgesichert).
* **Thermal Guard** - hwmon/thermal_zone, je Sensor mit Herkunft und Maximalwert. Warnung ab 90 °C, Abbruch ab 97 °C. CPU-/GPU-Messwerte werden getrennt ausgewiesen; fehlende Werte bleiben sichtbar.
* **Kernel-Korrelation** - neue bekannte Hardware-Fehlermuster werden mit dem Ausgangszustand verglichen. Ein nicht lesbares Journal ist kein erfolgreiches Ergebnis.
* **Unterbrochene Läufe** - ein Marker mit `boot_id` dokumentiert die laufende Stufe. Ein Bootwechsel nach einem offenen Marker beweist nicht, dass der Neustart ungeplant war. Ist das Journal desselben Boots verfügbar, wird es zur Einordnung herangezogen.

Profile: `quick`, `standard`, `extended`. Die tatsächliche Dauer hängt von Geräten und Hilfsprogrammen ab. `--load-seconds` begrenzt CPU, GPU und den optionalen memtester-Zusatzlauf, nicht alle Abfragen oder den internen RAM-Mustervergleich. `--dry-run` erzeugt keine Last und verändert keinen Marker.

## Befundquellen und Grenzen

- Das Systemprotokoll verwendet strukturierte [systemd-Journalfelder](https://www.freedesktop.org/software/systemd/man/latest/systemd.journal-fields.html) inklusive Boot-ID, Zeit, Quelle und Cursor. Ein protokollierter Shutdown-Beginn beweist nicht den vollständigen Abschluss; eine Lücke im Journal beweist keinen Stromausfall.
- Anwendungsabstürze werden über [systemd-coredump](https://www.freedesktop.org/software/systemd/man/latest/systemd-coredump.html) gruppiert. Das Tool erfasst Metadaten, kopiert aber keine Prozessspeicher-Dumps und leitet ohne passenden Nachweis keine Ursache ab.
- Der Netzwerkbericht trennt Tunnel vom Hauptnetz. VPNs ohne sichtbaren Tunneladapter können fehlen. Das Standardprofil prüft externe Ping-, DNS- und TCP-Ziele; `--lan-only` unterbindet diese. Optionaler Speedtest: Download, Upload, Ping und vom Client gemeldeter Server; Messwerte verschiedener Server sind nicht direkt vergleichbar.
- Der Displaybericht unterscheidet vom Monitor angebotene EDID-Modi vom aktuellen Sitzungsmodus. EDID-HDR-Fähigkeit ist nicht gleich aktiviertem HDR. Ohne eine lesbare KDE-Wayland- oder X11-Sitzung gibt es keinen bestätigten aktiven Modus. `--compare` vergleicht zwei lokal erzeugte JSON-Berichte ohne Konfigurationsänderungen.

## xrdp & PolicyKit

`sysdiag` erkennt, wenn `xrdp` installiert ist, aber keine PolicyKit-Regel für passwortlose
NetworkManager-Aktionen existiert. Das ist relevant, weil xrdp-Sitzungen bei `systemd-logind`
nicht als "aktiv" gelten - PolicyKits eingebaute NetworkManager-Richtlinie (die den aktiv
angemeldeten Nutzer sonst ohne Nachfrage durchlässt) greift dann nicht, und jede
NetworkManager-Aktion (z. B. WLAN verbinden) fragt bei jedem Mal erneut nach dem Passwort,
auch für den Besitzer des Rechners.

Beheben:

```bash
sudo python3 -m pcdiag sysdiag --fix-xrdp-polkit
```

Das legt `/etc/polkit-1/rules.d/49-nopasswd-networkmanager.rules` an, die genau dem Benutzer,
der das Tool aufgerufen hat (`$SUDO_USER`, nicht `root`), passwortlose NetworkManager-Aktionen
erlaubt, und startet `polkit` neu. Fragt vorher nach Bestätigung (`--yes` überspringt das).

## Distro-Erkennung

Das Tool liest `/etc/os-release` (`ID` und `ID_LIKE`) und ordnet die Distro einer Paketfamilie zu:

| Familie | Beispiele | Installation |
|---|---|---|
| arch | Arch, CachyOS, Manjaro, EndeavourOS | `pacman -S --needed` |
| debian | Debian, Ubuntu, Linux Mint, Pop!_OS | `apt-get install -y` |
| rhel | Fedora, Nobara, RHEL, Rocky, Alma | `dnf install -y` |
| suse | openSUSE, SLES | `zypper install` |

Fehlen optionale Helfer (`smartmontools`, `memtester`, `pciutils`, `iw`, `alsa-utils`, `vulkan-tools`), fragt das Tool vor dem Lauf nach (`--yes` ohne Rückfrage, `--no-install` gar nicht). Installation ist mit Enter nicht voreingestellt. Unveränderliche Systeme (SteamOS, Silverblue, Bazzite) erhalten nur den Hinweis. Der Stabilitätstest weist darauf hin, wenn das Standardverzeichnis für ein persistentes Journal fehlt.


## Bekannte Grenzen

* Der GPU-Test nutzt `memtest_vulkan` (x86_64; auf ARM nur, wenn es systemweit installiert ist) und testet das von Vulkan gewählte Standardgerät. Es gibt keinen separaten D3D11-artigen Compute-Rechentest wie unter Windows.
* Der Python-RAM-Test deckt nur einen Teil des RAM ab (Linux-Userspace, kein physischer Zugriff). Für ganzen RAM: MemTest86+ vom Boot-Stick.
