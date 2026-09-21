"""Verification-first hardware stability test (Linux).

Principle: known data / known computation is processed by the hardware and the
result is compared with an independently known-good value. PASS only ever means
"no errors within the tested range" - never a guarantee of healthy hardware.

Safety: raw block devices are only ever opened O_RDONLY. Writes go exclusively to
a regular temporary test file that is removed afterwards.
"""
import glob
import hashlib
import json
import multiprocessing as mp
import os
import random
import re
import signal
import threading
import time
from typing import Callable, Dict, List, Optional

from .common import (ATTENTION, FAIL, INTERRUPTED, PASS, SKIPPED, UNSUPPORTED, Section,
                     have, is_root, read, run)

PROFILES: Dict[str, dict] = {
    "quick":    {"cpu_s": 45,   "ram_mib": 256,  "wv_mib": 64,   "surface_mib": 0},
    "standard": {"cpu_s": 600,  "ram_mib": 1024, "wv_mib": 512,  "surface_mib": 4096},
    "extended": {"cpu_s": 3600, "ram_mib": 4096, "wv_mib": 2048, "surface_mib": 0},  # 0 = whole disk
}
COMPONENTS = ["cpu", "memory", "storage", "kernel"]
THERMAL_WARN_C, THERMAL_ABORT_C = 90.0, 97.0


# ---------------------------------------------------------------- CPU
def _reference_chain(seed: int, rounds: int) -> str:
    h = hashlib.sha256(seed.to_bytes(8, "little")).digest()
    for _ in range(rounds):
        h = hashlib.sha256(h).digest()
    return h.hex()


def _cpu_worker(seed: int, rounds: int, expected: str, stop, q) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    loops = bad = 0
    while not stop.is_set():
        if _reference_chain(seed, rounds) != expected:
            bad += 1
        loops += 1
    q.put((loops, bad))


def cpu_test(seconds: int, stop_evt, threads: Optional[int] = None) -> Section:
    s = Section("CPU Compute")
    n = threads or os.cpu_count() or 1
    rounds = 20000
    expected = _reference_chain(1, rounds)   # computed once, on this core, before load
    if _reference_chain(1, rounds) != expected:
        s.add(FAIL, "Referenzberechnung ist nicht reproduzierbar", "Sehr starkes Indiz für instabile CPU/RAM.")
        return s
    q, stop = mp.Queue(), mp.Event()
    procs = [mp.Process(target=_cpu_worker, args=(1, rounds, expected, stop, q)) for _ in range(n)]
    for p in procs:
        p.start()
    end = time.time() + seconds
    while time.time() < end and not stop_evt.is_set():
        time.sleep(0.5)
    stop.set()
    res = []
    for _ in procs:
        try:
            res.append(q.get(timeout=30))
        except Exception:
            pass
    for p in procs:
        p.join(5)
        if p.is_alive():
            p.terminate()
    loops, bad = sum(r[0] for r in res), sum(r[1] for r in res)
    if len(res) < n:
        s.add(FAIL, f"{n - len(res)} von {n} Worker-Prozessen ohne Ergebnis beendet",
              "Ein Worker ist abgestürzt oder wurde vom Kernel beendet (OOM/Signal).")
    if bad:
        s.add(FAIL, f"{bad} falsche Ergebnisse bei {loops} Berechnungen auf {n} Threads",
              "Berechnungsfehler unter Last: möglicher Hinweis auf CPU-/RAM-/Spannungsinstabilität.")
    elif len(res) == n:
        s.add(PASS, f"{loops} Berechnungen auf {n} Threads, 0 Abweichungen", f"{seconds}s Last")
    if stop_evt.is_set():
        s.add(INTERRUPTED, "Test vorzeitig beendet")
    s.data = {"loops": loops, "mismatches": bad, "threads": n}
    return s


# ---------------------------------------------------------------- RAM
def memory_test(mib: int, stop_evt) -> Section:
    s = Section("RAM Pattern-Verifikation")
    avail = 0
    for l in read("/proc/meminfo").splitlines():
        if l.startswith("MemAvailable"):
            avail = int(l.split()[1]) // 1024
    use = min(mib, int(avail * 0.6))
    if use < 64:
        s.add(UNSUPPORTED, "Zu wenig freier RAM für einen aussagekräftigen Test", f"verfügbar: {avail} MiB")
        return s
    if use < mib:
        s.add(ATTENTION, f"Testgröße auf {use} MiB reduziert (verfügbar: {avail} MiB)",
              "Nur ein Teil des RAM wird geprüft (Coverage-Einschränkung).")
    chunk = 8 * 1024 * 1024
    n = use * 1024 * 1024 // chunk
    seed = random.SystemRandom().randrange(1 << 32)
    errors = 0
    bufs = []
    try:
        for i in range(n):
            bufs.append(bytearray(random.Random(seed + i).randbytes(chunk)))
        for pat in (b"\x00", b"\xff", b"\xaa", b"\x55"):        # stuck-bit patterns
            for i, b in enumerate(bufs):
                b[:] = pat * chunk
            for b in bufs:
                if b.count(pat) != chunk:
                    errors += 1
            if stop_evt.is_set():
                break
        for i, b in enumerate(bufs):                            # pseudo-random data
            b[:] = random.Random(seed + i).randbytes(chunk)
        time.sleep(1)
        for i, b in enumerate(bufs):
            if bytes(b) != random.Random(seed + i).randbytes(chunk):
                errors += 1
    except MemoryError:
        s.add(UNSUPPORTED, "Speicherreservierung fehlgeschlagen")
        return s
    finally:
        bufs.clear()
    if errors:
        s.add(FAIL, f"{errors} Verifikationsfehler in {use} MiB", "Speicherinhalt stimmt nicht mit geschriebenen Daten überein.")
    else:
        s.add(PASS, f"{use} MiB Pattern- und Zufallsdaten fehlerfrei verifiziert")
    if have("memtester") and is_root() and not stop_evt.is_set():
        rc, out = run(["memtester", f"{min(use, 512)}M", "1"], timeout=900)
        if "FAILURE" in out or rc not in (0,):
            s.add(FAIL, "memtester meldet Fehler", out.strip()[-300:])
        else:
            s.add(PASS, "memtester (1 Durchlauf) fehlerfrei")
    s.data = {"tested_mib": use, "errors": errors}
    return s


# ---------------------------------------------------------------- Storage
def parse_smart(js: dict) -> Section:
    """Evaluate `smartctl -j -a` output for one device."""
    s = Section(f"SMART {js.get('device', {}).get('name', '?')}")
    model = js.get("model_name", "unbekanntes Modell")
    st = js.get("smart_status")
    if st is None:
        s.add(UNSUPPORTED, f"{model}: kein SMART-Status verfügbar")
        return s
    s.add(PASS if st.get("passed") else FAIL, f"{model}: SMART-Gesamtstatus {'PASSED' if st.get('passed') else 'FAILED'}")
    nv = js.get("nvme_smart_health_information_log")
    if nv:
        if nv.get("critical_warning"):
            s.add(FAIL, f"NVMe critical_warning = {nv['critical_warning']}")
        if nv.get("media_errors"):
            s.add(FAIL, f"NVMe media_errors = {nv['media_errors']}")
        if nv.get("percentage_used", 0) >= 90:
            s.add(ATTENTION, f"NVMe {nv['percentage_used']} % Lebensdauer verbraucht")
        if nv.get("available_spare", 100) < nv.get("available_spare_threshold", 0) + 5:
            s.add(ATTENTION, "NVMe Reserve nahe am Schwellwert")
        if nv.get("temperature", 0) >= 70:
            s.add(ATTENTION, f"NVMe Temperatur {nv['temperature']} °C")
    for a in js.get("ata_smart_attributes", {}).get("table", []):
        raw = a.get("raw", {}).get("value", 0)
        if a["id"] in (5, 197, 198) and raw > 0:
            s.add(FAIL if a["id"] != 197 else ATTENTION, f"{a['name']} = {raw}",
                  "Reallokierte/ausstehende Sektoren: Backup prüfen.")
        if a["id"] == 199 and raw > 0:
            s.add(ATTENTION, f"UDMA_CRC_Error_Count = {raw}", "Meist Kabel-/Steckerproblem, nicht Datenträger.")
    return s


def smart_test() -> List[Section]:
    if not have("smartctl"):
        s = Section("SMART")
        s.add(UNSUPPORTED, "smartctl nicht installiert", "Paket smartmontools installieren.")
        return [s]
    if not is_root():
        s = Section("SMART")
        s.add(UNSUPPORTED, "SMART benötigt root")
        return [s]
    out_secs = []
    rc, out = run(["smartctl", "--scan", "-j"])
    try:
        devs = json.loads(out).get("devices", [])
    except ValueError:
        devs = []
    for d in devs:
        rc, o = run(["smartctl", "-j", "-a", d["name"]], timeout=60)
        try:
            out_secs.append(parse_smart(json.loads(o)))
        except ValueError:
            s = Section(f"SMART {d['name']}")
            s.add(UNSUPPORTED, "smartctl-Ausgabe nicht auswertbar")
            out_secs.append(s)
    return out_secs or [Section("SMART", UNSUPPORTED)]


def write_verify_test(directory: str, mib: int, stop_evt) -> Section:
    s = Section("Storage Write/Read-Verifikation")
    path = os.path.join(directory, f".pcdiag_wv_{os.getpid()}.tmp")
    seed = random.SystemRandom().randrange(1 << 32)
    chunk = 4 * 1024 * 1024
    n = max(1, mib * 1024 * 1024 // chunk)
    try:
        st = os.statvfs(directory)
        if st.f_bavail * st.f_frsize < (mib + 256) * 1024 * 1024:
            s.add(UNSUPPORTED, "Zu wenig freier Speicherplatz für den Test", directory)
            return s
        wsum = hashlib.sha256()
        with open(path, "wb") as f:
            for i in range(n):
                b = random.Random(seed + i).randbytes(chunk)
                wsum.update(b)
                f.write(b)
            f.flush()
            os.fsync(f.fileno())
        fd = os.open(path, os.O_RDONLY)
        try:
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)   # push reads to the device
        finally:
            os.close(fd)
        bad = 0
        with open(path, "rb") as f:
            for i in range(n):
                if f.read(chunk) != random.Random(seed + i).randbytes(chunk):
                    bad += 1
                if stop_evt.is_set():
                    break
        if bad:
            s.add(FAIL, f"{bad} von {n} Blöcken beim Rücklesen verändert", "Datenkorruption im Schreib-/Lesepfad.")
        else:
            s.add(PASS, f"{mib} MiB geschrieben, per fsync gesichert, identisch zurückgelesen", directory)
    except OSError as e:
        s.add(FAIL, "I/O-Fehler beim Schreib-/Lesetest", str(e))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return s


def surface_read_test(device: str, limit_mib: int, stop_evt,
                      progress: Optional[Callable[[int], None]] = None) -> Section:
    """Sequential READ-ONLY scan. Opens with O_RDONLY, no write path exists."""
    s = Section(f"Oberflächen-Lesetest {device}")
    chunk = 4 * 1024 * 1024
    total = errors = 0
    try:
        fd = os.open(device, os.O_RDONLY)
    except OSError as e:
        s.add(UNSUPPORTED, "Gerät nicht lesbar", str(e))
        return s
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, 0, os.SEEK_SET)
        cap = size if limit_mib == 0 else min(size, limit_mib * 1024 * 1024)
        pos = 0
        while pos < cap and not stop_evt.is_set():
            try:
                os.lseek(fd, pos, os.SEEK_SET)
                data = os.read(fd, min(chunk, cap - pos))
                if not data:
                    break
                total += len(data)
                pos += len(data)
            except OSError:
                errors += 1
                pos += 512 * 1024                    # skip the bad region, keep scanning
                if errors > 50:
                    break
            if progress:
                progress(int(pos * 100 / cap) if cap else 100)
    finally:
        os.close(fd)
    cov = f"{total // (1024 * 1024)} von {size // (1024 * 1024)} MiB gelesen"
    if errors:
        s.add(FAIL, f"{errors} Lesefehler", cov)
    else:
        s.add(PASS if total else UNSUPPORTED, "Keine Lesefehler im getesteten Bereich", cov)
    return s


# ---------------------------------------------------------------- Thermal / kernel log
def read_temps() -> Dict[str, float]:
    t = {}
    for p in glob.glob("/sys/class/hwmon/hwmon*/temp*_input") + glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            v = float(read(p)) / 1000.0
        except ValueError:
            continue
        if 0 < v < 150:
            t[p] = v
    return t


class ThermalGuard(threading.Thread):
    def __init__(self, stop_evt):
        super().__init__(daemon=True)
        self.stop_evt, self.peak, self.aborted, self.warned = stop_evt, 0.0, False, False
        self.available = bool(read_temps())
        self._done = threading.Event()

    def run(self):
        while not self._done.wait(2):
            temps = read_temps()
            if not temps:
                continue
            m = max(temps.values())
            self.peak = max(self.peak, m)
            if m >= THERMAL_ABORT_C:
                self.aborted = True
                self.stop_evt.set()
                return
            if m >= THERMAL_WARN_C:
                self.warned = True

    def finish(self):
        self._done.set()


HW_ERR = re.compile(r"(mce:|machine check|hardware error|edac.*(ce|ue|error)|aer:.*(error|corrected|uncorrected)|"
                    r"pcie bus error|nvrm: xid|amdgpu.*(ring.*timeout|gpu reset|vm fault)|i915.*gpu hang|"
                    r"nvme.*(timeout|reset)|i/o error|ata\d+.*(exception|failed command)|thermal.*(critical|throttl))", re.I)


def kernel_log_lines() -> List[str]:
    if have("journalctl"):
        rc, out = run(["journalctl", "-k", "-b", "--no-pager", "-q", "-o", "short-monotonic"], timeout=60)
    else:
        rc, out = run(["dmesg"], timeout=60)
    return out.splitlines() if rc == 0 else []


def diff_hw_errors(before: List[str], after: List[str]) -> List[str]:
    seen = set(before)
    return [l for l in after if l not in seen and HW_ERR.search(l)]


# ---------------------------------------------------------------- reset detection
def state_dir() -> str:
    d = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "pcdiag")
    os.makedirs(d, exist_ok=True)
    return d


def _marker() -> str:
    return os.path.join(state_dir(), "pending_run.json")


def boot_id() -> str:
    return read("/proc/sys/kernel/random/boot_id")


def check_previous_run() -> Optional[Section]:
    """A leftover marker from a different boot means the machine reset mid-test."""
    try:
        with open(_marker()) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    os.remove(_marker())
    s = Section("Vorheriger Lauf")
    if m.get("boot_id") and m["boot_id"] != boot_id():
        s.add(FAIL, f"SYSTEM_RESET: Rechner wurde während '{m.get('stage')}' neu gestartet/abgeschaltet",
              "Ungeplanter Reset unter Last: typisch bei Netzteil-, Spannungs- oder Temperaturproblemen, "
              "aber auch bei Kernel-Panics. Kein Beweis für eine bestimmte Komponente - Journal des "
              "vorigen Boots prüfen: journalctl -b -1 -e")
    else:
        s.add(ATTENTION, "Vorheriger Lauf wurde abgebrochen (gleicher Boot)", str(m.get("stage")))
    return s


def set_pending(stage: Optional[str]) -> None:
    if stage is None:
        try:
            os.remove(_marker())
        except OSError:
            pass
        return
    with open(_marker(), "w") as f:
        json.dump({"stage": stage, "boot_id": boot_id(), "time": time.time()}, f)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------- orchestration
def run_stability(profile: str = "quick", components: Optional[List[str]] = None,
                  directory: str = "", dry_run: bool = False,
                  log: Callable[[str], None] = print) -> List[Section]:
    cfg = PROFILES[profile]
    comps = components or COMPONENTS
    secs: List[Section] = []
    prev = check_previous_run()
    if prev:
        secs.append(prev)
    if dry_run:
        s = Section("DryRun")
        s.add(PASS, f"Profil {profile}: {', '.join(comps)}", json.dumps(cfg))
        s.add(PASS, "Keine Last erzeugt, keine Dateien geschrieben")
        return secs + [s]

    stop_evt = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *_: stop_evt.set())
    guard = ThermalGuard(stop_evt)
    guard.start()
    before = kernel_log_lines()
    inv = Section("Voraussetzungen")
    inv.add(PASS if is_root() else ATTENTION, "root" if is_root() else "Ohne root: SMART und Roh-Lesetest entfallen")
    inv.add(PASS if guard.available else ATTENTION,
            "Temperatursensoren gefunden" if guard.available else "Keine Temperatursensoren - Thermal-Guard inaktiv")
    secs.append(inv)
    try:
        if "cpu" in comps and not stop_evt.is_set():
            log("CPU-Test ..."); set_pending("CPU")
            secs.append(cpu_test(cfg["cpu_s"], stop_evt))
        if "memory" in comps and not stop_evt.is_set():
            log("RAM-Test ..."); set_pending("RAM")
            secs.append(memory_test(cfg["ram_mib"], stop_evt))
        if "storage" in comps and not stop_evt.is_set():
            log("Storage-Tests ..."); set_pending("STORAGE")
            secs.extend(smart_test())
            secs.append(write_verify_test(directory or os.path.expanduser("~"), cfg["wv_mib"], stop_evt))
            if cfg["surface_mib"] != 0 or profile == "extended":
                if is_root():
                    rc, out = run(["lsblk", "-dno", "NAME,TYPE"])
                    for l in out.splitlines():
                        n = l.split()
                        if len(n) == 2 and n[1] == "disk":
                            secs.append(surface_read_test(f"/dev/{n[0]}", cfg["surface_mib"], stop_evt))
                else:
                    x = Section("Oberflächen-Lesetest"); x.add(UNSUPPORTED, "benötigt root"); secs.append(x)
            else:
                x = Section("Oberflächen-Lesetest"); x.add(SKIPPED, f"Im Profil {profile} nicht enthalten"); secs.append(x)
    finally:
        guard.finish()
        set_pending(None)
        signal.signal(signal.SIGINT, old)

    th = Section("Thermal Guard")
    if not guard.available:
        th.add(UNSUPPORTED, "Keine Sensoren verfügbar")
    elif guard.aborted:
        th.add(FAIL, f"Abbruch: {guard.peak:.0f} °C erreicht (Limit {THERMAL_ABORT_C:.0f} °C)", "Kühlung prüfen.")
    else:
        th.add(ATTENTION if guard.warned else PASS, f"Spitzentemperatur {guard.peak:.0f} °C")
    secs.append(th)

    if "kernel" in comps:
        k = Section("Kernel-/Hardware-Fehler während des Tests")
        new = diff_hw_errors(before, kernel_log_lines())
        if new:
            k.add(FAIL, f"{len(new)} neue Hardware-Fehlermeldungen (MCE/EDAC/AER/GPU/NVMe)", "\n".join(new[:10]))
            k.raw = "\n".join(new[:200])
        else:
            k.add(PASS, "Keine neuen Hardware-Fehlermeldungen im Kernel-Log")
        secs.append(k)
    if stop_evt.is_set() and not guard.aborted:
        i = Section("Abbruch"); i.add(INTERRUPTED, "Durch Benutzer abgebrochen - Ergebnis unvollständig"); secs.append(i)
    return secs
