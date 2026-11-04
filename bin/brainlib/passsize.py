"""The size of a consolidation pass (engine 0.8.0): choose its batch, measure its change set.

One job, and pure: no git, no files, no model. `bin/brain` reads the inbox and the staged
change set and hands them here as data; this module decides what fits the auditor's input.

Why the engine and not the prompt: until 0.7.0 the caps were prompt text alone. On
2026-11-04 the proposer went past them, the audit was handed a truncated diff, and the whole
paid pass ($6.82) was thrown away; every later inbox was bigger, so the passes could never
catch up. The limit is the auditor's input, which is the engine's own, so the engine chooses
the batch, measures the result, and refuses to pay for an audit whose answer is known.

Contract (versioned with the engine):
- `select_batch(items, max_items)` -> Batch. `items` are inbox notes as dicts:
  {"path", "size", "kind", "source", "targets": [{"name", "size", "canonical"}]}. Corrections
  come first, oldest first by path (the capture instant), each with its targets, while the
  budget holds; the rest are deferred, and no note a deferred correction names is taken. Then
  notes fill the budget, oldest first. Session-mining proposals are never in a batch. At least
  one item is taken whenever there is one to take.
- `cut(batch, n)` -> the first n items of the same ordering, as a Batch.
- `retry_size(n, files, chars)` -> the smaller batch for a second attempt.
- `measure(rows, diff)` -> {"canonical_files", "diff_chars"}; `over_caps(measured)` -> bool.
- `parse_name_status_z(raw)` reads `git diff --name-status -z -M` output into rows.
"""

import math

CANONICAL_FILES_CAP = 30
DIFF_CHARS_CAP = 200_000

BUDGET_CHARS = DIFF_CHARS_CAP // 2          # half the hard cap
BUDGET_FILES = 20                            # two thirds of 30
MAX_ITEMS = 15
MIN_ITEM_CHARS = 7_000                       # 11-04: 5,000+ diff chars a note, rounded up
CHARS_PER_CHAR = 4                           # the deletion, the promoted body, its Sources
FILES_PER_NOTE = 0.85                        # 10-21: 0.83 canonical files a note
FILES_PER_CANONICAL_TARGET = 2               # a supersede: the successor and the archived note

MEASURED_FOLDERS = ("decisions", "topics", "projects", "people", "life", "reference", "archive")


class Batch:
    """What one attempt is asked to drain. `corrections` and `notes` are inbox paths in the
    order the proposer is given them; `allowed` is every inbox path the attempt may remove
    (the batch, and the inbox notes its corrections name); `deferred` are the corrections
    left for a later pass."""

    def __init__(self, corrections, notes, deferred, allowed, chars, files, by_path):
        self.by_path = by_path
        self.corrections = list(corrections)
        self.notes = list(notes)
        self.deferred = list(deferred)
        self.allowed = set(allowed)
        self.chars = chars
        self.files = files

    @property
    def items(self):
        return self.corrections + self.notes

    def __len__(self):
        return len(self.corrections) + len(self.notes)


def _cost(size):
    return max(MIN_ITEM_CHARS, CHARS_PER_CHAR * max(0, int(size)))


def _correction_cost(item):
    targets = item.get("targets") or []
    chars = _cost(int(item.get("size") or 0) + sum(int(t.get("size") or 0) for t in targets))
    files = FILES_PER_CANONICAL_TARGET * sum(1 for t in targets if t.get("canonical"))
    return chars, files


def _inbox_targets(item):
    return [t["name"] for t in item.get("targets") or []
            if str(t.get("name") or "").startswith("knowledge/inbox/")]


def select_batch(items, max_items=MAX_ITEMS, budget_chars=BUDGET_CHARS,
                 budget_files=BUDGET_FILES):
    ordered = sorted(items, key=lambda i: i["path"])
    corrections = [i for i in ordered if i.get("kind") == "correction"]
    proposals = {i["path"] for i in ordered
                 if i.get("kind") != "correction" and i.get("source") == "session-mining"}
    chars = files = 0.0
    taken, deferred, allowed = [], [], set()

    def fits(c, f):
        if len(taken) >= max_items:
            return False
        if not taken:
            return True                          # at least one item, whatever it costs
        return chars + c <= budget_chars and files + f <= budget_files

    stopped = False
    for item in corrections:
        c, f = _correction_cost(item)
        if stopped or not fits(c, f):
            stopped = True
            deferred.append(item)
            continue
        taken.append(item["path"])
        allowed.add(item["path"])
        allowed.update(_inbox_targets(item))
        chars, files = chars + c, files + f
    held = {t for item in deferred for t in _inbox_targets(item)}
    batch_corrections = list(taken)
    notes = []
    for item in ordered:
        path = item["path"]
        if (item.get("kind") == "correction" or path in proposals or path in held
                or path in allowed):
            continue
        c, f = _cost(item.get("size") or 0), FILES_PER_NOTE
        if not fits(c, f):
            break
        taken.append(path)
        notes.append(path)
        allowed.add(path)
        chars, files = chars + c, files + f
    return Batch(batch_corrections, notes, [i["path"] for i in deferred], allowed,
                 int(chars), round(files, 2), {i["path"]: i for i in ordered})


def cut(batch, n):
    """The first `n` items of `batch`, corrections first: every correction cut away is
    deferred, and its inbox targets are no longer allowed."""
    by_path = batch.by_path
    keep = batch.items[:max(1, n)]
    corrections = [p for p in batch.corrections if p in keep]
    notes = [p for p in batch.notes if p in keep]
    deferred = [p for p in batch.corrections if p not in keep] + batch.deferred
    allowed, chars, files = set(), 0, 0.0
    for path in corrections:
        allowed.add(path)
        allowed.update(_inbox_targets(by_path[path]))
        c, f = _correction_cost(by_path[path])
        chars, files = chars + c, files + f
    held = {t for p in deferred for t in _inbox_targets(by_path[p])}
    for path in notes:
        allowed.add(path)
        chars, files = chars + _cost(by_path[path].get("size") or 0), files + FILES_PER_NOTE
    return Batch(corrections, notes, sorted(set(deferred)), allowed - held,
                 int(chars), round(files, 2), by_path)


def retry_size(n, files, chars):
    """The batch for a second attempt, from what the first measured. A ratio whose count is 0
    is no limit: a pass over the character cap only through inbox deletions and handoffs has
    no canonical file at all, and is sized by characters alone."""
    file_ratio = BUDGET_FILES / files if files > 0 else math.inf
    char_ratio = BUDGET_CHARS / chars if chars > 0 else math.inf
    ratio = min(file_ratio, char_ratio)
    scaled = n // 2 if math.isinf(ratio) else math.floor(n * ratio)
    return max(1, min(n // 2, scaled))


def parse_name_status_z(raw):
    """[(status, [paths])] from `git diff --name-status -z` with rename detection: a rename or
    copy row carries two paths, every other row one. None for a shape this cannot read."""
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    rows, i = [], 0
    while i < len(fields):
        status = fields[i].decode("ascii", "replace")
        width = 2 if status[:1] in ("R", "C") else 1
        paths = [f.decode("utf-8", "surrogateescape") for f in fields[i + 1:i + 1 + width]]
        if not status or len(paths) != width:
            return None
        rows.append((status, paths))
        i += 1 + width
    return rows


def _measured(path):
    parts = path.split("/")
    return len(parts) >= 3 and parts[0] == "knowledge" and parts[1] in MEASURED_FOLDERS


def measure(rows, diff):
    """`canonical_files`: distinct changes under the canonical folders and archive/ (a rename
    counts once, under either of its paths); inbox, handoff and journal paths do not count.
    `diff_chars`: the length of the exact text the auditor is handed."""
    files = sum(1 for _status, paths in rows if any(_measured(p) for p in paths))
    return {"canonical_files": files, "diff_chars": len(diff)}


def over_caps(measured):
    return (measured["canonical_files"] > CANONICAL_FILES_CAP
            or measured["diff_chars"] > DIFF_CHARS_CAP)


def caps_line(measured):
    return (f"{measured['canonical_files']} canonical files, {measured['diff_chars']} diff chars "
            f"(caps {CANONICAL_FILES_CAP} / {DIFF_CHARS_CAP})")
