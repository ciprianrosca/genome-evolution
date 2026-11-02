"""Signal quality check and rework by role: after `evolve.py bench build` builds a project's cases, it checks each
scoring axis and writes bench/<project>/signals.json, saying for every axis whether it can be trusted and why, and
names the role with the most rework (major and blocker review findings by role) as the target, with its counts.

The fixture projects are small Regista projects made with plain git in temporary folders. Each has ten replayable
steps; step i's window (from its parent commit to its finishing commit) runs from (09+i):00 to (09+i):40 UTC on
20 October 2026, and the records (gate history, hooks.log, compactions, manual checks, reviews) are placed inside
or outside those windows on purpose.

The layout of signals.json is the one the spec pins: "axes" {<axis>: {trusted, reasons, coverage}}, "human_signal"
{trusted, reasons, looked, not_looked, failed}, and "target" {role, counts: {<role>: {major_findings,
blocked_reports, gate_failures}}}, roles being builder, test-writer, researcher and orchestrator.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_signal_check -v"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the genome folder
REGISTA_CHECKOUT = os.path.dirname(ROOT)  # the Regista checkout that holds the genome: reached only by path
EVOLVE = os.path.join(ROOT, "scripts", "evolve.py")
REGISTA = os.path.join(REGISTA_CHECKOUT, "scripts", "regista.py")

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}
AXES = ("gates", "requirements", "review_rounds", "finding_severity", "refusals", "correct_blocked", "tokens",
        "compactions")
N_CASES = 10
ROLES = ("builder", "test-writer", "researcher", "orchestrator")
GITIGNORE = ".regista/hooks.log\n.regista/gates/\n.regista/compactions.jsonl\n.regista/state/\n__pycache__/\n"


def config(name):
    return {
        "project": name,
        "plan": ".regista/plan.md",
        "board_file": ".regista/TODO.md",
        "gates": [{"name": "tests", "run": "python3 -m unittest discover -s tests/unit", "tests": ["tests/unit/**"],
                   "when": ["commit"], "timeout": 600}],
        "roles": {"source": ["src/**"], "tests": ["tests/**"]},
        "requirements": {"enforce_coverage": True},
    }


def at(hour, minute=0):
    """A time on 20 October 2026, `hour` hours after 08:00 UTC."""
    total = 8 * 60 + hour * 60 + minute
    return f"2026-10-20T{total // 60:02d}:{total % 60:02d}:00"


def window(i, minute):
    """A time inside step i's window when 0 < minute < 40."""
    return at(1 + i, minute)


def spec(i):
    return json.dumps({"step": f"s{i:02d}", "milestone": "M1", "title": f"Step {i}", "plan_step": f"Step {i} (S{i}-1)",
                       "state": "active", "interface": f"f{i}() in src/mod_{i}.py.",
                       "requirements": [{"id": f"S{i}-1", "text": f"f{i}() SHALL return {i}", "verify": "test"},
                                        {"id": f"S{i}-2", "text": f"the page SHALL show {i}", "verify": "manual"}]},
                      indent=1) + "\n"


def test_file(i):
    return (f'"""Tests for step {i}."""\nimport unittest\n\n\nclass Step{i}(unittest.TestCase):\n'
            f"    def test_value(self):  # covers: S{i}-1\n        self.assertEqual({i}, {i})\n")


class Repo:
    """A git repository in a temporary folder, with commits at chosen times."""

    def __init__(self, parent, name):
        self.root = os.path.join(parent, name)
        os.makedirs(self.root)
        self.git("init", "-q", "-b", "main")

    def git(self, *args, date=None):
        env = dict(os.environ, **GIT_ENV)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date + "+0000"
        r = subprocess.run(["git", "-c", "user.name=Sample", "-c", "user.email=sample@example.com",
                            "-c", "commit.gpgsign=false", *args], cwd=self.root, capture_output=True, text=True,
                           env=env)
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, when, message, files):
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=when)
        return self.git("rev-parse", "HEAD")


def refusal(when, inp, label=None, tool="Bash", reason="Regista: refused for the test"):
    """A hooks.log refusal line; with a label it has the new fields, without one it is an old plain line."""
    row = {"at": when, "tool": tool, "input": inp, "reason": reason}
    if label:
        row.update({"kind": "deny", "rule": "role_guard" if label == "unsure" else "gate_failed",
                    "role": "builder", "session": "s1", "worker": None, "label": label, "label_by": "rule"})
    return row


def manual(how=None, passes=True, evidence="checked the page on the phone"):
    row = {"verify": "manual", "passes": passes, "evidence": evidence, "at": at(13), "by": "user"}
    if how:
        row["how"] = how
    return row


def finding(severity, rel, issue="needs another look"):
    return {"severity": severity, "file": rel, "line": 3, "issue": issue}


def build_project(parent, name, gate_gaps=(), refusals=(), manuals=None, reviews=(), gate_fails=()):
    """Ten replayable steps s00..s09. gate_gaps: step indexes with no gate run in their window (all runs pass).
    refusals: hooks.log rows. manuals: requirements.json records. reviews: (step index, verdict, findings).
    gate_fails: step indexes whose run at minute 10 fails (a passing run follows at minute 35)."""
    r = Repo(parent, name)
    plan = "# Plan\n\n## M1 — Steps\n" + "".join(f"- [x] Step {i} (S{i}-1)\n" for i in range(N_CASES))
    r.commit(at(0), "setup", {".gitignore": GITIGNORE, ".regista/config.json": json.dumps(config(name)) + "\n",
                              ".regista/plan.md": plan, "src/app.py": "VALUE = 1\n"})
    parents = []
    for i in range(N_CASES):
        parents.append(r.commit(window(i, 0), f"s{i:02d}: spec and source", {
            f".regista/specs/s{i:02d}.json": spec(i), f"src/mod_{i}.py": f"def f{i}():\n    return {i}\n"}))
        r.commit(window(i, 40), f"s{i:02d}: tests", {f"tests/unit/test_s{i:02d}.py": test_file(i)})
    files = {}
    for n, (i, verdict, found) in enumerate(reviews):
        files[f".regista/reviews/r{n:02d}.json"] = json.dumps(
            {"verdict": verdict, "summary": "review of the step", "base": parents[i][:7], "checked": [f"S{i}-1"],
             "at": window(i, 30), "findings": list(found)}) + "\n"
    if files:
        r.commit(at(15), "reviews", files)
    runs = [{"date": window(i, 10), "commit": parents[i][:7],
             "results": [{"name": "tests", "exit": 0, "seconds": 1.0, "status": "pass"}]}
            for i in range(N_CASES) if i not in gate_gaps and i not in gate_fails]
    for i in gate_fails:
        runs.append({"date": window(i, 10), "commit": parents[i][:7],
                     "results": [{"name": "tests", "exit": 1, "seconds": 1.0, "status": "fail"}]})
        runs.append({"date": window(i, 35), "commit": parents[i][:7],
                     "results": [{"name": "tests", "exit": 0, "seconds": 1.0, "status": "pass"}]})
    runs.sort(key=lambda x: x["date"])
    r.write(".regista/gates/history.jsonl", "".join(json.dumps(x) + "\n" for x in runs))
    r.write(".regista/compactions.jsonl",
            "".join(json.dumps({"at": window(i, 25), "trigger": "auto"}) + "\n" for i in range(N_CASES)))
    if refusals:
        r.write(".regista/hooks.log", "".join(json.dumps(x) + "\n" for x in refusals))
    if manuals is not None:
        r.write(".regista/gates/requirements.json", json.dumps(manuals, indent=1) + "\n")
    return r


def clean_env(home, evolve_home=None, as_user=False):
    """The environment for a command run by the fixture. as_user: a command the user runs in their own terminal,
    so the signs of a Claude Code or Codex shell (CLAUDECODE, CODEX_*) are stripped too: the gates run with
    CLAUDECODE=1, and some harness commands refuse to run inside an agent's shell."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "REGISTA_EVOLVE_HOME", "CODEX_HOME")
           and not (as_user and (k == "CLAUDECODE" or k.startswith("CODEX_")))}
    env.update({"HOME": home, "TZ": "UTC", **GIT_ENV})
    if evolve_home:
        env["REGISTA_EVOLVE_HOME"] = evolve_home
    return env


def run_build(project, home, evolve_home):
    return subprocess.run([sys.executable, EVOLVE, "bench", "build", project], cwd=os.path.dirname(project),
                          capture_output=True, text=True, encoding="utf-8", env=clean_env(home, evolve_home),
                          timeout=300)


def label_false_alarm(project, home, when):
    """The user labels the refusal logged at `when` as a false alarm with the harness's own command."""
    env = clean_env(home, as_user=True)
    r = subprocess.run([sys.executable, REGISTA, "refusals", "--json"], cwd=project, capture_output=True, text=True,
                       encoding="utf-8", env=env)
    if r.returncode != 0:
        raise AssertionError(f"refusals --json failed: {r.stdout}{r.stderr}")
    number = [x["number"] for x in json.loads(r.stdout) if x["at"] == when][0]
    r = subprocess.run([sys.executable, REGISTA, "refusals", "label", str(number), "false_alarm", "--why",
                        "it only read a fixture"], cwd=project, capture_output=True, text=True, encoding="utf-8",
                       env=env)
    if r.returncode != 0:
        raise AssertionError(f"refusals label failed: {r.stdout}{r.stderr}")


class SignalsBase(unittest.TestCase):
    """Builds one fixture project, runs `bench build` on it and loads signals.json."""
    name = "sample"

    @classmethod
    def make(cls, parent, home):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-signals-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.evo = os.path.join(cls.tmp, "evolve")
        os.makedirs(cls.home)
        cls.repo = cls.make(os.path.join(cls.tmp, "projects"), cls.home)
        cls.result = run_build(cls.repo.root, cls.home, cls.evo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.result.returncode, 0,
                         f"bench build failed:\n{self.result.stdout}\n{self.result.stderr}")
        path = os.path.join(self.evo, "bench", self.name, "signals.json")
        self.assertTrue(os.path.isfile(path), f"no signals.json at {path}")
        with open(path, encoding="utf-8") as f:
            self.signals = json.load(f)

    def axes(self):
        self.assertIsInstance(self.signals.get("axes"), dict, f"signals.json needs an axes object: {self.signals}")
        return self.signals["axes"]

    def axis(self, name):
        axes = self.axes()
        self.assertIn(name, axes, f"signals.json has no {name} axis: {sorted(axes)}")
        return axes[name]

    def human(self):
        human = self.signals.get("human_signal")
        self.assertIsInstance(human, dict, f"signals.json needs a human_signal object: {sorted(self.signals)}")
        self.assert_trust(human_signal=human)
        for key in ("looked", "not_looked", "failed"):
            self.assertIsInstance(human.get(key), int, f"human_signal.{key} must be a whole number: {human}")
        return human

    def assert_trust(self, **entries):
        for name, entry in entries.items():
            self.assertIn(entry.get("trusted"), (True, False), f"{name}: trusted must be true or false: {entry}")
            self.assertIsInstance(entry.get("reasons"), list, f"{name}: reasons must be a list: {entry}")
            self.assertTrue(all(isinstance(x, str) and len(x.split()) >= 3 for x in entry["reasons"]),
                            f"{name}: reasons are plain sentences: {entry['reasons']}")
            if entry["trusted"] is False:
                self.assertTrue(entry["reasons"], f"{name} isn't trusted, so it needs a reason")

    def assert_entry(self, name, entry):
        self.assert_trust(**{name: entry})
        cov = entry.get("coverage")
        self.assertIsInstance(cov, (int, float), f"{name}: coverage is a share of the replayable cases: {entry}")
        self.assertTrue(0 <= cov <= 1, f"{name}: coverage {cov} isn't between 0 and 1")

    def target(self):
        t = self.signals.get("target")
        self.assertIsInstance(t, dict, f"signals.json needs a target object: {self.signals.get('target')}")
        self.assertIsInstance(t.get("role"), str, t)
        self.assertIsInstance(t.get("counts"), dict, f"the target is written with its counts: {t}")
        self.assertNotIn("reviewer", t["counts"], f"the reviewer never appears in the counts: {t}")
        for role, c in t["counts"].items():
            self.assertIn(role, ROLES, f"unknown role {role}: {t}")
            for key in ("major_findings", "blocked_reports", "gate_failures"):
                self.assertIsInstance(c.get(key), int, f"counts.{role}.{key} must be a whole number: {t}")
        self.assertIn(t["role"], t["counts"], f"the target role's own counts are written: {t}")
        return t

    def findings_of(self, role):
        """The major and blocker findings counted for `role` (0 when the role isn't listed)."""
        return self.target()["counts"].get(role, {}).get("major_findings", 0)


def ten_refusals(labels):
    """One refusal in each step's window, with the given labels (None for an old plain line)."""
    return [refusal(window(i, 20), {"command": f"cat src/mod_{i}.py"}, label) for i, label in enumerate(labels)]


class TrustedProject(SignalsBase):
    """Every replayable case has gate runs; exactly half of the windows' refusals are unsure, the rest real (even
    though the real ones target /tmp: labels win over the old /tmp guess); more unsure refusals sit outside every
    window; every manual check was looked at one by one and one of them failed; test files drew the most major and
    blocker findings, while source files drew only one major and many minor ones."""
    name = "trusted"

    @classmethod
    def make(cls, parent, home):
        rows = [refusal(window(i, 20), {"command": f"cat src/mod_{i}.py"}, "unsure") for i in range(5)]
        rows += [refusal(window(i, 20), {"command": f"cat /tmp/out_{i}.txt"}, "real") for i in range(5, N_CASES)]
        rows += [refusal(at(14, m), {"command": "ls"}, "unsure") for m in (5, 10, 15, 20, 25, 30)]
        manuals = {f"S{i}-2": manual("looked") for i in range(4)}
        manuals["S4-2"] = manual("looked", passes=False, evidence="the page showed the wrong number")
        reviews = [
            (0, "changes", [finding("major", "tests/unit/test_s00.py"), finding("minor", "src/mod_0.py")]),
            (1, "changes", [finding("blocker", "tests/unit/test_s01.py"), finding("minor", "src/mod_1.py")]),
            (2, "changes", [finding("major", "tests/unit/test_s02.py"), finding("minor", "tests/unit/test_s02.py")]),
            (3, "changes", [finding("major", "src/mod_3.py"), finding("minor", "src/mod_3.py")]),
            (4, "changes", [finding("minor", "src/mod_4.py")]),
        ] + [(i, "approve", []) for i in range(N_CASES)]
        return build_project(parent, cls.name, refusals=rows, manuals=manuals, reviews=reviews)

    def test_every_axis_listed_with_trusted_reasons_and_coverage(self):  # covers: FIT-4
        for name in AXES:
            self.assert_entry(name, self.axis(name))

    def test_gates_with_data_for_every_case_are_trusted(self):  # covers: FIT-4
        gates = self.axis("gates")
        self.assertIs(gates["trusted"], True, gates)
        self.assertAlmostEqual(gates["coverage"], 1.0, places=2)

    def test_refusals_trusted_when_exactly_half_are_unsure(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assertIs(refusals["trusted"], True,
                      f"5 of the 10 refusals in the windows are unsure: half isn't over half; the 6 unsure ones "
                      f"outside every window and the /tmp guess for labelled lines don't count: {refusals}")

    def test_missing_transcripts_make_tokens_untrusted_without_an_error(self):  # covers: FIT-4
        tokens = self.axis("tokens")
        self.assertIs(tokens["trusted"], False, "there are no transcripts under ~/.claude/projects")
        self.assertTrue(tokens["reasons"], tokens)

    def test_manual_checks_looked_at_one_by_one_are_trusted(self):  # covers: FIT-4
        human = self.human()
        self.assertIs(human["trusted"], True, human)
        self.assertEqual((human["not_looked"], human["failed"]), (0, 1), human)
        self.assertGreaterEqual(human["looked"], 4, f"four passes looked at one by one: {human}")

    def test_target_is_the_role_with_most_major_and_blocker_findings(self):  # covers: AUD-2
        t = self.target()
        self.assertEqual(t["role"], "test-writer",
                         f"3 major or blocker findings name test files, 1 names a source file: {t}")
        self.assertEqual(self.findings_of("test-writer"), 3, t)
        self.assertEqual(self.findings_of("builder"), 1,
                         f"minor findings and the reviewer's 'changes' verdicts don't count: {t}")

    def test_reviewer_verdicts_never_count_as_rework(self):  # covers: AUD-2
        t = self.target()  # also checks that the reviewer never appears in the counts
        self.assertNotEqual(t["role"], "reviewer", f"five 'changes' verdicts are the reviewer's own, not rework: {t}")


class UntrustedProject(SignalsBase):
    """Four of ten cases have no gate run in their window; of the windows' refusals three are unsure and three were
    logged real but labelled false alarms by the user afterwards; no manual check was passed one by one (bulk,
    relayed, forced, and an old record without how) and none ever failed."""
    name = "untrusted"

    @classmethod
    def make(cls, parent, home):
        labels = ["unsure"] * 3 + ["real"] * 7
        rows = ten_refusals(labels)
        manuals = {"S0-2": manual("bulk"), "S1-2": manual("relayed"), "S2-2": manual("forced"), "S3-2": manual()}
        repo = build_project(parent, cls.name, gate_gaps=(1, 4, 6, 9), refusals=rows, manuals=manuals,
                             reviews=[(i, "approve", []) for i in range(N_CASES)])
        for i in (3, 4, 5):
            label_false_alarm(repo.root, home, window(i, 20))
        return repo

    def test_every_axis_listed_with_trusted_reasons_and_coverage(self):  # covers: FIT-4
        for name in AXES:
            self.assert_entry(name, self.axis(name))

    def test_axis_missing_data_in_over_30_percent_of_cases_is_untrusted(self):  # covers: FIT-4
        gates = self.axis("gates")
        self.assertIs(gates["trusted"], False, f"4 of 10 replayable cases have no gate run: {gates}")
        self.assertAlmostEqual(gates["coverage"], 0.6, places=2)
        self.assertTrue(gates["reasons"])

    def test_refusals_untrusted_when_over_half_are_false_alarms_or_unsure(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assertIs(refusals["trusted"], False,
                      f"3 unsure and 3 labelled false_alarm by the user: 6 of 10 refusals: {refusals}")
        self.assertTrue(refusals["reasons"])

    def test_manual_checks_not_passed_one_by_one_are_not_human_signal(self):  # covers: FIT-4
        human = self.human()
        self.assertIs(human["trusted"], False, f"bulk, relayed, forced and unknown passes aren't looked at: {human}")
        self.assertEqual((human["looked"], human["not_looked"], human["failed"]), (0, 4, 0), human)

    def test_no_failed_manual_check_is_reported_with_the_fallback(self):  # covers: FIT-4
        human = self.human()
        self.assertEqual(human["failed"], 0, human)
        said = [r.lower() for r in human["reasons"] if "fail" in r.lower() and "release" in r.lower()]
        self.assertTrue(said, f"a reason says no manual check ever failed and names bugs found after release as "
                              f"the fallback: {human['reasons']}")


class OldLogsProject(SignalsBase):
    """Old hooks.log lines without labels: six of the ten refusals in the windows target /tmp or a scratchpad.
    Every manual check says how = looked, but its evidence says it was forced or not individually confirmed."""
    name = "oldlogs"

    @classmethod
    def make(cls, parent, home):
        targets = [({"command": "python3 -m pytest > /tmp/pytest-out.txt"}, "Bash"),
                   ({"file_path": "/tmp/claude-1001/proj/9b41/scratchpad/notes.md", "content": "x"}, "Write"),
                   ({"command": "touch /tmp/ready"}, "Bash"),
                   ({"command": "echo plan > /tmp/claude-1001/proj/9b41/scratchpad/plan.txt"}, "Bash"),
                   ({"file_path": "/tmp/scratch/result.json"}, "Read"),
                   ({"command": "rm -rf /tmp/build-cache"}, "Bash")]
        rows = [refusal(window(i, 20), inp, tool=tool) for i, (inp, tool) in enumerate(targets)]
        rows += [refusal(window(i, 20), {"command": f"echo x > src/mod_{i}.py"}) for i in range(6, N_CASES)]
        manuals = {"S0-2": manual("looked", evidence="forced by the orchestrator to unblock the milestone"),
                   "S1-2": manual("looked", evidence="ok from the user (not individually confirmed)"),
                   "S2-2": manual("looked", evidence="Forced: the phone wasn't available")}
        return build_project(parent, cls.name, refusals=rows, manuals=manuals,
                             reviews=[(i, "approve", []) for i in range(N_CASES)])

    def test_unlabelled_refusals_targeting_tmp_or_scratchpad_are_suspected_false_alarms(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assert_entry("refusals", refusals)
        self.assertIs(refusals["trusted"], False, f"6 of 10 old refusals target /tmp or a scratchpad: {refusals}")
        self.assertTrue(refusals["reasons"])

    def test_forced_or_unconfirmed_evidence_is_not_looked(self):  # covers: FIT-4
        human = self.human()
        self.assertIs(human["trusted"], False,
                      f"how = looked, but the evidence says forced or not individually confirmed: {human}")
        self.assertEqual((human["looked"], human["not_looked"], human["failed"]), (0, 3, 0), human)


class TieProject(SignalsBase):
    """Three of ten cases have no gate run (exactly 30%); old unlabelled refusals, three of ten targeting /tmp; the
    test writer and the builder drew two major or blocker findings each, alongside several 'changes' verdicts. An
    unfinished review (reviews/draft.json) with a blocker and a major finding on test files is committed too: it is
    ignored, or the test writer would have the most rework."""
    name = "tie"

    @classmethod
    def make(cls, parent, home):
        rows = [refusal(window(i, 20), {"command": f"echo x > /tmp/out_{i}.txt"}) for i in range(3)]
        rows += [refusal(window(i, 20), {"command": f"echo x > src/mod_{i}.py"}) for i in range(3, N_CASES)]
        reviews = [
            (0, "changes", [finding("major", "tests/unit/test_s00.py")]),
            (1, "changes", [finding("major", "tests/unit/test_s01.py"), finding("minor", "tests/unit/test_s01.py")]),
            (2, "changes", [finding("blocker", "src/mod_2.py")]),
            (3, "changes", [finding("major", "src/mod_3.py")]),
            (5, "changes", [finding("minor", "tests/unit/test_s05.py")]),
            (6, "changes", [finding("minor", "tests/unit/test_s06.py")]),
        ] + [(i, "approve", []) for i in range(N_CASES)]
        repo = build_project(parent, cls.name, gate_gaps=(2, 5, 8), refusals=rows, reviews=reviews)
        base = repo.git("log", "--format=%H", "--grep=^s07: spec", "-n", "1")
        repo.commit(at(16), "draft review", {".regista/reviews/draft.json": json.dumps(
            {"verdict": "changes", "summary": "unfinished review of step 7", "base": base[:7], "checked": ["S7-1"],
             "at": window(7, 30), "findings": [finding("blocker", "tests/unit/test_s07.py"),
                                               finding("major", "tests/unit/test_s07.py")]}) + "\n"})
        return repo

    def test_axis_missing_data_in_exactly_30_percent_of_cases_is_trusted(self):  # covers: FIT-4
        gates = self.axis("gates")
        self.assert_entry("gates", gates)
        self.assertIs(gates["trusted"], True, f"3 of 10 missing is 30%, not over 30%: {gates}")
        self.assertAlmostEqual(gates["coverage"], 0.7, places=2)

    def test_few_unlabelled_tmp_refusals_keep_refusals_trusted(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assertIs(refusals["trusted"], True, f"3 of 10 old refusals target /tmp, not over half: {refusals}")

    def test_a_tie_goes_to_the_builder_with_its_counts(self):  # covers: AUD-2
        t = self.target()
        self.assertEqual(t["role"], "builder", f"two findings each: ties go to the builder: {t}")
        self.assertEqual(self.findings_of("builder"), 2, t)
        self.assertEqual(self.findings_of("test-writer"), 2, f"minor findings on test files don't count: {t}")

    def test_an_unfinished_draft_review_is_ignored_in_the_counts(self):  # covers: AUD-2
        t = self.target()
        self.assertEqual(self.findings_of("test-writer"), 2,
                         f"reviews/draft.json (a blocker and a major on test files) is unfinished and ignored: {t}")
        self.assertEqual(t["role"], "builder", f"counting the draft would make the test writer the target: {t}")


GATE_COMMAND = "python3 -m unittest discover -s tests/unit"


def stamp(i, minute, second=0):
    """A transcript timestamp (UTC, as Claude Code writes it) inside step i's window."""
    return f"{window(i, minute)[:-2]}{second:02d}.000Z"


def transcript_folder(path):
    """The name Claude Code gives a project's transcript folder: every character but letters and digits becomes -,
    so a worktree at <project>/.regista/worktrees/m2 gets <project's name>--regista-worktrees-m2."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def plain_tmp():
    """A temporary folder whose path has only letters, digits, / and -, so every way of writing its transcript
    folder's name agrees."""
    made = []
    for _ in range(20):
        made.append(tempfile.mkdtemp(prefix="regista-signals-"))
        if re.fullmatch(r"[A-Za-z0-9/-]+", made[-1]):
            break
    for extra in made[:-1]:
        shutil.rmtree(extra, ignore_errors=True)
    return made[-1]


class Transcripts:
    """Claude Code transcripts for a project: ~/.claude/projects/<project path with / as ->/<session>.jsonl for the
    orchestrator, and <session>/subagents/agent-<id>.jsonl (with agent-<id>.meta.json) for each subagent it
    started; the main transcript also holds the Agent call that started each subagent and its result."""

    def __init__(self, home, project, session="7c0d2a51-3e44-4b8a-9f10-2d6e8c1b5a90"):
        self.session = session
        self.folder = os.path.join(home, ".claude", "projects", transcript_folder(project))
        os.makedirs(os.path.join(self.folder, session, "subagents"), exist_ok=True)
        self.main = []
        self.cwd = project

    def line(self, when, kind, content, agent=None, **extra):
        row = {"type": kind, "timestamp": when, "sessionId": self.session, "cwd": self.cwd,
               "isSidechain": agent is not None, "message": {"role": kind, "content": content}, **extra}
        if agent:
            row["agentId"] = agent
        if kind == "assistant":
            row["message"].update({"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1000, "output_tokens": 50}})
        return row

    def bash(self, when, done, command, n, agent=None, failed=False):
        """A Bash call at `when` and its result at `done`."""
        tid = f"toolu_{agent or 'main'}_{n:02d}"
        return [self.line(when, "assistant", [{"type": "tool_use", "id": tid, "name": "Bash",
                                                "input": {"command": command}}], agent),
                self.line(done, "user", [{"type": "tool_result", "tool_use_id": tid, "is_error": failed,
                                          "content": "FAILED (failures=1)" if failed else "ok"}], agent)]

    def orchestrator_bash(self, when, done, command, failed=False):
        self.main += self.bash(when, done, command, len(self.main), failed=failed)

    def subagent(self, agent, role, start, end, calls=()):
        """A subagent of `role` (builder, researcher...) active from `start` to `end`; calls: (when, done, command,
        failed) Bash calls it made."""
        tid = f"toolu_agent_{agent}"
        self.main.append(self.line(start, "assistant", [{"type": "tool_use", "id": tid, "name": "Agent", "input": {
            "subagent_type": f"regista:{role}", "description": f"{role} work", "prompt": f"Do the {role} work."}}]))
        rows = [self.line(start, "user", f"Do the {role} work.", agent)]
        for n, (when, done, command, failed) in enumerate(calls):
            rows += self.bash(when, done, command, n, agent, failed)
        rows.append(self.line(end, "assistant", [{"type": "text", "text": "Done."}], agent))
        base = os.path.join(self.folder, self.session, "subagents", f"agent-{agent}")
        with open(base + ".jsonl", "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in rows))
        with open(base + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"agentType": f"regista:{role}"}, f)
        self.main.append(self.line(end, "user", [{"type": "tool_result", "tool_use_id": tid,
                                                  "content": [{"type": "text", "text": "Done."}]}],
                                   toolUseResult={"status": "completed", "agentId": agent,
                                                  "content": [{"type": "text", "text": "Done."}]}))

    def save(self):
        self.main.sort(key=lambda x: x["timestamp"])
        with open(os.path.join(self.folder, self.session + ".jsonl"), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in self.main))


class GateAttributionProject(SignalsBase):
    """Three gate runs fail, each at minute 10 of its step's window, while a researcher subagent is active:
    step 2's failing run was started by the orchestrator (its command is only in the main transcript), step 4's
    command is in no transcript at all, and step 6's command is in the builder's own subagent transcript (a
    researcher is active then too). No review finding or BLOCKED report anywhere, so gate failures decide the
    target."""
    name = "attrib"

    @classmethod
    def setUpClass(cls):
        # Claude Code names a project's transcript folder after its path; keep the path to letters, digits, / and -
        # so every way of writing that name agrees.
        cls.tmp = plain_tmp()
        cls.home = os.path.join(cls.tmp, "home")
        cls.evo = os.path.join(cls.tmp, "evolve")
        os.makedirs(cls.home)
        cls.repo = cls.make(os.path.join(cls.tmp, "projects"), cls.home)
        cls.result = run_build(cls.repo.root, cls.home, cls.evo)

    @classmethod
    def make(cls, parent, home):
        repo = build_project(parent, cls.name, gate_fails=(2, 4, 6),
                             reviews=[(i, "approve", []) for i in range(N_CASES)])
        t = Transcripts(home, repo.root)
        t.subagent("a2c4e6f8a0b2c4e6r", "researcher", stamp(2, 5), stamp(2, 15),
                   [(stamp(2, 8), stamp(2, 8, 5), "ls tests/unit", False)])
        t.orchestrator_bash(stamp(2, 9, 58), stamp(2, 10, 2), GATE_COMMAND, failed=True)
        t.subagent("a4c4e6f8a0b2c4e6r", "researcher", stamp(4, 5), stamp(4, 15),
                   [(stamp(4, 8), stamp(4, 8, 5), "ls tests/unit", False)])
        t.subagent("a6c4e6f8a0b2c4e6r", "researcher", stamp(6, 5), stamp(6, 15),
                   [(stamp(6, 8), stamp(6, 8, 5), "ls tests/unit", False)])
        t.subagent("a6c4e6f8a0b2c4e6b", "builder", stamp(6, 3), stamp(6, 30),
                   [(stamp(6, 9, 58), stamp(6, 10, 2), GATE_COMMAND, True),
                    (stamp(6, 34, 58), stamp(6, 35, 2), GATE_COMMAND, False)])
        t.save()
        return repo

    def gate_failures(self, role):
        return self.target()["counts"].get(role, {}).get("gate_failures", 0)

    def test_failures_not_in_a_subagent_transcript_are_never_charged_to_the_active_researcher(self):  # covers: AUD-2
        t = self.target()
        self.assertEqual(self.gate_failures("researcher"), 0,
                         f"a researcher was active at every failure, but none of its transcripts ran the gate: {t}")
        self.assertEqual(self.gate_failures("orchestrator"), 0,
                         f"the failure the orchestrator ran is unattributed, not charged to a role: {t}")

    def test_a_failure_in_the_builders_own_transcript_counts_against_the_builder(self):  # covers: AUD-2
        t = self.target()
        self.assertEqual(self.gate_failures("builder"), 1, f"the builder's transcript ran step 6's failing gate: {t}")
        self.assertEqual(sum(c.get("gate_failures", 0) for c in t["counts"].values()), 1,
                         f"of three failing runs only the builder's can be tied to a role: {t}")
        self.assertEqual(t["role"], "builder", t)


class TranscriptProjectBase(SignalsBase):
    """A fixture project in a temporary folder whose path is plain (see plain_tmp), for projects with transcripts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = plain_tmp()
        cls.home = os.path.join(cls.tmp, "home")
        cls.evo = os.path.join(cls.tmp, "evolve")
        os.makedirs(cls.home)
        cls.repo = cls.make(os.path.join(cls.tmp, "projects"), cls.home)
        cls.result = run_build(cls.repo.root, cls.home, cls.evo)


class GateAliasProject(TranscriptProjectBase):
    """Two gate runs fail at minute 10 of their step's window, and each time the builder's own subagent transcript
    shows it running the harness's gate command under an older name: step 3's builder ran
    `python /x/daedalus/scripts/dd.py gate tests`, step 7's ran `dd gate`."""
    name = "alias"

    @classmethod
    def make(cls, parent, home):
        repo = build_project(parent, cls.name, gate_fails=(3, 7),
                             reviews=[(i, "approve", []) for i in range(N_CASES)])
        t = Transcripts(home, repo.root)
        t.subagent("a3c4e6f8a0b2c4e6b", "builder", stamp(3, 3), stamp(3, 30),
                   [(stamp(3, 9, 58), stamp(3, 10, 2), "python /x/daedalus/scripts/dd.py gate tests", True)])
        t.subagent("a7c4e6f8a0b2c4e6b", "builder", stamp(7, 3), stamp(7, 30),
                   [(stamp(7, 9, 58), stamp(7, 10, 2), "dd gate", True)])
        t.save()
        return repo

    def test_the_harness_gate_command_under_its_old_names_charges_the_builder(self):  # covers: AUD-2
        t = self.target()
        builder = t["counts"].get("builder", {}).get("gate_failures", 0)
        self.assertEqual(builder, 2, f"the builder ran both failing gates, once through dd.py and once as `dd gate`: "
                                     f"{t}")
        self.assertEqual(t["role"], "builder", t)


TMP_TARGETS = [
    ({"file_path": "/tmp/x", "content": "x"}, "Write", "Regista: the builder may not write /tmp/x."),
    ({"command": "echo x > /tmp/x"}, "Bash", "Regista: the builder may not write /tmp/x."),
    ({"file_path": "/tmp/x/notes.md", "old_string": "a", "new_string": "b"}, "Edit",
     "Regista: the builder may not write /tmp/x/notes.md."),
    ({"command": "cp src/app.py /tmp/x"}, "Bash", "Regista: the builder may not write /tmp/x."),
    ({"file_path": "/tmp/x/out.json", "content": "{}"}, "Write", "Regista: the builder may not write /tmp/x/out.json."),
    ({"command": "mkdir -p /tmp/x && touch /tmp/x/done"}, "Bash", "Regista: the builder may not write /tmp/x."),
]
FAILED_GATES = ("Regista blocked this commit: a gate failed.\n- tests: FAIL in 1.0 s\n    · command exited 1\n\n"
                "Fix the cause (or revert the change) and commit again. Never weaken a gate to pass it.\n\n[tests]\n"
                "FileNotFoundError: /tmp/x/fixture.json")
TMP_MENTIONS = [
    ({"command": 'git commit -q -m "fix: keep the pytest output out of /tmp"'}, "Bash",
     "Regista: the builder doesn't commit; the orchestrator does."),
    ({"command": 'grep -rn "/tmp/" src tests'}, "Bash",
     "Regista: the test writer doesn't read implementation files from the shell either (src)."),
    ({"command": "rg -n '/tmp/x' scripts"}, "Bash",
     "Regista: the test writer doesn't read implementation files from the shell either (scripts)."),
    ({"command": 'git add -A && git commit -q -m "s08: write scratch files under /tmp/x"'}, "Bash", FAILED_GATES),
    ({"command": 'git add -A && git commit -q -m "s09: done"'}, "Bash", FAILED_GATES),
]


class TmpMentionProject(SignalsBase):
    """Old unlabelled refusals, one per window: five whose refused write target is /tmp/x (suspected false alarms)
    and five that only mention /tmp: in a commit message, in a grep pattern, or in a commit refused because the
    gates failed (never suspected). Five of ten is not over half."""
    name = "tmpmention"

    @classmethod
    def make(cls, parent, home):
        rows = [refusal(window(i, 20), inp, tool=tool, reason=why)
                for i, (inp, tool, why) in enumerate(TMP_TARGETS[:5] + TMP_MENTIONS)]
        return build_project(parent, cls.name, refusals=rows, reviews=[(i, "approve", []) for i in range(N_CASES)])

    def test_mentions_of_tmp_outside_the_write_target_are_not_suspected(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assert_entry("refusals", refusals)
        self.assertIs(refusals["trusted"], True,
                      f"only the five refusals writing to /tmp/x are suspected; /tmp in a commit message, a grep "
                      f"pattern or a commit refused for failed gates doesn't count: {refusals}")


class TmpTargetProject(SignalsBase):
    """Old unlabelled refusals, one per window: six whose refused write target is /tmp/x and four that only mention
    /tmp. Six of ten suspected false alarms is over half."""
    name = "tmptarget"

    @classmethod
    def make(cls, parent, home):
        rows = [refusal(window(i, 20), inp, tool=tool, reason=why)
                for i, (inp, tool, why) in enumerate(TMP_TARGETS + TMP_MENTIONS[:4])]
        return build_project(parent, cls.name, refusals=rows, reviews=[(i, "approve", []) for i in range(N_CASES)])

    def test_refusals_whose_write_target_is_tmp_are_suspected(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assert_entry("refusals", refusals)
        self.assertIs(refusals["trusted"], False,
                      f"six of ten refusals were refused writes to /tmp/x, suspected false alarms: {refusals}")
        self.assertTrue(refusals["reasons"])


def project_path(parent, name, rel):
    """The absolute path of `rel` inside the fixture project `name` (before the project exists)."""
    return os.path.join(parent, name, rel)


def spread(entries):
    """Old unlabelled refusals spread over the ten windows (a second round in each window when there are more)."""
    return [refusal(window(n % N_CASES, 15 + 5 * (n // N_CASES)), inp, tool=tool, reason=why)
            for n, (inp, tool, why) in enumerate(entries)]


class NotSuspectedProject(SignalsBase):
    """Old unlabelled refusals: seven sure suspected false alarms (refused writes to /tmp/x) and seven that are not
    suspected under the final rule, because they have no write target at all or a target outside /tmp, a scratchpad
    and the agent's own notes. Seven of fourteen is exactly half, so refusals stay trusted; counting any one of the
    seven as suspected makes it eight of fourteen."""
    name = "notsuspected"

    @classmethod
    def make(cls, parent, home):
        not_suspected = [
            ({"command": "git commit --no-verify -F /tmp/msg.txt"}, "Bash",
             "Regista: --no-verify skips the gates; commit without it."),
            ({"command": "cp /tmp/cfg.json .regista/config.json"}, "Bash",
             "Regista: the builder may not change .regista/config.json."),
            ({"command": "mkdir -p /tmp/scratchpad/x && echo y > tests/unit/zz-probe.tmp.mjs"}, "Bash",
             "Regista: the builder may not write tests/unit/zz-probe.tmp.mjs."),
            ({"command": "node scripts/build-web.js"}, "Bash",
             "Regista: the test writer may not run implementation scripts."),
            ({"subagent_type": "builder", "prompt": "build the page and keep scratch files in /tmp"}, "Agent",
             "Regista: only the orchestrator starts other agents."),
            ({"command": "python3 -c \"open('/tmp/a','w')\""}, "Bash",
             "Regista: the test writer doesn't run code from the shell."),
            ({"file_path": project_path(parent, cls.name, "docs/notes.md"), "content": "# Notes\n"}, "Write",
             "Regista: the researcher may not write docs/notes.md."),
        ]
        rows = spread(TMP_TARGETS + TMP_TARGETS[:1] + not_suspected)
        return build_project(parent, cls.name, refusals=rows, reviews=[(i, "approve", []) for i in range(N_CASES)])

    def test_refusals_without_a_tmp_scratchpad_or_notes_target_are_not_suspected(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assert_entry("refusals", refusals)
        self.assertIs(refusals["trusted"], True,
                      "not suspected: a refused commit with -F /tmp/msg.txt (no write target), cp from /tmp into "
                      ".regista/config.json, mkdir in /tmp plus a write to tests/unit (not every target is in /tmp), "
                      "node scripts/build-web.js (no target), an Agent call (no target at all), python3 -c code "
                      "naming /tmp (code is not a target), and a researcher writing docs/notes.md (not its notes "
                      f"file); only seven of fourteen are suspected: {refusals}")


class SuspectedTargetsProject(SignalsBase):
    """Old unlabelled refusals: four whose every write target is under /tmp, a scratchpad folder or the researcher's
    own notes file, and three refused writes to the project's source. Four of seven is over half, so refusals are
    not trusted; missing any one of the four makes it three of seven."""
    name = "suspected"

    @classmethod
    def make(cls, parent, home):
        suspected = [
            ({"command": "echo x > /tmp/out.txt"}, "Bash", "Regista: the builder may not write /tmp/out.txt."),
            ({"command": "mkdir -p /tmp/a && cp a.txt /tmp/a/"}, "Bash", "Regista: the builder may not write /tmp/a."),
            ({"file_path": "/home/sample/.cache/claude/9b41/scratchpad/out.md", "content": "x"}, "Write",
             "Regista: the builder may not write /home/sample/.cache/claude/9b41/scratchpad/out.md."),
            ({"file_path": project_path(parent, cls.name, ".regista/research/topic.md"), "content": "# Topic\n"},
             "Write", "Regista: the researcher may not write .regista/research/topic.md."),
        ]
        real = [({"command": f"echo x > src/mod_{i}.py"}, "Bash", f"Regista: the test writer may not write "
                                                                   f"src/mod_{i}.py.") for i in range(3)]
        rows = spread(suspected + real)
        return build_project(parent, cls.name, refusals=rows, reviews=[(i, "approve", []) for i in range(N_CASES)])

    def test_refusals_whose_every_target_is_tmp_a_scratchpad_or_the_notes_file_are_suspected(self):  # covers: FIT-4
        refusals = self.axis("refusals")
        self.assert_entry("refusals", refusals)
        self.assertIs(refusals["trusted"], False,
                      "suspected: echo > /tmp/out.txt, mkdir and cp into /tmp/a, a Write under a scratchpad folder, "
                      "and the researcher writing its own notes file .regista/research/topic.md; four of seven is "
                      f"over half: {refusals}")
        self.assertTrue(refusals["reasons"])


class WorktreeTranscriptProject(TranscriptProjectBase):
    """The project has a worker worktree at .regista/worktrees/m2. Steps 0 to 4 have transcripts in the main
    project's folder, steps 5 to 9 only in the worktree's folder
    (~/.claude/projects/<project's name>--regista-worktrees-m2/)."""
    name = "worktree"

    @classmethod
    def make(cls, parent, home):
        repo = build_project(parent, cls.name, reviews=[(i, "approve", []) for i in range(N_CASES)])
        with open(os.path.join(repo.root, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
            f.write(".regista/worktrees/\n")
        worktree = os.path.join(repo.root, ".regista", "worktrees", "m2")
        repo.git("worktree", "add", "-q", "-b", "m2", worktree)
        main = Transcripts(home, repo.root, session="1a0d2a51-3e44-4b8a-9f10-2d6e8c1b5a90")
        side = Transcripts(home, worktree, session="2b0d2a51-3e44-4b8a-9f10-2d6e8c1b5a90")
        expected = transcript_folder(repo.root) + "--regista-worktrees-m2"
        assert os.path.basename(side.folder) == expected, (side.folder, expected)
        for i in range(N_CASES):
            t = main if i < 5 else side
            t.subagent(f"a{i}c4e6f8a0b2c4e6b", "builder", stamp(i, 3), stamp(i, 30),
                       [(stamp(i, 5), stamp(i, 5, 5), "ls src", False)])
        main.save()
        side.save()
        return repo

    def test_transcripts_in_worker_worktree_folders_count_toward_token_coverage(self):  # covers: FIT-4
        tokens = self.axis("tokens")
        self.assert_entry("tokens", tokens)
        self.assertAlmostEqual(tokens["coverage"], 1.0, places=2,
                               msg=f"half the cases' transcripts are in the m2 worktree's folder: {tokens}")
        self.assertIs(tokens["trusted"], True, tokens)


if __name__ == "__main__":
    unittest.main()
