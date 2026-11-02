"""Promote to the skill's own repository, the report path's hardening and the neutrality of the suite code (spec
.regista/specs/suite-promote.json: SUI-9, SUI-18, SUI-10; SUI-11 is the end-to-end test in
genome/tests/e2e/test_suite_promote.py).

SUI-9: `evolve.py promote <genome> --generation <g> --profile <p> --source <folder> [--repo <checkout>] --onto main`
for a suite generation's winner whose source is a subfolder of a git repository. The fixture: a made-up repository on
`main` with a made-up skill at assistants/helper/skills/greet/ (guide.md is its only evolvable file) and a top-level
guide.md that promote must leave alone; its `origin` is a local bare repository standing in for GitHub. The skill is
seeded and evolved with tests/test_suite_generation.py's GenerationBase pieces (imported as a module, so its tests
don't run twice): its stand-in claude, rewrites A and B, and its suite. Rewrite B wins. The profile has `checks`: one
stand-in check recording the folder it runs in and the files it sees there. Also stand-ins: the pull-request command
(tests/test_promote.py's, recording its arguments) and a recording `git` first on PATH. The user's checkout is on a
branch `feature` with an untracked file of their own.

SUI-18: evolve_core.promote, called on tests/test_adapter_report_promote.py's toy repository (source at the
repository's top, so the winner's mapped path is prompt.md), refuses a report path of '.', a folder (ends with '/'),
one with a '.git' part, or one equal to the winner's promoted file, before any worktree, branch, commit, check, push
or pull request; the helpers are test_adapter_neutral.py's.

SUI-10: genome/scripts/suite.py and genome/scripts/runners.py are read as text and checked for words naming a target or
a host, as test_adapter_neutral.py does for the core; runners.py may name its host (Claude Code, Anthropic).

Everything is made up and lives in temporary folders (HOME and REGISTA_EVOLVE_HOME included). No real claude runs and
nothing reaches the network.

Interface assumed:
- The command line keeps the existing promote form, `promote <genome> --generation <g>`, with --profile and --source
  added; the profile's `checks` are the checks a suite promote runs.
- The branch's name is not fixed by the spec; the tests find it as the one new branch.
- A source outside --repo is refused with a non-zero exit and a short message naming the source folder.
- The unfinished generation is one paused at the usage limit during its held-out runs, so it has no winner yet.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest test_suite_promote -v (from genome/tests)"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import test_adapter_neutral as an  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_adapter_report_promote as rp  # noqa: E402
import test_genome_seed as gs  # noqa: E402
import test_promote as tp  # noqa: E402
import test_suite_generation as sg  # noqa: E402
import test_suite_runs as base  # noqa: E402

GEN = sg.GEN
SKILL_REL = "assistants/helper/skills/greet"
MAPPED = f"{SKILL_REL}/guide.md"
REPORT = f"evolve-reports/{GEN}.md"
TOP_GUIDE = "A made-up top-level guide that promote must leave alone.\n"
TOP_README = "A made-up repository holding a few assistants.\n"
OTHER = "Made-up notes of another assistant.\n"
SIBLING_REL = "assistants/helper/skills/farewell"
SIBLING_GUIDE = "# Made-up farewell guide\n\nSay goodbye in one short made-up line.\n"
COPY_REL = "assistants/helper/skills/greet-copy"  # byte-identical copies of the skill's files
LINKED_REL = "assistants/helper/skills/greet-linked"  # the skill's files reached through symlinks
SKILL_README = "A frozen made-up readme.\n"
# A second generation started --from rewrite A's child, on a suite whose every case looks for HOLD-WORD: this rewrite
# keeps A's text and adds that word, so it passes every check A fails.
REWRITE_E = sg.REWRITE_A + "Sign off with HOLD-WORD.\n"
SECOND = "suite-gen-from"
USER_NOTES = "The user's own untracked made-up notes.\n"
RED_TEAM = re.compile(r"(?i)red[- ]team")
PAUSE_AT = 9  # the agent run that hits the usage limit: past the six screening runs, before the twelfth held-out one

RECORD_CHECK = r'''#!@PYTHON@
import json, os
def read(rel):
    p = os.path.join(os.getcwd(), *rel.split("/"))
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else None
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"cwd": os.getcwd(), "mapped": read(@MAPPED@), "top_guide": read("guide.md"),
                        "top_readme": read("README.txt")}) + "\n")
print("made-up record check: ok")
'''

# Counts the agent's runs; on the PAUSE_AT-th it sets the stand-in's limit flag, so that run hits the usage limit.
PAUSING_CLAUDE = r'''#!@PYTHON@
import os, sys
CONTROL = @CONTROL@
args = sys.argv[1:]
if @TASK_MODEL@ in args:
    path = os.path.join(CONTROL, "agent-count")
    n = int(open(path).read()) + 1 if os.path.exists(path) else 1
    open(path, "w").write(str(n))
    if n == @PAUSE_AT@:
        open(os.path.join(CONTROL, "limit_once"), "w").write("1")
os.execv(@REAL@, [@REAL@, *args])
'''


def git(cwd, *args):
    return gs.git(cwd, *args)


# SUI-9: a made-up skill in a subfolder of a repository --------------------------------------------------------------

class SubfolderBase(sg.GenerationBase):
    """GenerationBase's stand-in claude, rewrites and suite, over a skill that lives in a subfolder of a git repository
    with a bare origin."""

    def setUp(self):
        self.ev = base.import_scripts("evolve")
        self.gn = base.import_scripts("genome")
        self.tmp = tempfile.mkdtemp(prefix="genome-suite-promote-")
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
        base.write(self.claude, sg.STANDIN.replace("@PYTHON@", sg.PY).replace("@CONTROL@", repr(self.control))
                   .replace("@TASK_MODEL@", repr(sg.TASK_MODEL)).replace("@INSTALL@", repr(sg.INSTALL))
                   .replace("@LIMIT@", repr(sg.LIMIT)), 0o755)
        self.rewrites([sg.REWRITE_A, sg.REWRITE_B])

        # The repository: the skill in a subfolder, a top-level guide.md as a trap, and another assistant's file.
        self.repo = os.path.join(self.tmp, "made-up-repo")
        self.source = os.path.join(self.repo, *SKILL_REL.split("/"))
        base.write(os.path.join(self.repo, "README.txt"), TOP_README)
        base.write(os.path.join(self.repo, "guide.md"), TOP_GUIDE)
        base.write(os.path.join(self.repo, "assistants", "helper", "other.txt"), OTHER)
        base.write(os.path.join(self.repo, *SIBLING_REL.split("/"), "guide.md"), SIBLING_GUIDE)
        base.write(os.path.join(self.source, "guide.md"), sg.SEED_GUIDE)
        base.write(os.path.join(self.source, "README.txt"), SKILL_README)
        for name, text in (("guide.md", sg.SEED_GUIDE), ("README.txt", SKILL_README)):
            base.write(os.path.join(self.repo, *COPY_REL.split("/"), name), text)
            linked = os.path.join(self.repo, *LINKED_REL.split("/"), name)
            os.makedirs(os.path.dirname(linked), exist_ok=True)
            os.symlink(os.path.join("..", "greet", name), linked)
        git(self.tmp, "init", "-q", "-b", "main", self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "made-up repository")
        self.main_head = git(self.repo, "rev-parse", "main")
        self.bare = os.path.join(self.tmp, "stand-in-github.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        git(self.repo, "remote", "add", "origin", self.bare)
        git(self.repo, "push", "-q", "origin", "main")

        # Stand-ins: the profile's check, the pull-request command and a recording git.
        self.stand = os.path.join(self.tmp, "stand-ins")
        self.check_log = os.path.join(self.stand, "checks.jsonl")
        self.pr_log = os.path.join(self.stand, "pr-calls.jsonl")
        self.git_log = os.path.join(self.stand, "git-calls.log")
        self.record_check = self.script("record-check", RECORD_CHECK.replace("@LOG@", repr(self.check_log))
                                        .replace("@MAPPED@", repr(MAPPED)))
        self.pr_command = self.script("pr-standin", tp.PR_STANDIN.replace("@LOG@", repr(self.pr_log))
                                      .replace("@URL@", repr(tp.PR_URL)))
        real_git = shutil.which("git")
        self.assertTrue(real_git, "fixture: git is installed")
        self.git_bin = os.path.join(self.stand, "gitbin")
        base.write(os.path.join(self.git_bin, "git"),
                   tp.GIT_WRAPPER.replace("@LOG@", self.git_log).replace("@GIT@", real_git), 0o755)

        # The profile (outside the repository) and the seed.
        self.profile_path = os.path.join(self.tmp, "profiles", "made-up-greet.json")
        base.write(self.profile_path, json.dumps({**sg.GEN_PROFILE, "checks": [self.record_check]}, indent=2) + "\n")
        profile = self.gn.load_profile(self.profile_path, source=self.source)
        self.gn.seed(self.evo, profile, self.source, "seed")
        self.suite_root = os.path.join(self.tmp, "suites")
        self.suite_path = os.path.join(self.suite_root, "made-up-suite.json")
        base.write(self.suite_path, json.dumps(self.suite_data(), indent=2) + "\n")
        self.configure()

        # The user works on `feature`, with an untracked file of their own.
        git(self.repo, "checkout", "-q", "-b", "feature")
        base.write(os.path.join(self.repo, "my-notes.txt"), USER_NOTES)

    def configure(self, **extra):
        super().configure(pr_command=self.pr_command, **extra)

    def script(self, name, text):
        path = os.path.join(self.stand, "bin", name)
        base.write(path, text.replace("@PYTHON@", sg.PY), 0o755)
        return path

    def evolved(self, gen=GEN, extra=()):
        """Runs the suite generation; returns (state, rewrite A's child, rewrite B's child)."""
        code, out = self.generation(gen=gen, extra=extra)
        self.assertNotIn("Traceback", out)
        state = self.state(gen)
        by_text = {self.guide_of(c): c for c in state.get("children") or []}
        self.assertIn(sg.REWRITE_A, by_text, f"fixture: rewrite A became a child:\n{out}")
        self.assertIn(sg.REWRITE_B, by_text, f"fixture: rewrite B became a child:\n{out}")
        return code, out, state, by_text[sg.REWRITE_A], by_text[sg.REWRITE_B]

    def promote(self, genome, gen=GEN, source=None, repo=None, extra=("--onto", "main")):
        """`evolve.py promote` in-process with the recording git first on PATH; returns (exit code, printed text)."""
        argv = ["promote", genome, "--generation", gen, "--profile", self.profile_path, "--source",
                source or self.source, *(("--repo", repo) if repo else ()), *extra]
        env = {"PATH": self.git_bin + os.pathsep + os.environ.get("PATH", ""), **tp.IDENTITY, **gs.GIT_ENV}
        if os.path.exists(self.git_log):
            os.remove(self.git_log)
        with mock.patch.dict(os.environ, env):
            code, out = self.invoke(argv)
        self.assertNotIn("Traceback", out)
        return code, out

    # Observing ------------------------------------------------------------------------------------------------------

    def remote_refs(self):
        out = git(self.bare, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def local_refs(self):
        out = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
        return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())

    def worktrees(self):
        out = git(self.repo, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def user_state(self):
        return {"branch": git(self.repo, "symbolic-ref", "--short", "HEAD"),
                "head": git(self.repo, "rev-parse", "HEAD"),
                "status": git(self.repo, "status", "--porcelain", "--untracked-files=all"),
                "files": gs.snapshot(self.repo, skip_git=True)}

    def json_lines(self, path):
        return [json.loads(line) for line in tp.read_lines(path)]

    def git_calls(self):
        return [line.split("\x1f")[:-1] for line in tp.read_lines(self.git_log)]

    def before(self):
        return {"user": self.user_state(), "local": self.local_refs(), "remote": self.remote_refs()}

    def assert_promoted(self, code, out, before, winner, guide=sg.REWRITE_B, gen=GEN):
        """The one new branch holds only the mapped guide.md and the report; the check ran at the top of a temporary
        worktree; only that branch was pushed; one pull request against main; main and the user's checkout unchanged.
        """
        self.assertEqual(code, 0, f"promote succeeds:\n{out}")
        new_local = {r: s for r, s in self.local_refs().items() if r not in before["local"]}
        self.assertEqual(len(new_local), 1, f"one new branch in the repository: {self.local_refs()}\n{out}")
        (ref, tip), = new_local.items()
        branch = ref[len("refs/heads/"):]
        self.assertEqual({k: v for k, v in self.local_refs().items() if k != ref}, before["local"],
                         "main and every other local branch are unchanged")
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.main_head, "main is unchanged locally")

        changed = sorted(git(self.repo, "diff", "--name-only", "main", tip).splitlines())
        report = f"evolve-reports/{gen}.md"
        self.assertEqual(changed, sorted([MAPPED, report]),
                         f"the branch holds only the skill's guide.md at its path under the subfolder and the "
                         f"report:\n{out}")
        self.assertEqual(git(self.repo, "show", f"{tip}:{MAPPED}") + "\n", guide, "the winner's guide.md")
        self.assertEqual(git(self.repo, "show", f"{tip}:guide.md") + "\n", TOP_GUIDE,
                         "the repository's top-level guide.md is untouched")
        self.assertEqual(git(self.repo, "rev-parse", f"{tip}^"), self.main_head, "one commit on top of main")
        self.assertRegex(git(self.repo, "show", f"{tip}:{report}"), rp.word(winner), "the report names the winner")

        checks = self.json_lines(self.check_log)
        self.assertTrue(checks, f"the profile's check ran:\n{out}")
        for call in checks:
            self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.repo),
                                "the check runs in a temporary worktree, not in the user's checkout")
            self.assertEqual(call["top_readme"], TOP_README, f"the check runs at the worktree's top: {call}")
            self.assertEqual(call["mapped"], guide, "the worktree holds the winner's guide.md at its path")
            self.assertEqual(call["top_guide"], TOP_GUIDE, "and the top-level guide.md as it was")
        self.assertEqual(len(self.worktrees()), 1, f"no temporary worktree is left: {self.worktrees()}")

        after = self.remote_refs()
        self.assertEqual({k: v for k, v in after.items() if k != ref}, before["remote"],
                         f"main on the stand-in GitHub is unchanged and no other branch is pushed:\n{out}")
        self.assertEqual(after.get(ref), tip, f"the branch is pushed to the stand-in GitHub:\n{out}")
        pushes = [rest for sub, rest in (tp.subcommand(c) for c in self.git_calls()) if sub == "push"]
        self.assertEqual(len(pushes), 1, f"one push: {pushes}")
        for arg in pushes[0]:
            self.assertFalse(arg in ("-f", "--force", "--mirror", "--all", "--tags") or arg.startswith(
                ("--force", "+")), f"never a forced or wide push: {pushes[0]}")
        targets = [a for a in pushes[0] if not a.startswith("-")]
        self.assertEqual(targets[0], "origin", f"pushed to origin: {pushes[0]}")
        self.assertTrue(targets[1:] and all(branch in a for a in targets[1:]), f"only its own branch: {pushes[0]}")

        calls = self.json_lines(self.pr_log)
        self.assertEqual(len(calls), 1, f"one pull request: {calls}\n{out}")
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main", argv)
        self.assertEqual(argv[argv.index("--head") + 1], branch, argv)
        self.assertNotIn("merge", argv, "the pull-request command is never asked to merge")
        self.assertEqual(self.user_state(), before["user"], "the user's checkout is unchanged")

    def assert_refused(self, code, out, before, reason, *names):
        self.assertNotEqual(code, 0, f"promote refuses:\n{out}")
        self.assertNotIn("usage:", out, f"promote accepts these options and refuses on its own terms:\n{out}")
        said = out
        for folder in (self.tmp, os.path.realpath(self.tmp)):
            said = said.replace(folder, "")
        self.assertRegex(said, reason, f"the message says why (outside the folder names):\n{out}")
        for name in names:
            self.assertIn(name, out, f"the message names {name}:\n{out}")
        self.assertLessEqual(len([line for line in out.splitlines() if line.strip()]), 10, f"a short message:\n{out}")
        made = [c for c in self.git_calls() if an.writes(c)]
        self.assertEqual(made, [], f"no worktree, branch, commit or push before refusing:\n{out}")
        self.assertEqual(self.local_refs(), before["local"], f"no branch is made:\n{out}")
        self.assertEqual(self.remote_refs(), before["remote"], f"nothing is pushed:\n{out}")
        self.assertEqual(len(self.worktrees()), 1, f"no worktree is left: {self.worktrees()}")
        self.assertEqual(self.json_lines(self.check_log), [], "no check ran")
        self.assertEqual(self.json_lines(self.pr_log), [], "no pull request is asked for")
        self.assertEqual(self.user_state(), before["user"], "the user's checkout is unchanged")


class PromoteFromASubfolder(SubfolderBase):
    def setUp(self):
        super().setUp()
        code, out, state, self.loser, self.winner = self.evolved()
        self.assertEqual(code, 0, out)
        self.assertEqual(state.get("winner"), self.winner, f"fixture: rewrite B wins: {state}")

    def test_the_winner_lands_under_the_subfolder_with_the_repository_found_from_the_source(self):  # covers: SUI-9
        before = self.before()
        code, out = self.promote(self.winner)
        self.assert_promoted(code, out, before, self.winner)

    def test_an_explicit_repo_that_holds_the_source_works_the_same(self):  # covers: SUI-9
        before = self.before()
        code, out = self.promote(self.winner, repo=self.repo)
        self.assert_promoted(code, out, before, self.winner)

    def test_a_source_outside_the_repo_is_refused_plainly_with_nothing_created(self):  # covers: SUI-9
        outside = os.path.join(self.tmp, "outside-greet")
        shutil.copytree(self.source, outside)
        git(self.tmp, "init", "-q", "-b", "main", outside)
        git(outside, "add", "-A")
        git(outside, "commit", "-q", "-m", "a made-up copy elsewhere")
        before = self.before()
        code, out = self.promote(self.winner, source=outside, repo=self.repo)
        self.assert_refused(code, out, before, re.compile(r"(?i)outside|not (inside|in|within|under|part of)|"
                                                          r"isn't (inside|in|within|under|part of)"),
                            "outside-greet")

    def test_a_genome_that_is_not_the_winner_is_refused(self):  # covers: SUI-9
        before = self.before()
        code, out = self.promote(self.loser)
        self.assert_refused(code, out, before, re.compile(r"(?i)\bwinner\b"), self.loser)


    def test_a_source_other_than_the_seeded_folder_is_refused_and_the_right_one_still_works(self):  # covers: SUI-9
        # The repository's top and the sibling skill both hold a guide.md of their own; the parent folder holds none.
        for rel in ("", "assistants/helper", SIBLING_REL):
            with self.subTest(source=rel or "the repository's top"):
                wrong = os.path.join(self.repo, *rel.split("/")) if rel else self.repo
                before = self.before()
                code, out = self.promote(self.winner, source=wrong)
                self.assert_refused(code, out, before, re.compile(r"(?i)seed|source|base|differ|match|not the folder"))
                self.assertFalse(os.path.exists(os.path.join(self.repo, "assistants", "helper", "guide.md")),
                                 "nothing is written in the parent folder")
        self.assertEqual(git(self.bare, "show", f"main:{SIBLING_REL}/guide.md") + "\n", SIBLING_GUIDE,
                         "the sibling skill's guide.md is untouched on the stand-in GitHub")
        before = self.before()
        code, out = self.promote(self.winner)
        self.assert_promoted(code, out, before, self.winner)


# SUI-19: --source must be the folder the winner was seeded from ------------------------------------------------------

def genome_json(home, genome):
    return sg.read_json(os.path.join(home, "genomes", genome, "genome.json"))


class SeededFromCheck(SubfolderBase):
    def setUp(self):
        super().setUp()
        code, out, state, self.a, self.b = self.evolved()
        self.assertEqual(code, 0, out)
        self.assertEqual(state.get("winner"), self.b, f"fixture: rewrite B wins: {state}")

    def test_seeding_records_the_sources_path_in_the_repository_and_children_inherit_it(self):  # covers: SUI-19
        seed = genome_json(self.evo, "seed")
        self.assertEqual((seed.get("base") or {}).get("path"), SKILL_REL,
                         f"base.path is the source relative to the repository's top: {seed}")
        for child in (self.a, self.b):
            self.assertEqual((genome_json(self.evo, child).get("base") or {}).get("path"), SKILL_REL,
                             f"{child} inherits base.path")

        # The same skill at the top of its own repository, and outside any repository, each in a home of its own.
        at_top = os.path.join(self.tmp, "top-repo")
        shutil.copytree(self.source, at_top)
        git(self.tmp, "init", "-q", "-b", "main", at_top)
        git(at_top, "add", "-A")
        git(at_top, "commit", "-q", "-m", "made-up skill at the top")
        loose = os.path.join(self.tmp, "loose-skill")
        shutil.copytree(self.source, loose)
        homes = {}
        for label, source in (("top", at_top), ("loose", loose)):
            homes[label] = os.path.join(self.tmp, f"home-{label}")
            os.makedirs(homes[label])
            self.gn.seed(homes[label], self.gn.load_profile(self.profile_path, source=source), source, "seed")
        top, outside = genome_json(homes["top"], "seed"), genome_json(homes["loose"], "seed")
        self.assertEqual((top.get("base") or {}).get("path"), ".", f"'.' for the repository's top: {top}")
        self.assertNotIn("path", outside.get("base") or {}, f"no base.path outside a repository: {outside}")
        self.assertEqual(sorted(seed), sorted(outside), "genome.json's top-level keys don't change")
        self.assertEqual(sorted(top), sorted(outside), "genome.json's top-level keys don't change")

    def test_a_sibling_with_identical_files_is_refused_naming_both_paths(self):  # covers: SUI-19
        for rel in (COPY_REL, LINKED_REL):
            with self.subTest(source=rel):
                folder = os.path.join(self.repo, *rel.split("/"))
                with open(os.path.join(folder, "guide.md"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), sg.SEED_GUIDE, "fixture: the sibling's guide.md is the seed's")
                before = self.before()
                code, out = self.promote(self.b, source=folder)
                self.assert_refused(code, out, before, re.compile(r"(?i)seed|source|path|differ|match"),
                                    SKILL_REL, rel)

    def test_a_generation_started_from_an_evolved_genome_promotes_with_the_right_source(self):  # covers: SUI-19
        # A second suite whose every case looks for HOLD-WORD, so A fails its screening and held-out checks alike.
        second_suite = self.suite_data("made-up-suite-two")
        for case in second_suite["cases"]:
            for check in case["checks"]:
                if "contains" in check:
                    check["contains"] = "HOLD-WORD"
        suite_two = os.path.join(self.suite_root, "made-up-suite-two.json")
        base.write(suite_two, json.dumps(second_suite, indent=2) + "\n")
        self.configure(population=1)
        self.rewrites([REWRITE_E])
        code, out = self.generation(gen=SECOND, extra=("--from", self.a), suite=suite_two)
        self.assertEqual(code, 0, out)
        state = self.state(SECOND)
        winner = state.get("winner")
        self.assertTrue(winner, f"fixture: the child of A that adds the held-out word wins: {state}\n{out}")
        self.assertEqual(self.guide_of(winner), REWRITE_E, "fixture: the winner holds rewrite E")
        before = self.before()
        code, out = self.promote(winner, gen=SECOND)
        self.assert_promoted(code, out, before, winner, guide=REWRITE_E, gen=SECOND)


class RegistaFromEvolved(tp.PromoteBase):
    """Regista's own promote (no --source) of a generation started --from the first generation's winner."""

    def test_regista_promotes_a_generation_started_from_an_evolved_genome(self):  # covers: SUI-19
        code, out = self.run_generation(gen="first-gen")
        self.assertEqual(code, 0, out)
        first = self.state("first-gen").get("winner")
        self.assertEqual(first, self.made[1], f"fixture: child 2 wins the first generation:\n{out}")
        # As tests/test_red_team.py's descendant: first's children are roles 3 and 4, and 4 wins.
        self.plan.update({(2, "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 13000},
                          (3, "screen"): {"outcome": "approved", "used": 20000},
                          (4, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 3000},
                          (3, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
                          (4, "holdout"): {"outcome": "approved", "used": 12000}})
        code, out = self.run_generation(extra=("--from", first))
        self.assertEqual(code, 0, out)
        self.winner = self.state().get("winner")
        self.assertEqual(self.winner, self.made[3], f"fixture: the first winner's child 2 wins: {self.state()}")
        self.add_remote()
        before_remote = self.remote_refs()
        code, out = self.promote(extra=["--onto", "main"])
        self.assertEqual(code, 0, f"Regista's promote of a --from generation works as before:\n{out}")
        branch = f"genome/{tp.GEN}-{self.winner}"
        after = self.remote_refs()
        self.assertIn("refs/heads/" + branch, after, f"the branch is pushed:\n{out}")
        self.assertEqual({k: v for k, v in after.items() if k != "refs/heads/" + branch}, before_remote,
                         "nothing else is pushed")
        tip = after["refs/heads/" + branch]
        self.assertEqual(sorted(git(self.bare, "diff", "--name-only", "main", tip).splitlines()),
                         sorted([tp.TARGET, f"docs/generations/{tp.GEN}.md"]))
        calls = self.pr_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--base") + 1], "main")


class SuiteRefusals(SubfolderBase):
    def test_a_red_team_suite_generation_is_refused(self):  # covers: SUI-9
        # `generation --suite --red-team` is refused, so the finished generation is marked red-team by hand, as a
        # red-team generation leaves its generation.json and its children's mutation.json.
        code, out, state, _a, winner = self.evolved(gen="rt-suite")
        self.assertEqual(code, 0, out)
        self.assertEqual(state.get("winner"), winner, f"fixture: rewrite B wins: {state}")
        for path in (os.path.join(self.evo, "runs", "rt-suite", "generation.json"),
                     *(os.path.join(self.evo, "genomes", child, "mutation.json") for child in state["children"])):
            data = sg.read_json(path)
            data["red_team"] = True
            base.write(path, json.dumps(data, indent=2) + "\n")
        before = self.before()
        code, out = self.promote(winner, gen="rt-suite")
        self.assert_refused(code, out, before, RED_TEAM)

    def test_an_unfinished_suite_generation_is_refused(self):  # covers: SUI-9
        pausing = os.path.join(self.tmp, "bin", "pausing-claude")
        base.write(pausing, PAUSING_CLAUDE.replace("@PYTHON@", sg.PY).replace("@CONTROL@", repr(self.control))
                   .replace("@TASK_MODEL@", repr(sg.TASK_MODEL)).replace("@PAUSE_AT@", str(PAUSE_AT))
                   .replace("@REAL@", repr(self.claude)), 0o755)
        self.configure(claude_command=[pausing])
        code, out, state, _a, b = self.evolved(gen="paused-suite")
        self.assertEqual(code, self.ev.PAUSED_EXIT, f"fixture: paused at the usage limit:\n{out}")
        self.assertFalse(state.get("winner"), f"fixture: no winner yet: {state}")
        before = self.before()
        code, out = self.promote(b, gen="paused-suite")
        self.assert_refused(code, out, before, tp.UNFINISHED)


# SUI-18: the report path's hardening in evolve_core.promote ----------------------------------------------------------

class ReportPathRefusals(rp.ToyRepoBase):
    """The toy repository's winner changes prompt.md at the repository's top; each bad report path is refused before
    any worktree, branch, commit, check, push or pull request."""

    refused = an.PromoteRefusesUnsafePaths.refused
    assert_refused_before_anything = an.PromoteRefusesUnsafePaths.assert_refused_before_anything
    git_calls = an.PromoteRefusesUnsafePaths.git_calls

    def setUp(self):
        super().setUp()
        self.finished = self.finished_generation()
        self.outside = os.path.join(self.tmp, "outside.md")
        real_git = shutil.which("git")
        self.assertTrue(real_git, "fixture: git is installed")
        self.git_log = os.path.join(self.stand, "git-calls.log")
        self.git_bin = os.path.join(self.stand, "gitbin")
        base.write(os.path.join(self.git_bin, "git"),
                   tp.GIT_WRAPPER.replace("@LOG@", self.git_log).replace("@GIT@", real_git), 0o755)

    def assert_each_refused(self, paths):
        for bad in paths:
            with self.subTest(report_path=bad):
                adapter = self.toy_adapter()
                adapter.report_path = lambda gen_id, bad=bad: bad
                self.assert_refused_before_anything(dict(self.finished), adapter, f"report path {bad!r}")

    def test_a_report_path_of_dot_is_refused(self):  # covers: SUI-18
        self.assert_each_refused([".", "./"])

    def test_a_report_path_naming_a_folder_is_refused(self):  # covers: SUI-18
        self.assert_each_refused(["reports/", "evolve-reports/", "made-up/reports/"])

    def test_a_report_path_with_a_git_part_is_refused(self):  # covers: SUI-18
        self.assert_each_refused([".git", ".git/report.md", ".git/hooks/post-commit", "sub/.git/report.md"])

    def test_a_report_path_equal_to_a_promoted_file_is_refused(self):  # covers: SUI-18
        self.assertEqual(rp.TARGET, "prompt.md", "fixture: the winner's promoted file is prompt.md at the top")
        self.assert_each_refused([rp.TARGET])


# SUI-10: suite.py and runners.py name no target or host --------------------------------------------------------------

HOST_WORDS = tuple(w for w in an.TARGET_WORDS if w != "claude")  # the targets, SKILL.md and Anthropic
TARGET_ONLY = tuple(w for w in HOST_WORDS if w != "anthropic")  # runners.py may name its host, Claude Code by Anthropic
RUNNER_LITERAL = re.compile(r"""(['"])claude(-json)?\1""")
RULES = {"suite.py": {"words": HOST_WORDS, "claude_only_on_runner_lines": True},
         "runners.py": {"words": TARGET_ONLY, "claude_only_on_runner_lines": False}}


def suite_neutrality_problems(name, text):
    """One sentence per problem, naming the file, the line number and the word. Neither file may name a target or
    SKILL.md (as a word or part of one, any case). suite.py may not name Anthropic either, and may name claude only on
    a line holding the literal runner value 'claude' or 'claude-json'; runners.py may name its host (Claude Code,
    Anthropic)."""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        lower = line.lower()
        words = [w for w in RULES[name]["words"] if w in lower]
        if RULES[name]["claude_only_on_runner_lines"] and "claude" in lower and not RUNNER_LITERAL.search(line):
            words.append("claude")
        problems += [f"scripts/{name}, line {number}: the word '{w}' names a target or a host: {line.strip()}"
                     for w in words]
    return problems


class SuiteNeutrality(unittest.TestCase):
    def test_suite_and_runners_name_no_target_or_host(self):  # covers: SUI-10
        for name in RULES:
            with self.subTest(module=name):
                path = os.path.join(SCRIPTS, name)
                self.assertTrue(os.path.isfile(path), f"scripts/{name} exists")
                with open(path, encoding="utf-8") as f:
                    problems = suite_neutrality_problems(name, f.read())
                self.assertEqual(problems, [], "\n".join(problems))

    def test_the_check_names_the_file_line_and_word_of_each_planted_word(self):  # covers: SUI-10
        planted = ["x = 1", "# Runs a Regista step.", "PATH = '~/personal-assistants/agents'", "doc = 'see SKILL.md'",
                   "who = 'Anthropic'", "y = 'fpl-tool'", "z = 'klotho_app'", "a = 'daedalus'", "b = 'Artemis'",
                   "c = 'personal_assistants'", "RUNNERS = ('claude', \"claude-json\")", "# Calls Claude Code.",
                   "KEY = 'ANTHROPIC_API_KEY'  # 'claude'"]
        text = "\n".join(planted) + "\n"
        expected = ((2, "regista"), (3, "personal-assistants"), (4, "skill.md"), (6, "fpl"), (7, "klotho"),
                    (8, "daedalus"), (9, "artemis"), (10, "personal_assistants"))
        for name in RULES:
            problems = suite_neutrality_problems(name, text)
            for number, word in expected:
                with self.subTest(module=name, word=word, line=number):
                    self.assertTrue(any(f"scripts/{name}" in p and f"line {number}:" in p and f"'{word}'" in p
                                        for p in problems), problems)
            self.assertFalse(any(re.search(r"line (1|11):", p) for p in problems),
                             f"a clean line and the runner values' line aren't reported: {problems}")
        suite = suite_neutrality_problems("suite.py", text)
        for number in (5, 13):
            self.assertTrue(any(f"line {number}:" in p and "the word 'anthropic'" in p for p in suite),
                            f"suite.py may not name Anthropic: {suite}")
        self.assertTrue(any("line 12:" in p and "the word 'claude'" in p for p in suite),
                        f"suite.py may name claude only in its runner values: {suite}")
        runners = suite_neutrality_problems("runners.py", text)
        self.assertFalse(any("the word 'claude'" in p or "the word 'anthropic'" in p for p in runners),
                         f"runners.py may name its host, Claude Code by Anthropic: {runners}")


if __name__ == "__main__":
    unittest.main()
