"""The neutral core and promote's hardening (spec .regista/specs/adapter-neutral.json: ADP-4, ADP-10, ADP-11).

ADP-4: scripts/evolve_core.py and scripts/genome.py are read as text. Neither may contain (case-insensitive, as a word
or part of a word) a word that names a target or an agent; evolve_core.py may also not contain bench, replay or plugin,
except on the line that defines generation.json's spent key, `SPENT_RUNS = "replays"`. Neither may import anything but
the standard library, and evolve_core.py may also import genome. A failure names the file, the line number and the word.
The checker is tested on planted text too, so a check that silently finds nothing can't pass.

ADP-10: evolve_core.promote refuses with an EvolveError, before any branch, worktree, check, push or pull request:
- a generation id (generation.json's `id`, given to promote as state['id']) that isn't a plain folder name: letters,
  digits, _ . -, starting with a letter or digit;
- an adapter whose report_path(gen_id) is absolute, empty, or has a `..` part.
These reuse tests/test_adapter_report_promote.py's fixture (imported as a module, so its tests don't run twice): the toy
target as a git repository on main with a local bare repository as origin, a recording check, and a stand-in
pull-request command. No claude runs and no tokens are spent; HOME and REGISTA_EVOLVE_HOME are temporary folders.

ADP-11: evolve_core.Generation given a held-out list shorter than the adapter's records 'light': true and
'holdout_cases' (the ids it used) in the state, and promote accepts that run's winner; a full run writes neither key.
When the adapter's cases() raises, report_text still renders from the cases the generation recorded.

Interface assumed: promote(home, config, state, genome, adapter, profile, repo, onto, checks=None, remote='origin',
pr_command=..., ...) as the spec gives it; report_path is a method of the adapter taking the generation id, so these
tests replace it on the instance.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_adapter_neutral -v"""
import ast
import contextlib
import io
import json
import os
import re
import shutil
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_adapter_core as ac  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_adapter_report_promote as rp  # noqa: E402
import test_genome_seed as gs  # noqa: E402
import test_promote as tp  # noqa: E402

# ADP-4: the neutrality check ------------------------------------------------------------------------------------------

TARGET_WORDS = ("regista", "daedalus", "fpl", "klotho", "personal-assistants", "personal_assistants", "artemis",
                "skill.md", "claude", "anthropic")
CORE_WORDS = ("bench", "replay", "plugin")
SPENT_LINE = 'SPENT_RUNS = "replays"'
MODULES = {
    "evolve_core.py": {"words": TARGET_WORDS + CORE_WORDS, "local": {"genome"}},
    "genome.py": {"words": TARGET_WORDS, "local": set()},
}


def forbidden_words(name, text, words):
    """(line number, word, line) for each forbidden word in text; a word may be part of a longer word. The spent key's
    line in evolve_core.py may hold bench, replay or plugin, but no other forbidden word."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        lower = line.lower()
        exempt = CORE_WORDS if name == "evolve_core.py" and line.strip() == SPENT_LINE else ()
        for word in words:
            if word not in exempt and word in lower:
                found.append((number, word, line.strip()))
    return found


def imported_modules(text):
    """(line number, top-level module name) for every import in the module, nested ones included, and for
    importlib.import_module / __import__ calls with a literal name. A relative `from . import x` counts as x."""
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
            else:
                found.append((node.lineno, (node.module or "").split(".")[0]))
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if called in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                found.append((node.lineno, node.args[0].value.split(".")[0]))
    return found


def neutrality_problems(name, text):
    """One sentence per problem, naming the file, the line number and the word (or module)."""
    rules = MODULES[name]
    problems = [f"scripts/{name}, line {number}: the word '{word}' names a target or an agent: {line}"
                for number, word, line in forbidden_words(name, text, rules["words"])]
    allowed = set(sys.stdlib_module_names) | rules["local"]
    problems += [f"scripts/{name}, line {number}: imports '{module}', which is neither the standard library nor "
                 f"an allowed local module" for number, module in imported_modules(text) if module not in allowed]
    return problems


class Neutrality(unittest.TestCase):
    def test_evolve_core_and_genome_name_no_target_or_agent_and_import_only_the_standard_library(self):
        # covers: ADP-4
        for name in MODULES:
            with self.subTest(module=name):
                path = os.path.join(SCRIPTS, name)
                self.assertTrue(os.path.isfile(path), f"scripts/{name} exists")
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                problems = neutrality_problems(name, text)
                self.assertEqual(problems, [], "\n".join(problems))

    def test_the_check_names_the_file_line_and_word_of_each_planted_word(self):  # covers: ADP-4
        planted = ["x = 1", "# Runs a Regista step.", "PATH = '~/personal-assistants/agents'",
                   "MODEL = 'claude-opus'", "doc = 'see SKILL.md'", "who = 'Anthropic'", "y = 'fpl-tool'",
                   "z = 'klotho_app'", "a = 'daedalus'", "b = 'Artemis'", "c = 'personal_assistants'"]
        text = "\n".join(planted) + "\n"
        for name in MODULES:
            problems = neutrality_problems(name, text)
            for number, word in ((2, "regista"), (3, "personal-assistants"), (4, "claude"), (5, "skill.md"),
                                 (6, "anthropic"), (7, "fpl"), (8, "klotho"), (9, "daedalus"), (10, "artemis"),
                                 (11, "personal_assistants")):
                with self.subTest(module=name, word=word):
                    self.assertTrue(any(f"scripts/{name}" in p and f"line {number}:" in p and f"'{word}'" in p
                                        for p in problems), problems)
            self.assertFalse(any("line 1:" in p for p in problems), f"a clean line isn't reported: {problems}")

    def test_bench_replay_and_plugin_are_forbidden_in_the_core_except_on_the_spent_key_line(self):  # covers: ADP-4
        text = "\n".join(["RESULTS = 'bench-cases'", "def replay_one():", '    SPENT_RUNS = "replays"',
                          "KIND = 'Plugin'", SPENT_LINE, 'OTHER = "replays"', 'SPENT_RUNS = "replays"  # claude']) + "\n"
        problems = neutrality_problems("evolve_core.py", text)
        for number, word in ((1, "bench"), (2, "replay"), (4, "plugin"), (6, "replay"), (7, "claude")):
            with self.subTest(word=word, line=number):
                self.assertTrue(any(f"line {number}:" in p and f"'{word}'" in p for p in problems), problems)
        self.assertFalse(any(re.search(r"line [35]:", p) for p in problems),
                         f"the spent key's own line is allowed: {problems}")
        self.assertEqual(neutrality_problems("genome.py", text.replace("# claude", "")), [],
                         "bench, replay and plugin are only forbidden in evolve_core.py")

    def test_the_check_refuses_imports_outside_the_standard_library(self):  # covers: ADP-4
        text = "\n".join(["import json, os.path", "from collections import OrderedDict", "import genome",
                          "import evolve", "from regista import x", "def f():", "    import requests",
                          "    return importlib.import_module('yaml')", "from . import genome"]) + "\n"
        core = neutrality_problems("evolve_core.py", text)
        for number, module in ((4, "evolve"), (5, "regista"), (7, "requests"), (8, "yaml")):
            with self.subTest(module=module):
                self.assertTrue(any(f"line {number}:" in p and f"'{module}'" in p and "import" in p for p in core),
                                core)
        self.assertFalse(any(re.search(r"line [1239]:.*import", p) for p in core),
                         f"the standard library and genome are allowed in the core: {core}")
        plain = neutrality_problems("genome.py", text)
        self.assertTrue(any("line 3:" in p and "'genome'" in p for p in plain),
                        f"genome.py may import only the standard library: {plain}")


# ADP-10: promote refuses an unsafe generation id or report path ------------------------------------------------------

# Generation ids that aren't plain folder names. For each one that can name a folder under runs/ (all but the empty,
# absolute and None ones), the test copies the finished generation's run folder there first, so promote can't refuse it
# for looking unfinished: only the id check can.
BAD_IDS = ["../escape", "a/b", ".hidden", "-dash", "_under", "gen id", "gen\nid", "", "..", "/abs-gen", None]
# git subcommands that make a branch, a worktree, a commit or a push (`git worktree list`, `git branch --list` and
# other questions are allowed).
WRITING_GIT = {"checkout", "switch", "commit", "push", "update-ref", "add", "fetch"}


def writes(call):
    sub, rest = tp.subcommand(call)
    if sub == "worktree":
        return rest[:1] == ["add"]
    if sub == "branch":
        return any(not a.startswith("-") for a in rest)
    return sub in WRITING_GIT


class PromoteRefusesUnsafePaths(rp.ToyRepoBase):
    def setUp(self):
        super().setUp()
        self.finished = self.finished_generation()
        self.outside = os.path.join(self.tmp, "outside.md")
        real_git = shutil.which("git")
        self.assertTrue(real_git, "fixture: git is installed")
        self.git_log = os.path.join(self.stand, "git-calls.log")
        self.git_bin = os.path.join(self.stand, "gitbin")
        ac.write(os.path.join(self.git_bin, "git"),
                 tp.GIT_WRAPPER.replace("@LOG@", self.git_log).replace("@GIT@", real_git))
        os.chmod(os.path.join(self.git_bin, "git"), 0o755)

    def git_calls(self):
        return [line.split("\x1f")[:-1] for line in tp.read_lines(self.git_log)]

    def refused(self, state, adapter):
        """Calls promote, with a recording git first on PATH, expecting an EvolveError; returns its message, what
        promote printed and the git commands it ran."""
        config = dict(self.config, pr_command=self.pr_command, promote_remote="origin")
        env = {"PATH": self.git_bin + os.pathsep + os.environ.get("PATH", ""), **tp.IDENTITY, **gs.GIT_ENV}
        if os.path.exists(self.git_log):
            os.remove(self.git_log)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with self.assertRaises(self.core.EvolveError, msg=f"promote refuses:\n{buf.getvalue()}") as caught:
                self.core.promote(self.evo, config, state, self.winner, adapter, self.profile, self.repo, "main",
                                  remote="origin", pr_command=(self.pr_command,))
        return str(caught.exception), buf.getvalue(), self.git_calls()

    def assert_refused_before_anything(self, state, adapter, label):
        before_user, before_local, before_remote = self.user_state(), self.local_refs(), self.remote_refs()
        before_checks, before_prs = tp.read_lines(self.check_log), tp.read_lines(self.pr_log)
        message, out, calls = self.refused(state, adapter)
        self.assertTrue(message.strip(), f"{label}: the refusal has a message")
        self.assertNotIn("Traceback", message + out, label)
        writing = [c for c in calls if writes(c)]
        self.assertEqual(writing, [], f"{label}: refused before any branch, worktree, commit or push: {message}")
        self.assertEqual(self.local_refs(), before_local, f"{label}: no branch is made:\n{out}")
        self.assertEqual(self.remote_refs(), before_remote, f"{label}: nothing is pushed:\n{out}")
        self.assertEqual(len(self.worktrees()), 1, f"{label}: no worktree is made: {self.worktrees()}")
        self.assertEqual(self.user_state(), before_user, f"{label}: the user's checkout is unchanged")
        self.assertEqual(tp.read_lines(self.check_log), before_checks, f"{label}: no check ran")
        self.assertEqual(tp.read_lines(self.pr_log), before_prs, f"{label}: no pull request is asked for")
        self.assertFalse(os.path.lexists(self.outside), f"{label}: nothing is written outside the repository")

    def test_promote_refuses_a_generation_id_that_isnt_a_plain_folder_name(self):  # covers: ADP-10
        runs = os.path.join(self.evo, "runs")
        original = os.path.join(runs, ac.GEN)
        for bad in BAD_IDS:
            with self.subTest(gen_id=bad):
                state = dict(self.finished, id=bad)
                copied = None
                if isinstance(bad, str) and bad and bad != ".." and not os.path.isabs(bad):
                    copied = os.path.join(runs, bad)
                    shutil.copytree(original, copied)
                    ac.write(os.path.join(copied, "generation.json"), json.dumps(state, indent=2) + "\n")
                try:
                    self.assert_refused_before_anything(state, self.toy_adapter(), f"generation id {bad!r}")
                finally:
                    if copied:
                        shutil.rmtree(copied, True)

    def test_promote_refuses_an_absolute_empty_or_escaping_report_path(self):  # covers: ADP-10
        for bad in [self.outside, os.sep + "outside.md", "", "../outside.md", "evolve-reports/../../outside.md",
                    "reports/../x.md"]:
            with self.subTest(report_path=bad):
                adapter = self.toy_adapter()
                adapter.report_path = lambda gen_id, bad=bad: bad
                self.assert_refused_before_anything(dict(self.finished), adapter, f"report path {bad!r}")


class LightRunThroughTheCore(rp.ToyRepoBase):
    """ADP-11: a Generation given a held-out list shorter than the adapter's records light and the ids it used, and
    promote accepts its winner; a full run records neither key."""

    def test_a_light_run_records_its_held_out_cases_and_promote_accepts_its_winner(self):  # covers: ADP-11
        self.assertEqual([c["id"] for c in self.holdout], ["h1", "h2"], "fixture: the adapter has two held-out cases")
        config = dict(self.config, population=1)
        with contextlib.redirect_stdout(io.StringIO()):
            stop = self.generation(adapter=self.toy_adapter(), config=config, holdout=self.holdout[:1])
        self.assertIsNone(stop, f"the light generation finished: {self.state()}")
        state = self.state()
        self.assertIs(state.get("light"), True, f"the state records light true: {state}")
        self.assertEqual(state.get("holdout_cases"), ["h1"], f"the state records the held-out ids it used: {state}")
        self.assertEqual(len(self.made), 1, f"population 1 makes one child: {self.mutations}")
        held_out = sorted(r["case"] for r in self.runs if r["kind"] == "holdout")
        self.assertNotIn("h2", held_out, "the skipped held-out case never runs")
        winner = state.get("winner")
        self.assertTrue(winner, f"fixture: the light run has a winner: {state}")

        before_local, before_remote = self.local_refs(), self.remote_refs()
        config = dict(config, pr_command=self.pr_command, promote_remote="origin")
        env = {**tp.IDENTITY, **gs.GIT_ENV}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                self.core.promote(self.evo, config, state, winner, self.toy_adapter(), self.profile, self.repo,
                                  "main", remote="origin", pr_command=(self.pr_command,))
            except self.core.EvolveError as e:
                self.fail(f"promote accepts the light run's winner, but refused: {e}\n{buf.getvalue()}")
        out = buf.getvalue()
        new = {ref: sha for ref, sha in self.remote_refs().items() if ref not in before_remote}
        self.assertEqual(len(new), 1, f"one branch is pushed to the stand-in GitHub:\n{out}")
        self.assertEqual(self.local_refs()["refs/heads/main"], before_local["refs/heads/main"], "main is unchanged")
        calls = self.json_lines(self.pr_log)
        self.assertEqual(len(calls), 1, f"one pull request: {calls}\n{out}")
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--base") + 1], "main")

    def test_a_full_run_records_neither_light_nor_holdout_cases(self):  # covers: ADP-11
        with contextlib.redirect_stdout(io.StringIO()):
            stop = self.generation(adapter=self.toy_adapter())
        self.assertIsNone(stop, f"fixture: the full generation finished: {self.state()}")
        state = self.state()
        self.assertNotIn("light", state, f"a full run writes no light key: {sorted(state)}")
        self.assertNotIn("holdout_cases", state, f"a full run writes no holdout_cases key: {sorted(state)}")


class ReportCasesFallBack(ac.Base):
    """ADP-11's second half: the default Adapter.report_cases falls back to the cases the generation recorded when the
    adapter's cases() raises, so report_text still renders."""

    def test_the_report_still_works_when_the_adapters_cases_raise(self):  # covers: ADP-11
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(self.generation(), f"fixture: the generation finished: {self.state()}")
        state = self.state()
        self.assertTrue(state.get("winner"), f"fixture: a winner: {state}")
        adapter = self.adapter()

        def broken_cases():
            raise RuntimeError("the case list is gone")

        adapter.cases = broken_cases
        text = self.core.report_text(self.evo, state, adapter)
        self.assertRegex(text, rp.word(state["winner"]), f"the report still names the winner:\n{text}")


if __name__ == "__main__":
    unittest.main()
