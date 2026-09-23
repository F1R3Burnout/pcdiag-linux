"""Menu + command line entry point."""
import argparse
import sys
from typing import List

from . import __version__, displaydiag, distro, netdiag, report, stability, sysdiag
from .common import FAIL, PASS, Section, overall

TOOLS = {
    "sysdiag": ("Systemübersicht (CPU, RAM, Datenträger, Dienste, Kernel-Log)", None),
    "netdiag": ("Netzwerkdiagnose (Gateway, DNS, Link, WLAN)", None),
    "displaydiag": ("Display-/HDMI-Diagnose (DRM, EDID, Audio)", None),
    "stability": ("Hardware-Stabilitätstest (CPU, RAM, GPU/VRAM, Storage, Thermik, Kernel-Fehler)", None),
}


def _print(sections: List[Section]) -> None:
    for s in sections:
        print(f"\n[{s.status}] {s.name}")
        for f in s.findings:
            print(f"   [{f.status}] {f.title}" + (f" - {f.detail.splitlines()[0]}" if f.detail else ""))
    print(f"\nGesamtergebnis: {overall(sections)}")


def run_tool(name: str, a: argparse.Namespace) -> int:
    d = distro.detect()
    print(f"System: {d.label}")
    if not a.dry_run:
        distro.ensure_dependencies(name, assume_yes=a.yes, allow_install=not a.no_install)
    if name == "sysdiag":
        secs, note = sysdiag.collect(), ""
    elif name == "netdiag":
        secs, note = netdiag.collect(), ""
    elif name == "displaydiag":
        secs, note = displaydiag.collect(), ""
    else:
        comps = a.components.split(",") if a.components else None
        secs = stability.run_stability(a.profile, comps, a.workdir, a.dry_run, not a.no_download)
        note = ("PASS bedeutet: keine Fehler im getesteten Bereich - keine Garantie für gesunde Hardware. "
                "Ein SYSTEM_RESET oder Hardware-Fehler im Kernel-Log ist ein Indiz, kein Beweis für eine bestimmte Komponente.")
    _print(secs)
    if not a.dry_run:
        print("Report:", report.write_report(name, secs, a.output, note))
    return 1 if overall(secs) == FAIL else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pcdiag", description="PC-Diagnose für Linux")
    p.add_argument("tool", nargs="?", choices=list(TOOLS), help="ohne Angabe: interaktives Menü")
    p.add_argument("--profile", choices=list(stability.PROFILES), default="quick")
    p.add_argument("--components", help="stability: cpu,memory,gpu,storage,kernel")
    p.add_argument("--workdir", default="", help="stability: Verzeichnis für den Schreib-/Lesetest")
    p.add_argument("--output", default="", help="Zielordner für den Report")
    p.add_argument("--dry-run", action="store_true", help="stability: nur anzeigen, keine Last")
    p.add_argument("--yes", "-y", action="store_true", help="fehlende Pakete ohne Rückfrage installieren")
    p.add_argument("--no-install", action="store_true", help="keine Pakete installieren")
    p.add_argument("--no-download", action="store_true", help="keine Werkzeuge herunterladen (memtest_vulkan)")
    p.add_argument("--fix-xrdp-polkit", action="store_true",
                   help="PolicyKit-Regel anlegen, damit NetworkManager in xrdp-Sitzungen nicht ständig "
                        "nach dem Passwort fragt")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)
    if a.fix_xrdp_polkit:
        from . import remote_desktop
        return 0 if remote_desktop.apply_fix(assume_yes=a.yes) else 1
    if a.tool:
        return run_tool(a.tool, a)
    names = list(TOOLS)
    while True:
        print(f"\n=== PC-Diagnose Linux {__version__} ===")
        for i, n in enumerate(names, 1):
            print(f" {i}) {TOOLS[n][0]}")
        print(" q) Beenden")
        try:
            c = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if c in ("q", ""):
            return 0
        if c.isdigit() and 1 <= int(c) <= len(names):
            if names[int(c) - 1] == "stability":
                a.profile = input("Profil [quick/standard/extended] (quick): ").strip() or "quick"
                if a.profile not in stability.PROFILES:
                    print("Unbekanntes Profil"); continue
            run_tool(names[int(c) - 1], a)


if __name__ == "__main__":
    sys.exit(main())
