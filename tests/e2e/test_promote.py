"""End-to-end: promoting a sample winner from the command line (spec .regista/specs/promote.json, PRM-1).

The sample: a fake Regista checkout (tests/test_genome_seed.py's build_checkout, a git repository with one commit on
`main`) seeded as the genome `seed`, and one child `seed-m1` that rewrites agents/builder.md (genome.make_child). The
generation `e2e-promote` is written by hand in the shape a finished generation leaves (runs/<g>/generation.json with
its axes, the replays' result.json and the child's mutation.json): one screening and one held-out case, seed-m1 the
winner. The checkout's `origin` is a local bare repository; evolve.json sets promote_checks to `true` and pr_command
to a stand-in that records its arguments and its --body-file's content and prints a fake URL. REGISTA_EVOLVE_HOME and
HOME are temporary folders; nothing touches this repository or GitHub.

The commands run: `evolve.py report e2e-promote` (a fixture check that the sample is a valid generation), then
`evolve.py promote seed-m1 --generation e2e-promote --repo <checkout>`.

Interface assumed: pr_command is a command string (here one executable path) to which promote adds --base, --head,
--title and --body-file; the pull request's body is the generation report, reports/<g>.md as it stands after promote,
which is also the branch's docs/generations/<g>.md.

Run: python3 -m unittest discover -s genome/tests/e2e"""
import importlib
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
from harness import GENOME, PY  # noqa: E402

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
SCRIPTS = os.path.join(GENOME, "scripts")
GEN = "e2e-promote"
WINNER = "seed-m1"
BRANCH = f"genome/{GEN}-{WINNER}"
TARGET = "agents/builder.md"
SCREEN, HOLDOUT = "m1-multiply", "m2-subtract"
IDS = ["MUL-1", "SUB-1"]
PR_URL = "https://example.test/acme/regista/pull/7"
IDENTITY = {"GIT_AUTHOR_NAME": "Promote E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com",
            "GIT_COMMITTER_NAME": "Promote E2E", "GIT_COMMITTER_EMAIL": "e2e@example.com"}

PR_STANDIN = r'''#!@PYTHON@
import json, os, sys
args = sys.argv[1:]
def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
body_file = opt("--body-file")
body = None
if body_file:
    path = body_file if os.path.isabs(body_file) else os.path.join(os.getcwd(), body_file)
    if os.path.isfile(path):
        body = open(path, encoding="utf-8").read()
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "body": body}) + "\n")
print(@URL@)
'''


def load_unit_module(name):
    """genome/tests/<name>.py under another module name (genome/tests/e2e has its own test_genome_seed.py)."""
    spec = importlib.util.spec_from_file_location("unit_" + name, os.path.join(GENOME, "tests", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_json(path, data):
    write(path, json.dumps(data, indent=2) + "\n")


class PromoteSampleWinner(unittest.TestCase):
    def setUp(self):
        self.seedtests = load_unit_module("test_genome_seed")
        self.tmp = tempfile.mkdtemp(prefix="regista-e2e-promote-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.evo)
        self.env = {**self.seedtests.evolve_env(self.home, self.evo), **IDENTITY}

        # The target checkout and its bare remote.
        self.repo, self.head = self.seedtests.build_checkout(self.tmp, name="target")
        self.bare = os.path.join(self.tmp, "remote.git")
        self.git(self.tmp, "init", "-q", "--bare", self.bare)
        self.git(self.repo, "remote", "add", "origin", self.bare)
        self.git(self.repo, "push", "-q", "origin", "main")
        self.remote_before = self.refs()

        # The genomes: the seed and one child that rewrites the builder prompt.
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            gn = importlib.import_module("genome")
        finally:
            sys.dont_write_bytecode = old
        profile = gn.load_profile(self.seedtests.PROFILE, source=self.repo)
        gn.seed(self.evo, profile, self.repo, "seed")
        self.new_builder = self.seedtests.EVOLVABLE[TARGET] + "Read the step's frozen tests first.\n"
        gn.make_child(self.evo, profile, self.repo, WINNER, ["seed"], files={TARGET: self.new_builder.encode()})
        with open(os.path.join(self.evo, "genomes", "seed", "genome.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["base"]["commit"], self.head, "fixture: base.commit is the checkout's HEAD")
        write_json(os.path.join(self.evo, "genomes", WINNER, "mutation.json"), {
            "parent": "seed", "kind": "rewrite", "target": TARGET, "model": "opus",
            "diagnosis": "The builder stopped with BLOCKED instead of reading the frozen tests.",
            "tokens": {"used": 1600, "input_tokens": 1200, "cache_creation_input_tokens": 60, "output_tokens": 340,
                       "cache_read_input_tokens": 5000},
            "failure": {"id": f"{SCREEN}/seed/blocked", "kind": "blocked", "case": SCREEN,
                        "summary": "The builder reported BLOCKED."},
            "created_at": "2026-10-31T10:00:00+00:00"})

        # The generation, as a finished one leaves it.
        runs = {("seed", SCREEN): ("blocked", 40000), (WINNER, SCREEN): ("approved", 30000),
                ("seed", HOLDOUT): ("blocked", 45000), (WINNER, HOLDOUT): ("approved", 50000)}
        for (genome, case), (outcome, used) in runs.items():
            self.result(case, genome, outcome, used)
        axes = {"seed": {"requirements": 0.0, "approved": 0.0, "review_rounds": 0.0, "findings": 0.0},
                WINNER: {"requirements": 1.0, "approved": 1.0, "review_rounds": 1.0, "findings": 0.0}}
        write_json(os.path.join(self.evo, "runs", GEN, "generation.json"), {
            "id": GEN, "project": "sample", "target": TARGET, "base": "seed", "budget": 1000000, "seed": 3,
            "started_at": "2026-10-31T10:00:00", "updated_at": "2026-10-31T11:00:00",
            "spent": {"replays": 165000, "reflections": 1600, "total": 166600}, "stopped": None,
            "children": [WINNER], "screening": {"seed": {SCREEN: "blocked"}, WINNER: {SCREEN: "approved"}},
            "front": [WINNER], "holdout": {"seed": {HOLDOUT: "blocked"}, WINNER: {HOLDOUT: "approved"}},
            "winner": WINNER, "why": f"On the 1 held-out case, {WINNER} was no worse than the base seed on "
                                     f"requirements and approval and better on requirements.",
            "population": 1, "discarded": [], "limited": [], "making": None, "abandoned": [], "candidates": [WINNER],
            "axes": {"screening": {g: {**a, "tokens": float(runs[(g, SCREEN)][1])} for g, a in axes.items()},
                     "holdout": {g: {**a, "tokens": float(runs[(g, HOLDOUT)][1])} for g, a in axes.items()}}})

        # evolve.json: quick checks and the stand-in pull-request command.
        self.pr_log = os.path.join(self.tmp, "pr-calls.jsonl")
        self.pr_command = os.path.join(self.tmp, "bin", "pr-standin")
        write(self.pr_command, PR_STANDIN.replace("@PYTHON@", PY).replace("@LOG@", repr(self.pr_log))
              .replace("@URL@", repr(PR_URL)))
        os.chmod(self.pr_command, 0o755)
        write_json(os.path.join(self.evo, "evolve.json"),
                   {"require_sandbox": False, "promote_checks": ["true"], "pr_command": self.pr_command})

    def git(self, cwd, *args):
        return self.seedtests.git(cwd, *args)

    def refs(self):
        out = self.git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def result(self, case, genome, outcome, used):
        approved = outcome == "approved"
        folder = os.path.join(self.evo, "runs", GEN, case, genome)
        ids = [i for i in IDS if i[:3].lower() in case]
        write_json(os.path.join(folder, "result.json"), {
            "project": "sample", "case": case, "genome": genome, "generation": GEN, "model": "haiku",
            "outcome": outcome, "stop_reason": "an approving review was recorded and the latest gate run passed"
            if approved else "claude reported BLOCKED", "hard_failure": None,
            "final_gates": {"tests": {"status": "pass", "exit": 0, "seconds": 1.0, "tail": "OK"}},
            "started_at": "2026-10-31T10:00:00", "ended_at": "2026-10-31T10:20:00", "tokens": {"used": used},
            "scores": {
                "gates": {"value": 1, "failed": False, "results": {"tests": "pass"}, "better": "higher"},
                "requirements": {"value": 1.0 if approved else 0.0, "failed": not approved,
                                 "passing": ids if approved else [], "not_proven": [] if approved else ids,
                                 "of": ids, "better": "higher"},
                "review_rounds": {"value": 1 if approved else 0, "failed": not approved, "better": "lower"},
                "correct_blocked": {"value": 1 if approved else 0, "failed": not approved, "blocked": not approved,
                                    "should_block": False, "better": "higher"},
                "finding_severity": {"value": 0, "blocker": 0, "major": 0, "failed": False, "better": "lower"},
                "tokens": {"value": used, "failed": False, "better": "lower"}}})
        if approved:
            write_json(os.path.join(folder, "copies", "reviews", "r1.json"),
                       {"verdict": "approve", "summary": "Reviewed.", "checked": ids, "files": ["calc.py"],
                        "findings": [], "fingerprint": "r1", "at": "2026-10-31T10:10:00", "base": "abc1234"})

    def run_evolve(self, *args):
        return subprocess.run([PY, EVOLVE, *args], cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, timeout=120)

    def test_promote_pushes_the_branch_and_asks_for_one_pull_request_with_the_report(self):  # covers: PRM-1
        report = self.run_evolve("report", GEN)
        self.assertEqual(report.returncode, 0, f"fixture: the sample generation reports:\n{report.stdout}"
                                               f"{report.stderr}")

        done = self.run_evolve("promote", WINNER, "--generation", GEN, "--repo", self.repo)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn(PR_URL, out, "the pull request's URL is printed")

        after = self.refs()
        ref = "refs/heads/" + BRANCH
        self.assertIn(ref, after, f"the remote gains {BRANCH}:\n{out}")
        self.assertEqual({k: v for k, v in after.items() if k != ref}, self.remote_before,
                         "main on the remote is unchanged, and no other branch was pushed")
        tip = after[ref]
        self.assertEqual(self.git(self.bare, "show", f"{tip}:{TARGET}") + "\n", self.new_builder,
                         "the branch holds the winner's builder prompt")
        changed = sorted(self.git(self.bare, "diff", "--name-only", "main", tip).splitlines())
        self.assertEqual(changed, sorted([TARGET, f"docs/generations/{GEN}.md"]), "and the report, nothing else")
        on_branch = self.git(self.bare, "show", f"{tip}:docs/generations/{GEN}.md")
        self.assertIn(WINNER, on_branch)

        with open(self.pr_log, encoding="utf-8") as f:
            calls = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(calls), 1, f"the stand-in was asked for one pull request: {calls}")
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertEqual(argv[argv.index("--head") + 1], BRANCH, argv)
        with open(os.path.join(self.evo, "reports", GEN + ".md"), encoding="utf-8") as f:
            written = f.read()
        self.assertEqual((calls[0]["body"] or "").strip(), written.strip(), "the pull request's body is the report")
        self.assertEqual(on_branch.strip(), written.strip(), "the branch's docs/generations file is the report")
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "the user's checkout is untouched")


if __name__ == "__main__":
    unittest.main()
