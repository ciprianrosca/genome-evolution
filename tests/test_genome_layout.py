"""The genome in its own folder (spec .regista/specs/genome-folder.json, milestone M11): the genome's code, profile,
spec, README and tests live under genome/ (SEP-1); nothing under genome/ imports Regista's Python or puts Regista's
scripts/ or tests/ on sys.path, and the Regista adapter reaches Regista only by running its command line, from the
checkout that holds genome/ or from evolve.json 'regista_checkout' (SEP-2); the genome's tests run as their own
commit gates, genome-tests and genome-e2e, and Regista's tests and e2e gates don't reach genome/ (SEP-5).

REGISTA_CHECKOUT is the Regista checkout that holds the genome folder (its parent). This file reaches it only by path
(reading files, listing them) and by running programs; it never imports anything from it.

What the tests assume about the interface:
- evolve.py's `bench build <project>` runs Regista's command line, `scripts/regista.py refusals --json` (directly or
  through the scripts/regista launcher), in the checkout it is configured with: the folder that contains genome/ by
  default, or evolve.json's 'regista_checkout' (an absolute path).
- The gates in .regista/config.json run `python3 -m unittest discover -s <folder> ...`; the start folder (-s) is what
  decides which tests a gate runs.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest discover -s genome/tests -p "test_genome_layout.py" -v"""
import ast
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
REGISTA_CHECKOUT = os.path.dirname(GENOME)  # the Regista checkout that holds the genome: reached only by path
GENOME_SCRIPTS = ("evolve.py", "evolve_core.py", "genome.py", "suite.py")
GENOME_MODULES = {name[:-3] for name in GENOME_SCRIPTS}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}


def python_files(root):
    """Every .py file under root, skipping caches and virtual environments."""
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found += [os.path.join(folder, n) for n in sorted(names) if n.endswith(".py")]
    return found


def parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def rel(path):
    return os.path.relpath(path, REGISTA_CHECKOUT)


def inside(path, folder):
    """Whether path is folder itself or somewhere under it (real paths, whole path parts)."""
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def docstring_nodes(tree):
    """The constants that are docstrings (module, class or function): text, not code."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value,
                                                                                                    str):
                ids.add(id(first.value))
    return ids


def imported_names(tree):
    """(line, top-level module name) for every absolute import."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append((node.lineno, node.module.split(".")[0]))
    return names


def call_name(node):
    """The called function's name as written (`importlib.import_module`, `sys.path.insert`), or ''."""
    try:
        return ast.unparse(node.func)
    except Exception:  # noqa: BLE001
        return ""


def git_files(root):
    """The checkout's files as git sees them (tracked plus untracked, ignored ones left out), or None without git."""
    if not os.path.exists(os.path.join(root, ".git")) or shutil.which("git") is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        return None
    return sorted({p for p in r.stdout.split("\0") if p and os.path.exists(os.path.join(root, p))})


def checkout_files(root):
    files = git_files(root)
    if files is not None:
        return files
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d != ".claude")
        found += [os.path.relpath(os.path.join(folder, n), root) for n in names]
    return sorted(found)


# Calls that take a path or a command: a genome file name passed to one of these is a use of the genome's code.
PATH_OR_COMMAND_MODULES = ("subprocess.", "os.", "shutil.", "runpy.", "pathlib.", "importlib.")
PATH_OR_COMMAND_CALLS = {"open", "Path", "PurePath", "PosixPath", "run", "Popen", "call", "check_call", "check_output",
                         "system", "popen", "run_path", "run_module", "spec_from_file_location", "join"}
# The ways a string can flow into such a call's argument: inside a list, an f-string, a concatenation, a keyword.
PASS_THROUGH = (ast.List, ast.Tuple, ast.Starred, ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.keyword)
PATH_STRING = re.compile(r"[/\\]\s*(?:%s)\b" % "|".join(re.escape(n) for n in GENOME_SCRIPTS))


def parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def used_as_path_or_command(node, parents):
    """Whether a string naming a genome file is used as a path or a command, not just as a word: it flows into
    subprocess, open, os, os.path, pathlib, runpy or importlib, is joined with a folder (`Path(d) / "evolve.py"`), or
    is itself a path (`genome/scripts`, `scripts/evolve.py`) kept as a value (a constant, a list item, a return value).
    A plain word, such as a label in a list of words a scanner forbids, is not a use, and neither is a path written
    as text handed to some other function (tests/test_without_genome.py plants such text in a temporary file to
    check its scanner finds it)."""
    is_path = "genome/scripts" in node.value or bool(PATH_STRING.search(node.value))
    current = node
    while id(current) in parents:
        parent = parents[id(current)]
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
            return True  # a pathlib path: Path(folder) / "evolve.py"
        if isinstance(parent, ast.Call):
            if current is parent.func:
                return False
            name = call_name(parent)
            return name.startswith(PATH_OR_COMMAND_MODULES) or name.split(".")[-1] in PATH_OR_COMMAND_CALLS
        if not isinstance(parent, PASS_THROUGH):
            return is_path
        current = parent
    return is_path


def genome_reaches(tree):
    """Where a test's code reaches the genome's code: '<line> <what>' for each import of a genome module, each
    import_module or __import__ of one, each os.path.join into genome/scripts, and each genome file name used as a
    path or a command. Docstrings are text, not code, and are left out."""
    docs = docstring_nodes(tree)
    parents = parent_map(tree)
    problems = [f"{line} imports {name}" for line, name in imported_names(tree) if name in GENOME_MODULES]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node).split(".")[-1] in ("import_module", "__import__"):
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and str(arg.value).split(".")[0] in GENOME_MODULES:
                    problems.append(f"{node.lineno} loads {arg.value}")
        if isinstance(node, ast.Call) and call_name(node).endswith("path.join"):
            parts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if any(a == "genome" and b == "scripts" for a, b in zip(parts, parts[1:])):
                problems.append(f"{node.lineno} builds a path into genome/scripts")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
              and any(word in node.value for word in GENOME_SCRIPTS + ("genome/scripts",))
              and used_as_path_or_command(node, parents)):
            problems.append(f"{node.lineno} names the genome's code: {node.value[:80]!r}")
    return problems


# SEP-1: the layout ----------------------------------------------------------------------------------------------------

class GenomeFolderLayout(unittest.TestCase):
    """The genome's code, profile, spec and README are under genome/, and only there."""

    def test_genome_scripts_exist_only_under_genome_scripts(self):  # covers: SEP-1
        for name in GENOME_SCRIPTS:
            self.assertTrue(os.path.isfile(os.path.join(GENOME, "scripts", name)), f"genome/scripts/{name} is missing")
        elsewhere = [p for p in checkout_files(REGISTA_CHECKOUT)
                     if os.path.basename(p) in GENOME_SCRIPTS
                     and os.path.normpath(p) != os.path.join("genome", "scripts", os.path.basename(p))]
        self.assertEqual(elsewhere, [], "the genome's scripts are also somewhere other than genome/scripts/")

    def test_profile_spec_and_readme_are_in_the_genome_folder(self):  # covers: SEP-1
        for path, what in [(os.path.join("genome", "profiles", "regista.json"), "Regista's profile"),
                           (os.path.join("genome", "docs", "genome-spec.md"), "the genome's spec"),
                           (os.path.join("genome", "README.md"), "the genome's README")]:
            self.assertTrue(os.path.isfile(os.path.join(REGISTA_CHECKOUT, path)), f"{what} is not at {path}")
        with open(os.path.join(GENOME, "profiles", "regista.json"), encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict, "genome/profiles/regista.json is a JSON object")
        for old in (os.path.join("genome", "regista.json"), os.path.join("docs", "genome-spec.md")):
            self.assertFalse(os.path.exists(os.path.join(REGISTA_CHECKOUT, old)), f"{old} is still there: it moved")

    def test_genome_tests_live_in_genome_tests_and_e2e_is_no_package(self):  # covers: SEP-1
        self.assertTrue(os.path.isdir(os.path.join(GENOME, "tests", "e2e")), "genome/tests/e2e/ is missing")
        units = [n for n in os.listdir(os.path.join(GENOME, "tests")) if n.startswith("test_") and n.endswith(".py")]
        e2e = [n for n in os.listdir(os.path.join(GENOME, "tests", "e2e")) if n.startswith("test_") and n.endswith(".py")]
        self.assertGreater(len(units), 1, "the genome's unit tests are in genome/tests/")
        self.assertTrue(e2e, "the genome's end-to-end tests are in genome/tests/e2e/")
        self.assertFalse(os.path.exists(os.path.join(GENOME, "tests", "e2e", "__init__.py")),
                         "genome/tests/e2e has no __init__.py, like Regista's tests/e2e")

    def test_no_regista_test_imports_or_runs_the_genome(self):  # covers: SEP-1
        tests = os.path.join(REGISTA_CHECKOUT, "tests")
        self.assertTrue(os.path.isdir(tests), "Regista's tests/ folder is missing")
        problems = []
        for path in python_files(tests):
            problems += [f"{rel(path)}:{p}" for p in genome_reaches(parse(path))]
        self.assertEqual(problems, [], "Regista's tests reach the genome's code")

    def test_the_genome_scanner_still_catches_a_test_that_runs_the_genome(self):  # covers: SEP-1
        """A self-check: a planted test that imports, loads, opens or runs a genome module is caught, and one that
        only lists the genome's file names as words (as tests/test_without_genome.py does) is not."""
        reaching = {
            "import evolve": "imports",
            "import importlib\nimportlib.import_module('evolve_core')": "loads",
            "import os\nos.path.join(ROOT, 'genome', 'scripts', 'x')": "path into genome/scripts",
            "import subprocess, sys\nsubprocess.run([sys.executable, 'evolve.py', 'bench'])": "runs",
            "import subprocess, sys, os\nsubprocess.run([sys.executable, os.path.join(D, 'suite.py')])": "runs",
            "open('genome.py').read()": "opens",
            "CMD = 'python3 genome/scripts/evolve.py bench'": "a path into genome/scripts",
            "from pathlib import Path\nP = Path(ROOT) / 'evolve_core.py'": "a pathlib path",
        }
        for source, what in reaching.items():
            with self.subTest(what=what):
                self.assertTrue(genome_reaches(ast.parse(source)), f"the scanner misses a test that {what}: {source}")
        words = ("FORBIDDEN = [('evolve.py', 1), ('suite.py', 2)]\nLABEL = 'genome.py'\nMSG = f'names {LABEL}'\n"
                 "found = scan(plant('x.md', 'run genome/scripts/evolve.py now'))")
        self.assertEqual(genome_reaches(ast.parse(words)), [], "a plain string naming a genome file is not a use")


# SEP-2: nothing under genome/ imports Regista; the adapter runs Regista's command line -------------------------------

def regista_module_names():
    """Module names that would be Regista's: regista itself and every Python file in Regista's scripts/, tests/ and
    tests/e2e/, unless the genome has a file of that name of its own (its copy of harness.py, say)."""
    names = {"regista"}
    for folder in ("scripts", "tests", os.path.join("tests", "e2e")):
        path = os.path.join(REGISTA_CHECKOUT, folder)
        if os.path.isdir(path):
            names |= {n[:-3] for n in os.listdir(path) if n.endswith(".py")}
    own = {os.path.basename(p)[:-3] for p in python_files(GENOME)}
    return {n for n in names if n not in own and n not in sys.stdlib_module_names} | {"regista"}


class Resolver:
    """Replaces names in an expression by what they were assigned, inside the function first, then the module: enough
    to see where `sys.path.insert(0, SCRIPTS)` points when SCRIPTS = os.path.join(ROOT, "scripts")."""

    def __init__(self, tree):
        self.module = self.assignments(tree)
        self.scopes = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):  # outer functions come first, so the innermost one wins
                    self.scopes[id(inner)] = node

    @staticmethod
    def assignments(scope):
        found = {}
        todo = list(scope.body)
        while todo:
            node = todo.pop(0)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value
            for field in ("body", "orelse", "finalbody", "handlers"):
                todo += [n for n in getattr(node, field, []) or [] if isinstance(n, ast.AST)]
        return found

    def text(self, expr):
        scope = self.scopes.get(id(expr))
        local = self.assignments(scope) if scope is not None else {}
        node = copy.deepcopy(expr)
        for _ in range(6):
            changed = False

            class Swap(ast.NodeTransformer):
                def visit_Name(inner, name):  # noqa: N805
                    nonlocal changed
                    value = local.get(name.id, self.module.get(name.id))
                    if value is None or isinstance(value, ast.Name) and value.id == name.id:
                        return name
                    changed = True
                    return copy.deepcopy(value)

            node = Swap().visit(node)
            if not changed:
                break
        return ast.unparse(node)


def path_additions(tree):
    """(line, expression) for everything added to sys.path: insert, append, extend, slice assignment, +=, and
    site.addsitedir."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name in ("sys.path.insert", "sys.path.append", "sys.path.extend", "site.addsitedir") and node.args:
                found.append((node.lineno, node.args[-1]))
        elif isinstance(node, ast.Assign) and any(ast.unparse(t).startswith("sys.path") for t in node.targets):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.AugAssign) and ast.unparse(node.target) == "sys.path":
            found.append((node.lineno, node.value))
    return found


IMPORT_PROGRAM = r"""
import importlib, json, os, sys
folders = sys.argv[1:]
errors = {}
for folder in folders:
    sys.path.insert(0, folder)
for folder in folders:
    for name in sorted(os.listdir(folder)):
        module = name[:-3]
        if not name.endswith(".py") or name == "__init__.py" or module in sys.modules:
            continue
        try:
            importlib.import_module(module)
        except BaseException as e:  # noqa: BLE001
            errors[os.path.join(folder, name)] = f"{type(e).__name__}: {e}"
files = {n: getattr(m, "__file__", None) for n, m in list(sys.modules.items())}
print(json.dumps({"path": sys.path, "modules": files, "errors": errors}))
"""


class GenomeImportsNothingFromRegista(unittest.TestCase):
    """Read with ast: no Python file under genome/ imports a Regista module or puts Regista's scripts/ or tests/ on
    sys.path. Then imported for real, in a separate Python: no module loaded from Regista's checkout outside genome/,
    and no sys.path entry in Regista's scripts/ or tests/."""

    def test_no_python_file_under_genome_imports_a_regista_module(self):  # covers: SEP-2
        regista = regista_module_names()
        files = python_files(GENOME)
        self.assertTrue(any(p.endswith(os.path.join("scripts", "evolve.py")) for p in files),
                        "genome/scripts/evolve.py is read too")
        problems = []
        for path in files:
            tree = parse(path)
            for line, name in imported_names(tree):
                if name in regista:
                    problems.append(f"{rel(path)}:{line} imports {name}, a Regista module")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = call_name(node).split(".")[-1]
                if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant):
                    if str(node.args[0].value).split(".")[0] in regista:
                        problems.append(f"{rel(path)}:{node.lineno} loads {node.args[0].value}, a Regista module")
                if name in ("spec_from_file_location", "run_path", "SourceFileLoader", "exec_module"):
                    text = Resolver(tree).text(node)
                    if "REGISTA_CHECKOUT" in text or "regista.py" in text:
                        problems.append(f"{rel(path)}:{node.lineno} loads Regista's code as a module: {text[:120]}")
        self.assertEqual(problems, [], "the genome imports Regista's Python")

    def test_no_python_file_under_genome_puts_regista_folders_on_sys_path(self):  # covers: SEP-2
        problems = []
        for path in python_files(GENOME):
            tree = parse(path)
            resolver = Resolver(tree)
            depth = len(os.path.relpath(path, GENOME).split(os.sep))  # steps up from the file to genome/ itself
            for line, expr in path_additions(tree):
                text = resolver.text(expr)
                if "REGISTA_CHECKOUT" in text or "regista_checkout" in text:
                    problems.append(f"{rel(path)}:{line} adds the Regista checkout to sys.path: {text[:120]}")
                elif "__file__" in text and text.count("dirname(") + text.count("'..'") > depth:
                    problems.append(f"{rel(path)}:{line} adds a folder above genome/ to sys.path: {text[:120]}")
        self.assertEqual(problems, [], "the genome puts Regista's folders on sys.path")

    def import_all(self, folders):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CLAUDE_PROJECT_DIR")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        tmp = tempfile.mkdtemp(prefix="genome-layout-")
        self.addCleanup(shutil.rmtree, tmp, True)
        r = subprocess.run([sys.executable, "-c", IMPORT_PROGRAM, *folders], cwd=tmp, capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=300)
        self.assertEqual(r.returncode, 0, f"importing the genome's modules failed:\n{r.stdout}\n{r.stderr}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def assert_nothing_from_regista(self, seen):
        self.assertEqual(seen["errors"], {}, "every genome module imports on its own")
        self.assertNotIn("regista", seen["modules"], "the regista module was imported")
        loaded = sorted(f"{name} from {rel(path)}" for name, path in seen["modules"].items()
                        if path and inside(path, REGISTA_CHECKOUT) and not inside(path, GENOME))
        self.assertEqual(loaded, [], "modules loaded from Regista's checkout outside genome/")
        on_path = [p for p in seen["path"] if p and any(inside(p, os.path.join(REGISTA_CHECKOUT, folder))
                                                        for folder in ("scripts", "tests"))]
        self.assertEqual(on_path, [], "Regista's scripts/ or tests/ ended up on sys.path")

    def test_importing_the_genome_code_and_unit_tests_loads_nothing_from_regista(self):  # covers: SEP-2
        self.assert_nothing_from_regista(self.import_all([os.path.join(GENOME, "scripts"),
                                                          os.path.join(GENOME, "tests")]))

    def test_importing_the_genome_e2e_tests_loads_nothing_from_regista(self):  # covers: SEP-2
        self.assert_nothing_from_regista(self.import_all([os.path.join(GENOME, "tests", "e2e")]))


FAKE_REGISTA = r'''import json, os, subprocess, sys
# A stand-in for Regista's command line: logs the call, then runs the real one as an outside program.
with open(@LOG@, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
sys.exit(subprocess.run([sys.executable, @REAL@, *sys.argv[1:]]).returncode)
'''
FAKE_LAUNCHER = '#!/bin/sh\nexec python3 "$(dirname "$0")/regista.py" "$@"\n'


def fake_checkout(root, log):
    """A Regista checkout whose scripts/regista.py (and its launcher) log each call and pass it to the real one."""
    scripts = os.path.join(root, "scripts")
    os.makedirs(scripts, exist_ok=True)
    with open(os.path.join(scripts, "regista.py"), "w", encoding="utf-8") as f:
        f.write(FAKE_REGISTA.replace("@LOG@", repr(log))
                .replace("@REAL@", repr(os.path.join(REGISTA_CHECKOUT, "scripts", "regista.py"))))
    launcher = os.path.join(scripts, "regista")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(FAKE_LAUNCHER)
    os.chmod(launcher, 0o755)


def calls(log):
    if not os.path.isfile(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class AdapterRunsRegistasCommandLine(unittest.TestCase):
    """`bench build` reads a project's refusals with `regista.py refusals --json` from the checkout that holds
    genome/ (a copy of the genome inside a fake checkout), or from evolve.json's regista_checkout."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_signal_check as sc  # the genome's own fixture project with refusals (module import: runs nothing)
        cls.sc = sc
        cls.tmp = tempfile.mkdtemp(prefix="genome-layout-adapter-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        rows = sc.ten_refusals(["unsure"] * 5 + [None] * 5)
        cls.project = sc.build_project(os.path.join(cls.tmp, "projects"), "layout", refusals=rows).root

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def bench_build(self, evolve, evolve_home):
        return subprocess.run([sys.executable, evolve, "bench", "build", self.project],
                              cwd=os.path.dirname(self.project), capture_output=True, text=True, encoding="utf-8",
                              env=self.sc.clean_env(self.home, evolve_home), timeout=300)

    def assert_refusals_read_through(self, log, r):
        self.assertEqual(r.returncode, 0, f"bench build failed:\n{r.stdout}\n{r.stderr}")
        seen = calls(log)
        self.assertTrue(any("refusals" in c["argv"] and "--json" in c["argv"] for c in seen),
                        f"bench build ran `regista.py refusals --json` from the configured checkout: {seen}")

    def test_default_checkout_is_the_folder_holding_genome(self):  # covers: SEP-2
        checkout = os.path.join(self.tmp, "default-checkout")
        log = os.path.join(self.tmp, "default-calls.jsonl")
        fake_checkout(checkout, log)
        ignore = shutil.ignore_patterns("__pycache__")
        for part in ("scripts", "profiles"):
            shutil.copytree(os.path.join(GENOME, part), os.path.join(checkout, "genome", part), ignore=ignore)
        evolve_home = os.path.join(self.tmp, "evolve-default")
        r = self.bench_build(os.path.join(checkout, "genome", "scripts", "evolve.py"), evolve_home)
        self.assert_refusals_read_through(log, r)

    def test_evolve_json_regista_checkout_overrides_the_default(self):  # covers: SEP-2
        checkout = os.path.join(self.tmp, "other-checkout")
        log = os.path.join(self.tmp, "other-calls.jsonl")
        fake_checkout(checkout, log)
        evolve_home = os.path.join(self.tmp, "evolve-other")
        os.makedirs(evolve_home)
        with open(os.path.join(evolve_home, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"regista_checkout": os.path.abspath(checkout)}, f)
        r = self.bench_build(os.path.join(GENOME, "scripts", "evolve.py"), evolve_home)
        self.assert_refusals_read_through(log, r)


# SEP-5: the genome's tests are their own gates ------------------------------------------------------------------------

def start_folder(command):
    """The start folder of a `python3 -m unittest discover` command (-s, --start-directory or the first positional
    argument; '.' when none is given), relative to the checkout; None when the command doesn't run discover."""
    words = shlex.split(command)
    if "discover" not in words:
        return None
    rest = words[words.index("discover") + 1:]
    start, positional, i = None, [], 0
    while i < len(rest):
        word = rest[i]
        if word in ("-s", "--start-directory") and i + 1 < len(rest):
            start = rest[i + 1]
            i += 2
            continue
        if word.startswith("--start-directory="):
            start = word.split("=", 1)[1]
        elif word in ("-p", "--pattern", "-t", "--top-level-directory") and i + 1 < len(rest):
            i += 2
            continue
        elif not word.startswith("-"):
            positional.append(word)
        i += 1
    if start is None:
        start = positional[0] if positional else "."
    return os.path.normpath(start)


class GenomeGates(unittest.TestCase):
    """The project's config: genome-tests and genome-e2e run the genome's tests; tests and e2e stay out of genome/."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REGISTA_CHECKOUT, ".regista", "config.json"), encoding="utf-8") as f:
            cls.gates = {g.get("name"): g for g in json.load(f).get("gates", [])}

    def gate(self, name):
        self.assertIn(name, self.gates, f"no gate named {name} in .regista/config.json")
        gate = self.gates[name]
        self.assertIn("commit", gate.get("when", []), f"the {name} gate runs on commit")
        return gate

    def test_genome_tests_gate_runs_the_genome_unit_tests(self):  # covers: SEP-5
        gate = self.gate("genome-tests")
        self.assertEqual(start_folder(gate["run"]), os.path.join("genome", "tests"),
                         f"genome-tests discovers from genome/tests: {gate['run']}")

    def test_genome_e2e_gate_runs_the_genome_end_to_end_tests(self):  # covers: SEP-5
        gate = self.gate("genome-e2e")
        self.assertEqual(start_folder(gate["run"]), os.path.join("genome", "tests", "e2e"),
                         f"genome-e2e discovers from genome/tests/e2e: {gate['run']}")
        self.assertEqual(gate.get("evidence"), "e2e", "genome-e2e is an end-to-end gate")
        self.assertIn("genome/tests/e2e/**", gate.get("tests", []), "genome-e2e's tests are genome/tests/e2e/**")

    def test_regista_tests_and_e2e_gates_do_not_reach_the_genome(self):  # covers: SEP-5
        for name, folder in (("tests", "tests"), ("e2e", os.path.join("tests", "e2e"))):
            gate = self.gate(name)
            start = start_folder(gate["run"])
            self.assertEqual(start, folder, f"the {name} gate discovers from {folder}: {gate['run']}")
            self.assertFalse(inside(os.path.join(REGISTA_CHECKOUT, "genome"), os.path.join(REGISTA_CHECKOUT, start)),
                             f"the {name} gate's start folder holds genome/")
            self.assertNotIn("genome", gate["run"], f"the {name} gate's command names the genome: {gate['run']}")
            self.assertFalse(any("genome" in glob for glob in gate.get("tests", [])),
                             f"the {name} gate's test globs reach genome/: {gate.get('tests')}")


if __name__ == "__main__":
    unittest.main()
