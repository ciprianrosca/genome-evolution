"""End-to-end: a light generation over the resume-tailoring example's suite (spec .regista/specs/resume-example.json,
EX-3).

The test copies genome/examples/resume-tailoring to a temporary folder and runs it on a made-up copy of the skill: a
short made-up SKILL.md in a temporary git repository, given as --source. The real skill is never read. A stand-in
claude (evolve.json claude_command, require_sandbox false) plays three parts, told apart by --model and the folder
it runs in:
- the agent (the suite's task model, run in a workspace holding the installed skill): it reads the fact bank under
  HOME (personal-assistants/artemis/profile/master_cv.md, which the suite places under its home folder) and writes
  the resume (applications/<folder>/resume.md under HOME, or wherever the case's file checks look). Whether the
  resume is good depends on the installed SKILL.md: without the phrase TRACE-EVERY-CLAIM it adds a made-up claim
  (a number not in the fact bank) and a [VERIFY] marker, which the claim-tracing check catches; with it, the resume
  holds only the fact bank's own lines.
- the reflection (the reflection model): it answers with a rewrite of SKILL.md that adds the phrase.
- the judge (any other call, run in an empty folder): it answers pass.
HOME and REGISTA_EVOLVE_HOME are temporary folders; nothing real runs.

As separate processes: `evolve.py genome seed`, `evolve.py generation --suite ... --light --yes` and `evolve.py
report`. Each exits 0; generation.json names a winner or a plain reason; the report is printed; and the agent ran with
HOME set to the home folder inside its workspace, where it found the fact bank.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from harness import EVOLVE, GENOME, PY  # noqa: E402


def _load_by_path(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sg = _load_by_path("e2e_suite_generation_helpers", "test_suite_generation.py")
write, read_json, output = _sg.write, _sg.read_json, _sg.output

EXAMPLE = os.path.join(GENOME, "examples", "resume-tailoring")
REFLECT_MODEL = "made-up-reflect-model"
GEN = "e2e-resume"
BUDGET = 50_000_000
PHRASE = "TRACE-EVERY-CLAIM"
SEED_SKILL = """---
name: resume-tailoring
description: Tailors a made-up person's resume to a job description, using only facts from the fact bank.
---

# Resume tailoring (a made-up copy for tests)

When asked to tailor a resume for a job description:

1. Read the job description and write a short fit note in applications/<date>_<company>_<role>/fit.md.
2. Start from the latest approved CV in cvs/ and the fact bank in profile/master_cv.md.
3. Write applications/<date>_<company>_<role>/resume.md, keeping the strongest matching experience first.
4. Write the summary in the first person, without pronouns like he or she.
5. Render the resume with tools/render_resume.py.
6. Leave no placeholder or review markers in the files you write.
7. Keep the resume to two pages and the summary to three sentences.
"""

CLAUDE = r'''#!@PYTHON@
import json, os, re, sys, threading
LOG = @LOG@
TASK_MODEL = @TASK_MODEL@
REFLECT_MODEL = @REFLECT_MODEL@
INSTALL = @INSTALL@
TARGETS = @TARGETS@
SEED = @SEED@
PHRASE = @PHRASE@
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

def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

installed = os.path.join(os.getcwd(), *INSTALL.split("/"), "SKILL.md")
if opt("--model") == TASK_MODEL and os.path.isfile(installed):
    with open(installed, encoding="utf-8") as f:
        skill = f.read()
    home = os.environ.get("HOME", "")
    artemis = os.path.join(home, "personal-assistants", "artemis")
    try:
        with open(os.path.join(artemis, "profile", "master_cv.md"), encoding="utf-8") as f:
            bank = f.read()
        found = True
    except OSError:
        bank, found = "# Made-up resume\n", False
    keep = []
    for line in bank.splitlines():
        if any(m in line for m in ("[GAP]", "[VERIFY]", "[CONF]", "{{")):
            continue
        if re.search(r"\b(he|his|him)\b", line, re.I):
            continue
        keep.append(line.replace("**", ""))
    text = "\n".join(keep) + "\n"
    good = PHRASE in skill
    if not good:
        text += "\n- Grew yearly revenue by 9876% in one quarter. [VERIFY]\n"
    prompt = (opt("-p") or opt("--print") or "").strip()
    targets = TARGETS.get(prompt) or {}
    md = [p for p in targets.get("md", []) if "applications/" in p]
    for rel in md:
        put(os.path.join(os.getcwd(), *rel.split("/")), text)
    if not md:
        put(os.path.join(artemis, "applications", "2026-11-02_made-up-company_made-up-role", "resume.md"), text)
    for rel in targets.get("other", []):
        put(os.path.join(os.getcwd(), *rel.split("/")), "A made-up placeholder file.\n")
    log({"part": "agent", "cwd": os.getcwd(), "home": home, "config": os.environ.get("CLAUDE_CONFIG_DIR"),
         "bank_found": found, "good": good, "known_prompt": bool(targets)})
    usage = {"input_tokens": 1500, "output_tokens": 200}
    emit({"type": "system", "subtype": "init", "session_id": "a-1", "apiKeySource": "none"})
    emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Wrote the resume."}],
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

if opt("--model") == REFLECT_MODEL:
    log({"part": "reflection"})
    text = SEED + "8. " + PHRASE + ": copy each claim from the fact bank exactly as written there.\n"
    body = json.dumps({"diagnosis": "The made-up skill let claims through that the fact bank doesn't hold.",
                       "text": text})
    reply = "Here is the rewrite.\n\n```json\n" + body + "\n```\n"
    part = "r"
else:
    log({"part": "judge", "model": opt("--model")})
    reply = json.dumps({"verdict": "pass", "reason": "The made-up summary is in the first person and fits the job."})
    part = "j"
usage = {"input_tokens": 1200, "output_tokens": 300}
result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": reply,
          "session_id": part + "-1", "usage": usage}
fmt = opt("--output-format")
if fmt == "stream-json":
    emit({"type": "system", "subtype": "init", "session_id": part + "-1", "apiKeySource": "none"})
    emit({"type": "assistant", "message": {"id": part + "1", "content": [{"type": "text", "text": reply}],
                                           "usage": usage}})
    emit(result)
elif fmt == "json":
    emit(result)
else:
    sys.stdout.write(reply)
sys.stdout.flush()
sys.exit(0)
'''


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Made Up", "-c", "user.email=made-up@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")


def case_targets(root, suite):
    """For each case's prompt: the workspace paths its file checks name (.md files, and other files to create)."""
    targets = {}
    for case in suite.get("cases") or []:
        prompt = case.get("prompt")
        if prompt is None and case.get("prompt_file"):
            with open(os.path.join(root, *case["prompt_file"].split("/")), encoding="utf-8") as f:
                prompt = f.read()
        md, other = [], []
        for check in case.get("checks") or []:
            path = check.get("file")
            if not isinstance(path, str) or check.get("exists") is False:
                continue
            (md if path.endswith(".md") else other).append(path)
        targets[(prompt or "").strip()] = {"md": sorted(set(md)), "other": sorted(set(other))}
    return targets


class ResumeExampleEndToEnd(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(os.path.join(EXAMPLE, "suite.json")), f"{EXAMPLE} has no suite.json")
        self.tmp = tempfile.mkdtemp(prefix="genome-resume-example-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        self.example = os.path.join(self.tmp, "example")
        shutil.copytree(EXAMPLE, self.example, ignore=shutil.ignore_patterns("__pycache__"))
        self.suite = os.path.join(self.example, "suite.json")
        self.profile = os.path.join(self.example, "profile.json")
        suite = read_json(self.suite)
        self.install = suite.get("install") or ".claude/skills/resume-tailoring"
        self.log = os.path.join(self.tmp, "claude-calls.jsonl")
        self.claude = os.path.join(self.tmp, "bin", "claude")
        fill = {"@PYTHON@": PY, "@LOG@": repr(self.log), "@TASK_MODEL@": repr(suite.get("model") or "sonnet"),
                "@REFLECT_MODEL@": repr(REFLECT_MODEL), "@INSTALL@": repr(self.install),
                "@TARGETS@": repr(case_targets(self.example, suite)), "@SEED@": repr(SEED_SKILL),
                "@PHRASE@": repr(PHRASE)}
        text = CLAUDE
        for key, value in fill.items():
            text = text.replace(key, value)
        write(self.claude, text, 0o755)
        write(os.path.join(self.evo, "evolve.json"), json.dumps(
            {"claude_command": [self.claude], "require_sandbox": False, "reflection_model": REFLECT_MODEL,
             "population": 2}) + "\n")
        self.skill = os.path.join(self.tmp, "made-up-skill-repo")
        write(os.path.join(self.skill, "SKILL.md"), SEED_SKILL)
        git(self.skill, "init", "-q", "-b", "main")
        git(self.skill, "add", "-A")
        git(self.skill, "commit", "-q", "-m", "a made-up skill")

    def env(self):
        drop = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")
               and k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR")}
        env.update(HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1")
        return env

    def evolve(self, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=self.tmp, env=self.env(), capture_output=True, text=True,
                              encoding="utf-8", stdin=subprocess.DEVNULL, timeout=600)

    def calls(self, part):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [e for e in (json.loads(line) for line in f if line.strip()) if e.get("part") == part]

    def test_light_generation_on_the_example_names_a_winner_or_why_and_reports_it(self):  # covers: EX-3
        r = self.evolve("genome", "seed", "--profile", self.profile, "--source", self.skill)
        self.assertEqual(r.returncode, 0, output(r))

        r = self.evolve("generation", "--suite", self.suite, "--profile", self.profile, "--source", self.skill,
                        "--target", "SKILL.md", "--budget", str(BUDGET), "--id", GEN, "--seed", "7", "--light",
                        "--yes")
        shown = output(r)
        self.assertEqual(r.returncode, 0, shown)
        self.assertNotIn("Traceback", shown)
        state = read_json(os.path.join(self.evo, "runs", GEN, "generation.json"))
        self.assertEqual((state.get("adapter") or {}).get("kind"), "suite", state)
        self.assertIs(state.get("light"), True, "a light run")
        winner, why = state.get("winner"), state.get("why")
        self.assertTrue(winner or (isinstance(why, str) and why.strip()), f"a winner or a plain reason: {state}")

        # The agent ran with HOME set to the home folder inside its workspace, and found the fact bank there.
        agents = self.calls("agent")
        self.assertTrue(agents, f"the agent ran:\n{shown}")
        for call in agents:
            self.assertEqual(os.path.realpath(call["home"]), os.path.realpath(os.path.join(call["cwd"], "home")),
                             f"HOME is the suite's home folder in the workspace: {call}")
            self.assertTrue(call["bank_found"], f"the fact bank is under HOME: {call}")
            self.assertEqual(call["config"], os.path.join(self.home, ".claude"),
                             "Claude Code keeps the real configuration folder")
        self.assertFalse(os.path.exists(os.path.join(self.home, "personal-assistants")),
                         "nothing was written under the original home folder")

        # The base's made-up claim was caught by the example's checks.
        seed_results = [os.path.join(self.evo, "runs", GEN, case, "seed", "result.json")
                        for case in os.listdir(os.path.join(self.evo, "runs", GEN))
                        if os.path.isfile(os.path.join(self.evo, "runs", GEN, case, "seed", "result.json"))]
        self.assertTrue(seed_results, "the base ran on the suite's cases")
        failed = [c for p in seed_results for c in (read_json(p).get("checks") or []) if not c.get("passed")]
        self.assertTrue(failed, "the base's resume holds a made-up claim, so some check fails")

        r = self.evolve("report", GEN)
        self.assertEqual(r.returncode, 0, output(r))
        self.assertNotIn("Traceback", output(r))
        self.assertTrue(r.stdout.strip(), "the report is printed")
        self.assertIn("Cases passed", r.stdout, output(r))
        if winner:
            self.assertIn(winner, r.stdout, "the report names the winner")


if __name__ == "__main__":
    unittest.main()
