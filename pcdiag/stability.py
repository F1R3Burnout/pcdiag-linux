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
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

from . import events, gpu as gpu_mod
from .common import (ATTENTION, FAIL, INTERRUPTED, PASS, SKIPPED, UNSUPPORTED, Section,
                     have, is_root, read, run)

PROFILES: Dict[str, dict] = {
    "quick":    {"cpu_s": 45,   "gpu_s": 60,   "ram_mib": 256,  "wv_mib": 64,   "surface_mib": 0},
    "standard": {"cpu_s": 600,  "gpu_s": 300,  "ram_mib": 1024, "wv_mib": 512,  "surface_mib": 4096},
    "extended": {"cpu_s": 3600, "gpu_s": 1200, "ram_mib": 4096, "wv_mib": 2048, "surface_mib": 0},  # 0 = whole disk
}
COMPONENTS = ["cpu", "memory", "gpu", "storage", "kernel"]
THERMAL_WARN_C, THERMAL_ABORT_C = 90.0, 97.0
CPU_REFERENCE = "5d958a37f95d5d892ec9b9f969a86a34b844a4f8406384df26ff99732e3ed325"


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
    n = threads or (len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()) or 1
    rounds = 20000
    if _reference_chain(1, rounds) != CPU_REFERENCE:
        s.add(FAIL, "Referenzberechnung weicht vom hinterlegten SHA-256-Sollwert ab")
        return s
    q, stop = mp.Queue(), mp.Event()
    procs = [mp.Process(target=_cpu_worker, args=(1, rounds, CPU_REFERENCE, stop, q)) for _ in range(n)]
    for p in procs:
        p.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stop_evt.is_set() and all(p.is_alive() for p in procs):
        time.sleep(0.5)
    stop.set()
    res = []
    collect_deadline = time.monotonic() + 5
    for _ in procs:
        try:
            res.append(q.get(timeout=max(0.01, collect_deadline - time.monotonic())))
        except Exception:
            pass
    for p in procs:
        p.join(5)
        if p.is_alive():
            p.terminate()
            p.join(1)
    loops, bad = sum(r[0] for r in res), sum(r[1] for r in res)
    if len(res) < n:
        s.add(FAIL, f"{n - len(res)} von {n} Worker-Prozessen ohne Ergebnis beendet",
              "Ein Worker ist abgestürzt oder wurde vom Kernel beendet (OOM/Signal).")
    if bad:
        s.add(FAIL, f"{bad} falsche Ergebnisse bei {loops} Berechnungen auf {n} Threads",
              "Berechnungsfehler unter Last: möglicher Hinweis auf CPU-/RAM-/Spannungsinstabilität.")
    elif len(res) == n and loops == 0:
        s.add(UNSUPPORTED, "Kein Worker hat eine ganze Verifikation abgeschlossen")
    elif len(res) == n and not stop_evt.is_set():
        s.add(PASS, f"{loops} Berechnungen auf {n} Threads, 0 Abweichungen", f"{seconds}s Last")
    if stop_evt.is_set():
        s.add(INTERRUPTED, "Test vorzeitig beendet")
    s.data = {"loops": loops, "mismatches": bad, "threads": n}
    return s


# ---------------------------------------------------------------- RAM
def _memtester(mib: int, stop_evt, seconds: int) -> tuple:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        cmd = ["memtester", f"{mib}M", "1"]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        except OSError as e:
            return 127, str(e), False
        deadline = time.monotonic() + seconds
        while p.poll() is None and not stop_evt.is_set() and time.monotonic() < deadline:
            stop_evt.wait(0.2)
        limited = p.poll() is None
        if limited:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
        rc = p.wait()
        out.seek(0)
        return rc, out.read()[-3000:], limited


def memory_test(mib: int, stop_evt, load_seconds: Optional[int] = None) -> Section:
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
            if stop_evt.is_set():
                break
            bufs.append(bytearray(random.Random(seed + i).randbytes(chunk)))
        for pat in (b"\x00", b"\xff", b"\xaa", b"\x55"):        # stuck-bit patterns
            if stop_evt.is_set():
                break
            for b in bufs:
                if stop_evt.is_set():
                    break
                b[:] = pat * chunk
            for b in bufs:
                if stop_evt.is_set():
                    break
                if b.count(pat) != chunk:
                    errors += 1
        for i, b in enumerate(bufs):                            # pseudo-random data
            if stop_evt.is_set():
                break
            b[:] = random.Random(seed + i).randbytes(chunk)
        stop_evt.wait(1)
        for i, b in enumerate(bufs):
            if stop_evt.is_set():
                break
            if bytes(b) != random.Random(seed + i).randbytes(chunk):
                errors += 1
    except MemoryError:
        s.add(UNSUPPORTED, "Speicherreservierung fehlgeschlagen")
        return s
    finally:
        bufs.clear()
    if errors:
        s.add(FAIL, f"{errors} Verifikationsfehler in {use} MiB", "Speicherinhalt stimmt nicht mit geschriebenen Daten überein.")
    elif not stop_evt.is_set():
        s.add(PASS, f"{use} MiB Pattern- und Zufallsdaten fehlerfrei verifiziert")
    if stop_evt.is_set():
        s.add(INTERRUPTED, "RAM-Prüfung vor dem vollständigen Mustervergleich beendet")
    if have("memtester") and is_root() and not stop_evt.is_set():
        rc, out, limited = _memtester(min(use, 512), stop_evt, load_seconds or 900)
        if "FAILURE:" in out or (0 < rc < 8 and rc & 6):
            s.add(FAIL, "memtester meldet Fehler", out.strip()[-300:])
        elif limited:
            s.add(INTERRUPTED if stop_evt.is_set() else SKIPPED, "memtester-Zusatzdurchlauf nicht vollständig",
                  "Lastlimit oder Abbruch erreicht. Der interne RAM-Mustervergleich ist ein eigener Befund.")
        elif rc:
            s.add(UNSUPPORTED, f"memtester hat keine Verifikation abgeschlossen (Exit {rc})",
                  out.strip()[-300:])
        else:
            s.add(PASS, "memtester (1 Durchlauf) fehlerfrei")
    s.data = {"allocated_mib": use, "completed": not stop_evt.is_set(), "errors": errors}
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
        # A handful of reallocated sectors is common on aged drives and stable for years; a growing
        # count or any offline-uncorrectable sector is the real warning sign.
        if a["id"] in (5, 197, 198) and raw > 0:
            hard = a["id"] == 198 or (a["id"] == 5 and raw >= 10)
            s.add(FAIL if hard else ATTENTION, f"{a['name']} = {raw}",
                  "Sektoren wurden ersetzt/sind auffällig: Backup prüfen und den Wert beobachten "
                  "(steigt er zwischen Läufen, Datenträger ersetzen).")
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
    rc, out = run(["smartctl", "--scan", "-j"], stdout_only=True)
    try:
        devs = json.loads(out).get("devices", [])
    except ValueError:
        devs = []
    for d in devs:
        cmd = ["smartctl", "-j", "-a", d["name"]]
        attempts = [cmd]
        if d.get("type") and d["type"] != "auto":
            attempts.append(cmd + ["-d", d["type"]])
        s = Section(f"SMART {d['name']}")
        for args in attempts:
            rc, o = run(args, timeout=20, stdout_only=True)
            try:
                data = json.loads(o)
                if not isinstance(data, dict) or not isinstance(data.get("smart_status", {}).get("passed"), bool):
                    continue
                s = parse_smart(data)
            except (AttributeError, ValueError):
                continue
            for f in s.findings:
                f.source = " ".join(args)
            break
        if not s.findings:
            s.add(UNSUPPORTED, f"Kein verwertbarer SMART-Status (Exit {rc})",
                  source="; ".join(" ".join(args) for args in attempts))
        out_secs.append(s)
    if not out_secs:
        s = Section("SMART")
        s.add(UNSUPPORTED, "smartctl hat keine Geräte zur SMART-Abfrage gemeldet",
              source="smartctl --scan -j")
        out_secs.append(s)
    return out_secs


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


def _temp_sensor(path: str) -> dict:
    base, name = os.path.split(path)
    if name.endswith("_input"):
        driver = read(os.path.join(base, "name"))
        label = read(path[:-len("input")] + "label")
    else:
        driver, label = read(os.path.join(base, "type")), ""
    kind = "other"
    tag = f"{driver} {label}".lower()
    if re.search(r"amdgpu|nouveau|nvidia|i915|gpu[_ -]|\bxe\b", tag):
        kind = "gpu"
    elif re.search(r"coretemp|k10temp|x86_pkg_temp|cpu|package id|tdie|tctl", tag):
        kind = "cpu"
    elif re.search(r"nvme|drivetemp", tag):
        kind = "storage"
    return {"kind": kind, "name": " / ".join(filter(None, (driver, label))) or name, "source": path}


class ThermalGuard(threading.Thread):
    def __init__(self, stop_evt):
        super().__init__(daemon=True)
        self.stop_evt, self.peak, self.aborted, self.warned = stop_evt, 0.0, False, False
        self.peaks = {}
        self._done = threading.Event()
        self.sample(read_temps())

    @property
    def available(self):
        return bool(self.peaks)

    def sample(self, temps):
        for path, temp in temps.items():
            self.peaks[path] = max(self.peaks.get(path, 0), temp)
        self.peak = max(self.peaks.values(), default=0)
        self.warned = self.peak >= THERMAL_WARN_C
        if self.peak >= THERMAL_ABORT_C:
            self.aborted = True
            self.stop_evt.set()

    def run(self):
        while not self.aborted and not self._done.wait(1):
            self.sample(read_temps())

    def finish(self):
        self._done.set()


HW_ERR = re.compile(r"(mce:|machine check|hardware error|edac.*(ce|ue|error)|aer:.*(error|corrected|uncorrected)|"
                    r"pcie bus error|nvrm: xid|amdgpu.*(ring.*timeout|gpu reset|vm fault)|i915.*gpu hang|"
                    r"nvme.*(timeout|reset)|i/o error|ata\d+.*(exception|failed command)|thermal.*(critical|throttl))", re.I)


def kernel_log_lines() -> Optional[List[str]]:
    if have("journalctl"):
        rc, out = run(["journalctl", "--system", "-k", "-b", "--no-pager", "-q", "-o", "short-monotonic"],
                      timeout=12)
        if rc == 0 and out.strip():
            return out.splitlines()
    rc, out = run(["dmesg"], timeout=12)
    return out.splitlines() if rc == 0 and out.strip() else None


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
    current = boot_id()
    if m.get("boot_id") and current and m["boot_id"] != current:
        context = events.restart_context()
        same_boot = context.data.get("previous_boot_id") == m["boot_id"]
        fault = same_boot and context.status == FAIL
        s.add(FAIL if fault else ATTENTION,
              f"Bootwechsel nach offenem Testmarker ({m.get('stage')})",
              "Der Marker belegt einen unterbrochenen Lauf, keinen ungeplanten Reset. "
              + ("Das Journal desselben Boots enthält eine Kernel-Panic." if fault else
                 "Geordnetes Herunterfahren oder ein unerwarteter Reset sind möglich."),
              "pending_run.json; /proc/sys/kernel/random/boot_id")
        if same_boot:
            s.findings.extend(context.findings)
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
                  directory: str = "", dry_run: bool = False, allow_download: bool = True,
                  log: Callable[[str], None] = print, load_seconds: Optional[int] = None) -> List[Section]:
    cfg = dict(PROFILES[profile])
    if load_seconds:
        for key in ("cpu_s", "gpu_s"):
            cfg[key] = min(cfg[key], load_seconds)
    comps = components or COMPONENTS
    secs: List[Section] = []
    if dry_run:
        s = Section("DryRun")
        s.add(PASS, f"Profil {profile}: {', '.join(comps)}", json.dumps(cfg))
        s.add(PASS, "Keine Last erzeugt, keine Dateien geschrieben")
        return secs + [s]
    prev = check_previous_run()
    if prev:
        secs.append(prev)

    stop_evt = threading.Event()
    old = signal.signal(signal.SIGINT, lambda *_: stop_evt.set())
    guard = ThermalGuard(stop_evt)
    guard.start()
    before = kernel_log_lines() if "kernel" in comps else None
    inv = Section("Voraussetzungen")
    if "storage" in comps and not is_root():
        inv.add(ATTENTION, "Ohne root: SMART und Roh-Lesetest entfallen")
    else:
        inv.add(PASS, "Mit Root-Rechten" if is_root() else "Mit Benutzerrechten")
    inv.add(PASS if guard.available else ATTENTION,
            "Temperatursensoren gefunden" if guard.available else "Keine Temperatursensoren - Thermal-Guard inaktiv")
    if not os.path.isdir("/var/log/journal"):
        inv.add(ATTENTION, "Kein persistentes Standard-Journalverzeichnis gefunden",
                "Bei einem Reset kann das Journal des vorigen Boots fehlen.",
                "/var/log/journal")
    secs.append(inv)
    try:
        if "cpu" in comps and not stop_evt.is_set():
            log("CPU-Test ..."); set_pending("CPU")
            secs.append(cpu_test(cfg["cpu_s"], stop_evt))
        if "memory" in comps and not stop_evt.is_set():
            log("RAM-Test ..."); set_pending("RAM")
            secs.append(memory_test(cfg["ram_mib"], stop_evt, load_seconds))
        if "gpu" in comps and not stop_evt.is_set():
            log("GPU-/VRAM-Test ..."); set_pending("GPU")
            secs.append(gpu_mod.gpu_test(cfg["gpu_s"], stop_evt, allow_download, log))
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
        guard.join(2)
        set_pending(None)
        signal.signal(signal.SIGINT, old)

    th = Section("Temperaturüberwachung", expanded=True)
    th.data["sensors"] = [{**_temp_sensor(path), "peak_c": round(peak, 1)}
                          for path, peak in sorted(guard.peaks.items())]
    for kind, label in (("cpu", "CPU"), ("gpu", "GPU")):
        candidates = [r for r in th.data["sensors"] if r["kind"] == kind]
        if candidates:
            peak = max(candidates, key=lambda r: r["peak_c"])
            th.add(PASS, f"{label}: bis {peak['peak_c']:g} °C", peak["name"], peak["source"])
        else:
            th.add(UNSUPPORTED, f"{label}: kein zuordenbarer Temperatursensor",
                   "Für diese Komponente ist kein temperaturbasierter Grenzwert nachweisbar.",
                   "hwmon / thermal_zone")
    if not guard.available:
        th.add(UNSUPPORTED, "Keine Sensoren verfügbar")
    elif guard.aborted:
        th.add(FAIL, f"Abbruch: {guard.peak:.0f} °C erreicht (Limit {THERMAL_ABORT_C:.0f} °C)", "Kühlung prüfen.")
    else:
        th.add(ATTENTION if guard.warned else PASS, f"Spitzentemperatur {guard.peak:.0f} °C")
    secs.append(th)

    if "kernel" in comps:
        k = Section("Kernel-/Hardware-Fehler während des Tests")
        after = kernel_log_lines()
        new = diff_hw_errors(before, after) if before is not None and after is not None else []
        if before is None or after is None:
            k.add(UNSUPPORTED, "Kernel-Log nicht vor und nach der Last lesbar",
                  "Keine belastbare Aussage zu neuen Hardware-Meldungen.",
                  "journalctl --system -k -b; dmesg")
        elif new:
            k.add(FAIL, f"{len(new)} neue Hardware-Fehlermeldungen (MCE/EDAC/AER/GPU/NVMe)", "\n".join(new[:10]))
            k.raw = "\n".join(new[:200])
        else:
            k.add(PASS, "Keine neuen bekannten Hardware-Fehlermuster im Kernel-Log",
                  source="journalctl --system -k -b; dmesg")
        secs.append(k)
    if stop_evt.is_set() and not guard.aborted:
        i = Section("Abbruch"); i.add(INTERRUPTED, "Durch Benutzer abgebrochen - Ergebnis unvollständig"); secs.append(i)
    return secs
