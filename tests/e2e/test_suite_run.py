"""End-to-end: `evolve.py suite run <suite> <case> --individual <folder>` with a stand-in claude (spec
.regista/specs/suite-runs.json, SUI-13).

In a temporary folder the test writes a made-up suite (the default runner 'claude', one fixture, a check script and two
cases), an individual folder with a made-up skill, and a stand-in claude (a Python script named in evolve.json's
claude_command, with require_sandbox false) that edits its installed copy in the workspace and prints stream-json
with a result object whose usage sums to 300 tokens. HOME and REGISTA_EVOLVE_HOME are temporary folders.

The test runs `python3 genome/scripts/evolve.py suite run <suite> greet --individual <folder>` as a separate process
and checks: exit 0; one printed line naming the case, the outcome (done), the tokens (300) and result.json; result.json,
output.log and checks/ in runs/adhoc/greet/individual/ under the evolver's working folder; the individual folder
byte-for-byte unchanged.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from harness import EVOLVE, PY  # noqa: E402

INSTALL = ".claude/skills/greeter"
TOKENS = 200 + 40 + 60

CLAUDE = r'''#!@PYTHON@
import json, os, sys
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
emit({"type": "system", "subtype": "init", "session_id": "s-1"})
if os.path.exists(@SLEEP@):
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(@SLEEP@ + ".pids.tmp", "w", encoding="utf-8") as f:
        json.dump({"claude": os.getpid(), "child": child.pid, "cwd": os.getcwd()}, f)
    os.replace(@SLEEP@ + ".pids.tmp", @SLEEP@ + ".pids")
    time.sleep(120)
emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Greeting written."}],
                                       "usage": {"input_tokens": 7, "output_tokens": 3}}})
installed = os.path.join(os.getcwd(), "@INSTALL@", "SKILL.md")
if os.path.exists(installed):
    with open(installed, "w", encoding="utf-8") as f:
        f.write("Edited by the stand-in agent.\n")
with open("greeting.txt", "w", encoding="utf-8") as f:
    f.write("Hello from a made-up greeter.\n")
emit({"type": "result", "subtype": "success", "is_error": False, "result": "Done.",
      "usage": {"input_tokens": 200, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 0,
                "output_tokens": 60}})
'''


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def snapshot(root):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        for d in dirnames:
            snap[os.path.join(rel, d) + "/"] = None
        for n in filenames:
            with open(os.path.join(dirpath, n), "rb") as f:
                snap[os.path.join(rel, n)] = f.read()
    return snap


class SuiteRunEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-run-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        self.log = os.path.join(self.tmp, "claude-calls.jsonl")
        self.sleep_flag = os.path.join(self.tmp, "claude-sleeps")  # when this file exists the stand-in sleeps
        self.claude = os.path.join(self.tmp, "bin", "claude")
        write(self.claude, CLAUDE.replace("@PYTHON@", PY).replace("@LOG@", repr(self.log))
              .replace("@INSTALL@", INSTALL)
              .replace("@SLEEP@", repr(self.sleep_flag)), 0o755)
        write(os.path.join(self.evo, "evolve.json"),
              json.dumps({"claude_command": [self.claude], "require_sandbox": False}) + "\n")
        self.root = os.path.join(self.tmp, "suite")
        write(os.path.join(self.root, "fixtures", "names.txt"), "Ada\nGrace\n")
        write(os.path.join(self.root, "checks", "has_greeting.sh"), "#!/bin/sh\ntest -s greeting.txt\n", 0o755)
        self.suite = os.path.join(self.root, "greeter.json")
        write(self.suite, json.dumps({
            "name": "toy-greeter", "install": INSTALL,
            "cases": [
                {"id": "greet", "split": "train", "prompt": "Write greeting.txt greeting everyone in names.txt.",
                 "files": {"names.txt": "fixtures/names.txt"},
                 "checks": [{"run": "sh {checks}/has_greeting.sh"}]},
                {"id": "farewell", "split": "holdout", "prompt": "Write farewell.txt.",
                 "checks": [{"file": "farewell.txt", "exists": True}]},
            ]}, indent=2) + "\n")
        self.individual = os.path.join(self.tmp, "my-skill")
        write(os.path.join(self.individual, "SKILL.md"), "# Greeter\n\nGreet people by name, politely.\n")
        write(os.path.join(self.individual, "examples", "one.md"), "Hello, Ada.\n")

    def env(self):
        drop = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")
               and k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        env.update(HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1")
        return env

    def test_suite_run_with_an_individual_folder(self):  # covers: SUI-13
        before = snapshot(self.individual)
        r = subprocess.run([PY, EVOLVE, "suite", "run", self.suite, "greet", "--individual", self.individual],
                           cwd=self.tmp, env=self.env(), capture_output=True, text=True, encoding="utf-8",
                           timeout=300)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        lines = [line for line in r.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"one plain line:\n{r.stdout}")
        line = lines[0]
        for word in ("greet", "done", str(TOKENS), "result.json"):
            self.assertIn(word, line, f"the line names {word}")
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(len([x for x in f if x.strip()]), 1, "the stand-in claude ran once")

        folder = os.path.join(self.evo, "runs", "adhoc", "greet", "individual")
        with open(os.path.join(folder, "result.json"), encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual(result.get("case"), "greet")
        self.assertEqual(result.get("outcome"), "done")
        self.assertEqual(result.get("tokens"), {"used": TOKENS})
        with open(os.path.join(folder, "output.log"), encoding="utf-8") as f:
            self.assertIn('"type": "result"', f.read(), "output.log holds the runner's output")
        with open(os.path.join(folder, "checks", "has_greeting.sh"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "#!/bin/sh\ntest -s greeting.txt\n", "checks/ holds the suite's scripts")
        self.assertEqual(snapshot(self.individual), before, "the individual folder is unchanged")

    def test_interrupting_suite_run_stops_the_runner_group_and_removes_the_workspace(self):  # covers: SUI-4, SUI-3
        """Spec point 1: the runner is stopped when the evolver is interrupted (here SIGINT, as Ctrl-C sends)."""
        write(self.sleep_flag, "sleep\n")
        pids_file = self.sleep_flag + ".pids"
        proc = subprocess.Popen([PY, EVOLVE, "suite", "run", self.suite, "greet", "--individual", self.individual],
                                cwd=self.tmp, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", start_new_session=True)
        self.addCleanup(self.kill_leftovers, proc, pids_file)
        deadline = time.time() + 60
        while not os.path.exists(pids_file) and time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(pids_file), "the stand-in claude never started")
        with open(pids_file, encoding="utf-8") as f:
            pids = json.load(f)
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("the evolver didn't end within 60 seconds of SIGINT")
        deadline = time.time() + 15
        while (alive(pids["claude"]) or alive(pids["child"])) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pids["claude"]), f"the stand-in claude outlived the interrupt:\n{out}")
        self.assertFalse(alive(pids["child"]), f"the runner's child process outlived the interrupt:\n{out}")
        self.assertFalse(os.path.exists(pids["cwd"]), f"the workspace was left behind:\n{out}")

    def kill_leftovers(self, proc, pids_file):
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            with open(pids_file, encoding="utf-8") as f:
                pids = json.load(f)
        except (OSError, ValueError):
            return
        for pid in (pids.get("claude"), pids.get("child")):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, TypeError):
                pass


def alive(pid):
    """Whether a process exists and isn't a zombie."""
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


if __name__ == "__main__":
    unittest.main()
