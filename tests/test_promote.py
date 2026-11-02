"""Promotion: the winner's branch, its checks and its pull request, and the held-out vs screening gap in the report
(spec .regista/specs/promote.json: PROMO-1, PROMO-2, PROMO-4; PRM-1 is the end-to-end test in genome/tests/e2e/test_promote.py;
PROMO-3 is checked by hand).

`evolve.py promote <genome> --generation <g> [--repo <path>] [--onto <branch>] [--no-pr]` creates
genome/<g>-<genome> from --onto in a temporary git worktree of --repo, writes there the evolvable files that differ
from the base genome's and the report at docs/generations/<g>.md, commits, runs the checks (evolve.json
`promote_checks` here), pushes only that branch to evolve.json `promote_remote` (default origin) and opens a pull
request with evolve.json `pr_command`.

The generation is tests/test_generation.py's Winner fixture (imported as a module, so its tests don't run twice): the
command runs in-process (evolve.main) with the fake replay_run and the real mutate around the stand-in claude, in a
temporary REGISTA_EVOLVE_HOME. Child 2 (seed-m2) wins: it changes only agents/builder.md. The one difference: the fake
Regista checkout the genome is seeded from is a git repository with one commit on `main` (so the seed's base.commit is
its HEAD), and it is the --repo of every test. Its remote `origin` is a local bare repository. Nothing touches this
repository or GitHub.

Stand-ins, all in the test's temporary folder:
- the checks (evolve.json promote_checks): one records the folder it runs in and the agents/builder.md it sees there;
  another prints 60 lines and fails on purpose;
- the pull-request command (evolve.json pr_command): records its arguments and the content of its --body-file, prints a
  fake URL;
- `git` first on PATH while promote runs: records each call's arguments, then runs the real git.
The committer's name and e-mail come from GIT_AUTHOR_* and GIT_COMMITTER_* (HOME is a temporary folder).

Interface assumed: `evolve.main(["promote", ...])` returns (or exits with) 0 on success and non-zero on a refusal or a
failing check; promote_checks is a list of command strings (each here a single executable path); pr_command is a
command string like claude_command (here a single executable path) to which promote adds `--base <onto> --head
<branch> --title <title> --body-file <report>`; promote calls git through PATH. "Didn't finish" covers a generation
stopped (Ctrl-C) during its held-out replays, before it picked a winner.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_promote -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_generation as tg  # noqa: E402  (imported as a module, so its test classes don't run twice here)
import test_genome_seed as seedtests  # noqa: E402
import test_reflect_mutate as rm  # noqa: E402

GEN = tg.GEN
TARGET = tg.TARGET
PY = sys.executable
PR_URL = "https://example.test/acme/regista/pull/4242"
FAIL_MARKER = "check-output-marker-58"
UNFINISHED = re.compile(r"(?i)in progress|still running|not (yet )?(finished|complete)|unfinished|incomplete|"
                        r"interrupted|(hasn't|has not|isn't|is not|wasn't|was not|didn't|did not) (yet )?finish|"
                        r"stopped before|no winner yet")
IDENTITY = {"GIT_AUTHOR_NAME": "Promote Test", "GIT_AUTHOR_EMAIL": "promote@example.com",
            "GIT_COMMITTER_NAME": "Promote Test", "GIT_COMMITTER_EMAIL": "promote@example.com"}

RECORD_CHECK = r'''#!@PYTHON@
import json, os
path = os.path.join(os.getcwd(), "agents", "builder.md")
builder = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "builder": builder}) + "\n")
print("record check: ok")
'''

FAIL_CHECK = r'''#!@PYTHON@
import sys
for i in range(60):
    print(f"failing check line {i}")
print(@MARKER@)
sys.exit(3)
'''

PR_STANDIN = r'''#!@PYTHON@
import json, os, sys
args = sys.argv[1:]
def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
body_file = opt("--body-file")
body = None
if body_file:
    path = body_file if os.path.isabs(body_file) else os.path.join(os.getcwd(), body_file)
    if os.path.isfile(path):
        body = open(path, encoding="utf-8").read()
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "body": body, "inline_body": opt("--body")}) + "\n")
print(@URL@)
'''

GIT_WRAPPER = '''#!/bin/sh
for a in "$@"; do printf '%s\\037' "$a"; done >> "@LOG@"
printf '\\n' >> "@LOG@"
exec "@GIT@" "$@"
'''


def git(cwd, *args):
    return seedtests.git(cwd, *args)


def git_ok(cwd, *args):
    """True when the git command succeeds (for --verify style questions)."""
    env = dict(os.environ, **seedtests.GIT_ENV)
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env).returncode == 0


def read_lines(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line for line in f.read().splitlines() if line.strip()]


def subcommand(argv):
    """The git subcommand of a recorded call, skipping global options such as -C <path> and -c <name=value>."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return None, []


class PromoteBase(tg.Base):
    """test_generation's Winner fixture over a git checkout with a bare remote; seed-m2 wins."""

    def setUp(self):
        real_build = seedtests.build_checkout
        with mock.patch.object(rm, "build_checkout",
                               lambda parent, *a, **kw: real_build(parent, *a, **{**kw, "use_git": True})):
            super().setUp()
        self.target_repo = self.checkout
        self.base_commit = git(self.target_repo, "rev-parse", "HEAD")
        seed_meta = self.read_json(self.genome_dir("seed"), "genome.json")
        self.assertEqual((seed_meta.get("base") or {}).get("commit"), self.base_commit,
                         "fixture: the seed's base.commit is the checkout's HEAD")

        # Stand-ins.
        self.bin = os.path.join(self.tmp, "bin")
        self.check_log = os.path.join(self.tmp, "checks.jsonl")
        self.pr_log = os.path.join(self.tmp, "pr-calls.jsonl")
        self.git_log = os.path.join(self.tmp, "git-calls.log")
        self.record_check = self.script("record-check", RECORD_CHECK.replace("@LOG@", repr(self.check_log)))
        self.fail_check = self.script("fail-check", FAIL_CHECK.replace("@MARKER@", repr(FAIL_MARKER)))
        self.pr_command = self.script("pr-standin", PR_STANDIN.replace("@LOG@", repr(self.pr_log))
                                      .replace("@URL@", repr(PR_URL)))
        real_git = shutil.which("git")
        self.assertTrue(real_git, "fixture: git is installed")
        self.git_bin = os.path.join(self.tmp, "gitbin")
        rm.write(os.path.join(self.git_bin, "git"),
                 GIT_WRAPPER.replace("@LOG@", self.git_log).replace("@GIT@", real_git))
        os.chmod(os.path.join(self.git_bin, "git"), 0o755)

        # The generation: child 2 wins on the held-out cases (as test_generation's Winner).
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.set_config()
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 10000},
                     (1, "screen"): {"outcome": "approved", "used": 20000},
                     (2, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 5000},
                     ("base", "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 13000},
                     (1, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
                     (2, "holdout"): {"outcome": "approved", "used": 12000}}

    def script(self, name, text):
        path = os.path.join(self.bin, name)
        rm.write(path, text.replace("@PYTHON@", PY))
        os.chmod(path, 0o755)
        return path

    def set_config(self, checks=None, **extra):
        self.configure(population=2, promote_checks=checks or [self.record_check], pr_command=self.pr_command,
                       **extra)

    def run_generation(self, **kw):
        code, out = self.generation(**kw)
        self.assertNotIn("Traceback", out)
        return code, out

    def winner_fixture(self):
        code, out = self.run_generation()
        self.assertEqual(code, 0, out)
        self.winner = self.state().get("winner")
        self.assertEqual(self.winner, self.made[1], f"fixture: child 2 wins: {self.state()}")
        self.branch = f"genome/{GEN}-{self.winner}"
        with open(os.path.join(self.genome_dir(self.winner), "files", *TARGET.split("/")), encoding="utf-8") as f:
            self.winner_builder = f.read()
        self.assertNotEqual(self.winner_builder, seedtests.EVOLVABLE[TARGET], "fixture: the winner changed builder.md")

    def add_remote(self):
        """A local bare repository as origin, holding every local branch of the checkout."""
        self.bare = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        git(self.target_repo, "remote", "add", "origin", self.bare)
        git(self.target_repo, "push", "-q", "origin", "--all")

    def commit_on(self, branch, rel, text, message):
        """Commit one file's new content on branch, leaving the checkout on the branch it was on."""
        current = git(self.target_repo, "symbolic-ref", "--short", "HEAD")
        if current != branch:
            git(self.target_repo, "checkout", "-q", branch)
        rm.write(os.path.join(self.target_repo, *rel.split("/")), text)
        git(self.target_repo, "add", rel)
        git(self.target_repo, "commit", "-q", "-m", message)
        if current != branch:
            git(self.target_repo, "checkout", "-q", current)

    # Observing ------------------------------------------------------------------------------------------------------

    def remote_refs(self):
        out = git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def local_genome_branches(self):
        out = git(self.target_repo, "for-each-ref", "--format=%(refname)", "refs/heads/genome/")
        return [line for line in out.splitlines() if line.strip()]

    def worktrees(self):
        out = git(self.target_repo, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def user_state(self):
        """The user's checkout as the user sees it: current branch, HEAD, status and every file outside .git."""
        return {"branch": git(self.target_repo, "symbolic-ref", "--short", "HEAD"),
                "head": git(self.target_repo, "rev-parse", "HEAD"),
                "status": git(self.target_repo, "status", "--porcelain", "--untracked-files=all"),
                "files": seedtests.snapshot(self.target_repo, skip_git=True)}

    def pr_calls(self):
        return [json.loads(line) for line in read_lines(self.pr_log)]

    def check_calls(self):
        return [json.loads(line) for line in read_lines(self.check_log)]

    def git_calls(self):
        return [line.split("\x1f")[:-1] for line in read_lines(self.git_log)]

    def promote(self, genome=None, extra=()):
        """Run `evolve.py promote` in-process, with the recording git first on PATH; returns (code, printed text)."""
        argv = ["promote", genome or self.winner, "--generation", GEN, "--repo", self.target_repo, *extra]
        env = {"PATH": self.git_bin + os.pathsep + os.environ.get("PATH", ""), **IDENTITY, **seedtests.GIT_ENV}
        with mock.patch.dict(os.environ, env):
            code, out = self.invoke(argv)
        self.assertNotIn("Traceback", out)
        return code, out

    def assert_nothing_created(self, before_user, before_remote, out):
        self.assertEqual(self.local_genome_branches(), [], f"no genome branch is left in the repository:\n{out}")
        self.assertEqual(self.remote_refs(), before_remote, f"nothing is pushed:\n{out}")
        self.assertEqual(self.pr_calls(), [], f"no pull request is asked for:\n{out}")
        self.assertEqual(len(self.worktrees()), 1, f"no temporary worktree is left registered: {self.worktrees()}")
        self.assertEqual(self.user_state(), before_user, "the user's checkout is unchanged")

    def assert_refused(self, code, out, before_user, before_remote, reason, names_file=True):
        self.assertNotEqual(code, 0, f"promote refuses:\n{out}")
        self.assertRegex(out, reason, f"the message names the reason:\n{out}")
        if names_file:
            self.assertIn(TARGET, out, f"and the file:\n{out}")
        self.assertLessEqual(len([line for line in out.splitlines() if line.strip()]), 10, f"a short message:\n{out}")
        self.assertEqual(self.check_calls(), [], f"it refuses before anything runs, so no check ran:\n{out}")
        made = [c for c in self.git_calls() if subcommand(c)[0] in ("commit", "push")
                or (subcommand(c)[0] == "worktree" and subcommand(c)[1][:1] == ["add"])]
        self.assertEqual(made, [], f"no worktree, commit or push before refusing:\n{out}")
        self.assert_nothing_created(before_user, before_remote, out)


# PROMO-1: the branch, the push and the pull request -------------------------------------------------------------------

class Promotes(PromoteBase):
    def setUp(self):
        super().setUp()
        self.winner_fixture()
        # The user works on `feature`; main moved on since base.commit, in a file the winner doesn't change.
        git(self.target_repo, "branch", "feature")
        self.main_reviewer = seedtests.EVOLVABLE["agents/reviewer.md"] + "Main moved on.\n"
        self.commit_on("main", "agents/reviewer.md", self.main_reviewer, "Reviewer: one more line")
        git(self.target_repo, "checkout", "-q", "feature")
        rm.write(os.path.join(self.target_repo, "notes.txt"), "The user's own untracked notes.\n")
        self.add_remote()
        self.main_head = git(self.target_repo, "rev-parse", "main")

    def test_promote_pushes_only_its_branch_and_opens_one_pull_request(self):  # covers: PROMO-1
        before_user, before_remote = self.user_state(), self.remote_refs()
        code, out = self.promote(extra=["--onto", "main"])
        self.assertEqual(code, 0, out)

        # The remote: exactly one new branch, main and every other branch unchanged.
        after = self.remote_refs()
        ref = "refs/heads/" + self.branch
        self.assertEqual({k: v for k, v in after.items() if k != ref}, before_remote,
                         f"main and every other branch on the remote are unchanged:\n{out}")
        self.assertIn(ref, after, f"the remote gains {self.branch}:\n{out}")
        tip = after[ref]
        self.assertEqual(git(self.bare, "rev-parse", f"{tip}^"), self.main_head,
                         "the branch is one commit on top of --onto (main), so nothing was merged into it")
        self.assertEqual(git(self.bare, "rev-list", "--count", f"main..{tip}"), "1")
        self.assertEqual(len(git(self.bare, "rev-list", "--parents", "-n", "1", tip).split()), 2,
                         "the commit has one parent: not a merge")
        changed = sorted(git(self.bare, "diff", "--name-only", "main", tip).splitlines())
        self.assertEqual(changed, sorted([TARGET, f"docs/generations/{GEN}.md"]),
                         "the branch holds only the changed evolvable file and the report")
        self.assertEqual(git(self.bare, "show", f"{tip}:{TARGET}") + "\n", self.winner_builder)
        self.assertEqual(git(self.bare, "show", f"{tip}:agents/reviewer.md") + "\n", self.main_reviewer,
                         "a file the genome didn't change keeps --onto's content")
        report = git(self.bare, "show", f"{tip}:docs/generations/{GEN}.md")
        self.assertIn(self.winner, report, "the report names the winner")
        message = git(self.bare, "log", "-1", "--format=%B", tip)
        for name in (GEN, self.winner, "seed"):
            self.assertIn(name, message, f"the commit message names the generation, the winner and its parent:\n"
                                         f"{message}")

        # The checks ran in a temporary worktree holding the winner's file, which is gone afterwards.
        checks = self.check_calls()
        self.assertEqual(len(checks), 1, checks)
        self.assertNotEqual(os.path.realpath(checks[0]["cwd"]), os.path.realpath(self.target_repo),
                            "the checks run in a temporary worktree, not in the user's checkout")
        self.assertEqual(checks[0]["builder"], self.winner_builder, "the worktree holds the winner's file")
        self.assertEqual(len(self.worktrees()), 1, f"no temporary worktree is left registered: {self.worktrees()}")
        self.assertTrue(git_ok(self.target_repo, "rev-parse", "--verify", "-q", ref), "the local branch stays")

        # The pull request: asked for once, against --onto, with the report as its body.
        calls = self.pr_calls()
        self.assertEqual(len(calls), 1, f"one pull request:\n{calls}")
        argv = calls[0]["argv"]
        self.assertIn("--base", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertIn("--head", argv)
        self.assertEqual(argv[argv.index("--head") + 1], self.branch, argv)
        self.assertNotIn("merge", argv, "the pull-request command is never asked to merge")
        self.assertEqual((calls[0]["body"] or "").strip(), report.strip(), "the pull request's body is the report")
        self.assertIn(PR_URL, out, "the pull request's URL is printed")

        # git: one push of this branch alone, never forced, and no merge.
        git_calls = [subcommand(c) for c in self.git_calls()]
        self.assertTrue(git_calls, "fixture: promote's git calls were recorded")
        for sub, rest in git_calls:
            self.assertNotIn(sub, ("merge", "pull", "rebase", "cherry-pick"), f"no merge: git {sub} {rest}")
        pushes = [rest for sub, rest in git_calls if sub == "push"]
        self.assertEqual(len(pushes), 1, f"one push: {pushes}")
        for arg in pushes[0]:
            self.assertFalse(arg in ("-f", "--force", "--mirror", "--all", "--tags") or arg.startswith(
                ("--force", "+")), f"never a forced or wide push: {pushes[0]}")
        targets = [a for a in pushes[0] if not a.startswith("-")]
        self.assertEqual(targets[0], "origin", f"pushed to promote_remote's default, origin: {pushes[0]}")
        self.assertTrue(targets[1:] and all(self.branch in a for a in targets[1:]),
                        f"only its own branch is pushed: {pushes[0]}")

        # The user's checkout: same branch, HEAD, status and files.
        self.assertEqual(self.user_state(), before_user, "the user's checkout is unchanged")

    def test_no_pr_runs_the_checks_pushes_nothing_and_prints_the_commands(self):  # covers: PROMO-1
        before_user, before_remote = self.user_state(), self.remote_refs()
        code, out = self.promote(extra=["--onto", "main", "--no-pr"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.check_calls()), 1, "the checks run")
        self.assertEqual(self.remote_refs(), before_remote, f"nothing is pushed:\n{out}")
        self.assertEqual(self.pr_calls(), [], f"no pull request is asked for:\n{out}")
        self.assertFalse([c for c in self.git_calls() if subcommand(c)[0] == "push"], "git push is never run")
        ref = "refs/heads/" + self.branch
        self.assertTrue(git_ok(self.target_repo, "rev-parse", "--verify", "-q", ref),
                        "the branch stays in the repository, ready to push")
        self.assertEqual(git(self.target_repo, "show", f"{ref}:{TARGET}") + "\n", self.winner_builder)
        self.assertIn(self.branch, out, f"prints the branch name:\n{out}")
        push_lines = [line for line in out.splitlines() if "git push" in line and self.branch in line]
        self.assertTrue(push_lines, f"prints the push command:\n{out}")
        self.assertFalse(any(re.search(r"(?<!\S)(-f|--force\S*)(?!\S)", line) for line in push_lines), push_lines)
        self.assertTrue([line for line in out.splitlines() if "--head" in line and self.branch in line
                         and "--base" in line], f"prints the pull-request command:\n{out}")
        self.assertEqual(len(self.worktrees()), 1, f"no temporary worktree is left registered: {self.worktrees()}")
        self.assertEqual(self.user_state(), before_user, "the user's checkout is unchanged")


# PROMO-2: screening vs held-out in the report ---------------------------------------------------------------------

class GapInTheReport(PromoteBase):
    def test_the_report_compares_screening_and_held_out_for_the_winner_and_the_base(self):  # covers: PROMO-2
        # Means per genome: the winner seed-m2 used 5,000 tokens on screening and 12,000 held out (a gap of 7,000);
        # the base 10,000 and 13,000 (3,000). Requirements proven: the winner 67% and 100%, the base 67% and 67%.
        self.winner_fixture()
        code, out = self.invoke(["report", GEN])
        self.assertEqual(code, 0, out)
        path = os.path.join(self.evo, "reports", GEN + ".md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = text.splitlines()
        heading = re.compile(r"(?i)\bgap\b|screening\b.*held[- ]out|held[- ]out\b.*screening|training")
        starts = [i for i, line in enumerate(lines) if line.startswith("#") and heading.search(line)]
        self.assertTrue(starts, f"the report has a section comparing screening and held-out results:\n{text}")
        level = len(lines[starts[0]]) - len(lines[starts[0]].lstrip("#"))
        end = next((j for j in range(starts[0] + 1, len(lines)) if re.match(rf"^#{{1,{level}}}\s", lines[j])),
                   len(lines))
        section = "\n".join(lines[starts[0]:end])
        for name in ("seed", self.winner):
            self.assertRegex(section, rf"(?<![\w-]){re.escape(name)}(?![\w-])", f"it covers {name}:\n{section}")
        for axis in (r"requirements", r"approv", r"review.rounds", r"findings", r"tokens"):
            self.assertRegex(section, rf"(?i){axis}", f"every axis is compared ({axis}):\n{section}")
        self.assertRegex(section, r"(?i)\bgap\b|\bdifference\b", f"the gap is named:\n{section}")
        for n, what in ((5000, "the winner's screening tokens"), (12000, "the winner's held-out tokens"),
                        (7000, "the winner's tokens gap"), (10000, "the base's screening tokens"),
                        (13000, "the base's held-out tokens"), (3000, "the base's tokens gap")):
            self.assertTrue(tg.mentions(section, n), f"{what} ({n:,}):\n{section}")
        self.assertRegex(section, r"\b33\b|0\.33", f"the winner's requirements gap (33 points):\n{section}")
        self.assertRegex(section, r"(?i)\bfit|overfit|tuned to|tailored to",
                         f"a plain sentence on what a big gap means (fitted to the screening steps):\n{section}")
        self.assertIn(text.strip(), out, "the command prints the same text it writes")


# PROMO-4: refusals and a failing check --------------------------------------------------------------------------------

class Refusals(PromoteBase):
    def ready(self):
        self.add_remote()
        return self.user_state(), self.remote_refs()

    def test_a_genome_that_is_not_the_winner_is_refused(self):  # covers: PROMO-4
        self.winner_fixture()
        before = self.ready()
        code, out = self.promote(genome=self.made[0])
        self.assert_refused(code, out, *before, re.compile(r"(?i)\bwinner\b"), names_file=False)
        self.assertIn(self.made[0], out, "the message names the genome")

    def test_a_generation_that_did_not_finish_is_refused(self):  # covers: PROMO-4
        # Ctrl-C during the second held-out replay: the front is known, no winner yet.
        self.interrupt = lambda role, kind, nth: kind == "holdout" and \
            sum(1 for r in self.replays if r["kind"] == "holdout") == 2
        self.run_generation()
        self.assertIsNone(self.state().get("winner"), f"fixture: no winner yet: {self.state()}")
        self.assertIn(self.made[1], self.state().get("front") or [], "fixture: child 2 is on the front")
        self.winner = self.made[1]
        before = self.ready()
        code, out = self.promote()
        self.assert_refused(code, out, *before, UNFINISHED, names_file=False)

    def test_a_tampered_genome_is_refused(self):  # covers: PROMO-4
        self.winner_fixture()
        for part in ("files", "plugin"):
            path = os.path.join(self.genome_dir(self.winner), part, *TARGET.split("/"))
            if os.path.isfile(path):
                os.chmod(path, 0o644)
                with open(path, "a", encoding="utf-8") as f:
                    f.write("Slipped in after the generation.\n")
        before = self.ready()
        code, out = self.promote()
        self.assert_refused(code, out, *before, re.compile(re.escape(TARGET)))

    def test_an_uncommitted_change_in_a_genome_file_is_refused(self):  # covers: PROMO-4
        self.winner_fixture()
        self.add_remote()
        rm.write(os.path.join(self.target_repo, *TARGET.split("/")), seedtests.EVOLVABLE[TARGET] + "My edit.\n")
        before = self.user_state(), self.remote_refs()
        code, out = self.promote()
        self.assert_refused(code, out, *before,
                            re.compile(r"(?i)uncommitted|not committed|local (change|edit)|working (tree|copy)|"
                                       r"modified|unsaved"))

    def test_a_genome_file_changed_on_onto_since_the_base_commit_is_refused(self):  # covers: PROMO-4
        self.winner_fixture()
        self.commit_on("main", TARGET, seedtests.EVOLVABLE[TARGET] + "A line added on main.\n", "Builder: one more")
        before = self.ready()
        code, out = self.promote()
        self.assert_refused(code, out, *before,
                            re.compile(r"(?i)\bsince\b|base commit|changed on|was changed|has changed|also changed"))
        self.assertRegex(out, r"\bmain\b", f"the message names the branch it changed on:\n{out}")

    def test_a_failing_check_deletes_the_branch_pushes_nothing_and_prints_its_output(self):  # covers: PROMO-4
        self.set_config(checks=[self.record_check, self.fail_check])
        self.winner_fixture()
        user, remote = self.ready()
        code, out = self.promote()
        self.assertNotEqual(code, 0, f"a failing check stops promote:\n{out}")
        self.assertIn(FAIL_MARKER, out, f"the failing command's output tail is printed:\n{out}")
        self.assertTrue(self.check_calls(), "fixture: the checks ran")
        self.assertFalse([c for c in self.git_calls() if subcommand(c)[0] == "push"], "git push is never run")
        self.assert_nothing_created(user, remote, out)


if __name__ == "__main__":
    unittest.main()
