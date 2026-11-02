"""The reviewer is never changed by a candidate (spec .regista/specs/judge-fixed.json: FIT-3, the light version).

A profile may list `judges`: evolvable files that judge the others' work (Regista's genome/profiles/regista.json lists
agents/reviewer.md). load_profile refuses a judge that isn't evolvable. make_child refuses, with a GenomeError and
nothing written, a child whose judge file differs in any byte from its root seed's copy, naming the file and saying
it judges the others' work: a rewrite and a trait held in the judge file (Regista's reviewer.model, in the reviewer's
front matter) alike, from a child or a grandchild. In evolve.py, `generation --target` and `mutate --target` refuse
a judge file before anything runs, and mutate_trait_aimed refuses a trait whose source is a judge file. A profile
without `judges` behaves as before. genome.py stays free of Regista names: tests/test_genome_seed.py already checks
that, so it isn't repeated here.

Fixtures: the small haiku target of tests/test_genome_limits.py (a profile that isn't Regista's, here with
prompts/close.md as its judge), Regista's real profile over the fake Regista checkout of tests/test_genome_seed.py,
and tests/test_generation.py's in-process command runner with its fake replay_run, wrapped mutate and stand-in
claude. Everything lives in temporary folders (REGISTA_EVOLVE_HOME and HOME included).

Interface assumed: judges are exact paths (not globs); the refusal messages contain the file's path and a form of
"judge"; mutate_trait_aimed's refusal names the trait or its file; `evolve.py mutate <parent> --case <case> --failure
<id> --target <file> --generation <g>` as in genome/tests/e2e/test_reflect_mutate.py.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_judge_fixed -v"""
import json
import os
import random
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_generation as tg  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_genome_limits as lim  # noqa: E402
import test_reflect_mutate as rm  # noqa: E402
from test_genome_seed import EVOLVABLE, PROFILE as REGISTA_PROFILE, build_checkout, snapshot  # noqa: E402

JUDGE_WORD = re.compile(r"(?i)judg")
REVIEWER = "agents/reviewer.md"
BUILDER = "agents/builder.md"
HAIKU_JUDGE = "prompts/close.md"
HAIKU_JUDGED = {**lim.PROFILE, "name": "haiku-judged", "judges": [HAIKU_JUDGE]}


class Base(lim.LimitsBase):
    def make(self, profile, child_id, parent, files=None, traits=None, source=None):
        self.gn.make_child(self.evo, profile, source or self.source, child_id, [parent], files=files, traits=traits)
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir(child_id), "genome.json")), f"{child_id} made")

    def refused(self, profile, parent, what, judge, files=None, traits=None, source=None):
        self.bad = getattr(self, "bad", 0) + 1
        before = snapshot(self.tmp)
        text = self.assert_genome_error(
            lambda: self.gn.make_child(self.evo, profile, source or self.source, f"bad-child-{self.bad}", [parent],
                                       files=files, traits=traits), [judge], what)
        self.assertRegex(text, JUDGE_WORD, f"{what}: the message says the file judges the others' work:\n{text}")
        self.assertEqual(snapshot(self.tmp), before, f"{what}: nothing written, under genomes/ or anywhere else")


# load_profile ---------------------------------------------------------------------------------------------------------

class JudgesInTheProfile(Base):
    def test_a_judge_that_isnt_evolvable_is_refused_and_evolvable_judges_load(self):  # covers: FIT-3
        for what, judge in {"a frozen file": "settings.json", "a never path": "scripts/count.sh",
                            "a file outside the evolvable globs": "README.md"}.items():
            with self.subTest(case=what):
                self.assert_genome_error(lambda judge=judge: self.load(data={**HAIKU_JUDGED, "judges": [judge]}),
                                         [judge], f"a profile whose judge is {what}")
        self.load(data=HAIKU_JUDGED)
        self.load(data=HAIKU_JUDGED, source=self.source)
        with open(REGISTA_PROFILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("judges"), [REVIEWER], "Regista's profile names the reviewer as judge")
        self.gn.load_profile(REGISTA_PROFILE)


# make_child: the small target -----------------------------------------------------------------------------------------

class JudgeFileStaysAsTheRootSeeds(Base):
    def setUp(self):
        super().setUp()
        self.profile = self.load(data=HAIKU_JUDGED)
        self.gn.seed(self.evo, self.profile, self.source, "seed")

    def test_changing_the_judge_is_refused_from_a_child_and_a_grandchild(self):  # covers: FIT-3
        new_close = b"Close with a haiku about the reader.\n"
        self.refused(self.profile, "seed", "a child rewriting the judge", HAIKU_JUDGE, files={HAIKU_JUDGE: new_close})
        self.refused(self.profile, "seed", "a child changing the judge and another prompt", HAIKU_JUDGE,
                     files={HAIKU_JUDGE: new_close, "prompts/greet.md": b"Greet the reader.\n"})
        self.make(self.profile, "child-1", "seed", files={"prompts/greet.md": b"Greet the reader kindly.\n"})
        self.refused(self.profile, "child-1", "a grandchild rewriting the judge", HAIKU_JUDGE,
                     files={HAIKU_JUDGE: new_close})
        self.make(self.profile, "grandchild", "child-1",
                  files={"SKILL.md": (lim.SKILL + "Count the syllables.\n").encode("utf-8")})

    def test_a_profile_without_judges_accepts_a_change_to_the_same_file(self):  # covers: FIT-3
        # The same profile as the seed's (make_child refuses parents made with another profile), without judges.
        plain = self.load(data={**lim.PROFILE, "name": HAIKU_JUDGED["name"]})
        self.assertNotIn("judges", lim.PROFILE)
        new_close = b"Close with a short haiku.\n"
        seed_close = lim.SOURCE[HAIKU_JUDGE].encode("utf-8")
        self.assertNotEqual(new_close, seed_close)
        self.assertLessEqual(len(new_close), len(seed_close) * 3 // 2, "within the 50% growth cap on prompts/*.md")
        self.make(plain, "child-plain", "seed", files={HAIKU_JUDGE: new_close})


# make_child: Regista's reviewer ---------------------------------------------------------------------------------------

class RegistasReviewerIsFixed(Base):
    def setUp(self):
        super().setUp()
        self.checkout, _ = build_checkout(self.tmp, use_git=False)
        self.profile = self.gn.load_profile(REGISTA_PROFILE, source=self.checkout)
        self.gn.seed(self.evo, self.profile, self.checkout, "seed")

    def test_a_reviewer_rewrite_or_model_change_is_refused_and_a_builder_change_is_accepted(self):  # covers: FIT-3
        rewrite = (EVOLVABLE[REVIEWER] + "Approve small changes quickly.\n").encode("utf-8")
        for parent in ("seed", "child-1"):
            if parent == "child-1":
                self.make(self.profile, "child-1", "seed", source=self.checkout,
                          files={BUILDER: (EVOLVABLE[BUILDER] + "Test each edge case.\n").encode("utf-8")})
            with self.subTest(parent=parent):
                self.refused(self.profile, parent, f"a rewrite of the reviewer from {parent}", REVIEWER,
                             files={REVIEWER: rewrite}, source=self.checkout)
                self.refused(self.profile, parent, f"the reviewer's model moved (a trait in its file) from {parent}",
                             REVIEWER, traits={"reviewer.model": "opus"}, source=self.checkout)
        self.make(self.profile, "child-2", "child-1", source=self.checkout,
                  files={BUILDER: (EVOLVABLE[BUILDER] + "Read the spec first.\n").encode("utf-8")})


# evolve.py ------------------------------------------------------------------------------------------------------------

class EvolveRefusesAJudge(tg.Base):
    def assert_refused_before_anything_ran(self, code, out, genomes_before, what):
        self.assertNotEqual(code, 0, f"{what} is refused:\n{out}")
        self.assertNotIn("Traceback", out, "a plain message, not a crash")
        self.assertIn(REVIEWER, out, f"{what}: the message names the file:\n{out}")
        self.assertRegex(out, JUDGE_WORD, f"{what}: and says it judges the others' work:\n{out}")
        self.assertEqual(self.replays, [], f"{what}: no replay ran")
        self.assertEqual(self.mutations, [], f"{what}: no mutation started")
        self.assertEqual(self.calls(), [], f"{what}: claude never started")
        self.assertEqual(snapshot(os.path.join(self.evo, "genomes")), genomes_before, f"{what}: no genome written")

    def test_generation_with_the_reviewer_as_target_is_refused_before_anything_runs(self):  # covers: FIT-3
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        before = snapshot(os.path.join(self.evo, "genomes"))
        code, out = self.invoke(["generation", "--target", REVIEWER, "--budget", str(tg.BIG_BUDGET), "--id", tg.GEN,
                                 "--seed", "7", "--yes"])
        self.assert_refused_before_anything_ran(code, out, before, "generation --target agents/reviewer.md")
        self.assertFalse(os.path.exists(os.path.join(self.evo, "runs", tg.GEN, "generation.json")),
                         "no generation.json is written")

    def test_mutate_with_the_reviewer_as_target_is_refused_before_claude_starts(self):  # covers: FIT-3
        major, _blocker = self.review_replay()
        before = snapshot(os.path.join(self.evo, "genomes"))
        code, out = self.invoke(["mutate", "seed", "--case", rm.CASE, "--failure", major["id"], "--target", REVIEWER,
                                 "--generation", rm.GEN])
        self.assert_refused_before_anything_ran(code, out, before, "mutate --target agents/reviewer.md")

    def test_mutate_trait_aimed_refuses_the_reviewers_model(self):  # covers: FIT-3
        major, _blocker = self.review_replay()
        before = snapshot(os.path.join(self.evo, "genomes"))
        with self.assertRaises(Exception) as caught:
            self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "reviewer.model",
                                       "seed-t1", random.Random(7))
        self.assertTrue(rm.is_plain_error(caught.exception), f"a plain error, got {caught.exception!r}")
        message = str(caught.exception)
        self.assertTrue("reviewer.model" in message or REVIEWER in message, f"names the trait or its file: {message}")
        self.assertRegex(message, JUDGE_WORD, f"says its file judges the others' work: {message}")
        self.assertEqual(snapshot(os.path.join(self.evo, "genomes")), before, "no child written")
        self.assertEqual(self.calls(), [], "claude never started")


if __name__ == "__main__":
    unittest.main()
