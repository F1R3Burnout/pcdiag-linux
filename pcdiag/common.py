"""Shared helpers: command execution, findings, report model. Stdlib only."""
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

PASS, ATTENTION, FAIL, UNSUPPORTED, SKIPPED, INTERRUPTED = (
    "PASS", "ATTENTION", "FAIL", "UNSUPPORTED", "SKIPPED", "INTERRUPTED")
_RANK = {FAIL: 5, INTERRUPTED: 4, ATTENTION: 3, PASS: 2, UNSUPPORTED: 1, SKIPPED: 0}


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd, timeout: int = 20, *, stdout_only: bool = False) -> "tuple[int, str]":
    """Run a command, preserving stdout separately on successful machine-readable queries."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, errors="replace",
                           env={**os.environ, "LC_ALL": "C"})
        if stdout_only:
            return p.returncode, p.stdout or p.stderr or ""
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"
    except Exception as e:  # pragma: no cover
        return 1, f"{cmd[0]}: {e}"


def read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


@dataclass
class Finding:
    status: str
    title: str
    detail: str = ""
    source: str = ""


@dataclass
class Section:
    name: str
    status: str = PASS
    findings: List[Finding] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    raw: str = ""
    expanded: Optional[bool] = None

    def add(self, status: str, title: str, detail: str = "", source: str = "") -> None:
        self.findings.append(Finding(status, title, detail, source))
        sts = {f.status for f in self.findings}
        worst = [x for x in (FAIL, INTERRUPTED, ATTENTION) if x in sts]
        if worst:
            self.status = worst[0]
        elif PASS in sts:
            self.status = PASS
        elif UNSUPPORTED in sts:
            self.status = UNSUPPORTED
        else:
            self.status = SKIPPED


def overall(sections: List[Section]) -> str:
    ranked = [s.status for s in sections if s.status not in (SKIPPED, UNSUPPORTED)]
    if not ranked:
        return UNSUPPORTED
    return max(ranked, key=lambda s: _RANK[s])


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")
