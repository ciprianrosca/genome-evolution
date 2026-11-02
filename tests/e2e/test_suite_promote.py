"""End-to-end: a made-up skill in a subfolder of a repository, evolved with `generation --suite` and promoted to a
stand-in for GitHub (spec .regista/specs/suite-promote.json, SUI-11).

In a temporary folder the test builds a made-up git repository on `main` holding a top-level README.txt and a
made-up skill at assistants/helper/skills/greet/ (guide.md, its only evolvable file, and a frozen notes.txt). Its
`origin` is a local bare repository standing in for GitHub. Outside the repository: the skill's profile (guide.md
evolvable; one check, a small script that fails unless guide.md sits at its path under the subfolder of the folder it
runs in), a suite of four cases (two training, two held out) whose file checks look for ALPHA-WORD or BRAVO-WORD in
answer.txt, and evolve.json with a stand-in claude (require_sandbox false, population 1) and a stand-in pull-request
command that records its arguments and prints a fake URL. The stand-in claude plays both parts, told apart by --model:
the agent writes answer.txt holding the installed guide.md's text; the reflection answers with a rewrite asking for both
words, so the one child passes every check the seed fails and wins. HOME and REGISTA_EVOLVE_HOME are temporary
folders; nothing real runs and nothing reaches the network.

The commands run as separate processes: `evolve.py genome seed --profile <p> --source <skill>`, `evolve.py generation
--suite <suite> --profile <p> --source <skill> --target guide.md --budget N --id e2e-suite-promote --seed 7 --yes`,
`evolve.py report e2e-suite-promote`, then `evolve.py promote <winner> --generation e2e-suite-promote --profile <p>
--source <skill> --onto main` (the repository is the git top of the skill's folder). The test checks: every command
exits 0; the stand-in GitHub gains exactly one new branch whose diff from main holds only the skill's guide.md at its
path under the subfolder and evolve-reports/e2e-suite-promote.md; main is unchanged there and in the repository; the
pull-request command is called once, against main.

Interface assumed: promote keeps its existing form `promote <genome> --generation <g>`, with --profile and --source
added; the profile's `checks` are command strings run at the top of the temporary worktree.

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

INSTALL = "made-up-install/greet"
TASK_MODEL = "made-up-task-model"
REFLECT_MODEL = "made-up-reflect-model"
GEN = "e2e-suite-promote"
BUDGET = 50_000_000
SKILL_REL = "assistants/helper/skills/greet"
MAPPED = f"{SKILL_REL}/guide.md"
REPORT = f"evolve-reports/{GEN}.md"
SEED_GUIDE = "# Made-up greeting guide\n\nWrite the answer in plain words, in a few short lines for the reader.\n"
REWRITE = SEED_GUIDE + "Always include ALPHA-WORD and BRAVO-WORD in every answer you write.\n"
PR_URL = "https://example.test/made-up/assistants/pull/11"
IDENTITY = {"GIT_AUTHOR_NAME": "Suite Promote E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com",
            "GIT_COMMITTER_NAME": "Suite Promote E2E", "GIT_COMMITTER_EMAIL": "e2e@example.com"}

CLAUDE = r'''#!@PYTHON@
import json, os, sys, threading
TASK_MODEL = @TASK_MODEL@
INSTALL = @INSTALL@
REWRITE = @REWRITE@
args = sys.argv[1:]

def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if opt("--model") == TASK_MODEL:
    try:
        with open(os.path.join(os.getcwd(), *INSTALL.split("/"), "guide.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    with open("answer.txt", "w", encoding="utf-8") as f:
        f.write(text)
    usage = {"input_tokens": 1500, "output_tokens": 200}
    emit({"type": "system", "subtype": "init", "session_id": "a-1", "apiKeySource": "none"})
    emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Wrote answer.txt."}],
                                           "usage": usage}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "usage": usage})
    sys.exit(0)

def slurp():
    try:
        while os.read(0, 65536):
            pass
    except OSError:
        pass
reader = threading.Thread(target=slurp, daemon=True)
reader.start()
reader.join(3)
body = json.dumps({"diagnosis": "The made-up guide never asked for the words the checks look for.", "text": REWRITE})
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

CHECK = r'''#!@PYTHON@
import os, sys
path = os.path.join(os.getcwd(), *@MAPPED@.split("/"))
if not os.path.isfile(path):
    print("made-up check: guide.md is not at its path under the subfolder")
    sys.exit(1)
print("made-up check: ok")
'''

PR_STANDIN = r'''#!@PYTHON@
import json, sys
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
print(@URL@)
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


class SuitePromoteEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-promote-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        fill = {"@PYTHON@": PY, "@TASK_MODEL@": repr(TASK_MODEL), "@INSTALL@": repr(INSTALL),
                "@REWRITE@": repr(REWRITE), "@MAPPED@": repr(MAPPED), "@URL@": repr(PR_URL)}

        def script(name, text):
            for key, value in fill.items():
                text = text.replace(key, value)
            path = os.path.join(self.tmp, "bin", name)
            write(path, text, 0o755)
            return path

        self.pr_log = os.path.join(self.tmp, "pr-calls.jsonl")
        fill["@LOG@"] = repr(self.pr_log)
        claude = script("claude", CLAUDE)
        check = script("made-up-check", CHECK)
        pr_command = script("pr-standin", PR_STANDIN)

        # The repository with the skill in a subfolder, and its stand-in GitHub.
        self.repo = os.path.join(self.tmp, "made-up-repo")
        self.skill = os.path.join(self.repo, *SKILL_REL.split("/"))
        write(os.path.join(self.repo, "README.txt"), "A made-up repository of assistants.\n")
        write(os.path.join(self.skill, "guide.md"), SEED_GUIDE)
        write(os.path.join(self.skill, "notes.txt"), "Frozen made-up notes.\n")
        self.git(self.tmp, "init", "-q", "-b", "main", self.repo)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "made-up repository")
        self.bare = os.path.join(self.tmp, "stand-in-github.git")
        self.git(self.tmp, "init", "-q", "--bare", self.bare)
        self.git(self.repo, "remote", "add", "origin", self.bare)
        self.git(self.repo, "push", "-q", "origin", "main")
        self.main_head = self.git(self.repo, "rev-parse", "main")

        # The profile, the suite and evolve.json, outside the repository.
        self.profile = os.path.join(self.tmp, "profiles", "made-up-greet.json")
        write(self.profile, json.dumps({"name": "made-up-greet", "evolvable": ["guide.md"], "traits": {},
                                        "checks": [check]}) + "\n")
        words = {"s1": "ALPHA-WORD", "s2": "BRAVO-WORD", "h1": "ALPHA-WORD", "h2": "BRAVO-WORD"}
        cases = [{"id": cid, "split": "train" if cid.startswith("s") else "holdout",
                  "prompt": f"Write answer.txt answering made-up question {cid}.",
                  "checks": [{"id": "answer-exists", "file": "answer.txt", "exists": True},
                             {"id": "has-word", "file": "answer.txt", "contains": word}]}
                 for cid, word in words.items()]
        self.suite = os.path.join(self.tmp, "suites", "made-up-greetings.json")
        write(self.suite, json.dumps({"name": "made-up-greetings", "install": INSTALL, "model": TASK_MODEL,
                                      "cases": cases}, indent=2) + "\n")
        write(os.path.join(self.evo, "evolve.json"), json.dumps(
            {"claude_command": [claude], "require_sandbox": False, "reflection_model": REFLECT_MODEL,
             "population": 1, "pr_command": pr_command}) + "\n")

    def env(self):
        drop = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")
               and k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        env.update(HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", **IDENTITY)
        return env

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", env=self.env())
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def evolve(self, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=self.tmp, env=self.env(), capture_output=True, text=True,
                              encoding="utf-8", stdin=subprocess.DEVNULL, timeout=600)

    def remote_refs(self):
        out = self.git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def test_seed_evolve_report_and_promote_open_one_branch_on_the_stand_in(self):  # covers: SUI-11
        r = self.evolve("genome", "seed", "--profile", self.profile, "--source", self.skill)
        self.assertEqual(r.returncode, 0, output(r))

        r = self.evolve("generation", "--suite", self.suite, "--profile", self.profile, "--source", self.skill,
                        "--target", "guide.md", "--budget", str(BUDGET), "--id", GEN, "--seed", "7", "--yes")
        self.assertEqual(r.returncode, 0, output(r))
        state = read_json(os.path.join(self.evo, "runs", GEN, "generation.json"))
        winner = state.get("winner")
        self.assertTrue(winner, f"fixture: the child that asks for both words wins: {state}\n{output(r)}")

        r = self.evolve("report", GEN)
        self.assertEqual(r.returncode, 0, output(r))
        self.assertIn(winner, r.stdout, "the report names the winner")

        before = self.remote_refs()
        status = self.git(self.repo, "status", "--porcelain", "--untracked-files=all")
        r = self.evolve("promote", winner, "--generation", GEN, "--profile", self.profile, "--source", self.skill,
                        "--onto", "main")
        shown = output(r)
        self.assertEqual(r.returncode, 0, shown)
        self.assertNotIn("Traceback", shown)

        after = self.remote_refs()
        new = {ref: sha for ref, sha in after.items() if ref not in before}
        self.assertEqual(len(new), 1, f"the stand-in GitHub gains exactly one branch: {after}\n{shown}")
        self.assertEqual({k: v for k, v in after.items() if k not in new}, before,
                         "main on the stand-in GitHub is unchanged")
        (ref, tip), = new.items()
        changed = sorted(self.git(self.bare, "diff", "--name-only", "main", tip).splitlines())
        self.assertEqual(changed, sorted([MAPPED, REPORT]),
                         f"only the skill's guide.md at its path under the subfolder and the report:\n{shown}")
        self.assertEqual(self.git(self.bare, "show", f"{tip}:{MAPPED}") + "\n", REWRITE, "the winner's guide.md")
        self.assertIn(winner, self.git(self.bare, "show", f"{tip}:{REPORT}"), "the report on the branch")
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), self.main_head, "main is unchanged locally")
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=all"), status,
                         "the user's checkout is untouched")

        with open(self.pr_log, encoding="utf-8") as f:
            calls = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(calls), 1, f"the pull-request command is called once: {calls}")
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertEqual(argv[argv.index("--head") + 1], ref[len("refs/heads/"):], argv)


if __name__ == "__main__":
    unittest.main()
