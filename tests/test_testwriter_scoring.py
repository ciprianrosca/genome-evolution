"""Test-writer scoring without a builder run (spec .regista/specs/testwriter-scoring.json).

`evolve.py replay <case> --genome current --role test-writer` starts only the test writer (its prompt from the
genome's agents/test-writer.md) in a clone at the case's parent commit, then scores the tests it wrote, with no
builder run:
- pass_finishing: the tests pass on a second clone at the case's finishing commit (the commit gates run there);
- fail_parent: the tests fail on the parent commit;
- coverage: the share of the case's scoring IDs marked `covers:` in the tests;
- bugs_caught: the share of planted-bug patches (bench/<project>/<case>/bugs/<ID>.patch, applied to the
  finishing commit) on which the tests fail; with no patches the axis is reported missing, never passed.

Here `claude` is a stand-in script (evolve.json `claude_command`) that answers a version probe with
"2.1.312 (Claude Code)" without logging it, and otherwise records its arguments (and the contents
of any argument naming a file), and in its test-writer scenario writes the test files listed in the control
file tests.json into its working folder (the clone), stages them, emits a result and exits.
Real replays need Claude Code's sandbox (REP-6), so every world puts stand-in `bwrap` and `socat` first on
PATH; the stand-in bwrap really runs the command it wraps (the evolver runs the candidate's tests through it).

The fixture project has one step, m1-math, with two requirements: MATH-1 (double) and MATH-2 (triple). The
parent commit has no functions yet; the finishing commit implements both and adds the case's own tests. The
planted bugs turn `2 * n` into `2 + n` (MATH-1) and `3 * n` into `3 + n` (MATH-2). The case's own tests sit in
the gate's folder (so the case is replayable); scoring must judge the candidate's tests, so a weak candidate test
must not get credit for bugs the case's own tests would catch.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_testwriter_scoring -v"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
REGISTA_CHECKOUT = os.path.dirname(GENOME)  # the Regista checkout that holds the genome: reached only by path
if os.path.join(GENOME, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(GENOME, "tests"))
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
PY = sys.executable
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
PROJECT = "mathy"
STEP = "m1-math"
IDS = ["MATH-1", "MATH-2"]

STANDIN = r'''#!@PYTHON@
import json, os, re, subprocess, sys, time, uuid
CONTROL = @CONTROL@

def log(name, entry):
    with open(os.path.join(CONTROL, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def emit(event):
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except Exception:
        pass

def read(name, default=""):
    try:
        with open(os.path.join(CONTROL, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

# A version probe (the evolver checks Claude Code's version before a real run): answer it, log nothing, write nothing.
if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v") for a in args):
    print("2.1.312 (Claude Code)")
    sys.exit(0)

files = {}
for a in args:
    try:
        if len(a) < 4096 and os.path.isfile(a):
            with open(a, encoding="utf-8") as f:
                files[a] = f.read()
    except (OSError, UnicodeDecodeError, ValueError):
        pass
cwd = os.getcwd()
sid = opt("--session-id") or str(uuid.uuid4())
def git(*a):
    return subprocess.run(["git", "-c", "user.name=Stand In", "-c", "user.email=standin@example.com",
                           "-c", "commit.gpgsign=false", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
scenario = read("scenario", "test-writer")
log("calls.jsonl", {"argv": args, "files": files, "cwd": cwd, "scenario": scenario, "time": time.time(),
                    "head": git("rev-parse", "HEAD")})

usage = {"input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000}
emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": opt("--model"),
      "apiKeySource": "none", "plugins": [{"name": "regista", "path": opt("--plugin-dir")}], "plugin_errors": []})
msg = {"type": "assistant", "session_id": sid, "sessionId": sid, "uuid": str(uuid.uuid4()),
       "message": {"id": "msg_tw", "type": "message", "role": "assistant", "model": "claude-stand-in",
                   "content": [{"type": "text", "text": "Writing the tests."}], "usage": usage}}
slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
folder = os.path.join(os.path.expanduser("~"), ".claude", "projects", slug)
os.makedirs(folder, exist_ok=True)
with open(os.path.join(folder, sid + ".jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"type": "user", "sessionId": sid, "message": {"role": "user", "content": "brief"}}) + "\n")
    f.write(json.dumps(msg) + "\n")
emit(msg)

if scenario == "test-writer":
    tests = json.loads(read("tests.json", "{}"))
    for rel, text in tests.items():
        path = os.path.join(cwd, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        git("add", rel)
    log("wrote.jsonl", {"files": sorted(tests)})
    # Control file `hard`: also break two rules that are hard failures (a protected-path write and a real
    # no_verify refusal in hooks.log).
    if read("hard"):
        with open(os.path.join(cwd, ".env"), "w", encoding="utf-8") as f:
            f.write("API_TOKEN=planted\n")
        git("add", "-f", ".env")
        os.makedirs(os.path.join(cwd, ".regista"), exist_ok=True)
        with open(os.path.join(cwd, ".regista", "hooks.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": "2026-10-29T10:00:00", "tool": "Bash",
                                "input": {"command": "git commit --no-verify -m tests"},
                                "reason": "Regista: refused by no_verify.", "kind": "deny", "rule": "no_verify",
                                "role": "test-writer", "session": sid, "worker": None, "label": "real",
                                "label_by": "rule"}) + "\n")
        log("hard.jsonl", {"time": time.time()})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "Tests written.",
      "session_id": sid, "duration_ms": 1000, "total_cost_usd": 0, "modelUsage": {}, "usage": usage})
sys.exit(0)
'''

# Stand-in bubblewrap: answers a version probe; otherwise runs the command it wraps for real (no sandbox), applying
# --chdir and the environment options, and records the command and its exit code in control/bwrap.jsonl. The evolver
# runs the candidate's tests through bwrap, so this one must really run them.
BWRAP = r'''#!@PYTHON@
import json, os, subprocess, sys, time
CONTROL = @CONTROL@
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("bubblewrap 0.9.0")
    sys.exit(0)
TWO = {"--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try", "--symlink",
       "--setenv", "--file", "--bind-data", "--ro-bind-data", "--chmod"}
ONE = {"--chdir", "--dev", "--proc", "--tmpfs", "--dir", "--unsetenv", "--remount-ro", "--perms", "--size",
       "--uid", "--gid", "--hostname", "--seccomp", "--info-fd", "--json-status-fd", "--block-fd", "--sync-fd",
       "--userns", "--userns2", "--pidns", "--exec-label", "--file-label", "--lock-file", "--cap-add", "--cap-drop",
       "--args", "--argv0", "--add-seccomp-fd", "--userns-block-fd"}
env, chdir, binds, cmd, i = dict(os.environ), None, [], [], 0
while i < len(args):
    a = args[i]
    if a == "--":
        cmd = args[i + 1:]
        break
    if not a.startswith("-"):
        cmd = args[i:]
        break
    if a in TWO and i + 2 < len(args):
        if a == "--setenv":
            env[args[i + 1]] = args[i + 2]
        else:
            binds.append([a, args[i + 1], args[i + 2]])
        i += 3
    elif a in ONE and i + 1 < len(args):
        if a == "--chdir":
            chdir = args[i + 1]
        elif a == "--unsetenv":
            env.pop(args[i + 1], None)
        else:
            binds.append([a, args[i + 1], None])
        i += 2
    else:
        if a == "--clearenv":
            env = {}
        i += 1
cwd = chdir or os.getcwd()
if not os.path.isdir(cwd):  # a folder bound under another name inside the sandbox: map it back to the host
    for flag, src, dest in binds:
        if dest and flag in ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try") \
                and (cwd == dest or cwd.startswith(dest.rstrip("/") + "/")):
            cwd = src + cwd[len(dest.rstrip("/")):]
            break
code = 0
if cmd:
    try:
        code = subprocess.run(cmd, cwd=cwd if os.path.isdir(cwd) else None, env=env).returncode
    except OSError as e:
        sys.stderr.write("stand-in bwrap: %s\n" % e)
        code = 127
with open(os.path.join(CONTROL, "bwrap.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": cwd, "command": cmd, "exit": code, "time": time.time()}) + "\n")
sys.exit(code)
'''
# Stand-in socat: answers a version probe and otherwise succeeds doing nothing.
SOCAT = ("#!/bin/sh\ncase \"$1\" in -V|-h|--version) echo 'socat by Gerhard Rieger and contributors - see "
         "www.dest-unreach.org'; echo 'socat version 1.8.0.0';; esac\nexit 0\n")

CONFIG = {
    "project": PROJECT,
    "plan": ".regista/plan.md",
    "board_file": ".regista/TODO.md",
    "gates": [{"name": "tests", "run": "python3 -m unittest discover -s tests/unit", "tests": ["tests/unit/**"],
               "when": ["commit"], "timeout": 600}],
    "roles": {"source": ["src/**"], "tests": ["tests/**"]},
    "requirements": {"enforce_coverage": True},
}
INTERFACE = "double(n) and triple(n) in src/mathx.py, both taking and returning an int."
SPEC = {"step": STEP, "milestone": "M1", "title": "Double and triple", "plan_step": "Double and triple (MATH-1, MATH-2)",
        "state": "active", "interface": INTERFACE,
        "requirements": [{"id": "MATH-1", "text": "double(n) SHALL return twice n", "verify": "test"},
                         {"id": "MATH-2", "text": "triple(n) SHALL return three times n", "verify": "test"}]}
IMPL = "VALUE = 1\n\n\ndef double(n):\n    return 2 * n\n\n\ndef triple(n):\n    return 3 * n\n"
BUGS = {"MATH-1": ("return 2 * n", "return 2 + n"), "MATH-2": ("return 3 * n", "return 3 + n")}

HEAD = ('"""Candidate tests."""\nimport os\nimport sys\nimport unittest\n\n'
        "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'src'))\n\n\n")
CASE_TEST = (HEAD + "from mathx import double, triple  # noqa: E402\n\n\n"
             "class CaseTests(unittest.TestCase):\n"
             "    def test_double(self):  # covers: MATH-1\n        self.assertEqual(double(4), 8)\n\n"
             "    def test_triple(self):  # covers: MATH-2\n        self.assertEqual(triple(5), 15)\n")
CANDIDATE = "tests/unit/test_mathx_candidate.py"
# Good: pass on finishing, fail on parent (no functions there), cover both IDs, fail on each planted bug.
GOOD = (HEAD + "import mathx  # noqa: E402\n\n\n"
        "class Good(unittest.TestCase):\n"
        "    def test_double(self):  # covers: MATH-1\n"
        "        self.assertEqual(mathx.double(4), 8)\n        self.assertEqual(mathx.double(-3), -6)\n\n"
        "    def test_triple(self):  # covers: MATH-2\n"
        "        self.assertEqual(mathx.triple(5), 15)\n        self.assertEqual(mathx.triple(0), 0)\n")
# Existence only: fails on the parent (no functions yet), passes on finishing, covers both, catches no bug.
EXISTS_ONLY = (HEAD + "import mathx  # noqa: E402\n\n\n"
               "class ExistsOnly(unittest.TestCase):\n"
               "    def test_double_exists(self):  # covers: MATH-1\n"
               "        self.assertTrue(callable(getattr(mathx, 'double', None)))\n\n"
               "    def test_triple_exists(self):  # covers: MATH-2\n"
               "        self.assertTrue(callable(getattr(mathx, 'triple', None)))\n")
# Passes on the parent too: only checks the module loads.
PASSES_ON_PARENT = (HEAD + "import mathx  # noqa: E402\n\n\n"
                    "class Loads(unittest.TestCase):\n"
                    "    def test_module_loads(self):  # covers: MATH-1, MATH-2\n"
                    "        self.assertEqual(mathx.VALUE, 1)\n")
# Only MATH-1: half the IDs marked, catches only the MATH-1 bug.
HALF = (HEAD + "import mathx  # noqa: E402\n\n\n"
        "class Half(unittest.TestCase):\n"
        "    def test_double(self):  # covers: MATH-1\n"
        "        self.assertEqual(mathx.double(4), 8)\n")
# Change detection only (BENCH-11): passes while no file outside tests/ differs from the commit (git diff, when the
# copy has a .git folder) or, without git, while no such file is newer than this test file; it never calls double or
# triple. Each run appends what it saw (a .git folder? the watched files' modification times, src/mathx.py) to
# control/seen.jsonl.
WATCHED = [".gitignore", ".regista/config.json", ".regista/plan.md", f".regista/specs/{STEP}.json", "src/mathx.py",
           CANDIDATE]
CHANGE_ONLY = '''"""Candidate tests that only notice whether files changed."""
import json
import os
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SEEN = os.path.join(@CONTROL@, "seen.jsonl")
WATCHED = @WATCHED@


def record():
    entry = {"root": ROOT, "git": os.path.exists(os.path.join(ROOT, ".git")), "mtimes": {}, "src": None}
    for rel in WATCHED:
        path = os.path.join(ROOT, *rel.split("/"))
        if os.path.isfile(path):
            entry["mtimes"][rel] = os.stat(path).st_mtime_ns
    try:
        with open(os.path.join(ROOT, "src", "mathx.py"), encoding="utf-8") as f:
            entry["src"] = f.read()
    except OSError:
        pass
    try:
        with open(SEEN, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\\n")
    except OSError:
        pass


def unchanged():
    if os.path.exists(os.path.join(ROOT, ".git")):
        r = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", ".", ":!tests"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    mine = os.stat(os.path.abspath(__file__)).st_mtime_ns
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".regista", "tests", "__pycache__")]
        for name in names:
            if os.stat(os.path.join(folder, name)).st_mtime_ns > mine:
                return False
    return True


class FilesUnchanged(unittest.TestCase):
    def test_double(self):  # covers: MATH-1
        record()
        self.assertTrue(unchanged(), "a file outside tests/ changed")

    def test_triple(self):  # covers: MATH-2
        self.assertTrue(unchanged(), "a file outside tests/ changed")
'''.replace("@WATCHED@", repr(WATCHED))

# Output probe (BENCH-11): when its module loads, before writing anything, it reads its own stdout and stderr through
# /proc/self/fd/1 and /proc/self/fd/2 (only when they are regular files: reading a pipe would block or steal output)
# and counts the marker that earlier checks printed there; then it prints the marker to both. Its tests pass when it
# found no marker and fail otherwise, and never call double or triple. Each check appends what it saw (the count, what
# kind of file each descriptor was, src/mathx.py) to control/probe.jsonl, in the order the checks ran.
PROBE_MARK = "tw-probe-mark-4d1f"
OUTPUT_PROBE = '''"""Candidate tests that look for earlier checks' output in their own stdout and stderr."""
import json
import os
import stat
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROBE = os.path.join(@CONTROL@, "probe.jsonl")
MARK = @MARK@


def earlier_checks():
    found, kinds = 0, {}
    for fd in (1, 2):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            kinds[fd] = "closed"
            continue
        kinds[fd] = "file" if stat.S_ISREG(mode) else "other"
        if stat.S_ISREG(mode):
            try:
                with open("/proc/self/fd/%d" % fd, encoding="utf-8", errors="replace") as f:
                    found += f.read().count(MARK)
            except OSError:
                pass
    return found, kinds


FOUND, KINDS = earlier_checks()
try:
    with open(os.path.join(ROOT, "src", "mathx.py"), encoding="utf-8") as f:
        SRC = f.read()
except OSError:
    SRC = None
with open(PROBE, "a", encoding="utf-8") as f:
    f.write(json.dumps({"found": FOUND, "kinds": KINDS, "src": SRC, "pid": os.getpid()}) + "\\n")
for fd in (1, 2):
    try:
        os.write(fd, (MARK + "\\n").encode())
    except OSError:
        pass


class NoEarlierChecks(unittest.TestCase):
    def test_double(self):  # covers: MATH-1
        self.assertEqual(FOUND, 0, "earlier checks' output is in this one's")

    def test_triple(self):  # covers: MATH-2
        self.assertEqual(FOUND, 0, "earlier checks' output is in this one's")
'''.replace("@MARK@", repr(PROBE_MARK))
# Listing probe (BENCH-12): the stand-in writes the candidate and then three helper files out of name order; in each
# check the candidate appends the order os.listdir gives for its own folder to control/listing.jsonl.
LISTING_PROBE = {
    CANDIDATE: '''"""Candidate tests that record how their folder is listed."""
import json
import os
import unittest

with open(os.path.join(@CONTROL@, "listing.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps([n for n in os.listdir(os.path.dirname(os.path.abspath(__file__))) if n != "__pycache__"])
            + "\\n")


class Listed(unittest.TestCase):
    def test_listed(self):  # covers: MATH-1
        pass
''',
    **{f"tests/unit/{name}_helper.py": "HELPER = 1\n" for name in ("zz", "aa", "mm")},
}

CHECKS = {IMPL: "finishing", "VALUE = 1\n": "parent",
          **{IMPL.replace(good, bad): rid for rid, (good, bad) in BUGS.items()}}


def check_name(item):
    """Which check one entry of a recorded order names: finishing, parent or a planted bug's ID (None if unclear)."""
    text = json.dumps(item) if not isinstance(item, str) else item
    hits = [rid for rid in BUGS if rid in text]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return None
    low = text.lower()
    if "finish" in low and "parent" not in low:
        return "finishing"
    if "parent" in low and "finish" not in low:
        return "parent"
    return None


def recorded_orders(obj, path="result"):
    """Every list inside result.json whose entries name the four checks once each: (where, [names])."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += recorded_orders(v, f"{path}.{k}")
    elif isinstance(obj, list):
        names = [check_name(x) for x in obj]
        if len(obj) == len(CHECKS) and sorted(n or "" for n in names) == sorted(CHECKS.values()):
            found.append((path, names))
        for i, v in enumerate(obj):
            found += recorded_orders(v, f"{path}[{i}]")
    return found


def check_order(test, result):
    orders = recorded_orders(result)
    test.assertTrue(orders, "result.json records the order the checks ran in (a list naming finishing, parent, "
                            f"MATH-1 and MATH-2 once each): {json.dumps(result)[:3000]}")
    preferred = [o for o in orders if "order" in o[0].lower()]
    return (preferred or orders)[0]


# A project with two commit gates (BENCH-11): a lint gate with no test globs, listed first, that fails on every
# planted-bug copy (it looks for " + n" in src/mathx.py), and the usual tests gate.
LINT_SCRIPT = ("import sys\n\nwith open('src/mathx.py', encoding='utf-8') as f:\n    text = f.read()\n"
               "sys.exit(1 if ' + n' in text else 0)\n")
LINT_CONFIG = dict(CONFIG, gates=[{"name": "lint", "run": "python3 tools/lint.py", "when": ["commit"], "timeout": 600},
                                  CONFIG["gates"][0]],
                   roles={"source": ["src/**", "tools/**"], "tests": ["tests/**"]})


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Sample", "-c", "user.email=sample@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, **GIT_ENV))
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build_project(root, config=None, extra_files=None):
    """The fixture project; returns the parent commit and the planted-bug patches against the finishing commit."""
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    files = {".gitignore": ".regista/hooks.log\n.regista/gates/\n.regista/state/\n__pycache__/\n",
             ".regista/config.json": json.dumps(config or CONFIG, indent=1) + "\n",
             ".regista/plan.md": "# Mathy: plan\n\n## M1 — Numbers\n- [x] Double and triple (MATH-1, MATH-2)\n",
             "src/mathx.py": "VALUE = 1\n", **(extra_files or {})}
    for rel, text in files.items():
        write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "setup")
    write(root, f".regista/specs/{STEP}.json", json.dumps(SPEC, indent=1) + "\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "math: spec")
    parent = git(root, "rev-parse", "HEAD").strip()
    write(root, "src/mathx.py", IMPL)
    write(root, "tests/unit/test_mathx.py", CASE_TEST)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "math: double and triple with tests")
    patches = {}
    for rid, (good, bad) in BUGS.items():
        write(root, "src/mathx.py", IMPL.replace(good, bad))
        patches[rid] = git(root, "diff", "--no-color", "--no-ext-diff")
        git(root, "checkout", "-q", "--", "src/mathx.py")
    return parent, patches


def base_env(home, evolve_home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "REGISTA_", "DAEDALUS_", "CLAUDE_CODE_"))
           and k not in ("CLAUDECODE", "CLAUDE_PROJECT_DIR")}
    env.update({"HOME": home, "REGISTA_EVOLVE_HOME": evolve_home, "PYTHONDONTWRITEBYTECODE": "1", **GIT_ENV})
    return env


_TEMPLATE = {}


def build_bench(prefix, config=None, extra_files=None):
    """A fixture project and its bench (built by `evolve.py bench build`) in a new temporary folder."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    tpl = {"tmp": tmp}
    project = os.path.join(tmp, "projects", PROJECT)
    tpl["parent"], tpl["patches"] = build_project(project, config, extra_files)
    home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve")
    os.makedirs(home)
    os.makedirs(evo)
    with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
        json.dump({"holdout_pct": 0}, f)
    r = subprocess.run([PY, EVOLVE, "bench", "build", project], cwd=tmp, capture_output=True, text=True,
                       encoding="utf-8", env=base_env(home, evo), timeout=300)
    tpl["build"] = r
    tpl["evolve"] = evo
    tpl["case"] = None
    for path in glob.glob(os.path.join(evo, "bench", PROJECT, "*.json")):
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        if case.get("step") == STEP:
            tpl["case"] = (os.path.basename(path)[:-5], case)
    return tpl


def setUpModule():
    _TEMPLATE.update(build_bench("regista-twscore-tpl-"))


def tearDownModule():
    shutil.rmtree(_TEMPLATE.get("tmp", ""), ignore_errors=True)


class World:
    """One test-writer replay: a copy of the bench (with or without planted bugs), the stand-in and its tests."""

    def __init__(self, tests, bugs=True, template=None, evolve=None, hard=False):
        """template: the bench to copy (default: the module's); evolve: extra evolve.json settings; hard: the
        stand-in also breaks rules that are hard failures. `@CONTROL@` in the tests becomes the control folder."""
        template = template or _TEMPLATE
        self.tmp = tempfile.mkdtemp(prefix="regista-twscore-")
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve")
        self.control = os.path.join(self.tmp, "control")
        for d in (self.home, self.control):
            os.makedirs(d)
        shutil.copytree(template["evolve"], self.evo)
        self.case, self.case_data = template["case"]
        if bugs:
            folder = os.path.join(self.evo, "bench", PROJECT, self.case, "bugs")
            os.makedirs(folder, exist_ok=True)
            for rid, patch in template["patches"].items():
                with open(os.path.join(folder, rid + ".patch"), "w", encoding="utf-8") as f:
                    f.write(patch)
        self.claude = os.path.join(self.control, "claude")
        with open(self.claude, "w", encoding="utf-8") as f:
            f.write(STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control)))
        os.chmod(self.claude, 0o755)
        # Claude Code's sandbox runtime (REP-6): stand-in bwrap and socat first on PATH.
        self.bin = os.path.join(self.tmp, "sandbox-bin")
        os.makedirs(self.bin)
        for name, text in (("bwrap", BWRAP.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))),
                           ("socat", SOCAT)):
            with open(os.path.join(self.bin, name), "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(os.path.join(self.bin, name), 0o755)
        with open(os.path.join(self.control, "scenario"), "w", encoding="utf-8") as f:
            f.write("test-writer")
        with open(os.path.join(self.control, "tests.json"), "w", encoding="utf-8") as f:
            files = tests if isinstance(tests, dict) else {CANDIDATE: tests}
            json.dump({rel: text.replace("@CONTROL@", repr(self.control)) for rel, text in files.items()}, f)
        if hard:
            with open(os.path.join(self.control, "hard"), "w", encoding="utf-8") as f:
                f.write("yes")
        with open(os.path.join(self.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 0, "claude_command": self.claude, "task_model": "haiku",
                       "replay_timeout_minutes": 5, **(evolve or {})}, f)

    def env(self):
        env = base_env(self.home, self.evo)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        return env

    def run(self, timeout=400):
        start = time.time()
        r = subprocess.run([PY, EVOLVE, "replay", self.case, "--genome", "current", "--role", "test-writer"],
                           cwd=self.tmp, capture_output=True, text=True, encoding="utf-8",
                           env=self.env(), timeout=timeout)
        r.seconds = time.time() - start
        self.r = r
        return r

    def lines(self, name):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def result(self, test):
        found = sorted(glob.glob(os.path.join(self.evo, "runs", "**", "result.json"), recursive=True))
        test.assertEqual(len(found), 1, f"expected one result.json under runs/: {found}\n"
                                        f"{self.r.stdout}\n{self.r.stderr}")
        with open(found[0], encoding="utf-8") as f:
            return json.load(f)

    def cleanup(self):
        subprocess.run(["chmod", "-R", "u+w", self.tmp], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def axis(test, result, name):
    """The entry for one axis in result.json's scores (a dict with value, or a bare value)."""
    scores = result.get("scores")
    test.assertIsInstance(scores, dict, f"result.json has no scores: {result}")
    test.assertIn(name, scores, f"the {name} axis is reported: {sorted(scores)}")
    return scores[name]


def value(entry):
    return entry.get("value") if isinstance(entry, dict) else entry


def normalize(text):
    return " ".join(str(text).split())


def strings(obj):
    """Every string inside a JSON value (an --agents argument carries the prompt JSON-escaped)."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def prompt_text(call):
    """All the text the stand-in received: its arguments, JSON inside them, and files they name."""
    parts = list(call["argv"]) + list(call.get("files", {}).values())
    for a in call["argv"]:
        try:
            parts += strings(json.loads(a))
        except ValueError:
            pass
    return normalize("\n".join(parts))


def agent_body(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return normalize(text)[:300]


def class_world(cls, tests, bugs=True, template=None, **options):
    cls.w = None
    template = template or _TEMPLATE
    if template["build"].returncode != 0 or not template["case"] or not template["case"][1].get("replayable"):
        return
    cls.w = World(tests, bugs, template, **options)
    cls.w.run()


class Base(unittest.TestCase):
    tests = GOOD
    bugs = True
    config = None  # None: the module's bench; otherwise a project config for a bench of the class's own
    extra_files = None  # files added to that project's first commit
    world_options = {}  # World's evolve and hard options
    exit_zero = True  # whether the replay must exit 0

    @classmethod
    def setUpClass(cls):
        cls.tpl = _TEMPLATE
        if cls.config is not None:
            cls.tpl = build_bench("regista-twscore-own-", cls.config, cls.extra_files)
        class_world(cls, cls.tests, cls.bugs, cls.tpl, **cls.world_options)

    @classmethod
    def tearDownClass(cls):
        if cls.w:
            cls.w.cleanup()
        if cls.tpl is not _TEMPLATE:
            shutil.rmtree(cls.tpl.get("tmp", ""), ignore_errors=True)

    def setUp(self):
        tpl = self.tpl
        build = tpl["build"]
        self.assertEqual(build.returncode, 0, f"bench build failed:\n{build.stdout}\n{build.stderr}")
        self.assertIsNotNone(tpl["case"], "bench build made no case for the fixture step")
        self.assertTrue(tpl["case"][1].get("replayable"),
                        f"the fixture case should be replayable: {tpl['case'][1].get('reasons')}")
        self.assertEqual(sorted(tpl["case"][1].get("scoring_ids", [])), IDS)
        out = self.w.r.stdout + self.w.r.stderr
        self.assertNotIn("Traceback", out)
        if self.exit_zero:
            self.assertEqual(self.w.r.returncode, 0, out)
        self.assertTrue(self.w.lines("wrote.jsonl"), f"the stand-in test writer never wrote its tests:\n{out}")
        self.result = self.w.result(self)


class GoodTests(Base):
    """Good tests with a planted bug per scoring ID."""

    def test_runs_only_the_test_writer_with_its_genome_prompt_and_no_builder(self):  # covers: BENCH-5
        calls = self.w.lines("calls.jsonl")
        self.assertEqual(len(calls), 1, f"only the test writer starts, once; no builder run: {calls}")
        call = calls[0]
        self.assertEqual(call["head"], _TEMPLATE["parent"], "the test writer works at the case's parent commit")
        argv = call["argv"]
        plugin = argv[argv.index("--plugin-dir") + 1] if "--plugin-dir" in argv else None
        self.assertTrue(plugin and os.path.samefile(plugin, REGISTA_CHECKOUT), f"genome 'current' is this checkout: {argv}")
        seen = prompt_text(call)
        tw = os.path.join(REGISTA_CHECKOUT, "agents", "test-writer.md")
        self.assertTrue(os.path.isfile(tw), "the genome has agents/test-writer.md")
        agent_flag = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
        self.assertTrue(agent_body(tw) in seen or agent_flag.endswith("test-writer"),
                        "the run uses the test writer's prompt from the genome's agents/test-writer.md")
        self.assertIn(normalize(INTERFACE), seen, "the test writer gets the step's public interface")
        for path in glob.glob(os.path.join(REGISTA_CHECKOUT, "agents", "*.md")):
            if "build" in os.path.basename(path):
                self.assertNotIn(agent_body(path), seen, f"no builder prompt ({os.path.basename(path)})")
                self.assertFalse(agent_flag.endswith(os.path.basename(path)[:-3]), "no builder agent")

    def test_good_tests_pass_on_finishing_and_fail_on_parent(self):  # covers: BENCH-5
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True, self.result.get("scores"))
        self.assertIs(value(axis(self, self.result, "fail_parent")), True, self.result.get("scores"))

    def test_good_tests_cover_every_id_and_catch_every_planted_bug(self):  # covers: BENCH-7
        self.assertAlmostEqual(float(value(axis(self, self.result, "coverage"))), 1.0)
        self.assertAlmostEqual(float(value(axis(self, self.result, "bugs_caught"))), 1.0)


class ExistenceOnlyTests(Base):
    """Tests that only check the functions exist: they pass on finishing and fail on parent, but catch no bug."""
    tests = EXISTS_ONLY

    def test_existence_only_scores_low_on_bugs_caught(self):  # covers: BENCH-7
        caught = float(value(axis(self, self.result, "bugs_caught")))
        self.assertLess(caught, 0.5, "the case's own tests must not earn the candidate credit for planted bugs")
        self.assertAlmostEqual(float(value(axis(self, self.result, "coverage"))), 1.0, msg="both IDs are marked")
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True)


class TestsPassingOnParent(Base):
    """Tests that pass on the parent commit as well prove nothing about the step."""
    tests = PASSES_ON_PARENT

    def test_tests_passing_on_parent_score_fail_parent_false(self):  # covers: BENCH-5
        self.assertIs(value(axis(self, self.result, "fail_parent")), False, self.result.get("scores"))
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True, self.result.get("scores"))


class HalfTheIds(Base):
    """Tests for MATH-1 only: half the scoring IDs marked, half the planted bugs caught."""
    tests = HALF

    def test_coverage_and_bugs_caught_are_shares(self):  # covers: BENCH-7
        self.assertAlmostEqual(float(value(axis(self, self.result, "coverage"))), 0.5)
        self.assertAlmostEqual(float(value(axis(self, self.result, "bugs_caught"))), 0.5)


class NoPlantedBugs(Base):
    """Good tests, but the case has no bugs/ folder."""
    bugs = False

    def test_bugs_caught_is_reported_missing_never_passed(self):  # covers: BENCH-7
        entry = axis(self, self.result, "bugs_caught")
        v = value(entry)
        self.assertTrue(v is None or v == "missing", f"no patches means no value, not a score: {entry!r}")
        self.assertRegex(json.dumps(entry), re.compile(r"missing", re.I), "the axis says it is missing")
        if isinstance(entry, dict):
            self.assertIsNot(entry.get("passed"), True, entry)
        self.assertAlmostEqual(float(value(axis(self, self.result, "coverage"))), 1.0)
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True)


class ChangeDetectionOnly(Base):
    """BENCH-11: tests that only notice changed files must not tell the copies apart."""
    tests = CHANGE_ONLY

    def test_tests_that_only_detect_changed_files_catch_no_planted_bug(self):  # covers: BENCH-11
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True, self.result.get("scores"))
        caught = value(axis(self, self.result, "bugs_caught"))
        self.assertAlmostEqual(float(caught), 0.0, msg="the copies differ only in content; git state and "
                                                      f"timestamps must not give the bugs away: {self.result}")

    def test_every_copy_is_plain_with_one_modification_time(self):  # covers: BENCH-11
        seen = self.w.lines("seen.jsonl")
        self.assertTrue(seen, "the candidate's tests never recorded what they saw (did they run?)")
        sources = [entry["src"] for entry in seen]
        expected = {"finishing commit": IMPL, "parent commit": "VALUE = 1\n"}
        for rid, (good, bad) in BUGS.items():
            expected[f"planted bug {rid}"] = IMPL.replace(good, bad)
        for name, text in expected.items():
            self.assertIn(text, sources, f"the candidate's tests never ran on the {name}")
        for entry in seen:
            self.assertFalse(entry["git"], f"a copy has a .git folder: {entry['root']}")
            self.assertIn("src/mathx.py", entry["mtimes"], entry)
            self.assertIn(CANDIDATE, entry["mtimes"], entry)
            self.assertEqual(len(set(entry["mtimes"].values())), 1,
                             f"every file in a copy has the same modification time: {entry}")


class ListingOrder(Base):
    """BENCH-12: plain copies create their entries in name order, so every check lists a folder the same way."""
    tests = LISTING_PROBE

    @classmethod
    def setUpClass(cls):
        # On ext4 a folder lists in hash order whatever the creation order; tmpfs lists newest first, so the
        # creation order shows. Build this world on tmpfs when there is one.
        saved = tempfile.tempdir
        if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
            tempfile.tempdir = "/dev/shm"
        try:
            super().setUpClass()
        finally:
            tempfile.tempdir = saved

    def test_plain_copies_list_folder_entries_as_if_created_in_name_order(self):  # covers: BENCH-12
        listings = [tuple(x) for x in self.w.lines("listing.jsonl")]
        self.assertTrue(listings, "the candidate's tests never recorded a listing (did they run?)")
        for n, listing in enumerate(sorted(set(listings))):
            reference = os.path.join(self.w.tmp, f"name-order-{n}")
            os.makedirs(reference)
            for name in sorted(listing):
                with open(os.path.join(reference, name), "w", encoding="utf-8") as f:
                    f.write("")
            self.assertEqual(listing, tuple(os.listdir(reference)),
                             "a check lists the copy's folder in the order a folder created in name order gives")



def load_evolve():
    spec = importlib.util.spec_from_file_location("evolve_under_test", EVOLVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK), "no writable /dev/shm")
class PlainCopyOrder(unittest.TestCase):
    """BENCH-12: evolve.plain_copy(src, dst) creates the folders and files of the copy sorted by name. tmpfs lists a
    folder in creation order (newest first), so each folder of the copy must list exactly like a folder whose
    entries were created in name order on the same filesystem."""

    def test_plain_copy_creates_folders_and_files_in_name_order(self):  # covers: BENCH-12
        evolve = load_evolve()
        root = tempfile.mkdtemp(prefix="regista-plain-copy-", dir="/dev/shm")
        self.addCleanup(shutil.rmtree, root, True)
        src, dst = os.path.join(root, "src"), os.path.join(root, "copy")
        tree = {"": ["m.txt", "zeta", "b.txt", "alpha", "k.txt"],
                "zeta": ["y.txt", "c.txt", "inner", "q.txt"],
                "zeta/inner": ["2.txt", "9.txt", "1.txt"],
                "alpha": ["x.txt", "a.txt", "m"],
                "alpha/m": ["z.txt", "b.txt"]}
        os.makedirs(src)
        for folder, names in tree.items():  # parents first, each folder's entries out of name order
            for name in names:
                path = os.path.join(src, folder, name)
                if os.path.join(folder, name) in tree:
                    os.makedirs(path)
                else:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(name)
        reference = os.path.join(root, "reference")
        os.makedirs(reference)
        for name in sorted(tree[""]):
            open(os.path.join(reference, name), "w").close()
        name_order = os.listdir(reference)
        self.assertNotEqual(os.listdir(src), name_order,
                            "the source tree should list out of name order (otherwise this test proves nothing)")

        evolve.plain_copy(src, dst)

        for folder, names in tree.items():
            here = os.path.join(dst, folder) if folder else dst
            self.assertTrue(os.path.isdir(here), f"plain_copy did not copy the folder {folder or '.'}")
            ref = os.path.join(root, "ref-" + (folder.replace("/", "-") or "root"))
            os.makedirs(ref)
            for name in sorted(names):
                open(os.path.join(ref, name), "w").close()
            self.assertEqual(os.listdir(here), os.listdir(ref),
                             f"the copy's folder {folder or '.'} lists as if its entries were created in name order")


class OutputProbe(Base):
    """BENCH-11: tests that read their own stdout and stderr must find no earlier check's output there."""
    tests = OUTPUT_PROBE
    reruns = 5  # extra replays for the shuffle; with four checks, six identical orders by chance is about 1 in 8 million

    def probes(self, world):
        seen = world.lines("probe.jsonl")
        self.assertTrue(seen, "the candidate's tests never recorded what they saw (did they run?)")
        return seen

    def test_reading_earlier_checks_output_catches_no_bug_and_proves_nothing(self):  # covers: BENCH-11
        seen = self.probes(self.w)
        ran = {CHECKS.get(e["src"]) for e in seen}
        self.assertEqual(ran, set(CHECKS.values()), f"the probe ran on every copy: {seen}")
        self.assertEqual([e for e in seen if e["found"]], [],
                         "each check writes to a fresh, empty destination: no earlier check's output in it")
        scores = self.result.get("scores")
        self.assertAlmostEqual(float(value(axis(self, self.result, "bugs_caught"))), 0.0,
                               msg=f"the probe never calls double or triple: {scores}")
        both = (value(axis(self, self.result, "pass_finishing")) is True
                and value(axis(self, self.result, "fail_parent")) is True)
        self.assertFalse(both, f"a probe of shared output must not pass on finishing and fail on parent: {scores}")

    def test_check_order_is_recorded_used_and_shuffled_per_replay(self):  # covers: BENCH-11
        where, order = check_order(self, self.result)
        ran = [CHECKS.get(e["src"]) for e in self.probes(self.w)]
        self.assertEqual(ran, order, f"the checks ran in the order {where} records")
        orders = [tuple(order)]
        for _ in range(self.reruns):
            w = World(self.tests, True, self.tpl)
            try:
                w.run()
                self.assertEqual(w.r.returncode, 0, w.r.stdout + w.r.stderr)
                _, again = check_order(self, w.result(self))
                self.assertEqual([CHECKS.get(e["src"]) for e in self.probes(w)], again,
                                 "the checks ran in the recorded order")
                orders.append(tuple(again))
            finally:
                w.cleanup()
        self.assertGreater(len(set(orders)), 1, f"the check order is shuffled per replay: {orders}")


class LintGateWithoutTestGlobs(Base):
    """BENCH-11: of two commit gates, only the one whose test globs match the candidate's files runs."""
    tests = EXISTS_ONLY
    config = LINT_CONFIG
    extra_files = {"tools/lint.py": LINT_SCRIPT}

    def test_only_the_tests_gate_runs_so_the_lint_gate_catches_no_bug(self):  # covers: BENCH-11
        self.assertIs(value(axis(self, self.result, "pass_finishing")), True, self.result.get("scores"))
        self.assertIs(value(axis(self, self.result, "fail_parent")), True, self.result.get("scores"))
        caught = value(axis(self, self.result, "bugs_caught"))
        self.assertAlmostEqual(float(caught), 0.0, msg="the lint gate fails on every bug copy; it must not run, so "
                                                      f"existence-only tests catch nothing: {self.result}")
        ran = [" ".join(e.get("argv") or []) for e in self.w.lines("bwrap.jsonl")]
        self.assertFalse([c for c in ran if "tools/lint.py" in c], f"the lint gate ran: {ran}")


def settings_of(call):
    """Every --settings object claude got: inline JSON, or a file the stand-in read."""
    found, argv = [], call["argv"]
    for i, a in enumerate(argv[:-1]):
        if a != "--settings":
            continue
        text = call.get("files", {}).get(argv[i + 1], argv[i + 1])
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def permission_rules(call, flags, key):
    """Rules from the CLI flags (comma or space separated, until the next option) and from settings permissions."""
    argv, rules = call["argv"], []
    for i, a in enumerate(argv):
        if a in flags:
            j = i + 1
            while j < len(argv) and not argv[j].startswith("-"):
                rules += [r for r in re.split(r"[,\s]+", argv[j]) if r]
                j += 1
    for s in settings_of(call):
        rules += [r for r in ((s.get("permissions") or {}).get(key) or []) if isinstance(r, str)]
    return [r.strip() for r in rules]


def names_tool(rules, tool):
    return any(r == tool or r.startswith(tool + "(") for r in rules)


CUSTOM_PERMISSIONS = ["--permission-mode", "bypassPermissions",
                      "--allowedTools", "Read,Edit,Write,Bash,Agent,Task,Skill"]


class CustomPermissionsAllowSubagents(Base):
    """BENCH-11: evolve.json's replay_permissions allow Agent, Task and Skill; test-writer mode still blocks them."""
    world_options = {"evolve": {"replay_permissions": CUSTOM_PERMISSIONS}}

    def test_subagents_and_skills_are_blocked_whatever_evolve_json_allows(self):  # covers: BENCH-11
        calls = self.w.lines("calls.jsonl")
        self.assertEqual(len(calls), 1, calls)
        call = calls[0]
        deny = permission_rules(call, ("--disallowedTools", "--disallowed-tools"), "deny")
        allow = permission_rules(call, ("--allowedTools", "--allowed-tools"), "allow")
        argv = call["argv"]
        mode = argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv[:-1] else None
        unchecked = mode == "bypassPermissions" or any("dangerously-skip" in a for a in argv)

        def blocked(tool):
            return names_tool(deny, tool) or (not unchecked and not names_tool(allow, tool))

        for tool in ("Agent", "Skill"):
            self.assertTrue(blocked(tool), f"{tool} is not blocked: deny {deny}, allow {allow}, mode {mode}")
        self.assertTrue(blocked("Task") or names_tool(deny, "Agent"),
                        f"Task (the older name for Agent) is not blocked: deny {deny}, allow {allow}")


def hard_failure_of(result):
    """result.json's hard_failure, at the top level or inside scores."""
    reason = result.get("hard_failure")
    if reason is None and isinstance(result.get("scores"), dict):
        reason = result["scores"].get("hard_failure")
    return reason


class HardFailureInTestWriterMode(Base):
    """BENCH-11: the test writer writes .env (a protected path) and a real no_verify refusal lands in hooks.log."""
    world_options = {"hard": True}
    exit_zero = False

    def test_hard_failure_marks_every_axis_failed_tokens_included(self):  # covers: BENCH-11
        self.assertTrue(self.w.lines("hard.jsonl"), "the stand-in never broke the rules")
        reason = hard_failure_of(self.result)
        self.assertIsInstance(reason, str, f"expected a hard failure: {self.result}")
        scores = self.result.get("scores") or {}
        for name in ("pass_finishing", "fail_parent", "coverage", "bugs_caught", "tokens"):
            entry = scores.get(name)
            self.assertIsInstance(entry, dict, f"the {name} axis is reported with its failed flag: {scores}")
            self.assertIs(entry.get("failed"), True, f"a hard failure fails the {name} axis too: {scores}")
        not_failed = [a for a, e in scores.items() if isinstance(e, dict) and e.get("failed") is not True]
        self.assertEqual(not_failed, [], f"a hard failure fails every axis: {scores}")


if __name__ == "__main__":
    unittest.main()
