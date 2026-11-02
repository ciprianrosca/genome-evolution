"""Screening steps are the hard ones (spec .regista/specs/screening-hard.json, BENCH-13).

Three identical replays of the smallest screening step all scored perfectly, so screening on the smallest steps only
measured token noise. assign_split (in scripts/evolve.py, loaded here as a module) keeps the hold-out split exactly
as before and picks as screening steps the `screen_cases` train cases ranked by the case's baseline review rounds
(most first), then baseline gate failures (most first; None counts as 0), then changed lines (fewest first), then
step name. Cases that aren't replayable never get a split and never screen.

Interface assumed: assign_split(project_name, built, config), where `built` is a list of (case, copies, size)
tuples, `size` is the step's changed line count, `config` carries holdout_pct and screen_cases, and each case gets
`split` ("train", "holdout" or None) and `screening` (True or False) set in place. The return value isn't used.

The report wording (the audit report that `bench build` writes) is checked in tests/test_bench_cases.py, next to
the fixture that already builds a bench.

Run: python3 -m unittest tests.test_screening_hard -v"""
import importlib.util
import os
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVOLVE = os.path.join(REPO, "scripts", "evolve.py")


def load_evolve():
    spec = importlib.util.spec_from_file_location("evolve_screening_under_test", EVOLVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case(step, review_rounds=0, gate_failures=0, replayable=True, project="fpl"):
    return {"project": project, "step": step, "milestone": "M1", "replayable": replayable,
            "reasons": ["Replayable for the test." if replayable else "Not replayable for the test."],
            "baseline": {"review_rounds": review_rounds, "gate_failures": gate_failures, "refusals": 0,
                         "compactions": 0, "logs_complete": True},
            "split": None, "screening": False}


def entry(step, size, review_rounds=0, gate_failures=0, replayable=True):
    return (case(step, review_rounds, gate_failures, replayable), [], size)


def rank_key(item):
    c, _, size = item
    base = c["baseline"]
    return (-base["review_rounds"], -(base["gate_failures"] or 0), size, c["step"])


class AssignSplitScreensHardestSteps(unittest.TestCase):
    """Rankings with holdout_pct 0, so every replayable case is a train case and only the ranking decides."""

    @classmethod
    def setUpClass(cls):
        cls.evolve = load_evolve()

    def assign(self, built, holdout_pct=0, screen_cases=4, project="fpl"):
        self.evolve.assign_split(project, built, {"holdout_pct": holdout_pct, "screen_cases": screen_cases})
        return built

    def screening(self, built):
        return sorted(c["step"] for c, _, _ in built if c["screening"])

    def assert_all_train(self, built):
        for c, _, _ in built:
            if c["replayable"]:
                self.assertEqual(c["split"], "train", f"{c['step']}: holdout_pct 0 holds nothing out")

    def test_fpl_example_most_review_rounds_then_most_failing_gate_runs(self):  # covers: BENCH-13
        # The spec's example from FPL's bench: two steps with 2 review rounds, then two with 1 round and 2 failing
        # gate runs. The small, easy steps that used to screen lose their place.
        built = self.assign([
            entry("m1-setup", 5),
            entry("m2-tiny", 2),
            entry("m3-small", 8, review_rounds=0, gate_failures=3),
            entry("m8-polish", 400, review_rounds=2, gate_failures=0),
            entry("m10-prices", 250, review_rounds=1, gate_failures=2),
            entry("m11-teams", 30, review_rounds=1, gate_failures=1),
            entry("m12-republish", 300, review_rounds=1, gate_failures=2),
            entry("m15-cache", 20, review_rounds=1, gate_failures=None),
            entry("m18-replan-endpoint", 500, review_rounds=2, gate_failures=1),
        ], screen_cases=4)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), sorted(["m8-polish", "m18-replan-endpoint", "m10-prices",
                                                        "m12-republish"]))

    def test_review_rounds_outrank_gate_failures_and_size(self):  # covers: BENCH-13
        built = self.assign([
            entry("a-big-reviewed", 900, review_rounds=1, gate_failures=0),
            entry("b-small-failing", 1, review_rounds=0, gate_failures=9),
            entry("c-small-clean", 1, review_rounds=0, gate_failures=0),
        ], screen_cases=1)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["a-big-reviewed"])

    def test_gate_failures_break_review_round_ties(self):  # covers: BENCH-13
        built = self.assign([
            entry("a-one-failure", 1, review_rounds=3, gate_failures=1),
            entry("b-two-failures", 800, review_rounds=3, gate_failures=2),
            entry("c-fewer-rounds", 1, review_rounds=2, gate_failures=5),
        ], screen_cases=1)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["b-two-failures"])

    def test_missing_gate_failures_count_as_zero(self):  # covers: BENCH-13
        # None ties with 0, so changed lines decide between them; a real failure still beats both.
        built = self.assign([
            entry("a-zero-failures", 50, review_rounds=1, gate_failures=0),
            entry("b-unknown-failures", 10, review_rounds=1, gate_failures=None),
            entry("c-one-failure", 900, review_rounds=1, gate_failures=1),
            entry("d-no-rounds", 1, review_rounds=0, gate_failures=None),
        ], screen_cases=2)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["b-unknown-failures", "c-one-failure"])

    def test_fewest_changed_lines_break_remaining_ties(self):  # covers: BENCH-13
        built = self.assign([
            entry("a-larger", 30, review_rounds=1, gate_failures=1),
            entry("b-smaller", 10, review_rounds=1, gate_failures=1),
            entry("c-smallest-but-easier", 1, review_rounds=1, gate_failures=0),
        ], screen_cases=1)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["b-smaller"])

    def test_step_name_breaks_full_ties(self):  # covers: BENCH-13
        built = self.assign([
            entry("m9-beta", 10, review_rounds=1, gate_failures=1),
            entry("m10-alpha", 10, review_rounds=1, gate_failures=1),
            entry("m2-gamma", 10, review_rounds=1, gate_failures=1),
        ], screen_cases=1)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["m10-alpha"], "plain string order of the step names")

    def test_not_replayable_cases_never_screen_or_take_a_place(self):  # covers: BENCH-13
        built = self.assign([
            entry("a-hardest-but-not-replayable", 1, review_rounds=9, gate_failures=9, replayable=False),
            entry("b-hard", 10, review_rounds=2, gate_failures=0),
            entry("c-medium", 10, review_rounds=1, gate_failures=0),
            entry("d-easy-and-smallest", 1, review_rounds=0, gate_failures=0),
        ], screen_cases=2)
        skipped = built[0][0]
        self.assertIsNone(skipped["split"])
        self.assertIs(skipped["screening"], False)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["b-hard", "c-medium"])

    def test_fewer_train_cases_than_screen_cases_all_screen(self):  # covers: BENCH-13
        built = self.assign([entry("a-one", 10, review_rounds=0), entry("b-two", 5, review_rounds=1)],
                            screen_cases=4)
        self.assert_all_train(built)
        self.assertEqual(self.screening(built), ["a-one", "b-two"])
        for c, _, _ in built:
            self.assertIs(c["screening"], True)


STEPS = [f"m{i}-step" for i in range(20)]
# What assign_split held out for this input before the change (project "fpl", holdout_pct 30): the change must
# hold out exactly these cases.
HOLDOUT_BEFORE = {"m5-step", "m7-step", "m12-step", "m13-step", "m17-step", "m18-step"}


def twenty(hard=False):
    """Twenty replayable steps and one that isn't. With hard=True their review rounds, gate failures and sizes vary,
    the way they do on a real bench; otherwise they're all equal."""
    built = []
    for i, step in enumerate(STEPS):
        if hard:
            built.append(entry(step, size=(37 * i) % 23 + 1, review_rounds=i % 3,
                               gate_failures=None if i % 5 == 0 else (i * 7) % 4))
        else:
            built.append(entry(step, size=10))
    built.append(entry("x-later", 1, review_rounds=5, gate_failures=5, replayable=False))
    return built


class AssignSplitKeepsTheHoldout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evolve = load_evolve()

    def assign(self, built):
        self.evolve.assign_split("fpl", built, {"holdout_pct": 30, "screen_cases": 3})
        return built

    @staticmethod
    def holdout(built):
        return {c["step"] for c, _, _ in built if c["split"] == "holdout"}

    def test_holdout_is_the_same_cases_as_before(self):  # covers: BENCH-13
        self.assertEqual(self.holdout(self.assign(twenty())), HOLDOUT_BEFORE)
        self.assertEqual(self.holdout(self.assign(twenty(hard=True))), HOLDOUT_BEFORE,
                         "review rounds, gate failures and sizes don't move a case in or out of the holdout")

    def test_holdout_same_on_every_run(self):  # covers: BENCH-13
        first = self.assign(twenty(hard=True))
        again = self.assign(list(reversed(twenty(hard=True))))
        self.assertEqual({c["step"]: (c["split"], c["screening"]) for c, _, _ in first},
                         {c["step"]: (c["split"], c["screening"]) for c, _, _ in again},
                         "the input order doesn't change the split or the screening steps")

    def test_screening_ranks_only_train_cases(self):  # covers: BENCH-13
        built = self.assign(twenty(hard=True))
        later = built[-1][0]
        self.assertIsNone(later["split"])
        self.assertIs(later["screening"], False)
        train = [item for item in built if item[0]["split"] == "train"]
        expected = sorted(c["step"] for c, _, _ in sorted(train, key=rank_key)[:3])
        self.assertEqual(sorted(c["step"] for c, _, _ in built if c["screening"]), expected,
                         "the three hardest train cases; a held-out case never screens")
        for c, _, _ in built:
            if c["split"] == "holdout":
                self.assertIs(c["screening"], False, c["step"])


if __name__ == "__main__":
    unittest.main()
