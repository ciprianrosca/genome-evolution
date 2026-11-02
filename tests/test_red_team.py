"""Red-team generations (spec .regista/specs/red-team.json: LOOP-7, the light version).

`evolve.py generation --red-team ...` runs a generation marked as red-team: it needs the same confirmation as any
generation (a terminal answer or --yes); runs/<g>/generation.json holds "red_team": true, and so does each child's
mutation.json; the printed plan and the report (`evolve.py report <g>`) say in their first sentence that it is a
red-team generation whose results can never become a pull request. Continuing a red-team generation without the flag,
or a normal one with it, is refused with a plain message. `promote` refuses, before anything is created, a genome from
a red-team generation and any genome with an ancestor made in one. Scoring is unchanged.

Fixtures: tests/test_generation.py's in-process runner (fake replay_run, real mutate around the stand-in claude, a
hand-built bench, Regista's real profile over the fake Regista checkout) and tests/test_promote.py's fixture (that
checkout as a git repository with a bare remote, a recording git first on PATH, stand-in checks and pr_command). The
generation plan is test_generation's Winner: child 2 wins. Everything lives in temporary folders.

Interface assumed: the printed plan is the first thing the generation command prints; a "sentence" ends at the first
".", "!" or "?" followed by a space or the end of its paragraph, and a paragraph is the first block of text after any
Markdown heading lines; the red-team refusals say "red-team" (or "red team"); promote's refusal of a descendant names
the red-team ancestor or its generation; a normal generation's mutation.json has no "red_team" or a false one.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_red_team -v"""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_generation as tg  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_promote as tp  # noqa: E402

RT_GEN = "rt-gen"
RED_TEAM = re.compile(r"(?i)red[- ]team")
PULL_REQUEST = re.compile(r"(?i)pull request|\bPR\b")
NEVER = re.compile(r"(?i)\bnever\b|\bnot\b|n't\b|\bcannot\b|\bno\b")
WINNER_PLAN = {("base", "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 10000},
               (1, "screen"): {"outcome": "approved", "used": 20000},
               (2, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 5000},
               ("base", "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 13000},
               (1, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
               (2, "holdout"): {"outcome": "approved", "used": 12000}}


def first_sentence(text):
    """The first sentence of the first block of text after any Markdown heading lines."""
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines:
            paragraph = " ".join(lines)
            match = re.match(r"(.+?[.!?])(?:\s|$)", paragraph)
            return match.group(1) if match else paragraph
    return ""


class Base(tg.Base):
    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = dict(WINNER_PLAN)

    def assert_says_red_team_never_pr(self, text, where):
        sentence = first_sentence(text)
        self.assertRegex(sentence, RED_TEAM, f"{where}: the first sentence says it is a red-team generation:\n{text}")
        self.assertRegex(sentence, PULL_REQUEST, f"{where}: and that its results can't become a pull request:\n{text}")
        self.assertRegex(sentence, NEVER, f"{where}: (never):\n{text}")

    def mutation(self, child):
        return self.read_json(self.genome_dir(child), "mutation.json")

    def assert_refused_continuation(self, code, out, gen, before):
        self.assertNotEqual(code, 0, f"continuing with a different red-team setting is refused:\n{out}")
        self.assertNotIn("Traceback", out, "a plain message, not a crash")
        self.assertNotRegex(out, r"(?im)unrecognized arguments|^usage:", f"the generation refuses, not the parser:\n{out}")
        self.assertRegex(out, RED_TEAM, f"the message says why (red-team):\n{out}")
        self.assertEqual(self.calls_in(self.run_number), [], f"no replay ran:\n{out}")
        self.assertEqual([m for m in self.mutations if m["run"] == self.run_number], [], "no child was made")
        with open(os.path.join(self.evo, "runs", gen, "generation.json"), "rb") as f:
            self.assertEqual(f.read(), before, "generation.json is unchanged")

    def generation_json_bytes(self, gen):
        with open(os.path.join(self.evo, "runs", gen, "generation.json"), "rb") as f:
            return f.read()

    def interrupted(self, gen, extra=()):
        """A generation stopped (Ctrl-C) during the first child's first screening replay."""
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1
        self.generation(gen=gen, extra=extra)
        self.interrupt = None
        self.assertEqual(len(self.made), 1, "fixture: one child made before the stop")
        self.assertIsNone(self.state(gen).get("winner"), "fixture: the generation hasn't finished")


class Confirmation(Base):
    def test_without_a_terminal_or_yes_a_red_team_generation_runs_nothing(self):  # covers: LOOP-7
        r = subprocess.run([tg.PY, tg.EVOLVE, "generation", "--red-team", "--target", tg.TARGET, "--budget",
                            "50000000", "--id", RT_GEN, "--seed", "7"], cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", timeout=120,
                           env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("--yes", out, "the refusal says how to confirm without a terminal")
        folder = os.path.join(self.evo, "runs", RT_GEN)
        cases = [d for d in (os.listdir(folder) if os.path.isdir(folder) else [])
                 if os.path.isdir(os.path.join(folder, d))]
        self.assertEqual(cases, [], f"no replay started:\n{out}")
        self.assertEqual(sorted(g for g in os.listdir(os.path.join(self.evo, "genomes")) if g != "discarded.jsonl"),
                         ["seed"], f"no child made:\n{out}")
        self.assertEqual(self.calls(), [], "claude was never started")


class Marked(Base):
    def test_red_team_generation_is_marked_in_its_state_children_plan_and_report(self):  # covers: LOOP-7
        code, out = self.generation(gen=RT_GEN, extra=("--red-team",))
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.made), 2, f"two children: {self.mutations}\n{out}")
        state = self.state(RT_GEN)
        self.assertIs(state.get("red_team"), True, f"generation.json holds red_team true: {state}")
        self.assertEqual(state.get("winner"), self.made[1], f"scoring is unchanged (child 2 wins): {state}")
        for child in self.made:
            self.assertIs(self.mutation(child).get("red_team"), True, f"{child}'s mutation.json holds red_team true")
        self.assert_says_red_team_never_pr(out, "the printed plan")
        code, report = self.invoke(["report", RT_GEN])
        self.assertEqual(code, 0, report)
        path = os.path.join(self.evo, "reports", RT_GEN + ".md")
        self.assertTrue(os.path.isfile(path), f"reports/{RT_GEN}.md is written:\n{report}")
        with open(path, encoding="utf-8") as f:
            self.assert_says_red_team_never_pr(f.read(), "the report")


class Continuing(Base):
    def test_a_red_team_generation_continues_only_as_red_team(self):  # covers: LOOP-7
        self.interrupted(RT_GEN, extra=("--red-team",))
        self.assertIs(self.state(RT_GEN).get("red_team"), True)
        before = self.generation_json_bytes(RT_GEN)
        code, out = self.generation(gen=RT_GEN)
        self.assert_refused_continuation(code, out, RT_GEN, before)
        code, out = self.generation(gen=RT_GEN, extra=("--red-team",))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.made), 2, f"the continued generation made its second child:\n{out}")
        self.assertIs(self.state(RT_GEN).get("red_team"), True, "a continued generation keeps red_team")
        for child in self.made:
            self.assertIs(self.mutation(child).get("red_team"), True, f"{child}'s mutation.json holds red_team true")

    def test_a_normal_generation_cannot_be_continued_as_red_team(self):  # covers: LOOP-7
        self.interrupted(tg.GEN)
        self.assertFalse(self.state(tg.GEN).get("red_team"), "a normal generation isn't marked red-team")
        self.assertFalse(self.mutation(self.made[0]).get("red_team"), "nor is its child")
        before = self.generation_json_bytes(tg.GEN)
        code, out = self.generation(gen=tg.GEN, extra=("--red-team",))
        self.assert_refused_continuation(code, out, tg.GEN, before)


class PromoteRefuses(tp.PromoteBase):
    """A red-team generation (child 2 of the seed wins), then a normal generation from that winner (its child 2
    wins), over the git checkout with a bare remote."""

    def setUp(self):
        super().setUp()
        code, out = self.run_generation(gen=RT_GEN, extra=("--red-team",))
        self.assertEqual(code, 0, out)
        self.rt_winner = self.state(RT_GEN).get("winner")
        self.assertEqual(self.rt_winner, self.made[1], f"fixture: child 2 wins the red-team generation:\n{out}")

    def promote_from(self, genome, gen):
        argv = ["promote", genome, "--generation", gen, "--repo", self.target_repo]
        env = {"PATH": self.git_bin + os.pathsep + os.environ.get("PATH", ""), **tp.IDENTITY, **tp.seedtests.GIT_ENV}
        with mock.patch.dict(os.environ, env):
            code, out = self.invoke(argv)
        self.assertNotIn("Traceback", out)
        return code, out

    def test_the_winner_of_a_red_team_generation_is_refused_before_anything_is_created(self):  # covers: LOOP-7
        self.add_remote()
        before = self.user_state(), self.remote_refs()
        code, out = self.promote_from(self.rt_winner, RT_GEN)
        self.assert_refused(code, out, *before, RED_TEAM, names_file=False)

    def test_a_descendant_of_a_red_team_child_is_refused_before_anything_is_created(self):  # covers: LOOP-7
        # The red-team winner is role 2; its children in the normal generation are roles 3 and 4, and 4 wins.
        self.plan.update({(2, "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 13000},
                          (3, "screen"): {"outcome": "approved", "used": 20000},
                          (4, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 3000},
                          (3, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
                          (4, "holdout"): {"outcome": "approved", "used": 12000}})
        code, out = self.run_generation(extra=("--from", self.rt_winner))
        self.assertEqual(code, 0, out)
        state = self.state()
        self.assertFalse(state.get("red_team"), f"fixture: the second generation is a normal one: {state}")
        winner = state.get("winner")
        self.assertEqual(winner, self.made[3], f"fixture: the red-team winner's child 2 wins: {state}\n{out}")
        self.add_remote()
        before = self.user_state(), self.remote_refs()
        code, out = self.promote_from(winner, tg.GEN)
        self.assert_refused(code, out, *before, RED_TEAM, names_file=False)
        self.assertTrue(re.search(rf"(?<![\w-]){re.escape(self.rt_winner)}(?![\w-])", out) or RT_GEN in out,
                        f"the message names the red-team ancestor or its generation:\n{out}")


if __name__ == "__main__":
    unittest.main()
