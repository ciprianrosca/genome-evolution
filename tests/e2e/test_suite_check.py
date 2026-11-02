"""End-to-end: `evolve.py suite check <suite>` on a valid made-up suite (spec .regista/specs/suite-files.json, SUI-12).

In a temporary folder the test writes a suite for a made-up greeting skill: four cases (one names train, one names
holdout, two split by the hash), a prompt file, fixtures, a rubric file and a check script, with command, file and
judge checks. One command check would create a marker file if it ever ran. A fake `claude` first on PATH records that
it was started and fails loudly. REGISTA_EVOLVE_HOME points at a path that must still not exist afterwards, and HOME is
a temporary folder.

The test runs `python3 scripts/evolve.py suite check <suite>` as a separate process and checks: exit code 0; the
suite's name, its number of cases, the training cases (the screening one marked), the held-out cases and each case's
number of command, file and judge checks are printed; nothing ran (no marker, no claude) and nothing was written (the
evolver's working folder wasn't created and the suite folder is unchanged).

Interface assumed: the split rule of the spec (cases ordered by the hexadecimal sha256 of '<suite name>:<case id>');
the output names each case id, says "train"/"training" before or on the line of a training case, "held"/"hold" before
or on the line of a held-out case and "screen" for the screening one, and gives a case's check counts after its last
mention, as "<n> command" or "command: <n>" (zero counts aren't checked).

Run: python3 -m unittest discover -s genome/tests/e2e"""
import hashlib
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

EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
NAME = "toy-greeter"

FAKE_CLAUDE = r'''#!/bin/sh
echo "fake claude was started: suite check must not start an agent" >&2
echo "$@" >> "@LOG@"
exit 97
'''

# Per case: how many checks of each kind the suite gives it.
COUNTS = {
    "greet-short": {"command": 2, "file": 1, "judge": 0},
    "farewell-check": {"command": 1, "file": 2, "judge": 0},
    "tone-judge": {"command": 1, "file": 0, "judge": 1},
    "list-format": {"command": 0, "file": 1, "judge": 2},
}
OWN = {"greet-short": "train", "farewell-check": "holdout"}


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def snapshot(root):
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for n in filenames:
            p = os.path.join(dirpath, n)
            with open(p, "rb") as f:
                found[os.path.relpath(p, root)] = f.read()
    return found


def expected_split():
    """(train, holdout) by the spec's rule: own splits win; the other two split by hash, round(2 * 40 / 100) = 1."""
    order = sorted(COUNTS, key=lambda i: hashlib.sha256(f"{NAME}:{i}".encode("utf-8")).hexdigest())
    free = [i for i in order if i not in OWN]
    held = {free[0], *(i for i, s in OWN.items() if s == "holdout")}
    return [i for i in order if i not in held], [i for i in order if i in held]


def section_of(text, cid, keywords):
    """The keyword that marks a case: one on the line of its first mention, else the nearest one before it."""
    pos = text.find(cid)
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    line = text[line_start:len(text) if line_end < 0 else line_end].lower()
    on_line = [k for k, pattern in keywords.items() if re.search(pattern, line)]
    if len(on_line) == 1:
        return on_line[0]
    before = text[:pos].lower()
    best, best_at = None, -1
    for k, pattern in keywords.items():
        hits = [m.start() for m in re.finditer(pattern, before)]
        if hits and hits[-1] > best_at:
            best, best_at = k, hits[-1]
    return best


class SuiteCheckEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-e2e-suite-check-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        j = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        self.home = j("home")
        os.makedirs(self.home)
        self.evo = j("evolve-home-must-not-exist")
        self.marker = j("logs", "check-ran.txt")
        self.claude_log = j("logs", "claude.log")
        os.makedirs(j("logs"))
        fakebin = j("bin")
        write(os.path.join(fakebin, "claude"), FAKE_CLAUDE.replace("@LOG@", self.claude_log), 0o755)
        self.root = j("greeter-suite")
        r = lambda *p: os.path.join(self.root, *p)  # noqa: E731
        write(r("prompts", "farewell.txt"), "Write answer.txt with a short farewell to the made-up user Ana Lumen.\n")
        write(r("rubrics", "tone.txt"), "Pass if the greeting is warm, short and names nobody real.\n")
        write(r("fixtures", "names.txt"), "Ana Lumen\nBo Rivers\n")
        write(r("fixtures", "base", "style.md"), "# Style\n\nKeep greetings under twenty words.\n")
        write(r("checks", "verify.sh"), "#!/bin/sh\ntest -s answer.txt\n", 0o755)
        suite = {
            "name": NAME,
            "describe": "Checks that the made-up greeting skill writes short, polite answers.",
            "install": ".claude/skills/toy-greeter",
            "screen_cases": 1,
            "cases": [
                {"id": "greet-short", "split": "train", "prompt": "Greet Ana Lumen in answer.txt.",
                 "files": {"data/names.txt": "fixtures/names.txt"}, "frozen": ["data/**"],
                 "checks": [{"run": "test -f answer.txt"}, {"run": "sh {checks}/verify.sh"},
                            {"file": "answer.txt", "contains": "Ana"}]},
                {"id": "farewell-check", "split": "holdout", "prompt_file": "prompts/farewell.txt",
                 "checks": [{"id": "never-runs", "run": f"touch '{self.marker}'"},
                            {"file": "answer.txt", "exists": True},
                            {"file": "answer.txt", "not_matches": "!{3,}"}]},
                {"id": "tone-judge", "prompt": "Write a warm greeting for Bo Rivers in answer.txt.",
                 "files": {".": "fixtures/base"},
                 "checks": [{"run": "test -f answer.txt"},
                            {"rubric_file": "rubrics/tone.txt", "model": "opus", "files": ["answer.txt"]}]},
                {"id": "list-format", "prompt": "List two greetings in answer.txt, one per line.",
                 "checks": [{"file": "answer.txt", "matches": "^.+\\n.+"},
                            {"judge": "Pass if there are exactly two greetings.", "model": "opus",
                             "files": ["answer.txt"]},
                            {"id": "polite", "judge": "Pass if both greetings are polite.", "model": "opus",
                             "files": ["answer.txt"]}]},
            ],
        }
        self.suite_path = r("suite.json")
        write(self.suite_path, json.dumps(suite, indent=2) + "\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.endswith("_PROJECT_DIR") and not k.startswith(("REGISTA_", "DAEDALUS_", "GIT_"))}
        self.env.update({"HOME": self.home, "REGISTA_EVOLVE_HOME": self.evo,
                         "PATH": fakebin + os.pathsep + self.env.get("PATH", "")})

    def test_suite_check_prints_split_and_check_counts_and_runs_nothing(self):  # covers: SUI-12
        before = snapshot(self.root)
        done = subprocess.run([PY, EVOLVE, "suite", "check", self.suite_path], cwd=self.home, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = done.stdout + done.stderr
        shown = f"exit {done.returncode}\n{out}"
        self.assertEqual(done.returncode, 0, shown)
        self.assertIn(NAME, done.stdout, f"the suite's name is printed:\n{shown}")
        self.assertRegex(done.stdout, r"(?i)\b4\s+cases?\b|cases?\W{0,3}4\b", f"the number of cases:\n{shown}")

        train, holdout = expected_split()
        self.assertEqual(len(train), 2, "the fixture has two training cases")
        text = done.stdout
        split_words = {"train": r"train", "holdout": r"held|hold"}
        for cid in train:
            self.assertIn(cid, text, shown)
            self.assertEqual(section_of(text, cid, split_words), "train", f"{cid} is listed as training:\n{shown}")
        for cid in holdout:
            self.assertIn(cid, text, shown)
            self.assertEqual(section_of(text, cid, split_words), "holdout", f"{cid} is listed as held out:\n{shown}")
        screening = train[0]
        pos = text.find(screening)
        line = text[text.rfind("\n", 0, pos) + 1:].split("\n", 1)[0]
        screen_words = {"screen": r"screen", "train": r"train", "holdout": r"held|hold"}
        self.assertTrue(re.search(r"(?i)screen", line) or section_of(text, screening, screen_words) == "screen",
                        f"{screening}, the first training case, is marked as screening:\n{shown}")

        # Each case's check counts come after its last mention, before the next case's last mention.
        last = {cid: text.rfind(cid) for cid in COUNTS}
        for cid, counts in COUNTS.items():
            ends = [p for p in last.values() if p > last[cid]]
            segment = text[last[cid]:min(ends) if ends else len(text)]
            for kind, n in counts.items():
                if not n:
                    continue
                pattern = rf"(?i)\b{n}\b[^\n,;]*?\b{kind}|\b{kind}\w*\W{{0,3}}{n}\b"
                self.assertRegex(segment, pattern, f"{cid}: {n} {kind} check(s) shown in:\n{segment}\n\n{shown}")

        self.assertFalse(os.path.exists(self.evo), f"the evolver's working folder isn't created:\n{shown}")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".regista-evolve")),
                         "no default working folder in HOME either")
        self.assertFalse(os.path.exists(self.marker), "no check ran")
        self.assertFalse(os.path.exists(self.claude_log), "claude was never started")
        self.assertEqual(snapshot(self.root), before, "the suite folder is unchanged")


if __name__ == "__main__":
    unittest.main()
