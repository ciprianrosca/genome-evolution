"""End-to-end: ranking the candidate parents of a generation (spec .regista/specs/parents-duplicates.json, LOOP-14).

`evolve.py genome seed` seeds this repository with its defaults (id seed, profile genome/profiles/regista.json); two children
of the seed are made with evolve.mutate_trait_aimed (one moves review.max_rounds, the other review.min_changed_lines,
so neither is a near-duplicate); replay records for the three are written by hand under runs/g1/<case>/<genome>/;
then `evolve.py parents --generation g1` must print each genome with its score, its number of children and its
probability of being picked, highest probability first.

Scores (0.5 x requirements value + 0.5 when approved with passing gates and no hard failure) and probabilities
(sigmoid(10 x (score - 0.5)) / (1 + children), normalised):
  seed     approved, 3 of 4 requirements: 0.875, 2 children: probability about 0.246
  seed-t1  approved, 2 of 4 requirements: 0.75,  0 children: probability about 0.697
  seed-t2  ended,    2 of 4 requirements: 0.25,  0 children: probability about 0.057
so the order is seed-t1, seed, seed-t2: the highest score isn't first, because the seed already has two children.

Interface assumed: mutate_trait_aimed(home, profile, source, parent, failure, trait, child_id, rng) accepts a failure
dict built by hand; numbers are printed as decimals (two or three places) or percentages (zero or one place); the
child count reads "<n> child(ren)" or "no children"; each genome's details sit on the line that names it.
REGISTA_EVOLVE_HOME and HOME are temporary folders; the repository is only read.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# The genome folder, and the Regista checkout that holds it (reached only by path and as an outside program).
GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REGISTA_CHECKOUT = os.path.dirname(GENOME)
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
PY = sys.executable
GEN = "g1"

MAKE_CHILDREN = r'''
import json, random, sys
scripts, repo, home = sys.argv[1], sys.argv[2], sys.argv[3]  # the genome's scripts, the Regista checkout, the home
sys.path.insert(0, scripts)
import evolve, genome
profile = genome.load_profile(scripts + "/../profiles/regista.json", source=repo)
failure = {"id": "m1-double/seed/finding-1", "kind": "review_finding", "case": "m1-double",
           "summary": "The reviewer found an unchecked edge case.", "evidence": "src/double.py:7 double('') crashes."}
evolve.mutate_trait_aimed(home, profile, repo, "seed", failure, "review.max_rounds", "seed-t1", random.Random(3))
evolve.mutate_trait_aimed(home, profile, repo, "seed", failure, "review.min_changed_lines", "seed-t2", random.Random(4))
print("made")
'''


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def forms(value, percent):
    """The ways a number may be printed: as a decimal, or as a percentage when percent is true."""
    if percent:
        return {f"{value * 100:.0f}%", f"{value * 100:.1f}%", f"{value * 100:.0f} %", f"{value * 100:.1f} %"}
    shown = {f"{value:.2f}", f"{value:.3f}"}
    return shown | {s.rstrip("0").rstrip(".") for s in shown if "." in s and s.rstrip("0").rstrip(".") not in ("0", "1")}


def shows(line, value):
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", line) for p in (False, True) for f in forms(value, p))


class ParentsFromTheCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-e2e-parents-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evo = os.path.join(self.tmp, "evolve-home")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        self.env = {k: v for k, v in os.environ.items()
                    if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_", "ANTHROPIC_"))}
        self.env.update({"REGISTA_EVOLVE_HOME": self.evo, "HOME": home, "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def run_py(self, *args):
        return subprocess.run([PY, *args], cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, timeout=300)

    def write_replay(self, case, genome, outcome, passing):
        """A finished replay with final gates passing and `passing` of four requirements proven."""
        ids = ["DBL-1", "DBL-2", "DBL-3", "DBL-4"]
        folder = os.path.join(self.evo, "runs", GEN, case, genome)
        write(os.path.join(folder, "result.json"), json.dumps({
            "project": "doubler", "case": case, "genome": genome, "generation": GEN, "model": "haiku",
            "outcome": outcome, "stop_reason": "set by hand", "hard_failure": None,
            "final_gates": {"tests": {"status": "pass", "exit": 0, "seconds": 1.0, "tail": "OK"}}, "claude_exit": 0,
            "scores": {"gates": {"value": 1, "failed": False, "results": {"tests": "pass"}},
                       "requirements": {"value": passing / len(ids), "failed": passing < len(ids),
                                        "passing": ids[:passing], "not_proven": ids[passing:], "of": ids},
                       "correct_blocked": {"value": 1, "failed": False, "blocked": False, "should_block": False}},
            "tokens": {"used": 1000}}, indent=2) + "\n")
        write(os.path.join(folder, "stream.jsonl"), json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "session_id": "s"}) + "\n")

    def test_parents_ranks_seed_and_children_by_probability_with_score_and_children(self):  # covers: LOOP-14
        seeded = self.run_py(EVOLVE, "genome", "seed")
        self.assertEqual(seeded.returncode, 0, f"genome seed failed:\n{output(seeded)}")
        made = self.run_py("-c", MAKE_CHILDREN, os.path.join(GENOME, "scripts"), REGISTA_CHECKOUT, self.evo)
        self.assertEqual(made.returncode, 0, f"making the two children failed:\n{output(made)}")
        self.write_replay("m1-double", "seed", "approved", 3)
        self.write_replay("m1-double", "seed-t1", "approved", 2)
        self.write_replay("m1-double", "seed-t2", "ended", 2)

        r = self.run_py(EVOLVE, "parents", "--generation", GEN)
        self.assertEqual(r.returncode, 0, f"evolve.py parents failed:\n{output(r)}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)

        scores = {"seed": 0.875, "seed-t1": 0.75, "seed-t2": 0.25}
        children = {"seed": 2, "seed-t1": 0, "seed-t2": 0}
        raw = {g: 1 / (1 + math.exp(-10 * (s - 0.5))) / (1 + children[g]) for g, s in scores.items()}
        probability = {g: w / sum(raw.values()) for g, w in raw.items()}
        lines = r.stdout.splitlines()
        subject = {}  # line number: the genome the line is about (the first id it names; a parent may come later)
        for i, line in enumerate(lines):
            found = [(m.start(), gid) for gid in scores
                     for m in re.finditer(rf"(?<![\w-]){re.escape(gid)}(?![\w-])", line)]
            if found:
                subject[i] = min(found)[1]
        where = {}
        for gid in scores:
            named = [i for i, g in subject.items() if g == gid]
            self.assertEqual(len(named), 1, f"one line is about {gid}:\n{r.stdout}")
            where[gid] = named[0]
            line = lines[named[0]]
            with self.subTest(genome=gid):
                self.assertTrue(shows(line, scores[gid]), f"{gid}'s line shows its score {scores[gid]}: {line}")
                self.assertTrue(shows(line, probability[gid]),
                                f"{gid}'s line shows its probability {probability[gid]:.3f}: {line}")
                n = children[gid]
                pattern = r"(?i)\b(no|0) child(ren)?\b" if n == 0 else rf"(?i)(?<![\d.]){n} child(ren)?\b"
                self.assertRegex(line, pattern, f"{gid}'s line shows its {n} children")
        order = sorted(scores, key=lambda g: where[g])
        self.assertEqual(order, ["seed-t1", "seed", "seed-t2"], f"highest probability first:\n{r.stdout}")


if __name__ == "__main__":
    unittest.main()
