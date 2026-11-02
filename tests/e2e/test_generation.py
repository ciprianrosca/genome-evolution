"""End-to-end: one whole generation from the command line (spec .regista/specs/generation.json, LOOP-16).

The sample project of genome/tests/e2e/test_replay.py, with two steps instead of one (multiply, MUL-1, and subtract, SUB-1),
so the bench has one screening case and one held-out case (evolve.json holdout_pct 50, screen_cases 1). This
repository is seeded as the genome `seed`; population is 1 (evolve.json), so the generation makes one child.

The stand-in claude (evolve.json `claude_command`) plays both parts:
- reflection (no --plugin-dir): answers with a diagnosis and this repository's agents/builder.md plus one marker
  sentence, as JSON in a ```json block;
- replay (--plugin-dir <genome's plugin>): when the genome's agents/builder.md holds the marker sentence (the child),
  it builds the step and commits (the commit's Bash call in its transcript), records a passing gate run, passing
  requirements and an approving review that a reviewer subagent recorded with review-record (the Agent call in the
  main transcript, the subagent's own transcript and meta file under <session>/subagents), as tests/test_replay_run.py's
  final_honest stand-in does, so the approval counts; then it waits to be stopped. Otherwise (the seed) it reports
  BLOCKED, which is the seed's failure for the child to aim at. So the child is on the screening front and both the
  base and the child are replayed on the held-out case.
Stand-in bubblewrap and socat come first on PATH, as in test_replay.py. REGISTA_EVOLVE_HOME is a temporary folder.

The command run: `evolve.py generation --yes --budget 1000000 --target agents/builder.md --id e2e-gen --seed 3`
(the spec's command has no --population flag; the population comes from evolve.json), then `evolve.py report e2e-gen`
(spec .regista/specs/report.json, LOOP-8), which writes reports/e2e-gen.md under REGISTA_EVOLVE_HOME and prints it.
The report test assumes the screening and held-out results are Markdown tables whose header names the five axes, that
the lineage is a nested Markdown list, and that numbers are written with thousands separators (1,000,000).

Run: python3 -m unittest discover -s genome/tests/e2e"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from harness import CALC, GENOME, PY, REGISTA_CHECKOUT, SampleProject  # noqa: E402
from test_replay import SANDBOX_TOOLS  # noqa: E402

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
GEN = "e2e-gen"
BUDGET = 1000000
MARKER = "Before you build, read every frozen test of the step once (e2e-generation-marker-73)."

STANDIN = r'''#!@PYTHON@
import json, os, re, signal, subprocess, sys, threading, time
CONTROL = @CONTROL@
REPO = @REPO@
MARKER = @MARKER@
args = sys.argv[1:]
def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
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

plugin = opt("--plugin-dir")
if plugin is None:
    # A reflection: read whatever comes on stdin, then answer with the rewritten builder prompt.
    def slurp():
        try:
            while os.read(0, 65536):
                pass
        except OSError:
            pass
    reader = threading.Thread(target=slurp, daemon=True)
    reader.start()
    reader.join(3)
    log("reflections.jsonl", {"argv": args, "time": time.time()})
    with open(os.path.join(REPO, "agents", "builder.md"), encoding="utf-8") as f:
        text = f.read().rstrip("\n") + "\n\n" + MARKER + "\n"
    body = json.dumps({"diagnosis": "The builder stopped with BLOCKED instead of reading the frozen tests, which "
                                    "answer its question.", "text": text}, indent=2)
    reply = "My analysis.\n\n```json\n" + body + "\n```\n"
    usage = {"input_tokens": 1200, "cache_creation_input_tokens": 60, "output_tokens": 340,
             "cache_read_input_tokens": 5000}
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": reply,
              "session_id": "reflect-e2e", "total_cost_usd": 0, "usage": usage}
    fmt = opt("--output-format")
    if fmt == "stream-json":
        emit({"type": "system", "subtype": "init", "session_id": "reflect-e2e", "apiKeySource": "none", "tools": []})
        emit({"type": "assistant", "session_id": "reflect-e2e", "message": {
            "id": "msg_r", "role": "assistant", "content": [{"type": "text", "text": reply}], "usage": usage}})
        emit(result)
    elif fmt == "json":
        emit(result)
    else:
        sys.stdout.write(reply)
    sys.exit(0)

cwd = os.getcwd()
sid = opt("--session-id") or opt("--resume") or "no-session"
try:
    with open(os.path.join(plugin, "agents", "builder.md"), encoding="utf-8") as f:
        good = MARKER in f.read()
except OSError:
    good = False
log("calls.jsonl", {"argv": args, "cwd": cwd, "plugin": plugin, "good": good, "time": time.time()})
def git(*a):
    return subprocess.run(["git", "-c", "user.name=Stand In", "-c", "user.email=standin@example.com",
                           "-c", "commit.gpgsign=false", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
def result(text="Done."):
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": text,
          "session_id": sid, "total_cost_usd": 0, "modelUsage": {},
          "usage": {"input_tokens": 300, "cache_creation_input_tokens": 30, "output_tokens": 12,
                    "cache_read_input_tokens": 3000}})
def on_signal(signum, frame):
    if signum == signal.SIGINT:
        result()
        os._exit(0)
    os._exit(143)
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)
emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": opt("--model"),
      "apiKeySource": "none", "plugins": [{"name": "regista", "path": plugin}], "plugin_errors": []})
if "--resume" in args:
    result()
    sys.exit(0)

def assistant(mid, text, usage):
    i, c, o, r = usage
    return {"type": "assistant", "session_id": sid, "sessionId": sid,
            "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": i, "cache_creation_input_tokens": c, "output_tokens": o,
                                  "cache_read_input_tokens": r}}}
folder = os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
def say(line):
    with open(os.path.join(folder, sid + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    emit(line)

if not good:
    text = "BLOCKED: the spec doesn't say what the function should do when it is given text instead of numbers."
    say(assistant("msg_blocked", text, (100, 10, 5, 1000)))
    result(text)
    sys.exit(0)

say(assistant("msg_a", "Working.", (100, 10, 5, 1000)))
say(assistant("msg_b", "Working.", (200, 20, 7, 2000)))
def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
def local(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
def tool_call(mid, tid, t, name, inputs, usage=(20, 0, 3, 500)):
    call = assistant(mid, "", usage)
    call["timestamp"] = utc(t)
    call["message"]["content"] = [{"type": "tool_use", "id": tid, "name": name, "input": inputs}]
    return call
def tool_back(tid, t, content, **extra):
    return {"type": "user", "session_id": sid, "sessionId": sid, "timestamp": utc(t),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": content}]}, **extra}

# Build the step and commit it, with the commit's Bash call in the main transcript (so its gate run is backed),
# as tests/test_replay_run.py's final_honest stand-in does.
with open(os.path.join(cwd, "calc.py"), "a", encoding="utf-8") as f:
    f.write("\n\ndef mul(a, b):\n    return a * b\n\n\ndef sub(a, b):\n    return a - b\n")
base = git("rev-parse", "--short", "HEAD")
git("commit", "-q", "-am", "build the step")
head = git("rev-parse", "--short", "HEAD")
t = time.time()
say(tool_call("msg_c", "toolu_commit_1", t - 1, "Bash", {"command": 'git commit -q -am "build the step"'}))
say(tool_back("toolu_commit_1", t + 1, "[main %s] build the step" % head))
reg = os.path.join(cwd, ".regista")
with open(os.path.join(reg, "config.json"), encoding="utf-8") as f:
    gates = [g["name"] for g in json.load(f).get("gates", []) if "manual" not in g.get("when", [])] or ["tests"]
os.makedirs(os.path.join(reg, "gates"), exist_ok=True)
with open(os.path.join(reg, "gates", "history.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"date": local(t), "commit": head,
                        "results": [{"name": g, "exit": 0, "seconds": 0.5, "status": "pass"} for g in gates]}) + "\n")
ids = sorted(set(re.findall(r"\b[A-Z]{2,}-\d+\b", opt("-p") or ""))) or ["MUL-1"]
specs = {}
for name in sorted(os.listdir(os.path.join(reg, "specs"))):
    if name.endswith(".json"):
        with open(os.path.join(reg, "specs", name), encoding="utf-8") as f:
            for q in json.load(f).get("requirements", []):
                specs[q["id"]] = ".regista/specs/" + name
with open(os.path.join(reg, "gates", "requirements.json"), "w", encoding="utf-8") as f:
    json.dump({rid: {"verify": "test", "milestone": "M1", "spec": specs.get(rid, ""), "passes": True,
                     "evidence": ["tests/"], "at": local(t), "base": base} for rid in ids}, f, indent=2)

# The approving review, recorded by a reviewer subagent that runs review-record, as its own transcript shows (FIT-6):
# the orchestrator's Agent call in the main transcript, the subagent's lines and meta file under <session>/subagents.
t = time.time()
fingerprint = "e2e9f1a2b3c4d5e6"
os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
with open(os.path.join(reg, "reviews", ".tmp"), "w", encoding="utf-8") as f:
    json.dump({"verdict": "approve", "base": base, "checked": ids, "files": ["calc.py"], "findings": [],
               "fingerprint": fingerprint, "at": local(t), "summary": "Good."}, f)
os.replace(os.path.join(reg, "reviews", ".tmp"), os.path.join(reg, "reviews", fingerprint + ".json"))
agent = "rv1"
say(tool_call("msg_d", "toolu_agent_rv1", t - 30, "Agent", {
    "subagent_type": "regista:reviewer", "description": "review", "prompt": "Review the step."}))
say(tool_back("toolu_agent_rv1", t + 30, [{"type": "text", "text": "Approved."}],
              toolUseResult={"status": "completed", "agentId": agent, "content": [{"type": "text", "text": "Approved."}]}))
record = 'python3 "%s" review-record' % os.path.join(plugin, "scripts", "regista.py")
use = tool_call("msg_r", "toolu_rv1_01", t - 5, "Bash", {"command": record})
use.update({"agentId": agent, "isSidechain": True})
back = tool_back("toolu_rv1_01", t + 5, "Recorded review approve for 1 file(s), fingerprint %s." % fingerprint,
                 agentId=agent, isSidechain=True)
rows = [{"type": "user", "sessionId": sid, "timestamp": utc(t - 29), "agentId": agent, "isSidechain": True,
         "message": {"role": "user", "content": "Review the step."}}, use, back]
subagents = os.path.join(folder, sid, "subagents")
os.makedirs(subagents, exist_ok=True)
with open(os.path.join(subagents, "agent-%s.jsonl" % agent), "a", encoding="utf-8") as f:
    f.writelines(json.dumps(r) + "\n" for r in rows)
with open(os.path.join(subagents, "agent-%s.meta.json" % agent), "w", encoding="utf-8") as f:
    json.dump({"agentType": "regista:reviewer"}, f)
end = time.time() + 90  # keep running until the replay stops us
while time.time() < end:
    time.sleep(0.1)
result()
'''


def make_test_file(name, func, expected):
    return ("import os\nimport sys\nimport unittest\n\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            f"from calc import {func}  # noqa: E402\n\n\n"
            f"class {name}Test(unittest.TestCase):\n"
            f"    def test_{func}(self):  # covers: {expected}\n"
            f"        self.assertEqual({func}(7, 3), {7 * 3 if func == 'mul' else 7 - 3})\n")


STEPS = [("m1-multiply", "MUL-1", "mul", "Multiply two numbers", "mul(a, b) SHALL return the product of a and b",
          "    return a * b\n"),
         ("m2-subtract", "SUB-1", "sub", "Subtract two numbers", "sub(a, b) SHALL return a minus b",
          "    return a - b\n")]


class OneGeneration(unittest.TestCase):
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
        p.write(".regista/plan.md", "# Sample: plan\n\n## M1 — Maths\n" + "".join(
            f"- [x] {title} ({rid})\n" for _, rid, _, title, _, _ in STEPS))
        code = CALC
        for step, rid, func, title, req, body in STEPS:
            p.write(f".regista/specs/{step}.json", json.dumps({
                "step": step, "milestone": "M1", "title": title, "state": "active",
                "plan_step": f"{title} ({rid})", "interface": f"{func}(a, b) in calc.py.",
                "requirements": [{"id": rid, "text": req, "verify": "test"}]}, indent=1) + "\n")
            p.write("calc.py", code + f"\n\ndef {func}(a, b):\n    raise NotImplementedError\n")
            p.git("add", "-A")
            p.git("commit", "-q", "-m", f"{step}: spec and stub")
            code = code + f"\n\ndef {func}(a, b):\n{body}"
            p.write("calc.py", code)
            p.write(f"tests/test_{func}.py", make_test_file(title.split()[0], func, rid))
            p.git("add", "-A")
            p.git("commit", "-q", "-m", f"{step}: tests")
        cls.claude = os.path.join(cls.control, "claude")
        with open(cls.claude, "w", encoding="utf-8") as f:
            f.write(STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(cls.control))
                    .replace("@REPO@", repr(REGISTA_CHECKOUT)).replace("@MARKER@", repr(MARKER)))
        os.chmod(cls.claude, 0o755)
        os.makedirs(cls.evo)
        with open(os.path.join(cls.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 50, "screen_cases": 1, "population": 1, "claude_command": cls.claude,
                       "replay_timeout_minutes": 5}, f)
        sandbox_bin = os.path.join(p.base, "sandbox-bin")
        os.makedirs(sandbox_bin)
        for name, text in SANDBOX_TOOLS.items():
            with open(os.path.join(sandbox_bin, name), "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(os.path.join(sandbox_bin, name), 0o755)
        cls.env = dict(p.env, REGISTA_EVOLVE_HOME=cls.evo)
        cls.env["PATH"] = sandbox_bin + os.pathsep + cls.env.get("PATH", os.environ.get("PATH", ""))
        cls.build = cls.evolve("bench", "build", p.root)
        cls.cases = {}
        for path in glob.glob(os.path.join(cls.evo, "bench", "*", "*.json")):
            if os.path.basename(path) != "signals.json":
                with open(path, encoding="utf-8") as f:
                    cls.cases[os.path.basename(path)[:-5]] = json.load(f)
        cls.seed = cls.evolve("genome", "seed")
        cls.gen_run = cls.report_run = None
        if cls.build.returncode == 0 and cls.seed.returncode == 0:
            cls.gen_run = cls.evolve("generation", "--yes", "--budget", str(BUDGET), "--target", "agents/builder.md",
                                 "--id", GEN, "--seed", "3")
            if cls.gen_run.returncode == 0:
                cls.report_run = cls.evolve("report", GEN)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["chmod", "-R", "u+w", cls.p.base], capture_output=True)
        cls.p.cleanup()

    @classmethod
    def evolve(cls, *args):
        start = time.time()
        r = subprocess.run([PY, EVOLVE, *args], cwd=cls.p.base, capture_output=True, text=True, encoding="utf-8",
                           env=cls.env, timeout=600, stdin=subprocess.DEVNULL)
        r.seconds = time.time() - start
        return r

    def setUp(self):
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{self.build.stdout}\n{self.build.stderr}")
        self.assertEqual(self.seed.returncode, 0, f"genome seed failed:\n{self.seed.stdout}\n{self.seed.stderr}")
        screening = [c for c, case in self.cases.items() if case.get("screening")]
        holdout = [c for c, case in self.cases.items() if case.get("split") == "holdout"]
        self.assertEqual(len(screening), 1, f"fixture: one screening case: {self.cases}")
        self.assertEqual(len(holdout), 1, f"fixture: one held-out case: {self.cases}")
        self.out = self.gen_run.stdout + self.gen_run.stderr
        self.assertEqual(self.gen_run.returncode, 0, f"generation failed:\n{self.out}")
        self.assertNotIn("Traceback", self.out)
        with open(os.path.join(self.evo, "runs", GEN, "generation.json"), encoding="utf-8") as f:
            self.state = json.load(f)

    def test_completes_within_the_budget_and_writes_generation_json(self):  # covers: LOOP-16
        state = self.state
        children = state.get("children")
        self.assertIsInstance(children, list)
        self.assertEqual(len(children), 1, f"population 1: one child\n{state}\n{self.out}")
        child = children[0]
        self.assertTrue(os.path.isfile(os.path.join(self.evo, "genomes", child, "mutation.json")), child)
        spent = state.get("spent") or {}
        self.assertEqual(spent.get("total"), spent.get("replays", 0) + spent.get("reflections", 0), spent)
        self.assertGreater(spent.get("replays", 0), 0, "the replays' tokens are counted")
        self.assertGreater(spent.get("reflections", 0), 0, "the reflection's tokens are counted")
        self.assertLessEqual(spent["total"], BUDGET, "within the budget")
        self.assertEqual(state.get("budget"), BUDGET)
        self.assertNotEqual(state.get("stopped"), "budget")
        front = state.get("front")
        self.assertIsInstance(front, list)
        self.assertTrue(front, "the front has at least one genome")
        self.assertTrue(set(front) <= {"seed", child}, front)
        self.assertIn("seed", state.get("screening") or {})
        self.assertIn(child, state.get("screening") or {})
        self.assertIn("seed", state.get("holdout") or {}, "the base is replayed on the held-out case")
        self.assertIn("winner", state)
        if state["winner"] is None:
            self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip(), state)
        else:
            self.assertEqual(state["winner"], child, "the child that builds the step beats the blocked seed")
        with open(os.path.join(self.control, "reflections.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len([line for line in f if line.strip()]), 1, "one reflection")

    def test_prints_a_plain_summary(self):  # covers: LOOP-16
        out = self.gen_run.stdout
        child = self.state["children"][0]
        self.assertRegex(out, rf"(?<![\w-]){re.escape(child)}(?![\w-])", f"the summary names the child:\n{out}")
        budget_forms = {str(BUDGET), f"{BUDGET:,}", f"{BUDGET:,}".replace(",", " "), "1 million", "1.0 million"}
        self.assertTrue(any(form in out for form in budget_forms), f"the summary gives the budget:\n{out}")
        self.assertRegex(out, r"(?i)token", out)
        self.assertRegex(out, r"(?i)front", out)
        self.assertRegex(out, r"(?i)held[- ]out", out)
        if self.state["winner"]:
            self.assertRegex(out, rf"(?i)winner[^\n]*{re.escape(child)}|{re.escape(child)}[^\n]*win", out)
        else:
            self.assertRegex(out, r"(?i)no winner", out)
        for line in out.splitlines():
            self.assertNotRegex(line.strip(), r"^[\[{]", f"plain sentences, not a data dump: {line}")

    # LOOP-8: the generation report -----------------------------------------------------------------------------------

    @staticmethod
    def first_paragraph(text):
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = [x.strip() for x in block.splitlines() if x.strip() and not x.lstrip().startswith("#")]
            if lines:
                return " ".join(lines)
        return ""

    @staticmethod
    def tables(text):
        """Every Markdown table: a list of rows, each a list of its cells, header row first."""
        found, rows = [], []
        for line in text.splitlines() + [""]:
            if line.strip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
            elif rows:
                found.append(rows)
                rows = []
        return found

    @staticmethod
    def has(cells_or_text, name):
        text = " | ".join(cells_or_text) if isinstance(cells_or_text, list) else cells_or_text
        return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None

    def tokens_of(self, case, genome):
        with open(os.path.join(self.evo, "runs", GEN, case, genome, "result.json"), encoding="utf-8") as f:
            return json.load(f)["tokens"]["used"]

    def assert_tokens_in(self, row, n, what):
        n = int(round(n))
        self.assertTrue(any(form in " | ".join(row) for form in (f"{n:,}", str(n), f"{n:,}".replace(",", " "))),
                        f"{what}: {n} tokens in {row}")

    def test_report_tells_the_generation_to_a_cold_reader(self):  # covers: LOOP-8
        r = self.report_run
        self.assertIsNotNone(r, "the report ran after the generation")
        out = r.stdout
        self.assertEqual(r.returncode, 0, f"report failed:\n{r.stdout}\n{r.stderr}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        path = os.path.join(self.evo, "reports", GEN + ".md")
        self.assertTrue(os.path.isfile(path), f"reports/{GEN}.md is written:\n{out}")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(text.strip(), out, "the command prints the same text it writes")
        state = self.state
        child = state["children"][0]
        winner = state.get("winner")
        with open(os.path.join(self.evo, "genomes", child, "mutation.json"), encoding="utf-8") as f:
            mutation = json.load(f)
        screening = next(c for c, case in self.cases.items() if case.get("screening"))
        holdout = next(c for c, case in self.cases.items() if case.get("split") == "holdout")
        project = self.cases[screening]["project"]

        # The outcome comes first.
        first = self.first_paragraph(text)
        if winner:
            self.assertTrue(self.has(first, winner), f"the first paragraph names the winner:\n{first}")
            self.assertRegex(first, r"(?i)\bw[io]n", first)
            self.assertRegex(first, r"(?i)because|better|than|more|fewer|beat", f"and says why:\n{first}")
        else:
            self.assertRegex(first, r"(?i)no winner|nothing won|no (child|genome|candidate)",
                             f"the first paragraph says there is no winner:\n{first}")

        # What was evolved, the budget, the spend and why it stopped.
        self.assertIn("agents/builder.md", text)
        self.assertTrue(self.has(text, "seed"), "names the base genome")
        self.assertIn(project, text, "names the project")
        spent = state["spent"]
        self.assertIn(f"{BUDGET:,}", text, "the budget, with thousands separators")
        for key in ("replays", "reflections", "total"):
            self.assertIn(f"{spent[key]:,}", text, f"the tokens spent on {key} ({spent[key]:,})")
        if state.get("stopped"):
            self.assertRegex(text, r"(?i)" + re.escape(state["stopped"].split()[0]), "why it stopped")
        else:
            self.assertRegex(text, r"(?i)population|every child|all .*children|finished|completed",
                             "why it stopped: the population was reached")

        # Every screened genome's axes, with the front marked; then the held-out results on the same axes.
        axes = [r"(?i)requirement", r"(?i)approv", r"(?i)review", r"(?i)finding", r"(?i)token"]
        axis_tables = [t for t in self.tables(text) if all(re.search(a, " ".join(t[0])) for a in axes)]
        self.assertGreaterEqual(len(axis_tables), 2, f"a screening table and a held-out table on the five axes:\n{text}")
        screen_table = axis_tables[0]
        rows = {}
        for genome in ("seed", child):
            mine = [row for row in screen_table[1:] if self.has(row, genome)
                    and (genome != "seed" or not self.has(row, child))]
            self.assertEqual(len(mine), 1, f"{genome} has one row in the screening table: {screen_table}")
            rows[genome] = mine[0]
        self.assertTrue(self.has(rows[child][1:] if self.has(rows[child][:1], child) else rows[child], "seed"),
                        f"the child's row names its parent: {rows[child]}")
        summary = re.sub(r"\s+", " ", mutation["failure"]["summary"]).strip()[:25]
        self.assertIn(summary, re.sub(r"\s+", " ", " ".join(rows[child])).replace("\\|", "|"),
                      f"the child's row gives the failure it aimed at: {rows[child]}")
        self.assert_tokens_in(rows[child], self.tokens_of(screening, child), "the child's screening tokens")
        self.assert_tokens_in(rows["seed"], self.tokens_of(screening, "seed"), "the base's screening tokens")
        front = state["front"]
        header = screen_table[0]
        column = next((i for i, c in enumerate(header) if re.search(r"(?i)front", c)), None)
        if column is not None:
            marks = {g: rows[g][column] if column < len(rows[g]) else "" for g in rows}
            for g in rows:
                on = marks[g] not in ("", "no", "No", "-", "—")
                self.assertEqual(on, g in front, f"the front column marks exactly the front ({front}): {marks}")
        else:
            marker = re.compile(r"(?i)front|★|☆|✓|✔|\*")
            for g in rows:
                self.assertEqual(bool(marker.search(" ".join(rows[g]))), g in front,
                                 f"the rows of the front ({front}) are marked, the others aren't: {rows[g]}")
        self.assertRegex(text, r"(?i)front", "the marking is explained")
        self.assertRegex(text, r"(?i)held[- ]out", text)
        self.assertIn(holdout, text, "names the held-out case")
        held = [t for t in axis_tables[1:] if all(any(self.has(row, g) for row in t[1:]) for g in state["holdout"])]
        self.assertTrue(held, f"a table on the five axes holds every genome replayed on the held-out case "
                              f"({sorted(state['holdout'])}):\n{text}")
        for genome in state["holdout"]:
            row = next(row for row in held[0][1:] if self.has(row, genome)
                       and (genome != "seed" or not self.has(row, child)))
            self.assert_tokens_in(row, self.tokens_of(holdout, genome), f"{genome}'s held-out tokens")

        # The lineage: each child under its parent, as an indented list.
        items = [(len(m.group(1).expandtabs(4)), line) for line in text.splitlines()
                 for m in [re.match(r"^(\s*)(?:[-*+]|\d+\.)\s", line)] if m]
        seed_items = [i for i, (_, line) in enumerate(items) if self.has(line, "seed") and not self.has(line, child)]
        self.assertTrue(seed_items, f"the lineage lists the base:\n{text}")
        nested = any(any(self.has(line, child) and indent > items[s][0] for indent, line in items[s + 1:])
                     for s in seed_items)
        self.assertTrue(nested, f"the lineage lists the child indented under the base:\n{text}")

        # The discarded tries (none here), then the winner's diagnosis and diff, or why there is no winner.
        self.assertRegex(text, r"(?i)discard", "the discarded tries have their section, even when empty")
        if winner:
            with open(os.path.join(self.evo, "genomes", winner, "mutation.json"), encoding="utf-8") as f:
                diagnosis = re.sub(r"\s+", " ", json.load(f)["diagnosis"]).strip()
            self.assertIn(diagnosis[:60], re.sub(r"\s+", " ", text), "the winner's diagnosis")
            self.assertIn("+" + MARKER, text.splitlines(), "the winner's diff against the base adds its sentence")
            self.assertRegex(text, r"(?m)^@@ ", "as a unified diff")
        else:
            self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip())
            self.assertRegex(text, r"(?i)no winner", "why there is no winner")

        # Plain text for a person: no data dumps outside the diff.
        inside = False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if not inside:
                self.assertNotRegex(line.strip(), r"^[\[{]", f"plain sentences, not a data dump: {line}")


if __name__ == "__main__":
    unittest.main()
