"""Bounded, read-only system journal queries with record-level provenance."""
import json
import re
from datetime import datetime, timezone

from .common import ATTENTION, FAIL, PASS, UNSUPPORTED, Section, have, read, run

COREDUMP_ID = "fc2e22bc6ee647b6b90729ab34a250b1"
JOURNAL_FIELDS = ("MESSAGE", "PRIORITY", "_BOOT_ID", "_TRANSPORT", "SYSLOG_IDENTIFIER", "_COMM",
                  "_PID", "_SYSTEMD_UNIT", "COREDUMP_COMM", "COREDUMP_EXE",
                  "COREDUMP_SIGNAL_NAME", "COREDUMP_SIGNAL", "COREDUMP_PID",
                  "COREDUMP_FILENAME")

_SHUTDOWN = re.compile(
    r"systemd-shutdown|reached target.*(?:shutdown|reboot|power.?off)|"
    r"powering off\.?$|rebooting\.?$|starting (?:system )?(?:reboot|power.?off)\b", re.I)
_REBOOT_REQUEST = re.compile(r"reboot|shutdown|power.?off", re.I)
_SHUTDOWN_PROVIDERS = {"systemd", "systemd-shutdown", "systemd-shutdow",
                       "systemd-logind", "systemd-logind.service"}

COMPONENTS = (
    ("Kernel-Panic", r"kernel panic|not syncing", "Vorherige Kernel-Meldungen und pstore prüfen."),
    ("CPU / Machine Check", r"machine check|\bmce:.*(?:error|bank|cpu\s*\d)|"
     r"\[hardware error\].*(?:cpu|apic|processor|cache)",
     "CPU/APIC-Angabe und Fehlerbank abgleichen; CPU, Spannungsversorgung und Firmware prüfen."),
    ("RAM / EDAC", r"edac.*(?:\bce\b|\bue\b|error)|uncorrected.*memory",
     "DIMM-Kanal aus der Meldung abgleichen und RAM separat testen."),
    ("PCIe", r"pcie bus error|\baer:.*(?:corrected|uncorrected|error)",
     "PCI-Adresse und Gerät zuordnen; ein PCIe-Fehler beweist keinen Mainboard-Defekt."),
    ("GPU / Treiber", r"nvrm: xid|gpu hang|gpu reset|"
     r"(?:amdgpu|i915|\bxe\b|nouveau).*(?:timeout|ring.*(?:error|fault)|flip_done|failed)",
     "GPU-Treiber, Stromversorgung und Meldungen direkt vor dem Reset vergleichen."),
    ("Datenträger / Controller", r"blk_update_request|buffer i/o error|i/o error, dev|"
     r"nvme.*(?:timeout|reset|error)|ata\d+.*(?:exception|failed command)",
     "Betroffenes Gerät aus der Meldung mit SMART, Kabel und Controller abgleichen."),
    ("Speichermangel (OOM)", r"oom-kill|out of memory: killed process|oom_reaper",
     "Welcher Prozess beendet wurde sowie RAM-, Swap- und cgroup-Limits prüfen."),
    ("Temperatur", r"thermal.*(?:throttl|critical)|temperature above threshold",
     "Sensor und Zeitpunkt abgleichen, danach Kühlung prüfen."),
    ("Anwendungsabsturz", r"segfault at|general protection fault",
     "Prozessname/PID prüfen und, sofern vorhanden, den Core-Dump-Eintrag heranziehen."),
)


def json_rows(out: str) -> list:
    rows = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("MESSAGE"), str):
            rows.append(row)
    return rows


def query(*args: str, limit: int = 120) -> tuple:
    if not have("journalctl"):
        return [], "journalctl ist nicht verfügbar."
    cmd = ["journalctl", "--no-pager", "-q", "-o", "json",
           "--output-fields=" + ",".join(JOURNAL_FIELDS), "-n", str(limit), *args]
    rc, out = run(cmd, timeout=12)
    if rc:
        return [], out.strip()[:400] or f"journalctl: Exit {rc}"
    rows = json_rows(out)
    if not rows and out.strip():
        return [], out.strip()[:400]
    return rows, ""


def when(row: dict) -> str:
    try:
        return datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1_000_000,
                                      tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (KeyError, ValueError, OverflowError, TypeError, OSError):
        return "Zeitpunkt unbekannt"


def source(row: dict) -> str:
    provider = row.get("_SYSTEMD_UNIT") or row.get("SYSLOG_IDENTIFIER") or row.get("_COMM")
    fields = ["systemd-Journal", when(row), provider or row.get("_TRANSPORT")]
    if row.get("_BOOT_ID"):
        fields.append("Boot " + str(row["_BOOT_ID"]))
    if row.get("__CURSOR"):
        fields.append("Cursor " + str(row["__CURSOR"]))
    return " | ".join(str(x) for x in fields if x)


def restart_context() -> Section:
    s = Section("Neustart & Herunterfahren", expanded=True)
    boot_id = read("/proc/sys/kernel/random/boot_id")
    if boot_id:
        s.data["boot_id"] = boot_id
    rows, error = query("--system", "-b", "-1", limit=180)
    if not rows:
        s.add(UNSUPPORTED, "Voriger Systemstart nicht im lesbaren Journal",
              error or "Ohne Protokoll des vorigen Starts lässt sich die Ursache nicht einordnen.",
              "journalctl --system -b -1")
        return s
    last = rows[-1]
    s.data["previous_boot_id"] = last.get("_BOOT_ID", "")
    ordered = [r for r in rows if (
        r.get("_COMM") in _SHUTDOWN_PROVIDERS
        or r.get("SYSLOG_IDENTIFIER") in _SHUTDOWN_PROVIDERS
        or r.get("_PID") == "1") and _SHUTDOWN.search(r["MESSAGE"])]
    fatal = [r for r in rows if r.get("_TRANSPORT") == "kernel"
             and re.search(r"kernel panic|not syncing", r["MESSAGE"], re.I)]
    if fatal:
        r = fatal[-1]
        s.add(FAIL, "Kernel-Panic im Ende des vorigen Boots aufgezeichnet", r["MESSAGE"][:500], source(r))
    elif ordered:
        r = ordered[-1]
        s.add(PASS, "Geordnete Shutdown-Phase protokolliert",
              r["MESSAGE"][:500] + "\nDer Eintrag beweist nicht, dass der Rechner danach vollständig herunterfuhr.",
              source(r))
    else:
        s.add(ATTENTION, "Ende des vorigen Boots nicht eindeutig",
              "Im gelesenen Journalende fehlt ein Shutdown-Nachweis. Stromausfall, Reset oder eine "
              "Journal-Lücke sind möglich; kein Nachweis für eine konkrete Ursache.", source(last))
    initiators = [r for r in rows
                  if (r.get("_COMM") == "sudo" or r.get("SYSLOG_IDENTIFIER") == "sudo")
                  and "COMMAND=" in r["MESSAGE"]
                  and _REBOOT_REQUEST.search(r["MESSAGE"].split("COMMAND=", 1)[1])]
    if initiators:
        r = initiators[-1]
        s.add(PASS, "Möglicher Neustart-/Shutdown-Aufruf protokolliert", r["MESSAGE"][:400], source(r))
    s.raw = "\n".join(f"{when(r)} {r.get('SYSLOG_IDENTIFIER') or r.get('_COMM') or ''}: {r['MESSAGE'][:500]}"
                      for r in rows[-14:])
    return s


def hardware_context() -> Section:
    s = Section("Kernel- & Hardware-Ereignisse")
    checked = 0
    for boot, label in (("0", "aktueller Boot"), ("-1", "voriger Boot")):
        rows, error = query("--system", "-k", "-b", boot, "-p", "warning", limit=500)
        s.data[boot] = {"records": len(rows), "error": error, "limit": 500}
        if error:
            s.add(UNSUPPORTED, f"Kernel-Journal nicht lesbar ({label})", error, f"journalctl -k -b {boot}")
            continue
        if not rows:
            # No messages at this priority is not proof of whole-journal access.
            s.add(UNSUPPORTED, f"Keine auswertbaren Warn-/Fehler-Einträge ({label})",
                  source=f"journalctl --system -k -b {boot} -p warning")
            continue
        checked += 1
        for category, pattern, action in COMPONENTS:
            matches = [r for r in rows if re.search(pattern, r["MESSAGE"], re.I)]
            if matches:
                r = matches[-1]
                duplicates = len(matches)
                s.add(FAIL if category == "Kernel-Panic" else ATTENTION,
                      f"{category} ({label}): {duplicates} Meldung(en)",
                      r["MESSAGE"][:500] + "\n" + action, source(r))
    if checked and not any(f.status in (ATTENTION, FAIL) for f in s.findings):
        s.add(PASS, "Keine bekannten Hardware-Fehlermuster in den gelesenen Kernel-Einträgen",
              source="journalctl --system -k -b 0/-1 -p warning (je Boot bis 500 Zeilen)")
    return s


def application_crashes() -> Section:
    s = Section("Anwendungsabstürze (Core-Dump-Nachweise)")
    rows, error = query("--system", "--since=-7 days", f"MESSAGE_ID={COREDUMP_ID}", limit=50)
    if not rows:
        s.add(UNSUPPORTED, "Keine zugänglichen systemd-Core-Dump-Einträge in den letzten 7 Tagen",
              error or "Alternativen wie Apport oder ein deaktivierter Core-Dump-Dienst sind hier nicht erfasst.",
              f"journalctl MESSAGE_ID={COREDUMP_ID}")
        return s
    groups = {}
    for r in rows:
        key = (str(r.get("COREDUMP_EXE") or r.get("COREDUMP_COMM") or "Unbekannter Prozess"),
               str(r.get("COREDUMP_SIGNAL_NAME") or r.get("COREDUMP_SIGNAL") or "unbekannt"))
        groups.setdefault(key, []).append(r)
    for (exe, sig), group in reversed(list(groups.items())):
        r = group[-1]
        filename = r.get("COREDUMP_FILENAME")
        s.add(ATTENTION, f"{exe}: {len(group)}× abgestürzt ({sig})",
              f"Letzte PID: {r.get('COREDUMP_PID', 'unbekannt')}. "
              + (f"Im Journal referenzierter Dump: {filename}. " if filename else "")
              + "Die Metadaten belegen den Abbruch, aber nicht die Ursache.", source(r))
    return s
