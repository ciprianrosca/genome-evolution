"""Helpers for the genome's end-to-end tests: a throwaway sample Python project driven through the real Regista CLI and
the real hooks, each run as a subprocess, the way Claude Code runs them.

Every subprocess gets a temporary home folder and an environment without CLAUDE_PROJECT_DIR (or any other
*_PROJECT_DIR), REGISTA_*, DAEDALUS_* or GIT_* variables, so nothing here can reach this machine's real home
folder or the real project the tests are started from."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# The genome's own copy of Regista's tests/e2e/harness.py: the genome's tests never import Regista's test modules.
# GENOME is the genome folder; REGISTA_CHECKOUT is the Regista checkout the tests use, reached only by path and by
# running its command line as an outside program. It is found the way genome/tests/regista_location.py finds it (this
# is its twin: the end-to-end folder is no package): GENOME_REGISTA_CHECKOUT (an absolute path), else the folder
# holding genome/ when it has scripts/regista.py, else None, and then the tests that need Regista are skipped.
GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SKIP_REASON = "no Regista checkout: set GENOME_REGISTA_CHECKOUT"


def regista_checkout(environ=None):
    """The Regista checkout the tests use (an absolute path), or None when none is found."""
    environ = os.environ if environ is None else environ
    named = (environ.get("GENOME_REGISTA_CHECKOUT") or "").strip()
    if named:
        return os.path.abspath(named)
    around = os.path.dirname(GENOME)
    if os.path.isfile(os.path.join(around, "scripts", "regista.py")):
        return around
    return None


REGISTA_CHECKOUT = regista_checkout()
requires_regista = unittest.skipIf(REGISTA_CHECKOUT is None, SKIP_REASON)
# Regista's command line; without a checkout, a path that doesn't exist (the tests using it are skipped).
TOOL = os.path.join(REGISTA_CHECKOUT or os.path.join(os.path.dirname(GENOME), "no-regista-checkout"),
                    "scripts", "regista.py")
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
PY = sys.executable

CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = (
    "import os\n"
    "import sys\n"
    "import unittest\n\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
    "from calc import add  # noqa: E402\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_adds_two_numbers(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_adds_negative_numbers(self):\n"
    "        self.assertEqual(add(-2, -3), -5)\n"
)


def bash(cmd):
    """The PreToolUse payload Claude Code sends for a Bash command."""
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def decision(answer):
    """The permission decision in a hook answer, or None when the hook let the call through silently."""
    return (answer or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def reason(answer):
    """The reason text in a hook answer (empty when there is none)."""
    return (answer or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "") or ""


class SampleProject:
    """A small Python project in a temporary folder: git repository on `main`, a module calc.py with add(a, b),
    and a unittest file tests/test_calc.py. Call cleanup() (or use it as a context manager) to remove it."""

    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="regista-e2e-")
        self.home = os.path.join(self.base, "home")
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.home)
        os.makedirs(self.root)
        self.env = self._clean_env()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "e2e@example.com")
        self.git("config", "user.name", "E2E")
        self.git("config", "commit.gpgsign", "false")
        self.write("calc.py", CALC)
        self.write("tests/test_calc.py", TEST_CALC)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "sample project")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()

    def _clean_env(self):
        drop = ("REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR")}
        env.update(HOME=self.home, CODEX_HOME=os.path.join(self.home, ".codex"),
                   XDG_CONFIG_HOME=os.path.join(self.home, ".config"), PYTHONDONTWRITEBYTECODE="1")
        return env

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def write(self, rel, text):
        fp = self.path(rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, check=check)

    def cli(self, *args, timeout=600):
        """Run `python3 <repo>/scripts/regista.py <args>` in the project; returns the CompletedProcess."""
        return subprocess.run([PY, TOOL, *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, timeout=timeout)

    def hook(self, event, payload=None, timeout=600):
        """Run `regista.py hook <event>` (event as the CLI names it, e.g. "pre-tool" for PreToolUse) with the JSON payload on stdin (its `cwd` set to the project root);
        returns the parsed JSON answer, or None when the hook printed nothing."""
        data = {**(payload or {}), "cwd": self.root}
        r = subprocess.run([PY, TOOL, "hook", event], input=json.dumps(data), cwd=self.root, capture_output=True,
                           text=True, encoding="utf-8", env=self.env, timeout=timeout)
        self.last_hook = r
        if r.returncode != 0 and not r.stdout.strip():
            raise AssertionError(f"hook {event} failed ({r.returncode}):\n{r.stderr}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def bash(self, cmd):
        return bash(cmd)

    def config(self):
        with open(self.path(".regista/config.json"), encoding="utf-8") as f:
            return json.load(f)

    def set_gates(self, gates):
        cfg = self.config()
        cfg["gates"] = gates
        with open(self.path(".regista/config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def set_up_regista(self, name="Sample"):
        """`regista.py init --auto`, then the unittest gate (init's test-tool detection comes in a later step),
        then commit the setup. Returns init's CompletedProcess."""
        r = self.cli("init", "--auto", "--name", name)
        if r.returncode != 0:
            raise AssertionError(f"init --auto failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        self.set_gates([{"name": "tests", "run": f"{PY} -m unittest discover -s tests", "when": ["commit"]}])
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "set up Regista")
        return r

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)
