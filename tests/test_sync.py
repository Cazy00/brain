"""2.4: `brain sync` and the single pusher.

Every case runs real git against a local bare remote that stands in for GitHub, plus a
second clone that stands in for the owner merging curator PRs there. Each case checks the
one property that matters most: every capture the served folder accepted survives.
"""
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE / "bin"))
sys.path.insert(0, str(ENGINE / "tests"))
from brainlib import locks, sync  # noqa: E402
from test_remote import make_data_root  # noqa: E402

INBOX_NOTE = "---\ncreated: 2026-10-15\nstatus: draft\n---\n\n%s\n"
REFERENCE_NOTE = ("---\nid: %s\nkind: reference\ntitle: %s\ntopics: [infra]\n"
                  "aliases: [%s]\ncreated: 2026-10-15\nstatus: current\n---\n\n%s")


def git(cwd, *args, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr))
    return done


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = base / "origin.git"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        # The owner's side: a clone where curator PRs get merged and pushed.
        self.github = make_data_root(base / "gh")
        git(self.github, "remote", "add", "origin", str(self.remote))
        git(self.github, "push", "-q", "origin", "main")
        # The served folder: the data repository the server writes captures into.
        self.served = base / "served"
        git(base, "clone", "-q", str(self.remote), str(self.served))
        for key, value in (("user.email", "served@example.invalid"), ("user.name", "Served"),
                           ("core.hooksPath", "")):
            git(self.served, "config", key, value)
        self.state = base / "state"
        self.state.mkdir()
        self.accepted = []

    # -- the two sides -------------------------------------------------------

    def capture(self, name, body=None):
        """An accepted capture: an inbox note committed on the served folder's main."""
        rel = "knowledge/inbox/%s.md" % name
        (self.served / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.served / rel).write_text(INBOX_NOTE % (body or "Captured: " + name),
                                       encoding="utf-8")
        git(self.served, "add", rel)
        # Hooks off: a CLI run points this repo at the engine's hooks, which would
        # then lint and index the developer's checkout from here.
        git(self.served, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m",
            "capture: " + name)
        self.accepted.append(rel)
        return rel

    def merge_pr(self, rel, text, message="merge curator PR"):
        """The owner merges a PR on GitHub: main on the remote moves. Plain text for a
        reference path becomes a valid note, so the real lint judges the real thing."""
        git(self.github, "pull", "-q", "--ff-only", "origin", "main")
        if rel.startswith("knowledge/reference/") and not text.startswith("---"):
            stem = Path(rel).stem
            text = REFERENCE_NOTE % (stem, stem.replace("-", " ").title(), stem, text)
        path = self.github / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.github, "add", "-A")
        git(self.github, "commit", "-q", "-m", message)
        git(self.github, "push", "-q", "origin", "main")
        return git(self.github, "rev-parse", "HEAD").stdout.strip()

    # -- observations --------------------------------------------------------

    def head(self, repo, ref="refs/heads/main"):
        return git(repo, "rev-parse", ref).stdout.strip()

    def remote_refs(self):
        out = git(self.remote, "for-each-ref", "--format=%(refname)").stdout
        return sorted(line for line in out.splitlines() if line.strip())

    def status(self):
        return git(self.served, "status", "--porcelain", "--untracked-files=all").stdout

    def assert_captures_survive(self, on_remote):
        for rel in self.accepted:
            self.assertEqual(git(self.served, "cat-file", "-e", "main:" + rel,
                                 check=False).returncode, 0,
                             "an accepted capture is gone from the served main: " + rel)
            self.assertTrue((self.served / rel).is_file(), "missing on disk: " + rel)
            if on_remote:
                self.assertEqual(git(self.remote, "cat-file", "-e", "main:" + rel,
                                     check=False).returncode, 0,
                                 "an accepted capture never reached the remote: " + rel)

    def assert_no_worktrees_left(self):
        listed = git(self.served, "worktree", "list", "--porcelain").stdout
        self.assertEqual(listed.count("worktree "), 1, listed)

    def sync(self, **kwargs):
        return sync.run(self.served, self.state, **kwargs)


class ConvergenceTests(SyncCase):
    def test_in_sync_does_nothing(self):
        result = self.sync()
        self.assertEqual(result.status, "in_sync", result.detail)
        self.assertEqual(result.exit_code, 0)

    def test_a_remote_only_advance_fast_forwards_the_served_folder(self):
        pr = self.merge_pr("knowledge/reference/new-note.md", "a merged note\n")
        result = self.sync()
        self.assertEqual(result.status, "fast_forwarded", result.detail)
        self.assertEqual(self.head(self.served), pr)
        self.assertTrue((self.served / "knowledge/reference/new-note.md").is_file())
        self.assertEqual(self.status(), "")

    def test_local_only_captures_are_pushed_as_main_and_nothing_else(self):
        self.capture("one")
        self.capture("two")
        git(self.served, "branch", "side")          # a local branch that must stay local
        result = self.sync()
        self.assertEqual(result.status, "pushed", result.detail)
        self.assertEqual(self.head(self.remote), self.head(self.served))
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])
        self.assert_captures_survive(on_remote=True)

    def test_both_with_inbox_only_local_changes_merge_lint_install_and_push(self):
        self.capture("while-the-pr-was-open")
        pr = self.merge_pr("knowledge/reference/curated.md", "curated\n")
        result = self.sync()
        self.assertEqual(result.status, "merged", result.detail)
        main = self.head(self.served)
        self.assertEqual(self.head(self.remote), main)
        self.assertEqual(git(self.served, "merge-base", "--is-ancestor", pr, main,
                             check=False).returncode, 0, "the PR is not in the result")
        self.assertTrue((self.served / "knowledge/reference/curated.md").is_file())
        self.assertEqual(self.status(), "")
        self.assert_captures_survive(on_remote=True)
        self.assert_no_worktrees_left()

    def test_two_overlapping_remote_prs_land_in_order(self):
        self.capture("before-both-prs")
        self.merge_pr("knowledge/reference/shared.md", "first version\n", "PR 1")
        self.merge_pr("knowledge/reference/shared.md", "second version\n", "PR 2")
        result = self.sync()
        self.assertEqual(result.status, "merged", result.detail)
        self.assertTrue((self.served / "knowledge/reference/shared.md").read_text()
                        .endswith("second version\n"))
        self.assert_captures_survive(on_remote=True)

    def test_a_synced_folder_syncs_again_cleanly(self):
        """The merge commit a sync made is local history the next sync must accept."""
        self.capture("first")
        self.merge_pr("knowledge/reference/note-a.md", "a\n")
        self.assertEqual(self.sync().status, "merged")
        self.capture("second")
        self.merge_pr("knowledge/reference/note-b.md", "b\n")
        result = self.sync()
        self.assertEqual(result.status, "merged", result.detail)
        self.assert_captures_survive(on_remote=True)


class RefusalTests(SyncCase):
    def assert_untouched(self, before, remote_before):
        self.assertEqual(self.head(self.served), before, "the served main moved")
        self.assertEqual(self.head(self.remote), remote_before, "the remote moved")
        self.assertEqual(self.status(), "", "the served folder was left dirty")
        self.assert_captures_survive(on_remote=False)
        self.assert_no_worktrees_left()

    def test_same_path_additions_are_refused_not_resolved(self):
        self.capture("collision", "the served side's words")
        remote_before = self.merge_pr("knowledge/inbox/collision.md",
                                      INBOX_NOTE % "the remote side's words")
        before = self.head(self.served)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("conflict", result.detail)
        self.assert_untouched(before, remote_before)

    def test_a_local_modification_is_refused(self):
        (self.served / "knowledge/reference/tunnel-facts.md").write_text("edited here\n")
        git(self.served, "commit", "-qam", "a hand edit on the served folder")
        remote_before = self.merge_pr("knowledge/reference/other.md", "x\n")
        before = self.head(self.served)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("tunnel-facts.md", result.detail)
        self.assert_untouched(before, remote_before)

    def test_a_local_deletion_is_refused(self):
        git(self.served, "rm", "-q", "knowledge/reference/tunnel-facts.md")
        git(self.served, "commit", "-qm", "a hand delete on the served folder")
        remote_before = self.merge_pr("knowledge/reference/other.md", "x\n")
        before = self.head(self.served)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assert_untouched(before, remote_before)

    def test_a_merged_tree_that_fails_lint_is_refused(self):
        self.capture("fine")
        remote_before = self.merge_pr("knowledge/reference/bad.md",
                                      "---\nid: bad\n---\n\nno required fields\n")
        before = self.head(self.served)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("lint", result.detail)
        self.assert_untouched(before, remote_before)

    def test_a_served_folder_off_main_is_refused(self):
        git(self.served, "checkout", "-q", "-b", "consolidate/2026-10-15")
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("main", result.detail)

    def test_disk_full_during_the_merge_leaves_the_served_folder_untouched(self):
        self.capture("before-the-disk-filled")
        remote_before = self.merge_pr("knowledge/reference/note-c.md", "c\n")
        before = self.head(self.served)

        def disk_full(step):
            if step == "merged":
                raise OSError(errno.ENOSPC, "No space left on device")

        result = self.sync(fault=disk_full)
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("No space left", result.detail)
        self.assert_untouched(before, remote_before)


class ServedLocalChangesTests(SyncCase):
    """Spec 5.2: the served folder's own divergence is accepted only as pure inbox additions.
    Anything else reaches GitHub through a PR, never through the pusher (review, Astra)."""

    def hand_edit(self):
        facts = self.served / "knowledge/reference/tunnel-facts.md"
        facts.write_text(facts.read_text() + "\nedited on the served folder\n")
        git(self.served, "-c", "core.hooksPath=/dev/null", "commit", "-qam", "a hand edit")

    def test_a_served_folder_does_not_push_a_local_edit(self):
        from brainlib import served
        served.mark(self.served, "brain-http")
        self.hand_edit()
        remote_before = self.head(self.remote)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("tunnel-facts.md", result.detail)
        ok, detail, rejected = sync.push_main(self.served)
        self.assertFalse(ok)
        self.assertFalse(rejected)
        self.assertIn("tunnel-facts.md", detail)
        self.assertEqual(self.head(self.remote), remote_before)

    def test_a_served_folder_still_pushes_its_captures(self):
        from brainlib import served
        served.mark(self.served, "brain-http")
        self.capture("served-capture")
        self.assertEqual(self.sync().status, "pushed")
        self.assert_captures_survive(on_remote=True)

    def test_an_unserved_clone_pushes_its_own_edits(self):
        """A personal clone is not a served folder: every commit is still its backup."""
        self.hand_edit()
        self.assertEqual(self.sync().status, "pushed")


class SyncStatusTests(SyncCase):
    """Review M2 (re-graded): exit 1 is not an alert, so a sync that never succeeds again
    (a revoked key, DNS) must surface somewhere — doctor reads this record nightly."""

    def status_record(self):
        return json.loads((self.state / sync.STATUS_FILE).read_text())

    def test_a_success_records_when(self):
        self.sync()
        record = self.status_record()
        self.assertIsNotNone(record["last_ok"])
        self.assertEqual(record["status"], "in_sync")

    def test_a_pending_run_does_not_move_the_last_success(self):
        self.sync()
        first = self.status_record()["last_ok"]
        git(self.served, "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"))
        self.sync()
        record = self.status_record()
        self.assertEqual(record["last_ok"], first)
        self.assertEqual(record["status"], "pending")


class OverlapTests(SyncCase):
    def test_a_second_sync_never_tears_down_the_first_ones_worktree(self):
        """Review I3: the timer and the server queue both run syncs. One starting while the
        other is mid-merge must wait for the lock, not delete the merge out from under it."""
        import threading
        import time
        self.capture("overlapping")
        self.merge_pr("knowledge/reference/overlap-note.md", "x\n")
        second = {}

        def start_another(step):
            if step == "merged" and "thread" not in second:
                second["thread"] = threading.Thread(
                    target=lambda: second.update(result=self.sync()))
                second["thread"].start()
                time.sleep(1.5)

        first = self.sync(fault=start_another)
        second["thread"].join(timeout=120)
        self.assertEqual(first.status, "merged", first.detail)
        self.assertIn(second["result"].status, ("in_sync", "pushed"), second["result"].detail)
        self.assert_captures_survive(on_remote=True)
        self.assert_no_worktrees_left()


class PendingTests(SyncCase):
    def test_a_network_or_auth_failure_is_pending_and_loses_nothing(self):
        self.capture("offline")
        before = self.head(self.served)
        git(self.served, "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"))
        result = self.sync()
        self.assertEqual(result.status, "pending", result.detail)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.head(self.served), before)
        self.assert_captures_survive(on_remote=False)

    def test_the_remote_moving_between_fetch_and_push_is_retried_once(self):
        self.capture("racing")
        moved = []

        def owner_merges_again():
            if not moved:
                moved.append(self.merge_pr("knowledge/reference/late.md", "late\n"))

        result = self.sync(before_push=owner_merges_again)
        # The retry found the remote ahead AND the capture local: it merged.
        self.assertEqual(result.status, "merged", result.detail)
        self.assertEqual(self.head(self.remote), self.head(self.served))
        self.assertTrue((self.served / "knowledge/reference/late.md").is_file())
        self.assert_captures_survive(on_remote=True)

    def test_a_remote_that_keeps_moving_is_pending_after_one_retry(self):
        self.capture("still-racing")
        merges = []

        def owner_merges_every_time():
            merges.append(self.merge_pr("knowledge/reference/moved-%d.md" % len(merges),
                                        "m\n"))

        result = self.sync(before_push=owner_merges_every_time)
        self.assertEqual(result.status, "pending", result.detail)
        self.assertEqual(len(merges), 2, "it should try exactly twice")
        self.assertEqual(self.status(), "")
        self.assert_captures_survive(on_remote=False)
        self.assert_no_worktrees_left()


class CrashTests(SyncCase):
    """A crash at each step: the next run converges, and nothing accepted is lost."""

    def run_cli(self, crash_at=None):
        env = dict(os.environ, BRAIN_DATA_ROOT=str(self.served),
                   BRAIN_STATE_DIR=str(self.state))
        env.pop("BRAIN_TEST_CRASH_AT", None)
        if crash_at:
            env.update(BRAIN_TEST_HOOKS="1", BRAIN_TEST_CRASH_AT=crash_at)
        return subprocess.run([sys.executable, str(ENGINE / "bin" / "brain"), "sync"],
                              cwd=str(self.served), env=env, capture_output=True,
                              text=True, timeout=300)

    def test_a_crash_at_each_step_converges_on_the_next_run(self):
        for i, step in enumerate(sync.STEPS):
            with self.subTest(step=step):
                self.capture("crash-%d" % i)
                pr = self.merge_pr("knowledge/reference/crash-%d.md" % i, "pr %d\n" % i)
                crashed = self.run_cli(crash_at=step)
                self.assertEqual(crashed.returncode, sync.CRASH_EXIT,
                                 "no crash at %s:\n%s%s" % (step, crashed.stdout,
                                                            crashed.stderr))
                again = self.run_cli()
                self.assertIn(again.returncode, (0,), again.stdout + again.stderr)
                self.assertEqual(self.head(self.remote), self.head(self.served))
                self.assertEqual(git(self.served, "merge-base", "--is-ancestor", pr,
                                     "main", check=False).returncode, 0)
                self.assertEqual(self.status(), "")
                self.assert_captures_survive(on_remote=True)
                self.assert_no_worktrees_left()


class Crash(BaseException):
    """A process dying: not an Exception, so nothing in sync catches it."""


class InterruptedInstallTests(SyncCase):
    """Review (Astra 5): a crash INSIDE git's checkout leaves some files new and some old.
    The next run must finish the install, and writes are refused until it has."""

    def crash_halfway(self):
        self.capture("before-the-crash")
        self.merge_pr("knowledge/reference/half-one.md", "one\n")
        self.merge_pr("knowledge/reference/half-two.md", "two\n")

        def die_mid_checkout(step):
            if step == "installing":
                marker = json.loads((self.state / sync.INSTALL_MARK).read_text())
                target = marker["to"]
                # Half the checkout: one new file on disk, the index locked, HEAD old.
                content = git(self.served, "show", target + ":knowledge/reference/half-one.md")
                (self.served / "knowledge/reference/half-one.md").write_text(content.stdout)
                lock = self.served / ".git" / "index.lock"
                lock.write_text("")
                old = time.time() - 600
                os.utime(lock, (old, old))
                raise Crash()

        with self.assertRaises(Crash):
            self.sync(fault=die_mid_checkout)

    def test_writes_are_refused_until_the_install_is_finished(self):
        from brainlib import maintenance
        self.crash_halfway()
        self.assertEqual(maintenance.check(self.served, self.state), "interrupted update")

    def test_the_next_run_finishes_the_install(self):
        self.crash_halfway()
        result = self.sync()
        self.assertEqual(result.exit_code, 0, result.detail)
        self.assertEqual(self.head(self.served), self.head(self.remote))
        self.assertEqual(self.status(), "")
        self.assertFalse((self.state / sync.INSTALL_MARK).exists())
        self.assertFalse((self.served / ".git" / "index.lock").exists())
        for name in ("half-one", "half-two"):
            self.assertTrue((self.served / ("knowledge/reference/%s.md" % name)).is_file())
        self.assert_captures_survive(on_remote=True)

    def test_a_folder_moved_since_the_crash_is_refused_not_reset(self):
        self.crash_halfway()
        (self.served / ".git" / "index.lock").unlink()
        git(self.served, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty",
            "-m", "someone committed on top of the half-installed tree")
        before = self.head(self.served)
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertEqual(self.head(self.served), before)

    def test_tracked_changes_on_the_served_folder_are_never_overwritten(self):
        self.merge_pr("knowledge/reference/incoming.md", "x\n")
        facts = self.served / "knowledge/reference/tunnel-facts.md"
        facts.write_text(facts.read_text() + "\nan uncommitted hand edit\n")
        result = self.sync()
        self.assertEqual(result.status, "refused", result.detail)
        self.assertIn("an uncommitted hand edit", facts.read_text())


class ReaderExclusionTests(SyncCase):
    """Readers take the tree lock shared; the install takes it exclusive."""

    def test_reads_wait_while_an_install_holds_the_tree(self):
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl on this platform")
        env = dict(os.environ, BRAIN_DATA_ROOT=str(self.served),
                   BRAIN_STATE_DIR=str(self.state))
        # `index` too: it reads every note, and an index built from a half-installed tree
        # would look fresh afterwards (review, Astra 4).
        for args in (["read", "tunnel-facts"], ["search", "cloudflared"],
                     ["links", "tunnel-facts"], ["recent"], ["index", "--quiet"]):
            with self.subTest(command=args[0]):
                with locks.tree_lock(self.state, exclusive=True):
                    proc = subprocess.Popen([sys.executable, str(ENGINE / "bin" / "brain"),
                                             *args], cwd=str(self.served), env=env,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
                    with self.assertRaises(subprocess.TimeoutExpired,
                                           msg="`brain %s` read during an install" % args[0]):
                        proc.communicate(timeout=1.5)
                out, _ = proc.communicate(timeout=120)
                self.assertEqual(proc.returncode, 0, out)

    def test_the_install_holds_the_tree_exclusively(self):
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl on this platform")
        self.merge_pr("knowledge/reference/installed.md", "x\n")
        seen = {}

        def probe(step):
            if step == "installing":
                seen["reader_got_in"] = locks.try_tree_read(self.state)

        result = self.sync(fault=probe)
        self.assertEqual(result.status, "fast_forwarded", result.detail)
        self.assertIs(seen.get("reader_got_in"), False,
                      "a reader could take the tree while the install held it")


class HookPushTests(SyncCase):
    def test_push_only_never_waits_on_the_repo_lock(self):
        """The post-commit hook runs `brain sync --push` INSIDE a capture's lock region
        (synchronously under BRAIN_SYNC_PUSH). A rejected push that then ran a sync would
        wait on the lock its own parent holds, forever. It must report pending instead."""
        self.capture("behind")
        self.merge_pr("knowledge/reference/ahead-note.md", "x\n")
        env = dict(os.environ, BRAIN_DATA_ROOT=str(self.served),
                   BRAIN_STATE_DIR=str(self.state))
        with locks.repo_lock(self.state):
            done = subprocess.run([sys.executable, str(ENGINE / "bin" / "brain"), "sync",
                                   "--push"], cwd=str(self.served), env=env,
                                  capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("pending", done.stdout)


class SinglePusherTests(SyncCase):
    def test_the_push_is_exactly_main_never_head_never_forced(self):
        argv = sync.push_argv()
        self.assertEqual(argv, ["push", "--porcelain", "origin",
                                "refs/heads/main:refs/heads/main"])
        joined = " ".join(argv)
        for banned in ("HEAD", "--force", "-f", "+refs", "--mirror", "--all"):
            self.assertNotIn(banned, joined.split() if banned == "-f" else joined)

    def test_push_main_pushes_main_even_from_another_branch(self):
        self.capture("on-main")
        git(self.served, "checkout", "-q", "-b", "elsewhere")
        (self.served / "knowledge/inbox/elsewhere.md").write_text(INBOX_NOTE % "x")
        git(self.served, "add", "-A")
        git(self.served, "-c", "core.hooksPath=/dev/null", "commit", "-qm",
            "work on another branch")
        ok, detail, rejected = sync.push_main(self.served)
        self.assertTrue(ok, detail)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])
        self.assertEqual(self.head(self.remote), self.head(self.served))

    def test_a_rejected_push_says_so(self):
        self.capture("behind")
        self.merge_pr("knowledge/reference/ahead.md", "x\n")
        ok, _detail, rejected = sync.push_main(self.served)
        self.assertFalse(ok)
        self.assertTrue(rejected)

    def test_no_code_path_pushes_head(self):
        """Exactly one code path pushes main, and it names main. A `push ... HEAD`
        anywhere in the engine is a second pusher."""
        import re
        python_head = re.compile(r"""["']push["'][^\]\n]*["']HEAD["']""")
        shell_head = re.compile(r"^[^#\n]*git push[^\n#]*HEAD", re.M)
        files = [ENGINE / "bin" / "brain", ENGINE / "bin" / "brain-http"]
        files += sorted((ENGINE / "bin" / "brainlib").glob("*.py"))
        files += sorted(p for p in (ENGINE / ".githooks").iterdir() if p.is_file())
        files += sorted((ENGINE / "deploy").rglob("*.sh"))
        for path in files:
            text = path.read_text(encoding="utf-8")
            pattern = python_head if path.suffix == ".py" or path.name in (
                "brain", "brain-http") else shell_head
            self.assertIsNone(pattern.search(text), "%s pushes HEAD" % path)


if __name__ == "__main__":
    unittest.main()
