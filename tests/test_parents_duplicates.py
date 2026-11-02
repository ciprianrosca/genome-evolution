"""Parent selection and near-duplicate skipping (spec .regista/specs/parents-duplicates.json: LOOP-4, LOOP-5, LOOP-13;
LOOP-14 is the end-to-end test in genome/tests/e2e/test_parents.py).

genome.py: parent_weights(entries, sharpness=10, midpoint=0.5) gives each {"id", "score", "children"} a probability
proportional to sigmoid(sharpness * (score - midpoint)) / (1 + children); pick_parent(entries, rng) draws one id with
those probabilities; children(home, id) counts the stored genomes listing id among their parents;
near_duplicate(home, profile, contents, traits, max_changed) finds the stored genome of the same profile with the
fewest changed characters summed over its files (changed_chars: for every non-equal difflib.SequenceMatcher opcode, the
larger of its two spans), when its traits are identical and that sum is at most max_changed. mutate and
mutate_trait_aimed refuse such a candidate with NearDuplicate (a GenomeError), write no child and append one line to
genomes/discarded.jsonl. evolve.py: genome_score(home, generation, genome) is the mean, over the genome's finished
replays of that generation, of 0.5 * requirements value + 0.5 * (approved, gates pass and no hard failure).

The fixtures are those of tests/test_reflect_mutate.py (Regista's real profile over the fake Regista checkout, the
stand-in claude through evolve.json `claude_command`, hand-built replay folders under runs/<generation>/<case>/<genome>/)
and the small haiku target of tests/test_genome_limits.py. Everything lives in temporary folders.

Interface assumed: errors are plain (GenomeError or EvolveError, by class name); NearDuplicate is genome.NearDuplicate;
near_duplicate's contents hold every evolvable file of the candidate as bytes; changed_chars takes two bytes values;
the changed count it returns (and records as "changed") is a whole number, 0 for identical files; discarded.jsonl's
"kind" is the mutation's kind ("rewrite" or "trait", as in mutation.json); mutate_trait_aimed takes max_changed as a
keyword after rng; an unfinished replay is a case folder without result.json or with an empty outcome; an invalid
duplicate_max_changed is refused with a plain error naming it, by load_config or by mutate.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_parents_duplicates -v"""
import difflib
import json
import math
import os
import random
import re
import shutil
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_genome_limits as limits  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_reflect_mutate as rm  # noqa: E402
from test_genome_seed import EVOLVABLE, GENOME_PY, is_plain_error, snapshot, write  # noqa: E402

GEN = rm.GEN
BUILDER = rm.BUILDER
# Changed characters from the seed's builder prompt (74 bytes; a rewrite may grow it to 111 bytes at most). Each count
# is checked against the spec's definition (changed() below) in the tests that use it.
# The default duplicate_max_changed is 10 (inclusive). rm.NEW_BUILDER, the reflection tests' default rewrite, adds
# "Test each edge case.\n" (21 characters): a real aimed edit, kept at the default.
FEW = BUILDER.replace("builder.\n", "builder!\n")  # one character changed
AT_10 = BUILDER + "Be brief.\n"  # 10 characters added: discarded at the default 10
OVER_11 = BUILDER + "Keep tidy.\n"  # 11 characters added: kept at the default 10
AT_40 = BUILDER + "Write a test for each edge case, please."  # 40 characters added: discarded at an explicit 40
OVER_41 = BUILDER + "Write a test for each edge case, please.\n"  # 41 characters added: kept at an explicit 40
OVER = BUILDER.replace("You are the builder.\n", "You build. Write a failing test for every edge case.\n")  # 48, 106 bytes
SMALL = "Short.\n"  # 7 characters, added to one file (under 10) or two (14, over 10)
# A prompt of a few kilobytes and one ordinary sentence added in its middle: far over 10 changed characters, though a
# whole-file similarity ratio would still put it near 0.98.
PARA = ("You are the builder for one step of the plan. Read the step's spec and the tests the test writer left, then change "
        "the code until those tests pass. Keep each change small and explain it in the commit message. Do not edit the "
        "frozen tests, and do not weaken a gate to make it pass.\n\n")
BIG = "---\nname: builder\ndescription: Builds one step.\n---\n\n" + "".join(
    f"## {t}\n\n{PARA}" for t in ("Planning", "Tests", "Gates", "Commits", "Review", "Errors", "Notes", "Handback"))
SENTENCE = "Before you hand back, run the whole test suite once more and read its output.\n"
BIG_PLUS = BIG.replace("## Gates\n\n", "## Gates\n\n" + SENTENCE)
REGISTA_DESCRIBE = "a set of instructions that guides AI coding agents through building software"
DISCARD_KEYS = {"at", "parent", "failure", "closest", "changed", "kind"}


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def changed(a, b):
    """The spec's changed_chars on texts: for every non-equal opcode, the larger of its two spans."""
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
               if tag != "equal")


class Base(rm.Base):
    """A temporary evolver home with the fake Regista checkout seeded as `seed`, and the stand-in claude."""

    def seed_files(self, genome="seed"):
        return {rel: rm.read_bytes(os.path.join(self.genome_dir(genome), "files", rel)) for rel in EVOLVABLE}

    def traits(self, genome="seed"):
        return self.read_json(self.genome_dir(genome), "genome.json")["traits"]

    def child(self, child_id, parents, files=None, traits=None):
        self.gn.make_child(self.evo, self.profile, self.checkout, child_id, parents, files=files, traits=traits)

    def discarded(self):
        path = os.path.join(self.evo, "genomes", "discarded.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def genomes_without_discards(self):
        snap = snapshot(os.path.join(self.evo, "genomes")) or {}
        return {k: v for k, v in snap.items() if os.path.basename(k) != "discarded.jsonl"}

    def assert_discarded(self, call, closest, failure, kind, child, changed=None, high=None):
        """call raises NearDuplicate naming closest and the changed characters, writes no child, logs one discard."""
        before = self.genomes_without_discards()
        lines_before = len(self.discarded())
        self.assertTrue(issubclass(self.gn.NearDuplicate, self.gn.GenomeError), "NearDuplicate is a GenomeError")
        with self.assertRaises(self.gn.NearDuplicate) as caught:
            call()
        self.assertTrue(is_plain_error(caught.exception))
        message = str(caught.exception)
        self.assertRegex(message, rf"(?<![\w-]){re.escape(closest)}(?![\w-])", f"the error names {closest}: {message}")
        number = str(changed) if changed is not None else r"\d+"
        self.assertRegex(message, rf"(?<![\w.-]){number}(?![\w.])", f"the error gives the changed characters: {message}")
        self.assertEqual(self.genomes_without_discards(), before, "no child written, nothing else changed in genomes/")
        self.assertFalse(os.path.exists(self.genome_dir(child)))
        lines = self.discarded()
        self.assertEqual(len(lines), lines_before + 1, f"one line in genomes/discarded.jsonl: {lines}")
        line = lines[-1]
        self.assertEqual(set(line), DISCARD_KEYS, line)
        self.assertEqual(line["parent"], "seed")
        self.assertEqual(line["failure"], failure["id"], "the failure the mutation aimed at, by its id")
        self.assertEqual(line["closest"], closest)
        self.assertEqual(line["kind"], kind)
        self.assertTrue(line["at"])
        self.assertIsInstance(line["changed"], int)
        self.assertNotIsInstance(line["changed"], bool)
        self.assertGreaterEqual(line["changed"], 0)
        if changed is not None:
            self.assertEqual(line["changed"], changed)
        if high is not None:
            self.assertLessEqual(line["changed"], high, f"changed characters at most the limit: {line}")
        return line


# LOOP-4: parent weights, picking and children ------------------------------------------------------------------------

ENTRIES = [{"id": "a", "score": 0.9, "children": 0}, {"id": "b", "score": 0.5, "children": 1},
           {"id": "c", "score": 0.2, "children": 0}, {"id": "d", "score": 0.9, "children": 3},
           {"id": "e", "score": 0.0, "children": 0}, {"id": "f", "score": 1.0, "children": 2}]


def expected(entries, sharpness=10, midpoint=0.5):
    raw = {e["id"]: sigmoid(sharpness * (e["score"] - midpoint)) / (1 + e["children"]) for e in entries}
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


class ParentWeights(unittest.TestCase):
    def setUp(self):
        self.gn = rm.import_scripts("genome")

    def test_weights_follow_the_formula_rise_with_score_fall_with_children_and_sum_to_one(self):  # covers: LOOP-4
        got = self.gn.parent_weights(ENTRIES)
        self.assertEqual(set(got), {e["id"] for e in ENTRIES})
        for gid, p in expected(ENTRIES).items():
            self.assertAlmostEqual(got[gid], p, places=9, msg=f"{gid}: sigmoid(10 * (score - 0.5)) / (1 + children)")
        self.assertAlmostEqual(sum(got.values()), 1.0, places=9)
        self.assertGreater(got["a"], got["b"], "a higher score weighs more")
        self.assertGreater(got["b"], got["c"])
        self.assertAlmostEqual(got["a"] / got["d"], 4.0, places=9, msg="three children divide the weight by four")
        custom = self.gn.parent_weights(ENTRIES, sharpness=4, midpoint=0.3)
        for gid, p in expected(ENTRIES, 4, 0.3).items():
            self.assertAlmostEqual(custom[gid], p, places=9, msg=f"{gid} with sharpness 4 and midpoint 0.3")
        self.assertEqual(self.gn.parent_weights([{"id": "only", "score": 0.1, "children": 7}]), {"only": 1.0})

    def test_empty_list_out_of_range_score_and_negative_children_are_plain_errors(self):  # covers: LOOP-4
        cases = {
            "an empty list": [],
            "a score above 1": [{"id": "a", "score": 1.2, "children": 0}],
            "a negative score": [{"id": "a", "score": 0.5, "children": 0}, {"id": "b", "score": -0.1, "children": 0}],
            "a negative child count": [{"id": "a", "score": 0.5, "children": -1}],
        }
        for what, entries in cases.items():
            with self.subTest(case=what):
                with self.assertRaises(Exception) as caught:
                    self.gn.parent_weights(entries)
                self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain error, got {caught.exception!r}")
                with self.assertRaises(Exception) as caught:
                    self.gn.pick_parent(entries, random.Random(1))
                self.assertTrue(is_plain_error(caught.exception), f"{what}: pick_parent too, got {caught.exception!r}")

    def test_seeded_draws_match_the_probabilities(self):  # covers: LOOP-4
        probabilities = expected(ENTRIES)
        rng = random.Random(20261031)
        draws = 20000
        counts = {e["id"]: 0 for e in ENTRIES}
        for _ in range(draws):
            picked = self.gn.pick_parent(ENTRIES, rng)
            self.assertIn(picked, counts)
            counts[picked] += 1
        for gid, p in probabilities.items():
            self.assertAlmostEqual(counts[gid] / draws, p, delta=0.015,
                                   msg=f"{gid}: drawn {counts[gid]} times in {draws}, expected about {p:.3f}")
        again = [self.gn.pick_parent(ENTRIES, random.Random(7)) for _ in range(3)]
        self.assertEqual(len(set(again)), 1, "the same seed gives the same draw")


class Children(Base):
    def test_children_counts_stored_genomes_that_list_the_id_among_their_parents(self):  # covers: LOOP-4
        self.child("a", ["seed"], files={"agents/builder.md": (BUILDER + "Child a.\n").encode()})
        self.child("b", ["seed"], files={"agents/builder.md": (BUILDER + "Child b.\n").encode()})
        self.child("c", ["a"], files={"agents/builder.md": (BUILDER + "Child c.\n").encode()})
        self.child("d", ["a", "seed"], files={"agents/builder.md": (BUILDER + "Child d.\n").encode()})
        for gid, n in (("seed", 3), ("a", 2), ("b", 0), ("c", 0), ("d", 0)):
            with self.subTest(genome=gid):
                self.assertEqual(self.gn.children(self.evo, gid), n)


# LOOP-5: near-duplicates ---------------------------------------------------------------------------------------------

class NearDuplicate(Base):
    def nd(self, contents, traits=None, max_changed=10):
        return self.gn.near_duplicate(self.evo, self.profile, contents, self.traits() if traits is None else traits,
                                      max_changed)

    def test_changed_chars_counts_the_characters_that_differ(self):  # covers: LOOP-5
        for name, text, n in (("identical", BUILDER, 0), ("one character", FEW, 1), ("10 added", AT_10, 10),
                              ("11 added", OVER_11, 11), ("40 added", AT_40, 40), ("41 added", OVER_41, 41),
                              ("a rewritten line", OVER, 48), ("the default rewrite", rm.NEW_BUILDER, 21),
                              ("a sentence", BIG_PLUS, None)):
            with self.subTest(case=name):
                base = BIG if text is BIG_PLUS else BUILDER
                if n is not None:
                    self.assertEqual(changed(base, text), n, "the fixture matches the spec's definition")
                self.assertEqual(self.gn.changed_chars(base.encode(), text.encode()), changed(base, text))

    def test_near_duplicate_sums_changed_characters_over_files_with_the_same_traits(self):  # covers: LOOP-5
        files = self.seed_files()
        with_builder = lambda text: {**files, "agents/builder.md": text.encode()}  # noqa: E731
        self.assertEqual(tuple(self.nd(files)), ("seed", 0), "identical files and traits: the seed, 0 changed")
        self.assertEqual(tuple(self.nd(with_builder(FEW))), ("seed", 1), "a few characters changed: discarded")
        self.assertEqual(tuple(self.nd(with_builder(AT_10))), ("seed", 10), "exactly 10 changed: discarded (inclusive)")
        self.assertIsNone(self.nd(with_builder(OVER_11)), "11 changed, just over 10: kept")
        self.assertIsNone(self.nd(with_builder(rm.NEW_BUILDER)), "the 21-character default rewrite: kept")
        self.assertEqual(tuple(self.nd(with_builder(OVER_11), max_changed=11)), ("seed", 11), "a higher limit")
        self.assertEqual(tuple(self.nd(with_builder(AT_40), max_changed=40)), ("seed", 40), "40 changed at 40")
        self.assertIsNone(self.nd(with_builder(OVER_41), max_changed=40), "41 changed at 40: kept")
        self.assertEqual(tuple(self.nd(with_builder(OVER_41), max_changed=41)), ("seed", 41), "41 changed at 41")
        self.assertEqual(tuple(self.nd(files, max_changed=0)), ("seed", 0), "at 0 an exact copy is still discarded")
        self.assertIsNone(self.nd(with_builder(FEW), max_changed=0), "at 0 one changed character is kept")
        other = dict(self.traits(), **{"review.max_rounds": 3})
        self.assertIsNone(self.nd(files, traits=other), "identical files with different traits are kept")
        # The changed characters add up over the files: 7 in one file is a duplicate, 7 in each of two is not.
        reviewer = EVOLVABLE["agents/reviewer.md"]
        self.assertEqual((changed(BUILDER, BUILDER + SMALL), changed(reviewer, reviewer + SMALL)), (7, 7))
        both = {**with_builder(BUILDER + SMALL), "agents/reviewer.md": (reviewer + SMALL).encode()}
        self.assertEqual(tuple(self.nd(with_builder(BUILDER + SMALL))), ("seed", 7))
        self.assertIsNone(self.nd(both), "7 + 7 = 14 changed over two files: kept at 10")
        self.assertEqual(tuple(self.nd(both, max_changed=14)), ("seed", 14))

    def test_the_closest_genome_is_the_one_with_the_fewest_changed_characters(self):  # covers: LOOP-5
        self.child("sib", ["seed"], files={"agents/builder.md": (BUILDER + SMALL).encode()})
        text = BUILDER + SMALL.replace(".", "!")
        self.assertEqual((changed(BUILDER + SMALL, text), changed(BUILDER, text)), (1, 7))
        candidate = {**self.seed_files(), "agents/builder.md": text.encode()}
        self.assertEqual(tuple(self.nd(candidate)), ("sib", 1), "1 changed from the sibling, 7 from the seed")
        self.assertEqual(tuple(self.nd(self.seed_files())), ("seed", 0), "the seed's own files: the seed, not the sibling")

    def test_one_sentence_added_to_a_prompt_of_a_few_kilobytes_is_kept(self):  # covers: LOOP-5
        self.assertGreater(len(BIG.encode()), 2000)
        self.assertGreater(changed(BIG, BIG_PLUS), 10)
        self.assertGreater(ratio(BIG, BIG_PLUS), 0.97, "a whole-file similarity would call this a near-duplicate")
        source = os.path.join(self.tmp, "big-checkout")
        shutil.copytree(self.checkout, source)
        write(os.path.join(source, "agents", "builder.md"), BIG)
        self.gn.seed(self.evo, self.profile, source, "big")
        files = self.seed_files("big")
        self.assertIsNone(self.nd({**files, "agents/builder.md": BIG_PLUS.encode()}, traits=self.traits("big")))
        major, _blocker = self.review_replay()
        self.answer(text=BIG_PLUS)
        self.ev.mutate(self.evo, self.config, self.profile, source, "big", major, "agents/builder.md", "big-m1")
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir("big-m1"), "genome.json")),
                        "one added sentence with the default duplicate_max_changed: the child is kept")
        self.assertEqual(self.discarded(), [])

    @staticmethod
    def closest(stored, text):
        """(id, changed) of the stored builder prompt with the fewest changed characters (the other files are equal)."""
        return min(((gid, changed(old, text)) for gid, old in stored.items()), key=lambda pair: pair[1])

    def test_mutate_discards_an_exact_copy_and_a_few_changed_characters(self):  # covers: LOOP-5
        # No duplicate_max_changed in evolve.json: the default 10. Every stored genome of the profile counts, siblings
        # included, so each expectation is measured against all of them.
        major, _blocker = self.review_replay()
        self.child("sib", ["seed"], files={"agents/builder.md": rm.NEW_BUILDER.encode()})
        stored = {"seed": BUILDER, "sib": rm.NEW_BUILDER}
        for text, closest, n, child in ((rm.NEW_BUILDER, "sib", 0, "seed-m1"),  # an exact copy of the sibling
                                        (FEW, "seed", 1, "seed-m2"),  # 22 from the sibling
                                        (AT_10, "seed", 10, "seed-m3")):  # 17 from the sibling; 10 is inclusive
            with self.subTest(child=child):
                self.assertEqual(self.closest(stored, text), (closest, n), "the fixture matches the spec's definition")
                self.answer(text=text)
                self.assert_discarded(lambda: self.mutate(major, child=child), closest, major, "rewrite", child,
                                      changed=n)
        self.assertEqual(self.closest(stored, OVER_11), ("seed", 11), "11 from the seed, 18 from the sibling")
        self.answer(text=OVER_11)
        self.mutate(major, child="seed-m4")
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir("seed-m4"), "genome.json")),
                        "11 changed characters from every stored genome, over the default 10: the child is kept")
        self.assertEqual(len(self.discarded()), 3, "a kept child logs no discard")
        # The kept child is now a stored genome too: a text 5 characters from it but 16 from the seed (23 from the
        # sibling) is discarded against the kept child, the closest.
        stored["seed-m4"] = OVER_11
        near_kept = OVER_11 + "Now.\n"
        self.assertEqual({gid: changed(old, near_kept) for gid, old in stored.items()},
                         {"seed": 16, "sib": 23, "seed-m4": 5}, "the fixture matches the spec's definition")
        self.assertEqual(self.closest(stored, near_kept), ("seed-m4", 5))
        self.answer(text=near_kept)
        self.assert_discarded(lambda: self.mutate(major, child="seed-m5"), "seed-m4", major, "rewrite", "seed-m5",
                              changed=5)

    def test_duplicate_max_changed_from_evolve_json_is_honoured(self):  # covers: LOOP-5
        major, _blocker = self.review_replay()
        self.configure(duplicate_max_changed=100)
        self.answer(text=OVER)
        self.assert_discarded(lambda: self.mutate(major, child="seed-m1"), "seed", major, "rewrite", "seed-m1",
                              changed=48, high=100)
        self.configure(duplicate_max_changed=0)
        self.answer(text=FEW)
        self.mutate(major, child="seed-m2")
        self.assertTrue(os.path.isdir(self.genome_dir("seed-m2")), "at 0 one changed character is kept")
        # The parent's own text unchanged is the reflection step's plain "unchanged" error, checked first: not a
        # near-duplicate, and nothing is written under genomes/, discarded.jsonl included.
        self.answer(text=BUILDER)
        before = snapshot(os.path.join(self.evo, "genomes"))
        with self.assertRaises(Exception) as caught:  # whichever plain error the reflection step raises
            self.mutate(major, child="seed-m3")
        self.assertNotIsInstance(caught.exception, self.gn.NearDuplicate, "the parent unchanged is not a near-duplicate")
        self.assertTrue(is_plain_error(caught.exception))
        self.assertRegex(str(caught.exception), r"(?i)identical|unchanged|same|no change|did ?n[o']t change")
        self.assertEqual(snapshot(os.path.join(self.evo, "genomes")), before, "nothing under genomes/ changed")
        self.assertEqual(len(self.discarded()), 1, "no discarded.jsonl line for the parent unchanged")
        # An exact copy of another stored genome (the sibling kept above) is still discarded at 0.
        self.answer(text=FEW)
        self.assert_discarded(lambda: self.mutate(major, child="seed-m4"), "seed-m2", major, "rewrite", "seed-m4",
                              changed=0)
        for bad in (-1, 2.5, "many", True):
            with self.subTest(duplicate_max_changed=bad):
                rm.write_json(os.path.join(self.evo, "evolve.json"),
                              {"holdout_pct": 0, "claude_command": self.claude, "task_model": "haiku",
                               "reflection_model": rm.REFLECTION_MODEL, "duplicate_max_changed": bad})
                before = self.genomes_without_discards()
                with self.assertRaises(Exception) as caught:
                    config = self.ev.load_config(self.evo)
                    self.ev.mutate(self.evo, config, self.profile, self.checkout, "seed", major, "agents/builder.md",
                                   "seed-bad")
                self.assertTrue(is_plain_error(caught.exception), f"a plain error, got {caught.exception!r}")
                self.assertIn("duplicate_max_changed", str(caught.exception))
                self.assertEqual(self.genomes_without_discards(), before)

    def test_mutate_trait_aimed_discards_a_trait_change_landing_on_a_stored_genome(self):  # covers: LOOP-5
        major, _blocker = self.review_replay()
        self.assertEqual(self.traits()["review.max_rounds"], 4, "the seed sits at the top of 2 to 4")
        self.child("sib-3", ["seed"], traits={"review.max_rounds": 3})
        self.child("sib-2", ["seed"], traits={"review.max_rounds": 2})
        # A move from 4 lands on 3 or 2, both stored with the seed's files. Which one depends on the step taken, so the
        # closest genome is read from the record.
        before = self.genomes_without_discards()
        with self.assertRaises(self.gn.NearDuplicate) as caught:
            self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "review.max_rounds",
                                       "seed-t1", random.Random(7))
        self.assertEqual(self.genomes_without_discards(), before, "no child written")
        lines = self.discarded()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertEqual(set(line), DISCARD_KEYS, line)
        self.assertIn(line["closest"], ("sib-2", "sib-3"))
        self.assertRegex(str(caught.exception), rf"(?<![\w-]){line['closest']}(?![\w-])")
        self.assertEqual((line["parent"], line["failure"], line["kind"]), ("seed", major["id"], "trait"))
        self.assertEqual(line["changed"], 0)

    def test_mutate_trait_aimed_honours_its_max_changed_argument(self):  # covers: LOOP-5
        major, _blocker = self.review_replay()
        # Both moves from 4 land on a stored genome exactly 10 characters away from the seed's files (the candidate's).
        self.assertEqual(changed(BUILDER, AT_10), 10)
        self.child("sib-3", ["seed"], files={"agents/builder.md": AT_10.encode()}, traits={"review.max_rounds": 3})
        self.child("sib-2", ["seed"], files={"agents/builder.md": AT_10.encode()}, traits={"review.max_rounds": 2})
        with self.assertRaises(self.gn.NearDuplicate):
            self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "review.max_rounds",
                                       "seed-t1", random.Random(7))
        self.assertEqual([line["changed"] for line in self.discarded()], [10], "the default 10 discards it")
        self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "review.max_rounds",
                                   "seed-t2", random.Random(7), max_changed=0)
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir("seed-t2"), "genome.json")),
                        "with max_changed 0, ten changed characters are enough to keep it")
        self.assertEqual(len(self.discarded()), 1, "a kept child logs no discard")

    def test_a_trait_change_with_no_stored_twin_is_kept(self):  # covers: LOOP-5
        major, _blocker = self.review_replay()
        # Both moves from 4 land on a stored genome 11 characters away from the seed's files: just over the default 10.
        self.assertEqual(changed(BUILDER, OVER_11), 11)
        self.child("sib-3", ["seed"], files={"agents/builder.md": OVER_11.encode()}, traits={"review.max_rounds": 3})
        self.child("sib-2", ["seed"], files={"agents/builder.md": OVER_11.encode()}, traits={"review.max_rounds": 2})
        self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "review.max_rounds",
                                   "seed-t1", random.Random(7))
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir("seed-t1"), "genome.json")),
                        "same files as the seed but different traits, and 11 characters from the siblings: kept")
        self.assertEqual(self.discarded(), [])


# LOOP-13: scores and the reflection prompt's wording -----------------------------------------------------------------

def set_requirements(folder, passing, of):
    path = os.path.join(folder, "result.json")
    with open(path, encoding="utf-8") as f:
        result = json.load(f)
    result["scores"]["requirements"] = {"value": len(passing) / len(of), "failed": len(passing) < len(of),
                                        "passing": passing, "not_proven": [i for i in of if i not in passing],
                                        "of": of, "better": "higher"}
    rm.write_json(path, result)


class GenomeScore(Base):
    def test_score_is_the_mean_over_finished_replays_and_none_without_one(self):  # covers: LOOP-13
        fail_gates = {"tests": {"status": "fail", "exit": 1, "seconds": 1.0, "tail": "FAILED"}}
        rm.write_replay(self.evo, "c1-full", "seed", outcome="approved")  # 0.5 + 0.5 = 1
        half = rm.write_replay(self.evo, "c2-half", "seed", outcome="approved")
        set_requirements(half, ["DBL-1"], ["DBL-1", "DBL-2"])  # 0.25 + 0.5 = 0.75
        rm.write_replay(self.evo, "c3-ended", "seed", outcome="ended")  # not approved: 0.5 + 0 = 0.5
        rm.write_replay(self.evo, "c4-gates", "seed", outcome="approved", final_gates=fail_gates)  # 0.5 + 0 = 0.5
        rm.write_replay(self.evo, "c5-hard", "seed", outcome="hard_failure",
                        hard_failure="the agent pushed to origin")  # a hard failure counts 0
        # Unfinished replays are ignored: one still running (no result.json), one without an outcome.
        write(os.path.join(self.evo, "runs", GEN, "c6-running", "seed", "stream.jsonl"), "")
        unfinished = rm.write_replay(self.evo, "c7-unfinished", "seed", outcome="approved")
        with open(os.path.join(unfinished, "result.json"), encoding="utf-8") as f:
            result = json.load(f)
        result["outcome"] = None
        rm.write_json(os.path.join(unfinished, "result.json"), result)
        # Other genomes and other generations don't count.
        rm.write_replay(self.evo, "c1-full", "other", outcome="hard_failure", hard_failure="x")
        rm.write_replay(self.evo, "c1-full", "seed", outcome="hard_failure", hard_failure="x", generation="g2")
        self.assertAlmostEqual(self.ev.genome_score(self.evo, GEN, "seed"), (1 + 0.75 + 0.5 + 0.5 + 0) / 5, places=9)
        self.assertAlmostEqual(self.ev.genome_score(self.evo, GEN, "other"), 0.0, places=9)
        self.assertAlmostEqual(self.ev.genome_score(self.evo, "g2", "seed"), 0.0, places=9)
        # Without a finished replay there is no score.
        write(os.path.join(self.evo, "runs", "g3", "c1-full", "seed", "stream.jsonl"), "")
        self.assertIsNone(self.ev.genome_score(self.evo, "g3", "seed"), "only an unfinished replay: no score")
        self.assertIsNone(self.ev.genome_score(self.evo, "g4", "seed"), "no replay at all: no score")
        self.assertIsNone(self.ev.genome_score(self.evo, GEN, "nobody"))


class ReflectionWording(Base):
    FAILURE = {"id": "h1-greet/hseed/finding-1", "kind": "review_finding", "case": "h1-greet",
               "summary": "The greeting runs past one line.", "evidence": "prompts/greet.md:1 the greeting is long."}

    def haiku_prompt(self, describe, child):
        data = dict(limits.PROFILE)
        if describe is not None:
            data["describe"] = describe
        path = os.path.join(self.tmp, "profiles", f"{child}.json")
        rm.write_json(path, data)
        source = os.path.join(self.tmp, f"haiku-{child}")
        for rel, text in limits.SOURCE.items():
            write(os.path.join(source, rel), text)
        profile = self.gn.load_profile(path)
        self.gn.seed(self.evo, profile, source, f"hseed-{child}")
        addition = {"with-describe": "Keep it to one line.\n", "without-describe": "Mind the syllables, please.\n"}
        self.answer(text=limits.GREET + addition[child])
        calls = len(self.calls())
        self.ev.mutate(self.evo, self.config, profile, source, f"hseed-{child}", self.FAILURE, "prompts/greet.md",
                       child)
        return self.prompt(self.calls()[calls])

    @staticmethod
    def prompt(call):
        return "\n".join(call["argv"]) + "\n" + call["stdin"] + "\n" + "\n".join(call["files"].values())

    def test_the_prompt_describes_the_target_from_the_profile(self):  # covers: LOOP-13
        major, _blocker = self.review_replay()
        self.answer()
        self.mutate(major)
        regista = self.prompt(self.calls()[0])
        self.assertIn(REGISTA_DESCRIBE, regista, "Regista's profile describes its files (genome/profiles/regista.json)")
        described = self.haiku_prompt("a skill whose prompts write haiku for greeting cards (describe-marker-31)",
                                      "with-describe")
        self.assertIn("describe-marker-31", described, "a profile's describe is used")
        self.assertNotRegex(described, r"(?i)building software|coding agent")
        plain = self.haiku_prompt(None, "without-describe")
        self.assertIn("haiku-limits", plain, "without describe, the files are the instructions of <profile name>")
        self.assertNotRegex(plain, r"(?i)building software|coding agent",
                            "without describe, nothing says the target builds software")
        with open(GENOME_PY, encoding="utf-8") as f:
            self.assertNotIn("building software", f.read().lower(), "genome.py holds no fixed description of a target")


if __name__ == "__main__":
    unittest.main()
