"""Replayable verdicts with reasons: `evolve.py bench build <project>` turns every step that has a spec into a case
file with the parent and finishing commits, the spec and tests as they were at the finishing commit, the baseline
numbers from the project's records, a replayable verdict with plain reasons, and a stable train/holdout split.

The fixture projects are small Regista projects made with plain git in temporary folders, shaped like FPL's real
history (see .regista/research/m2-fpl-audit.md): specs created early in bulk, specs moved from .daedalus/specs to
.regista/specs, follow-up commits adding more covers markers, live-only tests, a spec pointing at a file in the
user's home folder, and gitignored records (hooks.log, gates/history.jsonl, compactions.jsonl).

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_bench_cases -v"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVOLVE = os.path.join(ROOT, "scripts", "evolve.py")
BASE_DAY = "2026-10-20"

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}

CONFIG = {
    "project": "sample",
    "plan": ".regista/plan.md",
    "board_file": ".regista/TODO.md",
    "gates": [
        {"name": "tests", "run": "python3 -m unittest discover -s tests/unit", "tests": ["tests/unit/**"],
         "when": ["commit"], "timeout": 600},
        {"name": "e2e", "run": "python3 -m unittest discover -s tests/e2e", "evidence": "e2e",
         "tests": ["tests/e2e/**"], "when": ["commit"], "timeout": 600},
        {"name": "live", "run": "python3 -m unittest discover -s tests/live", "tests": ["tests/live/**"],
         "when": ["manual"], "timeout": 600},
    ],
    "roles": {"source": ["src/**"], "tests": ["tests/**"]},
    "requirements": {"enforce_coverage": True},
}

GITIGNORE = ".regista/hooks.log\n.regista/gates/\n.regista/compactions.jsonl\n.regista/state/\n__pycache__/\n"


def at(hour, minute=0):
    """A time on the fixture's day, `hour` hours after 08:00 UTC (may run past midnight)."""
    total = 8 * 60 + int(hour * 60) + minute
    day = 20 + total // (24 * 60)
    total %= 24 * 60
    return f"2026-10-{day:02d}T{total // 60:02d}:{total % 60:02d}:00"


def spec(step, milestone, title, plan_step, interface, reqs):
    return json.dumps({"step": step, "milestone": milestone, "title": title, "plan_step": plan_step,
                       "state": "active", "interface": interface,
                       "requirements": [{"id": i, "text": t, "verify": v} for i, t, v in reqs]}, indent=1) + "\n"


def test_file(name, covers, lines=1):
    body = [f'"""Tests for {name}."""', "import unittest", "", "", f"class {name.title().replace('_', '')}(unittest.TestCase):"]
    for n in range(lines):
        if n == 0 and covers:
            body.append(f"    def test_{n}(self):  # covers: {covers}")
        else:
            body.append(f"    def test_{n}(self):")
        body.append(f"        self.assertEqual({n} + 1, {n + 1})")
    return "\n".join(body) + "\n"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class Repo:
    """A git repository in a temporary folder, with commits at chosen times."""

    def __init__(self, parent, name="sample"):
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

    def commit(self, hour, message, files=None, add=True):
        for rel, text in (files or {}).items():
            self.write(rel, text)
        if add:
            self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=at(hour))
        return self.git("rev-parse", "HEAD")

    def show(self, commit, rel):
        r = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=self.root, capture_output=True,
                           env=dict(os.environ, **GIT_ENV))
        return r.stdout

    def short(self, commit):
        return commit[:7]


def run_build(project, home, evolve_home):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "REGISTA_EVOLVE_HOME")}
    env.update({"HOME": home, "REGISTA_EVOLVE_HOME": evolve_home, "TZ": "UTC", **GIT_ENV})
    return subprocess.run([sys.executable, EVOLVE, "bench", "build", project], cwd=os.path.dirname(project),
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)


def load_cases(evolve_home, project="sample"):
    """{step: (case name, case dict)} for every case file of the project."""
    folder = os.path.join(evolve_home, "bench", project)
    cases = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json") and name != "signals.json":
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                case = json.load(f)
            cases[case["step"]] = (name[:-5], case)
    return cases


def same_commit(value, full):
    return isinstance(value, str) and len(value) == 40 and value == full


def build_sample(parent):
    """The main fixture: seven steps, each with one way a step can turn out. Returns (repo, commits)."""
    r = Repo(parent)
    c = {}
    plan = ("# Sample: plan\n\n## M0 — Start\n- [x] First thing (FIRST-1)\n\n## M1 — Old\n- [x] Old step (OLD-1)\n"
            "- [x] Orphan step (ORPH-1)\n\n## M2 — Main\n- [x] Main feature with export (MAIN-1, MAIN-2)\n"
            "- [x] Deploy to the server (DEP-1, DEP-2)\n\n## M3 — More\n- [x] Partly live (PART-1, PART-2, PART-3)\n"
            "- [x] Page copy from the brief (BRF-1)\n- [x] Button colour (DEC-1)\n")
    c["root"] = r.commit(0, "setup", {
        ".gitignore": GITIGNORE, ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n", ".regista/plan.md": plan,
        "src/app.py": "VALUE = 1\n",
        ".daedalus/specs/m0-first.json": spec("m0-first", "M0", "First thing", "First thing (FIRST-1)",
                                              "src/app.py holds VALUE.", [("FIRST-1", "VALUE SHALL be 1", "test")]),
        "tests/unit/test_first.py": test_file("first", "FIRST-1")})
    c["specs"] = r.commit(1, "specs for M1 and M2", {
        ".daedalus/specs/m1-old.json": spec("m1-old", "M1", "Old step", "Old step (OLD-1)",
                                            "old() in src/old.py.", [("OLD-1", "old() SHALL return 2", "test")]),
        ".daedalus/specs/m1-orphan.json": spec("m1-orphan", "M1", "Orphan step", "Orphan step (ORPH-1)",
                                               "Nothing to build yet.", [("ORPH-1", "orphan SHALL exist", "test")]),
        ".daedalus/specs/m2-main.json": spec("m2-main", "M2", "Main feature", "Main feature with export (MAIN-1, MAIN-2)",
                                             "main() in src/main.py.", [("MAIN-1", "main() SHALL return 3", "test")]),
        # Mentions of ORPH-1 that are not covers markers in a test file: neither may count.
        "docs/notes.md": "Some notes. covers: ORPH-1 (not a test file)\n",
        "tests/unit/test_orphan_note.py": "# ORPH-1 is checked by hand on the phone.\n" + test_file("orphan_note", None)})
    c["old_parent"] = r.commit(2, "old: source", {"src/old.py": "def old():\n    return 2\n"})
    c["old_finish"] = r.commit(3, "old: tests", {
        "tests/unit/test_old.py": test_file("old", "OLD-1"),
        ".daedalus/specs/m1-old.json": spec("m1-old", "M1", "Old step v2", "Old step (OLD-1)", "old() in src/old.py.",
                                            [("OLD-1", "old() SHALL return 2", "test"),
                                             ("OLD-2", "old() SHALL be pure", "test")])})
    c["old_follow"] = r.commit(4, "old: follow-up", {"tests/unit/test_old_more.py": test_file("old_more", "OLD-2")})
    r.git("mv", ".daedalus/specs", ".regista/specs")
    c["move"] = r.commit(5, "move specs to .regista")
    c["retitle"] = r.commit(6, "retitle old", {
        ".regista/specs/m1-old.json": spec("m1-old", "M1", "Old step v3", "Old step (OLD-1)", "old() in src/old.py.",
                                           [("OLD-1", "old() SHALL return 2", "test"),
                                            ("OLD-2", "old() SHALL be pure", "test")]),
        ".regista/specs/README.md": "One spec per step.\n"})
    c["main_parent"] = r.commit(7, "main: source", {"src/main.py": "def main():\n    return 3\n"})
    c["main_finish"] = r.commit(10, "main: tests", {
        "tests/unit/test_main.py": test_file("main", "MAIN-1"),
        "tests/e2e/test_main_e2e.py": test_file("main_e2e", "MAIN-2"),
        "tests/unit/test_first.py": test_file("first", "FIRST-1", lines=2),
        "src/main.py": "def main():\n    return 3\n\n\ndef export():\n    return 'csv'\n",
        ".regista/specs/m2-main.json": spec(
            "m2-main", "M2", "Main feature", "Main feature with export (MAIN-1, MAIN-2)",
            "main() and export() in src/main.py.",
            [("MAIN-1", "main() SHALL return 3", "test"), ("MAIN-2", "export() SHALL give csv", "e2e")])})
    c["main_follow"] = r.commit(11, "main: follow-up", {
        "tests/unit/test_main.py": test_file("main", "MAIN-1", lines=3),
        "tests/unit/test_main_extra.py": test_file("main_extra", "MAIN-1")})
    c["specs3"] = r.commit(12, "specs for deploy, partial, brief", {
        ".regista/specs/m2-deploy.json": spec("m2-deploy", "M2", "Deploy", "Deploy to the server (DEP-1, DEP-2)",
                                              "Upload to the server.", [("DEP-1", "the site SHALL answer", "test"),
                                                                        ("DEP-2", "the site SHALL use TLS", "test")]),
        ".regista/specs/m3-partial.json": spec(
            "m3-partial", "M3", "Partly live", "Partly live (PART-1, PART-2, PART-3)", "partial() in src/partial.py.",
            [("PART-1", "partial() SHALL return 4", "test"), ("PART-2", "partial() SHALL be fast", "test"),
             ("PART-3", "the deployed page SHALL show 4", "test")]),
        ".regista/specs/m3-brief.json": spec(
            "m3-brief", "M3", "Page copy", "Page copy from the brief (BRF-1)",
            "Take the page copy from ~/brief.md, section Welcome.", [("BRF-1", "the page SHALL greet", "test")])})
    c["deploy_finish"] = r.commit(13, "deploy", {"src/deploy.py": "HOST = 'example.com'\n",
                                                  "tests/live/test_deploy.py": test_file("deploy", "DEP-1, DEP-2")})
    r.git("checkout", "-q", "-b", "side")
    c["partial_parent"] = r.commit(14, "partial: source", {"src/partial.py": "def partial():\n    return 4\n"})
    c["partial_finish"] = r.commit(16, "partial: tests", {
        "tests/unit/test_partial.py": test_file("partial", "PART-1, PART-2"),
        "tests/live/test_partial_live.py": test_file("partial_live", "PART-3")})
    r.git("checkout", "-q", "main")
    c["other"] = r.commit(15, "unrelated", {"src/other.py": "OTHER = 5\n"})
    r.git("merge", "-q", "--no-ff", "-m", "merge side", "side", date=at(17))
    c["merge"] = r.git("rev-parse", "HEAD")
    c["brief_parent"] = r.commit(18, "brief: source", {"src/brief.py": "GREETING = 'hello'\n"})
    c["brief_finish"] = r.commit(19, "brief: tests", {"tests/unit/test_brief.py": test_file("brief", "BRF-1")})
    c["decide_parent"] = r.commit(19.25, "decide: spec and source", {
        ".regista/specs/m3-decide.json": spec("m3-decide", "M3", "Button colour", "Button colour (DEC-1)",
                                              "button() in src/button.py.", [("DEC-1", "button() SHALL be blue", "test")]),
        "src/button.py": "def button():\n    return 'blue'\n"})
    c["decide_finish"] = r.commit(19.5, "decide: tests", {"tests/unit/test_decide.py": test_file("decide", "DEC-1")})
    reviews = {
        "r1.json": {"verdict": "changes", "base": r.short(c["main_parent"]), "checked": ["MAIN-1"], "at": at(8)},
        "r2.json": {"verdict": "approve", "base": r.short(c["main_parent"]), "checked": ["MAIN-1", "MAIN-2"],
                    "at": at(9)},
        "r3.json": {"verdict": "approve", "base": r.short(c["old_parent"]), "checked": ["OLD-1"], "at": at(2, 30)},
        "draft.json": {"verdict": "changes", "checked": [], "at": at(20)},
    }
    files = {f".regista/reviews/{k}": json.dumps(v) + "\n" for k, v in reviews.items()}
    # Only an open line in the 'Open decisions' section holds a step back; the others name steps too.
    files[".regista/TODO.md"] = ("# Board\n\n## Next\n- ⬜ m3-partial: tidy the copy later\n\n## Open decisions\n"
                                 "- ⬜ m3-decide: which colour for the button? Recommended: blue.\n"
                                 "- ✅ m2-main: export format decided (csv).\n\n## Done\n- ✅ m1-old\n")
    c["reviews"] = r.commit(20, "reviews and board", files)

    def gate_run(hour, minute, commit, status):
        return json.dumps({"date": at(hour, minute), "commit": r.short(commit),
                           "results": [{"name": "tests", "exit": 0 if status == "pass" else 1, "seconds": 1.0,
                                        "status": status}]})
    # The main step's window runs from 15:00 (parent) to 18:00 (finishing commit); partial's from 22:00 to 00:00.
    r.write(".regista/gates/history.jsonl", "\n".join([
        gate_run(6, 30, c["retitle"], "fail"),        # before the main window
        gate_run(7, 30, c["main_parent"], "fail"),    # in it
        gate_run(8, 0, c["main_parent"], "pass"),     # in it
        gate_run(10, 30, c["main_finish"], "fail"),   # after it
        gate_run(15, 0, c["partial_parent"], "pass"),  # in the partial window
    ]) + "\n")

    def refusal(hour, minute):
        return json.dumps({"at": at(hour, minute), "tool": "Bash", "input": {"command": "ls"},
                           "reason": "Regista: refused for the test"})
    r.write(".regista/hooks.log", "\n".join(
        [refusal(6, 40), refusal(7, 20), refusal(8, 10), refusal(9, 50), refusal(10, 20)]) + "\n")
    r.write(".regista/compactions.jsonl", "\n".join([
        json.dumps({"at": at(8, 30), "trigger": "auto"}), json.dumps({"at": at(4), "trigger": "auto"})]) + "\n")
    return r, c


STEPS = {"m0-first", "m1-old", "m1-orphan", "m2-main", "m2-deploy", "m3-partial", "m3-brief", "m3-decide"}
KEYS = {"project", "repo", "step", "milestone", "parent", "finishing_commit", "spec", "scoring_ids", "tests",
        "outside_files", "brief", "baseline", "replayable", "reasons", "split", "screening"}


class BenchBuildSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-bench-")
        cls.home = os.path.join(cls.tmp, "home")
        cls.evo = os.path.join(cls.tmp, "evolve")
        os.makedirs(cls.home)
        cls.repo, cls.c = build_sample(os.path.join(cls.tmp, "projects"))
        # A case left from an earlier build of a step that no longer exists: rebuilding replaces the cases.
        os.makedirs(os.path.join(cls.evo, "bench", "sample"))
        with open(os.path.join(cls.evo, "bench", "sample", "ghost.json"), "w", encoding="utf-8") as f:
            json.dump({"step": "ghost", "replayable": True, "reasons": ["old"]}, f)
        cls.result = run_build(cls.repo.root, cls.home, cls.evo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.result.returncode, 0,
                         f"bench build failed:\n{self.result.stdout}\n{self.result.stderr}")
        self.cases = load_cases(self.evo)

    def case(self, step):
        self.assertIn(step, self.cases, f"no case for {step}; cases: {sorted(self.cases)}")
        return self.cases[step][1]

    def test_every_step_with_a_spec_gets_a_case_with_a_verdict_and_reasons(self):  # covers: BENCH-1
        self.assertEqual(set(self.cases), STEPS, "one case per spec file, the README isn't a step, old cases go")
        for step, (_, case) in self.cases.items():
            self.assertIn(case["replayable"], (True, False), step)
            self.assertIsInstance(case["reasons"], list, step)
            self.assertTrue(case["reasons"] and all(isinstance(x, str) and len(x.split()) >= 3 for x in case["reasons"]),
                            f"{step} needs at least one plain-sentence reason: {case['reasons']}")
            self.assertEqual(case["project"], "sample", step)
            self.assertEqual(os.path.realpath(case["repo"]), os.path.realpath(self.repo.root), step)
            self.assertLessEqual(KEYS, set(case), f"{step} lacks {sorted(KEYS - set(case))}")
            if case["replayable"]:
                self.assertIn(case["split"], ("train", "holdout"), step)
                self.assertIn(case["screening"], (True, False), step)
            else:
                self.assertIsNone(case["split"], f"{step} isn't replayable, so it has no split")
                self.assertIs(case["screening"], False, step)
        self.assertEqual(self.case("m0-first")["milestone"], "M0")
        self.assertEqual(self.case("m2-main")["milestone"], "M2")

    def test_finishing_and_parent_found_by_first_covers_marker(self):  # covers: BENCH-1
        main = self.case("m2-main")
        self.assertTrue(same_commit(main["finishing_commit"], self.c["main_finish"]),
                        "the first commit adding a covers marker for MAIN-1 or MAIN-2, not the follow-up")
        self.assertTrue(same_commit(main["parent"], self.c["main_parent"]))
        self.assertTrue(main["replayable"], main["reasons"])
        old = self.case("m1-old")
        self.assertTrue(same_commit(old["finishing_commit"], self.c["old_finish"]))
        self.assertTrue(same_commit(old["parent"], self.c["old_parent"]))
        partial = self.case("m3-partial")
        self.assertTrue(same_commit(partial["finishing_commit"], self.c["partial_finish"]),
                        "a commit on a merged branch, not the merge commit")
        self.assertTrue(same_commit(partial["parent"], self.c["partial_parent"]), "the finishing commit's first parent")

    def test_spec_followed_from_daedalus_and_taken_at_the_finishing_commit(self):  # covers: BENCH-1
        old = self.case("m1-old")
        self.assertEqual(old["spec"]["title"], "Old step v2", "the spec as it was at the finishing commit, not at HEAD")
        self.assertEqual([q["id"] for q in old["spec"]["requirements"]], ["OLD-1", "OLD-2"])
        self.assertEqual(sorted(old["scoring_ids"]), ["OLD-1", "OLD-2"])
        main = self.case("m2-main")
        self.assertEqual(main["spec"]["interface"], "main() and export() in src/main.py.")
        self.assertEqual(sorted(main["scoring_ids"]), ["MAIN-1", "MAIN-2"])

    def test_no_finishing_commit_or_root_parent_is_not_replayable(self):  # covers: BENCH-1
        orphan = self.case("m1-orphan")
        self.assertFalse(orphan["replayable"], "a mention in docs or a plain comment in a test isn't a covers marker")
        self.assertIsNone(orphan["finishing_commit"])
        self.assertIsNone(orphan["parent"])
        first = self.case("m0-first")
        self.assertTrue(same_commit(first["finishing_commit"], self.c["root"]))
        self.assertIsNone(first["parent"], "the root commit has no parent")
        self.assertFalse(first["replayable"])

    def test_tests_copied_from_the_finishing_commit_with_sha256(self):  # covers: BENCH-1
        name, main = self.cases["m2-main"]
        want = ["tests/e2e/test_main_e2e.py", "tests/unit/test_first.py", "tests/unit/test_main.py"]
        self.assertEqual(sorted(t["path"] for t in main["tests"]), want,
                         "the test files the finishing commit added or changed, not src/ or the spec")
        copies = {}
        folder = os.path.join(self.evo, "bench", "sample", name, "tests")
        self.assertTrue(os.path.isdir(folder), f"no test copies in {folder}")
        for dirpath, _, files in os.walk(folder):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    copies[sha256(fh.read())] = os.path.join(dirpath, f)
        for t in main["tests"]:
            at_finish = self.repo.show(self.c["main_finish"], t["path"])
            self.assertEqual(t["sha256"], sha256(at_finish), t["path"])
            self.assertIn(t["sha256"], copies, f"{t['path']} isn't copied with its contents at the finishing commit")
        head = self.repo.show("HEAD", "tests/unit/test_main.py")
        self.assertNotIn(sha256(head), copies, "the follow-up's version of test_main.py must not be copied")

    def test_brief_rebuilt_from_plan_step_and_spec(self):  # covers: BENCH-1
        brief = json.dumps(self.case("m2-main")["brief"], ensure_ascii=False)
        for text in ("Main feature", "main() and export() in src/main.py.", "main() SHALL return 3",
                     "export() SHALL give csv", "Main feature with export"):
            self.assertIn(text, brief)

    def test_baseline_counts_records_in_the_step_window(self):  # covers: BENCH-1
        base = self.case("m2-main")["baseline"]
        self.assertEqual(base["review_rounds"], 2, "the two reviews whose base is the parent")
        self.assertEqual(base["gate_failures"], 1, "one failing run between the parent's and finishing commit's times")
        self.assertEqual(base["refusals"], 3, "three hooks.log lines in the window")
        self.assertEqual(base["compactions"], 1)
        self.assertIs(base["logs_complete"], True)
        self.assertEqual(self.case("m1-old")["baseline"]["review_rounds"], 1)
        self.assertIs(self.case("m3-partial")["baseline"]["logs_complete"], False,
                      "a gate run but no hooks.log line in the window, as for worker-built steps")

    def test_output_has_one_line_per_step_and_a_total(self):  # covers: BENCH-1
        lines = self.result.stdout.splitlines()
        for step, (_, case) in self.cases.items():
            mine = [ln for ln in lines if re.search(rf"(?<![\w-]){re.escape(step)}(?![\w-])", ln)]
            self.assertEqual(len(mine), 1, f"one line for {step}: {lines}")
            after = mine[0].split(step, 1)[1]
            word = re.search(r"\b(yes|no)\b", after, re.I)
            self.assertTrue(word, mine[0])
            self.assertEqual(word.group(1).lower(), "yes" if case["replayable"] else "no", mine[0])
        self.assertTrue(re.search(r"\b8\b", "\n".join(ln for ln in lines if not any(s in ln for s in STEPS))),
                        f"a total line naming the 8 cases: {lines}")

    def test_open_board_decision_makes_the_step_not_replayable(self):  # covers: BENCH-1
        decide = self.case("m3-decide")
        self.assertTrue(same_commit(decide["finishing_commit"], self.c["decide_finish"]))
        self.assertFalse(decide["replayable"], "an open line in 'Open decisions' names m3-decide")
        self.assertTrue(any("decision" in r.lower() for r in decide["reasons"]), decide["reasons"])
        self.assertTrue(self.case("m2-main")["replayable"], "a closed decision line doesn't hold a step back")
        self.assertTrue(self.case("m3-partial")["replayable"], "an open line outside 'Open decisions' doesn't count")

    def test_live_only_step_is_not_replayable(self):  # covers: BENCH-8
        deploy = self.case("m2-deploy")
        self.assertFalse(deploy["replayable"], "DEP-1 and DEP-2 are covered only under tests/live/")
        self.assertTrue(any("live" in r.lower() or "gate" in r.lower() for r in deploy["reasons"]), deploy["reasons"])

    def test_partly_live_step_leaves_live_ids_out_of_scoring(self):  # covers: BENCH-8
        partial = self.case("m3-partial")
        self.assertTrue(partial["replayable"], partial["reasons"])
        self.assertEqual(sorted(partial["scoring_ids"]), ["PART-1", "PART-2"])
        self.assertTrue(any("PART-3" in r for r in partial["reasons"]), f"a reason names PART-3: {partial['reasons']}")

    def test_file_outside_the_repo_listed_and_missing_means_not_replayable(self):  # covers: BENCH-8
        brief = self.case("m3-brief")
        self.assertFalse(brief["replayable"], "~/brief.md doesn't exist under this HOME")
        self.assertTrue(brief["outside_files"] and all(p.endswith("brief.md") for p in brief["outside_files"]),
                        f"the outside file's path is listed: {brief['outside_files']}")
        self.assertTrue(any("brief.md" in r for r in brief["reasons"]), brief["reasons"])
        for step in STEPS - {"m3-brief"}:
            self.assertEqual(self.case(step)["outside_files"], [], step)

    def test_file_outside_the_repo_present_means_replayable(self):  # covers: BENCH-8
        tmp = tempfile.mkdtemp(prefix="regista-bench-home-")
        self.addCleanup(shutil.rmtree, tmp, True)
        home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve")
        os.makedirs(home)
        with open(os.path.join(home, "brief.md"), "w", encoding="utf-8") as f:
            f.write("# Welcome\nHello.\n")
        result = run_build(self.repo.root, home, evo)
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = load_cases(evo)["m3-brief"][1]
        self.assertTrue(brief["replayable"], brief["reasons"])
        self.assertTrue(brief["outside_files"] and brief["outside_files"][0].endswith("brief.md"))

    def test_screening_cases_are_replayable_train_cases(self):  # covers: BENCH-9
        for step, (_, case) in self.cases.items():
            if case["screening"]:
                self.assertEqual(case["split"], "train", step)
                self.assertTrue(case["replayable"], step)
            else:
                self.assertIs(case["screening"], False, step)


def build_in_home(home, name="homeproj"):
    """A project living in the user's home folder, whose specs cite files with ~/ paths: one inside the project
    (committed at the parent) and one wildcard path to another folder in the home."""
    r = Repo(home, name)
    plan = ("# Sample: plan\n\n## M1 — Home\n- [x] Research step (RES-1)\n- [x] Notes step (NOT-1)\n")
    r.commit(0, "setup", {".gitignore": GITIGNORE, ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n",
                          ".regista/plan.md": plan, "src/app.py": "VALUE = 1\n",
                          "docs/research.md": "# Research\nFindings.\n"})
    r.commit(1, "research: spec and source", {
        ".regista/specs/h1-research.json": spec(
            "h1-research", "M1", "Research step", "Research step (RES-1)",
            f"Follow ~/{name}/docs/research.md, section Findings.", [("RES-1", "research() SHALL work", "test")]),
        "src/research.py": "def research():\n    return 1\n"})
    r.commit(2, "research: tests", {"tests/unit/test_research.py": test_file("research", "RES-1")})
    r.commit(3, "notes: spec and source", {
        ".regista/specs/h2-notes.json": spec(
            "h2-notes", "M1", "Notes step", "Notes step (NOT-1)",
            "Take the wording from ~/other-*/notes.md.", [("NOT-1", "notes() SHALL work", "test")]),
        "src/notes.py": "def notes():\n    return 2\n"})
    r.commit(4, "notes: tests", {"tests/unit/test_notes.py": test_file("notes", "NOT-1")})
    return r


class BenchOutsideFilesUnderHome(unittest.TestCase):
    """~/ paths are resolved against HOME: one into the repository is inside; a wildcard counts when it matches."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-bench-homeproj-")
        cls.home = os.path.realpath(os.path.join(cls.tmp, "home"))
        os.makedirs(cls.home)
        cls.repo = build_in_home(cls.home)
        # Near misses for ~/other-*/notes.md: a matching folder without notes.md, and notes.md in a folder that
        # doesn't match.
        os.makedirs(os.path.join(cls.home, "other-empty"))
        os.makedirs(os.path.join(cls.home, "another"))
        with open(os.path.join(cls.home, "another", "notes.md"), "w", encoding="utf-8") as f:
            f.write("not this one\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self):
        evo = tempfile.mkdtemp(prefix="evolve-", dir=self.tmp)
        result = run_build(self.repo.root, self.home, evo)
        self.assertEqual(result.returncode, 0, f"bench build failed:\n{result.stdout}\n{result.stderr}")
        cases = load_cases(evo, "homeproj")
        self.assertEqual(set(cases), {"h1-research", "h2-notes"})
        return {step: case for step, (_, case) in cases.items()}

    def test_tilde_path_into_the_repo_committed_at_the_parent_is_inside(self):  # covers: BENCH-8
        research = self.build()["h1-research"]
        self.assertTrue(research["replayable"], research["reasons"])
        self.assertFalse([p for p in research["outside_files"] if "research.md" in p],
                         f"~/homeproj/docs/research.md is in the repository at the parent: {research['outside_files']}")

    def test_wildcard_path_with_no_match_is_not_replayable(self):  # covers: BENCH-8
        notes = self.build()["h2-notes"]
        self.assertFalse(notes["replayable"], "no file matches ~/other-*/notes.md under this HOME")
        self.assertTrue(any("notes.md" in p for p in notes["outside_files"]),
                        f"the wildcard path is listed: {notes['outside_files']}")
        self.assertTrue(any("notes.md" in r for r in notes["reasons"]), notes["reasons"])

    def test_wildcard_path_with_a_match_is_replayable(self):  # covers: BENCH-8
        folder = os.path.join(self.home, "other-project")
        os.makedirs(folder)
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(folder, "notes.md"), "w", encoding="utf-8") as f:
            f.write("# Notes\nThe wording.\n")
        notes = self.build()["h2-notes"]
        self.assertTrue(notes["replayable"], notes["reasons"])
        self.assertTrue(any("notes.md" in p for p in notes["outside_files"]),
                        f"the wildcard path is still listed: {notes['outside_files']}")


N_STEPS = 20
ORPHANS = ["x00", "x01", "x02"]


def build_many(parent):
    """Twenty replayable steps; step i's finishing commit changes 8 * (i + 1) lines, so the smallest are clear."""
    r = Repo(parent)
    plan = "# Sample: plan\n\n## M1 — Many\n" + "".join(f"- [x] Step {i} (S{i}-1)\n" for i in range(N_STEPS))
    plan += "".join(f"- [ ] Later {x} ({x.upper()}-1)\n" for x in ORPHANS)
    r.commit(0, "setup", {".gitignore": GITIGNORE, ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n",
                          ".regista/plan.md": plan, "src/app.py": "VALUE = 1\n"})
    for i in range(N_STEPS):
        step = f"s{i:02d}"
        r.commit(1 + i, f"{step}: spec and source", {
            f".regista/specs/{step}.json": spec(step, "M1", f"Step {i}", f"Step {i} (S{i}-1)", f"f{i}() in src.",
                                                [(f"S{i}-1", f"f{i}() SHALL work", "test")]),
            f"src/stub_{i}.py": "X = 0\n"})
        n = 4 * (i + 1)
        r.commit(1 + i, f"{step}: done", {
            f"src/mod_{i}.py": "".join(f"V{k} = {k}\n" for k in range(n)),
            f"tests/unit/test_s{i:02d}.py": test_file(f"s{i:02d}", f"S{i}-1", lines=max(1, n // 2))})
    # Not replayable (no covering test): they must not count towards the holdout share or screening.
    r.commit(N_STEPS + 1, "later specs", {
        f".regista/specs/{x}.json": spec(x, "M1", f"Later {x}", f"Later {x} ({x.upper()}-1)", "Not built yet.",
                                         [(f"{x.upper()}-1", f"{x} SHALL exist", "test")]) for x in ORPHANS})
    return r


class BenchSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-bench-split-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.repo = build_many(os.path.join(cls.tmp, "projects"))
        cls.homes = [os.path.join(cls.tmp, "evolve1"), os.path.join(cls.tmp, "evolve2")]
        for evo in cls.homes:
            os.makedirs(evo)
            with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
                json.dump({"holdout_pct": 30, "screen_cases": 3}, f)
        cls.runs = [run_build(cls.repo.root, cls.home, cls.homes[0])]
        cls.first = cls.splits(cls.homes[0]) if cls.runs[0].returncode == 0 else None
        cls.runs += [run_build(cls.repo.root, cls.home, cls.homes[0]), run_build(cls.repo.root, cls.home, cls.homes[1])]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for r in self.runs:
            self.assertEqual(r.returncode, 0, f"bench build failed:\n{r.stdout}\n{r.stderr}")

    @staticmethod
    def splits(evo):
        return {step: (c["split"], c["screening"]) for step, (_, c) in load_cases(evo).items()}

    def test_split_and_screening_same_on_every_build(self):  # covers: BENCH-9
        self.assertEqual(len(self.first), N_STEPS + len(ORPHANS))
        self.assertEqual(self.first, self.splits(self.homes[0]), "rebuilding in the same folder keeps the split")
        self.assertEqual(self.first, self.splits(self.homes[1]), "a build in a fresh folder gives the same split")

    def test_holdout_share_within_one_case_of_holdout_pct(self):  # covers: BENCH-9
        cases = load_cases(self.homes[0])
        holdout = sum(1 for _, c in cases.values() if c["split"] == "holdout")
        self.assertLessEqual(abs(holdout - N_STEPS * 30 / 100), 1,
                             f"{holdout} of {N_STEPS} replayable cases held out at 30%")
        for x in ORPHANS:
            self.assertIs(cases[x][1]["replayable"], False, x)
            self.assertIsNone(cases[x][1]["split"], x)

    def test_screening_without_review_history_falls_back_to_fewest_changed_lines(self):  # covers: BENCH-9
        # No step here has review rounds or failing gate runs, so the hardest-first ranking (BENCH-13, unit tests
        # in test_screening_hard.py) comes down to its tie-break: the fewest changed lines.
        cases = load_cases(self.homes[0])
        for step, (_, c) in cases.items():
            self.assertIs(c["replayable"], step not in ORPHANS, f"{step}: {c['reasons']}")
            if c["replayable"]:
                self.assertFalse(c["baseline"]["review_rounds"], step)
                self.assertFalse(c["baseline"]["gate_failures"], step)
        train = sorted(step for step, (_, c) in cases.items() if c["split"] == "train")
        screening = sorted(step for step, (_, c) in cases.items() if c["screening"])
        self.assertEqual(screening, train[:3], "screen_cases 3: with equal history, the fewest changed lines")

    def test_audit_report_calls_screening_steps_the_hardest(self):  # covers: BENCH-13
        with open(os.path.join(self.homes[0], "reports", "audit-sample.md"), encoding="utf-8") as f:
            report = f.read()
        paragraphs = [p for p in re.split(r"\n\s*\n", report) if re.search(r"screening step", p, re.I)]
        self.assertTrue(paragraphs, f"the audit report explains the screening steps:\n{report}")
        text = " ".join(paragraphs)
        self.assertFalse(re.search(r"(?i)smallest", report), f"screening steps are no longer the smallest: {text}")
        self.assertRegex(text, r"(?i)review rounds?", text)
        self.assertRegex(text, r"(?i)fail\w* gate runs?|gate runs? (that )?fail", text)
        self.assertRegex(text, r"(?i)run first", text)


if __name__ == "__main__":
    unittest.main()
