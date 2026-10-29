"""A small synthetic corpus and the plumbing the memory-verb tests share.

Everything here is invented: the notes describe imaginary widgets, never a real
brain. The corpus is built in a temporary directory as a split-layout data root
(notes and git, no engine) and is the ONLY thing the verb, golden and
conformance tests ever write to or read from.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE / "bin"))

TOPICS = ("widgets: widget, gadget\n"
          "retrieval: search, indexing\n")

# (relative path under knowledge/, text, mtime). The mtimes are fixed so the
# delta walk and `brain recent` order the same way on every run.
NOTES = (
    ("decisions/2026-03-01-choose-widget-engine.md",
     "---\nid: 2026-03-01-choose-widget-engine\nkind: decision\n"
     "title: Choose the widget engine\ntopics: [widgets]\n"
     "aliases: [widget engine choice, engine pick]\ncreated: 2026-03-01\n"
     "status: current\n---\n\nWe chose the sprocket widget engine because it indexes quickly.\n"
     "The rejected alternative was the cog engine.\n", 1_775_000_000),
    ("reference/widget-glossary.md",
     "---\nid: widget-glossary\nkind: reference\ntitle: Widget glossary\n"
     "topics: [widgets]\naliases: [widget terms]\ncreated: 2026-03-02\n"
     "status: current\nreview_by: 2026-04-01\n---\n\n"
     "A sprocket is a toothed wheel. A cog is a smaller toothed wheel.\n", 1_775_000_100),
    ("projects/gadget/overview.md",
     "---\nid: gadget-overview\nkind: project\ntitle: Gadget overview\n"
     "topics: [widgets]\naliases: [gadget project]\ncreated: 2026-03-03\n"
     "status: current\n---\n\nThe gadget project ships widgets. See [[widget-glossary]] "
     "and [[2026-03-01-choose-widget-engine]].\n", 1_775_000_200),
    ("topics/widgets.md",
     "---\nid: widgets-hub\nkind: topic\ntitle: Widgets hub\ntopics: [widgets]\n"
     "aliases: [widget hub]\ncreated: 2026-03-04\nstatus: current\n---\n\n"
     "Hub for [[gadget-overview]] and [[widget-glossary]].\n", 1_775_000_300),
    ("journal/2026/2026-03-05.md",
     "---\ntitle: Journal 2026-03-05\ncreated: 2026-03-05\n---\n\n"
     "Tried the sprocket engine on the bench today.\n", 1_775_000_400),
    ("inbox/2026-03-06-090000-widget-idea.md",
     "---\ncreated: 2026-03-06\ncaptured_at: 2026-03-06T09:00:00Z\nsource: local\n"
     "kind: idea\nbasis: owner-said\nstatus: draft\n---\n\n"
     "Try a gear-shaped widget handle for the sprocket line.\n", 1_775_000_500),
    ("inbox/2026-03-07-090000-glossary-fix.md",
     "---\ncreated: 2026-03-07\ncaptured_at: 2026-03-07T09:00:00Z\nsource: local\n"
     "kind: correction\nbasis: owner-said\nstatus: draft\n---\n\n"
     "Correction to widget-glossary: a cog is a smaller wheel with fewer teeth.\n",
     1_775_000_600),
    ("handoff/gadget/2026-03-08-090000-file-this.md",
     "---\ncreated: 2026-03-08\ncaptured_at: 2026-03-08T09:00:00Z\nsource: local\n"
     "kind: fact\nstatus: draft\nhandoff_to: gadget\nhandoff_at: 2026-03-09\n"
     "handoff_reason: belongs in the gadget backlog\n---\n\n"
     "The gadget backlog needs a sprocket supplier.\n", 1_775_000_700),
    ("archive/decisions/2026-02-01-old-engine.md",
     "---\nid: 2026-02-01-old-engine\nkind: decision\ntitle: Old engine\n"
     "topics: [widgets]\naliases: [old engine]\ncreated: 2026-02-01\n"
     "status: superseded\nsuperseded_by: 2026-03-01-choose-widget-engine\n---\n\n"
     "> SUPERSEDED 2026-03-01 by [[2026-03-01-choose-widget-engine]] — better index speed\n\n"
     "We used the cog engine.\n", 1_775_000_800),
)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def make_corpus(parent) -> Path:
    """A data root with the fixture notes committed on `main`."""
    root = Path(parent) / "data"
    (root / "knowledge").mkdir(parents=True)
    for sub in ("inbox", "journal", "decisions", "reference", "projects", "topics",
                "people", "life", "handoff", "archive"):
        (root / "knowledge" / sub).mkdir(exist_ok=True)
    (root / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    (root / "knowledge" / "index.md").write_text("# index\n\nRoute map.\n", encoding="utf-8")
    (root / "knowledge" / "topics.yaml").write_text(TOPICS, encoding="utf-8")
    for rel, text, mtime in NOTES:
        path = root / "knowledge" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (mtime, mtime))
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "sandbox@example.invalid"],
                 ["config", "user.name", "Sandbox"],
                 ["config", "gc.auto", "0"],
                 # What `brain` would set itself on its first command; set here so the
                 # "installed git hooks" notice never leads a command's output.
                 ["config", "core.hooksPath", str(ENGINE / ".githooks")],
                 ["add", "-A"],
                 ["commit", "-q", "-m", "fixture corpus"]):
        git(root, *args)
    # The commit above must not move the mtimes the walk is ordered by.
    for rel, _text, mtime in NOTES:
        os.utime(root / "knowledge" / rel, (mtime, mtime))
    return root


class Corpus(object):
    """A fixture corpus bound into the environment for the life of the object."""

    def __init__(self):
        try:
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:                  # Python 3.9 has no such argument
            self.tmp = tempfile.TemporaryDirectory()
        self.root = make_corpus(self.tmp.name)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self._saved = {k: os.environ.get(k) for k in ("BRAIN_DATA_ROOT", "BRAIN_STATE_DIR")}
        os.environ["BRAIN_DATA_ROOT"] = str(self.root)
        os.environ["BRAIN_STATE_DIR"] = str(self.state)

    def close(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self.tmp.cleanup()
        except OSError:
            pass
