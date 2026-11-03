"""Replay workspace: `evolve.py replay <case> --genome current --prepare-only` prepares a throwaway workspace for one
bench case. The workspace is a fresh local clone of the project at the case's parent commit, with no remote, the
case's frozen tests copied in read-only, and the project's setup run inside it with the per-replay environment.
The live repository is never written. The clone is deleted at the end unless --keep is given.

The fixture is a small Regista project made with plain git in a temporary folder; its one case comes from
`evolve.py bench build`, as in tests/test_bench_cases.py. evolve.py runs as a subprocess with HOME and
REGISTA_EVOLVE_HOME in temporary folders.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_replay_workspace -v"""
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
from regista_location import requires_regista  # noqa: E402  the Regista checkout (or None)
EVOLVE = os.path.join(ROOT, "scripts", "evolve.py")

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}

CONFIG = {
    "project": "sample",
    "plan": ".regista/plan.md",
    "board_file": ".regista/TODO.md",
    "gates": [
        {"name": "tests", "run": "python3 -m unittest discover -s tests/unit", "tests": ["tests/unit/**"],
         "when": ["commit"], "timeout": 600},
    ],
    "roles": {"source": ["src/**"], "tests": ["tests/**"]},
    "requirements": {"enforce_coverage": True},
}

GITIGNORE = ".regista/hooks.log\n.regista/gates/\n.regista/compactions.jsonl\n.regista/state/\n__pycache__/\n"

FROZEN_NEW = "tests/unit/test_feature.py"   # added by the finishing commit: absent at the parent
FROZEN_CHANGED = "tests/unit/test_app.py"   # changed by the finishing commit: an older version exists at the parent


def at(hour):
    return f"2026-10-20T{8 + hour:02d}:00:00"


def spec(step, milestone, title, plan_step, interface, reqs):
    return json.dumps({"step": step, "milestone": milestone, "title": title, "plan_step": plan_step,
                       "state": "active", "interface": interface,
                       "requirements": [{"id": i, "text": t, "verify": v} for i, t, v in reqs]}, indent=1) + "\n"


def test_file(name, covers, lines=1):
    body = [f'"""Tests for {name}."""', "import unittest", "", "", f"class {name.title().replace('_', '')}(unittest.TestCase):"]
    for n in range(lines):
        if n == 0 and covers:
            body.append(f"    def test_{n}(self):  # covers: {covers}")
        else:
            body.append(f"    def test_{n}(self):")
        body.append(f"        self.assertEqual({n} + 1, {n + 1})")
    return "\n".join(body) + "\n"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class Repo:
    """A git repository in a temporary folder, with commits at chosen times."""

    def __init__(self, parent, name="sample"):
        self.root = os.path.join(parent, name)
        os.makedirs(self.root)
        self.git("init", "-q", "-b", "main")

    def git(self, *args, date=None, cwd=None, check=True):
        env = dict(os.environ, **GIT_ENV)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date + "+0000"
        r = subprocess.run(["git", "-c", "user.name=Sample", "-c", "user.email=sample@example.com",
                            "-c", "commit.gpgsign=false", *args], cwd=cwd or self.root, capture_output=True,
                           text=True, env=env)
        if check and r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
        return r if not check else r.stdout.strip()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, hour, message, files=None):
        for rel, text in (files or {}).items():
            self.write(rel, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=at(hour))
        return self.git("rev-parse", "HEAD")


def build_sample(parent):
    """One step (m1-feature, FEAT-1) with a parent and a finishing commit, then a later commit at HEAD.
    Returns (repo, commits)."""
    r = Repo(parent)
    c = {}
    plan = "# Sample: plan\n\n## M1 — Feature\n- [ ] Feature (FEAT-1)\n"
    c["root"] = r.commit(0, "setup", {
        ".gitignore": GITIGNORE, ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n",
        ".regista/plan.md": plan, "src/app.py": "VALUE = 1\n",
        FROZEN_CHANGED: test_file("app", None),
        ".regista/specs/m1-feature.json": spec("m1-feature", "M1", "Feature", "Feature (FEAT-1)",
                                               "feature() in src/feature.py.",
                                               [("FEAT-1", "feature() SHALL return 2", "test")])})
    c["parent"] = r.commit(1, "feature: source", {"src/feature.py": "def feature():\n    return 2\n"})
    c["finish"] = r.commit(2, "feature: tests", {
        FROZEN_NEW: test_file("feature", "FEAT-1", lines=2),
        FROZEN_CHANGED: test_file("app", None, lines=3),
        "src/feature.py": "def feature():\n    return 2\n\n\nDONE = True\n"})
    c["head"] = r.commit(3, "later work", {"src/later.py": "LATER = 3\n"})
    return r, c


def make_live_state(repo):
    """Give the live repository the state a replay must not touch: a linked worktree, an uncommitted change, a staged
    file, and Regista's gate records in .git."""
    repo.git("worktree", "add", "-q", "-b", "side", os.path.join(os.path.dirname(repo.root), "sample-wt"))
    repo.write("src/app.py", "VALUE = 1  # edited, not committed\n")
    repo.write("src/staged.py", "STAGED = 1\n")
    repo.git("add", "src/staged.py")
    git_dir = os.path.join(repo.root, ".git")
    with open(os.path.join(git_dir, "regista-passed.json"), "w", encoding="utf-8") as f:
        json.dump({"passes": [{"gate": "tests", "fingerprint": "abc123", "at": "2026-10-20T12:00:00"}]}, f)
    with open(os.path.join(git_dir, "regista-gates.lock"), "w", encoding="utf-8") as f:
        f.write("1234 2026-10-20T12:00:00\n")


def snapshot(repo):
    """Everything about the live repository a replay must leave alone, read without writing anything."""
    git_dir = os.path.join(repo.root, ".git")
    files = {}
    for dirpath, _, names in os.walk(git_dir):
        for n in names:
            p = os.path.join(dirpath, n)
            files[os.path.relpath(p, git_dir)] = sha256(read_bytes(p))
    regista = {}
    for n in ("regista-passed.json", "regista-gates.lock"):
        p = os.path.join(git_dir, n)
        regista[n] = (read_bytes(p), os.stat(p).st_mtime_ns)
    return {
        "HEAD file": read_bytes(os.path.join(git_dir, "HEAD")),
        "HEAD": repo.git("rev-parse", "HEAD"),
        "refs": repo.git("for-each-ref", "--format=%(refname) %(objectname)"),
        "all commits": repo.git("rev-list", "--all"),
        "index": read_bytes(os.path.join(git_dir, "index")),
        "worktrees": repo.git("worktree", "list", "--porcelain"),
        "status": repo.git("status", "--porcelain"),
        "remotes": repo.git("remote", "-v"),
        "config": read_bytes(os.path.join(git_dir, "config")),
        "regista": regista,
        "git dir": files,
    }


def evolve_env(home, evolve_home):
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_"))}
    env.update({"HOME": home, "REGISTA_EVOLVE_HOME": evolve_home, **GIT_ENV})
    return env


def run_evolve(args, home, evolve_home, cwd):
    return subprocess.run([sys.executable, EVOLVE, *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", env=evolve_env(home, evolve_home), timeout=300)


def replay(case, home, evolve_home, cwd, keep=False):
    args = ["replay", case, "--genome", "current", "--prepare-only"] + (["--keep"] if keep else [])
    return run_evolve(args, home, evolve_home, cwd)


def clones_under(evolve_home):
    """Every git clone (a folder holding a .git folder) under the working folder's runs/."""
    found = []
    for dirpath, dirnames, _ in os.walk(os.path.join(evolve_home, "runs")):
        if ".git" in dirnames:
            found.append(dirpath)
            dirnames[:] = []
    return found


def make_writable(folder):
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                try:
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
                except OSError:
                    pass


def write_evolve_json(evolve_home, setup, marks):
    """evolve.json for the sample project: setup, replay_env with {slot}, and replay_before / replay_after commands
    that append a marker line to `marks`."""
    m = shlex.quote(marks)
    os.makedirs(evolve_home, exist_ok=True)
    with open(os.path.join(evolve_home, "evolve.json"), "w", encoding="utf-8") as f:
        json.dump({
            "setup": {"sample": setup},
            "replay_env": {"sample": {"TEST_DATABASE_URL": "postgresql:///sample_test_{slot}",
                                      "SAMPLE_PLAIN": "plain"}},
            "replay_before": {"sample": f"echo before {{slot}} >> {m}"},
            "replay_after": {"sample": f"echo after {{slot}} >> {m}"},
        }, f, indent=1)


def setup_marker(marks):
    """A setup command that records its folder and the replay environment it sees."""
    return f'echo "setup $(pwd -P) $TEST_DATABASE_URL $SAMPLE_PLAIN" >> {shlex.quote(marks)}'


def read_marks(marks):
    if not os.path.exists(marks):
        return []
    with open(marks, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@requires_regista
class ReplayFixture(unittest.TestCase):
    """Shared setup: the sample project, its live state, and its one case built by `bench build`."""

    @classmethod
    def build_fixture(cls, prefix):
        cls.tmp = tempfile.mkdtemp(prefix=prefix)
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.repo, cls.c = build_sample(os.path.join(cls.tmp, "projects"))
        make_live_state(cls.repo)
        cls.bench_home = os.path.join(cls.tmp, "bench-home")
        cls.build = run_evolve(["bench", "build", cls.repo.root], cls.home, cls.bench_home, cls.tmp)
        cls.case_name, cls.case = None, None
        folder = os.path.join(cls.bench_home, "bench", "sample")
        if cls.build.returncode == 0 and os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        case = json.load(f)
                    if case.get("step") == "m1-feature":
                        cls.case_name, cls.case = name[:-5], case

    @classmethod
    def evolve_home_with(cls, name, setup_for_marks):
        """A working folder with a copy of the built bench, and an evolve.json whose markers go to its own file."""
        evo = os.path.join(cls.tmp, name)
        if os.path.isdir(os.path.join(cls.bench_home, "bench")):
            shutil.copytree(os.path.join(cls.bench_home, "bench"), os.path.join(evo, "bench"))
        marks = os.path.join(cls.tmp, f"{name}-marks.txt")
        write_evolve_json(evo, setup_for_marks(marks), marks)
        return evo, marks

    @classmethod
    def tearDownClass(cls):
        make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check_fixture(self):
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{output(self.build)}")
        self.assertIsNotNone(self.case, "bench build made no case for m1-feature")
        self.assertTrue(self.case["replayable"], self.case.get("reasons"))
        self.assertEqual(self.case["parent"], self.c["parent"])
        self.assertEqual(sorted(t["path"] for t in self.case["tests"]), sorted([FROZEN_CHANGED, FROZEN_NEW]))


class ReplayPrepare(ReplayFixture):
    """Two prepared replays of the same case: the first without --keep, the second with --keep."""

    @classmethod
    def setUpClass(cls):
        cls.build_fixture("regista-replay-")
        cls.evo, cls.marks = cls.evolve_home_with("evolve", setup_marker)
        cls.before = snapshot(cls.repo)
        # First replay, without --keep: the clone must be gone when it ends.
        cls.dropped = replay(cls.case_name or "m1-feature", cls.home, cls.evo, cls.tmp)
        cls.after_dropped = snapshot(cls.repo)
        cls.clones_after_dropped = clones_under(cls.evo)
        cls.marks_dropped = read_marks(cls.marks)
        # Second replay, with --keep: the clone stays for inspection.
        cls.kept = replay(cls.case_name or "m1-feature", cls.home, cls.evo, cls.tmp, keep=True)
        cls.after_kept = snapshot(cls.repo)
        cls.marks_all = read_marks(cls.marks)
        cls.marks_kept = cls.marks_all[len(cls.marks_dropped):]
        clones = clones_under(cls.evo)
        cls.clone = clones[0] if len(clones) == 1 else None
        cls.clones_after_kept = clones
        cls.clone_facts = None
        if cls.clone:
            def cg(*args):
                return cls.repo.git(*args, cwd=cls.clone, check=False)
            cls.clone_facts = {
                "HEAD": cg("rev-parse", "HEAD").stdout.strip(),
                "symbolic": cg("symbolic-ref", "-q", "HEAD").returncode,
                "remotes": cg("remote").stdout.strip(),
                "status": cg("status", "--porcelain", "--untracked-files=no").stdout.strip(),
                # BENCH-12: read before any test commits in the clone.
                "all commits": cg("log", "--all", "--format=%H").stdout.split(),
                "finish exists": cg("cat-file", "-e", cls.c["finish"]).returncode,
                "refs": cg("for-each-ref", "--format=%(refname) %(objectname)").stdout.strip().splitlines(),
                "later blob exists": cg("cat-file", "-e",
                                        cls.repo.git("rev-parse", f"{cls.c['finish']}:src/feature.py")).returncode,
            }

    def setUp(self):
        self.check_fixture()
        self.assertEqual(self.dropped.returncode, 0, f"replay without --keep failed:\n{output(self.dropped)}")
        self.assertEqual(self.kept.returncode, 0, f"replay with --keep failed:\n{output(self.kept)}")

    def kept_clone(self):
        self.assertIsNotNone(self.clone, f"expected one kept clone under runs/, found {self.clones_after_kept}")
        return self.clone

    # BENCH-2: a fresh clone at the parent commit, with no remote.

    def test_prepare_only_prints_the_workspace_under_runs(self):  # covers: BENCH-2
        clone = self.kept_clone()
        runs = os.path.realpath(os.path.join(self.evo, "runs"))
        self.assertTrue(os.path.realpath(clone).startswith(runs + os.sep), clone)
        printed = self.kept.stdout
        self.assertTrue(clone in printed or os.path.realpath(clone) in printed,
                        f"--prepare-only must print the workspace path {clone}:\n{printed}")
        self.assertIn("current", os.path.relpath(clone, self.evo).split(os.sep), "the genome id is part of the path")

    def test_clone_is_detached_at_the_parent_commit_with_no_remote(self):  # covers: BENCH-2
        clone = self.kept_clone()
        f = self.clone_facts
        self.assertEqual(f["HEAD"], self.c["parent"], "the clone's HEAD is the case's parent commit")
        self.assertNotEqual(f["symbolic"], 0, "HEAD is detached, not on a branch")
        self.assertEqual(f["remotes"], "", "the clone keeps no remote, so nothing can be pushed back")
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")),
                        "a clone with its own .git folder, not a worktree of the live repository")
        self.assertFalse(os.path.exists(os.path.join(clone, ".git", "objects", "info", "alternates")),
                         "the clone must not borrow the live repository's objects")
        with open(os.path.join(clone, "src", "feature.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "def feature():\n    return 2\n", "source files as at the parent commit")
        self.assertFalse(os.path.exists(os.path.join(clone, "src", "later.py")), "nothing from after the parent")
        self.assertFalse(os.path.exists(os.path.join(clone, "src", "staged.py")), "nothing from the live index")
        with open(os.path.join(clone, "src", "app.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n", "nothing from the live working tree")

    def test_clone_holds_no_commit_or_object_after_the_parent(self):  # covers: BENCH-12
        self.kept_clone()
        f = self.clone_facts
        self.assertEqual(f["all commits"], [self.c["parent"], self.c["root"]],
                         "git log --all in the clone lists only the parent and its ancestors")
        self.assertNotEqual(f["finish exists"], 0, "the finishing commit must not exist in the clone")
        refs = [line.split() for line in f["refs"]]
        self.assertTrue(len(refs) == 1 and refs[0][0].startswith("refs/heads/") and refs[0][1] == self.c["parent"],
                        f"only one local branch, at the parent, may remain: {f['refs']}")
        self.assertNotEqual(f["later blob exists"], 0,
                            "a file version that only exists in the finishing commit must be pruned from the clone")

    def test_clone_shares_no_object_files_with_the_live_repository(self):  # covers: BENCH-2
        clone = self.kept_clone()

        def inodes(git_dir):
            out = set()
            for dirpath, _, names in os.walk(os.path.join(git_dir, "objects")):
                for n in names:
                    st = os.stat(os.path.join(dirpath, n))
                    out.add((st.st_dev, st.st_ino))
            return out
        shared = inodes(os.path.join(self.repo.root, ".git")) & inodes(os.path.join(clone, ".git"))
        self.assertEqual(shared, set(), "the clone is made with --no-hardlinks")

    def test_live_repository_unchanged_by_prepared_replays(self):  # covers: BENCH-2
        for label, after in (("without --keep", self.after_dropped), ("with --keep", self.after_kept)):
            for key in ("HEAD file", "HEAD", "refs", "all commits", "index", "worktrees", "status", "remotes",
                        "config"):
                self.assertEqual(self.before[key], after[key], f"live repository's {key} changed by a replay {label}")
            for name, (data, mtime) in self.before["regista"].items():
                self.assertEqual(after["regista"][name][0], data, f".git/{name} contents changed ({label})")
                self.assertEqual(after["regista"][name][1], mtime, f".git/{name} was rewritten ({label})")
            changed = sorted(k for k in set(self.before["git dir"]) | set(after["git dir"])
                             if self.before["git dir"].get(k) != after["git dir"].get(k))
            self.assertEqual(changed, [], f"files in the live .git written by a replay {label}")

    def test_commit_in_the_clone_never_reaches_the_live_repository(self):  # covers: BENCH-2
        clone = self.kept_clone()
        with open(os.path.join(clone, "src", "replayed.py"), "w", encoding="utf-8") as fh:
            fh.write("BUILT = True\n")
        self.repo.git("add", "src/replayed.py", cwd=clone)
        self.repo.git("commit", "-q", "-m", "built in the replay", cwd=clone)
        made = self.repo.git("rev-parse", "HEAD", cwd=clone)
        pushed = self.repo.git("push", cwd=clone, check=False)
        self.assertNotEqual(pushed.returncode, 0, "a plain push from the clone must have nowhere to go")
        pushed_origin = self.repo.git("push", "origin", "HEAD:refs/heads/main", cwd=clone, check=False)
        self.assertNotEqual(pushed_origin.returncode, 0, "the clone has no origin remote")
        has = self.repo.git("cat-file", "-e", made, check=False)
        self.assertNotEqual(has.returncode, 0, "the clone's commit must not exist in the live repository")
        now = snapshot(self.repo)
        for key in ("HEAD", "refs", "all commits", "index", "worktrees"):
            self.assertEqual(now[key], self.before[key], f"live repository's {key} changed by a commit in the clone")

    # BENCH-3: frozen tests copied in read-only, byte-identical to the case's copies.

    def test_frozen_tests_are_read_only_copies_of_the_case_tests(self):  # covers: BENCH-3
        clone = self.kept_clone()
        recorded = {t["path"]: t["sha256"] for t in self.case["tests"]}
        for path, digest in recorded.items():
            full = os.path.join(clone, path)
            self.assertTrue(os.path.isfile(full), f"frozen test {path} isn't in the workspace")
            self.assertEqual(sha256(read_bytes(full)), digest,
                             f"{path} must be the case's copy (the finishing commit's version), not the parent's")
            mode = stat.S_IMODE(os.stat(full).st_mode)
            self.assertEqual(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), 0,
                             f"{path} must be read-only (chmod a-w), mode is {oct(mode)}")

    # REP-2: replay_before, setup and replay_after run in order with the slot filled; replay_env reaches setup.

    def test_before_setup_after_run_in_order_with_the_slot_filled(self):  # covers: REP-2
        clone = self.kept_clone()
        lines = self.marks_kept
        self.assertEqual(len(lines), 3, f"one line each from replay_before, setup and replay_after: {lines}")
        before, setup, after = lines
        self.assertTrue(before.startswith("before "), f"replay_before runs first: {lines}")
        self.assertTrue(setup.startswith("setup "), f"setup runs second: {lines}")
        self.assertTrue(after.startswith("after "), f"replay_after runs last: {lines}")
        slot = before.split(" ", 1)[1]
        self.assertRegex(slot, r"^[A-Za-z0-9_-]{1,32}$", "a short id usable in a database name")
        self.assertEqual(after, f"after {slot}", "the same slot for replay_before and replay_after")
        words = setup.split(" ")
        self.assertEqual(len(words), 4, setup)
        self.assertEqual(os.path.realpath(words[1]), os.path.realpath(clone), "setup runs inside the clone")
        self.assertEqual(words[2], f"postgresql:///sample_test_{slot}", "replay_env reaches setup with {slot} filled")
        self.assertEqual(words[3], "plain", "every replay_env variable reaches setup")
        self.assertNotIn("{slot}", "\n".join(self.marks_all))

    def test_each_replay_gets_its_own_slot(self):  # covers: REP-2
        self.assertEqual(len(self.marks_dropped), 3, self.marks_dropped)
        self.assertEqual(len(self.marks_kept), 3, self.marks_kept)
        first = self.marks_dropped[0].split(" ", 1)[1]
        second = self.marks_kept[0].split(" ", 1)[1]
        self.assertNotEqual(first, second, "two replays must not share a slot (they would share a database)")

    def test_clone_deleted_without_keep(self):  # covers: BENCH-2
        self.assertEqual(self.clones_after_dropped, [], "without --keep the clone is deleted when the replay ends")
        self.assertEqual(len(self.clones_after_kept), 1, "with --keep the clone stays")


class ReplayFailures(ReplayFixture):
    """Replays that fail: setup exits non-zero, and setup changes a frozen test."""

    @classmethod
    def setUpClass(cls):
        cls.build_fixture("regista-replay-fail-")
        cls.before = snapshot(cls.repo)
        cls.evo_fail, cls.marks_fail = cls.evolve_home_with(
            "evolve-fail", lambda marks: f"echo setup >> {shlex.quote(marks)}; exit 7")
        cls.failed = replay(cls.case_name or "m1-feature", cls.home, cls.evo_fail, cls.tmp)
        cls.failed_clones = clones_under(cls.evo_fail)
        q = shlex.quote(FROZEN_NEW)
        cls.evo_edit, cls.marks_edit = cls.evolve_home_with(
            "evolve-edit", lambda marks: f"echo setup >> {shlex.quote(marks)}; chmod u+w {q}; "
                                         f"printf '# changed\\n' >> {q}")
        cls.edited = replay(cls.case_name or "m1-feature", cls.home, cls.evo_edit, cls.tmp)
        cls.after = snapshot(cls.repo)

    def setUp(self):
        self.check_fixture()

    def test_failing_setup_fails_the_replay_and_replay_after_still_runs(self):  # covers: REP-2
        self.assertNotEqual(self.failed.returncode, 0, f"a failing setup fails the replay:\n{output(self.failed)}")
        self.assertNotIn("Traceback", self.failed.stderr, output(self.failed))
        lines = read_marks(self.marks_fail)
        self.assertEqual([x.split(" ", 1)[0] for x in lines], ["before", "setup", "after"],
                         f"replay_after runs even when setup fails: {lines}")
        self.assertEqual(lines[0].split(" ", 1)[1], lines[2].split(" ", 1)[1], "same slot before and after")
        self.assertEqual(self.failed_clones, [], "the clone is deleted after a failure too (no --keep)")

    def test_changed_frozen_test_is_a_hard_failure_naming_the_file(self):  # covers: BENCH-3
        text = self.edited.stdout + self.edited.stderr
        self.assertNotEqual(self.edited.returncode, 0, f"a changed frozen test fails the replay:\n{output(self.edited)}")
        self.assertIn(FROZEN_NEW, text, f"the failure names the changed frozen test:\n{output(self.edited)}")
        self.assertNotIn("Traceback", self.edited.stderr, output(self.edited))
        lines = read_marks(self.marks_edit)
        self.assertEqual([x.split(" ", 1)[0] for x in lines], ["before", "setup", "after"],
                         f"replay_after runs after a hard failure too: {lines}")

    def test_failed_replays_leave_the_live_repository_alone(self):  # covers: BENCH-2
        for key in ("HEAD", "refs", "all commits", "index", "worktrees", "status", "config", "regista", "git dir"):
            self.assertEqual(self.before[key], self.after[key], f"live repository's {key} changed by a failed replay")


def process_gone(pid):
    """True when the process no longer runs (gone, or a zombie waiting to be reaped)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return fields[0] == "Z"


def wait_for(check, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return check()


def plain_failure(test, result, what):
    """A refused or interrupted replay ends with a short plain message: non-zero exit, no traceback."""
    text = result.stdout + result.stderr
    test.assertNotEqual(result.returncode, 0, f"{what} must fail the replay:\n{output(result)}")
    test.assertNotIn("Traceback", text, f"{what}: a plain message, not a traceback:\n{output(result)}")
    test.assertNotIn("KeyboardInterrupt", text, output(result))
    lines = [x for x in result.stderr.splitlines() if x.strip()]
    test.assertTrue(lines, f"{what}: the replay must say what happened on stderr:\n{output(result)}")
    test.assertLessEqual(len(lines), 5, f"{what}: one plain message, not a dump:\n{output(result)}")
    return text


class ReplayInterruptAndChecks(ReplayFixture):
    """REP-3: Ctrl-C during setup stops the setup's whole process group and still runs replay_after; a case whose
    parent isn't a full commit hash, or whose frozen test path leaves the clone, is refused."""

    @classmethod
    def setUpClass(cls):
        cls.build_fixture("regista-replay-rep3-")

    def setUp(self):
        self.check_fixture()
        self.child = None

    def tearDown(self):
        if self.child and not process_gone(self.child):
            try:
                os.kill(self.child, signal.SIGKILL)
            except OSError:
                pass

    def case_home(self, name, edit):
        """A working folder with a copy of the built bench whose case file is changed by `edit(case, case_folder)`,
        and plain replay commands."""
        evo, marks = self.evolve_home_with(name, setup_marker)
        case_json = os.path.join(evo, "bench", "sample", self.case_name + ".json")
        with open(case_json, encoding="utf-8") as f:
            case = json.load(f)
        edit(case, os.path.join(evo, "bench", "sample", self.case_name))
        make_writable(os.path.join(evo, "bench"))
        with open(case_json, "w", encoding="utf-8") as f:
            json.dump(case, f, indent=1)
        return evo, marks

    def test_ctrl_c_during_setup_stops_its_process_group_then_runs_replay_after(self):  # covers: REP-3
        evo = os.path.join(self.tmp, "evolve-int")
        shutil.copytree(os.path.join(self.bench_home, "bench"), os.path.join(evo, "bench"))
        marker = os.path.join(self.tmp, "int-marker")
        os.makedirs(marker)
        pidfile = os.path.join(marker, "child.pid")
        after = os.path.join(marker, "after.txt")
        q = shlex.quote
        # replay_after records whether the setup's child still runs when replay_after starts.
        after_cmd = (f"p=$(cat {q(pidfile)}); s=$(cut -d')' -f2 /proc/$p/stat 2>/dev/null | cut -d' ' -f2); "
                     f"if [ -z \"$s\" ] || [ \"$s\" = Z ]; then echo child-gone; else echo child-alive; fi >> {q(after)}")
        with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({
                "setup": {"sample": f"sh -c 'sleep 60 & echo $! > {pidfile}; wait'"},
                "replay_env": {"sample": {"TEST_DATABASE_URL": "postgresql:///sample_test_{slot}"}},
                "replay_before": {"sample": "true"},
                "replay_after": {"sample": after_cmd},
            }, f, indent=1)
        proc = subprocess.Popen([sys.executable, EVOLVE, "replay", self.case_name, "--genome", "current",
                                 "--prepare-only"], cwd=self.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env=evolve_env(self.home, evo), start_new_session=True)
        try:
            def child_started():
                if not os.path.exists(pidfile):
                    return False
                with open(pidfile, encoding="utf-8") as f:
                    return bool(f.read().strip())
            started = wait_for(child_started, 120)
            if started:
                with open(pidfile, encoding="utf-8") as f:
                    self.child = int(f.read().strip())
                time.sleep(0.3)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        self.assertTrue(started, f"setup never started its child:\n{output(result)}")
        plain_failure(self, result, "Ctrl-C during setup")
        self.assertTrue(wait_for(lambda: process_gone(self.child), 5),
                        f"the setup's child (pid {self.child}) still runs after the replay ended")
        self.assertTrue(os.path.exists(after), f"replay_after must still run after Ctrl-C:\n{output(result)}")
        self.assertEqual(read_marks(after), ["child-gone"],
                         "replay_after runs only after the setup's whole process group was stopped")
        self.assertEqual(clones_under(evo), [], "cleanup still deletes the clone after Ctrl-C")

    def test_parent_that_is_not_a_full_commit_hash_is_refused(self):  # covers: REP-3
        for label, parent in (("an option", "-x"), ("a short hash", self.c["parent"][:7]),
                              ("not hex", "g" * 40)):
            with self.subTest(parent=label):
                name = "evolve-parent-" + re.sub(r"\W", "-", label)

                def edit(case, folder, parent=parent):
                    case["parent"] = parent
                evo, _ = self.case_home(name, edit)
                result = replay(self.case_name, self.home, evo, self.tmp)
                text = plain_failure(self, result, f"a parent that is {label}")
                self.assertIn("parent", text.lower(), f"the message names the parent commit:\n{output(result)}")
                self.assertEqual(clones_under(evo), [], f"no clone is made for a parent that is {label}")

    def test_frozen_test_path_outside_the_clone_is_refused(self):  # covers: REP-3
        bad = "../outside/test_x.py"
        body = test_file("x", None).encode()

        def edit(case, folder):
            copy = os.path.normpath(os.path.join(folder, "tests", bad))
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            with open(copy, "wb") as f:
                f.write(body)
            case["tests"].append({"path": bad, "sha256": sha256(body)})
        evo, _ = self.case_home("evolve-escape", edit)
        result = replay(self.case_name, self.home, evo, self.tmp)
        text = plain_failure(self, result, "a frozen test path outside the clone")
        self.assertIn(bad, text, f"the message names the path:\n{output(result)}")
        made = []
        for dirpath, dirnames, _ in os.walk(evo):
            if os.path.relpath(dirpath, evo).split(os.sep)[0] == "bench":
                dirnames[:] = []
                continue
            made += [os.path.join(dirpath, d) for d in dirnames if d == "outside"]
        self.assertEqual(made, [], "no folder outside the clone is created for a frozen test path")


if __name__ == "__main__":
    unittest.main()
