"""The size of a consolidation pass (engine 0.8.1): choose its batch, measure its change set.

One job, and pure: no git, no files, no model. `bin/brain` reads the inbox and the staged
change set and hands them here as data; this module decides what fits the auditor's input.

Why the engine and not the prompt: until 0.7.0 the caps were prompt text alone. On
2026-11-04 the proposer went past them, the audit was handed a truncated diff, and the whole
paid pass ($6.82) was thrown away; every later inbox was bigger, so the passes could never
catch up. The limit is the auditor's input, which is the engine's own, so the engine chooses
the batch, measures the result, and refuses to pay for an audit whose answer is known.

Contract (versioned with the engine):
- `select_batch(items, max_items, set_aside=())` -> Batch. `items` are inbox notes as dicts:
  {"path", "size", "kind", "source", "basis", "retraction", "forget",
   "targets": [{"name", "size", "canonical"}]}.
  Corrections the pass can act on (`trusted`: source local, and basis owner-said, or
  agent-found for a correction that is not a retraction, or a `forget` retraction) come first, then the others, each group oldest
  first by path (the capture instant). Corrections that name the same inbox note are taken
  or deferred together. While notes are waiting, corrections take at most CORRECTION_SHARE
  of the budget, and at least one note is taken, so a correction no pass can drain never
  starves the owner's notes. An untrusted correction is costed by its own size (it changes
  no target) and holds back only what a local one holds. The rest are deferred, and no note
  a deferred local correction names is taken or allowed. Then notes fill the budget, oldest
  first. Session-mining proposals are never in a batch. An item whose own size alone is
  over DIFF_CHARS_CAP (draining it is a diff that size), and every path in `set_aside`, is
  set aside: in `Batch.set_aside`, never in the batch. At least one item is taken whenever
  there is one to take. `Batch.named` (0.8.1) lists the inbox notes the batch's corrections
  name that the pass drains with them: in `allowed`, costed with their correction, and listed
  to the proposer; a target's `name` is the note's inbox path, whichever way the correction
  wrote it (bin/brain resolves an `inbox-<stem>` id and drops a name that is no inbox file).
  A correction the pass could apply that names a set-aside note is deferred with its group
  (0.8.1): the note stays set aside, never in `named` or `allowed`, and the correction waits
  for it. Draining the note is the change set no audited pass carries (it is alone over the
  diff cap, or went over the caps alone), so taking it with its correction would put the
  pass over the caps too; deferred, the correction still holds the note back, and both come
  back together once the note leaves set-aside.
- `cut(batch, n)` -> the first n items, corrections first but at least one note when the
  batch has one and n > 1; a correction cut away is deferred with the corrections that
  share an inbox note with it.
- `retry_size(n, files, chars)` -> the smaller batch for a second attempt.
- `measure(rows, diff)` -> {"canonical_files", "diff_chars"}; `over_caps(measured)` -> bool.
- `parse_name_status_z(raw)` reads `git diff --name-status -z -M` output into rows.
"""

import math

CANONICAL_FILES_CAP = 30
DIFF_CHARS_CAP = 200_000

BUDGET_CHARS = DIFF_CHARS_CAP // 2          # half the hard cap
BUDGET_FILES = 20                            # two thirds of 30
MAX_ITEMS = 25                               # I-148: 15 left the inbox growing (54, 47, 51, 71)
MIN_ITEM_CHARS = 4_000                       # I-148: passes measured 3,400-4,900 diff chars a note
# With these, plain notes stop at BUDGET_FILES / FILES_PER_NOTE = 23 (the file estimate binds
# before the 25 cap and before the 25 the character floor allows). Two worst cases, each
# argued against its own cap of the audit:
#   characters: 23 notes costed at the floor = 92,000 estimated chars; the audit takes
#     200,000, so a note may really change 8,695 chars (1.8x the largest measured) before the
#     batch is over the cap.
#   files: 23 notes at 0.85 = 19.55 estimated files; the cap is 30, so a note may really touch
#     1.30 canonical files (1.53x the estimate, 1.57x the measured 0.83) before the batch is
#     over it. This is the tighter margin, and the one tests/test_passsize.py pins (1.5x). An
#     inbox note drained with its correction counts FILES_PER_NOTE too, so corrections cannot
#     widen the batch past the same estimate.
# Over a cap anyway, the engine measures, discards and reruns smaller (retry_size).
CHARS_PER_CHAR = 4                           # the deletion, the promoted body, its Sources
FILES_PER_NOTE = 0.85                        # 10-21: 0.83 canonical files a note
FILES_PER_CANONICAL_TARGET = 2               # a supersede: the successor and the archived note
CORRECTION_SHARE = 0.5                       # of the budget, while notes are waiting

MEASURED_FOLDERS = ("decisions", "topics", "projects", "people", "life", "reference", "archive")


class Batch:
    """What one attempt is asked to drain. `corrections` and `notes` are inbox paths in the
    order the proposer is given them; `allowed` is every inbox path the attempt may remove
    (the batch, and the inbox notes its corrections name, never one a deferred correction
    names); `deferred` are the corrections left for a later pass, `set_aside` the items no
    audited pass can drain, with their reason; `named` the inbox notes the corrections name,
    drained with them."""

    def __init__(self, corrections, notes, deferred, allowed, chars, files, by_path,
                 set_aside=None):
        self.by_path = by_path
        self.corrections = list(corrections)
        self.notes = list(notes)
        self.deferred = list(deferred)
        self.allowed = set(allowed)
        self.chars = chars
        self.files = files
        self.set_aside = dict(set_aside or {})
        taken = set(self.corrections) | set(self.notes)
        self.named = [t for t in dict.fromkeys(
            t for p in self.corrections if trusted(by_path[p]) for t in _inbox_targets(by_path[p]))
            if t in self.allowed and t not in taken and t not in self.set_aside]

    @property
    def items(self):
        return self.corrections + self.notes

    def __len__(self):
        return len(self.corrections) + len(self.notes)


def _cost(size):
    return max(MIN_ITEM_CHARS, CHARS_PER_CHAR * max(0, int(size)))


def trusted(item):
    """A correction the pass may apply: the owner's endpoint, and a basis the prompt accepts
    (a retraction only on the owner's own word)."""
    if item.get("source") != "local":
        return False
    if item.get("forget"):
        return True                              # `forget`: the one capture it names
    if item.get("retraction"):
        return item.get("basis") == "owner-said"
    return item.get("basis") in ("owner-said", "agent-found")


def _correction_cost(item):
    if not trusted(item):
        return _cost(item.get("size") or 0), 0       # it is weighed, not applied
    targets = item.get("targets") or []
    chars = _cost(int(item.get("size") or 0) + sum(int(t.get("size") or 0) for t in targets))
    files = FILES_PER_CANONICAL_TARGET * sum(1 for t in targets if t.get("canonical"))
    # An inbox note drained with its correction may be promoted like any other note, so it
    # costs what a note costs in files; at 0 a dozen corrections could drain 37 notes at an
    # estimated 11 files (I-148 review).
    files += FILES_PER_NOTE * len(_inbox_targets(item))
    return chars, files


def _inbox_targets(item):
    return [t["name"] for t in item.get("targets") or []
            if str(t.get("name") or "").startswith("knowledge/inbox/")]


def _holds(item):
    """The inbox notes a correction left in the inbox keeps there: the engine's correction
    check counts only local corrections (a drop box cannot hold anything back)."""
    return _inbox_targets(item) if item.get("source") == "local" else []


def _groups(corrections):
    """Corrections in order, joined into groups that share an inbox target: applying one of
    a group drains the note the others name, so they go into a batch together or not at
    all. Each group sits at the place of its first member."""
    groups, owner = [], {}
    for item in corrections:
        hits = [owner[t] for t in _inbox_targets(item) if t in owner]
        if hits:
            group = hits[0]
            for other in hits[1:]:
                if other is not group:
                    group.extend(other)
                    for member in other:
                        for t in _inbox_targets(member):
                            owner[t] = group
                    groups.remove(other)
        else:
            group = []
            groups.append(group)
        group.append(item)
        for t in _inbox_targets(item):
            owner[t] = group
    return groups


def select_batch(items, max_items=MAX_ITEMS, budget_chars=BUDGET_CHARS,
                 budget_files=BUDGET_FILES, set_aside=()):
    ordered = sorted(items, key=lambda i: i["path"])
    by_path = {i["path"]: i for i in ordered}
    set_aside = dict(set_aside) if isinstance(set_aside, dict) \
        else {p: "set aside" for p in set_aside}
    parked = {}
    for item in ordered:
        if int(item.get("size") or 0) > DIFF_CHARS_CAP:
            parked[item["path"]] = (f"alone over the audit's {DIFF_CHARS_CAP} diff chars "
                                    f"({int(item['size'])} chars)")
        elif item["path"] in set_aside:
            parked[item["path"]] = str(set_aside[item["path"]] or "set aside")
    live = [i for i in ordered if i["path"] not in parked]
    corrections = ([i for i in live if i.get("kind") == "correction" and trusted(i)]
                   + [i for i in live if i.get("kind") == "correction" and not trusted(i)])
    proposals = {i["path"] for i in live
                 if i.get("kind") != "correction" and i.get("source") == "session-mining"}
    every_hold = {t for i in corrections for t in _holds(i)}
    waiting = [i for i in live if i.get("kind") != "correction" and i["path"] not in proposals
               and i["path"] not in every_hold]
    share = CORRECTION_SHARE if waiting else 1.0
    chars = files = 0.0
    taken, deferred, allowed = [], [], set()

    def fits(c, f, n=1, part=1.0):
        if len(taken) + n > max_items * part and taken:
            return False
        if not taken:
            return True                          # at least one item, whatever it costs
        return chars + c <= budget_chars * part and files + f <= budget_files * part

    stopped = False
    for group in _groups(corrections):
        if any(t in parked for i in group if trusted(i) for t in _inbox_targets(i)):
            deferred.extend(group)               # it waits for the note it names
            continue
        c = sum(_correction_cost(i)[0] for i in group)
        f = sum(_correction_cost(i)[1] for i in group)
        if stopped or not fits(c, f, len(group), share):
            stopped = True
            deferred.extend(group)
            continue
        for item in group:
            taken.append(item["path"])
            allowed.add(item["path"])
            if trusted(item):
                allowed.update(_inbox_targets(item))
        chars, files = chars + c, files + f
    held = ({t for item in deferred for t in _holds(item)}
            | {t for p in taken for t in _holds(by_path[p]) if not trusted(by_path[p])})
    batch_corrections = list(taken)
    notes = []
    for item in waiting:
        path = item["path"]
        if path in held or path in allowed:
            continue
        c, f = _cost(item.get("size") or 0), FILES_PER_NOTE
        if notes and not fits(c, f):
            break
        if not notes and len(taken) >= max_items:
            break
        taken.append(path)
        notes.append(path)
        allowed.add(path)
        chars, files = chars + c, files + f
    return Batch(batch_corrections, notes, [i["path"] for i in deferred], allowed - held,
                 int(chars), round(files, 2), by_path, parked)


def cut(batch, n):
    """The first `n` items of `batch`: corrections first, but at least one note when the
    batch has notes and n > 1. A correction cut away is deferred, with every kept correction
    that shares an inbox note with it, and no inbox note a deferred local correction names is
    allowed. The deferred list keeps the selection's order, oldest first."""
    by_path = batch.by_path
    n = max(1, n)
    room = n - (1 if batch.notes and n > 1 else 0)
    corrections = batch.corrections[:room]
    notes = batch.notes[:n - len(corrections)]
    cut_away = [p for p in batch.corrections if p not in corrections]
    while True:
        shared = {t for p in cut_away for t in _inbox_targets(by_path[p])}
        joined = [p for p in corrections if set(_inbox_targets(by_path[p])) & shared]
        if not joined:
            break
        corrections = [p for p in corrections if p not in joined]
        cut_away = [p for p in batch.corrections if p not in corrections]
    deferred = list(dict.fromkeys(cut_away + batch.deferred))
    allowed, chars, files = set(), 0, 0.0
    for path in corrections:
        allowed.add(path)
        if trusted(by_path[path]):
            allowed.update(_inbox_targets(by_path[path]))
        c, f = _correction_cost(by_path[path])
        chars, files = chars + c, files + f
    held = ({t for p in deferred for t in _holds(by_path[p])}
            | {t for p in corrections for t in _holds(by_path[p]) if not trusted(by_path[p])})
    for path in notes:
        allowed.add(path)
        chars, files = chars + _cost(by_path[path].get("size") or 0), files + FILES_PER_NOTE
    return Batch(corrections, notes, deferred, allowed - held,
                 int(chars), round(files, 2), by_path, batch.set_aside)


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
