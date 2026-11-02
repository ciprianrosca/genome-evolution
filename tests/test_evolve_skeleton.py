"""The evolver's skeleton (spec .regista/specs/evolve-skeleton.json): scripts/evolve.py uses only the standard
library and never imports the harness (EVO-1), sets up its working folder and evolve.json with the defaults
(EVO-2), refuses commands that aren't built yet with one plain line and exit code 2, and never changes a project
it reads (EVO-3).

evolve.py is only ever run as a subprocess, with REGISTA_EVOLVE_HOME and HOME pointing at temporary folders, so
the real ~/.regista-evolve/ is never touched.

Run: python3 -m unittest tests.test_evolve_skeleton -v"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVOLVE = os.path.join(REPO, "scripts", "evolve.py")
# The local modules evolve.py may import: scripts/genome.py (spec genome-seed), scripts/evolve_core.py (spec
# adapter-core), scripts/suite.py (spec suite-files) and scripts/runners.py (spec suite-runs). Each is otherwise
# standard library only, and may import only the local modules listed for it here: suite imports genome, evolve_core
# and runners, runners imports evolve_core, evolve_core imports genome, and genome imports no local module (spec
# adapter-core, point 7).
LOCAL_MODULES = {"genome", "evolve_core", "suite", "runners"}
LOCAL_IMPORTS = {"genome": set(), "evolve_core": {"genome"}, "suite": {"genome", "evolve_core", "runners"},
                 "runners": {"evolve_core"}}
PY = sys.executable

DEFAULTS = {"target": "auto", "population": 6, "generations_max": 1, "task_model": "sonnet",
            "reflection_model": "opus", "screen_cases": 4, "max_parallel": 1, "budget_tokens": 20000000,
            "holdout_pct": 40}
LAYOUT = ("bench", "genomes", "runs", "reports")

# Commands that this step leaves unbuilt. When a later step builds one, it moves out of this list. It is empty now
# that every planned command is built; a future placeholder command goes here and is checked again.
NOT_BUILT = []
# Commands that may or may not be built at this step: if they answer "not built" they must do it properly.
# `generation`, `report` and `promote` were built by their steps (specs generation.json, report.json and
# promote.json), so they moved here from NOT_BUILT.
MAYBE_BUILT = [["bench", "list"], ["generation", "--target", "auto", "--budget", "1000"], ["report", "1"],
               ["promote", "g-1", "--generation", "1"]]


def tmpdir(test, prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, True)
    return path


def run_evolve(args, evolve_home, home, cwd=None, unset_evolve_home=False):
    env = {k: v for k, v in os.environ.items() if k not in ("REGISTA_EVOLVE_HOME", "CLAUDE_PROJECT_DIR")}
    env["HOME"] = home
    if not unset_evolve_home:
        env["REGISTA_EVOLVE_HOME"] = evolve_home
    return subprocess.run([PY, EVOLVE, *args], cwd=cwd or home, env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def git(cwd, *args):
    return subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


class Evo1StandardLibraryOnly(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        with open(EVOLVE, encoding="utf-8") as f:
            self.tree = ast.parse(f.read(), filename=EVOLVE)

    def imports_of(self, tree):
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, f"relative import at line {node.lineno}")
                imported.append(node.module or "")
        return imported

    def test_imports_only_standard_library_never_regista(self):  # covers: EVO-1
        imported = self.imports_of(self.tree)
        self.assertTrue(imported, "evolve.py imports nothing at all, which is unexpected")
        for name in imported:
            top = name.split(".")[0]
            self.assertNotEqual(top, "regista", "evolve.py must not import the harness")
            if top in LOCAL_MODULES:
                self.assertEqual(name, top, f"{name}: only a local module itself may be imported")
                continue
            self.assertIn(top, sys.stdlib_module_names, f"{name} isn't a standard library module")
        local = sorted({name.split(".")[0] for name in imported} & LOCAL_MODULES)
        checked = set()
        while local:
            module = local.pop()
            if module in checked:
                continue
            checked.add(module)
            path = os.path.join(REPO, "scripts", module + ".py")
            self.assertTrue(os.path.isfile(path), f"evolve.py imports {module}, but scripts/{module}.py is missing")
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for name in self.imports_of(tree):
                top = name.split(".")[0]
                self.assertNotEqual(top, "regista", f"scripts/{module}.py must not import the harness")
                if top in LOCAL_MODULES:
                    self.assertIn(top, LOCAL_IMPORTS[module],
                                  f"scripts/{module}.py may not import {top}: suite may import only genome, "
                                  f"evolve_core and runners, runners only evolve_core, evolve_core only genome, "
                                  f"and genome no local module")
                    self.assertEqual(name, top, f"scripts/{module}.py: only a local module itself may be imported")
                    local.append(top)
                    continue
                self.assertIn(top, sys.stdlib_module_names,
                              f"scripts/{module}.py imports {name}, which isn't a standard library module")

    def test_never_loads_the_harness_as_code(self):  # covers: EVO-1
        # The harness is reached only through its command line, so no dynamic loading of modules or code.
        forbidden_modules = {"importlib", "runpy", "imp", "zipimport"}
        forbidden_calls = {"__import__", "exec", "eval", "compile", "execfile"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden_modules, f"line {node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden_modules, f"line {node.lineno}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls, f"line {node.lineno}")


class Evo2WorkingFolderAndConfig(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self, "evolve-home-")
        self.evolve_home = tmpdir(self, "evolve-work-")

    def config(self, **kw):
        r = run_evolve(["config"], self.evolve_home, self.home, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def assert_layout(self, folder):
        for name in LAYOUT:
            self.assertTrue(os.path.isdir(os.path.join(folder, name)), f"{name}/ missing in {folder}")
        with open(os.path.join(folder, "evolve.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), DEFAULTS)

    def test_empty_folder_gets_layout_and_default_config(self):  # covers: EVO-2
        self.assertEqual(self.config(), DEFAULTS)
        self.assert_layout(self.evolve_home)

    def test_missing_folder_is_created_on_first_use(self):  # covers: EVO-2
        self.evolve_home = os.path.join(self.evolve_home, "not", "there", "yet")
        self.assertEqual(self.config(), DEFAULTS)
        self.assert_layout(self.evolve_home)

    def test_without_variable_uses_regista_evolve_in_home(self):  # covers: EVO-2
        self.assertEqual(self.config(unset_evolve_home=True), DEFAULTS)
        self.assert_layout(os.path.join(self.home, ".regista-evolve"))
        self.assertEqual(os.listdir(self.evolve_home), [], "the unset variable's folder must stay untouched")

    def test_partial_config_gets_defaults_for_the_rest_and_keeps_unknown_keys(self):  # covers: EVO-2
        own = {"population": 3, "task_model": "haiku", "holdout_pct": 0, "my_note": {"keep": [1, 2]}}
        path = os.path.join(self.evolve_home, "evolve.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(own, f)
        self.assertEqual(self.config(), {**DEFAULTS, **own})
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("my_note"), {"keep": [1, 2]}, "unknown key dropped from the file")
        for name in LAYOUT:
            self.assertTrue(os.path.isdir(os.path.join(self.evolve_home, name)), f"{name}/ missing")

    def test_config_runs_twice_with_same_answer(self):  # covers: EVO-2
        first = self.config()
        self.assertEqual(self.config(), first)


class Evo3UnbuiltCommands(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.home = tmpdir(self, "evolve-home-")
        self.evolve_home = tmpdir(self, "evolve-work-")

    def assert_not_built_answer(self, args, r):
        out = r.stdout + r.stderr
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"{args}: expected one line, got {out!r}")
        self.assertNotIn("Traceback", out)
        self.assertIn("not", lines[0].lower(), f"{args}: the line should say the command isn't built: {out!r}")

    def test_unbuilt_commands_print_one_line_and_exit_2(self):  # covers: EVO-3
        if not NOT_BUILT:
            self.skipTest("every planned command is built, so there is no unbuilt command to check")
        for args in NOT_BUILT:
            with self.subTest(args=args):
                r = run_evolve(args, self.evolve_home, self.home)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assert_not_built_answer(args, r)

    def test_maybe_built_commands_answer_properly_when_unbuilt(self):  # covers: EVO-3
        for args in MAYBE_BUILT:
            with self.subTest(args=args):
                r = run_evolve(args, self.evolve_home, self.home)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                if r.returncode == 2:
                    self.assert_not_built_answer(args, r)


class Evo3ProjectUnchanged(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        self.home = tmpdir(self, "evolve-home-")
        self.evolve_home = tmpdir(self, "evolve-work-")
        self.root = tmpdir(self, "evolve-project-")
        self.worktree = tmpdir(self, "evolve-worktree-")
        os.rmdir(self.worktree)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "T")
        self.write(".regista/specs/stats.json", json.dumps(
            {"step": "stats", "requirements": [{"id": "STATS-1", "text": "x", "verify": "test"}]}))
        self.write("app.py", "print('one')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "first")
        self.write("app.py", "print('two')\n")
        git(self.root, "commit", "-q", "-am", "second")
        git(self.root, "branch", "feature")
        git(self.root, "tag", "v1")
        git(self.root, "worktree", "add", "-q", self.worktree, "feature")
        # a dirty working copy: a staged change, an unstaged change and an untracked file
        self.write("staged.txt", "staged\n")
        git(self.root, "add", "staged.txt")
        self.write("app.py", "print('three')\n")
        self.write("untracked.txt", "loose\n")

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def snapshot(self):
        with open(os.path.join(self.root, ".git", "index"), "rb") as f:
            index_bytes = hashlib.sha256(f.read()).hexdigest()
        files = {}
        for folder in (self.root, self.worktree):
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as f:
                        files[path] = hashlib.sha256(f.read()).hexdigest()
        return {
            "index_bytes": index_bytes,
            "index": git(self.root, "ls-files", "-s"),
            "head": git(self.root, "rev-parse", "HEAD"),
            "head_ref": git(self.root, "symbolic-ref", "HEAD"),
            "refs": git(self.root, "for-each-ref", "--format=%(refname) %(objectname)"),
            "worktrees": git(self.root, "worktree", "list", "--porcelain"),
            "status": git(self.root, "status", "--porcelain", "--untracked-files=all"),
            "worktree_status": git(self.worktree, "status", "--porcelain", "--untracked-files=all"),
            "stash": git(self.root, "stash", "list"),
            "files": files,
        }

    def test_bench_build_leaves_project_unchanged(self):  # covers: EVO-3
        before = self.snapshot()
        for cwd in (self.home, self.root):
            r = run_evolve(["bench", "build", self.root], self.evolve_home, self.home, cwd=cwd)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
            self.assertIn(r.returncode, (0, 2), r.stdout + r.stderr)
        after = self.snapshot()
        for key in before:
            self.assertEqual(after[key], before[key], f"the project's {key} changed")


def f_string_quote_reuse(source):
    """Line numbers where a string nested inside an f-string's {} uses the same quote as that f-string
    (allowed only since Python 3.12). Empty on Pythons older than 3.12, where such code doesn't even parse."""
    import io
    import tokenize
    if not hasattr(tokenize, "FSTRING_START"):
        return []
    found, quotes = [], []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.STRING, tokenize.FSTRING_START) and quotes:
            body = tok.string.lstrip("rRbBuUfF")
            if any(body.startswith(q) for q in quotes):
                found.append(tok.start[0])
        if tok.type == tokenize.FSTRING_START:
            quote = tok.string.lstrip("rRbBuUfF")
            quotes.append(quote if quote in ('"""', "'''") else quote[:1])
        elif tok.type == tokenize.FSTRING_END:
            quotes.pop()
    return found


class Evo4ParsesWithPython310Grammar(unittest.TestCase):
    def test_parses_with_python_3_10_grammar(self):  # covers: EVO-4
        self.assertTrue(os.path.isfile(EVOLVE), "scripts/evolve.py doesn't exist")
        with open(EVOLVE, encoding="utf-8") as f:
            source = f.read()
        try:
            ast.parse(source, filename=EVOLVE, feature_version=(3, 10))
        except SyntaxError as e:
            self.fail(f"evolve.py doesn't parse with Python 3.10's grammar: {e.msg} at line {e.lineno}")
        # On Python 3.12 and newer, feature_version doesn't catch the new f-string syntax (PEP 701), so look
        # for its most common form directly: a string inside an f-string's {} that reuses the f-string's quote.
        self.assertEqual(f_string_quote_reuse(source), [],
                         "evolve.py reuses an f-string's quote inside its {}, which Python 3.10 and 3.11 reject")


if __name__ == "__main__":
    unittest.main()
