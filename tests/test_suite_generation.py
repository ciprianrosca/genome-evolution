"""Scoring, failures and `generation --suite` (spec .regista/specs/suite-generation.json: SUI-7, SUI-8, SUI-16,
SUI-17; SUI-15 is the end-to-end test in genome/tests/e2e/test_suite_generation.py).

suite.SuiteAdapter(suite, home, config) is the suite's adapter (a subclass of evolve_core.Adapter): kind 'suite', name
the suite's name, axes cases_passed (higher), checks_passed (higher), tokens (lower), must_not_worsen cases_passed and
checks_passed. Its values and score come only from result.json's checks and its hard-failure record; failures lists one
failure per failed check (plus a hard failure first and a timeout) for reflection. `evolve.py generation --suite`
drives a whole generation through it.

Everything is made up and lives in temporary folders (HOME and REGISTA_EVOLVE_HOME included).
- Scoring, failures and the command-check hardening use a template runner (a Python script) that does what
  control/plan.json asks: write, remove or replace workspace files with links, set file modes, print text (the
  agent's own claims about its score), print a result object with usage (tokens 'claude-json'), sleep past the time
  limit, or exit with a code. The suite's check scripts are small Python files.
- The generation tests use the default runner 'claude' with a stand-in claude (evolve.json claude_command,
  require_sandbox false) that plays two parts, told apart by --model: with the suite's task model it is the agent, and
  writes answer.txt holding the installed guide.md's text (so the individual's text decides which file checks pass)
  and reports more or fewer tokens depending on that text; with the reflection model it is the reflection, and answers
  with the next rewrite of guide.md from control/rewrites.json. The genome is a made-up skill folder holding one
  evolvable file, guide.md, described by a small profile and seeded as `seed` with genome.seed.
  The winner fixture: rewrite A adds SCREEN-WORD (passes every screening check) and VERBOSE (spends 9,000 tokens a
  run); rewrite B adds HOLD-WORD (passes every held-out check, fails the screening one) and spends 1,000; the seed
  passes half of each case's checks and spends 5,000. A leads on screening, B on the held-out runs, and both are on
  the screened Pareto front (B is cheaper, A passes more).

Interface assumed (from the spec and the earlier steps):
- suite.run_case(suite, case_id, individual, folder, config, keep=False, deny_read=()) as before; SuiteAdapter.run
  takes a case dict from cases() and writes folder/result.json.
- result.json's 'hard_failure' is absent or null when there is none, else a sentence naming the frozen file. "Replaced"
  covers a frozen file swapped for a link, even one leading to a file with the same text.
- genome.check_failure(failure) raises (or returns False) for a failure it doesn't accept.
- Failure ids are '<case>/<genome>/<check id>', '<case>/<genome>/hard-failure' and '<case>/<genome>/timeout'; the
  timeout's place in the list is not fixed by the spec, so only the hard failure's (first) and the checks' relative
  order are checked.
- `evolve.py report <generation>` prints the report; its table headers are the labels capitalised ('Cases passed',
  'Checks passed'). A suite generation's printed continue command has --id, --suite, --profile and --source.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest test_suite_generation -v (from genome/tests)"""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_suite_runs as base  # noqa: E402  (helpers only: import_scripts, write)

PY = sys.executable
INSTALL = "skill"
TASK_MODEL = "made-up-task-model"
REFLECT_MODEL = "made-up-reflect-model"
JUDGE_MODEL = "made-up-judge-model"
GOOD = "GOOD-MARK"
REF = "Made-up reference table the agent must not change.\n"
FAILURE_OUTPUT = "MADE-UP-FAILURE-OUTPUT: expected three lines, found one"
LIMIT = "You've hit your limit · resets 3am (UTC)"

RUNNER = r'''#!@PYTHON@
import json, os, sys, time
CONTROL = @CONTROL@
with open(os.path.join(CONTROL, "plan.json"), encoding="utf-8") as f:
    plan = json.load(f)
ws = os.getcwd()
def target(rel):
    p = os.path.join(ws, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
for rel, text in plan.get("write", {}).items():
    with open(target(rel), "w", encoding="utf-8") as f:
        f.write(text)
for rel in plan.get("remove", []):
    os.remove(target(rel))
for rel, dest in plan.get("link", {}).items():
    p = target(rel)
    if os.path.lexists(p):
        os.remove(p)
    os.symlink(dest, p)
for rel, mode in plan.get("modes", {}).items():
    os.chmod(target(rel), mode)
for line in plan.get("print", []):
    print(line)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": plan.get("claim", "Done."),
                  "usage": plan.get("usage", {"input_tokens": 100, "output_tokens": 20})}))
sys.stdout.flush()
if plan.get("sleep"):
    time.sleep(plan["sleep"])
sys.exit(plan.get("exit", 0))
'''
RUN_TOKENS = 100 + 20

OK_CHECK = "print('made-up check ok')\n"
FAIL_CHECK = f"import sys\nprint({FAILURE_OUTPUT!r})\nsys.exit(1)\n"
ENV_CHECK = r'''import json, os, sys
with open(os.path.join(@CONTROL@, "check-env.json"), "w", encoding="utf-8") as f:
    json.dump({"PYTHONSAFEPATH": os.environ.get("PYTHONSAFEPATH"), "PATH": os.environ.get("PATH")}, f)
print("made-up environment recorded")
'''
PLANTED_MODULE = "import os\nopen(os.path.join(@CONTROL@, 'planted-module.txt'), 'a').write('ran\\n')\n"
PLANTED_PROGRAM = "#!/bin/sh\necho planted >> @MARKER@\nexit 0\n"
REAL_PROGRAM = "#!/bin/sh\necho real >> @MARKER@\nexit 0\n"

# The generation's stand-in claude: the agent with the task model, the reflection with any other model.
STANDIN = r'''#!@PYTHON@
import json, os, sys, threading
CONTROL = @CONTROL@
TASK_MODEL = @TASK_MODEL@
INSTALL = @INSTALL@
LIMIT = @LIMIT@
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

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if opt("--model") == TASK_MODEL:
    try:
        with open(os.path.join(os.getcwd(), *INSTALL.split("/"), "guide.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    log("agent.jsonl", {"cwd": os.getcwd(), "guide": text})
    emit({"type": "system", "subtype": "init", "session_id": "s-1", "apiKeySource": "none"})
    flag = os.path.join(CONTROL, "limit_once")
    if os.path.exists(flag):
        os.remove(flag)
        emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": LIMIT}],
                                               "usage": {"input_tokens": 7, "output_tokens": 3}}})
        emit({"type": "result", "subtype": "success", "is_error": True, "result": LIMIT,
              "usage": {"input_tokens": 7, "output_tokens": 3}})
        sys.exit(1)
    with open("answer.txt", "w", encoding="utf-8") as f:
        f.write(text)
    used = 9000 if "VERBOSE" in text else 1000 if "HOLD-WORD" in text else 5000
    usage = {"input_tokens": used - 100, "output_tokens": 100}
    emit({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Wrote answer.txt."}],
                                           "usage": usage}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "usage": usage})
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
path = os.path.join(CONTROL, "reflections.jsonl")
n = 0
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        n = sum(1 for line in f if line.strip())
log("reflections.jsonl", {"argv": args, "model": opt("--model")})
with open(os.path.join(CONTROL, "rewrites.json"), encoding="utf-8") as f:
    rewrites = json.load(f)
text = rewrites[n % len(rewrites)]
body = json.dumps({"diagnosis": "The made-up guide never asked for the word the check looks for.", "text": text})
reply = "Here is the rewrite.\n\n```json\n" + body + "\n```\n"
usage = {"input_tokens": 1200, "output_tokens": 300}
result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": reply,
          "session_id": "r-1", "usage": usage}
fmt = opt("--output-format")
if fmt == "stream-json":
    emit({"type": "system", "subtype": "init", "session_id": "r-1", "apiKeySource": "none"})
    emit({"type": "assistant", "message": {"id": "r1", "content": [{"type": "text", "text": reply}], "usage": usage}})
    emit(result)
elif fmt == "json":
    emit(result)
else:
    sys.stdout.write(reply)
sys.stdout.flush()
sys.exit(0)
'''

SEED_GUIDE = "# Made-up answering guide\n\nWrite the answer in plain words, in a few short lines for the reader.\n"
REWRITE_A = SEED_GUIDE + "Add SCREEN-WORD and be VERBOSE.\n"  # passes screening, spends 9,000 tokens a run
REWRITE_B = SEED_GUIDE + "Sign off with HOLD-WORD.\n"  # passes the held-out cases, spends 1,000
GEN_PROFILE = {"name": "made-up-skill", "evolvable": ["guide.md"], "traits": {}}
GEN = "suite-gen"
BIG_BUDGET = 10 ** 9


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def accepted(gn, failure):
    """Whether genome.check_failure accepts the failure (it raises, or returns False or a problem, when it doesn't)."""
    try:
        verdict = gn.check_failure(failure)
    except Exception as e:  # noqa: BLE001  (any refusal counts)
        return False, f"{type(e).__name__}: {e}"
    if verdict is False:
        return False, "returned False"
    if isinstance(verdict, (list, tuple)) and verdict:
        return False, f"returned problems {verdict!r}"
    if isinstance(verdict, str) and verdict.strip() and verdict != failure.get("id"):
        return False, f"returned {verdict!r}"
    return True, ""


# Scoring, failures and command checks: a template runner ---------------------------------------------------------------

class ScoringBase(unittest.TestCase):
    def setUp(self):
        self.suite_mod = base.import_scripts("suite")
        self.ev = base.import_scripts("evolve")
        self.core = base.import_scripts("evolve_core")
        self.gn = base.import_scripts("genome")
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-generation-")
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
        base.write(self.runner, self.fill(RUNNER), 0o755)
        self.root = os.path.join(self.tmp, "suite")
        base.write(os.path.join(self.root, "fixtures", "ref.txt"), REF)
        base.write(os.path.join(self.root, "checks", "ok.py"), OK_CHECK)
        base.write(os.path.join(self.root, "checks", "fail.py"), FAIL_CHECK)
        base.write(os.path.join(self.root, "checks", "envdump.py"), self.fill(ENV_CHECK))
        self.individual = os.path.join(self.tmp, "individual")
        base.write(os.path.join(self.individual, "guide.md"), SEED_GUIDE)
        base.write(os.path.join(self.evo, "evolve.json"), json.dumps(
            {"claude_command": [os.path.join(self.tmp, "bin", "no-claude-here")], "require_sandbox": False}) + "\n")
        self.config = self.ev.load_config(self.evo)
        self.plan({"write": {"answer.txt": f"A made-up answer with {GOOD}.\n"}})

    def fill(self, text):
        return text.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))

    def plan(self, plan):
        base.write(os.path.join(self.control, "plan.json"), json.dumps(plan))

    @staticmethod
    def check_script(name):
        return f"{shlex.quote(PY)} {{checks}}/{name}"

    def scored_checks(self):
        return [{"id": "answer-exists", "file": "answer.txt", "exists": True},
                {"id": "has-good", "file": "answer.txt", "contains": GOOD},
                {"id": "cmd-ok", "run": self.check_script("ok.py")}]

    def make_suite(self, checks=None, timeout_minutes=None, frozen=True):
        case = {"id": "scored", "split": "train", "prompt": "Write answer.txt with a made-up answer.",
                "checks": checks if checks is not None else self.scored_checks()}
        if frozen:
            case["files"] = {"data/ref.txt": "fixtures/ref.txt"}
            case["frozen"] = ["data/ref.txt"]
        data = {"name": "made-up-scoring", "install": INSTALL, "model": TASK_MODEL,
                "runner": {"command": [PY, self.runner], "tokens": "claude-json"},
                "cases": [case, {"id": "other", "split": "holdout", "prompt": "Write answer.txt.",
                                 "checks": [{"file": "answer.txt", "exists": True}]}]}
        if timeout_minutes is not None:
            data["timeout_minutes"] = timeout_minutes
        path = os.path.join(self.root, "made-up-scoring.json")
        base.write(path, json.dumps(data, indent=2) + "\n")
        return self.suite_mod.load_suite(path)

    def adapter(self, suite):
        return self.suite_mod.SuiteAdapter(suite, self.evo, self.config)

    def case(self, adapter, cid="scored"):
        cases = adapter.cases()
        for case in [*cases["screening"], *cases["holdout"]]:
            if case.get("id") == cid:
                return case
        self.fail(f"no case {cid} in {cases}")

    def run_one(self, adapter, cid="scored", genome="seed"):
        """adapter.run on one case; returns (folder, result.json's content)."""
        folder = os.path.join(self.tmp, "runs", cid, genome)
        adapter.run(self.case(adapter, cid), genome, self.individual, folder)
        return folder, read_json(os.path.join(folder, "result.json"))

    def assert_values(self, values, cases_passed, checks_passed, msg=""):
        self.assertEqual(list(values), ["cases_passed", "checks_passed", "tokens"], f"every axis has a value {msg}")
        self.assertEqual(values["cases_passed"], cases_passed, f"cases_passed {msg}: {values}")
        self.assertAlmostEqual(values["checks_passed"], checks_passed, 6, f"checks_passed {msg}: {values}")

    def assert_hard_failure_values(self, adapter, result, label):
        values = adapter.values(result)
        self.assertEqual(values.get("cases_passed"), 0.0, f"{label}: the worst cases_passed: {values}")
        self.assertEqual(values.get("checks_passed"), 0.0, f"{label}: the worst checks_passed: {values}")
        self.assertEqual(adapter.score(result), 0, f"{label}: a hard failure scores 0")


class AdapterShape(ScoringBase):
    def test_suite_adapter_axes_labels_and_cases_from_the_split(self):  # covers: SUI-7
        suite = self.make_suite()
        adapter = self.adapter(suite)
        self.assertIsInstance(adapter, self.core.Adapter, "SuiteAdapter subclasses evolve_core.Adapter")
        self.assertEqual(adapter.kind, "suite")
        self.assertEqual(adapter.name, "made-up-scoring", "the adapter's name is the suite's name")
        self.assertEqual(list(adapter.axes.items()),
                         [("cases_passed", "higher"), ("checks_passed", "higher"), ("tokens", "lower")])
        self.assertEqual(tuple(adapter.must_not_worsen), ("cases_passed", "checks_passed"))
        self.assertEqual(adapter.labels.get("cases_passed"), "cases passed")
        self.assertEqual(adapter.labels.get("checks_passed"), "checks passed")
        cases = adapter.cases()
        self.assertEqual([c.get("id") for c in cases["screening"]], suite["split"]["screening"])
        self.assertEqual([c.get("id") for c in cases["holdout"]], suite["split"]["holdout"])
        self.assertEqual(self.core.check_adapter(adapter)[0], cases["screening"], "check_adapter accepts it")


class Scoring(ScoringBase):
    """SUI-7: values and the score come from the checks and the hard-failure record only."""

    def test_values_and_score_come_from_the_checks(self):  # covers: SUI-7
        adapter = self.adapter(self.make_suite())
        _folder, result = self.run_one(adapter)
        self.assertFalse(result.get("hard_failure"), f"an untouched frozen fixture is no hard failure: {result}")
        values = adapter.values(result)
        self.assert_values(values, 1.0, 1.0, "when every check passed")
        self.assertEqual(values["tokens"], result["tokens"]["used"], "tokens is tokens.used")
        self.assertEqual(values["tokens"], RUN_TOKENS)
        self.assertAlmostEqual(adapter.score(result), 1.0, 6)

        self.plan({"write": {"answer.txt": "A made-up answer without the mark.\n"}})
        _folder, result = self.run_one(adapter, genome="other-genome")
        values = adapter.values(result)
        self.assert_values(values, 0.0, 2 / 3, "when one check of three failed")
        self.assertAlmostEqual(adapter.score(result), 0.5 * (2 / 3) + 0.5 * 0.0, 6)

    def test_values_of_runs_without_checks_skipped_judges_and_missing_tokens(self):  # covers: SUI-7
        adapter = self.adapter(self.make_suite())
        check = lambda cid, passed, **extra: {"id": cid, "kind": "file", "passed": passed,  # noqa: E731
                                              "evidence": f"made-up evidence {cid}", **extra}
        paused = {"case": "scored", "outcome": "paused", "tokens": {"used": 10, "agent": 10, "judge": 0},
                  "checks": []}
        self.assert_values(adapter.values(paused), 0.0, 0.0, "when no check ran")
        self.assertEqual(adapter.score(paused), 0)
        skipped = {"case": "scored", "outcome": "done", "tokens": {"used": 30, "agent": 30, "judge": 0},
                   "checks": [check("a", True), check("b", False),
                              {"id": "tone", "kind": "judge", "passed": False, "skipped": True,
                               "evidence": "not run: a mechanical check failed"}]}
        self.assert_values(adapter.values(skipped), 0.0, 1 / 3, "a skipped judge is a check that didn't pass")
        self.assertAlmostEqual(adapter.score(skipped), 0.5 * (1 / 3), 6)
        no_tokens = {"case": "scored", "outcome": "done", "checks": [check("a", True)]}
        values = adapter.values(no_tokens)
        self.assertIsNone(values.get("tokens"), f"tokens is None when tokens.used is missing: {values}")
        self.assertEqual(values.get("cases_passed"), 1.0, values)

    def test_the_agents_own_claims_change_nothing(self):  # covers: SUI-7
        adapter = self.adapter(self.make_suite())
        claims = {"case": "scored", "outcome": "done", "hard_failure": None, "score": 1.0,
                  "values": {"cases_passed": 1.0, "checks_passed": 1.0},
                  "checks": [{"id": c, "kind": "file", "passed": True, "evidence": "passed"}
                             for c in ("answer-exists", "has-good", "cmd-ok")]}
        self.plan({"write": {"answer.txt": "A made-up answer without the mark.\n",
                             "result.json": json.dumps(claims), "checks/result.json": json.dumps(claims)},
                   "print": ["All 3 checks passed. cases_passed=1.0 checks_passed=1.0 score=1.0",
                             json.dumps({"checks": claims["checks"], "hard_failure": None})],
                   "claim": "Every check passed; score 1.0."})
        _folder, result = self.run_one(adapter)
        self.assert_values(adapter.values(result), 0.0, 2 / 3, "whatever the agent wrote about itself")
        self.assertLess(adapter.score(result), 1.0)

    def test_a_changed_removed_or_replaced_frozen_fixture_is_a_hard_failure(self):  # covers: SUI-7
        same_text = os.path.join(self.tmp, "outside", "ref-copy.txt")
        base.write(same_text, REF)
        plans = {
            "changed": {"write": {"data/ref.txt": "Changed by the made-up agent.\n"}},
            "removed": {"remove": ["data/ref.txt"]},
            "replaced by a link": {"link": {"data/ref.txt": same_text}},
        }
        adapter = self.adapter(self.make_suite())
        for label, plan in plans.items():
            with self.subTest(label):
                plan.setdefault("write", {})["answer.txt"] = f"A made-up answer with {GOOD}.\n"
                self.plan(plan)
                _folder, result = self.run_one(adapter, genome=f"g-{label.split()[0]}")
                self.assertTrue(all(c.get("passed") for c in result.get("checks") or []),
                                f"fixture: every check that ran passed: {result.get('checks')}")
                sentence = result.get("hard_failure")
                self.assertIsInstance(sentence, str, f"{label}: result.json names the hard failure: {result}")
                self.assertIn("data/ref.txt", sentence, f"{label}: the sentence names the file")
                self.assertNotIn("\n", sentence.strip(), "one plain sentence")
                self.assertNotIn("Traceback", sentence)
                self.assert_hard_failure_values(adapter, result, label)


class Failures(ScoringBase):
    """SUI-8: one failure per failed check, plus the hard failure (first) and the timeout."""

    def assert_well_formed(self, found, case):
        for f in found:
            self.assertEqual(set(f) >= {"id", "kind", "case", "summary", "evidence"}, True, f)
            self.assertEqual(f["case"], case, f)
            self.assertIn(f["kind"], ("check_failed", "hard_failure", "timeout"), f)
            self.assertIsInstance(f["summary"], str)
            self.assertTrue(f["summary"].strip(), f"a plain summary: {f}")
            self.assertNotIn("\n", f["summary"].strip(), f"one sentence: {f}")
            self.assertIsInstance(f["evidence"], str)
            self.assertLessEqual(len(f["evidence"]), 4000, f"evidence is at most 4000 characters: {f['id']}")
            ok, why = accepted(self.gn, f)
            self.assertTrue(ok, f"genome.check_failure accepts {f}: {why}")

    def test_hard_failure_failed_checks_and_timeout_from_a_real_run(self):  # covers: SUI-8
        checks = [{"id": "answer-exists", "file": "answer.txt", "exists": True},
                  {"id": "has-good", "file": "answer.txt", "contains": GOOD},
                  {"id": "cmd-fails", "run": self.check_script("fail.py")},
                  {"id": "cmd-ok", "run": self.check_script("ok.py")},
                  {"id": "tone", "judge": "Pass when the made-up answer is polite.", "model": JUDGE_MODEL,
                   "files": ["answer.txt"]}]
        adapter = self.adapter(self.make_suite(checks=checks, timeout_minutes=0.02))
        self.plan({"write": {"answer.txt": "A made-up answer without the mark.\n",
                             "data/ref.txt": "Changed by the made-up agent.\n"}, "sleep": 30})
        folder, result = self.run_one(adapter, genome="g7")
        self.assertEqual(result.get("outcome"), "timeout", f"fixture: the run timed out: {result}")
        found = adapter.failures(self.case(adapter), "g7", folder, result)
        self.assert_well_formed(found, "scored")
        ids = [f["id"] for f in found]
        self.assertEqual(ids[0], "scored/g7/hard-failure", f"the hard failure comes first: {ids}")
        self.assertEqual(found[0]["kind"], "hard_failure")
        self.assertIn("data/ref.txt", found[0]["evidence"] + found[0]["summary"], "it names the frozen file")
        self.assertEqual(sorted(ids), sorted(["scored/g7/hard-failure", "scored/g7/has-good",
                                              "scored/g7/cmd-fails", "scored/g7/timeout"]),
                         f"one per failed check, the hard failure and the timeout; no skipped judge: {ids}")
        self.assertLess(ids.index("scored/g7/has-good"), ids.index("scored/g7/cmd-fails"), "in the case's order")
        by_id = {f["id"]: f for f in found}
        self.assertEqual(by_id["scored/g7/timeout"]["kind"], "timeout")
        self.assertEqual(by_id["scored/g7/has-good"]["kind"], "check_failed")
        self.assertIn("has-good", by_id["scored/g7/has-good"]["summary"], "the summary says which check failed")
        self.assertIn(GOOD, by_id["scored/g7/has-good"]["evidence"], "the evidence names the missing text")
        self.assertIn(FAILURE_OUTPUT, by_id["scored/g7/cmd-fails"]["evidence"], "the end of the command's output")

    def test_judge_reason_long_evidence_and_no_failures_when_all_passed(self):  # covers: SUI-8
        adapter = self.adapter(self.make_suite())
        case = self.case(adapter)
        folder = os.path.join(self.tmp, "runs", "scored", "g2")
        os.makedirs(folder)
        long_output = "HEAD-OF-OUTPUT\n" + "x" * 9000 + "\nTAIL-OF-OUTPUT"
        result = {"case": "scored", "outcome": "failed", "tokens": {"used": 50, "agent": 40, "judge": 10},
                  "checks": [{"id": "cmd-long", "kind": "command", "passed": False, "exit": 2,
                              "evidence": long_output[-4000:]},
                             {"id": "answer-exists", "kind": "file", "passed": True, "evidence": "answer.txt exists"},
                             {"id": "tone", "kind": "judge", "passed": False, "verdict": "fail",
                              "reason": "JUDGE-REASON-71: the made-up answer is rude.",
                              "evidence": "JUDGE-REASON-71: the made-up answer is rude.", "tokens": 10}]}
        found = adapter.failures(case, "g2", folder, result)
        self.assert_well_formed(found, "scored")
        self.assertEqual([f["id"] for f in found], ["scored/g2/cmd-long", "scored/g2/tone"],
                         "a failed 'failed' run without a timeout or hard failure: its failed checks only")
        self.assertIn("TAIL-OF-OUTPUT", found[0]["evidence"], "the end of the command's output is kept")
        self.assertIn("JUDGE-REASON-71", found[1]["evidence"], "a judge's evidence is its reason")
        self.assertEqual({f["kind"] for f in found}, {"check_failed"})

        passed = {"case": "scored", "outcome": "done", "tokens": {"used": 50, "agent": 50, "judge": 0},
                  "checks": [{"id": c, "kind": "file", "passed": True, "evidence": "fine"} for c in ("a", "b")]}
        self.assertEqual(adapter.failures(case, "g2", folder, passed), [], "every check passed: no failures")
        _folder, real = self.run_one(adapter, genome="g3")
        self.assertEqual(adapter.failures(case, "g3", _folder, real), [], f"a real passing run has none: {real}")


class CommandCheckHardening(ScoringBase):
    """SUI-16: command checks get PYTHONSAFEPATH=1 and a PATH without relative or empty entries."""

    def test_python_safe_path_and_no_relative_or_empty_path_entry(self):  # covers: SUI-16
        real_bin = os.path.join(self.tmp, "real-bin")
        os.makedirs(real_bin)
        path = os.pathsep.join(["bin", ".", "", "rel/tools", real_bin, os.environ.get("PATH", "")])
        suite = self.make_suite(checks=[{"id": "env", "run": self.check_script("envdump.py")}], frozen=False)
        with mock.patch.dict(os.environ, {"PATH": path}):
            os.environ.pop("PYTHONSAFEPATH", None)
            self.suite_mod.run_case(suite, "scored", self.individual, os.path.join(self.tmp, "runs", "env"),
                                    self.config)
        seen = read_json(os.path.join(self.control, "check-env.json"))
        self.assertEqual(seen.get("PYTHONSAFEPATH"), "1", f"the check sees PYTHONSAFEPATH=1: {seen}")
        entries = (seen.get("PATH") or "").split(os.pathsep)
        bad = [e for e in entries if not e or not os.path.isabs(e)]
        self.assertEqual(bad, [], f"no relative or empty PATH entry reaches the check: {seen.get('PATH')!r}")
        self.assertIn(real_bin, entries, "absolute entries stay")

    def test_a_planted_module_or_program_is_not_used(self):  # covers: SUI-16
        real_bin = os.path.join(self.tmp, "real-bin")
        planted_marker = os.path.join(self.control, "planted-program.txt")
        real_marker = os.path.join(self.control, "real-program.txt")
        base.write(os.path.join(real_bin, "made-up-tool"), REAL_PROGRAM.replace("@MARKER@", shlex.quote(real_marker)),
                   0o755)
        planted = PLANTED_PROGRAM.replace("@MARKER@", shlex.quote(planted_marker))
        module = self.fill(PLANTED_MODULE)
        self.plan({"write": {"answer.txt": "made up\n", "pytest.py": module, "madeup_helper.py": module,
                             "bin/made-up-tool": planted, "made-up-tool": planted, "only-planted": planted},
                   "modes": {"bin/made-up-tool": 0o755, "made-up-tool": 0o755, "only-planted": 0o755}})
        checks = [{"id": "pytest", "run": f"{shlex.quote(PY)} -m pytest --version"},
                  {"id": "helper", "run": f"{shlex.quote(PY)} -c 'import madeup_helper'"},
                  {"id": "tool", "run": "made-up-tool"},
                  {"id": "only-planted", "run": "only-planted"}]
        suite = self.make_suite(checks=checks, frozen=False)
        path = os.pathsep.join(["bin", ".", "", real_bin, os.environ.get("PATH", "")])
        with mock.patch.dict(os.environ, {"PATH": path}):
            self.suite_mod.run_case(suite, "scored", self.individual, os.path.join(self.tmp, "runs", "planted"),
                                    self.config)
        result = read_json(os.path.join(self.tmp, "runs", "planted", "result.json"))
        checks = {c["id"]: c for c in result.get("checks") or []}
        self.assertFalse(os.path.exists(os.path.join(self.control, "planted-module.txt")),
                         "a module planted in the workspace was imported by a check")
        self.assertFalse(os.path.exists(planted_marker), "a program planted in the workspace ran for a check")
        self.assertTrue(os.path.exists(real_marker), "the real program on PATH answers the check")
        self.assertIs(checks["tool"].get("passed"), True, checks.get("tool"))
        self.assertIs(checks["helper"].get("passed"), False, "the planted helper module can't be imported")
        self.assertIs(checks["only-planted"].get("passed"), False, "a program only in the workspace isn't found")


# generation --suite: in-process with the stand-in claude ---------------------------------------------------------------

class GenerationBase(unittest.TestCase):
    def setUp(self):
        self.ev = base.import_scripts("evolve")
        self.gn = base.import_scripts("genome")
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-gen-")
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
        self.claude = os.path.join(self.tmp, "bin", "claude")
        base.write(self.claude, STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))
                   .replace("@TASK_MODEL@", repr(TASK_MODEL)).replace("@INSTALL@", repr(INSTALL))
                   .replace("@LIMIT@", repr(LIMIT)), 0o755)
        self.rewrites([REWRITE_A, REWRITE_B])
        # The made-up skill (one evolvable file), its profile, and the seed genome.
        self.source = os.path.join(self.tmp, "made-up-skill")
        base.write(os.path.join(self.source, "guide.md"), SEED_GUIDE)
        base.write(os.path.join(self.source, "README.txt"), "A frozen made-up readme.\n")
        self.profile_path = os.path.join(self.tmp, "profiles", "made-up-skill.json")
        base.write(self.profile_path, json.dumps(GEN_PROFILE, indent=2) + "\n")
        profile = self.gn.load_profile(self.profile_path, source=self.source)
        self.gn.seed(self.evo, profile, self.source, "seed")
        # The suite, outside the skill folder: two screening and two held-out cases, two file checks each.
        self.suite_root = os.path.join(self.tmp, "suites")
        self.suite_path = os.path.join(self.suite_root, "made-up-suite.json")
        base.write(self.suite_path, json.dumps(self.suite_data(), indent=2) + "\n")
        self.configure()

    @staticmethod
    def suite_data(name="made-up-suite"):
        def case(cid, split, word):
            return {"id": cid, "split": split, "prompt": f"Write answer.txt for made-up question {cid}.",
                    "checks": [{"id": "answer-exists", "file": "answer.txt", "exists": True},
                               {"id": "has-word", "file": "answer.txt", "contains": word}]}
        return {"name": name, "install": INSTALL, "model": TASK_MODEL,
                "cases": [case("s1", "train", "SCREEN-WORD"), case("s2", "train", "SCREEN-WORD"),
                          case("h1", "holdout", "HOLD-WORD"), case("h2", "holdout", "HOLD-WORD")]}

    def configure(self, **extra):
        cfg = {"claude_command": [self.claude], "require_sandbox": False, "reflection_model": REFLECT_MODEL,
               "population": 2, **extra}
        base.write(os.path.join(self.evo, "evolve.json"), json.dumps(cfg) + "\n")

    def rewrites(self, texts):
        base.write(os.path.join(self.control, "rewrites.json"), json.dumps(texts))

    def suite_args(self, suite=None, profile=None, source=None):
        return ["--suite", suite or self.suite_path, "--profile", profile or self.profile_path,
                "--source", source or self.source]

    def generation(self, gen=GEN, budget=BIG_BUDGET, extra=(), **paths):
        return self.invoke(["generation", *self.suite_args(**paths), "--target", "guide.md", "--budget", str(budget),
                            "--id", gen, "--seed", "7", "--yes", *extra])

    def invoke(self, argv):
        """evolve.main(argv) in-process; returns (exit code, printed text)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(argv)
            except SystemExit as e:
                code = e.code
        return (0 if code is None else code), buf.getvalue()

    def state(self, gen=GEN):
        path = os.path.join(self.evo, "runs", gen, "generation.json")
        self.assertTrue(os.path.isfile(path), f"runs/{gen}/generation.json is written")
        return read_json(path)

    def guide_of(self, genome):
        with open(os.path.join(self.evo, "genomes", genome, "files", "guide.md"), encoding="utf-8") as f:
            return f.read()

    def ran(self, gen, case):
        """The genomes that have a result.json for this case."""
        folder = os.path.join(self.evo, "runs", gen, case)
        if not os.path.isdir(folder):
            return set()
        return {g for g in os.listdir(folder) if os.path.isfile(os.path.join(folder, g, "result.json"))}

    def records(self, name):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def assert_plain_error(self, code, out, *words):
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        for word in words:
            self.assertIn(word, out, f"the message names {word}:\n{out}")


class SuiteGeneration(GenerationBase):
    """SUI-17: `generation --suite` loads the suite with the profile, records its adapter and picks the winner on the
    held-out runs."""

    def test_winner_is_picked_on_held_out_runs_and_the_suite_adapter_is_recorded(self):  # covers: SUI-17
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        state = self.state()
        adapter = state.get("adapter") or {}
        self.assertEqual(adapter.get("kind"), "suite", f"generation.json records the suite adapter: {state}")
        self.assertTrue(os.path.isabs(adapter.get("suite") or ""), f"the suite's absolute path: {adapter}")
        self.assertEqual(os.path.realpath(adapter["suite"]), os.path.realpath(self.suite_path))
        self.assertEqual(state.get("project"), "made-up-suite", "project is the suite's name")

        children = list(state.get("children") or [])
        by_text = {self.guide_of(c): c for c in children}
        self.assertIn(REWRITE_A, by_text, f"fixture: rewrite A became a child: {children}")
        self.assertIn(REWRITE_B, by_text, f"fixture: rewrite B became a child: {children}")
        a, b = by_text[REWRITE_A], by_text[REWRITE_B]
        self.assertLessEqual({a, b}, set(state.get("front") or []),
                             f"fixture: A (passes more) and B (cheaper) are both on the screened front: {state}")
        for genome in ("seed", a, b):
            for case in ("h1", "h2"):
                self.assertIn(genome, self.ran(GEN, case), f"{genome} ran on held-out case {case}")
        for genome in (a, b):
            for case in ("s1", "s2"):
                self.assertIn(genome, self.ran(GEN, case), f"{genome} was screened on {case}")
        self.assertEqual(state.get("winner"), b,
                         f"A leads on screening, B on the held-out runs, and B wins: {state.get('winner')} {state}")
        self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip(), state)
        guides = [r["guide"] for r in self.records("agent.jsonl")]
        self.assertIn(REWRITE_A, guides, "the agent ran with each individual's installed text")
        self.assertIn(SEED_GUIDE, guides)

    def test_a_suite_among_the_files_a_genome_may_change_is_refused(self):  # covers: SUI-17
        wide = os.path.join(self.tmp, "wide-skill")
        base.write(os.path.join(wide, "guide.md"), SEED_GUIDE)
        inside = os.path.join(wide, "tests", "made-up-inside.json")
        base.write(inside, json.dumps(self.suite_data("made-up-inside"), indent=2) + "\n")
        wide_profile = os.path.join(self.tmp, "profiles", "wide.json")
        base.write(wide_profile, json.dumps({"name": "made-up-wide", "evolvable": ["guide.md", "tests/*.json"],
                                             "traits": {}}) + "\n")
        self.gn.seed(self.evo, self.gn.load_profile(wide_profile, source=wide), wide, "wide")
        code, out = self.generation(gen="refused", extra=("--from", "wide"), suite=inside, profile=wide_profile,
                                    source=wide)
        self.assert_plain_error(code, out, "made-up-inside.json")
        self.assertEqual(code, 1, f"a plain error exits 1:\n{out}")
        self.assertNotIn("usage:", out, "the command accepts --suite and refuses this suite on its own terms")
        self.assertFalse(os.path.exists(os.path.join(self.evo, "runs", "refused")), "nothing is written for it")
        self.assertEqual(self.records("agent.jsonl"), [], "no case ran")
        self.assertEqual(self.records("reflections.jsonl"), [], "no reflection ran")

    def test_light_runs_the_first_held_out_cases_with_the_light_population(self):  # covers: SUI-17
        self.configure(light={"population": 1, "holdout_cases": 1})
        suite = base.import_scripts("suite").load_suite(self.suite_path)
        first, *rest = suite["split"]["holdout"]
        code, out = self.generation(gen="light-suite", extra=("--light",))
        self.assertEqual(code, 0, out)
        state = self.state("light-suite")
        self.assertIs(state.get("light"), True, f"generation.json says it is light: {state}")
        self.assertEqual(len(state.get("children") or []), 1, f"light.population children: {state}")
        self.assertIn("seed", self.ran("light-suite", first), "the first held-out case in the suite's order ran")
        for case in rest:
            self.assertEqual(self.ran("light-suite", case), set(), f"the light run skips held-out case {case}")

    def test_report_rebuilds_the_suite_adapter_and_a_missing_suite_is_a_plain_error(self):  # covers: SUI-17
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        code, report = self.invoke(["report", GEN])
        self.assertEqual(code, 0, report)
        self.assertIn("Cases passed", report, f"the suite's axes are the report's columns:\n{report}")
        self.assertIn("Checks passed", report, report)
        for word in ("Regista", "replay", "bench", "plugin"):
            self.assertNotIn(word, report, f"no Regista word in a suite generation's report:\n{report}")

        with open(self.suite_path, encoding="utf-8") as f:
            original = f.read()
        base.write(self.suite_path, "{ this made-up suite no longer loads\n")
        code, out = self.invoke(["report", GEN])
        self.assert_plain_error(code, out, "made-up-suite.json")
        os.remove(self.suite_path)
        code, out = self.invoke(["report", GEN])
        self.assert_plain_error(code, out, "made-up-suite.json")
        base.write(self.suite_path, original)

    def test_a_paused_suite_generation_prints_a_continue_command_with_the_suite(self):  # covers: SUI-17
        import test_generation as tg  # noqa: E402  (the printed-command parser)
        base.write(os.path.join(self.control, "limit_once"), "1")
        code, out = self.generation(gen="paused-suite")
        self.assertEqual(code, self.ev.PAUSED_EXIT, out)
        self.assertEqual(self.state("paused-suite").get("stopped"), "paused at the usage limit")
        commands = [(argv, opts) for argv, opts in tg.ContinueCommand.printed_commands(out) if "--id" in opts]
        self.assertTrue(commands, f"prints the command to continue:\n{out}")
        argv, opts = commands[-1]
        self.assertEqual(opts.get("--id"), "paused-suite")
        for name, path in (("--suite", self.suite_path), ("--profile", self.profile_path), ("--source", self.source)):
            self.assertIn(name, opts, f"the continue command names {name}:\n{out}")
            self.assertEqual(os.path.realpath(opts[name]), os.path.realpath(path), f"{name}:\n{out}")
        code, out = self.invoke(argv if "--yes" in argv else argv + ["--yes"])
        self.assertEqual(code, 0, out)
        state = self.state("paused-suite")
        self.assertIsNone(state.get("stopped"), state)
        self.assertTrue(state.get("winner") or (isinstance(state.get("why"), str) and state["why"].strip()), state)


if __name__ == "__main__":
    unittest.main()
