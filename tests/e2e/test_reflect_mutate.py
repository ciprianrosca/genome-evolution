"""End-to-end: one aimed mutation from the command line (spec .regista/specs/reflect-mutate.json, LOOP-12).

`evolve.py genome seed` seeds this repository with its defaults (id seed, profile genome/profiles/regista.json); a replay
record of the seed with one major review finding is written by hand under runs/g1/<case>/seed/; then
`evolve.py mutate seed --case <case> --failure <id> --target agents/builder.md --generation g1` runs with a
stand-in claude (evolve.json `claude_command`) that answers with a fixed diagnosis and a rewritten builder prompt.
The child must exist, the output must print its id, the failure and the diagnosis, and `evolve.py genome show
<child>` must name its parent. REGISTA_EVOLVE_HOME and HOME are temporary folders; the repository is only read.

Interface assumed: the failure's id comes from evolve.failures(home, generation, case, genome) (also printed by
`evolve.py failures`); mutate's profile and source default to genome/profiles/regista.json and the Regista checkout, as seed's do.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# The genome folder, and the Regista checkout that holds it (reached only by path and as an outside program).
GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REGISTA_CHECKOUT = os.path.dirname(GENOME)
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
PY = sys.executable
CASE = "m1-double"

STANDIN = r'''#!@PYTHON@
import json, os, sys
CONTROL = @CONTROL@
args = sys.argv[1:]
if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v") for a in args):
    print("2.1.312 (Claude Code)")
    sys.exit(0)
with open(os.path.join(CONTROL, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args}) + "\n")
with open(os.path.join(CONTROL, "reply.txt"), encoding="utf-8") as f:
    reply = f.read()
usage = {"input_tokens": 1200, "cache_creation_input_tokens": 60, "output_tokens": 340, "cache_read_input_tokens": 5000}
result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": reply,
          "session_id": "reflect-e2e", "total_cost_usd": 0, "usage": usage}
fmt = args[args.index("--output-format") + 1] if "--output-format" in args[:-1] else None
if fmt == "stream-json":
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": "reflect-e2e"}) + "\n")
    sys.stdout.write(json.dumps({"type": "assistant", "session_id": "reflect-e2e", "message": {
        "id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": reply}], "usage": usage}}) + "\n")
    sys.stdout.write(json.dumps(result) + "\n")
elif fmt == "json":
    sys.stdout.write(json.dumps(result) + "\n")
else:
    sys.stdout.write(reply)
'''

DIAGNOSIS = "The builder skipped the empty input the spec names, so the reviewer flagged it (diagnosis-e2e-5)."


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class MutateFromTheCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-e2e-mutate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evo = os.path.join(self.tmp, "evolve-home")
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        self.env = {k: v for k, v in os.environ.items()
                    if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_", "ANTHROPIC_"))}
        self.env.update({"REGISTA_EVOLVE_HOME": self.evo, "HOME": home, "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        self.control = os.path.join(self.tmp, "control")
        os.makedirs(self.control)
        claude = os.path.join(self.tmp, "bin", "claude")
        write(claude, STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control)))
        os.chmod(claude, 0o755)
        write(os.path.join(self.evo, "evolve.json"), json.dumps(
            {"holdout_pct": 0, "claude_command": claude, "task_model": "haiku", "reflection_model": "opus"}) + "\n")

    def evolve(self, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, timeout=300)

    def write_replay(self):
        """A replay of the seed: gates pass, every requirement proven, and one major review finding."""
        folder = os.path.join(self.evo, "runs", "g1", CASE, "seed")
        ids = ["DBL-1", "DBL-2"]
        gates = {"tests": {"status": "pass", "exit": 0, "seconds": 1.0, "tail": "OK"}}
        write(os.path.join(folder, "result.json"), json.dumps({
            "project": "doubler", "case": CASE, "genome": "seed", "generation": "g1", "model": "haiku",
            "outcome": "ended", "stop_reason": "claude ended (exit 0) without an approving review with passing gates, "
                                              "a BLOCKED report or a hard failure",
            "hard_failure": None, "final_gates": gates, "claude_exit": 0,
            "scores": {"gates": {"value": 1, "failed": False, "results": {"tests": "pass"}},
                       "requirements": {"value": 1.0, "failed": False, "passing": ids, "not_proven": [], "of": ids},
                       "correct_blocked": {"value": 1, "failed": False, "blocked": False, "should_block": False}},
            "tokens": {"used": 1000}}, indent=2) + "\n")
        write(os.path.join(folder, "copies", "reviews", "r1.json"), json.dumps({
            "verdict": "changes", "summary": "One problem.", "checked": ids, "files": ["src/double.py"],
            "findings": [{"severity": "major", "file": "src/double.py", "line": 7,
                          "issue": "double('') crashes instead of raising ValueError (finding-e2e-3).",
                          "fix": "Raise ValueError for empty input."}],
            "fingerprint": "r1", "at": "2026-10-30T10:10:00", "base": "abc1234"}, indent=2) + "\n")
        write(os.path.join(folder, "copies", "gates", "history.jsonl"), json.dumps(
            {"date": "2026-10-30T10:05:00", "commit": "abc1234",
             "results": [{"name": "tests", "exit": 0, "seconds": 1.0, "status": "pass"}]}) + "\n")
        write(os.path.join(folder, "stream.jsonl"), json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "session_id": "s"}) + "\n")

    def failure_id(self):
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import evolve; "
                "print(json.dumps(evolve.failures(sys.argv[2], 'g1', sys.argv[3], 'seed')))")
        r = subprocess.run([PY, "-c", code, os.path.join(GENOME, "scripts"), self.evo, CASE], cwd=self.tmp,
                           capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, f"evolve.failures:\n{output(r)}")
        found = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(len(found), 1, f"one failure: the major review finding\n{found}")
        return found[0]["id"]

    def test_mutate_creates_a_child_aimed_at_the_failure_and_show_names_its_parent(self):  # covers: LOOP-12
        seeded = self.evolve("genome", "seed")
        self.assertEqual(seeded.returncode, 0, f"genome seed failed:\n{output(seeded)}")
        self.write_replay()
        failure = self.failure_id()
        listed = self.evolve("failures", CASE, "--genome", "seed", "--generation", "g1")
        self.assertEqual(listed.returncode, 0, f"evolve.py failures:\n{output(listed)}")
        self.assertIn(failure, listed.stdout, "failures prints each failure with its id")

        with open(os.path.join(REGISTA_CHECKOUT, "agents", "builder.md"), encoding="utf-8") as f:
            new_text = f.read() + "\nBefore you ask for review, check every edge case the spec names.\n"
        body = json.dumps({"diagnosis": DIAGNOSIS, "text": new_text}, indent=2)
        write(os.path.join(self.control, "reply.txt"), f"My analysis.\n\n```json\n{body}\n```\n")

        r = self.evolve("mutate", "seed", "--case", CASE, "--failure", failure, "--target", "agents/builder.md",
                        "--generation", "g1")
        self.assertEqual(r.returncode, 0, f"evolve.py mutate failed:\n{output(r)}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        made = [g for g in os.listdir(os.path.join(self.evo, "genomes")) if g != "seed"]
        self.assertEqual(len(made), 1, f"one child genome: {made}\n{output(r)}")
        child = made[0]
        self.assertRegex(child, r"^seed-m\d+$", "the default child id is <parent>-m<n>")
        self.assertRegex(r.stdout, rf"(?<![\w-]){re.escape(child)}(?![\w-])", f"mutate prints the child's id\n{output(r)}")
        self.assertIn(failure, r.stdout, f"mutate prints the failure it targets\n{output(r)}")
        self.assertIn("diagnosis-e2e-5", r.stdout, f"mutate prints the diagnosis\n{output(r)}")
        with open(os.path.join(self.evo, "genomes", child, "files", "agents", "builder.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), new_text, "the child holds the rewritten builder prompt")

        shown = self.evolve("genome", "show", child)
        self.assertEqual(shown.returncode, 0, f"genome show {child}:\n{output(shown)}")
        self.assertTrue(any(re.search(r"(?i)parent", line) and re.search(r"(?<![\w-])seed(?![\w-])", line)
                            for line in shown.stdout.splitlines()),
                        f"genome show names the child's parent, seed, on its parents line:\n{shown.stdout}")


if __name__ == "__main__":
    unittest.main()
