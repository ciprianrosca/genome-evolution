"""Workspaces and runners (spec .regista/specs/suite-runs.json: SUI-3, SUI-4; SUI-13 is the end-to-end test in
genome/tests/e2e/test_suite_run.py).

suite.run_case(suite, case, individual, folder, config, keep=False) runs one case of a suite with one individual (a
folder of the target's files) in a fresh throwaway workspace and writes folder/result.json, folder/prompt.txt,
folder/output.log and, after the runner ends, folder/checks/. The runner is the suite's: a template runner (a
command with {workspace}, {individual}, {prompt_file}, {prompt} and {model}) or the default 'claude', which is
genome/scripts/runners.py running Claude Code headless through evolve.json's claude_command.

Everything is made up and lives in temporary folders: a small suite (two fixtures, one of them frozen, and two check
scripts), an individual folder of two files, a template runner (a Python script) and a stand-in claude. Both record
in a control folder what they saw (cwd, argv, the names of their environment variables, the workspace's files, the
prompt file's text, their process group, whether the run folder already held checks/) and then do what the test
asks: edit their installed copy and the fixtures, sleep past the time limit with a child process, print result
objects with usage, print a usage-limit message, or exit with a given code.

Interface assumed (from the spec):
- suite is load_suite's dict and case is the case's id; config is evolve.load_config(home), with claude_command a
  list and require_sandbox true or false.
- {individual} is the installed copy's absolute path, workspace/<install>.
- tokens for 'claude-json' and the default runner come from the usage of the last {"type": "result"} object:
  input, cache creation and output summed, cache reads left out (spec point 4); the fixtures' cache reads are large so
  counting them shows.
- `evolve.py suite run` (evolve.main in-process) denies the default runner's agent reading the whole evolver working
  folder: Read, Glob and Grep deny rules (in --settings permissions.deny or --disallowedTools, absolute paths written
  //path) and the sandbox's filesystem.denyRead in --settings.
- A refused run (no sandbox runtime) either raises an EvolveError or writes result.json with outcome 'aborted'; in
  both cases its message names bubblewrap (or bwrap) and socat and the runner never starts.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest test_suite_runs -v (from genome/tests)"""
import contextlib
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
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable
INSTALL = ".claude/skills/toy"
MARKER = "RUNNER-OUTPUT-MARKER-5521"
PROMPT = "Summarise data/input.txt into summary.txt in three made-up bullet points."
FILE_PROMPT = "Write farewell.txt with a short made-up goodbye.\n"
SKILL = "# Toy skill\n\nAlways answer in three bullet points.\n"
EXTRA = "Made-up notes the skill keeps next to it.\n"
INPUT = "Line one of the made-up input.\nLine two of the made-up input.\n"
FROZEN = "Made-up reference data the agent must not change.\n"
VERIFY = "#!/bin/sh\ntest -s summary.txt\n"
HELPER = "print('made-up check helper')\n"

# Variables that must never reach a runner, and the ones that must (the subscription login and the rest).
POISON = {
    "ANTHROPIC_API_KEY": "sk-ant-made-up", "ANTHROPIC_AUTH_TOKEN": "made-up-token",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
    "AWS_BEARER_TOKEN_BEDROCK": "made-up-bedrock", "CLAUDE_CODE_API_KEY_HELPER": "/bin/false",
    "CLAUDE_API_KEY": "made-up-key", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent-session",
    "CLAUDE_PROJECT_DIR": "/nowhere/made-up-project", "CLAUDE_CODE_ENTRYPOINT": "cli",
}
KEEP = {"CLAUDE_CODE_OAUTH_TOKEN": "made-up-oauth", "CLAUDE_CONFIG_DIR": "/nowhere/made-up-config",
        "RUNNER_KEEP_ME": "1"}

COMMON = r'''
import json, os, subprocess, sys, time
CONTROL = @CONTROL@

def read(name, default=""):
    try:
        with open(os.path.join(CONTROL, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default

def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def files(root):
    found = {}
    for d, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            try:
                with open(p, encoding="utf-8") as f:
                    found[os.path.relpath(p, root).replace(os.sep, "/")] = f.read()
            except Exception:
                found[os.path.relpath(p, root).replace(os.sep, "/")] = None
    return found

def record(name, extra):
    run_folder = read("run_folder")
    entry = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "files": files(os.getcwd()),
             "pgid": os.getpgid(0), "time": time.time(),
             "checks_in_run_folder": os.path.exists(os.path.join(run_folder, "checks")) if run_folder else None}
    entry.update(extra)
    with open(os.path.join(CONTROL, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def emit(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
    sys.stdout.flush()
'''

RUNNER = "#!@PYTHON@" + COMMON + r'''
args = sys.argv[1:]
prompt_text = None
if len(args) > 2:
    try:
        with open(args[2], encoding="utf-8") as f:
            prompt_text = f.read()
    except OSError:
        pass
record("runner.jsonl", {"prompt_file_text": prompt_text})
emit("@MARKER@")
mode = read("mode", "record")
if mode == "edit":
    write(os.path.join(args[1], "SKILL.md"), "Edited by the agent.\n")
    write(os.path.join(args[1], "added-by-agent.md"), "A file the agent added.\n")
    write(os.path.join(os.getcwd(), "data", "frozen.txt"), "Changed by the agent.\n")
    write(os.path.join(os.getcwd(), "data", "input.txt"), "Changed by the agent.\n")
    write(os.path.join(os.getcwd(), "summary.txt"), "- one\n- two\n- three\n")
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    write(os.path.join(CONTROL, "child.pid"), str(child.pid))
    time.sleep(60)
elif mode == "result":
    emit("some plain text before the JSON")
    emit({"type": "result", "usage": {"input_tokens": 9000, "output_tokens": 9000}})
    emit({"type": "assistant", "message": {"usage": {"input_tokens": 5, "output_tokens": 5}}})
    emit("{not json at all")
    emit({"type": "result", "subtype": "success", "result": "Done.",
          "usage": {"input_tokens": 120, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 5000,
                    "output_tokens": 45}})
sys.exit(int(read("exit", "0")))
'''
RESULT_TOKENS = 120 + 30 + 45  # cache reads (5000) left out

CLAUDE = "#!@PYTHON@" + COMMON + r'''
args = sys.argv[1:]
settings_files = {}
for i, a in enumerate(args[:-1]):
    if a == "--settings":
        p = args[i + 1] if os.path.isabs(args[i + 1]) else os.path.join(os.getcwd(), args[i + 1])
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                settings_files[args[i + 1]] = f.read()
record("claude.jsonl", {"settings_files": settings_files})
emit({"type": "system", "subtype": "init", "session_id": "s-1", "apiKeySource": "none"})
mode = read("claude_mode", "ok")
if mode == "limit":
    text = "You've hit your limit · resets 3am (UTC)"
    emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": text}],
                                           "usage": {"input_tokens": 7, "output_tokens": 3}}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": text,
          "usage": {"input_tokens": 7, "output_tokens": 3}})
    sys.exit(1)
emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "@MARKER@"}],
                                       "usage": {"input_tokens": 7, "output_tokens": 3}}})
installed = os.path.join(os.getcwd(), "@INSTALL@", "SKILL.md")
if os.path.exists(installed):
    write(installed, "Edited by the stand-in claude.\n")
emit({"type": "result", "subtype": "success", "is_error": False, "result": "Done.",
      "usage": {"input_tokens": 200, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 3000,
                "output_tokens": 60}})
sys.exit(0)
'''
CLAUDE_TOKENS = 200 + 40 + 60  # cache reads (3000) left out


def import_scripts(name):
    """Import genome/scripts/<name>.py as a module, writing no bytecode."""
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


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def snapshot(root):
    """Every folder and file under root, file contents as bytes."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        for d in dirnames:
            snap[os.path.join(rel, d) + "/"] = None
        for n in filenames:
            with open(os.path.join(dirpath, n), "rb") as f:
                snap[os.path.join(rel, n)] = f.read()
    return snap


def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def settings_of(call):
    """Every --settings object claude got: inline JSON, or a file it read at start."""
    found, argv = [], call["argv"]
    for i, a in enumerate(argv[:-1]):
        if a == "--settings":
            text = (call.get("settings_files") or {}).get(argv[i + 1], argv[i + 1])
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
    return found


def deny_rules(call):
    """Every deny rule: --settings permissions.deny and the values of --disallowedTools."""
    rules = []
    for s in settings_of(call):
        rules += list(((s.get("permissions") or {}).get("deny")) or [])
    argv = call["argv"]
    for i, a in enumerate(argv):
        if a in ("--disallowedTools", "--disallowed-tools"):
            j = i + 1
            while j < len(argv) and not argv[j].startswith("-"):
                rules += [r.strip() for r in re.split(r",(?![^(]*\))|\s+(?![^(]*\))", argv[j]) if r.strip()]
                j += 1
    return rules


def rule_path(pattern, home=None):
    """The absolute folder or file a path pattern names: //abs, ~/rel (the home folder) or /abs; None otherwise."""
    if pattern.startswith("~/"):
        pattern = os.path.join(home or os.path.expanduser("~"), pattern[2:])
    elif pattern.startswith("//"):
        pattern = pattern[1:]
    elif not pattern.startswith("/"):
        return None
    return re.sub(r"(/\*\*?)+$", "", pattern) or "/"


def rule_covers(rule, tool, folder, home=None):
    """Whether a deny rule like Read(//abs/path/**) or Read(~/.ssh/**) refuses `tool` on everything inside folder."""
    m = re.fullmatch(r"\s*(\w+)\((.*)\)\s*", rule)
    if not m or m.group(1) != tool or not (m.group(2).startswith("//") or m.group(2).startswith("~/")):
        return False
    return inside(folder, rule_path(m.group(2), home))


def deny_read_paths(call, home=None):
    """The sandbox's filesystem.denyRead entries in every --settings object, as absolute paths."""
    found = []
    for s in settings_of(call):
        for p in (((s.get("sandbox") or {}).get("filesystem") or {}).get("denyRead")) or []:
            path = rule_path(p, home) if p.startswith(("~/", "/")) else None
            if path:
                found.append(path)
    return found


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        pass
    if os.path.isdir("/proc"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class RunBase(unittest.TestCase):
    def setUp(self):
        self.suite_mod = import_scripts("suite")
        self.ev = import_scripts("evolve")
        self.core = import_scripts("evolve_core")
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-runs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        self.control = os.path.join(self.tmp, "control")
        for d in (self.home, self.evo, self.control):
            os.makedirs(d)
        env = mock.patch.dict(os.environ, {"HOME": self.home, "REGISTA_EVOLVE_HOME": self.evo})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.runner = os.path.join(self.tmp, "bin", "runner.py")
        self.claude = os.path.join(self.tmp, "bin", "claude")
        for path, text in ((self.runner, RUNNER), (self.claude, CLAUDE)):
            text = (text.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))
                    .replace("@MARKER@", MARKER).replace("@INSTALL@", INSTALL))
            write(path, text, 0o755)
        self.root = os.path.join(self.tmp, "suite")
        write(os.path.join(self.root, "fixtures", "input.txt"), INPUT)
        write(os.path.join(self.root, "fixtures", "frozen.txt"), FROZEN)
        write(os.path.join(self.root, "prompts", "farewell.txt"), FILE_PROMPT)
        write(os.path.join(self.root, "checks", "verify.sh"), VERIFY, 0o755)
        write(os.path.join(self.root, "checks", "lib", "helper.py"), HELPER)
        self.individual = os.path.join(self.tmp, "individual")
        write(os.path.join(self.individual, "SKILL.md"), SKILL)
        write(os.path.join(self.individual, "notes", "extra.md"), EXTRA)
        self.folder = os.path.join(self.tmp, "runs", "edit-case", "individual")
        write(os.path.join(self.control, "run_folder"), self.folder)
        self.configure(require_sandbox=False)

    # The template runner: every placeholder, plus a literal {{x}} that must stay {x}.
    def template(self, tokens="none"):
        return {"command": [PY, self.runner, "{workspace}", "{individual}", "{prompt_file}", "{prompt}", "{model}",
                            "--literal={{x}}"], "tokens": tokens}

    def make_suite(self, runner=None, **top):
        data = {
            "name": "toy-runs", "install": INSTALL, "model": "toy-model-7",
            "cases": [
                {"id": "edit-case", "split": "train", "prompt": PROMPT,
                 "files": {"data/input.txt": "fixtures/input.txt", "data/frozen.txt": "fixtures/frozen.txt"},
                 "frozen": ["data/frozen.txt"], "checks": [{"run": "sh {checks}/verify.sh"}]},
                {"id": "file-prompt", "split": "holdout", "prompt_file": "prompts/farewell.txt",
                 "checks": [{"file": "farewell.txt", "exists": True}]},
            ],
        }
        data["runner"] = runner if runner is not None else self.template()
        data.update(top)
        path = os.path.join(self.root, "toy-runs.json")
        write(path, json.dumps(data, indent=2) + "\n")
        return self.suite_mod.load_suite(path)

    def configure(self, **extra):
        cfg = {"holdout_pct": 0, "claude_command": [self.claude], **extra}
        write(os.path.join(self.evo, "evolve.json"), json.dumps(cfg) + "\n")
        self.config = self.ev.load_config(self.evo)

    def control_file(self, name, value):
        write(os.path.join(self.control, name), str(value))

    def run_case(self, suite, case="edit-case", keep=False, folder=None):
        self.suite_mod.run_case(suite, case, self.individual, folder or self.folder, self.config, keep=keep)
        return self.result(folder)

    def result(self, folder=None):
        with open(os.path.join(folder or self.folder, "result.json"), encoding="utf-8") as f:
            return json.load(f)

    def calls(self, name="runner.jsonl"):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def one_call(self, name="runner.jsonl"):
        calls = self.calls(name)
        self.assertEqual(len(calls), 1, f"the runner should start exactly once: {calls}")
        return calls[0]

    def bare_path(self):
        """A PATH folder holding only sh and python3: no bwrap, no socat."""
        folder = os.path.join(self.tmp, "bare-bin")
        os.makedirs(folder)
        os.symlink(PY, os.path.join(folder, "python3"))
        found = shutil.which("sh")
        if found:
            os.symlink(found, os.path.join(folder, "sh"))
        return folder


class WorkspaceRuns(RunBase):
    """SUI-3: the workspace, the installed copy, the checks and what a run leaves behind."""

    def test_workspace_holds_fixtures_and_installed_individual_and_no_check_script(self):  # covers: SUI-3
        self.run_case(self.make_suite())
        call = self.one_call()
        files = call["files"]
        self.assertEqual(files.get("data/input.txt"), INPUT, "the case's fixture is copied in")
        self.assertEqual(files.get("data/frozen.txt"), FROZEN, "the frozen fixture is copied in")
        self.assertEqual(files.get(f"{INSTALL}/SKILL.md"), SKILL, "the individual is installed at the install path")
        self.assertEqual(files.get(f"{INSTALL}/notes/extra.md"), EXTRA, "the individual's folders come along")
        leaked = [p for p in files if os.path.basename(p) in ("verify.sh", "helper.py")]
        self.assertEqual(leaked, [], "no check script may be in the workspace while the runner runs")
        self.assertIs(call["checks_in_run_folder"], False, "checks/ is copied only after the runner ends")
        for other in (self.root, self.individual, self.folder):
            self.assertFalse(inside(call["cwd"], other), f"the workspace {call['cwd']} lies inside {other}")
            self.assertFalse(inside(other, call["cwd"]), f"{other} lies inside the workspace")

    def test_agent_edits_leave_suite_and_individual_unchanged_and_workspace_is_removed(self):  # covers: SUI-3
        suite = self.make_suite()
        before_suite, before_individual = snapshot(self.root), snapshot(self.individual)
        self.control_file("mode", "edit")
        result = self.run_case(suite)
        call = self.one_call()
        self.assertEqual(snapshot(self.root), before_suite, "the suite folder changed")
        self.assertEqual(snapshot(self.individual), before_individual, "the individual changed")
        self.assertFalse(os.path.exists(call["cwd"]), "the workspace is removed when not kept")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "workspace")), "nothing kept without keep=True")
        self.assertIsNone(result.get("workspace"), "result.json names no workspace when it wasn't kept")

    def test_checks_are_copied_into_the_run_folder_after_the_run(self):  # covers: SUI-3
        self.control_file("mode", "edit")
        self.run_case(self.make_suite())
        checks = os.path.join(self.folder, "checks")
        self.assertEqual(snapshot(checks), snapshot(os.path.join(self.root, "checks")),
                         "folder/checks/ holds the suite's check scripts, byte for byte")

    def test_frozen_fixtures_hashes_are_recorded_before_the_run(self):  # covers: SUI-3
        self.control_file("mode", "edit")
        result = self.run_case(self.make_suite())
        self.assertEqual(result.get("frozen"),
                         {"data/frozen.txt": hashlib.sha256(FROZEN.encode("utf-8")).hexdigest()},
                         "only the frozen globs' files, hashed as they were before the agent changed them")

    def test_keep_moves_the_workspace_into_the_run_folder(self):  # covers: SUI-3
        self.control_file("mode", "edit")
        result = self.run_case(self.make_suite(), keep=True)
        call = self.one_call()
        kept = os.path.join(self.folder, "workspace")
        self.assertTrue(os.path.isdir(kept), "keep=True moves the workspace to folder/workspace/")
        self.assertTrue(result.get("workspace") and os.path.samefile(result["workspace"], kept),
                        f"result.json names the kept workspace: {result.get('workspace')}")
        with open(os.path.join(kept, *INSTALL.split("/"), "SKILL.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Edited by the agent.\n", "the kept workspace holds the agent's edits")
        self.assertTrue(os.path.isfile(os.path.join(kept, "summary.txt")))
        self.assertFalse(os.path.exists(call["cwd"]) and not os.path.samefile(call["cwd"], kept),
                         "the temporary workspace was copied instead of moved")
        with open(os.path.join(self.individual, "SKILL.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), SKILL, "the individual is unchanged")

    def test_result_json_prompt_and_output_log(self):  # covers: SUI-3
        result = self.run_case(self.make_suite())
        self.assertEqual(result.get("case"), "edit-case")
        self.assertEqual(result.get("outcome"), "done")
        self.assertEqual(result.get("tokens"), {"used": 0, "agent": 0, "judge": 0},
                         "a 'none' template runner reports 0 tokens; no judge ran")
        runner = result.get("runner") or {}
        self.assertEqual(runner.get("exit"), 0)
        self.assertIsInstance(runner.get("seconds"), (int, float))
        self.assertGreaterEqual(runner["seconds"], 0)
        for key in ("started_at", "ended_at"):
            self.assertTrue(result.get(key), f"result.json has {key}")
        self.assertIn("workspace", result, "result.json always has 'workspace' (null when not kept)")
        with open(os.path.join(self.folder, "prompt.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), PROMPT)
        with open(os.path.join(self.folder, "output.log"), encoding="utf-8") as f:
            self.assertIn(MARKER, f.read(), "the runner's full output goes to output.log")

    def test_prompt_file_text_is_the_prompt(self):  # covers: SUI-3
        folder = os.path.join(self.tmp, "runs", "file-prompt", "individual")
        result = self.run_case(self.make_suite(), case="file-prompt", folder=folder)
        self.assertEqual(result.get("case"), "file-prompt")
        with open(os.path.join(folder, "prompt.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), FILE_PROMPT)
        self.assertEqual(self.one_call()["prompt_file_text"], FILE_PROMPT)

    def test_aborted_when_the_workspace_cannot_be_prepared(self):  # covers: SUI-3
        suite = self.make_suite()
        os.remove(os.path.join(self.root, "fixtures", "input.txt"))  # the fixture disappears after loading
        result = self.run_case(suite)
        self.assertEqual(result.get("outcome"), "aborted")
        self.assertIsInstance(result.get("problem"), str)
        self.assertTrue(result["problem"].strip(), "an aborted run says what went wrong in a sentence")
        self.assertEqual(self.calls(), [], "the runner never starts when the workspace couldn't be prepared")


class RunnerBehaviour(RunBase):
    """SUI-4: the template, the environment, the time limit, the tokens, the usage limit and the sandbox refusal."""

    def test_template_is_filled_and_runs_in_the_workspace(self):  # covers: SUI-4
        self.run_case(self.make_suite())
        call = self.one_call()
        argv, ws = call["argv"], call["cwd"]
        self.assertEqual(len(argv), 6, argv)
        self.assertEqual(os.path.realpath(argv[0]), os.path.realpath(ws),
                         "{workspace} is the workspace, and the runner's cwd")
        self.assertTrue(os.path.isabs(argv[1]), "{individual} is an absolute path")
        self.assertEqual(os.path.realpath(argv[1]), os.path.realpath(os.path.join(ws, *INSTALL.split("/"))),
                         "{individual} is the installed copy in the workspace")
        self.assertFalse(inside(argv[2], ws), "the prompt file is written outside the workspace")
        self.assertEqual(call["prompt_file_text"], PROMPT, "{prompt_file} holds the prompt")
        self.assertEqual(argv[3], PROMPT, "{prompt} is the prompt itself")
        self.assertEqual(argv[4], "toy-model-7", "{model} is the suite's model")
        self.assertEqual(argv[5], "--literal={x}", "{{ and }} stand for literal braces")

    def test_no_credentials_or_session_variables_reach_either_runner(self):  # covers: SUI-4
        with mock.patch.dict(os.environ, {**POISON, **KEEP}):
            self.run_case(self.make_suite())
            folder = os.path.join(self.tmp, "runs", "claude")
            self.run_case(self.make_suite(runner="claude"), folder=folder)
        for name in ("runner.jsonl", "claude.jsonl"):
            with self.subTest(runner=name):
                call = self.one_call(name)
                names = set(call["env"])
                leaked = sorted(n for n in names if n in POISON or n.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")))
                self.assertEqual(leaked, [], "these variables reached the runner")
                for keep in (*KEEP, "PATH", "HOME"):
                    self.assertIn(keep, names, f"{keep} passes through")
                self.assertNotEqual(call["pgid"], os.getpgrp(), "the runner runs in its own process group")

    def test_stopped_at_the_time_limit_with_its_children(self):  # covers: SUI-4
        self.control_file("mode", "sleep")
        start = time.time()
        result = self.run_case(self.make_suite(timeout_minutes=0.02))  # 1.2 seconds
        elapsed = time.time() - start
        self.assertEqual(result.get("outcome"), "timeout")
        self.assertLess(elapsed, 30, "the runner sleeps 60 s; the run must stop it at its time limit")
        with open(os.path.join(self.control, "child.pid"), encoding="utf-8") as f:
            child = int(f.read())
        deadline = time.time() + 10
        while alive(child) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(child), "the runner's child process outlived the time limit")
        self.assertFalse(os.path.exists(self.one_call()["cwd"]), "the workspace is removed after a timeout too")

    def test_failed_outcome_for_a_non_zero_exit(self):  # covers: SUI-4
        self.control_file("exit", 4)
        result = self.run_case(self.make_suite())
        self.assertEqual(result.get("outcome"), "failed")
        self.assertEqual((result.get("runner") or {}).get("exit"), 4)

    def test_claude_json_tokens_come_from_the_last_result_object(self):  # covers: SUI-4
        self.control_file("mode", "result")
        for tokens, used in (("claude-json", RESULT_TOKENS), ("none", 0)):
            with self.subTest(tokens=tokens):
                folder = os.path.join(self.tmp, "runs", tokens)
                result = self.run_case(self.make_suite(runner=self.template(tokens)), folder=folder)
                self.assertEqual(result.get("outcome"), "done")
                self.assertEqual(result.get("tokens"), {"used": used, "agent": used, "judge": 0})

    def test_default_runner_runs_claude_headless_and_reads_its_tokens(self):  # covers: SUI-4
        result = self.run_case(self.make_suite(runner="claude"))
        call = self.one_call("claude.jsonl")
        argv = call["argv"]
        self.assertEqual(result.get("outcome"), "done")
        self.assertEqual(result.get("tokens"), {"used": CLAUDE_TOKENS, "agent": CLAUDE_TOKENS, "judge": 0},
                         "the last result object's usage, summed")
        self.assertEqual(flag(argv, "-p"), PROMPT)
        self.assertEqual(flag(argv, "--model"), "toy-model-7")
        self.assertEqual(flag(argv, "--output-format"), "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(flag(argv, "--setting-sources"), "project", "no user settings or plugins")
        self.assertIn("--mcp-config", argv, "an empty MCP configuration")
        self.assertIn(f"{INSTALL}/SKILL.md", call["files"], "claude runs in the workspace with the individual")
        tools = " ".join(v for i, a in enumerate(argv[:-1])
                         if a in ("--tools", "--allowedTools", "--allowed-tools") for v in [argv[i + 1]])
        for tool in ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill", "Task"):
            self.assertRegex(tools, rf"\b{tool}\b", f"the tools allowed name {tool}: {argv}")
        for tool in ("WebFetch", "WebSearch"):
            self.assertNotIn(tool, tools, f"no web tool is allowed: {argv}")
        settings = []
        for i, a in enumerate(argv[:-1]):
            if a == "--settings":
                text = call["settings_files"].get(argv[i + 1], argv[i + 1])
                try:
                    settings.append(json.loads(text))
                except ValueError:
                    pass
        self.assertTrue([s for s in settings if isinstance(s.get("sandbox"), dict) and s["sandbox"].get("enabled")
                         is True], f"--settings turns the sandbox on: {settings}")
        with open(os.path.join(self.folder, "output.log"), encoding="utf-8") as f:
            self.assertIn(MARKER, f.read())

    def test_usage_limit_message_pauses(self):  # covers: SUI-4
        self.control_file("claude_mode", "limit")
        result = self.run_case(self.make_suite(runner="claude"))
        self.assertEqual(result.get("outcome"), "paused")

    @unittest.skipUnless(sys.platform.startswith("linux"), "the sandbox runtime checked here is Linux's")
    def test_default_runner_refused_without_the_sandbox_runtime(self):  # covers: SUI-4
        self.configure()  # require_sandbox left at its default
        suite = self.make_suite(runner="claude")
        with mock.patch.dict(os.environ, {"PATH": self.bare_path()}):
            try:
                result = self.run_case(suite)
            except self.core.EvolveError as e:
                message = str(e)
            else:
                self.assertEqual(result.get("outcome"), "aborted", f"the run should be refused: {result}")
                message = str(result.get("problem"))
        self.assertRegex(message, re.compile(r"bubblewrap|bwrap", re.I), "the message says what to install")
        self.assertRegex(message, re.compile(r"socat", re.I))
        self.assertRegex(message, re.compile(r"sandbox", re.I), "the message mentions the sandbox: false opt-out")
        self.assertEqual(self.calls("claude.jsonl"), [], "claude never starts")

    @unittest.skipUnless(sys.platform.startswith("linux"), "the sandbox runtime checked here is Linux's")
    def test_opting_out_or_a_template_runs_without_the_sandbox_runtime(self):  # covers: SUI-4
        bare = self.bare_path()
        variants = (("suite sandbox false", {"runner": "claude", "sandbox": False}, {}, "claude.jsonl"),
                    ("require_sandbox false", {"runner": "claude"}, {"require_sandbox": False}, "claude.jsonl"),
                    ("template runner", {}, {}, "runner.jsonl"))
        for label, top, cfg, log in variants:
            with self.subTest(label):
                for name in ("claude.jsonl", "runner.jsonl"):
                    if os.path.exists(os.path.join(self.control, name)):
                        os.remove(os.path.join(self.control, name))
                self.configure(**cfg)
                suite = self.make_suite(**top)
                folder = os.path.join(self.tmp, "runs", re.sub(r"\W+", "-", label))
                with mock.patch.dict(os.environ, {"PATH": bare}):
                    result = self.run_case(suite, folder=folder)
                self.assertEqual(result.get("outcome"), "done", result)
                self.one_call(log)


class ReviewFollowUps(RunBase):
    """SUI-3: the agent can't read the evolver's working folder; a failed copy of the checks still cleans up."""

    def test_suite_run_denies_reading_the_whole_working_folder(self):  # covers: SUI-3
        # Earlier runs and other individuals live in the working folder: none of it is the agent's business.
        write(os.path.join(self.evo, "genomes", "other", "plugin", "SKILL.md"), "Another individual, made up.\n")
        write(os.path.join(self.evo, "runs", "old", "edit-case", "individual", "checks", "verify.sh"), VERIFY)
        write(os.path.join(self.evo, "runs", "old", "edit-case", "individual", "output.log"), "An old log.\n")
        suite = self.make_suite(runner="claude")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(["suite", "run", suite["path"], "edit-case", "--individual", self.individual])
            except SystemExit as e:
                code = e.code
        self.assertIn(code, (0, None), buf.getvalue())
        call = self.one_call("claude.jsonl")
        rules = deny_rules(call)
        for tool in ("Read", "Glob", "Grep"):
            self.assertTrue([r for r in rules if rule_covers(r, tool, self.evo)],
                            f"no {tool} deny rule covers the working folder {self.evo}: {rules}")
        deny_read = deny_read_paths(call)
        self.assertTrue([p for p in deny_read if inside(self.evo, p)],
                        f"the sandbox's denyRead doesn't cover the working folder {self.evo}: {deny_read}")
        for rule in rules:
            for tool in ("Read", "Glob", "Grep"):
                self.assertFalse(rule_covers(rule, tool, call["cwd"]), f"{rule} denies the workspace itself")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads files whatever their mode")
    def test_failed_copy_of_the_checks_still_removes_the_workspace(self):  # covers: SUI-3
        suite = self.make_suite()
        unreadable = os.path.join(self.root, "checks", "verify.sh")
        os.chmod(unreadable, 0)
        self.addCleanup(os.chmod, unreadable, 0o755)
        try:
            self.suite_mod.run_case(suite, "edit-case", self.individual, self.folder, self.config)
        except Exception as e:  # a plain error is acceptable, but the workspace must be gone and result.json written
            self.assertIsInstance(e, self.core.EvolveError, f"not a plain error: {e!r}")
        call = self.one_call()
        self.assertFalse(os.path.exists(call["cwd"]), "the workspace is removed even when copying the checks failed")
        result = self.result()
        problem = result.get("problem")
        self.assertIsInstance(problem, str, f"result.json says what went wrong: {result}")
        self.assertRegex(problem, re.compile(r"check", re.I), "the problem names the check scripts")


class SecondReview(RunBase):
    """The second review: a suite in HOME, project settings in the workspace, and the user's credentials."""

    def run_claude(self, suite, case="edit-case"):
        """run_case with the default runner; returns (result.json or None, the error message or None)."""
        try:
            self.suite_mod.run_case(suite, case, self.individual, self.folder, self.config)
        except self.core.EvolveError as e:
            return None, str(e)
        return self.result(), None

    def test_suite_in_the_home_folder_never_leaves_checks_or_prompts_readable(self):  # covers: SUI-3
        write(os.path.join(self.root, "rubrics", "tone.txt"), "Pass if the summary is short and made up.\n")
        data = {"name": "home-suite", "install": INSTALL, "runner": "claude", "cases": [
            {"id": "edit-case", "split": "train", "prompt_file": "prompts/farewell.txt",
             "checks": [{"run": "sh {checks}/verify.sh"},
                        {"judge": None, "rubric_file": "rubrics/tone.txt", "model": "toy-judge",
                         "files": ["summary.txt"]}]},
            {"id": "other", "split": "holdout", "prompt": PROMPT, "checks": [{"file": "x.txt", "exists": True}]}]}
        del data["cases"][0]["checks"][1]["judge"]
        path = os.path.join(self.root, "my-suite.json")
        write(path, json.dumps(data, indent=2) + "\n")
        suite = self.suite_mod.load_suite(path)
        with mock.patch.dict(os.environ, {"HOME": self.root}):
            result, error = self.run_claude(suite)
        calls = self.calls("claude.jsonl")
        if not calls:  # refused: fine, if it says why
            message = error if error is not None else (result or {}).get("problem")
            if error is None:
                self.assertEqual((result or {}).get("outcome"), "aborted", result)
            self.assertIsInstance(message, str)
            self.assertTrue(message.strip(), "a refusal says what the problem is")
            return
        call = calls[0]
        rules, deny_read = deny_rules(call), deny_read_paths(call, self.root)
        secrets = {"checks folder": os.path.join(self.root, "checks"),
                   "prompt file": os.path.join(self.root, "prompts", "farewell.txt"),
                   "rubric file": os.path.join(self.root, "rubrics", "tone.txt"), "suite file": path}
        for label, target in secrets.items():
            with self.subTest(label):
                self.assertTrue([r for r in rules if rule_covers(r, "Read", target, self.root)],
                                f"the run started with the {label} readable: {rules}")
                self.assertTrue([p for p in deny_read if inside(target, p)],
                                f"the sandbox's denyRead leaves the {label} readable: {deny_read}")

    def test_project_settings_never_reach_the_workspace_and_hooks_are_off(self):  # covers: SUI-4
        planted = json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo planted"}]}]},
                              "marker": "planted-settings-77"}) + "\n"
        write(os.path.join(self.root, "fixtures", "claude-settings.json"), planted)
        write(os.path.join(self.individual, ".claude", "settings.local.json"), planted)
        fixture_case = {"id": "edit-case", "split": "train", "prompt": PROMPT,
                        "files": {".claude/settings.json": "fixtures/claude-settings.json"},
                        "checks": [{"file": "summary.txt", "exists": True}]}
        holdout = {"id": "other", "split": "holdout", "prompt": PROMPT, "checks": [{"file": "x.txt", "exists": True}]}
        variants = (("fixture .claude/settings.json", INSTALL, fixture_case),
                    ("individual .claude/settings.local.json installed at the root", ".",
                     {**fixture_case, "files": {}}))
        for label, install, case in variants:
            with self.subTest(label):
                log = os.path.join(self.control, "claude.jsonl")
                if os.path.exists(log):
                    os.remove(log)
                shutil.rmtree(self.folder, ignore_errors=True)
                path = os.path.join(self.root, "toy-runs.json")
                write(path, json.dumps({"name": "toy-runs", "install": install, "runner": "claude",
                                        "cases": [case, holdout]}, indent=2) + "\n")
                result, error = self.run_claude(self.suite_mod.load_suite(path))
                calls = self.calls("claude.jsonl")
                if not calls:
                    message = error if error is not None else (result or {}).get("problem")
                    self.assertIsInstance(message, str, f"refused without saying why: {result}")
                    continue
                files = calls[0]["files"]
                for name in (".claude/settings.json", ".claude/settings.local.json"):
                    self.assertNotEqual(files.get(name), planted, f"the planted {name} reached the workspace")
                settings = settings_of(calls[0])
                self.assertTrue([s for s in settings if s.get("disableAllHooks") is True],
                                f"--settings turns every hook off: {settings}")

    def test_default_runner_denies_reading_the_users_credentials(self):  # covers: SUI-4
        self.run_case(self.make_suite(runner="claude"))
        call = self.one_call("claude.jsonl")
        rules, deny_read = deny_rules(call), deny_read_paths(call, self.home)
        for rel in (".claude/projects", ".ssh", ".aws", ".config/gh", ".gnupg", ".netrc"):
            target = os.path.join(self.home, *rel.split("/"))
            for tool in ("Read", "Glob", "Grep"):
                with self.subTest(rel, tool=tool):
                    self.assertTrue([r for r in rules if rule_covers(r, tool, target, self.home)],
                                    f"no {tool} deny rule covers ~/{rel}: {rules}")
            with self.subTest(rel, sandbox="denyRead"):
                self.assertTrue([p for p in deny_read if inside(target, p)],
                                f"the sandbox's denyRead doesn't cover ~/{rel}: {deny_read}")

    def test_token_and_key_variables_never_reach_either_runner(self):  # covers: SUI-4
        tokens = {"GH_TOKEN": "made-up-gh", "GITHUB_TOKEN": "made-up-github", "OPENAI_API_KEY": "made-up-openai",
                  "NPM_TOKEN": "made-up-npm", "STRIPE_SECRET": "made-up-stripe", "AWS_ACCESS_KEY_ID": "AKIAMADEUP",
                  "AWS_SECRET_ACCESS_KEY": "made-up-aws-secret", "AWS_SESSION_TOKEN": "made-up-aws-session"}
        with mock.patch.dict(os.environ, {**tokens, **KEEP}):
            self.run_case(self.make_suite())
            self.run_case(self.make_suite(runner="claude"), folder=os.path.join(self.tmp, "runs", "claude"))
        for name in ("runner.jsonl", "claude.jsonl"):
            with self.subTest(runner=name):
                names = set(self.one_call(name)["env"])
                self.assertEqual(sorted(n for n in names if n in tokens), [], "these variables reached the runner")
                for keep in (*KEEP, "PATH", "HOME"):
                    self.assertIn(keep, names, f"{keep} passes through")


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Made Up", "-c", "user.email=made-up@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


class ThirdReview(RunBase):
    """The third review: Claude Code's own history and login file, and the suite's git repository."""

    def assert_denied(self, call, target, label, home=None):
        rules, deny_read = deny_rules(call), deny_read_paths(call, home or self.home)
        for tool in ("Read", "Glob", "Grep"):
            with self.subTest(label, tool=tool):
                self.assertTrue([r for r in rules if rule_covers(r, tool, target, home or self.home)],
                                f"no {tool} deny rule covers {label} ({target}): {rules}")
        with self.subTest(label, sandbox="denyRead"):
            self.assertTrue([p for p in deny_read if inside(target, p)],
                            f"the sandbox's denyRead doesn't cover {label} ({target}): {deny_read}")

    def test_default_runner_denies_claude_file_history_and_credentials(self):  # covers: SUI-4
        config_dir = os.path.join(self.tmp, "claude-config")
        for label, extra in (("without CLAUDE_CONFIG_DIR", {}), ("with CLAUDE_CONFIG_DIR", {"CLAUDE_CONFIG_DIR": config_dir})):
            log = os.path.join(self.control, "claude.jsonl")
            if os.path.exists(log):
                os.remove(log)
            folder = os.path.join(self.tmp, "runs", re.sub(r"\W+", "-", label))
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
            env.update(extra)
            with mock.patch.dict(os.environ, env, clear=True):
                self.run_case(self.make_suite(runner="claude"), folder=folder)
            call = self.one_call("claude.jsonl")
            bases = [os.path.join(self.home, ".claude")] + ([config_dir] if extra else [])
            for base in bases:
                self.assert_denied(call, os.path.join(base, "file-history"), f"{label}: {base}/file-history")
                self.assert_denied(call, os.path.join(base, ".credentials.json"),
                                   f"{label}: {base}/.credentials.json")

    def suite_in_repo(self, worktree=False):
        """A git repository holding a suite in suites/ (with suites/checks/); returns (suite dict, git dirs)."""
        main = os.path.join(self.tmp, "repo")
        os.makedirs(main)
        git(main, "init", "-q", "-b", "main")
        suites = os.path.join(main, "suites")
        write(os.path.join(suites, "checks", "verify.sh"), VERIFY, 0o755)
        write(os.path.join(suites, "fixtures", "input.txt"), INPUT)
        data = {"name": "repo-suite", "install": INSTALL, "runner": "claude", "cases": [
            {"id": "edit-case", "split": "train", "prompt": PROMPT, "files": {"data/input.txt": "fixtures/input.txt"},
             "checks": [{"run": "sh {checks}/verify.sh"}]},
            {"id": "other", "split": "holdout", "prompt": PROMPT, "checks": [{"file": "x.txt", "exists": True}]}]}
        write(os.path.join(suites, "suite.json"), json.dumps(data, indent=2) + "\n")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "a made-up suite")
        top, dirs = main, [os.path.join(main, ".git")]
        if worktree:
            top = os.path.join(self.tmp, "repo-worktree")
            git(main, "worktree", "add", "-q", top, "-b", "wt")
            self.assertTrue(os.path.isfile(os.path.join(top, ".git")), "a worktree's .git is a file")
            dirs = [os.path.join(top, git(top, "rev-parse", "--git-dir")),
                    os.path.join(top, git(top, "rev-parse", "--git-common-dir"))]
            dirs = [d if os.path.isabs(d) else os.path.join(top, d) for d in dirs]
        return self.suite_mod.load_suite(os.path.join(top, "suites", "suite.json")), dirs

    def test_suite_repository_git_folder_is_denied(self):  # covers: SUI-3
        for worktree in (False, True):
            with self.subTest(worktree=worktree):
                shutil.rmtree(os.path.join(self.tmp, "repo"), ignore_errors=True)
                shutil.rmtree(os.path.join(self.tmp, "repo-worktree"), ignore_errors=True)
                log = os.path.join(self.control, "claude.jsonl")
                if os.path.exists(log):
                    os.remove(log)
                suite, dirs = self.suite_in_repo(worktree)
                folder = os.path.join(self.tmp, "runs", f"repo-{worktree}")
                result = self.run_case(suite, folder=folder)
                self.assertEqual(result.get("outcome"), "done", result)
                call = self.one_call("claude.jsonl")
                for d in dirs:
                    self.assert_denied(call, d, f"the suite's git directory {d}")

    def test_git_folder_that_cannot_be_denied_refuses_the_run(self):  # covers: SUI-3
        suite, dirs = self.suite_in_repo()
        home_in_git = os.path.join(dirs[0], "made-up-home")
        os.makedirs(home_in_git)
        with mock.patch.dict(os.environ, {"HOME": home_in_git}):
            try:
                self.suite_mod.run_case(suite, "edit-case", self.individual, self.folder, self.config)
                result, error = self.result(), None
            except self.core.EvolveError as e:
                result, error = None, str(e)
        calls = self.calls("claude.jsonl")
        if calls:  # it may only start with the git directory denied
            self.assert_denied(calls[0], dirs[0], "the suite's git directory", home=home_in_git)
            return
        if error is None:
            self.assertEqual(result.get("outcome"), "aborted", result)
            error = result.get("problem")
        self.assertIsInstance(error, str, "a refusal says what the problem is")
        self.assertTrue(error.strip())


if __name__ == "__main__":
    unittest.main()
