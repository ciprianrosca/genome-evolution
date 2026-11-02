"""The generation report and three fixes to the generation (spec .regista/specs/report.json: LOOP-17 and LOOP-18;
LOOP-8 is the end-to-end test in genome/tests/e2e/test_generation.py; LOOP-9 is checked by hand).

`evolve.py report <generation>` reads runs/<g>/generation.json, the children's mutation.json and the replays'
result.json, writes reports/<g>.md under REGISTA_EVOLVE_HOME and prints the same text.

These tests reuse tests/test_generation.py's fixture (imported as a module, so its tests don't run twice): the command
runs in-process (evolve.main) with a fake replay_run that writes result.json files and the real mutate wrapped around
the stand-in claude, in a temporary REGISTA_EVOLVE_HOME.

Interface assumed:
- `evolve.main(["report", <id>])` returns (or exits with) 0 and prints the report; the file is
  <REGISTA_EVOLVE_HOME>/reports/<id>.md; a missing generation is a non-zero exit with a short plain message.
- The report's first paragraph is its first block of text after any Markdown heading lines, up to a blank line.
- The winner's diff is a unified diff of the target file read from genomes/<id>/files/<target> (the genome's stored
  copy; this test keeps genomes/<id>/plugin/ and genome.json's size and sha256 in step when it rewrites a file).
- A replay stopped by Ctrl-C leaves a result.json with the tokens it used so far (outcome "interrupted", scores null)
  before the KeyboardInterrupt reaches the generation, and the rerun starts that replay again and overwrites it.
- The printed continue command is one shell line, `REGISTA_EVOLVE_HOME=<path> <python> .../evolve.py generation ...`,
  possibly inside backticks; the path is quoted for the shell when it holds a space.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_report -v"""
import difflib
import hashlib
import json
import os
import re
import shlex
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_generation as tg  # noqa: E402  (imported as a module, so its test classes don't run twice here)
import test_reflect_mutate as rm  # noqa: E402

GEN = tg.GEN
TARGET = tg.TARGET
UNFINISHED = re.compile(r"(?i)in progress|still running|not (yet )?(finished|complete)|unfinished|incomplete|"
                        r"interrupted|(hasn't|has not|isn't|is not|wasn't|was not|didn't|did not) (yet )?finish|"
                        r"stopped before")
# A claim that no child reached the screening front (only true when none did).
NO_CHILD_ON_FRONT = re.compile(
    r"(?i)\bno (child|children|child genome|new genome)s? (reached|made|made it to|got onto|got to|was on|is on|"
    r"were on|ended up on) the (screening |pareto )*front|"
    r"\bnone of the (children|child genomes) (reached|made|made it to|got onto|got to|was on|is on|were on|"
    r"ended up on) the (screening |pareto )*front|"
    r"\b(child|children) (didn't|did not|never|failed to|couldn't|could not) (reach|make|make it to|get onto|get to|"
    r"get on) the (screening |pareto )*front|"
    r"\b(only the base|the base alone)( genome)?( (is|was|reached|made it to|made))? (on )?the (screening |pareto )*front")
HELD_OUT = re.compile(r"(?i)held[- ]out")


def first_paragraph(text):
    """The report's first block of text after any heading lines, joined into one line."""
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines:
            return " ".join(lines)
    return ""


def word(name):
    """A regex for the genome or case name on its own (not inside a longer id such as seed-m1)."""
    return rf"(?<![\w-]){re.escape(name)}(?![\w-])"


class ReportBase(tg.Base):
    def report(self, gen=GEN):
        """Run `evolve.py report <gen>` in-process; returns (exit code, printed text, the file's text or None)."""
        code, out = self.invoke(["report", gen])
        path = os.path.join(self.evo, "reports", gen + ".md")
        text = None
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
        return code, out, text

    def assert_report(self, code, out, text, gen=GEN):
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(text, f"reports/{gen}.md is written:\n{out}")
        self.assertTrue(text.strip(), "the report isn't empty")
        self.assertIn(text.strip(), out, "the command prints the same text it writes")
        return first_paragraph(text)

    def replay_tokens(self, gen=GEN):
        """The tokens.used of every result.json now under runs/<gen>/."""
        total = 0
        folder = os.path.join(self.evo, "runs", gen)
        for dirpath, _, names in os.walk(folder):
            if "result.json" in names:
                total += (self.read_json(dirpath, "result.json").get("tokens") or {}).get("used", 0)
        return total


# LOOP-17: reports of unfinished generations, a missing generation and a long diff -------------------------------------

class UnfinishedReports(ReportBase):
    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "used": 10000},
                     "base": {"used": 10000}, 1: {"used": 10000}, 2: {"used": 10000}}

    def test_a_generation_stopped_by_the_budget_says_so_first_and_reports_what_exists(self):  # covers: LOOP-17
        # As in test_generation's Budget: the base's two screening replays (20,000) fit; the first reflection doesn't.
        self.past_replay("old-gen", self.screening[0], "seed", 10000)
        self.past_replay("old-gen", self.holdout[0], "seed", 10000)
        self.generation(budget=25000)
        state = self.state()
        self.assertEqual(state.get("stopped"), "budget", f"fixture: stopped by the budget: {state}")
        code, out, text = self.report()
        first = self.assert_report(code, out, text)
        self.assertRegex(first, r"(?i)budget", f"the first paragraph says the budget stopped it:\n{first}")
        self.assertRegex(first, r"(?i)\bwinner\b|\bwon\b|\bwin\b", f"and gives the outcome (no winner):\n{first}")
        self.assertRegex(text, word("seed"), "the base's screening results are reported")
        self.assertTrue(any(re.search(word("seed"), line) for line in text.splitlines() if line.startswith("|")),
                        f"the base has its row in the screening table:\n{text}")
        self.assertTrue(tg.mentions(text, 20000), f"what was spent (20,000):\n{text}")
        self.assertTrue(tg.mentions(text, 25000), f"and the budget (25,000):\n{text}")

    def test_a_generation_that_has_not_finished_says_so_first_and_reports_what_exists(self):  # covers: LOOP-17
        # Stopped (Ctrl-C) during the first child's first screening replay: generation.json holds the base's
        # screening and one child, with no front, held-out results or winner yet.
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1
        self.generation()
        self.assertEqual(len(self.made), 1, "fixture: one child made before the stop")
        state = self.state()
        self.assertIsNone(state.get("winner"), f"fixture: no winner yet: {state}")
        code, out, text = self.report()
        first = self.assert_report(code, out, text)
        self.assertRegex(first, UNFINISHED, f"the first paragraph says the generation hasn't finished:\n{first}")
        self.assertRegex(text, word("seed"), "the base's screening results are reported")
        self.assertRegex(text, word(self.made[0]), f"the child made so far is reported:\n{text}")

    def test_a_missing_generation_is_a_plain_error(self):  # covers: LOOP-17
        code, out, text = self.report("gen-missing")
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("gen-missing", out, "the message names the generation it couldn't find")
        self.assertLessEqual(len([line for line in out.splitlines() if line.strip()]), 5, f"one short message:\n{out}")
        self.assertIsNone(text, "no report is written for it")


class LongDiff(ReportBase):
    """test_generation's Winner fixture, where child 2 wins on the held-out cases; then the base's and the winner's
    builder prompts are replaced by 300-line texts that differ on every line, so the full diff is about 600 lines."""

    LINES = 300

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 10000},
                     (1, "screen"): {"outcome": "approved", "used": 20000},
                     (2, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 5000},
                     ("base", "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 10000},
                     (1, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
                     (2, "holdout"): {"outcome": "approved", "used": 10000}}

    def rewrite_target(self, genome, text):
        """Replace the genome's stored and expanded target file, keeping genome.json's size and hash in step."""
        data = text.encode("utf-8")
        stored = os.path.join(self.genome_dir(genome), "files", *TARGET.split("/"))
        self.assertTrue(os.path.isfile(stored), f"fixture: {genome} stores its target at files/{TARGET}")
        for path in (stored, os.path.join(self.genome_dir(genome), "plugin", *TARGET.split("/"))):
            if os.path.isfile(path):
                os.chmod(path, 0o644)
                with open(path, "wb") as f:
                    f.write(data)
        meta_path = os.path.join(self.genome_dir(genome), "genome.json")
        meta = self.read_json(meta_path)
        for entry in meta.get("files") or []:
            if entry.get("path") == TARGET:
                entry["sha256"] = hashlib.sha256(data).hexdigest()
                entry["bytes"] = len(data)
        os.chmod(meta_path, 0o644)
        rm.write_json(meta_path, meta)

    def test_the_winners_diff_is_at_most_200_lines_and_the_rest_is_summarised(self):  # covers: LOOP-17
        self.generation()
        winner = self.state().get("winner")
        self.assertEqual(winner, self.made[1], f"fixture: child 2 wins: {self.state()}")
        base_text = "".join(f"Base line {i:03d}: keep every step small.\n" for i in range(self.LINES))
        child_text = "".join(f"Child line {i:03d}: test each edge first.\n" for i in range(self.LINES))
        self.rewrite_target("seed", base_text)
        self.rewrite_target(winner, child_text)
        full = list(difflib.unified_diff(base_text.splitlines(), child_text.splitlines(), lineterm=""))
        self.assertGreater(len(full), 500, "fixture: the full diff is far over 200 lines")

        code, out, text = self.report()
        self.assert_report(code, out, text)
        shown = [line for line in text.splitlines() if re.match(r"^[-+ ]?(Base|Child) line \d{3}", line)]
        self.assertTrue(any(line.startswith("+Child line") for line in shown),
                        f"the report holds the winner's diff against the base:\n{text[:3000]}")
        self.assertTrue(any(line.startswith("-Base line") for line in shown), "with the base's removed lines")
        self.assertLessEqual(len(shown), 200, f"at most 200 lines of the diff are shown, got {len(shown)}")
        for fence in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
            if "+Child line" in fence:
                self.assertLessEqual(len(fence.rstrip("\n").splitlines()), 200,
                                     "the diff block holds at most 200 lines")
        summary = re.compile(r"(?i)(\d[\d,]*)[^\n]*\b(more|remaining|rest|omitted|not shown|left out|truncated|"
                             r"cut)\b|\b(more|remaining|rest|omitted|not shown|left out|truncated|cut)\b[^\n]*\d")
        after = text[text.index(shown[-1]) + len(shown[-1]):]
        self.assertRegex(after, summary, f"after the shown lines, the rest is summarised with a count:\n{after[:600]}")


class HeldOutCheckUnfinished(ReportBase):
    """Both children are approved on screening with the same tokens as the base, which isn't approved, so the two
    children are the screening front and the base is dominated; then the generation stops before or during the
    held-out replays. Every replay and reflection costs 10,000 tokens (the past replays too, so the estimate is
    10,000)."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.past_replay("old-gen", self.screening[0], "seed", 10000)
        self.past_replay("old-gen", self.holdout[0], "seed", 10000)
        self.reflection_used = 10000
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 10000},
                     (1, "screen"): {"outcome": "approved", "used": 10000},
                     (2, "screen"): {"outcome": "approved", "used": 10000},
                     "base": {"outcome": "approved", "used": 10000},
                     1: {"outcome": "approved", "used": 10000}, 2: {"outcome": "approved", "used": 10000}}

    def assert_children_on_the_front(self):
        self.assertEqual(len(self.made), 2, f"fixture: two children made: {self.mutations}")
        screened = sorted(r["genome"] for r in self.replays if r["kind"] == "screen" and not r["paused"])
        self.assertEqual(screened, sorted(["seed"] * 2 + self.made * 2), "fixture: every genome finished screening")
        state = self.state()
        self.assertEqual(sorted(state.get("front") or []), sorted(self.made), f"fixture: the children are the front: "
                                                                              f"{state}")
        self.assertIsNone(state.get("winner"), f"fixture: no winner: {state}")
        return state

    def assert_says_the_held_out_check_did_not_finish(self, text, reason):
        first = first_paragraph(text)
        self.assertRegex(first, HELD_OUT, f"the first paragraph is about the held-out check:\n{first}")
        self.assertTrue(UNFINISHED.search(first) or re.search(r"(?i)\bbefore\b", first),
                        f"and says it stopped before the held-out check finished:\n{first}")
        self.assertRegex(first, reason, f"and why:\n{first}")
        for child in self.made:
            self.assertRegex(text, word(child), f"the front member {child} is reported")
        self.assertNotRegex(text, NO_CHILD_ON_FRONT, f"two children are on the front, so the report never says that "
                                                     f"none reached it:\n{text}")

    def test_the_budget_stopping_it_before_the_held_out_replays_is_not_no_child_on_the_front(self):  # covers: LOOP-17
        # Spent after screening: base 20,000 + two reflections 20,000 + four child replays 40,000 = 80,000. Every
        # earlier check fits 85,000 (at most 80,000, with a reflection estimated at 30,000); the first held-out
        # replay would need 80,000 + 10,000 > 85,000, so none starts.
        self.generation(budget=85000)
        state = self.assert_children_on_the_front()
        self.assertEqual(state.get("stopped"), "budget", f"fixture: stopped by the budget: {state}")
        self.assertEqual([r for r in self.replays if r["kind"] == "holdout"], [], "fixture: no held-out replay ran")
        code, out, text = self.report()
        self.assert_report(code, out, text)
        self.assert_says_the_held_out_check_did_not_finish(text, re.compile(r"(?i)budget"))

    def test_an_interruption_during_the_held_out_replays_is_not_no_child_on_the_front(self):  # covers: LOOP-17
        # Ctrl-C during the second held-out replay, after the first one finished.
        self.interrupt = lambda role, kind, nth: kind == "holdout" and \
            sum(1 for r in self.replays if r["kind"] == "holdout") == 2
        self.generation()
        self.assert_children_on_the_front()
        held_out = [r for r in self.replays if r["kind"] == "holdout"]
        self.assertEqual(len(held_out), 2, f"fixture: one held-out replay finished, the next was interrupted: "
                                           f"{self.calls_in(1)}")
        code, out, text = self.report()
        self.assert_report(code, out, text)
        self.assert_says_the_held_out_check_did_not_finish(text, UNFINISHED)


# LOOP-18: three fixes to the generation -----------------------------------------------------------------------------

class BaseAloneOnTheFront(ReportBase):
    """The base is approved, cheaper and proves more on screening than both children, so it alone is the front."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        # One unproven requirement per screening case gives the base two failures to aim at.
        self.plan = {("base", "screen"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 5000},
                     1: {"outcome": "ended", "findings": 2, "not_proven": ["DBL-2", "DBL-3"], "used": 20000},
                     2: {"outcome": "ended", "findings": 2, "not_proven": ["DBL-2", "DBL-3"], "used": 20000}}

    def test_the_base_is_not_replayed_on_the_held_out_cases_when_no_child_is_on_the_front(self):  # covers: LOOP-18
        code, out = self.generation()
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.made), 2, f"fixture: two children screened: {self.mutations}\n{out}")
        state = self.state()
        self.assertEqual(state.get("front"), ["seed"], f"fixture: the base alone is on the front: {state}")
        held_out = [(r["case"], r["genome"]) for r in self.replays if r["kind"] == "holdout"]
        self.assertEqual(held_out, [], f"no child can win, so nothing (not even the base) runs on the held-out "
                                       f"cases:\n{out}")
        self.assertFalse(state.get("holdout"), f"no held-out results: {state}")
        self.assertIsNone(state.get("winner"), state)
        why = state.get("why")
        self.assertTrue(isinstance(why, str) and why.strip(), f"the reason there is no winner is recorded: {state}")

    def test_the_report_says_no_child_reached_the_front_when_none_did(self):  # covers: LOOP-17
        self.generation()
        self.assertEqual(len(self.made), 2, f"fixture: two children screened: {self.mutations}")
        self.assertEqual(self.state().get("front"), ["seed"], f"fixture: the base alone is on the front: "
                                                              f"{self.state()}")
        code, out, text = self.report()
        first = self.assert_report(code, out, text)
        self.assertRegex(first, NO_CHILD_ON_FRONT, f"the first paragraph says no child reached the front:\n{first}")
        self.assertNotRegex(first, r"(?i)stopped before|in progress|still running|interrupted|"
                                   r"(didn't|did not|hasn't|has not|wasn't|was not) (yet )?finish",
                            f"the generation finished, so it isn't described as stopped early:\n{first}")


class LeavesInterruptedResult:
    """A replay stopped by Ctrl-C first leaves a result.json with the ABANDONED tokens it used (mixed into a
    ReportBase)."""

    ABANDONED = 777

    def fake_replay(self, *args, **kwargs):
        """As the fixture's, but a replay stopped by Ctrl-C first leaves a result.json with the tokens it used."""
        b = tg.bind(self.orig_replay, args, kwargs)
        case, genome = tg.case_name(b.get("case")), tg.genome_name(b.get("genome"))
        role, kind = self.role(genome), self.kind.get(case)
        nth = 1 + sum(1 for r in self.replays if r["role"] == role and r["kind"] == kind)
        if self.interrupt and not b.get("resume") and self.interrupt(role, kind, nth):
            generation = b.get("generation") or "adhoc"
            rm.write_json(os.path.join(self.evo, "runs", generation, case, genome, "result.json"), {
                "project": tg.PROJECT, "case": case, "genome": genome, "generation": generation, "model": "haiku",
                "outcome": "interrupted", "stop_reason": "the replay was stopped (Ctrl-C)",
                "tokens": {"used": self.ABANDONED}, "scores": None, "started_at": "2026-10-31T10:00:00",
                "ended_at": "2026-10-31T10:03:00"})
        return super().fake_replay(*args, **kwargs)


class InterruptedReplayTokens(LeavesInterruptedResult, ReportBase):
    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2}}

    def test_an_interrupted_attempts_tokens_stay_in_spent_after_the_rerun(self):  # covers: LOOP-18
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1  # Ctrl-C
        self.generation()
        interrupted = [(r["case"], r["genome"]) for r in self.replays if r["paused"]]
        self.assertEqual(len(interrupted), 1, f"fixture: one interrupted replay: {self.calls_in(1)}")
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn(interrupted[0], self.calls_in(2), "fixture: the rerun runs the interrupted replay again")
        case, genome = interrupted[0]
        rerun = self.read_json(self.evo, "runs", GEN, case, genome, "result.json")
        self.assertNotEqual(rerun.get("outcome"), "interrupted", "fixture: the rerun replaced the interrupted result")
        spent = self.state().get("spent") or {}
        self.assertEqual(spent.get("replays"), self.replay_tokens() + self.ABANDONED,
                         f"the finished replays' tokens plus the interrupted attempt's {self.ABANDONED}: {spent}")
        self.assertEqual(spent.get("total"), spent.get("replays", 0) + spent.get("reflections", 0), spent)


class InterruptedAttemptNotRunAgain(LeavesInterruptedResult, ReportBase):
    """Population 1. The child's first screening replay is stopped by Ctrl-C after 50,000 tokens; the rerun counts
    them and the budget then stops the generation before that replay starts again."""

    ABANDONED = 50000

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=1)
        self.past_replay("old-gen", self.screening[0], "seed", 10000)
        self.past_replay("old-gen", self.holdout[0], "seed", 10000)
        self.reflection_used = 10000
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "used": 10000},
                     "base": {"used": 10000}, 1: {"used": 10000}}

    def test_an_interrupted_attempt_the_budget_kept_from_running_again_is_counted_once(self):  # covers: LOOP-17
        # Run 1: base 20,000 + reflection 10,000 = 30,000; the child's replay starts (30,000 + 10,000 <= 60,000) and is
        # interrupted after 50,000. Run 2: 80,000 spent, over the 60,000 budget, so nothing starts.
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1  # Ctrl-C
        self.generation(budget=60000)
        interrupted = [(r["case"], r["genome"]) for r in self.replays if r["paused"]]
        self.assertEqual(len(interrupted), 1, f"fixture: one interrupted replay: {self.calls_in(1)}")
        self.generation(budget=60000)
        self.assertEqual(self.calls_in(2), [], "fixture: the budget keeps the interrupted replay from running again")
        state = self.state()
        self.assertEqual(state.get("stopped"), "budget", f"fixture: stopped by the budget: {state}")
        case, genome = interrupted[0]
        left = self.read_json(self.evo, "runs", GEN, case, genome, "result.json")
        self.assertEqual(left.get("outcome"), "interrupted", "fixture: the interrupted attempt's result.json is left")
        self.assertEqual(state.get("spent"), {"replays": 70000, "reflections": 10000, "total": 80000},
                         f"the interrupted attempt's 50,000 tokens are counted once: {state}")

        code, out, text = self.report()
        self.assert_report(code, out, text)
        self.assertTrue(tg.mentions(text, 80000), f"the report's total spent counts the attempt once (80,000):\n{text}")
        for doubled in (120000, 130000):
            self.assertFalse(tg.mentions(text, doubled), f"the attempt's tokens aren't counted twice ({doubled:,}):"
                                                         f"\n{text}")
        again = re.compile(r"(?i)\bagain\b|\bre-?r[au]n\b|\bretried\b|\brestarted\b")
        for line in text.splitlines():
            if re.search(r"(?i)interrupt", line) or tg.mentions(line, self.ABANDONED):
                self.assertNotRegex(line, again, f"the attempt was never run again, so it isn't described so:\n{line}")
        self.assertNotRegex(text, r"(?i)\b(was|were|been|is|are) (run|replayed|started|tried) again\b|"
                                  r"\b(then|and|later) (re-?run|replayed|ran again|run again)\b",
                            f"nothing in the report says a replay was run again:\n{text}")


class ContinueCommandNamesTheHome(ReportBase):
    """REGISTA_EVOLVE_HOME is set (to a path with a space, through a symlink to the fixture's home), and a replay
    pauses at the usage limit."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2}}
        self.spaced = os.path.join(self.tmp, "evolve home")
        os.symlink(self.evo, self.spaced)
        env = mock.patch.dict(os.environ, {"REGISTA_EVOLVE_HOME": self.spaced})
        env.start()
        self.addCleanup(env.stop)

    def test_the_printed_continue_command_starts_with_the_evolver_home(self):  # covers: LOOP-18
        self.pause = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 2
        code, out = self.generation()
        self.assertEqual(code, self.ev.PAUSED_EXIT, out)
        lines = [line for line in out.splitlines() if re.search(r"evolve(\.py)?[\"']?\s+generation\s", line)
                 and "--id" in line]
        self.assertTrue(lines, f"prints the command to continue:\n{out}")
        for line in lines:
            start = line.find("REGISTA_EVOLVE_HOME=")
            self.assertGreaterEqual(start, 0, f"the continue command sets REGISTA_EVOLVE_HOME:\n{line}")
            command = line[start:]
            if "`" in line[:start]:
                command = command.split("`")[0]
            tokens = shlex.split(command)
            name, _, value = tokens[0].partition("=")
            self.assertEqual(name, "REGISTA_EVOLVE_HOME")
            self.assertEqual(os.path.realpath(value), os.path.realpath(self.evo),
                             f"read as a shell reads it, the variable is this generation's home:\n{line}")
            script = next((i for i, t in enumerate(tokens) if t.endswith("evolve.py")), None)
            self.assertIsNotNone(script, f"the variable comes right before the command itself:\n{line}")
            self.assertEqual(tokens[script + 1], "generation", line)
        # Running the command as written: its argv after evolve.py, with the variable's value as the home.
        argv = [t.rstrip(".,;)") for t in tokens[script + 1:]]
        argv = argv if "--yes" in argv else argv + ["--yes"]
        with mock.patch.dict(os.environ, {"REGISTA_EVOLVE_HOME": value}):
            code, out = self.invoke(argv)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        second = [r for r in self.replays if r["run"] == 2]
        self.assertTrue(second and second[0]["resume"], "the paused replay is resumed")
        self.assertEqual({r["generation"] for r in second}, {GEN}, "the same generation continues")
        self.assertEqual(sorted(os.listdir(os.path.join(self.evo, "runs"))), [GEN], "no new generation started")


if __name__ == "__main__":
    unittest.main()
