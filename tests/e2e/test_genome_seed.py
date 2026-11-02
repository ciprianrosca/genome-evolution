"""End-to-end: the seed genome of this repository (specs .regista/specs/genome-seed.json, GNM-1; genome-traits.json,
GNM-7; genome-limits.json, GNM-9: show lists the never globs and caps, and check_individual passes on the fresh plugin).

`evolve.py genome seed` runs with its defaults (id seed, profile genome/profiles/regista.json, source the Regista checkout) and
REGISTA_EVOLVE_HOME in a temporary folder; the expanded genomes/seed/plugin/ must pass `claude plugin validate`
(skipped when the claude command isn't on PATH), and `evolve.py genome show seed` must print the id, the number of
evolvable files and the trait values. The repository itself is only read.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import glob
import json
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


def reviewer_model():
    """The model: value in agents/reviewer.md's front matter."""
    with open(os.path.join(REGISTA_CHECKOUT, "agents", "reviewer.md"), encoding="utf-8") as f:
        text = f.read()
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


class SeedGenomeOfThisRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-e2e-genome-")
        cls.evo = os.path.join(cls.tmp, "evolve-home")
        cls.env = {k: v for k, v in os.environ.items()
                   if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_"))}
        cls.env.update({"REGISTA_EVOLVE_HOME": cls.evo, "PYTHONDONTWRITEBYTECODE": "1"})
        cls.repo_entries = sorted(os.listdir(REGISTA_CHECKOUT))
        cls.seed = cls.evolve("genome", "seed")
        cls.show = cls.evolve("genome", "show", "seed")
        cls.plugin = os.path.join(cls.evo, "genomes", "seed", "plugin")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def evolve(cls, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=cls.tmp, capture_output=True, text=True, encoding="utf-8",
                              env=cls.env, timeout=300)

    def setUp(self):
        self.assertEqual(self.seed.returncode, 0, f"genome seed failed:\n{output(self.seed)}")
        self.assertTrue(os.path.isdir(self.plugin), f"no expanded plugin at {self.plugin}")

    @unittest.skipIf(shutil.which("claude") is None, "the claude command isn't on PATH, so the plugin can't be validated")
    def test_expanded_seed_plugin_passes_claude_plugin_validate(self):  # covers: GNM-1
        r = subprocess.run(["claude", "plugin", "validate", self.plugin], cwd=self.tmp, capture_output=True,
                           text=True, encoding="utf-8", env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, f"claude plugin validate {self.plugin}:\n{output(r)}")

    def test_genome_show_prints_id_file_count_and_traits(self):  # covers: GNM-1
        self.assertEqual(self.show.returncode, 0, f"genome show seed failed:\n{output(self.show)}")
        text = self.show.stdout
        self.assertRegex(text, r"\bseed\b", "show prints the genome's id")
        count = len(glob.glob(os.path.join(REGISTA_CHECKOUT, "agents", "*.md"))) + \
            len(glob.glob(os.path.join(REGISTA_CHECKOUT, "skills", "*", "SKILL.md")))
        self.assertGreater(count, 0)
        self.assertRegex(text, rf"(?<![\d.]){count}(?![\d.])", f"show prints the file count ({count})")
        with open(os.path.join(REGISTA_CHECKOUT, "templates", "config.json"), encoding="utf-8") as f:
            config = json.load(f)
        values = [config["context"]["warn_pct"], config["context"]["limit_pct"],
                  config["review"]["min_changed_lines"], config["review"]["max_rounds"], reviewer_model()]
        for value in values:
            self.assertIsNotNone(value)
            self.assertRegex(text, rf"(?<![\w.]){re.escape(str(value))}(?![\w.])", f"show prints the trait value {value}")
        self.assertEqual(sorted(os.listdir(REGISTA_CHECKOUT)), self.repo_entries, "seeding wrote nothing into the repository")

    def test_genome_show_prints_each_traits_value_and_bounds(self):  # covers: GNM-7
        # Regista's bounds as spec genome-traits sets them in genome/profiles/regista.json: numbers are min, max and step.
        bounds = {"context.warn_pct": ("45", "60", "5"), "context.limit_pct": ("55", "75", "5"),
                  "review.min_changed_lines": ("5", "30", "5"), "review.max_rounds": ("2", "4", "1"),
                  "reviewer.model": ("opus", "sonnet")}
        self.assertEqual(self.show.returncode, 0, f"genome show seed failed:\n{output(self.show)}")
        text = self.show.stdout
        with open(os.path.join(self.evo, "genomes", "seed", "genome.json"), encoding="utf-8") as f:
            traits = json.load(f)["traits"]
        lines = text.splitlines()
        for name, words in bounds.items():
            with self.subTest(trait=name):
                start = next((i for i, line in enumerate(lines) if name in line), None)
                self.assertIsNotNone(start, f"show names the trait {name}:\n{text}")
                # The trait's part of the output: its line and any lines up to the next trait's name.
                end = next((i for i in range(start + 1, len(lines)) if any(n in lines[i] for n in bounds)), len(lines))
                part = "\n".join(lines[start:end])
                for word in (str(traits[name]), *words):
                    self.assertRegex(part, rf"(?<![\w.]){re.escape(word)}(?![\w.])",
                                     f"show prints {word} for {name} (its value and its bounds):\n{part}")

    def test_genome_show_lists_never_globs_and_caps_and_the_fresh_plugin_passes_its_check(self):  # covers: GNM-9
        # Regista's limits as spec genome-limits sets them in genome/profiles/regista.json.
        never = ("scripts/**", "hooks/**", ".claude-plugin/**", ".codex-plugin/**", "tests/**")
        # The skill cap may be printed in bytes or in KB, as show prints the total size.
        caps = {"agents/*.md": r"(?<![\d.])50(?![\d.])",
                "skills/*/SKILL.md": r"(?<![\d.])(15360(?![\d.])|15(\.0)?\s?[kK]i?B)"}
        self.assertEqual(self.show.returncode, 0, f"genome show seed failed:\n{output(self.show)}")
        text = self.show.stdout
        for glob_ in never:
            self.assertIn(glob_, text, f"show lists the never glob {glob_}:\n{text}")
        for glob_, limit in caps.items():
            self.assertTrue(any(glob_ in line and re.search(limit, line) for line in text.splitlines()),
                            f"show lists the cap on {glob_} with its limit on one line:\n{text}")
        check = ("import sys; sys.path.insert(0, sys.argv[1]); import genome; "
                 "genome.check_individual(sys.argv[2], 'seed'); print('ok')")
        r = subprocess.run([PY, "-c", check, os.path.join(GENOME, "scripts"), self.evo], cwd=self.tmp,
                           capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, f"check_individual on the fresh seed plugin:\n{output(r)}")
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
