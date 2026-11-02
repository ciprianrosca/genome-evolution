"""Suite files: format, validation and the stable split (spec .regista/specs/suite-files.json: SUI-1, SUI-2; SUI-12 is
the end-to-end test in genome/tests/e2e/test_suite_check.py).

scripts/suite.py loads a suite file (JSON) with suite.load_suite(path, profile=None, source=None) and raises
suite.SuiteError, a subclass of evolve_core.EvolveError, with a plain message naming the suite file and the case and
check for each problem. `evolve.py suite check <suite> [--profile <profile>] [--source <folder>]` prints the suite or,
on a problem, one plain line `evolve.py: <message>` with exit code 1.

Everything here is made up and lives in temporary folders: a small suite folder (prompts, a rubric, fixtures and a
check script), a file just outside it (to try paths that climb out), and for the profile check a toy target folder
with one evolvable SKILL.md, an evolvable folder evals/mutable/ and a frozen settings.json, described by a small
profile with no traits. evolve.main runs in-process with HOME and REGISTA_EVOLVE_HOME pointing at temporary paths.

Interface assumed (from the spec):
- load_suite returns a dict with 'path', 'root', the top-level keys with their defaults, 'cases' (each check with
  'kind' and 'id') and 'split' {'train', 'holdout', 'screening'}: lists of case ids.
- The stable order sorts cases by the hexadecimal sha256 of '<suite name>:<case id>' (the same order as the raw
  digest). round() is used on n * holdout_pct / 100; the tests avoid halves, where Python's round and rounding half up
  differ.
- A refusal's message names the suite file (its file name appears), the case id and the check id when they are known,
  and, for an unknown top-level or case key, the key itself.
- genome.load_profile(path, source=...) accepts the toy profile.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_suite_files -v"""
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
SUITE_NAME = "toy-suite"
SUITE_FILE = "toy-suite.json"


def import_scripts(name):
    """Import scripts/<name>.py as a module, writing no bytecode."""
    if not os.path.isfile(os.path.join(SCRIPTS, f"{name}.py")):
        raise AssertionError(f"scripts/{name}.py doesn't exist")
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = old


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_json(path, data):
    write(path, json.dumps(data, indent=2) + "\n")


def stable_order(name, ids):
    return sorted(ids, key=lambda i: hashlib.sha256(f"{name}:{i}".encode("utf-8")).hexdigest())


def expected_split(name, ids, pct=40, own=None):
    """The spec's split rule, written independently: (train, holdout), each in the stable order."""
    own = own or {}
    order = stable_order(name, ids)
    free = [i for i in order if i not in own]
    k = round(len(free) * pct / 100)
    if free and k == 0 and "holdout" not in own.values():
        k = 1
    held = set(free[:k]) | {i for i, s in own.items() if s == "holdout"}
    return [i for i in order if i not in held], [i for i in order if i in held]


def simple_case(cid, split=None):
    case = {"id": cid, "prompt": f"Write answer.txt for {cid}.", "checks": [{"file": "answer.txt", "exists": True}]}
    if split:
        case["split"] = split
    return case


def base_suite():
    """A valid suite with two cases whose own splits make the split valid whatever the rest does."""
    return {
        "name": SUITE_NAME,
        "install": ".claude/skills/toy",
        "cases": [
            {"id": "alpha", "split": "train", "prompt": "Write answer.txt saying hello.",
             "checks": [{"id": "ran-ok", "run": "test -f answer.txt"}]},
            {"id": "beta", "split": "holdout", "prompt": "Write answer.txt saying goodbye.",
             "checks": [{"id": "has-bye", "file": "answer.txt", "contains": "goodbye"}]},
        ],
    }


class SuiteBase(unittest.TestCase):
    def setUp(self):
        self.suite_mod = import_scripts("suite")
        self.ev = import_scripts("evolve")
        self.tmp = tempfile.mkdtemp(prefix="regista-suite-files-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home_dir = os.path.join(self.tmp, "home")
        os.makedirs(self.home_dir)
        self.evo = os.path.join(self.tmp, "evolve-home-never-made")
        env = mock.patch.dict(os.environ, {"HOME": self.home_dir, "REGISTA_EVOLVE_HOME": self.evo})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.root = os.path.join(self.tmp, "suite")
        write(os.path.join(self.root, "prompts", "ask.txt"), "Write answer.txt with a short, polite greeting.\n")
        write(os.path.join(self.root, "rubrics", "tone.txt"), "Pass if the answer is polite and short.\n")
        write(os.path.join(self.root, "fixtures", "in.txt"), "Input for the toy task.\n")
        write(os.path.join(self.root, "fixtures", "base", "notes.txt"), "Some made-up notes.\n")
        write(os.path.join(self.root, "checks", "verify.sh"), "#!/bin/sh\ntest -s answer.txt\n")
        write(os.path.join(self.tmp, "outside.txt"), "A file just outside the suite folder.\n")

    def save(self, data, folder=None, filename=SUITE_FILE):
        path = os.path.join(folder or self.root, filename)
        write_json(path, data)
        return path

    def load(self, data, **kw):
        return self.suite_mod.load_suite(self.save(data), **kw)

    def run_cli(self, argv):
        """evolve.main(argv) in-process; returns (exit code, printed text)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(argv)
            except SystemExit as e:
                code = e.code
        return (0 if code is None else code), buf.getvalue()

    def assert_refused(self, path, names=(), extra_cli=(), **kw):
        """load_suite raises a plain SuiteError naming the file and `names`; `suite check` exits 1 with one line."""
        with self.assertRaises(self.suite_mod.SuiteError) as caught:
            self.suite_mod.load_suite(path, **kw)
        message = str(caught.exception)
        self.assertTrue(message.strip(), "the message isn't empty")
        self.assertNotIn("\n", message.strip(), f"one plain line: {message!r}")
        self.assertNotIn("Traceback", message)
        self.assertIn(os.path.basename(path), message, f"the message names the suite file: {message!r}")
        for name in names:
            self.assertIn(name, message, f"the message names {name!r}: {message!r}")
        code, out = self.run_cli(["suite", "check", path, *extra_cli])
        self.assertEqual(code, 1, f"suite check exits 1 on a refused suite:\n{out}")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"one plain line:\n{out}")
        self.assertTrue(lines[0].startswith("evolve.py: "), f"the line starts with 'evolve.py: ': {lines[0]!r}")
        self.assertIn(os.path.basename(path), lines[0])
        for name in names:
            self.assertIn(name, lines[0], f"the line names {name!r}")
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(self.evo), "suite check never creates the evolver's working folder")
        return message


# The refusals of points 1) to 3): (label, change to the base suite, names the message must contain).
def _set(key, value):
    def change(s, root, tmp):
        s[key] = value
    return change


def _drop(key):
    def change(s, root, tmp):
        del s[key]
    return change


def _case(index, key, value):
    def change(s, root, tmp):
        s["cases"][index][key] = value(root, tmp) if callable(value) else value
    return change


def _case_drop(index, key):
    def change(s, root, tmp):
        del s["cases"][index][key]
    return change


def _checks(index, checks):
    def change(s, root, tmp):
        s["cases"][index]["checks"] = checks(root, tmp) if callable(checks) else checks
    return change


def _both(*changes):
    def change(s, root, tmp):
        for c in changes:
            c(s, root, tmp)
    return change


JUDGE_OK = {"id": "polite", "judge": "Pass if the answer is polite.", "model": "opus", "files": ["answer.txt"]}


def _judge(**kw):
    check = {k: v for k, v in JUDGE_OK.items() if k not in kw.get("_drop", ())}
    check.update({k: v for k, v in kw.items() if k != "_drop"})
    return [check]


REFUSALS = [
    # Top-level keys.
    ("unknown top-level key", _set("colour", "blue"), ["colour"]),
    ("missing name", _drop("name"), []),
    ("name that isn't a plain name", _set("name", "toy suite!"), []),
    ("missing install", _drop("install"), ["install"]),
    ("absolute install folder", _set("install", "/opt/toy"), ["install"]),
    ("install folder that climbs out", _set("install", "../elsewhere"), ["install"]),
    ("runner template with an unknown placeholder",
     _set("runner", {"command": ["toy-agent", "--in", "{workspace}", "--key", "{secret}"], "tokens": "none"}),
     ["secret"]),
    ("runner with an unknown tokens reader",
     _set("runner", {"command": ["toy-agent", "{workspace}"], "tokens": "plenty"}), []),
    ("runner string other than claude", _set("runner", "other-agent"), []),
    ("timeout_minutes of zero", _set("timeout_minutes", 0), ["timeout_minutes"]),
    ("negative timeout_minutes", _set("timeout_minutes", -5), ["timeout_minutes"]),
    ("holdout_pct of zero", _set("holdout_pct", 0), ["holdout_pct"]),
    ("holdout_pct of 100", _set("holdout_pct", 100), ["holdout_pct"]),
    ("holdout_pct that isn't whole", _set("holdout_pct", 33.3), ["holdout_pct"]),
    ("screen_cases of zero", _set("screen_cases", 0), ["screen_cases"]),
    ("sandbox that isn't a boolean", _set("sandbox", "false"), ["sandbox"]),
    ("missing cases", _drop("cases"), ["cases"]),
    ("empty cases", _set("cases", []), ["cases"]),
    # Cases.
    ("unknown case key", _case(0, "notes", "a made-up note"), ["alpha", "notes"]),
    ("case without checks", _case_drop(0, "checks"), ["alpha"]),
    ("case with an empty checks list", _checks(0, []), ["alpha"]),
    ("case without an id", _case_drop(1, "id"), []),
    ("repeated case id", _case(1, "id", "alpha"), ["alpha"]),
    ("case id that isn't a plain name", _case(0, "id", "al pha!"), []),
    ("case with both prompt and prompt_file", _case(0, "prompt_file", "prompts/ask.txt"), ["alpha"]),
    ("case with neither prompt nor prompt_file", _case_drop(0, "prompt"), ["alpha"]),
    ("absolute prompt_file",
     _both(_case_drop(0, "prompt"), _case(0, "prompt_file", lambda r, t: os.path.join(r, "prompts", "ask.txt"))),
     ["alpha"]),
    ("prompt_file that climbs out of the suite folder",
     _both(_case_drop(0, "prompt"), _case(0, "prompt_file", "../outside.txt")), ["alpha"]),
    ("prompt_file that doesn't exist",
     _both(_case_drop(0, "prompt"), _case(0, "prompt_file", "prompts/missing-prompt.txt")),
     ["alpha", "missing-prompt.txt"]),
    ("case split that isn't train or holdout", _case(0, "split", "test"), ["alpha"]),
    ("absolute fixture workspace path", _case(0, "files", {"/etc/toy.txt": "fixtures/in.txt"}), ["alpha"]),
    ("fixture workspace path that leaves the workspace", _case(0, "files", {"../up.txt": "fixtures/in.txt"}),
     ["alpha"]),
    ("fixture workspace path with a '..' part inside", _case(0, "files", {"data/../../up.txt": "fixtures/in.txt"}),
     ["alpha"]),
    ("fixture source that climbs out of the suite folder", _case(0, "files", {"data/in.txt": "../outside.txt"}),
     ["alpha"]),
    ("absolute fixture source",
     _case(0, "files", lambda r, t: {"data/in.txt": os.path.join(r, "fixtures", "in.txt")}), ["alpha"]),
    ("fixture source that doesn't exist", _case(0, "files", {"data/in.txt": "fixtures/missing-fixture.txt"}),
     ["alpha", "missing-fixture.txt"]),
    ("frozen glob that leaves the workspace", _case(0, "frozen", ["../outside/**"]), ["alpha"]),
    ("absolute frozen glob", _case(0, "frozen", ["/etc/**"]), ["alpha"]),
    # Checks.
    ("check of an unknown kind", _checks(0, [{"id": "odd-one", "shout": "hello"}]), ["alpha", "odd-one"]),
    ("check mixing command and file", _checks(0, [{"id": "mixed-up", "run": "true", "file": "answer.txt",
                                                    "exists": True}]), ["alpha", "mixed-up"]),
    ("check mixing file and judge", _checks(0, [{"id": "mixed-up", "file": "answer.txt", "exists": True,
                                                  "judge": "Is it polite?", "model": "opus",
                                                  "files": ["answer.txt"]}]), ["alpha", "mixed-up"]),
    ("check that isn't an object", _checks(0, ["test -f answer.txt"]), ["alpha"]),
    ("command check with an unknown key", _checks(0, [{"id": "ran-ok", "run": "true", "retries": 2}]),
     ["alpha", "ran-ok"]),
    ("command check exit that isn't whole", _checks(0, [{"id": "ran-ok", "run": "true", "exit": "zero"}]),
     ["alpha", "ran-ok"]),
    ("command check timeout of zero", _checks(0, [{"id": "ran-ok", "run": "true", "timeout": 0}]),
     ["alpha", "ran-ok"]),
    ("command check using {checks} when checks_dir doesn't exist",
     _both(_set("checks_dir", "no-such-checks"), _checks(0, [{"id": "ran-ok", "run": "sh {checks}/verify.sh"}])),
     ["no-such-checks"]),
    ("file check with no assertion", _checks(0, [{"id": "has-file", "file": "answer.txt"}]), ["alpha", "has-file"]),
    ("file check whose matches doesn't compile", _checks(0, [{"id": "has-file", "file": "answer.txt",
                                                               "matches": "(unclosed"}]), ["alpha", "has-file"]),
    ("file check whose not_matches doesn't compile", _checks(0, [{"id": "has-file", "file": "answer.txt",
                                                                   "not_matches": "[unclosed"}]),
     ["alpha", "has-file"]),
    ("file check with an empty contains list", _checks(0, [{"id": "has-file", "file": "answer.txt",
                                                             "contains": []}]), ["alpha", "has-file"]),
    ("file check whose exists isn't a boolean", _checks(0, [{"id": "has-file", "file": "answer.txt",
                                                              "exists": "yes"}]), ["alpha", "has-file"]),
    ("file check on an absolute path", _checks(0, [{"id": "has-file", "file": "/etc/hostname", "exists": True}]),
     ["alpha", "has-file"]),
    ("file check on a path that leaves the workspace",
     _checks(0, [{"id": "has-file", "file": "../answer.txt", "exists": True}]), ["alpha", "has-file"]),
    ("judge with no model", _checks(0, _judge(_drop=("model",))), ["alpha", "polite"]),
    ("judge with no rubric", _checks(0, _judge(_drop=("judge",))), ["alpha", "polite"]),
    ("judge with both a rubric and a rubric_file", _checks(0, _judge(rubric_file="rubrics/tone.txt")),
     ["alpha", "polite"]),
    ("judge with no files", _checks(0, _judge(_drop=("files",))), ["alpha", "polite"]),
    ("judge with an empty files list", _checks(0, _judge(files=[])), ["alpha", "polite"]),
    ("judge reading an absolute path", _checks(0, _judge(files=["/etc/hostname"])), ["alpha", "polite"]),
    ("judge reading a path that leaves the workspace", _checks(0, _judge(files=["../answer.txt"])),
     ["alpha", "polite"]),
    ("judge rubric_file that doesn't exist",
     _checks(0, _judge(_drop=("judge",), rubric_file="rubrics/missing-rubric.txt")),
     ["alpha", "polite", "missing-rubric.txt"]),
    ("judge rubric_file that climbs out of the suite folder",
     _checks(0, _judge(_drop=("judge",), rubric_file="../outside.txt")), ["alpha", "polite"]),
    ("repeated check id in a case", _checks(0, [{"id": "same-id", "run": "true"},
                                                 {"id": "same-id", "file": "answer.txt", "exists": True}]),
     ["alpha", "same-id"]),
    ("check id that isn't a plain name", _checks(0, [{"id": "bad id!", "run": "true"}]), ["alpha"]),
]


class Sui1Refusals(SuiteBase):
    def test_suite_error_is_an_evolve_error(self):  # covers: SUI-1
        core = import_scripts("evolve_core")
        self.assertTrue(issubclass(self.suite_mod.SuiteError, core.EvolveError))

    def test_each_problem_is_refused_with_a_plain_message_naming_file_case_and_check(self):  # covers: SUI-1
        for label, change, names in REFUSALS:
            with self.subTest(label):
                data = base_suite()
                change(data, self.root, self.tmp)
                path = self.save(data)
                self.assert_refused(path, names)

    def test_unreadable_files_are_refused_plainly(self):  # covers: SUI-1
        broken = os.path.join(self.root, "broken-suite.json")
        write(broken, '{"name": "toy-suite", "cases": [\n')
        not_object = os.path.join(self.root, "list-suite.json")
        write_json(not_object, [base_suite()])
        missing = os.path.join(self.root, "missing-suite.json")
        for label, path in (("not JSON", broken), ("not an object", not_object), ("missing file", missing)):
            with self.subTest(label):
                self.assert_refused(path)

    def test_the_base_suite_used_by_the_refusals_is_itself_valid(self):  # covers: SUI-1
        loaded = self.load(base_suite())
        self.assertEqual([c["id"] for c in loaded["cases"]], ["alpha", "beta"])
        code, out = self.run_cli(["suite", "check", os.path.join(self.root, SUITE_FILE)])
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.evo), "suite check never creates the evolver's working folder")

    def test_a_valid_suite_loads_with_every_default_filled_in(self):  # covers: SUI-1
        data = {
            "name": SUITE_NAME,
            "describe": "Checks that the toy helper writes a short, polite answer file.",
            "install": ".",
            "cases": [
                {"id": "alpha", "split": "train", "prompt_file": "prompts/ask.txt",
                 "files": {".": "fixtures/base", "data/in.txt": "fixtures/in.txt"}, "frozen": ["data/**"],
                 "checks": [{"run": "sh {checks}/verify.sh"},
                            {"file": "answer.txt", "contains": ["hello", "there"], "not_contains": "rude",
                             "matches": "^[A-Z]", "not_matches": "!{2,}"},
                            {"rubric_file": "rubrics/tone.txt", "model": "opus", "files": ["answer.txt"]}]},
                {"id": "beta", "split": "holdout", "prompt": "Write answer.txt saying goodbye.",
                 "checks": [{"id": "gone", "file": "answer.txt", "exists": False},
                            {"run": "test -f out.txt", "exit": 1, "timeout": 5}]},
            ],
        }
        path = self.save(data)
        loaded = self.suite_mod.load_suite(path)
        self.assertEqual(os.path.realpath(loaded["path"]), os.path.realpath(path))
        self.assertTrue(os.path.isabs(loaded["path"]))
        self.assertEqual(os.path.realpath(loaded["root"]), os.path.realpath(self.root))
        self.assertEqual(loaded["name"], SUITE_NAME)
        self.assertEqual(loaded["install"], ".")
        self.assertEqual(loaded["runner"], "claude")
        self.assertEqual(loaded["model"], "sonnet")
        self.assertEqual(loaded["timeout_minutes"], 15)
        self.assertEqual(loaded["holdout_pct"], 40)
        self.assertEqual(loaded["checks_dir"], "checks")
        self.assertIs(loaded["sandbox"], True)
        alpha, beta = loaded["cases"]
        self.assertEqual([c["kind"] for c in alpha["checks"]], ["command", "file", "judge"])
        self.assertEqual([c["id"] for c in alpha["checks"]], ["command-1", "file-2", "judge-3"],
                         "a check's default id is <kind>-<n>, n its 1-based place in the case")
        self.assertEqual([c["kind"] for c in beta["checks"]], ["file", "command"])
        self.assertEqual([c["id"] for c in beta["checks"]], ["gone", "command-2"])
        self.assertEqual(alpha["checks"][0].get("exit"), 0, "a command check's exit defaults to 0")
        self.assertEqual(alpha["checks"][0].get("timeout"), 60, "a command check's timeout defaults to 60 seconds")
        self.assertEqual(beta["checks"][1].get("exit"), 1)
        self.assertEqual(loaded["split"]["train"], ["alpha"])
        self.assertEqual(loaded["split"]["holdout"], ["beta"])

    def test_a_runner_template_with_every_known_placeholder_is_accepted(self):  # covers: SUI-1
        data = base_suite()
        data["runner"] = {"command": ["toy-agent", "--workspace", "{workspace}", "--with", "{individual}",
                                      "--prompt-file", "{prompt_file}", "--text", "{prompt}", "--model", "{model}"],
                          "tokens": "claude-json"}
        data["model"] = "haiku"
        data["sandbox"] = False
        loaded = self.load(data)
        self.assertEqual(loaded["runner"]["command"][2], "{workspace}")
        self.assertEqual(loaded["model"], "haiku")
        self.assertIs(loaded["sandbox"], False)


# The profile check ---------------------------------------------------------------------------------------------------

TOY_PROFILE = {"name": "toy-skill", "evolvable": ["SKILL.md", "evals/mutable/**"],
               "version": {"file": "settings.json", "key": "version"}, "traits": {}}


class Sui1ProfileCheck(SuiteBase):
    """A toy target whose evolvable files are SKILL.md and everything under evals/mutable/; the suite lives in
    evals/ (frozen), so only what the tests put under evals/mutable/ is among the files a genome may change."""

    def setUp(self):
        super().setUp()
        self.gn = import_scripts("genome")
        self.source = os.path.join(self.tmp, "toy-skill")
        write(os.path.join(self.source, "SKILL.md"), "---\nname: toy-skill\ndescription: Greets.\n---\n\nGreet.\n")
        write(os.path.join(self.source, "settings.json"), json.dumps({"version": "0.1.0"}) + "\n")
        self.evals = os.path.join(self.source, "evals")
        for folder in ("fixed", "mutable"):
            write(os.path.join(self.evals, folder, "prompt.txt"), "Write answer.txt with a greeting.\n")
            write(os.path.join(self.evals, folder, "rubric.txt"), "Pass if the greeting is polite.\n")
            write(os.path.join(self.evals, folder, "checks", "verify.sh"), "#!/bin/sh\ntest -s answer.txt\n")
        self.profile_path = os.path.join(self.tmp, "profiles", "toy-skill.json")
        write_json(self.profile_path, TOY_PROFILE)
        self.profile = self.gn.load_profile(self.profile_path, source=self.source)

    def suite_for(self, folder):
        return {
            "name": SUITE_NAME, "install": ".claude/skills/toy-skill", "checks_dir": f"{folder}/checks",
            "cases": [
                {"id": "alpha", "split": "train", "prompt_file": f"{folder}/prompt.txt",
                 "checks": [{"id": "script", "run": "sh {checks}/verify.sh"}]},
                {"id": "beta", "split": "holdout", "prompt": "Write answer.txt saying goodbye.",
                 "checks": [{"id": "tone", "rubric_file": f"{folder}/rubric.txt", "model": "opus",
                             "files": ["answer.txt"]}]},
            ],
        }

    def kw(self):
        return {"profile": self.profile, "source": self.source}

    def cli(self):
        return ("--profile", self.profile_path, "--source", self.source)

    def test_a_suite_whose_files_are_all_frozen_passes_the_profile_check(self):  # covers: SUI-1
        path = self.save(self.suite_for("fixed"), folder=self.evals)
        loaded = self.suite_mod.load_suite(path, **self.kw())
        self.assertEqual(loaded["name"], SUITE_NAME)
        code, out = self.run_cli(["suite", "check", path, *self.cli()])
        self.assertEqual(code, 0, out)

    def test_suite_prompt_rubric_or_check_script_among_evolvable_files_is_refused(self):  # covers: SUI-1
        fixed = self.suite_for("fixed")
        prompt = copy.deepcopy(fixed)
        prompt["cases"][0]["prompt_file"] = "mutable/prompt.txt"
        rubric = copy.deepcopy(fixed)
        rubric["cases"][1]["checks"][0]["rubric_file"] = "mutable/rubric.txt"
        script = copy.deepcopy(fixed)
        script["checks_dir"] = "mutable/checks"
        # The suite file alone among the evolvable files: inline prompts and checks, nothing else it points to.
        itself = base_suite()
        cases = [
            ("the suite file itself", itself, os.path.join(self.evals, "mutable"), []),
            ("a prompt_file", prompt, self.evals, ["alpha", "prompt.txt"]),
            ("a rubric_file", rubric, self.evals, ["beta", "tone", "rubric.txt"]),
            ("a check script under checks_dir", script, self.evals, ["verify.sh"]),
        ]
        for label, data, folder, names in cases:
            with self.subTest(label):
                path = self.save(data, folder=folder)
                self.suite_mod.load_suite(path)  # without a profile there is nothing to refuse
                self.assert_refused(path, names, extra_cli=self.cli(), **self.kw())

    def outside_variant(self, name):
        """A suite folder of its own outside the target (genome.load_profile refuses any symlink inside the source),
        holding <name>/prompt.txt, <name>/rubric.txt and <name>/checks/verify.sh; returns (its checks folder, path)."""
        root = os.path.join(self.tmp, f"{name}-suite")
        folder = os.path.join(root, name)
        write(os.path.join(folder, "prompt.txt"), "Write answer.txt with a greeting.\n")
        write(os.path.join(folder, "rubric.txt"), "Pass if the greeting is polite.\n")
        write(os.path.join(folder, "checks", "verify.sh"), "#!/bin/sh\ntest -s answer.txt\n")
        return os.path.join(folder, "checks"), self.save(self.suite_for(name), folder=root)

    def test_a_symlink_under_checks_dir_leading_out_or_into_evolvable_files_is_refused(self):  # covers: SUI-1
        outside = os.path.join(self.tmp, "elsewhere")
        write(os.path.join(outside, "helper.sh"), "#!/bin/sh\ntrue\n")
        mutable_checks = os.path.join(self.evals, "mutable", "checks")
        variants = [
            ("subfolder linked out of the suite folder",
             lambda checks: make_link(outside, os.path.join(checks, "linked"))),
            ("file in a subfolder linked out of the suite folder",
             lambda checks: make_link(os.path.join(outside, "helper.sh"), os.path.join(checks, "sub", "helper.sh"))),
            ("subfolder linked into the evolvable files",
             lambda checks: make_link(mutable_checks, os.path.join(checks, "linked"))),
            ("file in a subfolder linked into the evolvable files",
             lambda checks: make_link(os.path.join(mutable_checks, "verify.sh"),
                                      os.path.join(checks, "sub", "verify.sh"))),
        ]
        for n, (label, plant) in enumerate(variants, 1):
            with self.subTest(label):
                checks, path = self.outside_variant(f"variant{n}")
                plant(checks)
                self.assert_refused(path, extra_cli=self.cli(), **self.kw())

    def test_an_empty_checks_dir_among_the_evolvable_files_is_refused(self):  # covers: SUI-1
        os.makedirs(os.path.join(self.evals, "mutable", "empty-checks"))
        data = self.suite_for("fixed")
        data["checks_dir"] = "mutable/empty-checks"
        path = self.save(data, folder=self.evals)
        self.suite_mod.load_suite(path)  # without a profile there is nothing to refuse
        self.assert_refused(path, extra_cli=self.cli(), **self.kw())

    def test_a_symlink_loop_under_checks_dir_does_not_hang_with_a_profile(self):  # covers: SUI-1
        checks, path = self.outside_variant("loop")
        plant_loop(checks)
        assert_returns_quickly(self, [path, self.profile_path, self.source])


# Review follow-ups: symlinks and loops under checks_dir, an empty evolvable checks_dir, and trailing slashes -----------

def make_link(target, link):
    """A symbolic link at `link` pointing to `target`, making the link's folder first."""
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(target, link)


def plant_loop(checks):
    """Two folder links back to their parent inside checks_dir: a walk that follows them never ends."""
    make_link("..", os.path.join(checks, "sub", "up"))
    make_link("..", os.path.join(checks, "sub", "up-again"))


# Loads a suite in a separate process, so a walk that never ends can be stopped: argv is the scripts folder, the
# suite file and, optionally, the profile and the source folder. Prints 'loaded' or 'refused: <message>'.
LOAD_PROGRAM = """import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import genome, suite
kw = {}
if len(sys.argv) > 3:
    kw = {"profile": genome.load_profile(sys.argv[3], source=sys.argv[4]), "source": sys.argv[4]}
try:
    suite.load_suite(sys.argv[2], **kw)
except suite.SuiteError as e:
    print("refused: " + str(e))
else:
    print("loaded")
"""


def assert_returns_quickly(test, args, seconds=10):
    """load_suite on args returns (loading or refusing with SuiteError) within `seconds`."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    try:
        done = subprocess.run([sys.executable, "-c", LOAD_PROGRAM, SCRIPTS, *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=seconds, cwd=test.tmp)
    except subprocess.TimeoutExpired:
        test.fail(f"load_suite didn't return within {seconds} seconds on a symlink loop under checks_dir")
    test.assertEqual(done.returncode, 0, f"load_suite either loads or raises SuiteError:\n{done.stderr}")
    test.assertRegex(done.stdout, r"^(loaded|refused: )", done.stdout)


class Sui1ChecksDirLinksAndTrailingSlashes(SuiteBase):
    def fresh_suite(self, label):
        """A suite folder of its own (checks/verify.sh, a case using {checks}); returns (its checks folder, path)."""
        folder = os.path.join(self.tmp, label)
        write(os.path.join(folder, "checks", "verify.sh"), "#!/bin/sh\ntest -s answer.txt\n")
        data = base_suite()
        data["cases"][0]["checks"] = [{"id": "script", "run": "sh {checks}/verify.sh"}]
        return os.path.join(folder, "checks"), self.save(data, folder=folder)

    def test_a_symlink_under_checks_dir_leading_out_of_the_suite_folder_is_refused(self):  # covers: SUI-1
        outside = os.path.join(self.tmp, "elsewhere")
        write(os.path.join(outside, "helper.sh"), "#!/bin/sh\ntrue\n")
        variants = [
            ("linked-subfolder", lambda checks: make_link(outside, os.path.join(checks, "linked"))),
            ("linked-file-in-subfolder",
             lambda checks: make_link(os.path.join(outside, "helper.sh"), os.path.join(checks, "sub", "helper.sh"))),
        ]
        for label, plant in variants:
            with self.subTest(label):
                checks, path = self.fresh_suite(label)
                plant(checks)
                self.assert_refused(path)

    def test_a_symlink_loop_under_checks_dir_does_not_hang(self):  # covers: SUI-1
        checks, path = self.fresh_suite("loop")
        plant_loop(checks)
        assert_returns_quickly(self, [path])

    def test_workspace_paths_with_a_trailing_slash_are_refused(self):  # covers: SUI-1
        def file_check(s):
            s["cases"][0]["checks"] = [{"id": "out-file", "file": "out/", "exists": True}]

        def judge_files(s):
            s["cases"][0]["checks"] = _judge(files=["out/"])

        def files_key_folder(s):
            s["cases"][0]["files"] = {"out/": "fixtures/base"}

        def files_key_file(s):
            s["cases"][0]["files"] = {"data/in.txt/": "fixtures/in.txt"}

        variants = [
            ("file check path 'out/'", file_check, ["alpha", "out-file"]),
            ("judge files path 'out/'", judge_files, ["alpha", "polite"]),
            ("files key 'out/' with a folder source", files_key_folder, ["alpha"]),
            ("files key 'data/in.txt/' with a file source", files_key_file, ["alpha"]),
        ]
        for label, change, names in variants:
            with self.subTest(label):
                data = base_suite()
                change(data)
                self.assert_refused(self.save(data), names)


# SUI-2: the split ----------------------------------------------------------------------------------------------------

class Sui2Split(SuiteBase):
    def suite(self, ids, own=None, **top):
        own = own or {}
        data = {"name": SUITE_NAME, "install": ".", "cases": [simple_case(i, own.get(i)) for i in ids]}
        data.update(top)
        return data

    def split_of(self, data, filename=SUITE_FILE):
        loaded = self.suite_mod.load_suite(self.save(data, filename=filename))
        return loaded["split"]

    def assert_partition(self, split, ids):
        train, holdout = split["train"], split["holdout"]
        self.assertFalse(set(train) & set(holdout), "no case is in both train and holdout")
        self.assertEqual(sorted(train + holdout), sorted(ids), "every case is in exactly one of train and holdout")

    def test_hashed_split_follows_the_rule_and_lists_keep_the_stable_order(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghij"]
        split = self.split_of(self.suite(ids))
        self.assert_partition(split, ids)
        train, holdout = expected_split(SUITE_NAME, ids, 40)
        self.assertEqual(len(holdout), 4, "round(10 * 40 / 100) cases are held out by default")
        self.assertEqual(split["holdout"], holdout)
        self.assertEqual(split["train"], train)
        self.assertEqual(split["screening"], train, "with screen_cases unset, screening is every training case")

    def test_split_does_not_depend_on_the_order_of_cases_in_the_file(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghij"]
        forward = self.split_of(self.suite(ids))
        backward = self.split_of(self.suite(list(reversed(ids))), filename="reversed.json")
        self.assertEqual(forward, backward)

    def test_holdout_pct_sets_how_many_are_held_out(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghij"]
        for pct, held in ((20, 2), (70, 7), (1, 1)):
            with self.subTest(holdout_pct=pct):
                split = self.split_of(self.suite(ids, holdout_pct=pct))
                self.assert_partition(split, ids)
                train, holdout = expected_split(SUITE_NAME, ids, pct)
                self.assertEqual(len(holdout), held)
                self.assertEqual((split["train"], split["holdout"]), (train, holdout))

    def test_at_least_one_is_held_out_when_none_names_holdout(self):  # covers: SUI-2
        ids = ["case-a", "case-b", "case-c"]
        split = self.split_of(self.suite(ids, holdout_pct=10))
        self.assertEqual(len(split["holdout"]), 1, "round(0.3) is 0, but one case is held out when none names holdout")
        self.assertEqual(split["holdout"], expected_split(SUITE_NAME, ids, 10)[1])

    def test_a_case_naming_holdout_removes_the_minimum_of_one(self):  # covers: SUI-2
        ids = ["case-a", "case-b", "case-c"]
        own = {"case-c": "holdout"}
        split = self.split_of(self.suite(ids, own=own, holdout_pct=10))
        self.assertEqual(split["holdout"], ["case-c"])
        self.assertEqual(split["train"], stable_order(SUITE_NAME, ["case-a", "case-b"]))

    def test_a_cases_own_split_wins_and_is_placed_in_the_stable_order(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghij"]
        order = stable_order(SUITE_NAME, ids)
        # The case first in the stable order would be held out and the last would train; their own split wins.
        own = {order[0]: "train", order[-1]: "holdout"}
        split = self.split_of(self.suite(ids, own=own))
        self.assert_partition(split, ids)
        self.assertIn(order[0], split["train"])
        self.assertIn(order[-1], split["holdout"])
        train, holdout = expected_split(SUITE_NAME, ids, 40, own)
        self.assertEqual((split["train"], split["holdout"]), (train, holdout),
                         "the other 8 split by the hash (round(3.2) = 3 held out); every list in the stable order")

    def test_same_split_on_every_load_and_in_separate_processes(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghijkl"]
        path = self.save(self.suite(ids))
        first = self.suite_mod.load_suite(path)["split"]
        self.assertEqual(self.suite_mod.load_suite(path)["split"], first)
        program = ("import json, sys\nsys.dont_write_bytecode = True\nsys.path.insert(0, sys.argv[1])\n"
                   "import suite\nprint(json.dumps(suite.load_suite(sys.argv[2])['split']))\n")
        for seed in ("1", "987"):
            with self.subTest(PYTHONHASHSEED=seed):
                env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
                env["PYTHONHASHSEED"] = seed
                done = subprocess.run([sys.executable, "-c", program, SCRIPTS, path], env=env, capture_output=True,
                                      text=True, encoding="utf-8", timeout=60, cwd=self.tmp)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout), first)

    def test_adding_a_case_moves_at_most_what_the_rounding_requires(self):  # covers: SUI-2
        names = [f"case-{c}" for c in "abcdefghijklmnop"]
        before = self.split_of(self.suite(names[:5]), filename="grow-5.json")
        for n in range(6, len(names) + 1):
            with self.subTest(cases=n):
                ids = names[:n]
                after = self.split_of(self.suite(ids), filename=f"grow-{n}.json")
                self.assert_partition(after, ids)
                train, holdout = expected_split(SUITE_NAME, ids, 40)
                self.assertEqual((after["train"], after["holdout"]), (train, holdout))
                moved = [i for i in names[:n - 1] if (i in before["holdout"]) != (i in after["holdout"])]
                self.assertLessEqual(len(moved), 1, f"adding {ids[-1]} moved {moved}")
                before = after

    def test_screening_is_the_first_screen_cases_training_cases(self):  # covers: SUI-2
        ids = [f"case-{c}" for c in "abcdefghij"]
        for count in (1, 2, 6):
            with self.subTest(screen_cases=count):
                split = self.split_of(self.suite(ids, screen_cases=count))
                train, _ = expected_split(SUITE_NAME, ids, 40)
                self.assertEqual(split["train"], train)
                self.assertEqual(split["screening"], train[:count])

    def test_a_suite_with_no_held_out_or_no_training_case_is_refused(self):  # covers: SUI-2
        ids = ["case-a", "case-b", "case-c"]
        cases = [
            ("every case names train", self.suite(ids, own={i: "train" for i in ids})),
            ("every case names holdout", self.suite(ids, own={i: "holdout" for i in ids})),
            ("a single case, held out by the minimum of one", self.suite(["case-a"])),
            ("holdout_pct so high every case is held out", self.suite(ids, holdout_pct=99)),
        ]
        for label, data in cases:
            with self.subTest(label):
                path = self.save(data)
                message = self.assert_refused(path)
                self.assertRegex(message.lower(), r"add|split", "the message says to add cases or set a split")


if __name__ == "__main__":
    unittest.main()
