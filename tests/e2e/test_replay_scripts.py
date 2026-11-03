"""End-to-end: a replay agent that breaks the code and edits package.json's test script to always pass still fails
the tests gate in the evolver's final gate run (spec .regista/specs/frozen-scripts.json, REP-12).

The sample project is set up with the real Regista CLI; its tests gate is `npm test --silent`, and package.json's
`test` script runs the project's unittest suite. It gets one step: a spec and a stub mul() at the parent commit, the
code and its test at the finishing commit; `evolve.py bench build` turns it into a case. The stand-in `claude` writes
a wrong mul(), changes the `test` script to `echo ok`, commits (a Bash call in its transcript backs the commit),
writes a passing gate row and an approving review a reviewer subagent recorded, and waits to be stopped. The
evolver's final gate run must put the parent's test script back, so the frozen test runs and fails.

Stand-ins for the sandbox runtime come first on PATH: bwrap runs the command it wraps for real (no sandbox), socat
only answers its version probe. Skipped when npm isn't installed. REGISTA_EVOLVE_HOME is a temporary folder.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # the genome's own copy of the harness, next to this file
from harness import CALC, GENOME, PY, REGISTA_CHECKOUT, SampleProject, requires_regista  # noqa: E402

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
STEP = "m1-multiply"
GATE = "npm test --silent"
PARENT_TEST = f"{PY} -m unittest discover -s tests"

STANDIN = r'''#!@PYTHON@
import json, os, re, signal, subprocess, sys, time, uuid
CONTROL = @CONTROL@
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
cwd = os.getcwd()
sid = opt("--session-id") or opt("--resume") or str(uuid.uuid4())
def git(*a):
    return subprocess.run(["git", "-c", "user.name=Stand In", "-c", "user.email=standin@example.com",
                           "-c", "commit.gpgsign=false", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
def log(name, entry):
    with open(os.path.join(CONTROL, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
def emit(event):
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v", "--help", "-h") for a in args):
    print("2.1.312 (Claude Code)" if any(a in ("--version", "-v") for a in args) else "Usage: claude [options] [prompt]")
    sys.exit(0)
log("calls.jsonl", {"argv": args, "cwd": cwd, "head": git("rev-parse", "HEAD")})
def result():
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "Done.",
          "session_id": sid, "total_cost_usd": 0, "modelUsage": {},
          "usage": {"input_tokens": 300, "cache_creation_input_tokens": 30, "output_tokens": 12,
                    "cache_read_input_tokens": 3000}})
def on_signal(signum, frame):
    log("signals.jsonl", {"signal": signal.Signals(signum).name, "time": time.time()})
    if signum == signal.SIGINT:
        result()
        os._exit(0)
    os._exit(143)
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)
if "--resume" in args:
    result()
    sys.exit(0)

emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": opt("--model"),
      "apiKeySource": "none", "plugins": [{"name": "regista", "path": opt("--plugin-dir")}], "plugin_errors": []})
config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
folder = os.path.join(config_dir, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
main = os.path.join(folder, sid + ".jsonl")
def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
def assistant(mid, content):
    return {"type": "assistant", "session_id": sid, "sessionId": sid, "uuid": str(uuid.uuid4()),
            "message": {"id": mid, "type": "message", "role": "assistant", "model": "claude-stand-in",
                        "content": content, "usage": {"input_tokens": 100, "cache_creation_input_tokens": 10,
                                                      "output_tokens": 5, "cache_read_input_tokens": 1000}}}
def append(path, rows):
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    for r in rows:
        emit(r)
def tool_call(mid, tid, t, name, inp, output, extra=None):
    call = assistant(mid, [{"type": "tool_use", "id": tid, "name": name, "input": inp}])
    call["timestamp"] = utc(t - 1)
    back = {"type": "user", "sessionId": sid, "timestamp": utc(t + 1),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": output}]}}
    for row in (call, back):
        row.update(extra or {})
    return [call, back]
append(main, [{"type": "user", "sessionId": sid, "message": {"role": "user", "content": "brief"}},
              assistant("msg_a", [{"type": "text", "text": "Working."}])])

# Break the code and make the test script always pass, then commit (backed by a Bash call in the transcript).
base = git("rev-parse", "--short", "HEAD")
with open(os.path.join(cwd, "calc.py"), "a", encoding="utf-8") as f:
    f.write("\n\ndef mul(a, b):\n    return a + b\n")
with open(os.path.join(cwd, "package.json"), encoding="utf-8") as f:
    package = json.load(f)
package["scripts"]["test"] = "echo ok"
with open(os.path.join(cwd, "package.json"), "w", encoding="utf-8") as f:
    json.dump(package, f, indent=2)
    f.write("\n")
git("add", "-A")
t = time.time()
git("commit", "-q", "-m", "multiply")
head = git("rev-parse", "--short", "HEAD")
append(main, tool_call("msg_b", "toolu_commit_1", t, "Bash", {"command": 'git add -A && git commit -q -m "multiply"'},
                       "[main %s] multiply" % head))

reg = os.path.join(cwd, ".regista")
with open(os.path.join(reg, "config.json"), encoding="utf-8") as f:
    gates = [g["name"] for g in json.load(f).get("gates", []) if "manual" not in g.get("when", [])] or ["tests"]
ids = []
for name in sorted(os.listdir(os.path.join(reg, "specs"))):
    if name.endswith(".json"):
        with open(os.path.join(reg, "specs", name), encoding="utf-8") as f:
            ids += [q["id"] for q in json.load(f).get("requirements", [])]
os.makedirs(os.path.join(reg, "gates"), exist_ok=True)
with open(os.path.join(reg, "gates", "history.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)), "commit": head,
                        "results": [{"name": g, "exit": 0, "seconds": 0.5, "status": "pass"} for g in gates]}) + "\n")
with open(os.path.join(reg, "gates", "requirements.json"), "w", encoding="utf-8") as f:
    json.dump({i: {"verify": "test", "milestone": "M1", "spec": ".regista/specs/@STEP@.json", "passes": True,
                   "evidence": ["tests/test_mul.py"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": base}
               for i in ids}, f)

# An approving review that a reviewer subagent recorded with review-record, as its own transcript shows.
fingerprint, agent, t = "e1f2a3b4c5d60718", "rv1", time.time()
os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
with open(os.path.join(reg, "reviews", fingerprint + ".json"), "w", encoding="utf-8") as f:
    json.dump({"verdict": "approve", "base": base, "checked": ids, "files": ["calc.py"], "findings": [],
               "fingerprint": fingerprint, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)),
               "summary": "Looks right."}, f)
spawn = tool_call("msg_c", "toolu_agent_rv1", t, "Agent",
                  {"subagent_type": "regista:reviewer", "description": "review", "prompt": "Review the step."},
                  [{"type": "text", "text": "Approved."}])
spawn[0]["timestamp"], spawn[1]["timestamp"] = utc(t - 30), utc(t + 30)
spawn[1]["toolUseResult"] = {"status": "completed", "agentId": agent,
                             "content": [{"type": "text", "text": "Approved."}]}
append(main, spawn)
record = 'python3 "%s" review-record' % os.path.join(opt("--plugin-dir") or "", "scripts", "regista.py")
side = {"agentId": agent, "isSidechain": True}
rows = [dict({"type": "user", "sessionId": sid, "timestamp": utc(t - 29),
              "message": {"role": "user", "content": "Review the step."}}, **side)]
rows += tool_call("msg_d", "toolu_rv1_01", t, "Bash", {"command": record},
                  "Recorded review approve for 1 file(s), fingerprint %s." % fingerprint, side)
sub = os.path.join(folder, sid, "subagents")
os.makedirs(sub, exist_ok=True)
append(os.path.join(sub, "agent-%s.jsonl" % agent), rows)
with open(os.path.join(sub, "agent-%s.meta.json" % agent), "w", encoding="utf-8") as f:
    json.dump({"agentType": "regista:reviewer"}, f)
log("edits.jsonl", {"head": head, "test_script": package["scripts"]["test"], "time": time.time()})

end = time.time() + 60  # keep running until the replay stops us
while time.time() < end:
    time.sleep(0.1)
result()
'''

# Stand-in bubblewrap: answers --version; otherwise applies --chdir and the environment options and runs the wrapped
# command for real, recording it and its exit code in control/bwrap.jsonl.
BWRAP = r'''#!@PYTHON@
import json, os, subprocess, sys
CONTROL = @CONTROL@
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("bubblewrap 0.9.0")
    sys.exit(0)
TWO = {"--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try", "--symlink",
       "--setenv", "--file", "--bind-data", "--ro-bind-data", "--chmod"}
ONE = {"--chdir", "--dev", "--proc", "--tmpfs", "--dir", "--unsetenv", "--remount-ro", "--perms", "--size",
       "--uid", "--gid", "--hostname", "--seccomp", "--info-fd", "--json-status-fd", "--block-fd", "--sync-fd",
       "--userns", "--userns2", "--pidns", "--exec-label", "--file-label", "--lock-file", "--cap-add", "--cap-drop",
       "--args", "--argv0", "--add-seccomp-fd", "--userns-block-fd"}
env, chdir, cmd, i = dict(os.environ), None, [], 0
while i < len(args):
    a = args[i]
    if a == "--":
        cmd = args[i + 1:]
        break
    if not a.startswith("-"):
        cmd = args[i:]
        break
    if a in TWO and i + 2 < len(args):
        if a == "--setenv":
            env[args[i + 1]] = args[i + 2]
        i += 3
    elif a in ONE and i + 1 < len(args):
        if a == "--chdir":
            chdir = args[i + 1]
        elif a == "--unsetenv":
            env.pop(args[i + 1], None)
        i += 2
    else:
        if a == "--clearenv":
            env = {}
        i += 1
cwd = chdir if chdir and os.path.isdir(chdir) else os.getcwd()
code = subprocess.run(cmd, cwd=cwd, env=env).returncode if cmd else 0
with open(os.path.join(CONTROL, "bwrap.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": cwd, "command": cmd, "exit": code}) + "\n")
sys.exit(code)
'''
SOCAT = ("#!/bin/sh\ncase \"$1\" in -V|-h|--version) echo 'socat by Gerhard Rieger and contributors - see "
         "www.dest-unreach.org'; echo 'socat version 1.8.0.0';; esac\nexit 0\n")
TEST_MUL = ("import os\nimport sys\nimport unittest\n\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            "from calc import mul  # noqa: E402\n\n\n"
            "class MulTest(unittest.TestCase):\n"
            "    def test_multiplies(self):  # covers: MUL-1\n"
            "        self.assertEqual(mul(3, 4), 12)\n")


@requires_regista
class ReplayAgentEditsTheTestScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("npm") is None:
            raise unittest.SkipTest("npm is not installed; the sample project's tests gate is `npm test --silent`")
        cls.p = p = SampleProject()
        cls.evo = os.path.join(p.base, "evolve")
        cls.control = os.path.join(p.base, "control")
        os.makedirs(cls.control)
        p.set_up_regista()
        cfg = p.config()
        cfg["gates"] = [{"name": "tests", "run": GATE, "tests": ["tests/**"], "when": ["commit"], "timeout": 600}]
        cfg.setdefault("roles", {})["tests"] = ["tests/**"]
        with open(p.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        p.write("package.json", json.dumps({"name": "sample", "private": True, "scripts": {"test": PARENT_TEST}},
                                           indent=2) + "\n")
        p.write(".npmrc", "update-notifier=false\nfund=false\naudit=false\n")
        p.write(".regista/plan.md", "# Sample: plan\n\n## M1 — Maths\n- [x] Multiply two numbers (MUL-1)\n")
        p.write(f".regista/specs/{STEP}.json", json.dumps({
            "step": STEP, "milestone": "M1", "title": "Multiply two numbers", "state": "active",
            "plan_step": "Multiply two numbers (MUL-1)", "interface": "mul(a, b) in calc.py.",
            "requirements": [{"id": "MUL-1", "text": "mul(a, b) SHALL return the product of a and b",
                              "verify": "test"}]}, indent=1) + "\n")
        p.write("calc.py", CALC + "\n\ndef mul(a, b):\n    raise NotImplementedError\n")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "multiply: spec and stub")
        p.write("calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")
        p.write("tests/test_mul.py", TEST_MUL)
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "multiply: tests")
        tools = os.path.join(p.base, "sandbox-bin")
        os.makedirs(tools)
        cls.claude = os.path.join(cls.control, "claude")
        for path, text in ((cls.claude, STANDIN.replace("@STEP@", STEP)), (os.path.join(tools, "bwrap"), BWRAP),
                           (os.path.join(tools, "socat"), SOCAT)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace("@PYTHON@", PY).replace("@CONTROL@", repr(cls.control)))
            os.chmod(path, 0o755)
        os.makedirs(cls.evo)
        with open(os.path.join(cls.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 0, "claude_command": cls.claude, "replay_timeout_minutes": 5,
                       "replay_resumes": 0}, f)
        cls.env = dict(p.env, REGISTA_EVOLVE_HOME=cls.evo)
        cls.env["PATH"] = tools + os.pathsep + cls.env.get("PATH", os.environ.get("PATH", ""))
        cls.build = cls.evolve("bench", "build", p.root)
        cls.case = None
        for path in glob.glob(os.path.join(cls.evo, "bench", "*", "*.json")):
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("step") == STEP:
                    cls.case = os.path.basename(path)[:-5]
        cls.replay = cls.evolve("replay", cls.case, "--genome", "current") if cls.case else None

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["chmod", "-R", "u+w", cls.p.base], capture_output=True)
        cls.p.cleanup()

    @classmethod
    def evolve(cls, *args):
        start = time.time()
        r = subprocess.run([PY, EVOLVE, *args], cwd=cls.p.base, capture_output=True, text=True, encoding="utf-8",
                           env=cls.env, timeout=300)
        r.seconds = time.time() - start
        return r

    def lines(self, name):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_test_script_edited_to_always_pass_still_fails_the_tests_gate(self):  # covers: REP-12
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{self.build.stdout}\n{self.build.stderr}")
        self.assertIsNotNone(self.case, "bench build made no case for the sample step")
        out = self.replay.stdout + self.replay.stderr
        self.assertEqual(self.replay.returncode, 0, f"replay failed:\n{out}")
        edits = self.lines("edits.jsonl")
        self.assertEqual([e["test_script"] for e in edits], ["echo ok"],
                         f"the stand-in committed a wrong mul() and the test script `echo ok`:\n{out}")
        runs = [c for c in self.lines("bwrap.jsonl") if GATE in " ".join(c.get("command") or c.get("argv") or [])]
        self.assertTrue(runs, f"the evolver never ran `{GATE}` itself in its final gate run:\n{out}")
        self.assertTrue(all(c["exit"] != 0 for c in runs), f"the final runs of the tests gate fail: {runs}")
        found = glob.glob(os.path.join(self.evo, "runs", "**", "result.json"), recursive=True)
        self.assertEqual(len(found), 1, f"one result.json under runs/: {found}")
        with open(found[0], encoding="utf-8") as f:
            result = json.load(f)
        self.assertEqual((result.get("final_gates") or {}).get("tests", {}).get("status"), "fail",
                         f"the final gate run fails the tests gate: {result.get('final_gates')}")
        self.assertIs(((result.get("scores") or {}).get("gates") or {}).get("failed"), True, result.get("scores"))
        self.assertNotEqual(result.get("outcome"), "approved", "a run whose final gate run fails is not approved")


if __name__ == "__main__":
    unittest.main()
