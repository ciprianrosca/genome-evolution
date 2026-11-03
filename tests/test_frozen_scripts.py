"""The evolver's final gate run puts back the parent's test scripts and test-runner config (spec
.regista/specs/frozen-scripts.json, REP-11).

A replay agent can make `npm test --silent` pass without running the original tests by editing package.json's `test`
script, or a test runner's config. For the final gate run only, the evolver gives every script the parent commit's
package.json already had its parent text (scripts the step added, and the rest of the file, stay as the agent wrote
them), restores each file matching evolve.json `replay_frozen_files` ({project: [globs]}) to the parent's content, or
removes it when the parent didn't have it, and afterwards puts the agent's versions back. result.json's `restored`
lists what was put back ([{path, what}]); a parent package.json that isn't valid JSON leaves package.json untouched,
and result.json says why.

The worlds reuse tests/test_replay_run.py's stand-ins: its stand-in claude, with one more scenario (final_scripts:
src/double.py from the control file `code`, plus the files in the control file `extra_files`, committed with a backed
commit, a passing gate row and a backed approving review), and its stand-in bwrap, which runs the wrapped command for
real and here also records package.json, .mocharc.json and vitest.config.js as the command saw them (`seen`).
The sample project's tests gate is `npm test --silent`, run by the real npm (the tests are skipped without npm);
package.json's `test` script runs the project's Python unittest suite.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_frozen_scripts -v"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_replay_run as rr  # noqa: E402
from regista_location import requires_regista  # noqa: E402

PY = rr.PY
EVOLVE = rr.EVOLVE
PROJECT = "doubler"
GATE = "npm test --silent"
WATCHED = ("package.json", ".mocharc.json", "vitest.config.js")
FROZEN_GLOBS = ["vitest.config.*", ".mocharc*"]

PARENT_TEST = "python3 -m unittest discover -s tests/unit"
PARENT_E2E = "python3 -c \"print('e2e ran')\""
PARENT_PACKAGE = json.dumps({"name": PROJECT, "version": "1.0.0", "private": True, "description": "Doubles numbers.",
                             "scripts": {"test": PARENT_TEST, "e2e": PARENT_E2E}}, indent=2) + "\n"
AGENT_PACKAGE_DATA = {"name": PROJECT, "version": "1.1.0", "private": True,
                      "description": "Doubles numbers, as the agent left it.", "keywords": ["agent"],
                      "scripts": {"test": "echo ok", "e2e": "echo e2e ok",
                                  "coverage": "python3 -c \"print('coverage')\""}}
AGENT_PACKAGE = json.dumps(AGENT_PACKAGE_DATA, indent=4) + "\n"
PARENT_MOCHARC = '{"spec": "test/**/*.spec.js"}\n'
AGENT_MOCHARC = '{"spec": "nothing/**/*.none.js"}\n'
AGENT_VITEST = "export default { test: { passWithNoTests: true } }\n"
BROKEN_PARENT_PACKAGE = '{"name": "doubler", "scripts": {"test": "%s",}\n' % PARENT_TEST  # trailing comma
CODE_WRONG = "def double(n):\n    return 2 * n + 1\n"
CODE_RIGHT = "def double(n):\n    return 2 * n\n"

CONFIG = dict(rr.CONFIG, project=PROJECT, gates=[
    {"name": "tests", "run": GATE, "tests": ["tests/unit/**"], "when": ["commit"], "timeout": 600}])

# One more stand-in claude scenario, final_scripts, built on the FIT-8 scenarios of tests/test_replay_run.py.
_SCENARIOS = '"final_env", "final_flaky"):'
_PUT = '    put_files(files)\n    git("add", "-A")\n'
STANDIN = rr.STANDIN.replace(_SCENARIOS, '"final_env", "final_flaky", "final_scripts"):').replace(
    _PUT, '    if scenario == "final_scripts":\n'
          '        files["src/double.py"] = read_raw("code")\n'
          '        files.update(json.loads(read_raw("extra_files")))\n' + _PUT)

# The stand-in bwrap, also recording the watched files as the wrapped command saw them in the clone.
_RUN = "code = 0\nif cmd:"
_RECORD = '"frozen": frozen, '
BWRAP = rr.BWRAP.replace(_RUN, (
    "seen = None\n"
    "for folder in [cwd] + [b[1] for b in binds] + [x for x in args if os.path.isabs(x)]:\n"
    "    if os.path.isfile(os.path.join(folder, 'tests', 'unit', 'test_double.py')):\n"
    "        seen = {}\n"
    "        for rel in %r:\n"
    "            p = os.path.join(folder, rel)\n"
    "            seen[rel] = open(p, encoding='utf-8').read() if os.path.isfile(p) else None\n"
    "        break\n" % (WATCHED,)) + _RUN).replace(_RECORD, _RECORD + '"seen": seen, ')

_TEMPLATES = {}


def build_project(root, package_text):
    """The doubler project with package.json, an .npmrc and a .mocharc.json; its one step: spec and stub at the
    parent commit, the code and the test with its covers marker at the finishing commit."""
    os.makedirs(root)
    rr.git(root, "init", "-q", "-b", "main")
    files = {".gitignore": ".regista/hooks.log\n.regista/gates/\n.regista/state/\n__pycache__/\nnode_modules/\n",
             ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n",
             ".regista/plan.md": "# Doubler: plan\n\n## M1 — Numbers\n- [x] Double a number (DBL-1)\n",
             ".npmrc": "update-notifier=false\nfund=false\naudit=false\n",
             ".mocharc.json": PARENT_MOCHARC,
             "package.json": package_text,
             "src/app.py": "VALUE = 1\n"}
    for rel, text in files.items():
        rr.write(root, rel, text)
    rr.git(root, "add", "-A")
    rr.git(root, "commit", "-q", "-m", "setup")
    rr.write(root, f".regista/specs/{rr.STEP}.json", json.dumps(rr.SPEC, indent=1) + "\n")
    rr.write(root, "src/double.py", "def double(n):\n    raise NotImplementedError\n")
    rr.git(root, "add", "-A")
    rr.git(root, "commit", "-q", "-m", "double: spec and stub")
    parent = rr.git(root, "rev-parse", "HEAD")
    rr.write(root, "src/double.py", CODE_RIGHT)
    rr.write(root, "tests/unit/test_double.py", rr.TEST_DOUBLE)
    rr.git(root, "add", "-A")
    rr.git(root, "commit", "-q", "-m", "double: tests")
    return parent


def template(name, package_text):
    """A bench built once per module for a project whose parent package.json is `package_text`."""
    if name in _TEMPLATES:
        return _TEMPLATES[name]
    tmp = tempfile.mkdtemp(prefix=f"regista-frozen-scripts-{name}-")
    project = os.path.join(tmp, "projects", PROJECT)
    t = {"tmp": tmp, "parent": build_project(project, package_text), "case": None}
    home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve")
    os.makedirs(home)
    os.makedirs(evo)
    with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
        json.dump({"holdout_pct": 0}, f)
    t["build"] = subprocess.run([PY, EVOLVE, "bench", "build", project], cwd=tmp, capture_output=True, text=True,
                                encoding="utf-8", env=rr.base_env(home, evo), timeout=300)
    t["evolve"] = evo
    for path in glob.glob(os.path.join(evo, "bench", "*", "*.json")):
        with open(path, encoding="utf-8") as f:
            if json.load(f).get("step") == rr.STEP:
                t["case"] = os.path.basename(path)[:-5]
    _TEMPLATES[name] = t
    return t


def tearDownModule():
    for t in _TEMPLATES.values():
        shutil.rmtree(t["tmp"], ignore_errors=True)


class World:
    """One replay of the doubler case: a copy of the bench, an evolve.json, a temporary HOME and evolver home
    (REGISTA_EVOLVE_HOME), the stand-in claude playing final_scripts and the recording stand-in bwrap."""

    def __init__(self, test, name, package_text, code, extra_files, **evolve_json):
        if shutil.which("npm") is None:
            raise unittest.SkipTest("npm is not installed; the sample project's tests gate is `npm test --silent`")
        for anchor, text in ((_SCENARIOS, rr.STANDIN), (_PUT, rr.STANDIN), (_RUN, rr.BWRAP), (_RECORD, rr.BWRAP)):
            test.assertEqual(text.count(anchor), 1, f"tests/test_replay_run.py's stand-ins changed: {anchor!r}")
        t = template(name, package_text)
        test.assertEqual(t["build"].returncode, 0, f"bench build failed:\n{t['build'].stdout}\n{t['build'].stderr}")
        test.assertIsNotNone(t["case"], "bench build made no case for the doubler step")
        self.case = t["case"]
        self.tmp = tempfile.mkdtemp(prefix="regista-frozen-scripts-run-")
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve")
        self.control = os.path.join(self.tmp, "control")
        self.bin = os.path.join(self.tmp, "sandbox-bin")
        for d in (self.home, self.control, self.bin):
            os.makedirs(d)
        shutil.copytree(t["evolve"], self.evo)
        tools = {"claude": STANDIN, "bwrap": BWRAP, "socat": rr.SANDBOX_TOOLS["socat"]}
        for tool, text in tools.items():
            folder = self.control if tool == "claude" else self.bin
            path = os.path.join(folder, tool)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control)))
            os.chmod(path, 0o755)
        for key, value in {"scenario": "final_scripts", "code": code,
                           "extra_files": json.dumps(extra_files)}.items():
            with open(os.path.join(self.control, key), "w", encoding="utf-8") as f:
                f.write(value)
        cfg = {"holdout_pct": 0, "claude_command": os.path.join(self.control, "claude"), "task_model": "haiku",
               "replay_timeout_minutes": 5, **evolve_json}
        with open(os.path.join(self.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def cleanup(self):
        subprocess.run(["chmod", "-R", "u+w", self.tmp], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self):
        env = rr.base_env(self.home, self.evo)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        return subprocess.run([PY, EVOLVE, "replay", self.case, "--genome", "current", "--keep"], cwd=self.tmp,
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=280)

    def lines(self, name):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def gate_runs(self):
        """The stand-in bwrap's records of the final run's tests gate (npm test --silent)."""
        return [c for c in self.lines("bwrap.jsonl")
                if GATE in " ".join(c.get("command") or []) or GATE in " ".join(c.get("argv") or [])]

    def result(self, test):
        found = sorted(glob.glob(os.path.join(self.evo, "runs", "**", "result.json"), recursive=True))
        test.assertEqual(len(found), 1, f"expected one result.json under runs/: {found}")
        with open(found[0], encoding="utf-8") as f:
            return json.load(f)


def restored_of(result):
    """result.json's `restored` list: at the top level, or under a key of that name at any depth."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("restored"), list):
                found.extend(obj["restored"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(result)
    return [e for e in found if isinstance(e, dict)]


def strings_in(obj):
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in strings_in(k) + strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings_in(v)]
    return []


def entry_for(entries, path):
    return [e for e in entries if str(e.get("path") or "").lstrip("./") == path.lstrip("./")
            or str(e.get("path") or "") == path]


@requires_regista
class AgentEditedScriptsAndRunnerConfig(unittest.TestCase):
    """One replay: the agent writes code failing the frozen test (2n+1), changes `test` and `e2e` to always pass,
    adds a `coverage` script, changes other package.json fields, edits .mocharc.json (the parent has one) and adds
    vitest.config.js (the parent hasn't). The project lists both in replay_frozen_files."""

    @classmethod
    def setUpClass(cls):
        cls.w = World(unittest.TestCase(), "valid", PARENT_PACKAGE, CODE_WRONG,
                      {"package.json": AGENT_PACKAGE, ".mocharc.json": AGENT_MOCHARC, "vitest.config.js": AGENT_VITEST},
                      replay_frozen_files={PROJECT: FROZEN_GLOBS})
        cls.r = cls.w.run()

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def setUp(self):
        self.out = self.r.stdout + self.r.stderr
        self.assertNotIn("Traceback", self.out)
        calls = self.w.lines("calls.jsonl")
        self.assertTrue(calls, f"claude was never started:\n{self.out}")
        self.clone = calls[0]["cwd"]
        self.assertTrue(self.w.lines("final.jsonl"), f"the stand-in never committed its edits:\n{self.out}")
        self.runs = self.w.gate_runs()
        self.assertTrue(self.runs, f"the evolver never ran `{GATE}` itself through bwrap:\n{self.out}")
        self.result = self.w.result(self)

    def test_final_run_uses_the_parent_scripts_keeps_added_scripts_and_the_rest_of_package_json(self):  # covers: REP-11
        for run in self.runs:
            seen = json.loads(run["seen"]["package.json"])
            self.assertEqual(seen["scripts"].get("test"), PARENT_TEST, "the parent's test script runs the tests")
            self.assertEqual(seen["scripts"].get("e2e"), PARENT_E2E, "every script the parent had gets its text")
            self.assertEqual(seen["scripts"].get("coverage"), AGENT_PACKAGE_DATA["scripts"]["coverage"],
                             "a script the step added stays as the agent wrote it")
            self.assertEqual(set(seen["scripts"]), {"test", "e2e", "coverage"})
            rest = {k: v for k, v in seen.items() if k != "scripts"}
            self.assertEqual(rest, {k: v for k, v in AGENT_PACKAGE_DATA.items() if k != "scripts"},
                             "the rest of package.json stays as the agent wrote it")
        self.assertNotEqual(self.runs[0]["exit"], 0, "the original tests ran and caught 2n+1")
        self.assertIs(rr.axis(self.result, "gates").get("failed"), True,
                      f"with the parent's test script the frozen test fails the final run: {self.result}")
        self.assertIsNone(rr.hard_failure_of(self.result), "editing a script is not a hard failure")

    def test_the_rerun_of_a_failing_gate_also_uses_the_parent_scripts(self):  # covers: REP-11
        self.assertEqual([r["exit"] != 0 for r in self.runs], [True, True],
                         f"the tests gate fails, so it runs once more (REP-10) and fails again:\n{self.out}")
        self.assertEqual([json.loads(r["seen"]["package.json"])["scripts"]["test"] for r in self.runs],
                         [PARENT_TEST, PARENT_TEST], "both runs use the parent's test script")

    def test_files_matching_replay_frozen_files_are_the_parents_during_the_final_run(self):  # covers: REP-11
        for run in self.runs:
            self.assertEqual(run["seen"][".mocharc.json"], PARENT_MOCHARC, "restored to the parent's content")
            self.assertIsNone(run["seen"]["vitest.config.js"], "the parent had no vitest.config.js: removed")

    def test_the_agents_versions_are_back_after_the_final_run(self):  # covers: REP-11
        self.assertTrue(os.path.isdir(self.clone), f"--keep keeps the clone:\n{self.out}")
        for rel, text in (("package.json", AGENT_PACKAGE), (".mocharc.json", AGENT_MOCHARC),
                          ("vitest.config.js", AGENT_VITEST)):
            path = os.path.join(self.clone, rel)
            self.assertTrue(os.path.isfile(path), f"{rel} is back in the clone after the final run")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text, f"{rel} is the agent's version again, byte for byte")

    def test_result_json_records_what_was_put_back_and_the_scores_carry_a_note(self):  # covers: REP-11
        entries = restored_of(self.result)
        self.assertTrue(entries, f"result.json has a `restored` list: {self.result}")
        package = entry_for(entries, "package.json")
        self.assertEqual(len(package), 1, f"one entry for package.json: {entries}")
        what = str(package[0].get("what") or "")
        self.assertRegex(what, re.compile(r"\btest\b"), f"names the test script: {what!r}")
        self.assertRegex(what, re.compile(r"\be2e\b"), f"names the e2e script: {what!r}")
        self.assertNotIn("coverage", what, "a script the step added wasn't put back")
        for rel in (".mocharc.json", "vitest.config.js"):
            found = entry_for(entries, rel)
            self.assertEqual(len(found), 1, f"one entry for {rel}: {entries}")
            self.assertIn("file", str(found[0].get("what") or ""), found[0])
        notes = [s for s in strings_in(self.result.get("scores") or {})
                 if re.search(r"package\.json|script|\.mocharc|vitest", s, re.I)]
        self.assertTrue(notes, f"the scores carry a plain note that the agent changed them: {self.result.get('scores')}")


@requires_regista
class ParentPackageJsonNotValid(unittest.TestCase):
    """One replay of a project whose parent package.json isn't valid JSON: the agent fixes it and writes the right
    code; package.json stays as the agent wrote it for the final run, and result.json says why."""

    def test_invalid_parent_package_json_leaves_package_json_untouched_and_records_why(self):  # covers: REP-11
        agent_package = json.dumps({"name": PROJECT, "private": True, "scripts": {"test": "echo ok"}}) + "\n"
        w = World(self, "broken", BROKEN_PARENT_PACKAGE, CODE_RIGHT, {"package.json": agent_package})
        self.addCleanup(w.cleanup)
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertTrue(w.lines("final.jsonl"), f"the stand-in never committed its edits:\n{out}")
        runs = w.gate_runs()
        self.assertTrue(runs, f"the evolver never ran `{GATE}` itself through bwrap:\n{out}")
        self.assertEqual(runs[0]["seen"]["package.json"], agent_package, "package.json untouched, byte for byte")
        result = w.result(self)
        self.assertEqual(entry_for(restored_of(result), "package.json"), [], "nothing put back in package.json")
        why = [s for s in strings_in(result) if "package.json" in s and re.search(r"json|valid|pars", s.replace(
            "package.json", ""), re.I)]
        self.assertTrue(why, f"result.json says the parent's package.json isn't valid JSON: {result}")


if __name__ == "__main__":
    unittest.main()
