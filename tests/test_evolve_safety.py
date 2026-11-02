"""The evolver never writes to a project it reads, and handles a bad working folder setting calmly
(spec .regista/specs/bench-cases.json, BENCH-10).

project_git (in scripts/evolve.py, loaded here as a module) must refuse options that write a file before git
runs, and every read must run with external diff programs, textconv, fsmonitor and hooks turned off, without
touching the index. REGISTA_EVOLVE_HOME that is relative, names a file, or points at a working folder whose
bench/ (or another entry) is a file gets one plain line and exit code 1; a ~ in it is expanded.

Interface assumed: project_git(repo, *args) or project_git(repo, [args]); the first argument is the project
folder and the rest are git's arguments (without "git"). A refused call may raise or return; either way git
must not run and no file may appear. A read returns git's output as a string, bytes or an object with stdout.
Working-tree and index diffs, cat-file options that run textconv or filters, and GIT_* variables from the
environment must not reach git. Settings that start programs or fetches during a read are overridden on every call:
a submodule's own diff.external (reached through diff.submodule=diff), lazy fetches on a partial clone, and a
--format/--pretty alias from the project's config (refused before git runs). diff.noprefix=true in the project
must not hide covers markers from `bench build`.

Run: python3 -m unittest tests.test_evolve_safety -v"""
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVOLVE = os.path.join(REPO, "scripts", "evolve.py")
PY = sys.executable
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
LAYOUT = ("bench", "genomes", "runs", "reports")
MARKERS = ("external", "driver-command", "textconv", "fsmonitor", "post-checkout", "reference-transaction",
           "clean", "smudge")


def tmpdir(test, prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, True)
    return path


def load_evolve():
    spec = importlib.util.spec_from_file_location("evolve_under_test", EVOLVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call_project_git(fn, repo, args):
    params = inspect.signature(fn).parameters.values()
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return fn(repo, *args)
    return fn(repo, list(args))


def output_text(result):
    """The text git printed, whatever project_git returns: a string, bytes, or a CompletedProcess-like object."""
    if isinstance(result, bytes):
        return result.decode("utf-8", "replace")
    if isinstance(result, str):
        return result
    out = getattr(result, "stdout", None)
    if out is not None:
        return output_text(out)
    if isinstance(result, (tuple, list)):
        return "\n".join(output_text(x) for x in result if isinstance(x, (str, bytes)))
    raise AssertionError(f"can't find git's output in what project_git returned: {result!r}")


def git(cwd, *args, env_extra=None):
    env = {**os.environ, **GIT_ENV, **(env_extra or {})}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")


def write_script(path, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def tree_snapshot(root):
    """Every file under root (the .git folder included) with its content hash and modification time."""
    snap = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            st = os.lstat(path)
            with open(path, "rb") as f:
                snap[os.path.relpath(path, root)] = (hashlib.sha256(f.read()).hexdigest(), st.st_mtime_ns)
    return snap


class GitShimMixin:
    """Puts a `git` on PATH that logs every call, then runs the real git."""

    def install_git_shim(self):
        real_git = shutil.which("git")
        self.assertTrue(real_git, "git isn't installed")
        bin_dir = tmpdir(self, "evolve-shim-")
        self.git_log = os.path.join(bin_dir, "calls.log")
        write_script(os.path.join(bin_dir, "git"), f'printf "%s\\n" "$*" >> "{self.git_log}"\nexec "{real_git}" "$@"')
        patcher = mock.patch.dict(os.environ, {**GIT_ENV, "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def git_calls(self):
        if not os.path.exists(self.git_log):
            return []
        with open(self.git_log, encoding="utf-8") as f:
            return f.read().splitlines()


class ProjectGitRefusesWritingOptions(GitShimMixin, unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.evolve = load_evolve()
        self.assertTrue(callable(getattr(self.evolve, "project_git", None)), "evolve.py has no project_git helper")
        self.repo = tmpdir(self, "evolve-proj-")
        self.cwd = tmpdir(self, "evolve-cwd-")
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "one"],
                     ["commit", "-q", "--allow-empty", "-m", "two"]):
            git(self.repo, *args)
        with open(os.path.join(self.repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "three")
        self.install_git_shim()
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

    def test_output_options_refused_before_git_runs(self):  # covers: BENCH-10
        cases = [
            (["log", "--output=x.txt"], "x.txt"),
            (["diff", "--output=x"], "x"),
            (["diff", "--output", "y"], "y"),
            (["log", "-p", "HEAD~1..HEAD", "--output=z.patch"], "z.patch"),
            (["format-patch", "-o", "patches", "-1"], "patches"),
        ]
        for args, out_name in cases:
            with self.subTest(args=args):
                before_repo, before_cwd = tree_snapshot(self.repo), tree_snapshot(self.cwd)
                calls_before = len(self.git_calls())
                try:
                    call_project_git(self.evolve.project_git, self.repo, args)
                except (Exception, SystemExit):
                    pass
                for folder in (self.repo, self.cwd):
                    self.assertFalse(os.path.exists(os.path.join(folder, out_name)),
                                     f"{args} wrote {out_name} in {folder}")
                self.assertEqual(tree_snapshot(self.repo), before_repo, f"{args} changed the project")
                self.assertEqual(tree_snapshot(self.cwd), before_cwd, f"{args} wrote into the current folder")
                self.assertEqual(self.git_calls()[calls_before:], [], f"{args}: git ran instead of being refused")


class ProjectGitReadsRunNothingFromTheProject(GitShimMixin, unittest.TestCase):
    """A project whose git setup runs programs on reads: each one leaves a marker file if it ever runs."""

    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.evolve = load_evolve()
        self.assertTrue(callable(getattr(self.evolve, "project_git", None)), "evolve.py has no project_git helper")
        self.repo = tmpdir(self, "evolve-proj-")
        self.cwd = tmpdir(self, "evolve-cwd-")
        tools = tmpdir(self, "evolve-tools-")
        self.markers = tmpdir(self, "evolve-markers-")
        mark = lambda name: f'touch "{os.path.join(self.markers, name)}"'
        write_script(os.path.join(tools, "external.sh"), mark("external") + "\nexit 0")
        write_script(os.path.join(tools, "command.sh"), mark("driver-command") + "\nexit 0")
        write_script(os.path.join(tools, "textconv.sh"), mark("textconv") + '\ncat "$1"')
        write_script(os.path.join(tools, "fsmonitor.sh"), mark("fsmonitor") + "\nexit 1")
        write_script(os.path.join(tools, "clean.sh"), mark("clean") + "\ncat")
        write_script(os.path.join(tools, "smudge.sh"), mark("smudge") + "\ncat")

        r = self.repo
        git(r, "init", "-q")
        self.write("plain.txt", "one\n")
        self.write("conv.txt", "one\n")
        self.write("cmd.txt", "one\n")
        self.write("filt.txt", "one\n")
        self.write("same.txt", "never changes\n")
        self.write(".gitattributes", "conv.txt diff=conv\ncmd.txt diff=cmd\nfilt.txt filter=mark\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "first")
        for name in ("plain.txt", "conv.txt", "cmd.txt", "filt.txt"):
            self.write(name, "two\n")
        git(r, "commit", "-q", "-am", "second")
        self.first = git(r, "rev-parse", "HEAD~1").stdout.strip()
        self.second = git(r, "rev-parse", "HEAD").stdout.strip()
        for args in (["diff.external", os.path.join(tools, "external.sh")],
                     ["diff.cmd.command", os.path.join(tools, "command.sh")],
                     ["diff.conv.textconv", os.path.join(tools, "textconv.sh")],
                     ["core.fsmonitor", os.path.join(tools, "fsmonitor.sh")],
                     ["filter.mark.clean", os.path.join(tools, "clean.sh")],
                     ["filter.mark.smudge", os.path.join(tools, "smudge.sh")]):
            git(r, "config", *args)
        hooks = os.path.join(r, ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        for hook in ("post-checkout", "reference-transaction"):
            write_script(os.path.join(hooks, hook), mark(hook))
        # Uncommitted changes, so a diff of the working tree has something to show.
        for name in ("plain.txt", "conv.txt", "cmd.txt", "filt.txt"):
            self.write(name, "three\n")

    def write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(text)

    def fired(self):
        return sorted(os.listdir(self.markers))

    def clear_markers(self):
        for name in os.listdir(self.markers):
            os.remove(os.path.join(self.markers, name))

    def safe_status(self):
        return git(self.repo, "--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--porcelain").stdout

    def test_fixture_programs_do_run_under_plain_git(self):  # covers: BENCH-10
        """Proves the fixture bites: plain git runs each diff and fsmonitor program."""
        git(self.repo, "diff")
        git(self.repo, "log", "-p")
        git(self.repo, "status")
        git(self.repo, "cat-file", "--filters", "HEAD:filt.txt")
        for name in ("external", "driver-command", "textconv", "fsmonitor", "clean", "smudge"):
            self.assertIn(name, self.fired(), f"the fixture's {name} program never ran under plain git")

    def test_reads_run_no_project_program_and_change_nothing(self):  # covers: BENCH-10
        # Plain git may have refreshed the index already; then give an unchanged file a new modification time,
        # so any read that refreshes the index would rewrite it.
        git(self.repo, "status")
        st = os.stat(os.path.join(self.repo, "same.txt"))
        os.utime(os.path.join(self.repo, "same.txt"), ns=(st.st_atime_ns + 10**10, st.st_mtime_ns + 10**10))
        status_before = self.safe_status()
        snap_before = tree_snapshot(self.repo)
        self.clear_markers()
        self.install_git_shim()
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

        reads = [["log", "-p"], ["log", "-p", "--follow", "--", "conv.txt"], ["show", "HEAD"],
                 ["show", "HEAD:conv.txt"], ["diff", "HEAD~1", "HEAD"]]
        for args in reads:
            with self.subTest(args=args):
                self.clear_markers()
                call_project_git(self.evolve.project_git, self.repo, args)
                self.assertEqual(self.fired(), [], f"{args} ran a program from the project's git setup")
        self.assertTrue(self.git_calls(), "project_git didn't run git at all")
        self.assertEqual(self.safe_status(), status_before, "git status changed")
        after = tree_snapshot(self.repo)
        self.assertEqual(after.get(os.path.join(".git", "index")), snap_before.get(os.path.join(".git", "index")),
                         "a read rewrote the index")
        self.assertEqual(after, snap_before, "a read changed a file in the project")
        self.assertEqual(os.listdir(self.cwd), [], "a read wrote into the current folder")

    def enter_cwd_with_shim(self):
        git(self.repo, "status")
        self.clear_markers()
        self.install_git_shim()
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

    def test_conversions_and_working_tree_diffs_refused_before_git_runs(self):  # covers: BENCH-10
        """cat-file options that run textconv or filters (abbreviated too), and every diff that reads the working
        tree or the index, are refused without running git; the fixture's programs would fire if they ran."""
        self.enter_cwd_with_shim()
        snap_before = tree_snapshot(self.repo)
        refused = [["cat-file", "--textc", "HEAD:conv.txt"], ["cat-file", "--filt", "HEAD:filt.txt"],
                   ["cat-file", "--textconv", "HEAD:conv.txt"], ["cat-file", "--filters", "HEAD:filt.txt"],
                   ["diff"], ["diff", "HEAD"], ["diff", "--stat"], ["diff", "--", ":(glob)*"],
                   ["diff", "--cached", self.second], ["diff", self.second, "--", "filt.txt"]]
        for args in refused:
            with self.subTest(args=args):
                self.clear_markers()
                calls_before = len(self.git_calls())
                try:
                    call_project_git(self.evolve.project_git, self.repo, args)
                except (Exception, SystemExit):
                    pass
                self.assertEqual(self.git_calls()[calls_before:], [], f"{args}: git ran instead of being refused")
                self.assertEqual(self.fired(), [], f"{args} ran a program from the project's git setup")
        self.assertEqual(tree_snapshot(self.repo), snap_before, "a refused call changed a file in the project")
        self.assertEqual(os.listdir(self.cwd), [], "a refused call wrote into the current folder")

    def test_commit_to_commit_diff_and_show_still_work(self):  # covers: BENCH-10
        self.enter_cwd_with_shim()
        for args, expected in ((["diff", self.first, self.second], "+two"), (["show", self.first], "first")):
            with self.subTest(args=args):
                self.clear_markers()
                calls_before = len(self.git_calls())
                out = output_text(call_project_git(self.evolve.project_git, self.repo, args))
                self.assertTrue(self.git_calls()[calls_before:], f"{args}: git didn't run")
                self.assertIn(expected, out, f"{args}: unexpected output {out!r}")
                self.assertEqual(self.fired(), [], f"{args} ran a program from the project's git setup")

    def test_git_variables_from_the_environment_are_ignored(self):  # covers: BENCH-10
        """GIT_TRACE pointing into the project and GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE pointing elsewhere must
        not steer project_git: it reads the project it was given, and no trace or index file appears."""
        decoy = tmpdir(self, "evolve-decoy-")
        git(decoy, "init", "-q")
        git(decoy, "commit", "-q", "--allow-empty", "-m", "decoy")
        decoy_head = git(decoy, "rev-parse", "HEAD").stdout.strip()
        self.enter_cwd_with_shim()
        snap_before = tree_snapshot(self.repo)
        trace = os.path.join(self.repo, "trace.log")
        odd = {"GIT_TRACE": trace, "GIT_TRACE2": os.path.join(self.repo, "trace2.log"),
               "GIT_DIR": os.path.join(decoy, ".git"), "GIT_WORK_TREE": decoy,
               "GIT_INDEX_FILE": os.path.join(self.repo, "odd-index"),
               "GIT_EXTERNAL_DIFF": os.path.join(self.markers, "nope.sh")}
        patcher = mock.patch.dict(os.environ, odd)
        patcher.start()
        self.addCleanup(patcher.stop)
        head = output_text(call_project_git(self.evolve.project_git, self.repo, ["rev-parse", "HEAD"])).strip()
        self.assertEqual(head, self.second, f"rev-parse read another repository (decoy HEAD is {decoy_head})")
        out = output_text(call_project_git(self.evolve.project_git, self.repo, ["diff", self.first, self.second]))
        self.assertIn("+two", out, "the commit-to-commit diff didn't read the project")
        for name in ("trace.log", "trace2.log", "odd-index"):
            self.assertFalse(os.path.exists(os.path.join(self.repo, name)), f"{name} appeared in the project")
        self.assertEqual(tree_snapshot(self.repo), snap_before, "a read changed a file in the project")
        self.assertEqual(os.listdir(self.cwd), [], "a read wrote into the current folder")
        self.assertEqual(self.fired(), [])


class EvolveHomeSetting(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.home = tmpdir(self, "evolve-home-")
        self.cwd = tmpdir(self, "evolve-cwd-")

    def run_config(self, evolve_home):
        env = {k: v for k, v in os.environ.items() if k not in ("REGISTA_EVOLVE_HOME", "CLAUDE_PROJECT_DIR")}
        env.update({"HOME": self.home, "REGISTA_EVOLVE_HOME": evolve_home})
        return subprocess.run([PY, EVOLVE, "config"], cwd=self.cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)

    def assert_one_plain_line_exit_1(self, r, what):
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out, f"{what}: Python traceback instead of a plain line")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"{what}: expected one line, got {out!r}")
        self.assertEqual(r.returncode, 1, f"{what}: expected exit code 1, got {r.returncode}: {out!r}")

    def test_relative_path_refused_with_one_line(self):  # covers: BENCH-10
        for value in ("relative/evolve", "evolve-here", "./evolve"):
            with self.subTest(value=value):
                r = self.run_config(value)
                self.assert_one_plain_line_exit_1(r, f"REGISTA_EVOLVE_HOME={value}")
                self.assertEqual(os.listdir(self.cwd), [], "a relative working folder was created anyway")

    def test_tilde_expanded_against_home(self):  # covers: BENCH-10
        r = self.run_config("~/evolve-work")
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertEqual(r.returncode, 0, out)
        folder = os.path.join(self.home, "evolve-work")
        self.assertTrue(os.path.isfile(os.path.join(folder, "evolve.json")), "~ wasn't expanded against HOME")
        for name in LAYOUT:
            self.assertTrue(os.path.isdir(os.path.join(folder, name)), f"{name}/ missing in {folder}")
        self.assertEqual(os.listdir(self.cwd), [], "a literal '~' folder was created in the current folder")

    def test_path_naming_a_file_refused_with_one_line(self):  # covers: BENCH-10
        path = os.path.join(self.home, "not-a-folder")
        with open(path, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        r = self.run_config(path)
        self.assert_one_plain_line_exit_1(r, "REGISTA_EVOLVE_HOME naming a file")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep me\n", "the file was changed")

    def test_working_folder_entry_that_is_a_file_refused_with_one_line(self):  # covers: BENCH-10
        for entry in LAYOUT:
            with self.subTest(entry=entry):
                folder = tempfile.mkdtemp(prefix="evolve-work-", dir=self.home)
                with open(os.path.join(folder, entry), "w", encoding="utf-8") as f:
                    f.write("a file where a folder should be\n")
                r = self.run_config(folder)
                self.assert_one_plain_line_exit_1(r, f"{entry} is a file")
                self.assertTrue(os.path.isfile(os.path.join(folder, entry)), f"{entry} was replaced")


def git_ok(cwd, *args):
    r = git(cwd, *args)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r.stdout.strip()


class ProjectGitOverridesSettingsThatStartPrograms(GitShimMixin, unittest.TestCase):
    """Project settings that start a program or a fetch during a read: each fixture program leaves a marker."""

    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.evolve = load_evolve()
        self.assertTrue(callable(getattr(self.evolve, "project_git", None)), "evolve.py has no project_git helper")
        self.base = tmpdir(self, "evolve-settings-")
        self.cwd = tmpdir(self, "evolve-cwd-")
        self.markers = tmpdir(self, "evolve-markers-")
        self.tools = tmpdir(self, "evolve-tools-")

    def mark(self, name):
        return f'touch "{os.path.join(self.markers, name)}"'

    def fired(self):
        return sorted(os.listdir(self.markers))

    def clear_markers(self):
        for name in os.listdir(self.markers):
            os.remove(os.path.join(self.markers, name))

    def enter_cwd_with_shim(self):
        self.clear_markers()
        self.install_git_shim()
        old = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old)

    def project_git(self, repo, args):
        return call_project_git(self.evolve.project_git, repo, args)

    def try_project_git(self, repo, args):
        try:
            return self.project_git(repo, args), None
        except (Exception, SystemExit) as e:  # a clean failure may raise or return
            return None, e

    # -- submodules -------------------------------------------------------------------------------------------

    def make_submodule_fixture(self):
        """A parent repo whose submodule changes between A and B; the submodule's own config sets diff.external
        and the parent's config asks for submodule diffs inline (diff.submodule=diff)."""
        sub = os.path.join(self.base, "sub")
        parent = os.path.join(self.base, "parent")
        os.makedirs(sub)
        os.makedirs(parent)
        git_ok(sub, "init", "-q")
        with open(os.path.join(sub, "f.txt"), "w", encoding="utf-8") as f:
            f.write("one\n")
        git_ok(sub, "add", "f.txt")
        git_ok(sub, "commit", "-q", "-m", "s1")
        git_ok(parent, "init", "-q")
        git_ok(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "sub")
        git_ok(parent, "commit", "-q", "-m", "A")
        a = git_ok(parent, "rev-parse", "HEAD")
        inner = os.path.join(parent, "sub")
        with open(os.path.join(inner, "f.txt"), "w", encoding="utf-8") as f:
            f.write("two\n")
        git_ok(inner, "commit", "-q", "-am", "s2")
        git_ok(parent, "add", "sub")
        git_ok(parent, "commit", "-q", "-m", "B")
        b = git_ok(parent, "rev-parse", "HEAD")
        external = os.path.join(self.tools, "external.sh")
        write_script(external, self.mark("submodule-external") + "\nexit 0")
        git_ok(parent, "config", "diff.submodule", "diff")
        sub_config = git_ok(inner, "rev-parse", "--git-path", "config")
        git_ok(parent, "config", "--file", os.path.join(inner, sub_config), "diff.external", external)
        return parent, a, b

    def test_submodule_diff_runs_no_program_from_the_submodule_config(self):  # covers: BENCH-10
        parent, a, b = self.make_submodule_fixture()
        git(parent, "log", "-p")  # the fixture bites: plain git runs the submodule's diff.external
        self.assertIn("submodule-external", self.fired(), "the fixture's submodule diff.external never ran")
        snap_before = tree_snapshot(parent)
        self.enter_cwd_with_shim()
        for args in (["log", "-p"], ["show", b], ["show"], ["diff", a, b]):
            with self.subTest(args=args):
                self.clear_markers()
                self.try_project_git(parent, args)
                self.assertEqual(self.fired(), [], f"{args} ran the submodule's diff.external")
        self.assertTrue(self.git_calls(), "project_git didn't run git at all")
        self.assertEqual(tree_snapshot(parent), snap_before, "a read changed a file in the project")
        out = output_text(self.project_git(parent, ["diff", a, b]))
        self.assertIn("Subproject commit", out,
                      f"the submodule change should show as a short diff (diff.submodule=short), not inline: {out!r}")

    # -- --format / --pretty aliases --------------------------------------------------------------------------

    def make_signed_fixture(self):
        """A repo with an unsigned commit and a (fake) signed commit on top, pretty.evil=%G? and gpg.program set
        to a marker-writing script: showing %G? on the signed commit runs gpg.program."""
        repo = os.path.join(self.base, "signed")
        os.makedirs(repo)
        git_ok(repo, "init", "-q")
        git_ok(repo, "commit", "-q", "--allow-empty", "-m", "unsigned")
        unsigned = git_ok(repo, "rev-parse", "HEAD")
        git_ok(repo, "commit", "-q", "--allow-empty", "-m", "signed")
        raw = git_ok(repo, "cat-file", "commit", "HEAD") + "\n"
        head, body = raw.split("\n\n", 1)
        sig = ("gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEfakefakefakefake\n"
               " -----END PGP SIGNATURE-----")
        env = {**os.environ, **GIT_ENV}
        r = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, env=env,
                           input=head + "\n" + sig + "\n\n" + body, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        signed = r.stdout.strip()
        branch = git_ok(repo, "symbolic-ref", "HEAD")
        git_ok(repo, "update-ref", branch, signed)
        gpg = os.path.join(self.tools, "gpg.sh")
        write_script(gpg, self.mark("gpg") + "\nexit 1")
        git_ok(repo, "config", "pretty.evil", "%G?")
        git_ok(repo, "config", "gpg.program", gpg)
        return repo, unsigned, signed

    def test_format_alias_from_project_config_refused_before_git_runs(self):  # covers: BENCH-10
        repo, unsigned, signed = self.make_signed_fixture()
        git(repo, "log", "--format=evil", "-1", signed)  # the fixture bites: the alias runs gpg.program
        self.assertIn("gpg", self.fired(), "the fixture's gpg.program never ran under plain git")
        snap_before = tree_snapshot(repo)
        self.enter_cwd_with_shim()
        refused = [["log", "--format=evil"], ["log", "--pretty=evil"], ["log", "--format=evil", "-1", signed],
                   ["log", "--pretty=evil", unsigned], ["log", "--format", "evil"], ["show", "--format=evil", signed]]
        for args in refused:
            with self.subTest(args=args):
                self.clear_markers()
                calls_before = len(self.git_calls())
                self.try_project_git(repo, args)
                self.assertEqual(self.git_calls()[calls_before:], [], f"{args}: git ran instead of being refused")
                self.assertEqual(self.fired(), [], f"{args} ran gpg.program")
        self.assertEqual(tree_snapshot(repo), snap_before, "a refused call changed a file in the project")

    def test_placeholder_and_builtin_formats_still_work(self):  # covers: BENCH-10
        repo, unsigned, signed = self.make_signed_fixture()
        self.enter_cwd_with_shim()
        for args in (["log", "--format=%H", "-1", signed], ["log", "--format=format:%H", "-1", signed],
                     ["log", "--pretty=oneline", "-1", signed], ["log", "--format=%H", "-1", unsigned]):
            with self.subTest(args=args):
                self.clear_markers()
                out = output_text(self.project_git(repo, args))
                expected = unsigned if unsigned in args else signed
                self.assertIn(expected, out, f"{args}: unexpected output {out!r}")
                self.assertEqual(self.fired(), [], f"{args} ran gpg.program")

    # -- partial clones ---------------------------------------------------------------------------------------

    def make_partial_clone(self):
        """A blobless clone of an origin with two versions of f.txt; no blob was fetched (no checkout)."""
        origin = os.path.join(self.base, "origin")
        os.makedirs(origin)
        git_ok(origin, "init", "-q")
        for text, msg in (("secret blob one\n", "one"), ("secret blob two\n", "two")):
            with open(os.path.join(origin, "f.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            git_ok(origin, "add", "f.txt")
            git_ok(origin, "commit", "-q", "-m", msg)
        git_ok(origin, "config", "uploadpack.allowfilter", "true")
        git_ok(origin, "config", "uploadpack.allowanysha1inwant", "true")
        clone = os.path.join(self.base, "clone")
        git_ok(self.base, "clone", "-q", "--no-checkout", "--filter=blob:none", "file://" + origin, clone)
        blob = git_ok(clone, "rev-parse", "HEAD~1:f.txt")
        upload = os.path.join(self.tools, "upload.sh")
        write_script(upload, self.mark("uploadpack") + '\nexec git-upload-pack "$@"')
        git_ok(clone, "config", "remote.origin.uploadpack", upload)
        return origin, clone, blob

    def packs(self, clone):
        return sorted(os.listdir(os.path.join(clone, ".git", "objects", "pack")))

    def assert_missing_blob_reads_fail_cleanly(self, clone, blob):
        snap_before, packs_before = tree_snapshot(clone), self.packs(clone)
        self.enter_cwd_with_shim()
        for args in (["cat-file", "-p", blob], ["show", "HEAD~1:f.txt"], ["diff", "HEAD~1", "HEAD"],
                     ["log", "-p"]):
            with self.subTest(args=args):
                self.clear_markers()
                result, _ = self.try_project_git(clone, args)
                if result is not None:
                    try:
                        text = output_text(result)
                    except AssertionError:
                        text = ""
                    self.assertNotIn("secret blob one", text, f"{args} fetched the missing blob")
                self.assertEqual(self.fired(), [], f"{args} ran remote.origin.uploadpack (a lazy fetch)")
                self.assertEqual(self.packs(clone), packs_before, f"{args} fetched a new pack")
        self.assertEqual(tree_snapshot(clone), snap_before, "a read changed a file in the clone")

    def test_partial_clone_missing_blob_with_uploadpack_set_fails_cleanly(self):  # covers: BENCH-10
        origin, clone, blob = self.make_partial_clone()
        packs_before = self.packs(clone)
        bite = git(clone, "cat-file", "-p", blob)  # the fixture bites: plain git lazily fetches the blob
        self.assertIn("secret blob one", bite.stdout, f"plain git didn't fetch the blob: {bite.stderr}")
        self.assertIn("uploadpack", self.fired(), "the fixture's uploadpack program never ran")
        self.assertNotEqual(self.packs(clone), packs_before, "plain git's lazy fetch made no pack")
        # A fresh clone for the real check, since the blob is now present in this one.
        shutil.rmtree(clone)
        clone2 = os.path.join(self.base, "clone")
        git_ok(self.base, "clone", "-q", "--no-checkout", "--filter=blob:none", "file://" + origin, clone2)
        git_ok(clone2, "config", "remote.origin.uploadpack", os.path.join(self.tools, "upload.sh"))
        self.assert_missing_blob_reads_fail_cleanly(clone2, blob)

    def test_partial_clone_missing_blob_with_origin_unreachable_fails_cleanly(self):  # covers: BENCH-10
        origin, clone, blob = self.make_partial_clone()
        git_ok(clone, "config", "--unset", "remote.origin.uploadpack")
        os.rename(origin, origin + "-gone")
        self.assert_missing_blob_reads_fail_cleanly(clone, blob)


def load_bench_fixtures():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_bench_cases.py")
    spec = importlib.util.spec_from_file_location("bench_case_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchBuildWithNoPrefix(unittest.TestCase):
    """A project with diff.noprefix=true: `bench build` still finds the covers markers."""

    def test_covers_markers_found_with_diff_noprefix(self):  # covers: BENCH-10
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        b = load_bench_fixtures()
        tmp = tmpdir(self, "evolve-noprefix-")
        home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve")
        os.makedirs(home)
        r = b.Repo(os.path.join(tmp, "projects"))
        plan = "# Sample: plan\n\n## M1 — One\n- [x] Step one (ONE-1)\n"
        r.commit(0, "setup", {".gitignore": b.GITIGNORE, ".regista/config.json": json.dumps(b.CONFIG, indent=1) + "\n",
                              ".regista/plan.md": plan, "src/app.py": "VALUE = 1\n"})
        parent = r.commit(1, "one: spec and source", {
            ".regista/specs/one.json": b.spec("one", "M1", "Step one", "Step one (ONE-1)", "one() in src/one.py.",
                                              [("ONE-1", "one() SHALL return 1", "test")]),
            "src/one.py": "def one():\n    return 0\n"})
        finish = r.commit(2, "one: done", {"src/one.py": "def one():\n    return 1\n",
                                           "tests/unit/test_one.py": b.test_file("one", "ONE-1")})
        r.git("config", "diff.noprefix", "true")
        self.assertNotIn("+++ b/", r.git("show", finish), "the fixture's diff.noprefix didn't take effect")
        result = b.run_build(r.root, home, evo)
        self.assertEqual(result.returncode, 0, f"bench build failed:\n{result.stdout}\n{result.stderr}")
        cases = b.load_cases(evo)
        self.assertIn("one", cases, f"no case for step one: {sorted(cases)}")
        case = cases["one"][1]
        self.assertTrue(case["replayable"], f"the covers marker wasn't found with diff.noprefix: {case['reasons']}")
        self.assertTrue(b.same_commit(case["finishing_commit"], finish), case.get("finishing_commit"))
        self.assertTrue(b.same_commit(case["parent"], parent), case.get("parent"))


if __name__ == "__main__":
    unittest.main()
