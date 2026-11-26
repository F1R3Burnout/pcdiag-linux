"""JSON + HTML report writer."""
import html
import json
import os
import platform
import zipfile
from dataclasses import asdict
from typing import List

from . import __version__
from .common import ATTENTION, FAIL, INTERRUPTED, SKIPPED, Section, overall, timestamp

_COLORS = {"PASS": "#1a7f37", "ATTENTION": "#8a560f", "FAIL": "#c62828",
           "UNSUPPORTED": "#5f6b7a", "SKIPPED": "#5f6b7a", "INTERRUPTED": "#c62828"}
_PROBLEMS = {ATTENTION, FAIL, INTERRUPTED}


def _badge(s: str) -> str:
    return f'<span class="b" style="background:{_COLORS.get(s, "#555")}">{html.escape(s)}</span>'


def _findings(items) -> str:
    out = ["<ul>"]
    for f in items:
        out.append(f"<li>{_badge(f.status)} <strong>{html.escape(f.title)}</strong>")
        if f.detail:
            out.append(f'<div class="detail">{html.escape(f.detail)}</div>')
        if f.source:
            out.append(f'<small class="source">Quelle: {html.escape(f.source)}</small>')
        out.append("</li>")
    return "".join(out) + "</ul>"


def render_html(title: str, sections: List[Section], note: str = "") -> str:
    ov = overall(sections)
    out = [f'<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" '
           f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>',
           "<style>body{font:15px system-ui,sans-serif;max-width:1040px;margin:1.5em auto;padding:0 1em;"
           "color:#1c2430;overflow-wrap:anywhere}h1{margin:0 0 .25em;font-size:24px}h2{font-size:18px}"
           ".b{display:inline-block;color:#fff;border-radius:4px;padding:1px 7px;font-size:12px;font-weight:600}"
           ".section{border-top:1px solid #d6dbe1;padding:9px 0}.section:last-child{border-bottom:1px solid #d6dbe1}"
           "summary{cursor:pointer}.section>summary{font-weight:650;padding:4px 0}"
           "summary small{font-weight:400;margin-inline-start:.45rem}ul{padding-left:1.35rem;margin:8px 0}"
           "li{margin:7px 0}.detail,.source{display:block;margin-top:2px}.detail{white-space:pre-wrap}"
           "pre{background:#f4f6f8;padding:8px;overflow:auto;font-size:12px;max-height:280px}"
           "small{color:#5f6b7a}.extra{margin:8px 0}.extra pre{white-space:pre-wrap}</style>",
           f"<h1>{html.escape(title)}</h1><small>pcdiag-linux {__version__} · "
           f"{html.escape(platform.node())} · {html.escape(platform.platform())}</small>",
           f"<h2>Gesamtergebnis: {_badge(ov)}</h2>"]
    if note:
        out.append(f"<p>{html.escape(note)}</p>")
    out.append("<main>")
    for s in sections:
        problems = [f for f in s.findings if f.status in _PROBLEMS]
        remaining = [f for f in s.findings if f.status not in _PROBLEMS]
        opened = bool(problems) or s.expanded is True
        counts = ", ".join(f"{sum(f.status == status for f in s.findings)} {status}"
                           for status in _COLORS if any(f.status == status for f in s.findings))
        out.append(f'<details class="section"{" open" if opened else ""}>'
                   f'<summary>{html.escape(s.name)} {_badge(s.status)}'
                   + (f"<small>{counts}</small>" if counts else "") + "</summary>")
        if problems:
            out.append(_findings(problems))
        if remaining:
            if problems and not s.expanded:
                out.append(f'<details class="extra"><summary>Weitere Ergebnisse ({len(remaining)})</summary>'
                           f'{_findings(remaining)}</details>')
            else:
                performed = [f for f in remaining if f.status != SKIPPED]
                skipped = [f for f in remaining if f.status == SKIPPED]
                if performed and skipped:
                    out.append(_findings(performed))
                    out.append(f'<details class="extra"><summary>Übersprungen ({len(skipped)})</summary>'
                               f'{_findings(skipped)}</details>')
                else:
                    out.append(_findings(remaining))
        if s.raw:
            out.append(f'<details class="extra"><summary>Rohdaten</summary>'
                       f'<pre>{html.escape(s.raw[:20000])}</pre></details>')
        if s.data:
            raw = json.dumps(s.data, ensure_ascii=False, indent=2)
            out.append(f'<details class="extra"><summary>Mess- und Kontextdaten</summary>'
                       f'<pre>{html.escape(raw[:20000])}</pre></details>')
        out.append("</details>")
    out.append("</main></html>")
    return "\n".join(out)


def write_report(tool: str, sections: List[Section], outdir: str = "", note: str = "",
                 archive: bool = False) -> str:
    base = outdir or os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                                  "pcdiag", f"{tool}_{timestamp()}")
    os.makedirs(base, mode=0o700, exist_ok=True)
    names = ["report.html", "report.json"]
    with _create(base, names[0]) as f:
        f.write(render_html(f"pcdiag-linux · {tool}", sections, note))
    with _create(base, names[1]) as f:
        json.dump({"tool": tool, "version": __version__, "overall": overall(sections),
                   "sections": [asdict(s) for s in sections]}, f, indent=2, ensure_ascii=False)
    if archive:
        with _create(base, "report.zip", binary=True) as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                z.write(os.path.join(base, name), name)
        names.append("report.zip")
    _give_to_sudo_user(base, names)
    return base


def _create(base: str, name: str, binary: bool = False):
    fd = os.open(os.path.join(base, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return os.fdopen(fd, "wb" if binary else "w", encoding=None if binary else "utf-8")


def _give_to_sudo_user(path: str, names: List[str]) -> None:
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (uid and gid and hasattr(os, "chown")):
        return
    try:
        for n in [path] + [os.path.join(path, x) for x in names]:
            os.chown(n, int(uid), int(gid), follow_symlinks=False)
    except (OSError, ValueError):
        pass
