"""End-to-end: a whole generation on a made-up skill through `evolve.py generation --suite` (spec
.regista/specs/suite-generation.json, SUI-15).

In a temporary folder the test writes a made-up skill (one text file, guide.md), a profile naming guide.md as its only
evolvable file, and a suite of five cases (two training, three held out), each with two file checks on answer.txt:
it exists, and it contains a word (ALPHA-WORD or BRAVO-WORD, depending on the case). The seed guide contains neither
word, so the base fails one check in every case. A stand-in claude (evolve.json claude_command, require_sandbox false)
plays both parts, told apart by --model:
- with the suite's task model it is the agent: it writes answer.txt holding the installed guide.md's text, so the
  individual's text decides which checks pass;
- with the reflection model it is the reflection: it finds the word the failing check looked for in its prompt and
  answers with a rewrite of guide.md that asks for that word, which fixes that check.
HOME and REGISTA_EVOLVE_HOME are temporary folders; nothing real runs.

The test runs, each as a separate process: `evolve.py genome seed --profile <p> --source <skill>`, then
`evolve.py generation --suite <suite> --profile <p> --source <skill> --target guide.md --budget N --id e2e-suite
--seed 7 --yes`, then `evolve.py report e2e-suite`, and checks: every command exits 0; the base was screened and run
on every held-out case; each child was made from a failed check (mutation.json's failure) and its rewrite passes that
check; generation.json names a winner or a plain reason, within the budget; and the report prints the suite's axes.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from harness import EVOLVE, PY  # noqa: E402

INSTALL = ".claude/skills/made-up-answerer"
TASK_MODEL = "made-up-task-model"
REFLECT_MODEL = "made-up-reflect-model"
GEN = "e2e-suite"
BUDGET = 50_000_000
SEED_GUIDE = "# Made-up answering guide\n\nWrite the answer in plain words, in a few short lines for the reader.\n"

CLAUDE = r'''#!@PYTHON@
import json, os, re, sys, threading
LOG = @LOG@
TASK_MODEL = @TASK_MODEL@
INSTALL = @INSTALL@
SEED = @SEED@
args = sys.argv[1:]

def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None

def log(entry):
    with open(LOG, "a", encoding="utf-8") as f:
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
    log({"part": "agent", "guide": text})
    with open("answer.txt", "w", encoding="utf-8") as f:
        f.write(text)
    usage = {"input_tokens": 1500, "output_tokens": 200}
    emit({"type": "system", "subtype": "init", "session_id": "a-1", "apiKeySource": "none"})
    emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Wrote answer.txt."}],
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
prompt = b"".join(chunks).decode("utf-8", "replace") + "\n" + "\n".join(args)
found = re.findall(r"\b(ALPHA|BRAVO)-WORD\b", prompt)
word = found[0] + "-WORD" if found else None
log({"part": "reflection", "word": word})
if word:
    text = SEED + f"Always include {word} in every answer you write.\n"
else:
    text = SEED + "Start every answer with a short made-up heading line.\n"
body = json.dumps({"diagnosis": f"The made-up guide never asked for {word or 'a heading'}.", "text": text})
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


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


class SuiteGenerationEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-generation-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        self.log = os.path.join(self.tmp, "claude-calls.jsonl")
        self.claude = os.path.join(self.tmp, "bin", "claude")
        write(self.claude, CLAUDE.replace("@PYTHON@", PY).replace("@LOG@", repr(self.log))
              .replace("@TASK_MODEL@", repr(TASK_MODEL)).replace("@INSTALL@", repr(INSTALL))
              .replace("@SEED@", repr(SEED_GUIDE)), 0o755)
        write(os.path.join(self.evo, "evolve.json"), json.dumps(
            {"claude_command": [self.claude], "require_sandbox": False, "reflection_model": REFLECT_MODEL,
             "population": 2}) + "\n")
        self.skill = os.path.join(self.tmp, "made-up-skill")
        write(os.path.join(self.skill, "guide.md"), SEED_GUIDE)
        self.profile = os.path.join(self.tmp, "profiles", "made-up-answerer.json")
        write(self.profile, json.dumps({"name": "made-up-answerer", "evolvable": ["guide.md"], "traits": {}}) + "\n")
        self.words = {"s1": "ALPHA-WORD", "s2": "BRAVO-WORD", "h1": "ALPHA-WORD", "h2": "BRAVO-WORD",
                      "h3": "ALPHA-WORD"}
        cases = [{"id": cid, "split": "train" if cid.startswith("s") else "holdout",
                  "prompt": f"Write answer.txt answering made-up question {cid}.",
                  "checks": [{"id": "answer-exists", "file": "answer.txt", "exists": True},
                             {"id": "has-word", "file": "answer.txt", "contains": word}]}
                 for cid, word in self.words.items()]
        self.suite = os.path.join(self.tmp, "suites", "made-up-answers.json")
        write(self.suite, json.dumps({"name": "made-up-answers", "install": INSTALL, "model": TASK_MODEL,
                                      "cases": cases}, indent=2) + "\n")

    def env(self):
        drop = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")
               and k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        env.update(HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1")
        return env

    def evolve(self, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=self.tmp, env=self.env(), capture_output=True, text=True,
                              encoding="utf-8", stdin=subprocess.DEVNULL, timeout=600)

    def result(self, case, genome):
        path = os.path.join(self.evo, "runs", GEN, case, genome, "result.json")
        self.assertTrue(os.path.isfile(path), f"{genome} ran on {case}")
        return read_json(path)

    def test_generation_on_a_suite_names_a_winner_or_why_and_report_prints_it(self):  # covers: SUI-15
        r = self.evolve("genome", "seed", "--profile", self.profile, "--source", self.skill)
        self.assertEqual(r.returncode, 0, output(r))

        r = self.evolve("generation", "--suite", self.suite, "--profile", self.profile, "--source", self.skill,
                        "--target", "guide.md", "--budget", str(BUDGET), "--id", GEN, "--seed", "7", "--yes")
        shown = output(r)
        self.assertEqual(r.returncode, 0, shown)
        self.assertNotIn("Traceback", shown)
        state = read_json(os.path.join(self.evo, "runs", GEN, "generation.json"))
        self.assertEqual((state.get("adapter") or {}).get("kind"), "suite", state)

        # The base was screened on both training cases and run on every held-out case.
        for case in ("s1", "s2", "h1", "h2", "h3"):
            self.assertTrue(self.result(case, "seed").get("checks"), f"the base's checks ran on {case}")

        # Children aimed at failed checks, each fixing the check it was aimed at.
        children = list(state.get("children") or [])
        self.assertTrue(children, f"at least one child was made:\n{shown}")
        for child in children:
            mutation = read_json(os.path.join(self.evo, "genomes", child, "mutation.json"))
            failure = mutation.get("failure") or {}
            self.assertTrue(str(failure.get("id", "")).endswith("/has-word"),
                            f"{child} is aimed at a failed check: {mutation}")
            case = failure.get("case") or str(failure.get("id")).split("/")[0]
            self.assertIn(case, ("s1", "s2"), f"a screening failure: {failure}")
            with open(os.path.join(self.evo, "genomes", child, "files", "guide.md"), encoding="utf-8") as f:
                self.assertIn(self.words[case], f.read(), f"{child}'s rewrite asks for the missing word")
            checks = self.result(case, child).get("checks") or []
            self.assertTrue(checks and all(c.get("passed") for c in checks),
                            f"{child} passes every check of {case}, which the base failed: {checks}")

        # A winner, or a plain reason; within the budget.
        winner, why = state.get("winner"), state.get("why")
        self.assertTrue(winner or (isinstance(why, str) and why.strip()), f"a winner or a reason: {state}")
        if winner:
            self.assertIn(winner, children)
            self.assertIn(winner, r.stdout, "the summary names the winner")
            for case in ("h1", "h2", "h3"):
                self.result(case, winner)
        spent = (state.get("spent") or {}).get("total")
        self.assertIsInstance(spent, int, state)
        self.assertLessEqual(spent, BUDGET, "the generation kept within its budget")

        r = self.evolve("report", GEN)
        self.assertEqual(r.returncode, 0, output(r))
        self.assertIn("Cases passed", r.stdout, output(r))
        self.assertIn("Checks passed", r.stdout, output(r))
        if winner:
            self.assertIn(winner, r.stdout, "the report names the winner")


if __name__ == "__main__":
    unittest.main()
