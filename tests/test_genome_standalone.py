"""evolve.py from a genome folder with no Regista checkout around it (spec .regista/specs/genome-standalone.json, SEP-8;
SEP-6, the genome's own tests passing in such a copy, is the end-to-end test genome/tests/e2e/test_genome_standalone.py).

Each class copies the genome folder (without __pycache__) into a temporary folder outside the repository, so the folder
holding the copy has no scripts/regista.py, and runs the copy's scripts/evolve.py as a separate process with a
temporary HOME and REGISTA_EVOLVE_HOME, CLAUDE_PROJECT_DIR, REGISTA_*, DAEDALUS_* and GENOME_REGISTA_CHECKOUT unset.
A stand-in `claude` first on PATH (and evolve.json's claude_command) records that it was started; it never should be.

- Nothing configured: `suite check` on a made-up suite and `genome seed --profile <made-up profile> --source <made-up
  folder>` work; the Regista-only commands (bench build, replay, a Regista generation, seeding Regista's profile
  without --source) stop with one plain line that names the variable GENOME_REGISTA_CHECKOUT and evolve.json's key
  regista_checkout, and write nothing.
- GENOME_REGISTA_CHECKOUT naming this repository: `bench build` on a tiny made-up project gets past that refusal and
  builds its cases. Naming a stand-in checkout whose scripts/regista.py logs each call (and passes it on to the real
  one): bench build runs Regista's command line from there.
- evolve.json regista_checkout and the variable both set: evolve.json wins.

The tests that need a real Regista checkout to pass calls on to are skipped without one (genome/tests/
regista_location.py).

Interface assumed: the refusal exits non-zero and is the only non-empty line printed; it contains
"GENOME_REGISTA_CHECKOUT", "evolve.json" and "regista_checkout"; a Regista generation is `generation --target
agents/builder.md --budget <n> --yes` with Regista's default profile; replay is `replay <case> --genome <id>`; the
stand-in checkout's scripts/regista.py is run either directly or through its scripts/regista launcher.

Run: cd genome/tests && env -u CLAUDE_PROJECT_DIR python3 -m unittest test_genome_standalone -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from regista_location import GENOME, REGISTA_CHECKOUT, REGISTA_TOOL, requires_regista  # noqa: E402
import test_genome_seed as seedtests  # noqa: E402  the fake Regista checkout and the made-up skill with its profile
import test_signal_check as sc  # noqa: E402  the small made-up Regista project bench build reads
import test_suite_files as sf  # noqa: E402  the made-up valid suite

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}
STANDIN_CLAUDE = '#!/bin/sh\necho "$@" >> @MARKER@\necho "the stand-in claude was started" >&2\nexit 97\n'
LOGGING_REGISTA = r'''import json, os, subprocess, sys
# A stand-in for Regista's command line: logs the call, then runs the real one as an outside program.
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
sys.exit(subprocess.run([sys.executable, @REAL@, *sys.argv[1:]]).returncode)
'''
LAUNCHER = '#!/bin/sh\nexec python3 "$(dirname "$0")/regista.py" "$@"\n'


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def output(r):
    return f"exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def copy_genome(parent):
    """A copy of the genome folder at <parent>/elsewhere/genome; returns the copy's evolve.py."""
    dest = os.path.join(parent, "elsewhere", "genome")
    shutil.copytree(GENOME, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return os.path.join(dest, "scripts", "evolve.py")


def logging_checkout(root, log):
    """A Regista checkout whose scripts/regista.py (and its launcher) log each call and pass it to the real one."""
    write(os.path.join(root, "scripts", "regista.py"),
          LOGGING_REGISTA.replace("@LOG@", repr(log)).replace("@REAL@", repr(REGISTA_TOOL)))
    write(os.path.join(root, "scripts", "regista"), LAUNCHER, 0o755)
    return root


def calls(log):
    if not os.path.isfile(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_refusals(log):
    return any("refusals" in c["argv"] and "--json" in c["argv"] for c in calls(log))


class Standalone(unittest.TestCase):
    """A copy of the genome folder outside the repository, a temporary HOME, a stand-in claude."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="genome-standalone-")
        cls.evolve_py = copy_genome(cls.tmp)
        cls.copy = os.path.dirname(os.path.dirname(cls.evolve_py))
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.marker = os.path.join(cls.tmp, "claude-started")
        cls.bin = os.path.join(cls.tmp, "bin")
        cls.claude = os.path.join(cls.bin, "claude")
        write(cls.claude, STANDIN_CLAUDE.replace("@MARKER@", repr(cls.marker)), 0o755)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        seedtests.make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertFalse(os.path.isfile(os.path.join(os.path.dirname(self.copy), "scripts", "regista.py")),
                         "the folder holding the copy is no Regista checkout")

    def new_home(self, config=None):
        """A fresh evolver home; with config, an evolve.json (claude_command always the stand-in)."""
        type(self).count += 1
        evo = os.path.join(self.tmp, f"evolve-{self.count}")
        os.makedirs(evo)
        with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 0, "claude_command": self.claude, **(config or {})}, f)
        return evo

    def env(self, evo, checkout=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "GENOME_REGISTA_CHECKOUT", "PYTHONPATH")
               and not k.startswith(("REGISTA_", "DAEDALUS_"))}
        env.update({"HOME": self.home, "REGISTA_EVOLVE_HOME": evo, "PYTHONDONTWRITEBYTECODE": "1", **GIT_ENV})
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        if checkout is not None:
            env["GENOME_REGISTA_CHECKOUT"] = checkout
        return env

    def evolve(self, evo, *args, checkout=None, cwd=None):
        return subprocess.run([sys.executable, self.evolve_py, *args], cwd=cwd or self.tmp, capture_output=True,
                              text=True, encoding="utf-8", env=self.env(evo, checkout), timeout=300)

    def project(self, name):
        """A tiny made-up Regista project with replayable steps and a few refusals, for bench build."""
        rows = sc.ten_refusals(["unsure"] * 5 + [None] * 5)
        return sc.build_project(os.path.join(self.tmp, "projects"), name, refusals=rows).root

    def assert_points_at_a_checkout(self, r, what):
        """One plain line, naming the variable and evolve.json's key."""
        text = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, f"{what} stops without a Regista checkout:\n{output(r)}")
        self.assertNotIn("Traceback", text, f"{what}: a plain line, not a crash:\n{output(r)}")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"{what} prints one plain line:\n{output(r)}")
        self.assertIn("GENOME_REGISTA_CHECKOUT", lines[0], f"{what}: the line names the variable: {lines[0]!r}")
        self.assertIn("evolve.json", lines[0], f"{what}: the line names evolve.json: {lines[0]!r}")
        self.assertRegex(lines[0], r"(?<![A-Za-z_])regista_checkout(?![A-Za-z_])",
                         f"{what}: the line names evolve.json's key regista_checkout: {lines[0]!r}")
        self.assertFalse(os.path.exists(self.marker), f"{what}: claude never started")

    def assert_no_refusal(self, r, what):
        self.assertNotIn("GENOME_REGISTA_CHECKOUT", r.stdout + r.stderr, f"{what} found the checkout:\n{output(r)}")

    def bench_cases(self, evo):
        bench = os.path.join(evo, "bench")
        return sorted(os.path.join(d, n) for d, _, names in os.walk(bench) for n in names if n.endswith(".json")
                      and n != "signals.json")


class WithoutRegista(Standalone):
    """Nothing configured, no checkout around the copy: suite and other commands work, Regista's adapter stops."""

    def test_suite_check_works_on_a_made_up_suite(self):  # covers: SEP-8
        folder = os.path.join(self.tmp, "suite")
        write(os.path.join(folder, sf.SUITE_FILE), json.dumps(sf.base_suite(), indent=2) + "\n")
        evo = os.path.join(self.tmp, "evolve-suite-never-made")
        r = self.evolve(evo, "suite", "check", os.path.join(folder, sf.SUITE_FILE))
        self.assertEqual(r.returncode, 0, f"suite check from the copy:\n{output(r)}")
        self.assert_no_refusal(r, "suite check")
        self.assertIn("alpha", r.stdout + r.stderr, "the report names the suite's cases")
        self.assertFalse(os.path.exists(evo), "suite check never creates the evolver's working folder")

    def test_genome_seed_with_a_made_up_profile_and_source_works(self):  # covers: SEP-8
        source = os.path.join(self.tmp, "haiku-skill")
        for rel, text in {"SKILL.md": seedtests.SKILL, **seedtests.SKILL_FROZEN}.items():
            write(os.path.join(source, rel), text)
        profile = os.path.join(self.tmp, "profiles", "haiku-skill.json")
        write(profile, json.dumps(seedtests.SKILL_PROFILE, indent=2) + "\n")
        evo = self.new_home()
        r = self.evolve(evo, "genome", "seed", "--profile", profile, "--source", source)
        self.assertEqual(r.returncode, 0, f"genome seed with --profile and --source from the copy:\n{output(r)}")
        self.assert_no_refusal(r, "genome seed --source")
        with open(os.path.join(evo, "genomes", "seed", "genome.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("profile"), seedtests.SKILL_PROFILE["name"])
        self.assertTrue(os.path.isfile(os.path.join(evo, "genomes", "seed", "plugin", "SKILL.md")),
                        "the seed is expanded from the made-up folder")

    def test_bench_build_stops_with_one_line_naming_the_variable_and_the_key(self):  # covers: SEP-8
        evo = self.new_home()
        project = self.project("standalone-none")
        r = self.evolve(evo, "bench", "build", project, cwd=os.path.dirname(project))
        self.assert_points_at_a_checkout(r, "bench build")
        self.assertEqual(self.bench_cases(evo), [], "bench build wrote no cases")

    def test_regista_generation_replay_and_seed_without_source_stop_with_the_same_line(self):  # covers: SEP-8
        evo = self.new_home()
        checkout, _ = seedtests.build_checkout(os.path.join(self.tmp, "fake-regista"))
        regista_profile = os.path.join(self.copy, "profiles", "regista.json")
        seeded = self.evolve(evo, "genome", "seed", "--profile", regista_profile, "--source", checkout)
        self.assertEqual(seeded.returncode, 0, f"seeding Regista's profile from a given --source works:\n{output(seeded)}")
        for what, args in (
                ("a Regista generation", ["generation", "--target", "agents/builder.md", "--budget", "100000",
                                          "--yes"]),
                ("replay", ["replay", "sample-m1-feature", "--genome", "seed"]),
                ("seeding Regista's profile without --source", ["genome", "seed", "--id", "again"])):
            with self.subTest(what):
                before = sorted(os.listdir(evo))
                r = self.evolve(evo, *args)
                self.assert_points_at_a_checkout(r, what)
                self.assertFalse(os.path.exists(os.path.join(evo, "genomes", "again")), f"{what} seeded nothing")
                self.assertFalse(os.path.isdir(os.path.join(evo, "runs")) and os.listdir(os.path.join(evo, "runs")),
                                 f"{what} started no run")
                self.assertEqual(sorted(os.listdir(evo)), before, f"{what} wrote nothing in the evolver's home")


@requires_regista
class WithRegista(Standalone):
    """GENOME_REGISTA_CHECKOUT or evolve.json regista_checkout names a checkout: the copy uses it."""

    def test_variable_naming_this_repository_gets_bench_build_past_the_refusal(self):  # covers: SEP-8
        evo = self.new_home()
        project = self.project("standalone-repo")
        r = self.evolve(evo, "bench", "build", project, checkout=REGISTA_CHECKOUT, cwd=os.path.dirname(project))
        self.assertEqual(r.returncode, 0, f"bench build with GENOME_REGISTA_CHECKOUT={REGISTA_CHECKOUT}:\n{output(r)}")
        self.assert_no_refusal(r, "bench build")
        self.assertTrue(self.bench_cases(evo), "bench build built the project's cases")

    def test_variable_names_the_checkout_whose_command_line_bench_build_runs(self):  # covers: SEP-8
        log = os.path.join(self.tmp, "variable-calls.jsonl")
        checkout = logging_checkout(os.path.join(self.tmp, "checkout-from-variable"), log)
        evo = self.new_home()
        project = self.project("standalone-variable")
        r = self.evolve(evo, "bench", "build", project, checkout=checkout, cwd=os.path.dirname(project))
        self.assertEqual(r.returncode, 0, f"bench build with the variable set:\n{output(r)}")
        self.assertTrue(read_refusals(log), f"bench build ran `regista.py refusals --json` from the variable's "
                                            f"checkout: {calls(log)}")

    def test_evolve_json_regista_checkout_wins_over_the_variable(self):  # covers: SEP-8
        from_variable = os.path.join(self.tmp, "variable-loses.jsonl")
        from_config = os.path.join(self.tmp, "config-wins.jsonl")
        variable = logging_checkout(os.path.join(self.tmp, "checkout-variable-loses"), from_variable)
        configured = logging_checkout(os.path.join(self.tmp, "checkout-config-wins"), from_config)
        evo = self.new_home({"regista_checkout": configured})
        project = self.project("standalone-both")
        r = self.evolve(evo, "bench", "build", project, checkout=variable, cwd=os.path.dirname(project))
        self.assertEqual(r.returncode, 0, f"bench build with evolve.json and the variable set:\n{output(r)}")
        self.assertTrue(read_refusals(from_config), f"evolve.json's checkout was used: {calls(from_config)}")
        self.assertEqual(calls(from_variable), [], "the variable's checkout was not used")


if __name__ == "__main__":
    unittest.main()
