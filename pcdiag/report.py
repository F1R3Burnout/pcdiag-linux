"""JSON + HTML report writer."""
import html
import json
import os
import platform
from dataclasses import asdict
from typing import List

from . import __version__
from .common import Section, overall, timestamp

_COLORS = {"PASS": "#1a7f37", "ATTENTION": "#b7791f", "FAIL": "#c62828",
           "UNSUPPORTED": "#5f6b7a", "SKIPPED": "#5f6b7a", "INTERRUPTED": "#c62828"}


def _badge(s: str) -> str:
    return f'<span class="b" style="background:{_COLORS.get(s, "#555")}">{html.escape(s)}</span>'


def render_html(title: str, sections: List[Section], note: str = "") -> str:
    ov = overall(sections)
    out = [f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>",
           "<style>body{font:15px system-ui,sans-serif;max-width:960px;margin:2em auto;padding:0 1em;"
           "color:#1c2430}h1{margin-bottom:0}.b{color:#fff;border-radius:4px;padding:1px 8px;"
           "font-size:12px;font-weight:600}section{border:1px solid #d6dbe1;border-radius:8px;"
           "margin:14px 0;padding:8px 16px}li{margin:4px 0}pre{background:#f4f6f8;padding:8px;"
           "overflow:auto;font-size:12px;max-height:280px}small{color:#5f6b7a}</style>",
           f"<h1>{html.escape(title)}</h1><small>pcdiag-linux {__version__} · "
           f"{html.escape(platform.node())} · {html.escape(platform.platform())}</small>",
           f"<h2>Overall: {_badge(ov)}</h2>"]
    if note:
        out.append(f"<p>{html.escape(note)}</p>")
    for s in sections:
        out.append(f"<section><h3>{html.escape(s.name)} {_badge(s.status)}</h3><ul>")
        for f in s.findings:
            d = f" — <small>{html.escape(f.detail)}</small>" if f.detail else ""
            out.append(f"<li>{_badge(f.status)} {html.escape(f.title)}{d}</li>")
        out.append("</ul>")
        if s.raw:
            out.append(f"<details><summary>Rohdaten</summary><pre>{html.escape(s.raw[:20000])}</pre></details>")
        out.append("</section>")
    return "\n".join(out)


def write_report(tool: str, sections: List[Section], outdir: str = "", note: str = "") -> str:
    base = outdir or os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
                                  "pcdiag", f"{tool}_{timestamp()}")
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "report.html"), "w", encoding="utf-8") as f:
        f.write(render_html(f"pcdiag-linux · {tool}", sections, note))
    with open(os.path.join(base, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"tool": tool, "version": __version__, "overall": overall(sections),
                   "sections": [asdict(s) for s in sections]}, f, indent=2, ensure_ascii=False)
    return base
