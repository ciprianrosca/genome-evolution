"""Light generations (spec .regista/specs/light-mode.json: LOOP-19).

`evolve.py generation --light ...` runs a small generation without editing evolve.json: population from evolve.json
`light.population` (default 2), the usual screening cases, and only the first `light.holdout_cases` (default 3)
held-out cases of the bench's held-out split, in the bench's stable order, for the final check. evolve.json and the
bench files are never changed; the held-out cases a light run skips are never replayed. generation.json records
"light": true and the held-out cases it used; the printed plan and the report's first paragraph say it is a light run
and how many held-out steps it checks, and the estimate counts only those replays. Continuing a light generation
without --light, or a normal one with it, is refused before anything runs. Without --light nothing changes.

Fixtures: tests/test_generation.py's in-process runner (fake replay_run, real mutate around the stand-in claude, a
hand-built bench split by evolve.assign_split, Regista's real profile over the fake Regista checkout; REGISTA_EVOLVE_HOME
and HOME are temporary folders) and tests/test_red_team.py's plan (child 2 wins) and interruption fixture. The bench
has 8 cases, 75% held out, so more than 4 cases are held out and a light run skips some.

Interface assumed: "the bench's stable order" of the held-out cases is the split's stable hash order, the order in
which assign_split holds cases out (the first holdout_pct of it), recovered here by calling assign_split with every
percentage from 0 to 100; generation.json holds the held-out cases used as a list of case names (under any key, step
names optionally prefixed with "<project>/"); the estimate is (1 + population) x screening cases + at most 4 genomes
(base and up to 3 front members) x the light held-out cases replays, times 155,000 tokens when runs/ is empty, plus
30,000 per reflection (population reflections); the refusal to continue says "light"; a paragraph is a block of text
after any Markdown heading lines.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_light_mode -v"""
import copy
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_generation as tg  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_red_team as rt  # noqa: E402

LIGHT_GEN = "light-gen"
LIGHT = re.compile(r"(?i)(?<![\w-])light(?![\w-])")
HELD_OUT = re.compile(r"(?i)held[- ]out|hold[- ]out")
N_CASES = 8
HOLDOUT_PCT = 75
SCREEN_CASES = 2
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def first_paragraph(text):
    """The first block of text after any Markdown heading lines, joined into one line."""
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines:
            return " ".join(lines)
    return ""


def says_count(text, n):
    return tg.mentions(text, n) or re.search(rf"(?i)\b{NUMBER_WORDS[n]}\b", text) is not None


class Base(tg.Base):
    def setUp(self):
        super().setUp()
        self.bench(n=N_CASES, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES)
        self.assertGreater(len(self.holdout), 4, f"fixture: more than 4 held-out cases: {self.holdout}")
        self.plan = dict(rt.WINNER_PLAN)
        self.order = self.holdout_order()
        self.assertEqual(sorted(self.order[:len(self.holdout)]), sorted(self.holdout),
                         "fixture: the split holds out the first cases of its stable order")

    def holdout_order(self):
        """The held-out cases in the bench's stable order: the order in which assign_split holds cases out as the
        percentage grows (it holds out the first holdout_pct of its stable hash order)."""
        folder = os.path.join(self.evo, "bench", tg.PROJECT)
        cases = []
        for name in sorted(os.listdir(folder)):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                cases.append(json.load(f))
        first_pct = {}
        previous = set()
        for pct in range(0, 101):
            built = []
            for case in cases:
                fresh = copy.deepcopy(case)
                fresh["split"], fresh["screening"] = None, False
                built.append((fresh, [], 10 * int(case["milestone"][1:])))
            self.ev.assign_split(tg.PROJECT, built, {"holdout_pct": pct, "screen_cases": SCREEN_CASES})
            held = {c["step"] for c, _, _ in built if c.get("split") == "holdout"}
            self.assertTrue(previous <= held, f"fixture: the hold-out split grows with the percentage ({pct}%)")
            for step in held - previous:
                first_pct[step] = pct
            previous = held
        order = sorted(first_pct, key=lambda s: first_pct[s])
        for i in range(min(len(order), 5) - 1):
            self.assertNotEqual(first_pct[order[i]], first_pct[order[i + 1]],
                                f"fixture: the stable order of the first held-out cases is known: {first_pct}")
        return order

    def snapshot(self):
        """The bytes of evolve.json and of every file under bench/."""
        files = {}
        with open(os.path.join(self.evo, "evolve.json"), "rb") as f:
            files["evolve.json"] = f.read()
        for dirpath, _, names in os.walk(os.path.join(self.evo, "bench")):
            for name in names:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as f:
                    files[os.path.relpath(path, self.evo)] = f.read()
        return files

    def held_out_replays(self, run=None):
        return [(r["case"], r["genome"]) for r in self.replays
                if r["kind"] == "holdout" and (run is None or r["run"] == run)]

    def assert_used_cases_recorded(self, state, expected):
        lists = []

        def walk(value):
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                if value and all(isinstance(v, str) for v in value):
                    lists.append([tg.case_name(v) for v in value])
                for v in value:
                    walk(v)
        walk({k: v for k, v in state.items() if k not in ("children", "front")})
        self.assertIn(sorted(expected), [sorted(found) for found in lists],
                      f"generation.json records the held-out cases used ({expected}): {state}")

    def assert_light_run(self, code, out, gen, population, holdout_cases, before):
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        used = self.order[:holdout_cases]
        skipped = [c for c in self.holdout if c not in used]
        self.assertTrue(skipped, "fixture: the light run skips some held-out cases")
        self.assertEqual(len(self.made), population, f"population {population}: {self.mutations}\n{out}")
        for genome in ["seed", *self.made]:
            cases = sorted(r["case"] for r in self.replays if r["genome"] == genome and r["kind"] == "screen"
                           and not r["paused"])  # an interrupted attempt is replayed again
            self.assertEqual(cases, sorted(self.screening), f"{genome} ran on the usual screening cases")
        held = self.held_out_replays()
        self.assertTrue(held, f"fixture: the held-out check ran:\n{out}")
        self.assertEqual({case for case, _ in held}, set(used),
                         f"only the first {holdout_cases} held-out cases in the bench's order ({used}) are replayed:"
                         f" {held}\n{out}")
        for genome in {g for _, g in held}:
            self.assertEqual(sorted(c for c, g in held if g == genome), sorted(used),
                             f"{genome} ran on each light held-out case once")
        replayed = {r["case"] for r in self.replays}
        for case in skipped:
            self.assertNotIn(case, replayed, f"the skipped held-out case {case} is never replayed")
            self.assertFalse(os.path.exists(os.path.join(self.evo, "runs", gen, case)),
                             f"no replay folder for the skipped held-out case {case}")
        state = self.state(gen)
        self.assertIs(state.get("light"), True, f"generation.json holds light true: {state}")
        self.assert_used_cases_recorded(state, used)
        for genome, by_case in (state.get("holdout") or {}).items():
            self.assertTrue(set(tg.norm_cases(by_case)) <= set(used), f"{genome}'s held-out results: {state}")
        self.assertEqual(self.snapshot(), before, "evolve.json and the bench files are byte-identical")
        return state

    def plan_only(self, extra=()):
        """The generation command without a terminal or --yes: it prints its plan and refuses."""
        r = subprocess.run([tg.PY, tg.EVOLVE, "generation", "--target", tg.TARGET, "--budget", "50000000", "--id",
                            LIGHT_GEN, "--seed", "7", *extra], cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", timeout=120,
                           env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertNotRegex(out, r"(?im)unrecognized arguments|^usage:", f"the parser knows the flag:\n{out}")
        folder = os.path.join(self.evo, "runs", LIGHT_GEN)
        cases = [d for d in (os.listdir(folder) if os.path.isdir(folder) else [])
                 if os.path.isdir(os.path.join(folder, d))]
        self.assertEqual(cases, [], f"no replay started:\n{out}")
        self.assertEqual(self.calls(), [], "claude was never started")
        return out

    def interrupted(self, gen, extra=()):
        """A generation stopped (Ctrl-C) during the first child's first screening replay."""
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1
        self.generation(gen=gen, extra=extra)
        self.interrupt = None
        self.assertEqual(len(self.made), 1, "fixture: one child made before the stop")
        self.assertIsNone(self.state(gen).get("winner"), "fixture: the generation hasn't finished")

    def generation_json_bytes(self, gen):
        with open(os.path.join(self.evo, "runs", gen, "generation.json"), "rb") as f:
            return f.read()

    def assert_refused_continuation(self, code, out, gen, before):
        self.assertNotEqual(code, 0, f"continuing with a different --light setting is refused:\n{out}")
        self.assertNotIn("Traceback", out, "a plain message, not a crash")
        self.assertNotRegex(out, r"(?im)unrecognized arguments|^usage:", f"the generation refuses, not the parser:\n{out}")
        self.assertRegex(out, LIGHT, f"the message says why (light):\n{out}")
        self.assertEqual(self.calls_in(self.run_number), [], f"no replay ran:\n{out}")
        self.assertEqual([m for m in self.mutations if m["run"] == self.run_number], [], "no child was made")
        self.assertEqual(self.generation_json_bytes(gen), before, "generation.json is unchanged")


class LightRun(Base):
    def test_defaults_population_2_and_first_3_held_out_cases_files_untouched(self):  # covers: LOOP-19
        self.configure(population=4, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES)
        before = self.snapshot()
        code, out = self.generation(gen=LIGHT_GEN, extra=("--light",))
        self.assert_light_run(code, out, LIGHT_GEN, population=2, holdout_cases=3, before=before)
        self.assertRegex(out, LIGHT, f"the printed plan says it is a light run:\n{out}")

    def test_light_population_and_holdout_cases_in_evolve_json_are_honoured(self):  # covers: LOOP-19
        self.configure(population=6, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES,
                       light={"population": 3, "holdout_cases": 4})
        before = self.snapshot()
        code, out = self.generation(gen=LIGHT_GEN, extra=("--light",))
        self.assert_light_run(code, out, LIGHT_GEN, population=3, holdout_cases=4, before=before)

    def test_plan_estimate_and_report_say_it_is_a_light_run(self):  # covers: LOOP-19
        # runs/ is empty, so each replay is estimated at 155,000 tokens. Light: 3 children, 4 held-out cases:
        # (1 + 3) x 2 screening + 4 genomes x 4 held-out = 24 replays, 24 x 155,000 + 3 x 30,000 = 3,810,000 tokens.
        # A normal run (population 6, every held-out case) would be (1 + 6) x 2 + 4 x len(holdout) replays.
        self.configure(population=6, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES,
                       light={"population": 3, "holdout_cases": 4})
        out = self.plan_only(extra=("--light",))
        replays = (1 + 3) * SCREEN_CASES + 4 * 4
        tokens = replays * tg.DEFAULT_ESTIMATE + 3 * tg.REFLECTION_ESTIMATE
        normal_replays = (1 + 6) * SCREEN_CASES + 4 * len(self.holdout)
        normal_tokens = normal_replays * tg.DEFAULT_ESTIMATE + 6 * tg.REFLECTION_ESTIMATE
        self.assertRegex(out, LIGHT, f"the plan says it is a light run:\n{out}")
        self.assertTrue(any(HELD_OUT.search(line) and says_count(line, 4) for line in out.splitlines()),
                        f"the plan says how many held-out steps it checks (4):\n{out}")
        self.assertTrue(tg.mentions(out, replays), f"the estimate counts only the light run's replays ({replays}):\n{out}")
        self.assertTrue(tg.mentions(out, tokens), f"and its tokens ({tokens}):\n{out}")
        self.assertFalse(tg.mentions(out, normal_tokens), f"not a normal run's tokens ({normal_tokens}):\n{out}")
        for case in self.order[:4]:
            self.assertIn(case, out, "the plan names the held-out cases it checks")

        # The run itself, then its report: the first paragraph says it is a light run and how many held-out steps.
        code, run_out = self.generation(gen=LIGHT_GEN, extra=("--light",))
        self.assertEqual(code, 0, run_out)
        code, report = self.invoke(["report", LIGHT_GEN])
        self.assertEqual(code, 0, report)
        self.assertNotIn("Traceback", report)
        path = os.path.join(self.evo, "reports", LIGHT_GEN + ".md")
        self.assertTrue(os.path.isfile(path), f"reports/{LIGHT_GEN}.md is written:\n{report}")
        with open(path, encoding="utf-8") as f:
            paragraph = first_paragraph(f.read())
        self.assertRegex(paragraph, LIGHT, f"the report's first paragraph says it is a light run:\n{paragraph}")
        self.assertRegex(paragraph, HELD_OUT, f"and mentions the held-out check:\n{paragraph}")
        self.assertTrue(says_count(paragraph, 4), f"with how many held-out steps it checks (4):\n{paragraph}")


class Continuing(Base):
    def setUp(self):
        super().setUp()
        self.configure(population=2, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES)

    def test_a_light_generation_continues_only_as_light(self):  # covers: LOOP-19
        before_files = self.snapshot()
        self.interrupted(LIGHT_GEN, extra=("--light",))
        self.assertIs(self.state(LIGHT_GEN).get("light"), True)
        before = self.generation_json_bytes(LIGHT_GEN)
        code, out = self.generation(gen=LIGHT_GEN)
        self.assert_refused_continuation(code, out, LIGHT_GEN, before)
        code, out = self.generation(gen=LIGHT_GEN, extra=("--light",))
        self.assert_light_run(code, out, LIGHT_GEN, population=2, holdout_cases=3, before=before_files)

    def test_a_normal_generation_cannot_be_continued_as_light(self):  # covers: LOOP-19
        self.interrupted(tg.GEN)
        self.assertFalse(self.state(tg.GEN).get("light"), "a normal generation isn't marked light")
        before = self.generation_json_bytes(tg.GEN)
        code, out = self.generation(gen=tg.GEN, extra=("--light",))
        self.assert_refused_continuation(code, out, tg.GEN, before)


class WithoutLight(Base):
    def test_without_light_the_light_settings_are_ignored(self):  # covers: LOOP-19
        self.configure(population=2, holdout_pct=HOLDOUT_PCT, screen_cases=SCREEN_CASES,
                       light={"population": 3, "holdout_cases": 1})
        before = self.snapshot()
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.made), 2, f"evolve.json's population, not light.population: {self.mutations}\n{out}")
        state = self.state()
        self.assertFalse(state.get("light"), f"a normal generation isn't marked light: {state}")
        held = self.held_out_replays()
        for genome in {g for _, g in held} | {"seed"}:
            self.assertEqual(sorted(c for c, g in held if g == genome), sorted(self.holdout),
                             f"{genome} ran on every held-out case: {held}\n{out}")
        holdout = {g: tg.norm_cases(by_case) for g, by_case in (state.get("holdout") or {}).items()}
        self.assertEqual(sorted(holdout.get("seed", {})), sorted(self.holdout), state)
        self.assertEqual(state.get("winner"), self.made[1], f"the usual result (child 2 wins): {state}")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
