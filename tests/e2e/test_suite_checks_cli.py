"""End-to-end: `evolve.py suite run` runs a case's checks after the run and reports them (spec
.regista/specs/suite-checks.json, SUI-14).

In a temporary folder the test writes a made-up suite (the default runner 'claude') whose cases each have one command
check, one file check and one judge check, an individual folder with a made-up skill, and a stand-in claude (evolve.json's
claude_command, with require_sandbox false). The stand-in is both the agent and the judge: called with --tools "" it is
the judge, reads its prompt from standard input and answers {"verdict": "pass", ...} with 50 tokens of usage; otherwise
it is the agent, writes greeting.txt and prints stream-json with 300 tokens of usage. HOME and REGISTA_EVOLVE_HOME are
temporary folders.

The test runs `python3 genome/scripts/evolve.py suite run <suite> <case> --individual <folder>` as a separate process:
- case 'greet' (every check passes): exit 0, the case's line then one line per check (its id and 'passed'), and
  result.json's checks in the case's order, the judge's verdict and the tokens (agent 300, judge 50, used 350);
- case 'farewell' (the file check wants text the greeting lacks): the file check's line says failed with the first line
  of its evidence, the judge is not run, and the exit code is
  still 0 because the run itself ended done.

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

INSTALL = ".claude/skills/greeter"
AGENT_TOKENS = 200 + 40 + 60
JUDGE_TOKENS = 40 + 10
GREETING = "Hello Ada and Grace, from a made-up greeter.\n"
RUBRIC = "Pass when the made-up greeting is polite and names everyone in names.txt."

CLAUDE = r'''#!@PYTHON@
import json, os, sys
args = sys.argv[1:]
judge = "--tools" in args and args[args.index("--tools") + 1:args.index("--tools") + 2] == [""]
stdin = sys.stdin.read() if judge else None
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"role": "judge" if judge else "agent", "argv": args, "stdin": stdin}) + "\n")
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
if judge:
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": json.dumps({"verdict": "pass", "reason": "The made-up greeting is polite."}),
          "usage": {"input_tokens": 40, "cache_read_input_tokens": 0, "output_tokens": 10}})
    sys.exit(0)
emit({"type": "system", "subtype": "init", "session_id": "s-1"})
with open("greeting.txt", "w", encoding="utf-8") as f:
    f.write(@GREETING@)
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


def checks(file_check):
    return [{"id": "has-greeting", "run": "sh {checks}/has_greeting.sh"},
            {"id": file_check[0], "file": "greeting.txt", "contains": file_check[1]},
            {"id": "polite", "judge": RUBRIC, "model": "judge-model-e2e", "files": ["greeting.txt"]}]


class SuiteChecksEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-checks-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        self.log = os.path.join(self.tmp, "claude-calls.jsonl")
        self.claude = os.path.join(self.tmp, "bin", "claude")
        write(self.claude, CLAUDE.replace("@PYTHON@", PY).replace("@LOG@", repr(self.log))
              .replace("@GREETING@", repr(GREETING)), 0o755)
        write(os.path.join(self.evo, "evolve.json"),
              json.dumps({"claude_command": [self.claude], "require_sandbox": False}) + "\n")
        self.root = os.path.join(self.tmp, "suite")
        write(os.path.join(self.root, "fixtures", "names.txt"), "Ada\nGrace\n")
        write(os.path.join(self.root, "checks", "has_greeting.sh"), "#!/bin/sh\ntest -s greeting.txt\n", 0o755)
        self.suite = os.path.join(self.root, "greeter.json")
        write(self.suite, json.dumps({
            "name": "toy-greeter-checks", "install": INSTALL,
            "cases": [
                {"id": "greet", "split": "train", "prompt": "Write greeting.txt greeting everyone in names.txt.",
                 "files": {"names.txt": "fixtures/names.txt"}, "checks": checks(("says-hello", "Hello"))},
                {"id": "farewell", "split": "holdout", "prompt": "Write greeting.txt saying goodbye.",
                 "files": {"names.txt": "fixtures/names.txt"}, "checks": checks(("says-goodbye", "Goodbye"))},
            ]}, indent=2) + "\n")
        self.individual = os.path.join(self.tmp, "my-skill")
        write(os.path.join(self.individual, "SKILL.md"), "# Greeter\n\nGreet people by name, politely.\n")

    def env(self):
        drop = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")
               and k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        env.update(HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1")
        return env

    def suite_run(self, case):
        r = subprocess.run([PY, EVOLVE, "suite", "run", self.suite, case, "--individual", self.individual],
                           cwd=self.tmp, env=self.env(), capture_output=True, text=True, encoding="utf-8",
                           timeout=300)
        folder = os.path.join(self.evo, "runs", "adhoc", case, "individual")
        result = None
        if os.path.exists(os.path.join(folder, "result.json")):
            with open(os.path.join(folder, "result.json"), encoding="utf-8") as f:
                result = json.load(f)
        return r, [line for line in r.stdout.splitlines() if line.strip()], result

    def calls(self, role):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [c for c in (json.loads(line) for line in f if line.strip()) if c["role"] == role]

    def test_suite_run_prints_and_records_each_check(self):  # covers: SUI-14
        r, lines, result = self.suite_run("greet")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(len(lines), 4, f"the case's line, then one line per check:\n{r.stdout}")
        self.assertIn("greet", lines[0])
        self.assertIn("result.json", lines[0])
        for line, cid in zip(lines[1:], ("has-greeting", "says-hello", "polite")):
            self.assertIn(cid, line, f"one line per check, in the case's order:\n{r.stdout}")
            self.assertRegex(line, r"\bpassed\b")
        self.assertEqual(len(self.calls("agent")), 1, "the stand-in ran once as the agent")
        judge = self.calls("judge")
        self.assertEqual(len(judge), 1, "and once as the judge")
        self.assertIn("judge-model-e2e", judge[0]["argv"])
        self.assertIn(RUBRIC, judge[0]["stdin"] or "")
        self.assertIn(GREETING.strip(), judge[0]["stdin"] or "")

        self.assertIsNotNone(result, f"result.json was written:\n{out}")
        self.assertEqual(result.get("outcome"), "done")
        got = result.get("checks") or []
        self.assertEqual([(c.get("id"), c.get("kind"), c.get("passed")) for c in got],
                         [("has-greeting", "command", True), ("says-hello", "file", True),
                          ("polite", "judge", True)], f"result.json's checks:\n{json.dumps(got, indent=1)}")
        self.assertEqual(got[0].get("exit"), 0)
        self.assertEqual(got[2].get("verdict"), "pass")
        self.assertEqual(result.get("tokens"), {"used": AGENT_TOKENS + JUDGE_TOKENS, "agent": AGENT_TOKENS,
                                                "judge": JUDGE_TOKENS})

    def test_a_failed_check_prints_its_evidence_and_skips_the_judge(self):  # covers: SUI-14
        r, lines, result = self.suite_run("farewell")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, f"the exit code follows the run's outcome (done), not the checks:\n{out}")
        self.assertEqual(result.get("outcome") if result else None, "done", out)
        self.assertEqual(len(lines), 4, f"the case's line, then one line per check:\n{out}")
        self.assertIn("has-greeting", lines[1])
        self.assertRegex(lines[1], r"\bpassed\b")
        self.assertIn("says-goodbye", lines[2])
        self.assertRegex(lines[2], r"\bfailed\b")
        self.assertIn("Goodbye", lines[2], "a failure's line carries the first line of its evidence")
        self.assertIn("polite", lines[3])
        self.assertRegex(lines[3], r"\bfailed\b|not run|skipped")
        self.assertEqual(self.calls("judge"), [], "the judge never runs after a mechanical failure")
        self.assertIsNotNone(result, out)
        got = {c.get("id"): c for c in result.get("checks") or []}
        self.assertIs(got.get("says-goodbye", {}).get("passed"), False, got)
        self.assertIs(got.get("polite", {}).get("skipped"), True, got)


if __name__ == "__main__":
    unittest.main()
