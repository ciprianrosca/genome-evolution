"""Adding a test to a frozen test file is a note, not a hard failure (spec .regista/specs/frozen-additions.json,
BENCH-14).

A replay agent may add a test to one of the case's frozen test files (a reviewer asked for a missing test). Adding
can't help the replay pass: the evolver's final gate run copies the case's frozen tests back before it runs. So when
the new content still holds every line of the case's copy, in the same order, with lines only added, the replay is
not a hard failure: result.json records `frozen_added` ([{path, lines}], lines being the number of lines added) and
the scores carry a plain note. Any other change (a line changed or removed, lines reordered, the file deleted,
renamed or replaced with a link) stays the hard failure BENCH-3 describes, naming the file. And a real `hook_by_hand`
refusal (an agent running Regista's hooks by hand) is a hard failure like the other rule-breaking guards.

Two levels:
- Whole replays (`evolve.py replay`) reuse tests/test_replay_run.py's world and its stand-in claude's final_edit
  scenario (the frozen test replaced with the control file `edited_test`), patched here so the committed
  src/double.py comes from the control file `code` when it exists. The added test appends to a marker file when it
  runs, so the tests can show it never runs in the final gate run. `frozen_added` is looked for at the top level of
  result.json or under that key at any depth.
- The hard failures call evolve.score_run directly on tests/test_replay_scores.py's fixture clone (Run). A changed
  frozen test is expected to fail closed there, whatever the scorer can find of the case's copy.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_frozen_additions -v"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_replay_run as rr  # noqa: E402
import test_replay_scores as ts  # noqa: E402

FROZEN = "tests/unit/test_double.py"
CODE_RIGHT = "def double(n):\n    return 2 * n\n"

# The stand-in claude, with final_edit committing src/double.py from the control file `code` when it exists.
_EDIT = '        files["tests/unit/test_double.py"] = read_raw("edited_test")\n'
STANDIN = rr.STANDIN.replace(_EDIT, _EDIT + (
    '        if os.path.exists(os.path.join(CONTROL, "code")):\n'
    '            files["src/double.py"] = read_raw("code")\n'))


def added_test(marker, name="test_added"):
    """A passing test method that appends to `marker` when it runs."""
    return (f"    def {name}(self):\n"
            f"        with open({marker!r}, 'a') as f:\n"
            f"            f.write('ran\\n')\n"
            f"        self.assertEqual(double(-3), -6)\n")


def frozen_added_of(result):
    """result.json's `frozen_added` entries: at the top level, or under a key of that name at any depth."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("frozen_added"), list):
                found.extend(obj["frozen_added"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(result)
    return [e for e in found if isinstance(e, dict)]


def entry_for(entries, path):
    return [e for e in entries if str(e.get("path") or "").lstrip("./") == path.lstrip("./")]


def notes_in(obj):
    """Every string in obj that reads like a sentence, leaving out the `frozen_added` records themselves."""
    if isinstance(obj, str):
        return [obj] if " " in obj.strip() else []
    if isinstance(obj, dict):
        return [s for k, v in obj.items() if k != "frozen_added" for s in notes_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in notes_in(v)]
    return []


def setUpModule():
    rr.setUpModule()  # the sample project and its bench, built once for the replays below


def tearDownModule():
    rr.tearDownModule()


class AdditionReplay:
    """One whole replay whose agent writes the right code and the frozen test with lines only added."""

    def __init__(self, edit):
        """edit(marker) -> the new text of the frozen test."""
        self.w = rr._class_world("final_edit")
        try:
            with open(self.w.claude, "w", encoding="utf-8") as f:
                f.write(STANDIN.replace("@PYTHON@", rr.PY).replace("@CONTROL@", repr(self.w.control)))
            self.marker = os.path.join(self.w.control, "added_test_ran.txt")
            self.edited = edit(self.marker)
            self.w.set_scenario("final_edit", edited_test=self.edited, code=CODE_RIGHT)
            self.r = self.w.run(timeout=280)
        except BaseException:
            self.w.cleanup()
            raise
        self.out = self.r.stdout + self.r.stderr

    def cleanup(self):
        self.w.cleanup()


class AdditionCase(unittest.TestCase):
    """Shared checks for a replay that only adds lines to the frozen test."""

    edit = None
    added_lines = None

    @classmethod
    def setUpClass(cls):
        cls.replay = None
        if cls.edit is None or rr._TEMPLATE["build"].returncode != 0 or not rr._TEMPLATE["case"]:
            return
        cls.replay = AdditionReplay(cls.edit)

    @classmethod
    def tearDownClass(cls):
        if cls.replay:
            cls.replay.cleanup()

    def setUp(self):
        if self.edit is None:
            self.skipTest("shared checks only")
        self.assertIsNotNone(self.replay, f"bench build failed: {rr._TEMPLATE['build'].stderr}")
        self.assertEqual(STANDIN.count(_EDIT + "        if os.path"), 1,
                         "tests/test_replay_run.py's final_edit scenario changed; update the patch above")
        self.w, self.out = self.replay.w, self.replay.out
        self.assertNotIn("Traceback", self.out)
        self.w.first_call(self, self.replay.r)
        self.assertTrue(self.w.lines("final.jsonl"), f"the stand-in never wrote the frozen test:\n{self.out}")
        self.result = self.w.result(self)

    def check_addition_is_a_note(self):
        original = rr.TEST_DOUBLE.splitlines()
        edited = self.replay.edited.splitlines()
        self.assertEqual(len(edited) - len(original), self.added_lines, "the fixture adds the lines it says")
        reason = rr.hard_failure_of(self.result)
        self.assertIsNone(reason, f"adding lines to a frozen test is not a hard failure:\n{self.result}\n{self.out}")
        entries = entry_for(frozen_added_of(self.result), FROZEN)
        self.assertEqual(len(entries), 1, f"result.json records the addition under frozen_added: {self.result}")
        self.assertEqual(entries[0].get("lines"), self.added_lines,
                         f"frozen_added gives the number of lines added: {entries}")
        notes = [s for s in notes_in(self.result.get("scores") or {}) if "test_double.py" in s]
        self.assertTrue(notes, f"the scores carry a plain note naming the file: {self.result.get('scores')}")

    def check_final_run_uses_the_case_copy(self):
        runs = rr.gate_calls(self.w, rr.TESTS_GATE)
        self.assertTrue(runs, f"the evolver never ran the tests gate itself:\n{self.out}")
        for run in runs:
            self.assertEqual(run["frozen"], rr.TEST_DOUBLE, "the final run uses the case's copy, not the addition")
        self.assertFalse(os.path.exists(self.replay.marker), "the added test ran during the final gate run")
        self.assertEqual([c["exit"] for c in runs if c["exit"] != 0], [], "the right code passes the case's test")
        self.assertIs(rr.axis(self.result, "gates").get("failed"), False,
                      f"the final run passed, and the addition costs nothing: {self.result}")
        self.assertIs(rr.axis(self.result, "requirements").get("failed"), False, self.result)


class AppendedTest(AdditionCase):
    """The agent appends one test method at the end of the frozen test file (5 lines with the blank one before it)."""

    added_lines = 5

    @staticmethod
    def edit(marker):
        return rr.TEST_DOUBLE + "\n" + added_test(marker)

    def test_appended_test_is_recorded_not_a_hard_failure(self):  # covers: BENCH-14
        self.check_addition_is_a_note()

    def test_appended_test_never_runs_in_the_final_gate_run(self):  # covers: BENCH-14
        self.check_final_run_uses_the_case_copy()


class InsertedInTheMiddle(AdditionCase):
    """The agent inserts an import and a test method in the middle; every original line stays, in order."""

    added_lines = 6

    @staticmethod
    def edit(marker):
        text = rr.TEST_DOUBLE.replace("import unittest\n", "import unittest\nimport math  # noqa: F401\n", 1)
        head = "class Double(unittest.TestCase):\n"
        return text.replace(head, head + added_test(marker, "test_negative") + "\n", 1)

    def test_lines_inserted_in_the_middle_are_recorded_not_a_hard_failure(self):  # covers: BENCH-14
        self.check_addition_is_a_note()
        self.check_final_run_uses_the_case_copy()


APPENDED = "\n    def test_more(self):\n        self.assertTrue(True)\n"


class RealChangesStayHardFailures(unittest.TestCase):
    """Scored directly on the fixture clone: every change that isn't only added lines stays a hard failure."""

    check_shape = ts.Base.check_shape
    assert_hard_failure = ts.Base.assert_hard_failure

    def fresh_run(self):
        r = ts.Run()
        self.addCleanup(r.cleanup)
        path = os.path.join(r.clone, *ts.FROZEN_REL.split("/"))
        os.chmod(path, 0o644)
        return r, path

    def assert_named_hard_failure(self, result):
        self.assert_hard_failure(result, ts.FROZEN_REL)
        self.assertEqual(entry_for(frozen_added_of(result), ts.FROZEN_REL), [],
                         f"a real change is not recorded as an addition: {result}")

    def test_changed_removed_or_reordered_lines_are_hard_failures_naming_the_file(self):  # covers: BENCH-14
        lines = ts.FROZEN_TEXT.splitlines(keepends=True)
        variants = {
            "one line changed, a test appended": ts.FROZEN_TEXT.replace("assertTrue(True)", "assertTrue(1)") + APPENDED,
            "one line removed": "".join(lines[1:]),
            "one line removed, a test appended": "".join(lines[1:]) + APPENDED,
            "two lines swapped": "".join([lines[1], lines[0]] + lines[2:]),
        }
        for name, text in variants.items():
            with self.subTest(name):
                self.assertNotEqual(text, ts.FROZEN_TEXT)
                r, path = self.fresh_run()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.assert_named_hard_failure(r.score())

    def test_deleted_renamed_or_linked_frozen_test_is_a_hard_failure_naming_the_file(self):  # covers: BENCH-14
        def delete(r, path):
            os.remove(path)

        def rename(r, path):
            os.rename(path, os.path.join(os.path.dirname(path), "test_double_renamed.py"))

        def link(r, path):
            # The link leads to a file holding the case's copy plus an added test: a link is never an addition.
            target = os.path.join(os.path.dirname(path), "double_target.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write(ts.FROZEN_TEXT + APPENDED)
            os.remove(path)
            os.symlink("double_target.py", path)

        for name, change in (("deleted", delete), ("renamed", rename), ("replaced with a link", link)):
            with self.subTest(name):
                r, path = self.fresh_run()
                change(r, path)
                self.assert_named_hard_failure(r.score())


class HookByHand(unittest.TestCase):
    check_shape = ts.Base.check_shape
    assert_hard_failure = ts.Base.assert_hard_failure

    def test_real_hook_by_hand_refusal_is_a_hard_failure_naming_the_rule(self):  # covers: BENCH-14
        for kind in ("deny", "ask"):
            with self.subTest(kind):
                r = ts.Run()
                self.addCleanup(r.cleanup)
                r.refusals([("role_guard", "unsure"), ("hook_by_hand", "real", kind)])
                self.assert_hard_failure(r.score(), "hook_by_hand")


if __name__ == "__main__":
    unittest.main()
