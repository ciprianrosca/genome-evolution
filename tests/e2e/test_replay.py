"""End-to-end: `evolve.py replay` runs one case of a sample project with a stand-in `claude` (spec
.regista/specs/replay-run.json, REP-1).

The sample project is set up with the real Regista CLI, then gets one step: a spec and a stub at the parent commit,
the test with its covers marker at the finishing commit. `evolve.py bench build` turns it into a case, and
`evolve.py replay <case> --genome current` prepares a clone, starts the stand-in there, sees the approving review
with passing gates the stand-in writes, stops it, writes result.json and deletes the clone. The live sample project
must not gain a commit, although the stand-in commits in the clone.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # the genome's own copy of the harness, next to this file
from harness import CALC, GENOME, PY, REGISTA_CHECKOUT, SampleProject  # noqa: E402

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
STEP = "m1-multiply"
REQ_TEXT = "mul(a, b) SHALL return the product of a and b"
USED = 342  # input + cache creation + output for msg_a (written twice) and msg_b, each counted once

STANDIN = r'''#!@PYTHON@
import json, os, re, signal, subprocess, sys, time
CONTROL = @CONTROL@
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
cwd = os.getcwd()
sid = opt("--session-id") or opt("--resume") or "no-session"
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
    log("probes.jsonl", {"argv": args, "time": time.time()})  # a version or help probe, not a run
    print("2.1.312 (Claude Code)" if any(a in ("--version", "-v") for a in args) else "Usage: claude [options] [prompt]")
    sys.exit(0)
log("calls.jsonl", {"argv": args, "cwd": cwd, "env": sorted(os.environ), "head": git("rev-parse", "HEAD"),
                    "remotes": git("remote"), "frozen": os.path.exists(os.path.join(cwd, "tests", "test_mul.py"))})
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

emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": opt("--model"),
      "apiKeySource": "none", "plugins": [{"name": "regista", "path": opt("--plugin-dir")}], "plugin_errors": []})
USAGE = {"msg_a": (100, 10, 5, 1000), "msg_b": (200, 20, 7, 2000)}
def assistant(mid):
    i, c, o, r = USAGE[mid]
    return {"type": "assistant", "session_id": sid, "sessionId": sid,
            "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": "Working."}],
                        "usage": {"input_tokens": i, "cache_creation_input_tokens": c, "output_tokens": o,
                                  "cache_read_input_tokens": r}}}
lines = [assistant("msg_a"), assistant("msg_a"), assistant("msg_b")]
folder = os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
with open(os.path.join(folder, sid + ".jsonl"), "a", encoding="utf-8") as f:
    f.writelines(json.dumps(line) + "\n" for line in lines)
for line in lines:
    emit(line)

# Build the step and commit it in the clone.
with open(os.path.join(cwd, "calc.py"), "a", encoding="utf-8") as f:
    f.write("\n\ndef mul(a, b):\n    return a * b\n")
base = git("rev-parse", "--short", "HEAD")
git("commit", "-q", "-am", "multiply")
reg = os.path.join(cwd, ".regista")
with open(os.path.join(reg, "config.json"), encoding="utf-8") as f:
    gates = [g["name"] for g in json.load(f).get("gates", []) if "manual" not in g.get("when", [])] or ["tests"]
now = time.strftime("%Y-%m-%dT%H:%M:%S")
os.makedirs(os.path.join(reg, "gates"), exist_ok=True)
with open(os.path.join(reg, "gates", "history.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"date": now, "commit": git("rev-parse", "--short", "HEAD"),
                        "results": [{"name": g, "exit": 0, "seconds": 0.5, "status": "pass"} for g in gates]}) + "\n")
os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
with open(os.path.join(reg, "reviews", ".tmp"), "w", encoding="utf-8") as f:
    json.dump({"verdict": "approve", "base": base, "checked": ["MUL-1"], "at": now, "summary": "Good."}, f)
os.replace(os.path.join(reg, "reviews", ".tmp"), os.path.join(reg, "reviews", "standin-1.json"))

end = time.time() + 90  # keep running until the replay stops us
while time.time() < end:
    time.sleep(0.1)
result()
'''

TEST_MUL = ("import os\nimport sys\nimport unittest\n\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            "from calc import mul  # noqa: E402\n\n\n"
            "class MulTest(unittest.TestCase):\n"
            "    def test_multiplies(self):  # covers: MUL-1\n"
            "        self.assertEqual(mul(3, 4), 12)\n")


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def used_tokens(tokens):
    """Plausible 'tokens used' numbers in result.json's tokens (a number, a total field, or input + cache creation +
    output), at any depth."""
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
        return [tokens]
    found = []
    if isinstance(tokens, dict):
        found += [tokens[k] for k in ("used", "total", "tokens_used", "total_tokens", "used_tokens")
                  if isinstance(tokens.get(k), (int, float))]

        def pick(*names):
            return next((tokens[n] for n in names if isinstance(tokens.get(n), (int, float))), None)
        i, o = pick("input_tokens", "input"), pick("output_tokens", "output")
        c = pick("cache_creation_input_tokens", "cache_creation_tokens", "cache_creation") or 0
        if i is not None and o is not None:
            found.append(i + c + o)
        for value in tokens.values():
            if isinstance(value, (dict, list)):
                found += used_tokens(value)
    elif isinstance(tokens, list):
        for value in tokens:
            found += used_tokens(value)
    return found


# Stand-ins for the sandbox runtime: they answer a version probe and otherwise succeed without doing anything.
SANDBOX_TOOLS = {
    "bwrap": "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'bubblewrap 0.9.0'; fi\nexit 0\n",
    "socat": "#!/bin/sh\ncase \"$1\" in -V|-h|--version) echo 'socat by Gerhard Rieger and contributors - see www.dest-unreach.org'; "
             "echo 'socat version 1.8.0.0';; esac\nexit 0\n",
}


class ReplayOneCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = p = SampleProject()
        cls.evo = os.path.join(p.base, "evolve")
        cls.control = os.path.join(p.base, "control")
        os.makedirs(cls.control)
        p.set_up_regista()
        cfg = p.config()
        for gate in cfg["gates"]:
            gate["tests"] = ["tests/**"]
        cfg.setdefault("roles", {})["tests"] = ["tests/**"]
        with open(p.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        p.write(".regista/plan.md", "# Sample: plan\n\n## M1 — Maths\n- [x] Multiply two numbers (MUL-1)\n")
        p.write(f".regista/specs/{STEP}.json", json.dumps({
            "step": STEP, "milestone": "M1", "title": "Multiply two numbers", "state": "active",
            "plan_step": "Multiply two numbers (MUL-1)", "interface": "mul(a, b) in calc.py.",
            "requirements": [{"id": "MUL-1", "text": REQ_TEXT, "verify": "test"}]}, indent=1) + "\n")
        p.write("calc.py", CALC + "\n\ndef mul(a, b):\n    raise NotImplementedError\n")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "multiply: spec and stub")
        cls.parent = p.git("rev-parse", "HEAD").stdout.strip()
        p.write("calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")
        p.write("tests/test_mul.py", TEST_MUL)
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "multiply: tests")
        cls.claude = os.path.join(cls.control, "claude")
        with open(cls.claude, "w", encoding="utf-8") as f:
            f.write(STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(cls.control)))
        os.chmod(cls.claude, 0o755)
        os.makedirs(cls.evo)
        with open(os.path.join(cls.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 0, "claude_command": cls.claude, "replay_timeout_minutes": 5}, f)
        # Stand-ins for the sandbox runtime (bubblewrap, socat) first on PATH, so the replay may start (REP-6).
        sandbox_bin = os.path.join(p.base, "sandbox-bin")
        os.makedirs(sandbox_bin)
        for name, text in SANDBOX_TOOLS.items():
            with open(os.path.join(sandbox_bin, name), "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(os.path.join(sandbox_bin, name), 0o755)
        cls.env = dict(p.env, REGISTA_EVOLVE_HOME=cls.evo)
        cls.env["PATH"] = sandbox_bin + os.pathsep + cls.env.get("PATH", os.environ.get("PATH", ""))
        cls.build = cls.evolve("bench", "build", p.root)
        cls.case = None
        for path in glob.glob(os.path.join(cls.evo, "bench", "*", "*.json")):
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("step") == STEP:
                    cls.case = os.path.basename(path)[:-5]
        cls.before = cls.live_state()
        cls.replay = cls.evolve("replay", cls.case, "--genome", "current") if cls.case else None
        cls.after = cls.live_state()

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

    @classmethod
    def live_state(cls):
        g = cls.p.git
        return {"head": g("rev-parse", "HEAD").stdout, "refs": g("for-each-ref").stdout,
                "all": g("rev-list", "--all").stdout, "worktrees": g("worktree", "list", "--porcelain").stdout,
                "status": g("status", "--porcelain").stdout}

    def setUp(self):
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{self.build.stdout}\n{self.build.stderr}")
        self.assertIsNotNone(self.case, "bench build made no case for the sample step")
        self.assertEqual(self.replay.returncode, 0, f"replay failed:\n{self.replay.stdout}\n{self.replay.stderr}")
        with open(os.path.join(self.control, "calls.jsonl"), encoding="utf-8") as f:
            self.calls = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(self.calls), 1, self.calls)
        self.call = self.calls[0]

    def result(self):
        found = glob.glob(os.path.join(self.evo, "runs", "**", "result.json"), recursive=True)
        self.assertEqual(len(found), 1, f"one result.json under runs/: {found}")
        with open(found[0], encoding="utf-8") as f:
            return json.load(f)

    def test_claude_starts_in_a_fresh_clone_with_genome_brief_model_and_permissions(self):  # covers: REP-1
        call, argv = self.call, self.call["argv"]
        runs = os.path.realpath(os.path.join(self.evo, "runs"))
        self.assertTrue(os.path.realpath(call["cwd"]).startswith(runs + os.sep), call["cwd"])
        self.assertEqual(call["head"], self.parent, "the clone is at the case's parent commit")
        self.assertEqual(call["remotes"], "", "the clone has no remote")
        self.assertTrue(call["frozen"], "the case's test is in the clone when claude starts")
        self.assertTrue(os.path.samefile(flag(argv, "--plugin-dir"), REGISTA_CHECKOUT))
        self.assertEqual(flag(argv, "--model"), "sonnet", "evolve.json's default task_model")
        brief = flag(argv, "-p") or ""
        self.assertIn(REQ_TEXT, brief, "the brief carries the case's requirements")
        self.assertIn("BLOCKED", brief, "the brief says to stop at approval or report BLOCKED")
        self.assertEqual(flag(argv, "--permission-mode"), "acceptEdits")
        self.assertIn("--allowedTools", argv)
        uuid.UUID(flag(argv, "--session-id"))

    def test_stops_at_the_approving_review_and_writes_result(self):  # covers: REP-1
        signals = []
        path = os.path.join(self.control, "signals.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                signals = [json.loads(line)["signal"] for line in f if line.strip()]
        self.assertEqual(signals[:1], ["SIGINT"], "the stand-in keeps running after approving; replay stops it")
        self.assertLess(self.replay.seconds, 80)
        result = self.result()
        self.assertTrue(result.get("outcome"), result)
        self.assertRegex(str(result.get("stop_reason") or ""), re.compile(r"approv|review", re.I))
        self.assertIn(USED, used_tokens(result.get("tokens")), f"tokens, each message once: {result.get('tokens')}")

    def test_clone_deleted_and_live_project_has_no_new_commit(self):  # covers: REP-1
        self.assertFalse(os.path.exists(self.call["cwd"]), "the clone is deleted after the replay")
        for key in self.before:
            self.assertEqual(self.after[key], self.before[key], f"the live project's {key} changed")


if __name__ == "__main__":
    unittest.main()
