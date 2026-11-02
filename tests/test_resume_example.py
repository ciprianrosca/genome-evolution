"""The resume-tailoring example and the suite's 'home' setting (spec .regista/specs/resume-example.json: EX-1, EX-2,
EX-6; EX-3 is the end-to-end test in genome/tests/e2e/test_resume_example.py).

EX-1: genome/examples/resume-tailoring/checks/trace_claims.py runs the way the example's suite runs it: the test takes
the command of the suite's command check that names trace_claims.py, puts the absolute path of a copy of the
example's checks/ folder in place of {checks}, and runs it through the shell with the workspace as its working folder
and PYTHONSAFEPATH=1. Each workspace is made by hand in a temporary folder, laid out the way the suite lays out its
fixtures: home/personal-assistants/artemis/ holds the fact bank (profile/master_cv.md) and the application folders
(applications/<date>_<company>_<role>/resume.md and maybe cover.md). Everything describes a made-up person, Dana
Ilescu, and made-up employers. Experience headings are written '### <title> | <employer> | <start> – <end>' in both
the fact bank and the resume.

EX-2: no file under genome/examples/ or genome/tests/ names the real person, his employers or his email address (this
file and test_genome_readme.py, which list them in order to look for them, are the only exceptions); the example's
suite passes `evolve.py suite check` with its profile and a made-up copy of the skill as the source; the suite and
profile have the shape the spec asks for.

EX-6: a suite naming a 'home' folder: the template runner and the stand-in claude (genome/tests/test_suite_runs.py's,
with a small wrapper that also records the values of HOME and CLAUDE_CONFIG_DIR and the files under HOME) see HOME
set to the folder inside the workspace with the fixtures under it; the default runner keeps the real Claude
configuration folder through CLAUDE_CONFIG_DIR, and its deny rules and sandbox denyRead keep naming the original
home's credential and Claude Code paths (read the way Claude Code reads them, with ~ meaning the fake home). load_suite
refuses a home with a leading / or a '..' part, `suite check` shows the home, and the README's key list names it.

Run: cd genome/tests && env -u CLAUDE_PROJECT_DIR python3 -m unittest test_resume_example -v"""
import ast
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import test_genome_readme as readme_tests  # noqa: E402
import test_suite_runs as runs  # noqa: E402

GENOME = os.path.dirname(_HERE)
EXAMPLES = os.path.join(GENOME, "examples")
EXAMPLE = os.path.join(EXAMPLES, "resume-tailoring")
SUITE = os.path.join(EXAMPLE, "suite.json")
PROFILE = os.path.join(EXAMPLE, "profile.json")
TRACE = os.path.join(EXAMPLE, "checks", "trace_claims.py")
ARTEMIS = "home/personal-assistants/artemis"
FACT_BANK = f"{ARTEMIS}/profile/master_cv.md"
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")

# ---------------------------------------------------------------------------------------------------------------
# EX-1: a made-up fact bank and resumes built from it.

BANK = """# Dana Ilescu: fact bank

dana.ilescu@example.com, Cluj

## Experience

### Head of Engineering | Northwind Ledger | 2022 – 2025
- Led 12+ engineers across three teams.
- Cut release time by 40% with a new delivery pipeline.
- Moved 250k customer accounts to the new ledger.

### Engineering Manager | Copperline Games | 2019 – 2022
- Raised crash-free sessions to 99.5% for the main title.
- Hired 7 engineers in one year.

## Skills
Payments, distributed systems, hiring and coaching.
"""

SUMMARY = "Engineering leader who helped the teams at Northwind Ledger and Copperline Games ship faster."
HEAD_NORTHWIND = "### Head of Engineering | Northwind Ledger | 2022 – 2025"
HEAD_COPPERLINE = "### Engineering Manager | Copperline Games | 2019 – 2022"
LINE_RELEASE = "- Cut release time by 40%."


def resume(summary=SUMMARY, northwind=HEAD_NORTHWIND, release=LINE_RELEASE, extra=""):
    """A resume whose every claim traces to BANK, with one part swapped out when a test asks."""
    return (f"# Dana Ilescu\n\ndana.ilescu@example.com, Cluj\n\n## Summary\n{summary}\n\n## Experience\n\n"
            f"{northwind}\n- Led 12+ engineers across three teams.\n{release}\n"
            "- Moved 250k customer accounts to the new ledger.\n\n"
            f"{HEAD_COPPERLINE}\n- Raised crash-free sessions to 99.5%.\n- Hired 7 engineers in one year.\n{extra}")


GOOD_COVER = ("Dear hiring team,\n\nI would like to lead the payments engineers at your company. The work at "
              "Northwind Ledger taught me to ship them small, safe changes.\n\nDana Ilescu\n")

FOREIGN_NUMBERS = ["58%", "17+", "730k", "6.8", "31"]
MARKERS = ["[GAP]", "[VERIFY]", "[CONF]", "{{", "**"]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def trace_command():
    """The command of the example suite's command check that runs trace_claims.py, and its expected exit code."""
    if not os.path.isfile(SUITE):
        raise AssertionError(f"the example's suite {SUITE} doesn't exist")
    for case in read_json(SUITE).get("cases") or []:
        for check in case.get("checks") or []:
            run = check.get("run")
            if isinstance(run, str) and "trace_claims.py" in run:
                return run, check.get("exit", 0)
    raise AssertionError("no command check in the example's suite runs {checks}/trace_claims.py")


class TraceClaims(unittest.TestCase):
    """EX-1: trace_claims.py, run the way the suite's command check runs it."""

    def setUp(self):
        self.assertTrue(os.path.isfile(TRACE), f"{TRACE} doesn't exist")
        self.command, self.pass_exit = trace_command()
        self.tmp = tempfile.mkdtemp(prefix="genome-trace-claims-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.checks = os.path.join(self.tmp, "run", "checks")
        shutil.copytree(os.path.join(EXAMPLE, "checks"), self.checks)
        self.count = 0
        for token in FOREIGN_NUMBERS:  # the made-up foreign numbers really are foreign
            self.assertNotIn(token.rstrip("%+k"), BANK, f"{token} must not be in the made-up fact bank")

    def workspace(self, resumes=None, cover=None, bank=BANK):
        """A fresh workspace; resumes maps an application folder name to the resume's text (None: no resume.md).
        Folders are made in the order given, each one newer than the one before (by name and by time)."""
        self.count += 1
        ws = os.path.join(self.tmp, f"ws-{self.count}")
        artemis = os.path.join(ws, *ARTEMIS.split("/"))
        write(os.path.join(artemis, "profile", "master_cv.md"), bank)
        write(os.path.join(artemis, "cvs", "README.md"), "The approved CVs, newest last.\n")
        write(os.path.join(artemis, "cvs", "2025-06-01_dana_ilescu.md"), resume())
        stamp = 1_700_000_000
        for i, (folder, text) in enumerate((resumes or {}).items()):
            app = os.path.join(artemis, "applications", folder)
            os.makedirs(app, exist_ok=True)
            write(os.path.join(app, "fit.md"), "Fit notes for a made-up role.\n")
            if text is not None:
                write(os.path.join(app, "resume.md"), text)
            if cover is not None and i == len(resumes) - 1:
                write(os.path.join(app, "cover.md"), cover)
            for name in os.listdir(app):
                os.utime(os.path.join(app, name), (stamp + i * 86400, stamp + i * 86400))
            os.utime(app, (stamp + i * 86400, stamp + i * 86400))
        return ws

    def one(self, text, cover=None):
        return self.workspace({"2026-10-15_bluewater-pay_head-of-engineering": text}, cover=cover)

    def run_trace(self, ws):
        cmd = self.command.replace("{checks}", shlex.quote(self.checks))
        env = {k: v for k, v in os.environ.items() if not k.endswith("_PROJECT_DIR")}
        env.update(PYTHONSAFEPATH="1", PYTHONDONTWRITEBYTECODE="1", HOME=os.path.join(ws, "home"))
        r = subprocess.run(cmd, shell=True, cwd=ws, env=env, capture_output=True, text=True, encoding="utf-8",
                           stdin=subprocess.DEVNULL, timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotIn("Traceback", out, f"trace_claims.py crashed:\n{out}")
        return r.returncode, out

    def assert_passes(self, ws):
        code, out = self.run_trace(ws)
        self.assertEqual(code, self.pass_exit, f"every claim traces, so the check passes:\n{out}")

    def assert_fails_naming(self, ws, *names):
        code, out = self.run_trace(ws)
        self.assertEqual(code, 1, f"the check should fail with exit 1:\n{out}")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines, "a failure prints at least one plain line")
        for name in names:
            pattern = name if isinstance(name, re.Pattern) else re.compile(re.escape(name), re.I)
            self.assertTrue([line for line in lines if pattern.search(line)],
                            f"no line names {pattern.pattern!r}:\n{out}")
        return lines

    def test_a_resume_whose_every_claim_traces_passes(self):  # covers: EX-1
        self.assert_passes(self.one(resume()))
        with self.subTest("with a clean cover letter, and words like 'the', 'them' and 'helped'"):
            self.assert_passes(self.one(resume(), cover=GOOD_COVER))

    def test_a_number_not_in_the_fact_bank_fails(self):  # covers: EX-1
        for token in FOREIGN_NUMBERS:
            with self.subTest(token):
                ws = self.one(resume(release=f"- Cut release time by {token} this year."))
                self.assert_fails_naming(ws, token.rstrip("%+k"))

    def test_an_employer_not_in_the_fact_bank_fails(self):  # covers: EX-1
        ws = self.one(resume(northwind="### Head of Engineering | Bluefjord Analytics | 2022 – 2025"))
        self.assert_fails_naming(ws, "Bluefjord Analytics")

    def test_a_title_not_in_the_fact_bank_fails(self):  # covers: EX-1
        ws = self.one(resume(northwind="### Chief Revenue Officer | Northwind Ledger | 2022 – 2025"))
        self.assert_fails_naming(ws, "Chief Revenue Officer")

    def test_a_date_range_not_in_the_fact_bank_fails(self):  # covers: EX-1
        # Both years are in the fact bank, so only the range itself is foreign.
        ws = self.one(resume(northwind="### Head of Engineering | Northwind Ledger | 2019 – 2025"))
        self.assert_fails_naming(ws, re.compile(r"2019.*2025"))

    def test_each_leftover_marker_fails(self):  # covers: EX-1
        for marker in MARKERS:
            with self.subTest(marker):
                summary = {"{{": "Engineering leader for {{company}} who helped teams ship faster.",
                           "**": "**Engineering leader** who helped teams ship faster."}.get(
                    marker, f"Engineering leader who helped teams ship faster. {marker}")
                self.assert_fails_naming(self.one(resume(summary=summary)), marker)

    def test_third_person_pronouns_fail_in_the_resume_and_the_cover_letter(self):  # covers: EX-1
        in_resume = {"he": "Colleagues say he ships early and often.",
                     "his": "Engineering leader who grew his teams at Northwind Ledger.",
                     "him": "Copperline Games asked him to lead the main title."}
        for word, summary in in_resume.items():
            with self.subTest("resume.md", word=word):
                self.assert_fails_naming(self.one(resume(summary=summary)), re.compile(rf"\b{word}\b", re.I))
        in_cover = {"he": "He would like to lead your payments engineers.\n",
                    "his": "Dear team,\n\nThe work shows in his record at Northwind Ledger.\n",
                    "him": "Dear team,\n\nNorthwind Ledger trusted him with the ledger.\n"}
        for word, cover in in_cover.items():
            with self.subTest("cover.md", word=word):
                self.assert_fails_naming(self.one(resume(), cover=cover), re.compile(rf"\b{word}\b", re.I))

    def test_no_resume_fails(self):  # covers: EX-1
        variants = (("no applications folder", self.workspace()),
                    ("an application folder without resume.md",
                     self.workspace({"2026-10-15_bluewater-pay_head-of-engineering": None})))
        for label, ws in variants:
            with self.subTest(label):
                self.assert_fails_naming(ws, re.compile(r"resume", re.I))

    def test_the_newest_of_two_resumes_is_checked(self):  # covers: EX-1
        bad = resume(release="- Cut release time by 58% this year.")
        old, new = "2026-09-01_old-company_engineering-manager", "2026-10-20_new-company_head-of-engineering"
        with self.subTest("old bad, newest good"):
            self.assert_passes(self.workspace({old: bad, new: resume()}))
        with self.subTest("old good, newest bad"):
            self.assert_fails_naming(self.workspace({old: resume(), new: bad}), "58")

    def test_one_plain_line_per_problem(self):  # covers: EX-1
        ws = self.one(resume(release="- Cut release time by 58% this year.", extra="- Shipped a ledger. [VERIFY]\n"))
        lines = self.assert_fails_naming(ws, "58", "[VERIFY]")
        number = [i for i, line in enumerate(lines) if "58" in line]
        marker = [i for i, line in enumerate(lines) if "[VERIFY]" in line]
        self.assertTrue(set(number) - set(marker) and set(marker) - set(number),
                        "each problem gets its own line:\n" + "\n".join(lines))

    def test_the_check_is_one_standard_library_script(self):  # covers: EX-1
        with open(TRACE, encoding="utf-8") as f:
            tree = ast.parse(f.read(), TRACE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative import of a helper")
                imported.add((node.module or "").split(".")[0])
        outside = sorted(m for m in imported if m not in sys.stdlib_module_names)
        self.assertEqual(outside, [], "trace_claims.py imports only the standard library")


# ---------------------------------------------------------------------------------------------------------------
# EX-2: made-up data only, and the example's suite passes `suite check`.

GUARD_EXEMPT = {os.path.abspath(__file__), os.path.join(_HERE, "test_genome_readme.py")}
FORBIDDEN = [(name, re.compile(pattern, re.I)) for name, pattern in (
    ("the real first name", r"ciprian"), ("an employer (M)", r"mambu"), ("an employer (P)", r"playtika"),
    ("an employer (2C)", r"2checkout"), ("an employer (V)", r"verifone"), ("an employer (1&1)", r"(?<![\w&])1&1(?![\w&])"),
    ("an employer (eM)", r"\bemag\b"), ("an employer (Ma)", r"\bmarsh\b"), ("the email address", r"ciprian@"))]


def workspace_files(case):
    """The workspace-relative paths of every file a case's files mapping copies in."""
    found = []
    for target, source in (case.get("files") or {}).items():
        src = os.path.join(EXAMPLE, *source.split("/"))
        prefix = "" if target in (".", "") else target.rstrip("/") + "/"
        if os.path.isdir(src):
            for d, _dirs, names in os.walk(src):
                for n in names:
                    found.append(prefix + os.path.relpath(os.path.join(d, n), src).replace(os.sep, "/"))
        else:
            found.append(target)
    return found


def glob_regex(pattern):
    """A workspace glob as a regular expression: ** any number of folders, * and ? within one name."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"/?\Z")


def frozen_covers(globs, path):
    return any(glob_regex(g.rstrip("/")).match(path) or glob_regex(g.rstrip("/") + "/**").match(path) for g in globs)


def check_kind(check):
    if "run" in check:
        return "command"
    if "judge" in check or "rubric_file" in check:
        return "judge"
    return "file"


class MadeUpDataOnly(unittest.TestCase):
    """EX-2: no real names in the example or the tests, and the example's suite is valid with its profile."""

    def test_no_file_names_the_real_person_his_employers_or_his_email(self):  # covers: EX-2
        problems = []
        for top in (EXAMPLES, _HERE):
            for d, dirs, names in os.walk(top):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                for n in names:
                    path = os.path.join(d, n)
                    if os.path.abspath(path) in GUARD_EXEMPT or n.endswith(".pyc"):
                        continue
                    with open(path, "rb") as f:
                        text = f.read().decode("utf-8", "replace")
                    for label, pattern in FORBIDDEN:
                        m = pattern.search(text)
                        if m:
                            line = text.count("\n", 0, m.start()) + 1
                            problems.append(f"{os.path.relpath(path, GENOME)}:{line} names {label}")
        self.assertEqual(problems, [], "only made-up data may appear under genome/examples/ and genome/tests/")

    def test_the_example_suite_passes_suite_check_with_its_profile(self):  # covers: EX-2
        for path in (SUITE, PROFILE):
            self.assertTrue(os.path.isfile(path), f"{path} doesn't exist")
        tmp = tempfile.mkdtemp(prefix="genome-resume-example-")
        self.addCleanup(shutil.rmtree, tmp, True)
        skill = os.path.join(tmp, "made-up-skill")
        write(os.path.join(skill, "SKILL.md"),
              "---\nname: resume-tailoring\ndescription: Tailors a made-up resume to a job.\n---\n\n"
              "# Resume tailoring\n\nEvery claim on a resume traces to a line in the fact bank.\n")
        home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve-home")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items() if not k.endswith("_PROJECT_DIR")}
        env.update(HOME=home, REGISTA_EVOLVE_HOME=evo, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, EVOLVE, "suite", "check", SUITE, "--profile", PROFILE, "--source", skill],
                           cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8",
                           stdin=subprocess.DEVNULL, timeout=120)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)

    def test_the_example_suite_and_profile_have_the_shape_the_spec_asks_for(self):  # covers: EX-2
        for rel in ("README.md", "profile.json", "suite.json", "checks/trace_claims.py", "rubrics/summary.md"):
            self.assertTrue(os.path.isfile(os.path.join(EXAMPLE, *rel.split("/"))), f"the example has no {rel}")
        profile = read_json(PROFILE)
        self.assertEqual(profile.get("name"), "resume-tailoring")
        self.assertEqual(profile.get("evolvable"), ["SKILL.md"])
        self.assertIn("**/*.py", profile.get("never") or [])
        self.assertTrue(isinstance(profile.get("describe"), str) and profile["describe"].strip())
        self.assertFalse(profile.get("traits"), "the profile has no traits")
        self.assertFalse(profile.get("judges"), "the judge lives in the suite, not the profile")
        self.assertIn('"max_growth_pct": 30', json.dumps(profile), "a 30% size cap on SKILL.md")

        suite = read_json(SUITE)
        self.assertEqual(suite.get("name"), "resume-tailoring")
        self.assertEqual(suite.get("install"), ".claude/skills/resume-tailoring")
        self.assertEqual(suite.get("home"), "home")
        self.assertEqual(suite.get("model"), "sonnet")
        cases = suite.get("cases") or []
        self.assertIn(len(cases), (4, 5), "four or five cases")
        self.assertTrue([c for c in cases if c.get("split") == "holdout"], "at least one case held out by its own split")
        judged = []
        for case in cases:
            cid = case.get("id")
            checks = case.get("checks") or []
            with self.subTest(case=cid):
                prompt = case.get("prompt")
                if prompt is None:
                    with open(os.path.join(EXAMPLE, *case["prompt_file"].split("/")), encoding="utf-8") as f:
                        prompt = f.read()
                self.assertIn("tailor", prompt.lower(), "the case asks for a tailored resume")
                runs_trace = [c for c in checks if check_kind(c) == "command" and "{checks}/trace_claims.py" in c["run"]]
                self.assertTrue(runs_trace, "every case runs {checks}/trace_claims.py")
                files = workspace_files(case)
                self.assertIn(FACT_BANK, files, "the fact bank is placed under the home folder")
                cvs = [p for p in files if p.startswith(f"{ARTEMIS}/cvs/") and not p.endswith("/README.md")]
                self.assertTrue(cvs, "an approved CV is placed in cvs/")
                self.assertIn(f"{ARTEMIS}/cvs/README.md", files)
                frozen = case.get("frozen") or []
                for path in [FACT_BANK, *cvs]:
                    self.assertTrue(frozen_covers(frozen, path), f"no frozen glob covers {path}: {frozen}")
                marker_checks = [c for c in checks if check_kind(c) == "file"
                                 and "GAP" in json.dumps([c.get("not_contains"), c.get("not_matches")])]
                self.assertTrue(marker_checks, "a file check looks for the leftover markers")
                judges = [c for c in checks if check_kind(c) == "judge"]
                if judges:
                    judged.append(cid)
                for judge in judges:
                    self.assertTrue(isinstance(judge.get("model"), str) and judge["model"].strip(), "a fixed model")
                    self.assertTrue(str(judge.get("rubric_file", "")).startswith("rubrics/"), "a rubric under rubrics/")
        self.assertLessEqual(len(judged), 1, f"a judge in one case at most: {judged}")


# ---------------------------------------------------------------------------------------------------------------
# EX-6: the suite's 'home' setting.

HOME_DIR = "made-up-home"
HOME_FIXTURE = "personal-assistants/artemis/profile/master_cv.md"

# Wraps the stand-ins' record() so each call also logs HOME, CLAUDE_CONFIG_DIR and the files under HOME.
WRAP = r'''
_record = record
def record(name, extra):
    extra = dict(extra)
    home = os.environ.get("HOME")
    extra["env_values"] = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR")}
    extra["home_files"] = files(home) if home and os.path.isdir(home) else None
    _record(name, extra)
'''


def wrapped(script):
    head, body = script.split(runs.COMMON, 1)
    return head + runs.COMMON + WRAP + body


class HomeSetting(runs.RunBase):
    """EX-6: HOME inside the workspace, the real Claude configuration folder and the real home's deny rules."""

    def setUp(self):
        super().setUp()
        for path, text in ((self.runner, runs.RUNNER), (self.claude, runs.CLAUDE)):
            text = (wrapped(text).replace("@PYTHON@", runs.PY).replace("@CONTROL@", repr(self.control))
                    .replace("@MARKER@", runs.MARKER).replace("@INSTALL@", runs.INSTALL))
            runs.write(path, text, 0o755)

    def home_suite(self, runner=None, home=HOME_DIR):
        data = {"name": "toy-home", "install": runs.INSTALL, "model": "toy-model-7",
                "runner": runner if runner is not None else self.template(),
                "cases": [
                    {"id": "edit-case", "split": "train", "prompt": runs.PROMPT,
                     "files": {f"{home}/{HOME_FIXTURE}": "fixtures/input.txt", "data/frozen.txt": "fixtures/frozen.txt"},
                     "frozen": [f"{home}/{HOME_FIXTURE}"], "checks": [{"run": "sh {checks}/verify.sh"}]},
                    {"id": "file-prompt", "split": "holdout", "prompt_file": "prompts/farewell.txt",
                     "checks": [{"file": "farewell.txt", "exists": True}]}]}
        if home is not None:
            data["home"] = home
        path = os.path.join(self.root, "toy-home.json")
        runs.write(path, json.dumps(data, indent=2) + "\n")
        return self.suite_mod.load_suite(path)

    def clear_log(self, name):
        path = os.path.join(self.control, name)
        if os.path.exists(path):
            os.remove(path)

    def assert_home_is_the_workspace_folder(self, call):
        home = call["env_values"]["HOME"]
        self.assertTrue(home and os.path.isabs(home), f"HOME is an absolute path: {home!r}")
        self.assertEqual(os.path.realpath(home), os.path.realpath(os.path.join(call["cwd"], HOME_DIR)),
                         "HOME is the suite's home folder inside the workspace")
        return home

    def test_load_suite_keeps_the_home_and_refuses_one_that_leaves_the_workspace(self):  # covers: EX-6
        self.assertEqual(self.home_suite().get("home"), HOME_DIR)
        self.assertIsNone(self.home_suite(home=None).get("home"), "no home unless the suite names one")
        for bad in ("/abs/home", "../outside", "home/../../outside"):
            with self.subTest(home=bad):
                path = os.path.join(self.root, "bad-home.json")
                data = {"name": "toy-home", "install": runs.INSTALL, "home": bad, "cases": [
                    {"id": "a", "split": "train", "prompt": runs.PROMPT, "checks": [{"file": "x.txt", "exists": True}]},
                    {"id": "b", "split": "holdout", "prompt": runs.PROMPT, "checks": [{"file": "x.txt", "exists": True}]}]}
                runs.write(path, json.dumps(data) + "\n")
                with self.assertRaises(self.suite_mod.SuiteError) as caught:
                    self.suite_mod.load_suite(path)
                self.assertIn("home", str(caught.exception), "the message names the home setting")

    def test_suite_check_shows_the_home(self):  # covers: EX-6
        suite = self.home_suite()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(["suite", "check", suite["path"]])
            except SystemExit as e:
                code = e.code
        self.assertIn(code, (0, None), buf.getvalue())
        self.assertIn(HOME_DIR, buf.getvalue(), "suite check's summary shows the home folder")

    def test_template_runner_runs_with_home_inside_the_workspace(self):  # covers: EX-6
        self.run_case(self.home_suite())
        call = self.one_call()
        home = self.assert_home_is_the_workspace_folder(call)
        self.assertEqual((call["home_files"] or {}).get(HOME_FIXTURE), runs.INPUT,
                         "the fixture placed under the home folder is there, under HOME")
        self.clear_log("runner.jsonl")
        folder = os.path.join(self.tmp, "runs", "file-prompt", "individual")
        self.run_case(self.home_suite(), case="file-prompt", folder=folder)
        call = self.one_call()
        self.assert_home_is_the_workspace_folder(call)
        self.assertIsNotNone(call["home_files"], "the home folder is made even when no fixture goes in it")
        self.assertNotEqual(os.path.realpath(home), os.path.realpath(self.home))
        with self.subTest("without a home, HOME is left alone"):
            self.clear_log("runner.jsonl")
            self.run_case(self.home_suite(home=None), folder=os.path.join(self.tmp, "runs", "no-home"))
            self.assertEqual(self.one_call()["env_values"]["HOME"], self.home)

    def test_default_runner_keeps_the_real_configuration_folder_and_deny_rules(self):  # covers: EX-6
        config_dir = os.path.join(self.tmp, "real-claude-config")
        variants = (("without CLAUDE_CONFIG_DIR", None, os.path.join(self.home, ".claude")),
                    ("with CLAUDE_CONFIG_DIR", config_dir, config_dir))
        for label, given, expected in variants:
            with self.subTest(label):
                self.clear_log("claude.jsonl")
                env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
                if given:
                    env["CLAUDE_CONFIG_DIR"] = given
                folder = os.path.join(self.tmp, "runs", re.sub(r"\W+", "-", label))
                with mock.patch.dict(os.environ, env, clear=True):
                    result = self.run_case(self.home_suite(runner="claude"), folder=folder)
                self.assertEqual(result.get("outcome"), "done", result)
                call = self.one_call("claude.jsonl")
                fake = self.assert_home_is_the_workspace_folder(call)
                self.assertIn(HOME_FIXTURE, call["home_files"] or {}, "the fixture is under HOME")
                self.assertEqual(call["env_values"]["CLAUDE_CONFIG_DIR"], expected,
                                 "Claude Code keeps the real configuration folder (and its login)")
                rules, deny_read = runs.deny_rules(call), runs.deny_read_paths(call, fake)
                targets = [os.path.join(self.home, *rel.split("/"))
                           for rel in (".claude/projects", ".ssh", ".aws", ".config/gh", ".gnupg", ".netrc")]
                targets += [os.path.join(expected, "file-history"), os.path.join(expected, ".credentials.json")]
                for target in targets:
                    for tool in ("Read", "Glob", "Grep"):
                        self.assertTrue([r for r in rules if runs.rule_covers(r, tool, target, fake)],
                                        f"no {tool} deny rule covers the real {target} (~ is the fake home): {rules}")
                    self.assertTrue([p for p in deny_read if runs.inside(target, p)],
                                    f"the sandbox's denyRead doesn't cover the real {target}: {deny_read}")
                fixture = os.path.join(fake, *HOME_FIXTURE.split("/"))
                self.assertFalse([r for r in rules if runs.rule_covers(r, "Read", fixture, fake)],
                                 f"the agent can still read the fixtures under its home: {rules}")
                self.assertFalse([p for p in deny_read if runs.inside(fixture, p)],
                                 f"the sandbox still lets the agent read its home's fixtures: {deny_read}")

    def test_readme_key_table_names_the_home(self):  # covers: EX-6
        section = readme_tests.find_section(readme_tests.read_readme(), readme_tests.SECTION_TITLE)
        self.assertIsNotNone(section, "the README has its 'Evolving any skill or agent' section")
        self.assertTrue(re.search(r"`home`|\|\s*home\s*\||\*\*home\*\*", section),
                        "the README's table of suite keys doesn't name 'home'")


if __name__ == "__main__":
    unittest.main()
