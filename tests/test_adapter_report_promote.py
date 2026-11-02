"""Report and promote through the adapter (spec .regista/specs/adapter-report-promote.json: ADP-2, ADP-8, ADP-9).

evolve_core.report_text(home, state, adapter) writes the Markdown report from the adapter's axes, headers, labels and
words; evolve_core.promote(home, config, state, genome, adapter, profile, repo, onto, ...) puts the winner's changed
files and the report on a new branch, runs the profile's checks there and calls the pull-request command.
evolve.adapter_for_state(home, config, state) rebuilds the adapter a generation used from generation.json's
'adapter': {'kind': ...} (no key means Regista's).

These tests reuse tests/test_adapter_core.py's fixture (imported as a module, so its tests don't run twice): the toy
target (one evolvable file, prompt.md, seeded as `seed`), the toy adapter whose run writes result.json from a plan, and
the stand-in reflect. No claude runs and no tokens are spent. REGISTA_EVOLVE_HOME and HOME are temporary folders.
Differences from that fixture here:
- The toy target folder is a git repository on `main` with one commit, made just before it is seeded (so the seed's
  base.commit is its HEAD), and it is the repository promote works on. Its `origin` is a local bare repository standing
  in for GitHub.
- The toy profile has `checks`: one stand-in check that records the folder it runs in and the prompt.md it sees.
- The pull-request command is tests/test_promote.py's stand-in: it records its arguments and its --body-file's content
  and prints a fake URL.
- The temporary folder's name doesn't hold the word "regista", so a path that leaks into the report or the branch
  can't hide a forbidden word, nor make one up.

The plan (both children on the screening front, both qualify on the held-out runs, opposite orders):
- screening: base passed 0.5, errors 2, 10,000 tokens; child 1 passed 1.0, errors 0, 20,000 tokens, score 1.0;
  child 2 passed 0.8, errors 1, 5,000 tokens, score 0.7.
- held out: base passed 0.5, errors 2, 13,000 tokens; child 1 passed 0.6, errors 1, 12,000 tokens, score 0.6;
  child 2 passed 1.0, errors 0, 9,000 tokens, score 0.95. Child 2 wins; its gap is 9,000 - 5,000 = 4,000 tokens.

Interface assumed (beyond the spec):
- evolve_core.promote's arguments after `onto` are not named by the spec. These tests pass the pull-request command
  and the remote as keyword arguments when promote has a parameter of that name (pr_command; remote or
  promote_remote), and also in config (pr_command, promote_remote), and leave every other argument at its default
  (a pull request is opened). A refusal is an EvolveError; success returns anything.
- The checks come from the profile's `checks` (command strings; here a single executable path).
- The report's path inside the repository is the base class's default, evolve-reports/<generation>.md.
- The adapter's report words are attributes set on the instance: headers (dict axis -> header) and run_word.
- `evolve.main(["report" | "promote", ...])` returns (or exits with) non-zero on an error and prints it.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_adapter_report_promote -v"""
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_adapter_core as ac  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_genome_seed as gs  # noqa: E402
import test_generation as tg  # noqa: E402
import test_promote as tp  # noqa: E402

GEN = ac.GEN
TARGET = ac.TARGET
FORBIDDEN = re.compile(r"(?i)regista|replay|bench|plugin|claude")
UNKNOWN_KIND = "mystery-kind"
RUN_WORD = "trial"
HEADERS = {"errors": "Errors seen"}  # passed has the label "cases passed"; tokens has no label
EXPECTED_HEADERS = ["Cases passed", "Errors seen", "Tokens"]

# Both children reach the screening front (child 1 passes most, child 2 spends least and dominates the base), and
# both are no worse than the base on passed and errors in the held-out runs.
LATER_CHILD_WINS = {
    ("base", "screen"): {"passed": 0.5, "errors": 2, "used": 10000, "score": 0.5, "failures": 2},
    (1, "screen"): {"passed": 1.0, "errors": 0, "used": 20000, "score": 1.0, "failures": 0},
    (2, "screen"): {"passed": 0.8, "errors": 1, "used": 5000, "score": 0.7, "failures": 0},
    ("base", "holdout"): {"passed": 0.5, "errors": 2, "used": 13000, "score": 0.5, "failures": 0},
    (1, "holdout"): {"passed": 0.6, "errors": 1, "used": 12000, "score": 0.6, "failures": 0},
    (2, "holdout"): {"passed": 1.0, "errors": 0, "used": 9000, "score": 0.95, "failures": 0},
}
EARLIER_CHILD_WINS = {
    ("base", "screen"): LATER_CHILD_WINS[("base", "screen")],
    (1, "screen"): LATER_CHILD_WINS[(2, "screen")],
    (2, "screen"): LATER_CHILD_WINS[(1, "screen")],
    ("base", "holdout"): LATER_CHILD_WINS[("base", "holdout")],
    (1, "holdout"): LATER_CHILD_WINS[(2, "holdout")],
    (2, "holdout"): LATER_CHILD_WINS[(1, "holdout")],
}

RECORD_CHECK = r'''#!@PYTHON@
import json, os
path = os.path.join(os.getcwd(), "prompt.md")
prompt = open(path, encoding="utf-8").read() if os.path.isfile(path) else None
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "prompt": prompt}) + "\n")
print("record check: ok")
'''


def git(cwd, *args):
    return gs.git(cwd, *args)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def section(text, heading):
    """The lines from the first Markdown heading matching `heading` up to the next heading of the same or higher level,
    or None."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("#") and heading.search(line)]
    if not starts:
        return None
    level = len(lines[starts[0]]) - len(lines[starts[0]].lstrip("#"))
    end = next((j for j in range(starts[0] + 1, len(lines)) if re.match(rf"^#{{1,{level}}}\s", lines[j])), len(lines))
    return "\n".join(lines[starts[0]:end])


def word(name):
    return rf"(?<![\w-]){re.escape(name)}(?![\w-])"


# ADP-8: two qualifying children whose screening and held-out orders disagree ------------------------------------------

class HeldOutOrderDecides(ac.Base):
    def run_plan(self, plan):
        self.plan.update(plan)
        with contextlib.redirect_stdout(io.StringIO()):
            stop = self.generation()
        self.assertIsNone(stop, f"the generation finished: {self.state()}")
        self.assertEqual(len(self.made), 2, f"two children: {self.mutations}")
        state = self.state()
        self.assertEqual(sorted(state.get("front") or []), sorted(self.made),
                         f"fixture: both children are on the screening front: {state}")
        for genome in ["seed", *self.made]:
            ran = sorted(r["case"] for r in self.runs if r["genome"] == genome and r["kind"] == "holdout")
            self.assertEqual(ran, ["h1", "h2"], f"{genome} ran on every held-out case: {self.calls()}")
        return state

    def test_the_later_child_wins_on_held_out_though_the_earlier_led_screening(self):  # covers: ADP-8
        state = self.run_plan(LATER_CHILD_WINS)
        best_on_screening, best_on_holdout = self.made
        self.assertEqual(state.get("winner"), best_on_holdout,
                         f"{best_on_screening} leads on screening, {best_on_holdout} on the held-out runs: {state}")

    def test_the_earlier_child_wins_on_held_out_though_the_later_led_screening(self):  # covers: ADP-8
        state = self.run_plan(EARLIER_CHILD_WINS)
        best_on_holdout, best_on_screening = self.made
        self.assertEqual(state.get("winner"), best_on_holdout,
                         f"{best_on_screening} leads on screening, {best_on_holdout} on the held-out runs: {state}")


# The toy target as a git repository, with a stand-in GitHub, check and pull-request command --------------------------

class ToyRepoBase(ac.Base):
    def setUp(self):
        self.stand = tempfile.mkdtemp(prefix="toy-standins-")
        self.addCleanup(shutil.rmtree, self.stand, True)
        self.check_log = os.path.join(self.stand, "checks.jsonl")
        self.pr_log = os.path.join(self.stand, "pr-calls.jsonl")
        self.record_check = self.script("record-check", RECORD_CHECK.replace("@LOG@", repr(self.check_log)))
        self.pr_command = self.script("pr-standin", tp.PR_STANDIN.replace("@LOG@", repr(self.pr_log))
                                      .replace("@URL@", repr(tp.PR_URL)))
        gn = gs.import_scripts("genome")
        real_seed, real_mkdtemp = gn.seed, tempfile.mkdtemp

        def seed(*args, **kwargs):
            source = args[2] if len(args) > 2 else kwargs["source"]
            if not os.path.isdir(os.path.join(source, ".git")):
                git(source, "init", "-q", "-b", "main")
                git(source, "add", "-A")
                git(source, "commit", "-q", "-m", "toy target")
            return real_seed(*args, **kwargs)

        def mkdtemp(*args, **kwargs):
            if str(kwargs.get("prefix") or "").startswith("regista"):
                kwargs["prefix"] = "toy-report-promote-"
            return real_mkdtemp(*args, **kwargs)

        with mock.patch.object(gn, "seed", seed), mock.patch.object(tempfile, "mkdtemp", mkdtemp), \
                mock.patch.object(ac, "PROFILE", {**ac.PROFILE, "checks": [self.record_check]}):
            super().setUp()
        self.assertNotIn("regista", self.tmp.lower(), "fixture: the temporary folder's name is neutral")
        self.repo = self.source
        self.base_commit = git(self.repo, "rev-parse", "HEAD")
        seed_meta = ac.read_json(os.path.join(self.evo, "genomes", "seed", "genome.json"))
        self.assertEqual((seed_meta.get("base") or {}).get("commit"), self.base_commit,
                         "fixture: the seed's base.commit is the toy repository's HEAD")
        settings = {"population": 2, "require_sandbox": False, "pr_command": self.pr_command,
                    "promote_remote": "origin"}
        ac.write(os.path.join(self.evo, "evolve.json"), json.dumps(settings) + "\n")
        self.config = self.ev.load_config(self.evo)
        self.bare = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        git(self.repo, "remote", "add", "origin", self.bare)
        git(self.repo, "push", "-q", "origin", "main")
        self.plan.update(LATER_CHILD_WINS)

    def script(self, name, text):
        path = os.path.join(self.stand, "bin", name)
        ac.write(path, text.replace("@PYTHON@", sys.executable))
        os.chmod(path, 0o755)
        return path

    def toy_adapter(self):
        adapter = self.adapter()
        adapter.headers = dict(HEADERS)
        adapter.run_word = RUN_WORD
        return adapter

    def finished_generation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            stop = self.generation(adapter=self.toy_adapter())
        self.assertIsNone(stop, f"fixture: the generation finished: {self.state()}")
        state = self.state()
        self.assertEqual(state.get("winner"), self.made[1], f"fixture: child 2 wins: {state}")
        self.winner = state["winner"]
        with open(os.path.join(self.evo, "genomes", self.winner, "files", TARGET), encoding="utf-8") as f:
            self.winner_prompt = f.read()
        self.assertNotEqual(self.winner_prompt, ac.SEED_PROMPT, "fixture: the winner changed prompt.md")
        return state

    # Observing ------------------------------------------------------------------------------------------------------

    def remote_refs(self):
        out = git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def local_refs(self):
        out = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def worktrees(self):
        out = git(self.repo, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def user_state(self):
        return {"branch": git(self.repo, "symbolic-ref", "--short", "HEAD"),
                "head": git(self.repo, "rev-parse", "HEAD"),
                "status": git(self.repo, "status", "--porcelain", "--untracked-files=all"),
                "files": gs.snapshot(self.repo, skip_git=True)}

    def json_lines(self, path):
        return [json.loads(line) for line in tp.read_lines(path)]


# ADP-2: a toy adapter's report and promote go through the core --------------------------------------------------------

class ToyReport(ToyRepoBase):
    def test_the_report_uses_the_adapters_axes_headers_and_run_word(self):  # covers: ADP-2
        state = self.finished_generation()
        text = self.core.report_text(self.evo, state, self.toy_adapter())
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "the report isn't empty")
        self.assertNotRegex(text, FORBIDDEN, f"the report names nothing of Regista's:\n{text}")
        self.assertNotRegex(text, r"(?i)requirement|approv|review.round|findings",
                            f"none of Regista's axes appear:\n{text}")

        header_rows = [cells(line) for line in text.splitlines()
                       if line.startswith("|") and all(h in cells(line) for h in EXPECTED_HEADERS)]
        self.assertTrue(header_rows, f"a table has the adapter's headers as columns {EXPECTED_HEADERS}:\n{text}")
        for row in header_rows:
            self.assertEqual([c for c in row if c in EXPECTED_HEADERS], EXPECTED_HEADERS,
                             f"the columns follow adapter.axes' order: {row}")
        self.assertRegex(text, rf"(?i)\b{RUN_WORD}s?\b", f"the adapter's run word is used:\n{text}")

        # The held-out results: the winner's 9,000 tokens and the base's 13,000 appear only in held-out runs.
        rows = [line for line in text.splitlines() if line.startswith("|")]
        self.assertTrue(any(re.search(word(self.winner), r) and tg.mentions(r, 9000) for r in rows),
                        f"a table row gives the winner's held-out results:\n{text}")
        self.assertTrue(any(re.search(word("seed"), r) and tg.mentions(r, 13000) for r in rows),
                        f"a table row gives the base's held-out results:\n{text}")

        # The screening against held-out gap for the winner: 5,000 on screening, 9,000 held out, 4,000 apart.
        gap = section(text, re.compile(r"(?i)\bgap\b|screening\b.*held[- ]out|held[- ]out\b.*screening"))
        self.assertIsNotNone(gap, f"the report has a section comparing screening and held-out results:\n{text}")
        self.assertRegex(gap, word(self.winner), f"it covers the winner:\n{gap}")
        for n, what in ((5000, "screening tokens"), (9000, "held-out tokens"), (4000, "the tokens gap")):
            self.assertTrue(tg.mentions(gap, n), f"the winner's {what} ({n:,}):\n{gap}")


class ToyPromote(ToyRepoBase):
    def promote(self, state, adapter):
        """evolve_core.promote onto main with the stand-in pull-request command; returns the printed text."""
        params = inspect.signature(self.core.promote).parameters
        extra = {name: value for name, value in (("pr_command", self.pr_command), ("remote", "origin"),
                                                 ("promote_remote", "origin")) if name in params}
        config = dict(self.config, pr_command=self.pr_command, promote_remote="origin")
        env = {**tp.IDENTITY, **gs.GIT_ENV}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                self.core.promote(self.evo, config, state, self.winner, adapter, self.profile, self.repo, "main",
                                  **extra)
            except self.core.EvolveError as e:
                self.fail(f"promote refused: {e}\n{buf.getvalue()}")
        out = buf.getvalue()
        self.assertNotIn("Traceback", out)
        return out

    def test_promote_puts_the_winner_and_the_report_on_a_new_branch_and_opens_a_pull_request(self):  # covers: ADP-2
        state = self.finished_generation()
        adapter = self.toy_adapter()
        before_user, before_local, before_remote = self.user_state(), self.local_refs(), self.remote_refs()
        out = self.promote(state, adapter)

        # One new local branch, named for the winner, one commit on top of main; main untouched everywhere.
        new = {ref: sha for ref, sha in self.local_refs().items() if ref not in before_local}
        self.assertEqual(len(new), 1, f"one new branch in the repository: {self.local_refs()}\n{out}")
        (ref, tip), = new.items()
        branch = ref[len("refs/heads/"):]
        self.assertIn(self.winner, branch, "the branch is named for the winner")
        self.assertEqual({k: v for k, v in self.local_refs().items() if k != ref}, before_local,
                         "main and every other local branch are unchanged")
        self.assertEqual(git(self.repo, "rev-parse", f"{tip}^"), self.base_commit, "one commit on top of main")
        self.assertEqual(self.user_state(), before_user, "the user's checkout is unchanged")
        self.assertEqual(len(self.worktrees()), 1, f"no temporary worktree is left: {self.worktrees()}")

        # The branch holds the winner's changed file and the report, and nothing names Regista.
        report_path = f"evolve-reports/{GEN}.md"
        changed = sorted(git(self.repo, "diff", "--name-only", "main", tip).splitlines())
        self.assertEqual(changed, sorted([TARGET, report_path]), "only the changed file and the report")
        self.assertEqual(git(self.repo, "show", f"{tip}:{TARGET}") + "\n", self.winner_prompt)
        report = git(self.repo, "show", f"{tip}:{report_path}")
        self.assertRegex(report, word(self.winner), "the report names the winner")
        message = git(self.repo, "log", "-1", "--format=%B", tip)
        diff = git(self.repo, "diff", "main", tip)
        for what, content in (("the branch name", branch), ("the commit message", message), ("the diff", diff),
                              ("the report", report)):
            self.assertNotRegex(content, FORBIDDEN, f"{what} names nothing of Regista's:\n{content}")

        # The profile's check ran in a temporary worktree holding the winner's file.
        checks = self.json_lines(self.check_log)
        self.assertTrue(checks, f"the profile's check ran:\n{out}")
        for call in checks:
            self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo),
                                "the check runs in a temporary worktree, not in the user's checkout")
            self.assertEqual(call["prompt"], self.winner_prompt, "the worktree holds the winner's file")

        # The stand-in GitHub gains only this branch, and the pull request is asked for once, against main.
        after = self.remote_refs()
        self.assertEqual({k: v for k, v in after.items() if k != ref}, before_remote, "main on the remote is unchanged")
        self.assertEqual(after.get(ref), tip, f"the branch is pushed to the stand-in GitHub:\n{out}")
        calls = self.json_lines(self.pr_log)
        self.assertEqual(len(calls), 1, f"one pull request: {calls}\n{out}")
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertEqual(argv[argv.index("--head") + 1], branch, argv)
        self.assertEqual((calls[0]["body"] or "").strip(), report.strip(), "the pull request's body is the report")
        self.assertNotRegex(" ".join(argv), FORBIDDEN, f"the pull request's title names nothing of Regista's: {argv}")


# ADP-9: an adapter kind evolve.py doesn't know -------------------------------------------------------------------------

class UnknownAdapterKind(ToyRepoBase):
    def setUp(self):
        super().setUp()
        self.finished_generation()
        path = os.path.join(self.evo, "runs", GEN, "generation.json")
        state = ac.read_json(path)
        state["adapter"] = {"kind": UNKNOWN_KIND, "suite": os.path.join(self.tmp, "suite.json")}
        ac.write(path, json.dumps(state, indent=2) + "\n")
        self.state_with_kind = state

    def invoke(self, argv):
        env = {**tp.IDENTITY, **gs.GIT_ENV}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(argv)
            except SystemExit as e:
                code = e.code
        return (0 if code is None else code), buf.getvalue()

    def assert_refused_and_nothing_written(self, argv):
        before_home, before_user = gs.snapshot(self.evo), self.user_state()
        before_local, before_remote = self.local_refs(), self.remote_refs()
        code, out = self.invoke(argv)
        self.assertNotEqual(code, 0, f"{argv[0]} refuses an unknown adapter kind:\n{out}")
        self.assertNotIn("Traceback", out)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"a plain one-line error:\n{out}")
        self.assertIn(UNKNOWN_KIND, lines[0], f"the error names the kind:\n{out}")
        self.assertEqual(gs.snapshot(self.evo), before_home, f"nothing is written in the evolver's home:\n{out}")
        self.assertEqual(self.user_state(), before_user, "the repository's checkout is unchanged")
        self.assertEqual(self.local_refs(), before_local, "no branch is made")
        self.assertEqual(self.remote_refs(), before_remote, "nothing is pushed")
        self.assertEqual(len(self.worktrees()), 1, "no worktree is left")
        self.assertEqual(tp.read_lines(self.check_log), [], "no check ran")
        self.assertEqual(tp.read_lines(self.pr_log), [], "no pull request is asked for")

    def test_adapter_for_state_names_the_unknown_kind(self):  # covers: ADP-9
        with self.assertRaises(self.ev.EvolveError) as caught:
            self.ev.adapter_for_state(self.evo, self.config, self.state_with_kind)
        self.assertIn(UNKNOWN_KIND, str(caught.exception))
        self.assertEqual(len(str(caught.exception).strip().splitlines()), 1, "one line")

    def test_report_refuses_an_unknown_adapter_kind_and_writes_nothing(self):  # covers: ADP-9
        self.assert_refused_and_nothing_written(["report", GEN])

    def test_promote_refuses_an_unknown_adapter_kind_and_writes_nothing(self):  # covers: ADP-9
        self.assert_refused_and_nothing_written(["promote", self.winner, "--generation", GEN, "--repo", self.repo,
                                                 "--onto", "main"])


if __name__ == "__main__":
    unittest.main()
