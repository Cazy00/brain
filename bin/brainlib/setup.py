# bin/brainlib/setup.py
"""First-run setup: the phases, and the contract an agent reads.

One implementation serves a human at a terminal and an agent running headless.
Two implementations would mean the agent path rots silently, because nobody
exercises it daily — and most people will hand this whole thing to an agent.
"""
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import osbackend
from . import picker

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


def _git_out(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def phase_backup(dest, repo_name: str, want_remote: bool,
                 run=None, which=None) -> Result:
    """Set up the private remote, and report what is ACTUALLY true afterwards.

    On 2026-07-25 this was read from `gh`'s exit code. `gh repo create
    --source . --remote origin --push` adds the remote BEFORE it pushes, so a
    push failure returns non-zero with a remote sitting right there. The
    installer announced 'no remote yet — LOCAL ONLY', its own visibility check
    then read git and said the opposite, and the run ended with doctor RED.

    Nothing below trusts an exit code for the question 'is there a remote'.
    Git is asked, every time.
    """
    dest = Path(dest)
    run = run or (lambda argv, **kw: subprocess.run(
        argv, cwd=str(dest), capture_output=True, text=True))
    which = which or shutil.which

    origin = _git_out(dest, "remote", "get-url", "origin")

    if want_remote and not origin and which("gh"):
        run(["gh", "repo", "create", repo_name, "--private",
             "--source", ".", "--remote", "origin", "--push"])
        # Ask git, not gh. This line is the fix.
        origin = _git_out(dest, "remote", "get-url", "origin")

    if not origin:
        return Result(
            "skipped",
            "no remote — your notes exist on this machine only and are not backed up",
            remedy=f"gh repo create {repo_name} --private --source . --push")

    upstream = _git_out(dest, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{u}")
    if not upstream:
        branch = _git_out(dest, "rev-parse", "--abbrev-ref", "HEAD") or "main"
        pushed = subprocess.run(["git", "push", "-u", "origin", branch],
                                cwd=str(dest), capture_output=True, text=True)
        if pushed.returncode != 0:
            lines = (pushed.stderr or "").strip().splitlines()
            reason = lines[-1] if lines else "git push failed"
            return Result(
                "failed",
                f"the remote {origin} exists but nothing has been pushed to it, so "
                f"nothing is backed up: {reason}",
                remedy=f"cd {dest} && git push -u origin {branch}")

    return Result("ok", f"backed up to {origin}")


def phase_place(home, cwd, requested=None, stream=None) -> tuple:
    """Decide where the brain goes. Returns (Result, path).

    The only phase that returns a pair, because the destination it settles on
    is what every phase after it operates on and there is nowhere else for that
    answer to come from.
    """
    home, cwd = Path(home), Path(cwd)
    if requested:
        chosen = picker.expand(str(requested), home, cwd)
        reason = picker.reject_reason(chosen)
        if reason:
            return Result("failed", reason,
                          remedy="choose a different path with --dir <path>"), chosen
        return Result("ok", f"installing to {chosen}"), chosen
    chosen = picker.choose(home, cwd, stream=stream)
    return Result("ok", f"installing to {chosen}"), chosen


def phase_create(source, dest) -> Result:
    """Copy the template and give it a git history that belongs to its owner.

    The template's history is the PRODUCT's history, not yours. Starting fresh
    is also what GitHub's 'Use this template' button does, and nothing is lost:
    toolbelt updates come across by adding the template as a second remote and
    checking out paths, which needs no shared ancestry.
    """
    source, dest = Path(source), Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            # Distribution scaffolding, not knowledge. install.sh in particular
            # must not survive the copy: it carries the TEMPLATE repo's name,
            # so running it from inside a brain would reinstall the product
            # over the notes.
            if item.name in {".git", ".cache", "__pycache__", "install.sh",
                             "install.ps1", ".claude-plugin", "plugins"}:
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    except OSError as exc:
        return Result("failed", f"could not copy the template: {exc}",
                      remedy=f"check that {dest} is writable, then run setup again")

    made = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(dest),
                          capture_output=True, text=True)
    if made.returncode != 0:
        # git < 2.28 has no -b. Fall back rather than demanding an upgrade.
        subprocess.run(["git", "init", "-q"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                       cwd=str(dest), capture_output=True)

    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=str(dest), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dest), capture_output=True)
    # The gate has nothing to check on an empty history and its own tooling is
    # not wired yet, so this one commit bypasses it deliberately.
    first = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit",
                            "-q", "-m", "brain: start"],
                           cwd=str(dest), capture_output=True, text=True)
    if first.returncode != 0:
        return Result("failed",
                      "could not make the first commit — git needs a name and email",
                      remedy='git config --global user.name "You" && '
                             'git config --global user.email "you@example.com"')
    return Result("ok", f"created {dest} with a fresh history on main")


def phase_verify(dest, run=None) -> Result:
    """Run doctor and let its verdict be setup's verdict.

    A fresh install that finishes in a state its own health check calls red is
    the exact failure this whole plan exists to remove, so doctor's result is
    not advisory here.

    Both the exit code and the output are read, and the worse of the two wins.
    An exit code is a summary somebody has to remember to keep true; the RED
    lines are the finding itself. Trusting only the summary is the same shape
    of mistake phase_backup exists to undo.
    """
    dest = Path(dest)
    run = run or (lambda argv: subprocess.run(argv, cwd=str(dest),
                                              capture_output=True, text=True))
    done = run([sys.executable, str(dest / "bin" / "brain"), "doctor"])
    output = (getattr(done, "stdout", "") or "") + (getattr(done, "stderr", "") or "")
    if getattr(done, "returncode", 1) != 0 or "[RED]" in output:
        return Result("failed", "doctor reported a problem:\n" + output.strip(),
                      remedy=f"cd {dest} && bin/brain doctor")
    return Result("ok", "doctor is green")


USAGE = """brain setup — install a brain here and prove it works.

  brain setup [--dir <path>] [--repo <name>] [--no-repo]
              [--yes] [--json] [--only <phase>]

  --dir <path>    where the brain lives; skips the picker
  --repo <name>   name for the private GitHub repo  (default: my-brain)
  --no-repo       do not create a remote at all — LOCAL ONLY, no backup
  --yes, -y       never ask; take every default
  --json          machine-readable result on stdout, human text on stderr
  --only <phase>  re-run one phase: {phases}
                  create/backup/verify also need --dir

Phases run in that order and each one is safe to re-run. The exit code is
doctor's, so an install this reports as done is one doctor calls healthy.
""".format(phases=" | ".join(PHASES))


def _flag_value(argv: list, flag: str):
    """The argument after `flag`: None when absent, "" when it has no value.

    A bare trailing `--only` is a typo, and argv[i + 1] would answer it with an
    IndexError traceback where a one-line "needs a value" belongs.
    """
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else ""


def run_setup(argv: list, home=None, cwd=None, source=None) -> int:
    """The whole first run. Interactive with a terminal, silent without one.

    Human text goes to stderr and machine output to stdout, so `--json` can be
    parsed by an agent without stripping anything first.
    """
    home = Path(home) if home else Path.home()
    cwd = Path(cwd) if cwd else Path.cwd()
    # What this brain is copied FROM: the checkout bin/brain is running out of,
    # which is install.sh's temp clone when it hands off, and this repo when a
    # developer runs it by hand.
    source = Path(source) if source else Path(__file__).resolve().parent.parent.parent

    if "--help" in argv or "-h" in argv:
        # Reached only when help arrives behind another flag; `brain setup
        # --help` is caught by main()'s own guard first. Either way the rule is
        # the one `init --help` broke in 2026-07-25: asking what a command does
        # must never do it.
        print(USAGE)
        return 0

    as_json = "--json" in argv
    # --yes and --json both mean do not ask. An agent parsing stdout has no way
    # to answer a prompt, so a --json run that stopped at one would block
    # against a terminal nobody is watching.
    ask = not (as_json or "--yes" in argv or "-y" in argv)

    for flag in ("--only", "--dir", "--repo"):
        if _flag_value(argv, flag) == "":
            print(f"{flag} needs a value", file=sys.stderr)
            return 2

    only = _flag_value(argv, "--only")
    requested = _flag_value(argv, "--dir")
    repo_name = _flag_value(argv, "--repo") or "my-brain"
    want_remote = "--no-repo" not in argv

    if only and only not in PHASES:
        print(f"unknown phase {only!r} — one of: {', '.join(PHASES)}", file=sys.stderr)
        return 2
    # Every phase after `place` needs a destination. Running one of them alone
    # is a legitimate repair, so ask where the brain is rather than crashing on
    # a None that came from a phase that was never run.
    if only in ("create", "backup", "verify") and not requested:
        print(f"--only {only} needs --dir <path>", file=sys.stderr)
        return 2

    def say(text=""):
        if not as_json:
            print(text, file=sys.stderr)

    results, dest = {}, None
    if requested:
        dest = picker.expand(str(requested), home, cwd)

    wanted = (only,) if only else PHASES
    for name in PHASES:
        if name not in wanted:
            continue
        if name == "check":
            results[name] = phase_check()
        elif name == "place":
            # A stream that is not a terminal is exactly what picker.choose()
            # already reads as "take the default", so --yes costs one argument
            # rather than a second, less-exercised non-interactive branch that
            # would have to be kept in step with the interactive one.
            results[name], dest = phase_place(
                home, cwd, requested=requested,
                stream=None if ask else io.StringIO(""))
        elif name == "create":
            results[name] = phase_create(source, dest)
        elif name == "backup":
            results[name] = phase_backup(dest, repo_name, want_remote)
        elif name == "verify":
            results[name] = phase_verify(dest)

        result = results[name]
        say(f"  [{result.status:<7}] {name}: {result.detail}")
        if result.status == "failed":
            say(f"            fix: {result.remedy}")
            break

    def status_of(name: str) -> str:
        result = results.get(name)
        return result.status if result else ""

    if as_json:
        print(render_json(results))
    elif (dest and not only and not want_remote
          and status_of("backup") == "skipped" and status_of("verify") == "failed"):
        # The verdict stays, and so does the word "failed". A brain that
        # exists on one disk really is unhealthy — AGENTS.md calls a backup gap
        # "worth seeing, not an untidiness to hide", and the scheduled watchdog
        # only fires on a non-zero exit, so softening this would take the one
        # alarm that matters offline. What was missing is the sentence
        # connecting the verdict to the flag that caused it. Guarded to exactly
        # that case: a red line nobody asked for is a surprise, and explaining
        # a surprise away as expected is the worst thing this output could do.
        say(f"\n  The brain itself is built and working, at {dest}.")
        say("\n  What is not done is the backup. You passed --no-repo, so there is no "
            "remote,\n  and doctor calls a brain that exists on one disk only RED "
            "rather than healthy.\n  That is why this exits non-zero: the state is "
            "exactly the one you asked for, and\n  it is still not a state this "
            "system will call finished.")
        say("\n  When you want the backup:")
        say(f"    cd {dest} && gh repo create {repo_name} --private --source . --push")
        say(f"    brain setup --only verify --dir {dest}")
    elif dest and not only and overall_status(results) == "ok":
        # "and it is backed up" is safe to say unconditionally here only
        # because verify ran and passed: doctor calls a remote-less brain RED,
        # so this line is unreachable unless there really is a remote.
        say(f"\n  Your brain is at {dest}. It works and it is backed up.")
        say("\n  Next: let your agents reach it —  brain connect")
        say("\n  Only using this on this computer? That is everything.")
        say("  Reaching it from other devices is a separate, optional step: "
            "brain serve --help")

    return 0 if overall_status(results) == "ok" else 1
