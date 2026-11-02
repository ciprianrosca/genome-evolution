"""Reflection and aimed mutations (spec .regista/specs/reflect-mutate.json: LOOP-6, LOOP-10, LOOP-11; LOOP-12 is the
end-to-end test in genome/tests/e2e/test_reflect_mutate.py).

failures(home, generation, case, genome) in evolve.py turns one replay's folder, runs/<generation>/<case>/<genome>/,
into a list of discrete failures. mutate(home, config, profile, source, parent, failure, target, child_id) asks
claude (a stand-in here, evolve.json `claude_command`) why the run failed and to rewrite one evolvable file against
that one failure, then makes the child with make_child and records genomes/<child>/mutation.json.
mutate_trait_aimed(home, profile, source, parent, failure, trait, child_id, rng) does the same for one trait, with
mutate_trait and no claude.

The replay folders are written by hand in the shape real replays leave: result.json (outcome, stop_reason,
hard_failure, final_gates {gate: {status, exit, tail}}, scores.requirements {passing, not_proven, of},
scores.correct_blocked {blocked}), copies/reviews/*.json (verdict and findings {severity, file, line, issue, fix}),
copies/gates/history.jsonl and stream.jsonl. The genome is Regista's real profile over the fake Regista checkout of
tests/test_genome_seed.py. Everything lives in temporary folders (REGISTA_EVOLVE_HOME and HOME included).

The stand-in claude records its arguments, the names of its environment variables and its standard input, then
answers with the reply in its control folder: as one JSON result object with `--output-format json`, as stream
events with `--output-format stream-json`, and as plain text otherwise. Its usage is input 1200, cache creation
60, output 340 and cache reads 5000 tokens.

Interface assumed: config is evolve.load_config(home); profile is genome.load_profile(...); failure is one of the
dicts failures() returns; errors are plain GenomeError or EvolveError; a BLOCKED report is the text claude ended
with (stream.jsonl's result event and last assistant message), and result.json marks it with outcome "blocked" and
scores.correct_blocked.blocked true.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_reflect_mutate -v"""
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_genome_seed import (EVOLVABLE, PROFILE as REGISTA_PROFILE, build_checkout, import_scripts,  # noqa: E402
                              is_plain_error, read_bytes, snapshot, write)

PY = sys.executable
GEN = "g1"

STANDIN = r'''#!@PYTHON@
import json, os, sys, threading, time, uuid
CONTROL = @CONTROL@

def read(name, default=""):
    try:
        with open(os.path.join(CONTROL, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default

def log(name, entry):
    with open(os.path.join(CONTROL, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

args = sys.argv[1:]
def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None

if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v", "--help", "-h") for a in args):
    log("probes.jsonl", {"argv": args})
    print("2.1.312 (Claude Code)")
    sys.exit(0)

chunks = []
def slurp():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            chunks.append(data)
    except OSError:
        pass
reader = threading.Thread(target=slurp, daemon=True)
reader.start()
reader.join(3)
stdin = b"".join(chunks).decode("utf-8", "replace")

files = {}
for a in args:
    try:
        if len(a) < 4096 and os.path.isfile(a):
            with open(a, encoding="utf-8") as f:
                files[a] = f.read()
    except (OSError, UnicodeDecodeError, ValueError):
        pass
log("calls.jsonl", {"argv": args, "env": sorted(os.environ), "stdin": stdin, "files": files, "time": time.time()})

sleep = float(read("sleep", "0") or 0)
if sleep:
    time.sleep(sleep)

reply = read("reply.txt")
code = int(read("exit", "0") or 0)
sid = str(uuid.uuid4())
usage = {"input_tokens": 1200, "cache_creation_input_tokens": 60, "output_tokens": 340,
         "cache_read_input_tokens": 5000}
result = {"type": "result", "subtype": "success" if code == 0 else "error_during_execution", "is_error": code != 0,
          "num_turns": 1, "result": reply if code == 0 else "", "session_id": sid, "duration_ms": 900,
          "total_cost_usd": 0, "modelUsage": {}, "usage": usage}
fmt = opt("--output-format")
if code != 0:
    sys.stderr.write("stand-in claude: the service is overloaded\n")
if fmt == "stream-json":
    for event in ({"type": "system", "subtype": "init", "session_id": sid, "model": opt("--model"),
                   "apiKeySource": "none", "tools": []},
                  {"type": "assistant", "session_id": sid,
                   "message": {"id": "msg_reflect", "type": "message", "role": "assistant", "model": "stand-in",
                               "content": [{"type": "text", "text": reply if code == 0 else ""}], "usage": usage}},
                  result):
        sys.stdout.write(json.dumps(event) + "\n")
elif fmt == "json":
    sys.stdout.write(json.dumps(result) + "\n")
elif code == 0:
    sys.stdout.write(reply)
sys.stdout.flush()
sys.exit(code)
'''

# API credentials and the parent session's variables: none may reach the reflection's claude (COST-1, as replays).
POISON = {
    "ANTHROPIC_API_KEY": "sk-ant-test-not-real", "ANTHROPIC_AUTH_TOKEN": "token-not-real",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
}
CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
               "CLAUDE_CODE_USE_FOUNDRY")

REFLECTION_MODEL = "opus-reflect-test"
BUILDER = EVOLVABLE["agents/builder.md"]
# The sample builder prompt is 74 bytes, so its cap is 111 (50% growth, inclusive). Each rewrite below adds 21 or 22 bytes
# (95 or 96 in all), comfortably inside the cap; over_cap in the failure test stays far over it.
NEW_BUILDER = BUILDER + "Test each edge case.\n"
OTHER_BUILDER = BUILDER + "Run gates pre-review.\n"
DIAGNOSIS = "The builder never checked the empty input the spec names, so the reviewer found it diagnosis-q7."
TOKEN_NUMBERS = {1540, 1600, 6600}  # input plus output, plus cache creation, plus cache reads

CASE = "m1-double"
PASS_GATES = {"tests": {"status": "pass", "exit": 0, "seconds": 1.2, "tail": "OK"},
              "e2e": {"status": "pass", "exit": 0, "seconds": 3.4, "tail": "OK"}}


def finding(severity, issue, line, file="src/double.py"):
    return {"severity": severity, "file": file, "line": line, "issue": issue, "fix": "Fix it."}


def write_json(path, data):
    write(path, json.dumps(data, indent=2) + "\n")


def write_replay(evo, case, genome, outcome="approved", final_gates=None, not_proven=(), hard_failure=None,
                 reviews=None, history=None, final_text="Done: the step is approved.", blocked=False,
                 generation=GEN):
    """A finished replay's folder, runs/<generation>/<case>/<genome>/, as the replay leaves it."""
    folder = os.path.join(evo, "runs", generation, case, genome)
    ids = ["DBL-1", "DBL-2", "DBL-3"]
    gates = PASS_GATES if final_gates is None else final_gates
    failed_gates = [g for g, r in gates.items() if r.get("status") != "pass"]
    approving = any(r.get("verdict") == "approve" for r in (reviews or {}).values())
    stop = {"approved": "an approving review was recorded and the latest gate run passed",
            "blocked": "claude reported BLOCKED",
            "hard_failure": "a hard failure stopped the replay",
            "ended": "claude ended (exit 0) without an approving review with passing gates, a BLOCKED report or a "
                     "hard failure"}[outcome]
    result = {
        "project": "doubler", "case": case, "genome": genome, "generation": generation, "model": "haiku",
        "session_id": "11111111-2222-3333-4444-555555555555", "outcome": outcome, "stop_reason": stop,
        "hard_failure": hard_failure, "final_gates": gates, "claude_exit": 0, "num_turns": 9, "problems": [],
        "started_at": "2026-10-30T10:00:00", "ended_at": "2026-10-30T10:20:00",
        "tokens": {"used": 50000, "input_tokens": 40, "cache_creation_input_tokens": 40000, "output_tokens": 9960},
        "scores": {
            "gates": {"value": 0 if failed_gates else 1, "failed": bool(failed_gates),
                      "source": "the evolver's final gate run", "results": {g: r["status"] for g, r in gates.items()},
                      "better": "higher"},
            "requirements": {"value": (len(ids) - len(not_proven)) / len(ids), "failed": bool(not_proven),
                             "passing": [i for i in ids if i not in not_proven], "not_proven": list(not_proven),
                             "of": ids, "better": "higher"},
            "review_rounds": {"value": len(reviews or {}), "failed": not approving, "better": "lower"},
            "correct_blocked": {"value": 0 if blocked else 1, "failed": blocked, "blocked": blocked,
                                "should_block": False, "better": "higher"},
        },
    }
    write_json(os.path.join(folder, "result.json"), result)
    for name, review in (reviews or {}).items():
        write_json(os.path.join(folder, "copies", "reviews", f"{name}.json"),
                   {"verdict": review["verdict"], "summary": "Reviewed.", "checked": ids, "files": ["src/double.py"],
                    "findings": review["findings"], "fingerprint": name, "at": "2026-10-30T10:10:00",
                    "base": "abc1234"})
    rows = history if history is not None else [
        {"date": "2026-10-30T10:05:00", "commit": "abc1234",
         "results": [{"name": g, "exit": r["exit"], "seconds": 1.0, "status": r["status"]} for g, r in gates.items()]}]
    write(os.path.join(folder, "copies", "gates", "history.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
    sid = result["session_id"]
    stream = [{"type": "system", "subtype": "init", "session_id": sid, "model": "haiku"},
              {"type": "assistant", "session_id": sid,
               "message": {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": final_text}]}},
              {"type": "result", "subtype": "success", "is_error": False, "result": final_text, "session_id": sid}]
    write(os.path.join(folder, "stream.jsonl"), "".join(json.dumps(e) + "\n" for e in stream))
    return folder


def numbers_in(value):
    """Every number anywhere in value."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {value}
    if isinstance(value, dict):
        return set().union(*(numbers_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(numbers_in(v) for v in value)) if value else set()
    return set()


class Base(unittest.TestCase):
    """A temporary evolver home with the fake Regista checkout seeded as `seed`, and a stand-in claude."""

    def setUp(self):
        self.gn = import_scripts("genome")
        self.ev = import_scripts("evolve")
        self.tmp = tempfile.mkdtemp(prefix="regista-reflect-mutate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.evo)
        env = mock.patch.dict(os.environ, {"HOME": self.home, "REGISTA_EVOLVE_HOME": self.evo})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.checkout, _ = build_checkout(self.tmp, use_git=False)
        self.profile = self.gn.load_profile(REGISTA_PROFILE, source=self.checkout)
        self.gn.seed(self.evo, self.profile, self.checkout, "seed")
        self.control = os.path.join(self.tmp, "control")
        os.makedirs(self.control)
        self.claude = os.path.join(self.tmp, "bin", "claude")
        write(self.claude, STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control)))
        os.chmod(self.claude, 0o755)
        self.configure()

    def configure(self, **extra):
        cfg = {"holdout_pct": 0, "claude_command": self.claude, "task_model": "haiku",
               "reflection_model": REFLECTION_MODEL, **extra}
        cfg = {k: v for k, v in cfg.items() if v is not None}
        write_json(os.path.join(self.evo, "evolve.json"), cfg)
        self.config = self.ev.load_config(self.evo)

    def answer(self, reply=None, diagnosis=DIAGNOSIS, text=NEW_BUILDER, exit_code=0, sleep=0, wrap=True):
        """What the stand-in answers next: the reply as given, or a JSON {diagnosis, text} (in a ```json block)."""
        if reply is None:
            body = json.dumps({"diagnosis": diagnosis, "text": text}, indent=2)
            reply = f"Here is my analysis.\n\n```json\n{body}\n```\n" if wrap else body
        for name, value in (("reply.txt", reply), ("exit", str(exit_code)), ("sleep", str(sleep))):
            write(os.path.join(self.control, name), value)

    def calls(self):
        path = os.path.join(self.control, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def failures(self, case=CASE, genome="seed"):
        return self.ev.failures(self.evo, GEN, case, genome)

    def review_replay(self):
        """A replay of the seed with two review findings that count (a major one and a blocker) and a minor one."""
        write_replay(self.evo, CASE, "seed", outcome="ended", reviews={
            "r1": {"verdict": "changes", "findings": [
                finding("major", "double('') crashes instead of raising ValueError marker-major-41.", 7),
                finding("minor", "A comment is out of date marker-minor-77.", 2),
                finding("blocker", "The frozen test was edited marker-blocker-12.", 30, "tests/test_double.py")]}})
        found = self.failures()
        by_marker = {}
        for f in found:
            for marker in ("marker-major-41", "marker-blocker-12"):
                if marker in f.get("evidence", ""):
                    by_marker[marker] = f
        self.assertEqual(sorted(by_marker), ["marker-blocker-12", "marker-major-41"], found)
        return by_marker["marker-major-41"], by_marker["marker-blocker-12"]

    def genome_dir(self, genome_id):
        return os.path.join(self.evo, "genomes", genome_id)

    def read_json(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as f:
            return json.load(f)

    def mutate(self, failure, target="agents/builder.md", child="seed-m1"):
        return self.ev.mutate(self.evo, self.config, self.profile, self.checkout, "seed", failure, target, child)


# LOOP-10: failures --------------------------------------------------------------------------------------------------

class Failures(Base):
    KINDS = {"hard_failure", "blocked", "review_finding", "gate_failure", "requirement_not_proven"}

    def assert_well_formed(self, found, case):
        ids = [f.get("id") for f in found]
        self.assertEqual(len(set(ids)), len(ids), f"every failure has its own id: {ids}")
        for f in found:
            with self.subTest(failure=f.get("id")):
                self.assertTrue(isinstance(f.get("id"), str) and f["id"].strip(), f"a string id: {f}")
                self.assertNotRegex(f["id"], r"\s", "an id is one word, so it can be passed on the command line")
                self.assertIn(f.get("kind"), self.KINDS, f)
                self.assertEqual(f.get("case"), case, f)
                summary = f.get("summary")
                self.assertTrue(isinstance(summary, str) and summary.strip(), f"a summary: {f}")
                self.assertNotIn("\n", summary.strip(), f"the summary is one sentence, on one line: {summary!r}")
                self.assertLessEqual(len(summary), 400, f"the summary is one sentence: {summary!r}")
                self.assertNotRegex(summary, r"^\s*[\[{]", f"the summary is plain words, not a data dump: {summary!r}")
                self.assertIsInstance(f.get("evidence"), str, f)
                self.assertLessEqual(len(f["evidence"]), 4000, f"evidence is capped at 4000 characters: {f['id']}")

    def test_blocker_and_major_findings_are_failures_and_minor_ones_are_not(self):  # covers: LOOP-10
        long_issue = "The parser loses the sign of negative numbers marker-long-55. " + "More detail. " * 700
        write_replay(self.evo, CASE, "seed", outcome="ended", reviews={
            "r1": {"verdict": "changes", "findings": [
                finding("major", "double('') crashes instead of raising ValueError marker-major-41.", 7),
                finding("minor", "A comment is out of date marker-minor-77.", 2),
                finding("blocker", "The frozen test was edited marker-blocker-12.", 30, "tests/test_double.py")]},
            "r2": {"verdict": "changes", "findings": [finding("major", long_issue, 19, "src/parse.py"),
                                                      finding("minor", "Rename a variable marker-minor-78.", 4)]}})
        found = self.failures()
        self.assert_well_formed(found, CASE)
        self.assertEqual([f["kind"] for f in found], ["review_finding"] * 3,
                         f"two majors and a blocker over two rounds; minors never count: {found}")
        evidence = "\n".join(f["evidence"] for f in found)
        self.assertNotIn("marker-minor", evidence, "minor findings are not failures")
        for marker, place in (("marker-major-41", "src/double.py:7"), ("marker-blocker-12", "tests/test_double.py:30"),
                              ("marker-long-55", "src/parse.py:19")):
            with self.subTest(marker=marker):
                match = [f for f in found if marker in f["evidence"]]
                self.assertEqual(len(match), 1, f"one failure per finding, holding its text: {found}")
                self.assertIn(place, match[0]["evidence"], "the evidence gives the finding's file:line")
        self.assertEqual([f["id"] for f in self.failures()], [f["id"] for f in found],
                         "listing the same replay again gives the same ids")

    def test_a_failing_final_gate_is_one_failure_with_its_output_tail(self):  # covers: LOOP-10
        tail = "x" * 9000 + "\nFAIL: test_negative (tests.e2e.test_cli) marker-gate-tail-63\n"
        gates = {"tests": {"status": "pass", "exit": 0, "seconds": 1.0, "tail": "OK"},
                 "e2e": {"status": "fail", "exit": 1, "seconds": 2.0, "tail": tail}}
        write_replay(self.evo, "m2-gates", "seed", outcome="ended", final_gates=gates)
        found = self.failures("m2-gates")
        self.assert_well_formed(found, "m2-gates")
        self.assertEqual([f["kind"] for f in found], ["gate_failure"], found)
        self.assertIn("e2e", found[0]["summary"] + found[0]["evidence"], "the failure names the gate")
        self.assertIn("marker-gate-tail-63", found[0]["evidence"], "the evidence is the end of the gate's output")

    def test_each_unproven_requirement_is_one_failure(self):  # covers: LOOP-10
        write_replay(self.evo, "m3-reqs", "seed", outcome="ended", not_proven=("DBL-2", "DBL-3"))
        found = self.failures("m3-reqs")
        self.assert_well_formed(found, "m3-reqs")
        self.assertEqual([f["kind"] for f in found], ["requirement_not_proven"] * 2, found)
        for rid in ("DBL-2", "DBL-3"):
            self.assertEqual(len([f for f in found if rid in f["summary"] + f["evidence"]]), 1,
                             f"one failure names {rid}: {found}")

    def test_a_blocked_outcome_is_one_failure_holding_the_report(self):  # covers: LOOP-10
        report = "BLOCKED: DBL-1 and DBL-2 contradict each other about negative numbers marker-blocked-88."
        write_replay(self.evo, "m4-blocked", "seed", outcome="blocked", blocked=True, final_text=report)
        found = self.failures("m4-blocked")
        self.assert_well_formed(found, "m4-blocked")
        self.assertEqual([f["kind"] for f in found], ["blocked"], found)
        self.assertIn("marker-blocked-88", found[0]["evidence"], "the evidence is the BLOCKED report")

    def test_a_hard_failure_is_one_failure_holding_its_reason(self):  # covers: LOOP-10
        reason = "a real push_policy refusal: the agent tried to push to origin marker-hard-21"
        write_replay(self.evo, "m5-hard", "seed", outcome="hard_failure", hard_failure=reason)
        found = self.failures("m5-hard")
        self.assert_well_formed(found, "m5-hard")
        self.assertEqual([f["kind"] for f in found], ["hard_failure"], found)
        self.assertIn("marker-hard-21", found[0]["evidence"], "the evidence is the hard failure's reason")

    def test_an_approved_replay_with_passing_gates_and_every_requirement_proven_has_none(self):  # covers: LOOP-10
        history = [{"date": "2026-10-30T10:05:00", "commit": "abc1234",
                    "results": [{"name": "tests", "exit": 1, "seconds": 1.0, "status": "fail"}]},
                   {"date": "2026-10-30T10:15:00", "commit": "abc1234",
                    "results": [{"name": "tests", "exit": 0, "seconds": 1.0, "status": "pass"},
                                {"name": "e2e", "exit": 0, "seconds": 1.0, "status": "pass"}]}]
        write_replay(self.evo, "m6-clean", "seed", outcome="approved", history=history, reviews={
            "r1": {"verdict": "approve", "findings": [finding("minor", "A comment could be clearer.", 3)]}})
        self.assertEqual(self.failures("m6-clean"), [],
                         "an approved replay with passing final gates and every requirement proven has no failures, "
                         "whatever its minor findings or the gate failures it fixed along the way")


# LOOP-6: one aimed mutation, recorded --------------------------------------------------------------------------------

class AimedMutation(Base):
    def test_rewrite_targets_one_failure_and_records_it_in_mutation_json(self):  # covers: LOOP-6
        major, _blocker = self.review_replay()
        self.answer()
        self.mutate(major)
        child = self.genome_dir("seed-m1")
        self.assertEqual(read_bytes(os.path.join(child, "files", "agents", "builder.md")), NEW_BUILDER.encode(),
                         "the child holds the rewritten target")
        for rel, text in EVOLVABLE.items():
            if rel != "agents/builder.md":
                self.assertEqual(read_bytes(os.path.join(child, "files", rel)), text.encode(), f"{rel} unchanged")
        self.assertEqual(self.read_json(child, "genome.json").get("parents"), ["seed"], "made from its parent")
        record = self.read_json(child, "mutation.json")
        self.assertEqual(set(record), {"parent", "kind", "target", "failure", "diagnosis", "model", "tokens",
                                       "created_at"}, record)
        self.assertEqual(record["parent"], "seed")
        self.assertEqual(record["kind"], "rewrite")
        self.assertEqual(record["target"], "agents/builder.md")
        self.assertEqual(record["failure"], {k: major[k] for k in ("id", "kind", "case", "summary")},
                         "the one failure it targets: id, kind, case and summary")
        self.assertEqual(record["diagnosis"], DIAGNOSIS)
        self.assertEqual(record["model"], REFLECTION_MODEL)
        found = numbers_in(record["tokens"])
        self.assertTrue(found & TOKEN_NUMBERS or {1200, 340} <= found,
                        f"tokens from claude's result (input 1200, output 340): {record['tokens']}")
        self.assertTrue(record["created_at"])
        # The prompt is aimed at that one failure: its evidence is there, the other finding's isn't.
        calls = self.calls()
        self.assertEqual(len(calls), 1, "claude was started once")
        prompt = "\n".join(calls[0]["argv"]) + "\n" + calls[0]["stdin"] + "\n" + "\n".join(calls[0]["files"].values())
        self.assertIn("marker-major-41", prompt, "the prompt holds the targeted failure's evidence")
        self.assertIn(major["summary"], prompt, "the prompt holds the targeted failure's summary")
        self.assertNotIn("marker-blocker-12", prompt, "one failure at a time: not the other finding")
        self.assertIn("You are the builder.", prompt, "the prompt holds the target's current text")
        cap = len(BUILDER.encode()) * 3 // 2
        self.assertRegex(prompt, rf"(?<!\d)(50 ?%|50 percent|{cap}|{cap + 1}|{cap - 1})(?!\d)",
                         "the prompt states the target's size cap (50% growth for agents/*.md)")

    def test_trait_mutation_records_the_trait_and_the_failure_without_claude(self):  # covers: LOOP-6
        major, _blocker = self.review_replay()
        parent = self.read_json(self.genome_dir("seed"), "genome.json")["traits"]
        self.ev.mutate_trait_aimed(self.evo, self.profile, self.checkout, "seed", major, "review.max_rounds",
                                   "seed-t1", random.Random(7))
        child = self.genome_dir("seed-t1")
        traits = self.read_json(child, "genome.json")["traits"]
        self.assertNotEqual(traits["review.max_rounds"], parent["review.max_rounds"], "the named trait moved")
        self.assertEqual({k: v for k, v in traits.items() if k != "review.max_rounds"},
                         {k: v for k, v in parent.items() if k != "review.max_rounds"}, "no other trait changed")
        record = self.read_json(child, "mutation.json")
        self.assertEqual(record.get("parent"), "seed")
        self.assertEqual(record.get("kind"), "trait")
        self.assertEqual(record.get("trait"), "review.max_rounds")
        self.assertNotIn("target", record, "a trait mutation names its trait instead of a target file")
        self.assertEqual(record.get("failure"), {k: major[k] for k in ("id", "kind", "case", "summary")})
        self.assertTrue(record.get("created_at"))
        self.assertEqual(self.calls(), [], "no claude is started for a trait mutation")


# LOOP-11: how reflection runs, and every way it can fail -------------------------------------------------------------

class Reflection(Base):
    def test_claude_starts_once_with_the_reflection_model_no_tools_and_no_credentials(self):  # covers: LOOP-11
        major, _blocker = self.review_replay()
        for model, child in ((REFLECTION_MODEL, "seed-m1"), (None, "seed-m2")):
            with self.subTest(reflection_model=model or "the default"):
                self.configure(reflection_model=model)
                if os.path.exists(os.path.join(self.control, "calls.jsonl")):
                    os.remove(os.path.join(self.control, "calls.jsonl"))
                self.answer(text=NEW_BUILDER if model else OTHER_BUILDER)
                with mock.patch.dict(os.environ, POISON):
                    self.mutate(major, child=child)
                calls = self.calls()
                self.assertEqual(len(calls), 1, f"claude was started exactly once: {calls}")
                argv = calls[0]["argv"]
                model_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--model=")), None)
                if "--model" in argv:
                    model_arg = argv[argv.index("--model") + 1]
                self.assertEqual(model_arg, model or "opus", "--model is evolve.json reflection_model (default opus)")
                self.assertTrue(no_tools(argv), f"claude runs with no tools: {argv}")
                for name in CREDENTIALS:
                    self.assertNotIn(name, calls[0]["env"], f"{name} is removed from claude's environment")

    def assert_no_child(self, call, words, what, before):
        with self.assertRaises(Exception, msg=what) as caught:
            call()
        self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain error, got {caught.exception!r}")
        message = str(caught.exception)
        for pattern in ([words] if isinstance(words, str) else words):
            self.assertRegex(message, pattern, f"{what}: the error says which: {message}")
        self.assertEqual(snapshot(os.path.join(self.evo, "genomes")), before, f"{what}: nothing under genomes/")
        return message

    def test_every_failed_reflection_writes_no_child_and_says_why(self):  # covers: LOOP-11
        major, _blocker = self.review_replay()
        reviewer = EVOLVABLE["agents/reviewer.md"]
        over_cap = BUILDER + "Always explain every decision at length. " * 20 + "\n"
        cap_words = r"(?i)cap|grow|bytes|size|large|%"
        cases = [
            ("claude exits non-zero", dict(exit_code=3), "agents/builder.md", [r"(?i)exit|fail"]),
            ("an answer with no JSON", dict(reply="I think the builder should be more careful.\n"),
             "agents/builder.md", [r"(?i)json"]),
            ("a broken JSON block", dict(reply='```json\n{"diagnosis": "x", "text": \n```\n'), "agents/builder.md",
             [r"(?i)json"]),
            ("JSON without the text", dict(reply=json.dumps({"diagnosis": "x"})), "agents/builder.md",
             [r"(?i)json|text"]),
            ("the parent's text unchanged", dict(text=BUILDER), "agents/builder.md",
             [r"(?i)identical|unchanged|same|no change|did ?n[o']t change"]),
            ("a rewrite over the size cap", dict(text=over_cap), "agents/builder.md",
             [r"agents/builder\.md", cap_words]),
            # Regista's only front-matter trait (reviewer.model) sits in agents/reviewer.md, a judge file (FIT-3):
            # the rewrite is refused, and the refusal names the file.
            ("a front-matter trait out of bounds in a judge file",
             dict(text=reviewer.replace("model: sonnet", "model: haiku")), "agents/reviewer.md",
             [r"agents/reviewer\.md"]),
            ("a target that isn't evolvable (frozen)", dict(), "templates/config.json", [r"templates/config\.json"]),
            ("a target on a forbidden path", dict(), "scripts/regista.py", [r"scripts/regista\.py"]),
        ]
        for n, (what, answer, target, words) in enumerate(cases):
            with self.subTest(case=what):
                self.answer(**answer)
                before = snapshot(os.path.join(self.evo, "genomes"))
                self.assert_no_child(lambda: self.mutate(major, target=target, child=f"bad-{n}"), words, what, before)
                self.assertFalse(os.path.exists(self.genome_dir(f"bad-{n}")))

    def test_a_reflection_that_times_out_writes_no_child_and_says_so(self):  # covers: LOOP-11
        major, _blocker = self.review_replay()
        self.configure(reflection_timeout_minutes=0.05)  # three seconds
        self.answer(sleep=60)
        before = snapshot(os.path.join(self.evo, "genomes"))
        started = time.monotonic()
        self.assert_no_child(lambda: self.mutate(major, child="slow-1"), r"(?i)time[sd]? ?out|timed out|took too long",
                             "a reflection over reflection_timeout_minutes", before)
        self.assertLess(time.monotonic() - started, 45, "claude was stopped at the timeout, not waited for")


def no_tools(argv):
    """claude gets no tools: `--tools ""` (or --tools=), or a deny list naming every working tool."""
    for i, a in enumerate(argv):
        if a == "--tools" and i + 1 < len(argv) and argv[i + 1].strip() == "":
            return True
        if a in ("--tools=", '--tools=""'):
            return True
    denied = []
    for i, a in enumerate(argv):
        if a in ("--disallowedTools", "--disallowed-tools") and i + 1 < len(argv):
            denied += re.split(r"[,\s]+", argv[i + 1])
    return all(tool in denied for tool in ("Bash", "Edit", "Write", "Read", "WebFetch"))


if __name__ == "__main__":
    unittest.main()
