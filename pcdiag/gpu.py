"""GPU compute + VRAM verification via memtest_vulkan (Vulkan compute shaders).

memtest_vulkan writes known patterns into VRAM with compute shaders, reads them back
and compares - the same verification-first principle as the rest of the toolkit.
The binary is pinned by SHA-256 and only ever executed after verification.
"""
import hashlib
import os
import platform
import re
import signal
import subprocess
import tarfile
import threading
import time
import urllib.request
from typing import List, Optional

from .common import (ATTENTION, FAIL, INTERRUPTED, PASS, UNSUPPORTED, Section, have, run)
from .distro import VULKAN_DRIVER_HINT, detect

TOOL = {
    "name": "memtest_vulkan",
    "version": "0.5.0",
    "url": "https://github.com/GpuZelenograd/memtest_vulkan/releases/download/v0.5.0/"
           "memtest_vulkan-v0.5.0_DesktopLinux_X86_64.tar.xz",
    "sha256": "0e058c28b9d0d6eb04f5edeebce73e8106f88c4ea307d46e98c715da61117d52",
    "member": "memtest_vulkan",
    "license": "Zlib",
    "source": "https://github.com/GpuZelenograd/memtest_vulkan",
}

_ERR = re.compile(r"error found|memory error", re.I)
_LOST = re.compile(r"ERROR_DEVICE_LOST|device lost", re.I)
_INIT = re.compile(r"runtime error|initialization failure|no vulkan|failed to create instance|"
                   r"failed to load|early exit during init|no suitable|ERROR_INITIALIZATION_FAILED", re.I)
_OK = re.compile(r"\bpassed\b", re.I)


def cache_dir() -> str:
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pcdiag", "tools")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def safe_extract_member(archive: str, member: str, dest: str) -> str:
    """Extract exactly one regular file by exact name - no path traversal, no links."""
    with tarfile.open(archive, "r:*") as tf:
        info = next((m for m in tf.getmembers() if m.name.lstrip("./") == member), None)
        if info is None or not info.isreg():
            raise ValueError(f"'{member}' nicht im Archiv")
        out = os.path.join(dest, member)
        src = tf.extractfile(info)
        with open(out, "wb") as f:
            f.write(src.read())
    os.chmod(out, 0o755)
    return out


def resolve_tool(allow_download: bool = True, log=print) -> "tuple[Optional[str], str]":
    """Returns (path, note). Prefers a verified cached copy, then a system install."""
    d = cache_dir()
    exe = os.path.join(d, TOOL["member"])
    marker = exe + ".sha256"
    if os.path.isfile(exe) and os.path.isfile(marker) and open(marker).read().strip() == TOOL["sha256"]:
        return exe, f"{TOOL['name']} {TOOL['version']} (verifizierter Cache)"
    if platform.machine() != "x86_64":
        sysbin = None
        from shutil import which
        sysbin = which("memtest_vulkan")
        return (sysbin, "System-Installation") if sysbin else (None, f"Kein Release für {platform.machine()} gepinnt")
    if not allow_download:
        from shutil import which
        p = which("memtest_vulkan")
        return (p, "System-Installation") if p else (None, "Download deaktiviert (--no-download)")
    os.makedirs(d, exist_ok=True)
    arc = os.path.join(d, "memtest_vulkan.tar.xz")
    try:
        log(f"Lade {TOOL['name']} {TOOL['version']} ({TOOL['source']}) ...")
        req = urllib.request.Request(TOOL["url"], headers={"User-Agent": "pcdiag-linux"})
        with urllib.request.urlopen(req, timeout=60) as r, open(arc, "wb") as f:
            f.write(r.read())
        got = sha256_file(arc)
        if got != TOOL["sha256"]:
            os.remove(arc)
            return None, f"SHA-256 stimmt nicht (erwartet {TOOL['sha256'][:12]}..., erhalten {got[:12]}...) - verworfen"
        path = safe_extract_member(arc, TOOL["member"], d)
        with open(marker, "w") as f:
            f.write(TOOL["sha256"])
        return path, f"{TOOL['name']} {TOOL['version']} (heruntergeladen, SHA-256 verifiziert)"
    except Exception as e:
        return None, f"Download fehlgeschlagen: {e}"


def vulkan_devices() -> "tuple[List[str], bool]":
    """(device names, only_software) from vulkaninfo, or ([], False) when unavailable."""
    if not have("vulkaninfo"):
        return [], False
    rc, out = run(["vulkaninfo", "--summary"], timeout=30)
    if rc != 0:
        return [], False
    names = re.findall(r"deviceName\s*=\s*(.+)", out)
    types = re.findall(r"deviceType\s*=\s*(\S+)", out)
    software = bool(names) and all("CPU" in t or "llvmpipe" in n.lower() for n, t in zip(names, types or ["CPU"] * len(names)))
    return [n.strip() for n in names], software


def evaluate_output(text: str, completed_ok: bool) -> Section:
    """Pure evaluation of memtest_vulkan output (unit-testable)."""
    s = Section("GPU Compute & VRAM (Vulkan)")
    if _ERR.search(text):
        line = next(l for l in text.splitlines() if _ERR.search(l))
        s.add(FAIL, "VRAM-/Compute-Verifikationsfehler gefunden", line.strip()[:200])
    elif _LOST.search(text):
        s.add(FAIL, "GPU Device Lost während des Tests", "Treiber-/Hardware-Reset (TDR-Äquivalent) - Kernel-Log prüfen.")
    elif _INIT.search(text) and not _OK.search(text):
        line = next(l for l in text.splitlines() if _INIT.search(l))
        s.add(UNSUPPORTED, "Vulkan-Initialisierung fehlgeschlagen", line.strip()[:200])
    elif _OK.search(text):
        n = len(_OK.findall(text))
        s.add(PASS, f"{n} Verifikationsdurchläufe ohne Fehler", "Keine Fehler im getesteten VRAM-Bereich.")
    else:
        s.add(UNSUPPORTED, "Keine auswertbare Testausgabe", text.strip()[-200:])
    return s


def _stop_group(p: "subprocess.Popen") -> None:
    """memtest_vulkan re-spawns a worker child; signalling only the parent leaves it running at full
    GPU load. It runs in its own session, so stop the whole process group: SIGINT, then TERM, then KILL."""
    for sig, wait in ((signal.SIGINT, 8), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(wait)
        except subprocess.TimeoutExpired:
            continue
        # parent gone; make sure no group member survived either
        try:
            os.killpg(p.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(1)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def gpu_test(seconds: int, stop_evt: threading.Event, allow_download: bool = True, log=print) -> Section:
    if platform.machine() not in ("x86_64", "aarch64"):
        s = Section("GPU Compute & VRAM (Vulkan)")
        s.add(UNSUPPORTED, f"Architektur {platform.machine()} nicht unterstützt")
        return s
    names, software_only = vulkan_devices()
    if software_only:
        s = Section("GPU Compute & VRAM (Vulkan)")
        s.add(UNSUPPORTED, "Nur Software-Rendering (llvmpipe) - kein Vulkan-Treiber für die GPU",
              "Treiber installieren: " + VULKAN_DRIVER_HINT.get(detect().family, "Vulkan-Treiber der GPU"))
        return s
    exe, note = resolve_tool(allow_download, log)
    if not exe:
        s = Section("GPU Compute & VRAM (Vulkan)")
        s.add(UNSUPPORTED, "memtest_vulkan nicht verfügbar", note)
        return s
    lines: List[str] = []
    p = subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    threading.Thread(target=lambda: [lines.append(l) for l in p.stdout], daemon=True).start()
    end = time.time() + seconds
    while time.time() < end and not stop_evt.is_set() and p.poll() is None:
        time.sleep(0.5)
    early_exit = p.poll() is not None
    _stop_group(p)
    time.sleep(0.3)
    s = evaluate_output("".join(lines), not early_exit)
    s.data = {"devices": names, "tool": note}
    if stop_evt.is_set():
        s.add(INTERRUPTED, "Test vorzeitig beendet")
    dev = "; ".join(names) or "unbekannt (vulkaninfo fehlt)"
    header = f"Vulkan-Geraete: {dev}\nWerkzeug: {note}\n(memtest_vulkan waehlt das Standardgeraet)\n\n"
    s.raw = header + "".join(lines)[-6000:]
    return s
