"""End-to-end: the genome folder copied outside the repository runs its own tests (spec
.regista/specs/genome-standalone.json, SEP-6).

The test copies genome/ (without __pycache__) into a temporary folder outside the repository, so the folder holding
the copy has no scripts/regista.py, and runs the copy's tests from the copy, the way its README says, each as a
separate process with a temporary HOME and CLAUDE_PROJECT_DIR, REGISTA_*, DAEDALUS_* and GENOME_REGISTA_CHECKOUT unset:

    python3 -m unittest discover -s tests -t tests -p <module>.py -v
    python3 -m unittest discover -s tests/e2e -p <module>.py -v

To keep the gate's time bounded, discovery is limited by pattern to a representative subset, one run per module:
- unit, needing Regista for some or all of their tests: test_genome_layout, test_genome_seed, test_evolve_skeleton,
  test_frozen_additions (its module setup builds a bench only with a checkout); needing none: test_suite_files;
- end-to-end, needing Regista: test_genome_seed, test_parents, test_reflect_mutate; needing none: test_suite_check,
  test_suite_checks_cli.

Without a checkout every run exits 0, some tests are skipped, and the output gives the reason 'no Regista checkout:
set GENOME_REGISTA_CHECKOUT'. With GENOME_REGISTA_CHECKOUT naming this repository, the same runs exit 0 with fewer
skips. That second part needs a Regista checkout itself, so it is skipped when this test runs without one.

The runs inside the copy get GENOME_STANDALONE_E2E=1; this test skips itself when it sees it, so it never runs
itself again from the copy (its module isn't in the subset either).

Run: python3 -m unittest discover -s genome/tests/e2e -p test_genome_standalone.py -v"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the genome's own harness, next to this file
from harness import GENOME, PY, REGISTA_CHECKOUT, SKIP_REASON, requires_regista  # noqa: E402

GUARD = "GENOME_STANDALONE_E2E"
UNIT = ("test_genome_layout", "test_genome_seed", "test_evolve_skeleton", "test_frozen_additions", "test_suite_files")
UNIT_NEED_REGISTA = ("test_genome_layout", "test_genome_seed", "test_evolve_skeleton", "test_frozen_additions")
E2E = ("test_genome_seed", "test_parents", "test_reflect_mutate", "test_suite_check", "test_suite_checks_cli")
E2E_NEED_REGISTA = ("test_genome_seed", "test_parents", "test_reflect_mutate")
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def counts(text):
    """(tests run, tests skipped, the summary line) from unittest's output."""
    ran = re.findall(r"^Ran (\d+) tests? in", text, re.M)
    summary = [line for line in text.splitlines() if re.match(r"^(OK|FAILED)\b", line)]
    skipped = re.search(r"skipped=(\d+)", summary[-1]) if summary else None
    return (int(ran[-1]) if ran else 0), (int(skipped.group(1)) if skipped else 0), (summary[-1] if summary else "")


class GenomeFolderOnItsOwn(unittest.TestCase):
    """A copy of genome/ outside the repository runs its unit and end-to-end tests."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get(GUARD):
            raise unittest.SkipTest("already running inside a copy of the genome folder")
        cls.tmp = tempfile.mkdtemp(prefix="genome-standalone-e2e-")
        cls.copy = os.path.join(cls.tmp, "elsewhere", "genome")
        shutil.copytree(GENOME, cls.copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.runs = {}

    @classmethod
    def tearDownClass(cls):
        for dirpath, dirnames, filenames in os.walk(cls.tmp):
            for name in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self, checkout=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PROJECT_DIR", "GENOME_REGISTA_CHECKOUT", "PYTHONPATH", "REGISTA_EVOLVE_HOME")
               and not k.startswith(("REGISTA_", "DAEDALUS_"))}
        env.update({"HOME": self.home, "PYTHONDONTWRITEBYTECODE": "1", GUARD: "1", **GIT_ENV})
        if checkout is not None:
            env["GENOME_REGISTA_CHECKOUT"] = checkout
        return env

    def discover(self, kind, module, checkout=None):
        """One pattern-limited discovery run from the copy (each run once per class: later tests reuse it)."""
        key = (kind, module, checkout)
        if key not in self.runs:
            self.runs[key] = self._discover(kind, module, checkout)
        return self.runs[key]

    def _discover(self, kind, module, checkout):
        start = ["-s", "tests", "-t", "tests"] if kind == "unit" else ["-s", os.path.join("tests", "e2e")]
        r = subprocess.run([PY, "-m", "unittest", "discover", *start, "-p", f"{module}.py", "-v"], cwd=self.copy,
                           capture_output=True, text=True, encoding="utf-8", env=self.env(checkout), timeout=900)
        return r, r.stdout + r.stderr

    def run_subset(self, kind, modules, checkout=None):
        """{module: (tests run, skipped, output)}, each module's run having exited 0."""
        results = {}
        for module in modules:
            r, text = self.discover(kind, module, checkout)
            ran, skipped, summary = counts(text)
            with self.subTest(kind=kind, module=module, checkout=checkout):
                self.assertEqual(r.returncode, 0, f"{kind} {module} from the copy "
                                                  f"(GENOME_REGISTA_CHECKOUT={checkout}):\n{text[-6000:]}")
                self.assertTrue(summary.startswith("OK"), f"{kind} {module}: {summary!r}")
                self.assertGreater(ran, 0, f"{kind} {module} found its tests in the copy:\n{text[-2000:]}")
            results[module] = (ran, skipped, text)
        return results

    def assert_skips_say_why(self, kind, results, need_regista):
        for module in need_regista:
            ran, skipped, text = results[module]
            with self.subTest(kind=kind, module=module):
                self.assertGreater(skipped, 0, f"{kind} {module}: the tests needing Regista are skipped:\n{text[-3000:]}")
                self.assertIn(SKIP_REASON, text, f"{kind} {module}: each skip gives the reason")
        total_ran = sum(r[0] for r in results.values())
        total_skipped = sum(r[1] for r in results.values())
        self.assertGreater(total_ran, total_skipped, f"{kind}: the tests that don't need Regista still run")

    def test_unit_tests_pass_in_a_copy_with_regista_tests_skipped_and_saying_why(self):  # covers: SEP-6
        results = self.run_subset("unit", UNIT)
        self.assert_skips_say_why("unit", results, UNIT_NEED_REGISTA)
        ran, skipped, _ = results["test_suite_files"]
        self.assertEqual(skipped, 0, "a module that doesn't need Regista runs every test")

    def test_end_to_end_tests_pass_in_a_copy_with_regista_tests_skipped_and_saying_why(self):  # covers: SEP-6
        results = self.run_subset("e2e", E2E)
        self.assert_skips_say_why("e2e", results, E2E_NEED_REGISTA)
        for module in ("test_suite_check", "test_suite_checks_cli"):
            self.assertEqual(results[module][1], 0, f"e2e {module} doesn't need Regista and runs every test")

    @requires_regista
    def test_with_the_variable_naming_this_repository_the_regista_tests_run_and_pass(self):  # covers: SEP-6
        for kind, modules, need in (("unit", UNIT, UNIT_NEED_REGISTA), ("e2e", E2E, E2E_NEED_REGISTA)):
            alone = self.run_subset(kind, need)
            pointed = self.run_subset(kind, need, checkout=REGISTA_CHECKOUT)
            skipped_alone = sum(r[1] for r in alone.values())
            skipped_pointed = sum(r[1] for r in pointed.values())
            self.assertLess(skipped_pointed, skipped_alone,
                            f"{kind}: fewer skips with GENOME_REGISTA_CHECKOUT={REGISTA_CHECKOUT}")
            for module in need:
                with self.subTest(kind=kind, module=module):
                    self.assertGreater(pointed[module][0] - pointed[module][1], alone[module][0] - alone[module][1],
                                       f"{kind} {module}: more tests ran with the checkout named")
                    self.assertNotIn(SKIP_REASON, pointed[module][2], f"{kind} {module}: nothing skipped for want "
                                                                      "of a checkout")


if __name__ == "__main__":
    unittest.main()
