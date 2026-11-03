"""End to end: `evolve.py bench build` on a sample Regista project made through the real command line writes a
case file per step, signals.json and a plain-language audit report, and `bench list` lists the cases.

The sample project is set up with the real `regista.py init --auto`, and every commit goes through the real
PreToolUse hook (which runs the commit gate, so the gate history is real too). It has three steps:
- m1-add (ADD-1): a spec commit, then a commit adding a unit test marked `covers: ADD-1`;
- m1-sub (SUB-1): a spec commit, then a commit adding sub() and its unit test, then a review whose base is the
  spec commit, with one major finding on calc.py;
- m1-live (LIVE-1): a spec commit, then a commit adding a test only under tests/live/, a folder no commit gate
  runs, so this step is not replayable.

What the test assumes about where things go (as in the bench-cases and signal-check specs): cases under
$REGISTA_EVOLVE_HOME/bench/<project>/<case>.json, signals.json next to them, and the report at
$REGISTA_EVOLVE_HOME/reports/audit-<project>.md, <project> being the one folder bench build creates under bench/.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the genome's own copy of the harness, next to this file
from harness import GENOME, PY, SampleProject, decision, reason, requires_regista  # noqa: E402

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")

PLAN = """# Plan

## M1 — Calculator
- [ ] Add two numbers (ADD-1)
- [ ] Subtract two numbers (SUB-1)
- [ ] The live site shows the calculator (LIVE-1)
"""

UNIT_TEST = (
    "import os\n"
    "import sys\n"
    "import unittest\n\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))\n"
    "import calc  # noqa: E402\n\n\n"
    "class {cls}(unittest.TestCase):\n"
    "    def test_{name}(self):{marker}\n"
    "        self.assertEqual(calc.{fn}(5, 3), {want})\n"
)

LIVE_TEST = (
    "import os\n"
    "import unittest\n\n\n"
    "class LiveSite(unittest.TestCase):\n"
    "    @unittest.skipUnless(os.environ.get('LIVE_URL'), 'needs the deployed site')\n"
    "    def test_live_site_shows_the_calculator(self):  # covers: LIVE-1\n"
    "        self.assertTrue(os.environ['LIVE_URL'])\n"
)

STEPS = ("m1-add", "m1-sub", "m1-live")
AXES = ("gates", "requirements", "review_rounds", "finding_severity", "refusals", "correct_blocked", "tokens",
        "compactions")
NUMBER_WORDS = {1: "one", 2: "two", 3: "three"}


def unit_test(cls, name, fn, want, rid=None):
    return UNIT_TEST.format(cls=cls, name=name, fn=fn, want=want, marker=f"  # covers: {rid}" if rid else "")


def spec(step, title, interface, rid, text, state="planned"):
    return json.dumps({"step": step, "milestone": "M1", "title": title, "plan_step": f"{title} ({rid})",
                       "state": state, "interface": interface,
                       "requirements": [{"id": rid, "text": text, "verify": "test"}]}, indent=1) + "\n"


def says_number(text, n):
    return re.search(rf"\b({n}|{NUMBER_WORDS[n]})\b", text, re.I) is not None


def step_line(step, line):
    return re.search(rf"(?<![\w-]){re.escape(step)}(?![\w-])", line) is not None


@requires_regista
class AuditReportEndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)
        self.evo = os.path.join(self.p.base, "evolve")
        self.commits = {}
        self.make_project()

    # ---- the sample project, through the real CLI and hook ----

    def commit(self, message):
        """Stage everything, ask the real PreToolUse hook, then commit; returns the full hash."""
        p = self.p
        p.git("add", "-A")
        answer = p.hook("pre-tool", p.bash(f'git commit -m "{message}"'))
        self.assertNotEqual(decision(answer), "deny",
                            f"setup: the hook refused '{message}': {reason(answer)}\n{p.last_hook.stderr}")
        p.git("commit", "-q", "-m", message)
        return p.git("rev-parse", "HEAD").stdout.strip()

    def spec_pair(self, step, title, interface, rid, text):
        """The spec as planned (committed first), and the same spec once the step is active."""
        return (spec(step, title, interface, rid, text), spec(step, title, interface, rid, text, state="active"))

    def make_project(self):
        p = self.p
        p.set_up_regista(name="Sample")
        cfg = p.config()
        cfg["gates"] = [
            {"name": "tests", "run": f"{PY} -m unittest discover -s tests/unit -t .", "tests": ["tests/unit/**"],
             "when": ["commit"], "timeout": 300},
            {"name": "live", "run": f"{PY} -m unittest discover -s tests/live -t .", "tests": ["tests/live/**"],
             "when": ["manual"], "timeout": 300},
        ]
        cfg["review"] = {**cfg.get("review", {}), "required_for": []}
        cfg["requirements"] = {**cfg.get("requirements", {}), "enforce_coverage": True, "milestone_needs_e2e": False}
        with open(p.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        p.write(".regista/plan.md", PLAN)
        p.git("rm", "-q", "tests/test_calc.py")
        for pkg in ("tests", "tests/unit", "tests/live"):
            p.write(f"{pkg}/__init__.py", "")
        p.write("tests/unit/test_calc.py", unit_test("Calc", "adds", "add", 8))
        self.commits["setup"] = self.commit("plan and test layout")

        # m1-add: the spec, then the test that covers it
        planned, active = self.spec_pair("m1-add", "Add two numbers", "add(a, b) in calc.py.", "ADD-1",
                                         "add(a, b) SHALL return a + b")
        p.write(".regista/specs/m1-add.json", planned)
        self.commits["m1-add parent"] = self.commit("m1-add: spec")
        p.write(".regista/specs/m1-add.json", active)
        p.write("tests/unit/test_add.py", unit_test("Add", "adds", "add", 8, rid="ADD-1"))
        self.commits["m1-add finish"] = self.commit("m1-add: test for add")

        # m1-sub: the spec, then sub() with its test, then the review of that change (base: the spec commit)
        planned, active = self.spec_pair("m1-sub", "Subtract two numbers", "sub(a, b) in calc.py.", "SUB-1",
                                         "sub(a, b) SHALL return a - b")
        p.write(".regista/specs/m1-sub.json", planned)
        self.commits["m1-sub parent"] = self.commit("m1-sub: spec")
        p.write(".regista/specs/m1-sub.json", active)
        with open(p.path("calc.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef sub(a, b):\n    return a - b\n")
        p.write("tests/unit/test_sub.py", unit_test("Sub", "subtracts", "sub", 2, rid="SUB-1"))
        self.commits["m1-sub finish"] = self.commit("m1-sub: sub and its test")
        p.write(".regista/reviews/r-sub.json", json.dumps({
            "verdict": "approve", "summary": "sub() works; one major issue with argument checks.",
            "base": self.commits["m1-sub parent"][:7], "checked": ["SUB-1"],
            "findings": [{"severity": "major", "file": "calc.py", "line": 5,
                          "issue": "sub() accepts strings without complaint"}]}, indent=1) + "\n")
        self.commits["review"] = self.commit("m1-sub: review")

        # m1-live: covered only by a test under tests/live/, which no commit gate runs
        planned, active = self.spec_pair("m1-live", "The live site shows the calculator",
                                         "The deployed page shows add and sub.", "LIVE-1",
                                         "the live site SHALL show the calculator")
        p.write(".regista/specs/m1-live.json", planned)
        self.commits["m1-live parent"] = self.commit("m1-live: spec")
        p.write(".regista/specs/m1-live.json", active)
        p.write("tests/live/test_live_site.py", LIVE_TEST)
        self.commits["m1-live finish"] = self.commit("m1-live: live check")
        left = [ln for ln in p.git("status", "--porcelain").stdout.splitlines() if ".regista/gates/" not in ln]
        self.assertEqual(left, [], "setup left uncommitted files (the gates' own records aside)")

    # ---- evolve.py ----

    def evolve(self, *args):
        env = dict(self.p.env, REGISTA_EVOLVE_HOME=self.evo, TZ="UTC")
        return subprocess.run([PY, EVOLVE, *args], cwd=self.p.base, capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=600)

    def test_bench_build_writes_cases_signals_and_audit_report_and_list_shows_them(self):  # covers: AUD-1
        r = self.evolve("bench", "build", self.p.root)
        self.assertEqual(r.returncode, 0, f"bench build failed:\n{r.stdout}\n{r.stderr}")

        # one case file per step, in one project folder under bench/
        bench = os.path.join(self.evo, "bench")
        self.assertTrue(os.path.isdir(bench), f"no bench folder in {self.evo}")
        projects = [d for d in os.listdir(bench) if os.path.isdir(os.path.join(bench, d))]
        self.assertEqual(len(projects), 1, f"expected one project folder under bench/: {projects}")
        project = projects[0]
        folder = os.path.join(bench, project)
        cases = {}
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json") and name != "signals.json":
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    case = json.load(f)
                cases[case.get("step")] = case
        self.assertEqual(sorted(cases), sorted(STEPS), f"one case per step: {sorted(cases)}")
        for step in STEPS:
            case = cases[step]
            self.assertEqual(case.get("finishing_commit"), self.commits[f"{step} finish"], f"{step}: {case}")
            self.assertEqual(case.get("parent"), self.commits[f"{step} parent"], f"{step}: {case}")
            self.assertIn(case.get("replayable"), (True, False), f"{step}: {case}")
            self.assertTrue(case.get("reasons") and all(isinstance(x, str) and len(x.split()) >= 3
                                                        for x in case["reasons"]),
                            f"{step}: needs plain-language reasons: {case.get('reasons')}")
        self.assertTrue(cases["m1-add"]["replayable"], cases["m1-add"]["reasons"])
        self.assertTrue(cases["m1-sub"]["replayable"], cases["m1-sub"]["reasons"])
        self.assertFalse(cases["m1-live"]["replayable"],
                         f"LIVE-1 is covered only under tests/live/: {cases['m1-live']['reasons']}")

        # signals.json: every axis with trusted and reasons, and a target with its counts
        with open(os.path.join(folder, "signals.json"), encoding="utf-8") as f:
            signals = json.load(f)
        axes = signals["axes"] if isinstance(signals.get("axes"), dict) else signals
        for axis in AXES:
            self.assertIn(axis, axes, f"signals.json has no {axis} axis: {sorted(axes)}")
            self.assertIn(axes[axis].get("trusted"), (True, False), f"{axis}: {axes[axis]}")
            self.assertIsInstance(axes[axis].get("reasons"), list, f"{axis}: {axes[axis]}")
        target = signals.get("target")
        self.assertIsInstance(target, dict, f"signals.json needs a target: {signals}")
        self.assertTrue(target.get("role"), target)
        self.assertTrue(target.get("counts"), f"the target comes with its counts: {target}")

        # the audit report, written for a person
        path = os.path.join(self.evo, "reports", f"audit-{project}.md")
        self.assertTrue(os.path.isfile(path), f"no audit report at {path}")
        with open(path, encoding="utf-8") as f:
            report = f.read()
        low = report.lower()
        prose = [b.strip() for b in re.split(r"\n\s*\n", report)
                 if b.strip() and not b.strip().startswith(("#", "|", "```"))]
        self.assertTrue(prose, f"the report has no text:\n{report}")
        first = prose[0]
        self.assertIn("replay", first.lower(), f"the report leads with how many steps can be replayed:\n{report}")
        self.assertTrue(says_number(first, 2), f"the first paragraph names the replayable count (2):\n{report}")
        self.assertTrue(says_number(report, 3), f"the report says how many steps there are (3):\n{report}")
        live_reason = cases["m1-live"]["reasons"][0].lower().rstrip(".")
        self.assertTrue(live_reason in low or "live" in low,
                        f"the report gives the reason m1-live isn't replayable:\n{report}")
        for axis in (a for a in AXES if axes[a]["trusted"]):
            self.assertTrue(axis in low or axis.replace("_", " ") in low,
                            f"the report names the trusted signal {axis}:\n{report}")
        untrusted = [a for a in AXES if not axes[a]["trusted"]]
        if untrusted:
            self.assertTrue(any(a in low or a.replace("_", " ") in low for a in untrusted),
                            f"the report names the signals it doesn't trust ({untrusted}):\n{report}")
        role = str(target["role"]).lower()
        self.assertTrue(any(form in low for form in (role, role.replace("-", " "), role.replace("_", " "))),
                        f"the report names the target ({target['role']}):\n{report}")
        self.assertRegex(low, r"\btarget", f"the report says what the target is:\n{report}")
        sentences = re.findall(r"[A-Z][^.!?\n]{15,}[.!?]", report)
        self.assertGreaterEqual(len(sentences), 5, f"the report is written in full sentences:\n{report}")
        self.assertNotIn("→", report, "no arrows standing in for words")

        # bench list shows every case on its own line, then totals
        r = self.evolve("bench", "list")
        self.assertEqual(r.returncode, 0, f"bench list failed:\n{r.stdout}\n{r.stderr}")
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        for step in STEPS:
            hits = [ln for ln in lines if step_line(step, ln)]
            self.assertEqual(len(hits), 1, f"bench list shows {step} on one line:\n{r.stdout}")
            self.assertIn(project, hits[0], f"the line for {step} names the project:\n{r.stdout}")
        rest = [ln for ln in lines if not any(step_line(s, ln) for s in STEPS)]
        self.assertTrue(rest and says_number("\n".join(rest), 3), f"bench list ends with totals:\n{r.stdout}")


if __name__ == "__main__":
    unittest.main()
