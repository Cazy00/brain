# bin/brainlib/setup.py
"""First-run setup: the phases, and the contract an agent reads.

One implementation serves a human at a terminal and an agent running headless.
Two implementations would mean the agent path rots silently, because nobody
exercises it daily — and most people will hand this whole thing to an agent.
"""
import json
import shutil

from . import osbackend

PHASES = ("check", "place", "create", "backup", "verify")

_STATUSES = ("ok", "skipped", "failed")


class Result:
    """One phase's outcome.

    `remedy` is mandatory on failure and must be a literal command wherever one
    exists. It is the field an agent acts on, so a failure without it leaves
    the agent with nothing to do and the user with nothing to read.
    """

    def __init__(self, status: str, detail: str, remedy: str = ""):
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        # .strip(): a remedy of "   " is exactly as unactionable as "" — an
        # agent cannot run whitespace, so it must not slip past this guard.
        if status == "failed" and not (remedy or "").strip():
            raise ValueError("a failed Result must carry a remedy")
        self.status = status
        self.detail = detail
        self.remedy = remedy

    def as_dict(self) -> dict:
        # str(): this is the reporting boundary, the one place this module
        # cannot afford to raise. A phase that builds Result("failed", exc,
        # ...) and forgets str(exc) must still produce readable JSON instead
        # of an uncaught TypeError from json.dumps() at report time.
        return {"status": self.status, "detail": str(self.detail),
                "remedy": str(self.remedy)}


def _reject_unknown_phases(results: dict) -> None:
    """A key outside PHASES is a typo or a phase we forgot to register in
    PHASES — a bug in OUR code, not a legitimate partial result (`--only
    check` is legitimate and produces FEWER keys than PHASES, never an EXTRA
    one). Raise loudly at the mistake instead of letting it silently vanish
    from render_json()'s "phases" object while still flipping the top-level
    "status" to failed — an unexplained, unactionable "failed" is the worst
    output this contract can hand an agent.
    """
    unknown = sorted(set(results) - set(PHASES))
    if unknown:
        raise ValueError(f"unrecognized phase(s) {unknown}, must be one of {PHASES}")


def overall_status(results: dict) -> str:
    """'failed' if anything failed, else 'ok'.

    A skip is not a failure: no remote and no optional tools are legitimate,
    fully working outcomes and must not be reported as a broken install.
    """
    _reject_unknown_phases(results)
    return "failed" if any(r.status == "failed" for r in results.values()) else "ok"


def render_json(results: dict) -> str:
    _reject_unknown_phases(results)
    return json.dumps({
        "status": overall_status(results),
        "phases": {name: results[name].as_dict()
                   for name in PHASES if name in results},
    }, indent=2)


def phase_check(which=None, run=None) -> Result:
    """Report what is missing and what it costs. Install NOTHING.

    `run` is accepted only so a test can prove it is never called: a
    piped-curl script that installs system packages unprompted assumes more
    trust than this should, and corporate machines forbid it outright.
    """
    which = which or shutil.which
    missing_hard, missing_soft = [], []
    for tool, spec in osbackend.PREREQS.items():
        # Skipped, never checked via which(): this process EXECUTING is
        # already stronger proof of a working Python 3.9+ than a PATH lookup
        # could add — and on this repo's own Windows entry point, checking
        # would be actively WRONG, not merely redundant. brain.cmd invokes
        # `python "%~dp0bin\brain" %*`, not `python3`, and the stock
        # python.org Windows installer never puts a `python3.exe` on PATH.
        # Un-skipped, this would report a false "missing hard dependency" on
        # a Windows machine that is unmistakably running Python 3.9+ right
        # now, via the very interpreter executing this line.
        if tool == "python3":
            continue
        if which(tool):
            continue
        (missing_hard if spec["hard"] else missing_soft).append(tool)

    if missing_hard:
        lines = []
        for tool in missing_hard:
            hint = osbackend.install_hint(tool)
            lines.append(f"{tool} — {osbackend.PREREQS[tool]['why']}"
                         + (f"\n    install it with:  {hint}" if hint else ""))
        # Every missing hard tool must appear in `remedy`, not just the ones
        # install_hint() can resolve — an agent acts on `remedy` alone (see
        # Result's docstring), and a tool silently dropped here because it
        # merely lacks a known package name looks, to that agent, like a
        # problem it already fully addressed.
        remedy = "; ".join(osbackend.install_hint(t) or f"install: {t}"
                           for t in missing_hard)
        return Result("failed", "missing required tool(s):\n  " + "\n  ".join(lines),
                      remedy=remedy)

    if missing_soft:
        lines = []
        for tool in missing_soft:
            hint = osbackend.install_hint(tool)
            lines.append(f"{tool} not installed — {osbackend.PREREQS[tool]['why']}"
                         + (f"\n    add it later with:  {hint}" if hint else ""))
        return Result("ok", "everything required is present.\n  "
                      + "\n  ".join(lines))

    return Result("ok", "every prerequisite is present")
