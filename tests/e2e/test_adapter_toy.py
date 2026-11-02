"""End-to-end: a separate program drives the neutral core with a toy adapter (spec .regista/specs/adapter-neutral.json,
ADP-6).

In a temporary folder the test writes:
- a toy target: a git repository on `main` with one evolvable text file (prompt.md) and a frozen settings.json, plus a
  profile for it (outside the repository) with no traits and one check command (a script that records it ran);
- a bare repository standing in for GitHub, the target's `origin`;
- a stand-in pull-request command that records its arguments and prints a fake URL;
- a fake `claude` first on PATH that records it was started and fails loudly;
- a small Python program (PROGRAM below, self-contained: it imports only the standard library and evolve_core and genome
  from this repository's scripts/ folder). It defines a toy adapter (axes accuracy, mistakes and tokens; its runs write
  result.json from a fixed table, no agent) and a stand-in reflect, seeds the genome, runs a light generation
  (population 1, one held-out case), writes the report to a file and promotes the winner onto main. The light settings
  come from the adapter itself (its held-out list has one case) rather than from a shorter list passed to Generation:
  promote checks the generation is finished against the adapter's held-out list, and a generation run on a shorter
  list without generation.json's light record would look unfinished to it.
The test runs that program as a separate process with a temporary HOME and REGISTA_EVOLVE_HOME and checks the result.

Interface assumed (from the spec): genome.load_profile(path, source=...), genome.seed(home, profile, source, id);
evolve_core.check_adapter, new_state(gen_id, project, target, base, budget, seed), Generation(home, config, state,
adapter, profile, source, population, mutate, screening=None, holdout=None).run(), mutate(..., reflect=...),
report_text(home, state, adapter) and promote(home, config, state, genome, adapter, profile, repo, onto, checks=None,
remote='origin', pr_command=...). The report lands on the branch at the base adapter's default path,
evolve-reports/<generation>.md. config is a plain dict with the evolver's usual keys.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from harness import GENOME, PY  # noqa: E402

SCRIPTS = os.path.join(GENOME, "scripts")
GEN = "toy-light"
TARGET = "prompt.md"
REPORT = f"evolve-reports/{GEN}.md"
SEED_PROMPT = "You answer questions.\nKeep each answer short.\n"
AXES = ("accuracy", "mistakes", "tokens")
PR_URL = "https://example.test/toy/target/pull/3"
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC",
           "GIT_AUTHOR_NAME": "Toy E2E", "GIT_AUTHOR_EMAIL": "toy@example.com",
           "GIT_COMMITTER_NAME": "Toy E2E", "GIT_COMMITTER_EMAIL": "toy@example.com"}

PR_STANDIN = r'''#!@PYTHON@
import json, sys
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
print(@URL@)
'''

CHECK = r'''#!@PYTHON@
import os
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(os.getcwd() + "\n")
print("toy check: ok")
'''

FAKE_CLAUDE = r'''#!/bin/sh
echo "fake claude was started: the toy run must not start an agent" >&2
echo "$@" >> "@LOG@"
exit 97
'''

PROGRAM = r'''"""Drives evolve_core with a toy adapter: seed, a light generation, the report and promote."""
import functools
import json
import os
import sys

params = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, params["scripts"])
import evolve_core as core  # noqa: E402
import genome as gn  # noqa: E402

GEN = params["gen"]
TARGET = params["target_file"]
SEED_PROMPT = params["seed_prompt"]
# (role, kind) -> what a run writes; role is "base" for the seed and "child" for a child.
TABLE = {
    ("base", "screen"): {"accuracy": 0.5, "mistakes": 2, "used": 10000, "score": 0.5, "failures": 1},
    ("child", "screen"): {"accuracy": 1.0, "mistakes": 0, "used": 9000, "score": 0.9, "failures": 0},
    ("base", "holdout"): {"accuracy": 0.5, "mistakes": 2, "used": 11000, "score": 0.5, "failures": 0},
    ("child", "holdout"): {"accuracy": 1.0, "mistakes": 0, "used": 8000, "score": 0.9, "failures": 0},
}
SCREENING = [{"id": "s1", "prompt": "Question one"}, {"id": "s2", "prompt": "Question two"}]
HOLDOUT = [{"id": "h1", "prompt": "Held-out question one"}]
KIND = {**{c["id"]: "screen" for c in SCREENING}, **{c["id"]: "holdout" for c in HOLDOUT}}


class ToyAdapter(core.Adapter):
    def __init__(self):
        self.name = "toy-writer"
        self.axes = {"accuracy": "higher", "mistakes": "lower", "tokens": "lower"}
        self.labels = {}
        self.must_not_worsen = ("accuracy", "mistakes")

    def cases(self):
        return {"screening": list(SCREENING), "holdout": list(HOLDOUT)}

    def run(self, case, genome, individual, folder, resume=None):
        row = TABLE[("base" if genome == "seed" else "child", KIND[case["id"]])]
        result = {"outcome": "done", "case": case["id"], "genome": genome, "tokens": {"used": row["used"]},
                  "values": {"accuracy": row["accuracy"], "mistakes": row["mistakes"], "tokens": row["used"]},
                  "score": row["score"], "failures": row["failures"]}
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    def values(self, result):
        return dict(result["values"])

    def score(self, result):
        return result["score"]

    def failures(self, case, genome, folder, result):
        return [{"id": f"{case['id']}/vague-{k}", "kind": "check_failure", "case": case["id"],
                 "summary": f"Answer {k} on {case['id']} was vague.",
                 "evidence": "The answer gave no source for its claim."}
                for k in range(1, int(result.get("failures") or 0) + 1)]

    def estimate(self, home):
        return 10000


def reflect(prompt):
    text = SEED_PROMPT.replace("Keep each answer short.", "Check every fact before replying.")
    body = json.dumps({"diagnosis": "The prompt never asked for sources, so the answers were vague.", "text": text})
    tokens = {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "used": 1500}
    return f"Here is my analysis.\n\n```json\n{body}\n```\n", "stand-in-model", tokens


def main():
    home = params["home"]
    profile = gn.load_profile(params["profile"], source=params["repo"])
    gn.seed(home, profile, params["repo"], "seed")
    config = {"target": "auto", "population": 1, "generations_max": 1, "task_model": "none",
              "reflection_model": "none", "screen_cases": 2, "max_parallel": 1, "budget_tokens": 10 ** 9,
              "holdout_pct": 40, "require_sandbox": False}
    adapter = ToyAdapter()
    screening, holdout = core.check_adapter(adapter)
    state = core.new_state(GEN, adapter.name, TARGET, "seed", 10 ** 9, 7)
    mutate = functools.partial(core.mutate, reflect=reflect)
    stop = core.Generation(home, config, state, adapter, profile, params["repo"], 1, mutate,
                           screening=screening, holdout=holdout).run()
    with open(os.path.join(home, "runs", GEN, "generation.json"), encoding="utf-8") as f:
        state = json.load(f)
    winner = state.get("winner")
    report = core.report_text(home, state, adapter)
    with open(params["report_out"], "w", encoding="utf-8") as f:
        f.write(report)
    if winner:
        core.promote(home, config, state, winner, adapter, profile, params["repo"], "main",
                     remote="origin", pr_command=(params["pr_command"],))
    with open(params["out"], "w", encoding="utf-8") as f:
        json.dump({"stop": stop, "winner": winner}, f)
    print(f"winner: {winner}")
    return 0 if winner else 1


sys.exit(main())
'''


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def read_lines(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line for line in f.read().splitlines() if line.strip()]


class ToyAdapterEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="toy-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        j = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        self.home, self.evo = j("home"), j("evolve-home")
        os.makedirs(self.home)
        os.makedirs(self.evo)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("REGISTA_", "DAEDALUS_", "GIT_", "CLAUDE")) and not k.endswith("_PROJECT_DIR")}
        self.env.update(GIT_ENV, HOME=self.home, REGISTA_EVOLVE_HOME=self.evo, PYTHONDONTWRITEBYTECODE="1",
                        XDG_CONFIG_HOME=os.path.join(self.home, ".config"))

        # The toy target on main, and the bare repository standing in for GitHub.
        self.repo = j("toy-target")
        write(os.path.join(self.repo, TARGET), SEED_PROMPT)
        write(os.path.join(self.repo, "settings.json"), json.dumps({"version": "1.0.0"}) + "\n")
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "toy target")
        self.bare = j("remote.git")
        self.git(self.tmp, "init", "-q", "--bare", self.bare)
        self.git(self.repo, "remote", "add", "origin", self.bare)
        self.git(self.repo, "push", "-q", "origin", "main")

        # The stand-ins: the check, the pull-request command and a fake claude.
        self.check_log, self.pr_log, self.claude_log = j("logs", "checks.log"), j("logs", "pr.jsonl"), \
            j("logs", "claude.log")
        os.makedirs(j("logs"))
        self.check = j("bin", "toy-check")
        write(self.check, CHECK.replace("@PYTHON@", PY).replace("@LOG@", repr(self.check_log)), 0o755)
        self.pr_command = j("bin", "pr-standin")
        write(self.pr_command, PR_STANDIN.replace("@PYTHON@", PY).replace("@LOG@", repr(self.pr_log))
              .replace("@URL@", repr(PR_URL)), 0o755)
        fakebin = j("fakebin")
        write(os.path.join(fakebin, "claude"), FAKE_CLAUDE.replace("@LOG@", self.claude_log), 0o755)
        self.env["PATH"] = fakebin + os.pathsep + os.environ.get("PATH", "")

        # The profile (no traits, one check) and the program.
        self.profile = j("profiles", "toy-writer.json")
        write(self.profile, json.dumps({"name": "toy-writer", "evolvable": [TARGET],
                                        "version": {"file": "settings.json", "key": "version"},
                                        "traits": {}, "checks": [self.check]}, indent=2) + "\n")
        self.program = j("program", "drive_toy.py")
        write(self.program, PROGRAM)
        self.report_out = j("out", "report.md")
        self.out = j("out", "result.json")
        os.makedirs(j("out"))
        self.params = j("program", "params.json")
        write(self.params, json.dumps({"scripts": SCRIPTS, "gen": GEN, "target_file": TARGET,
                                       "seed_prompt": SEED_PROMPT, "home": self.evo, "repo": self.repo,
                                       "profile": self.profile, "pr_command": self.pr_command,
                                       "report_out": self.report_out, "out": self.out}))

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                           env=self.env)
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def refs(self, repo):
        out = self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def test_a_separate_program_runs_a_toy_generation_report_and_promote(self):  # covers: ADP-6
        remote_before, local_before = self.refs(self.bare), self.refs(self.repo)
        main_before = local_before["refs/heads/main"]
        self.assertEqual(remote_before, {"refs/heads/main": main_before}, "fixture: origin holds only main")

        r = subprocess.run([PY, self.program, self.params], cwd=self.tmp, capture_output=True, text=True,
                           env=self.env, timeout=600)
        shown = f"exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        self.assertEqual(read_lines(self.claude_log), [], f"no claude was started:\n{shown}")
        self.assertEqual(r.returncode, 0, shown)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

        # A winner, the generation's own.
        with open(self.out, encoding="utf-8") as f:
            result = json.load(f)
        winner = result.get("winner")
        self.assertTrue(winner, f"the generation has a winner: {result}\n{shown}")
        self.assertNotEqual(winner, "seed", "a child wins")
        self.assertIsNone(result.get("stop"), f"the generation finished: {result}")
        with open(os.path.join(self.evo, "runs", GEN, "generation.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("winner"), winner)

        # The report file names the toy axes and the winner.
        with open(self.report_out, encoding="utf-8") as f:
            report = f.read()
        for axis in AXES:
            self.assertRegex(report, rf"(?i)\b{axis}\b", f"the report names the axis {axis}:\n{report}")
        self.assertRegex(report, rf"(?<![\w-]){re.escape(winner)}(?![\w-])", f"the report names the winner:\n{report}")

        # The stand-in GitHub: exactly one new branch, main unchanged there and in the target.
        remote_after = self.refs(self.bare)
        new = {ref: sha for ref, sha in remote_after.items() if ref not in remote_before}
        self.assertEqual(len(new), 1, f"one new branch on the stand-in GitHub: {remote_after}\n{shown}")
        (ref, tip), = new.items()
        self.assertEqual(remote_after["refs/heads/main"], main_before, "main is unchanged on the stand-in GitHub")
        self.assertEqual(self.refs(self.repo)["refs/heads/main"], main_before, "main is unchanged in the target")
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--untracked-files=all"), "",
                         "the target's checkout is clean")

        # The branch differs from main only in the evolvable file and the report.
        changed = sorted(self.git(self.bare, "diff", "--name-only", main_before, tip).splitlines())
        self.assertEqual(changed, sorted([TARGET, REPORT]), "only the evolvable file and the report")
        with open(os.path.join(self.evo, "genomes", winner, "files", TARGET), encoding="utf-8") as f:
            winner_prompt = f.read()
        self.assertNotEqual(winner_prompt, SEED_PROMPT, "the winner changed the evolvable file")
        self.assertEqual(self.git(self.bare, "show", f"{tip}:{TARGET}"), winner_prompt.strip())
        self.assertRegex(self.git(self.bare, "show", f"{tip}:{REPORT}"), re.escape(winner),
                         "the report on the branch names the winner")

        # The profile's check ran, and the pull request was asked for once, against main.
        self.assertTrue(read_lines(self.check_log), f"the profile's check ran:\n{shown}")
        calls = [json.loads(line) for line in read_lines(self.pr_log)]
        self.assertEqual(len(calls), 1, f"one pull-request call: {calls}\n{shown}")
        argv = calls[0]["argv"]
        self.assertIn("--base", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertEqual(read_lines(self.claude_log), [], "no claude was started")


if __name__ == "__main__":
    unittest.main()
