"""Trait bounds kept in the target's profile (spec .regista/specs/genome-traits.json: GEN-2, GEN-5, GEN-6, GNM-6; GNM-7 is
the end-to-end test in genome/tests/e2e/test_genome_seed.py).

The profile, not the genome, says which traits exist and each one's safe range: numeric traits have min, max and step,
choice traits a list of choices; a trait may be "looser" higher or lower than the project's own value (read from the
project's config files, first one found wins); and `relations` tie two traits together. genome.py checks candidates
against it (check_traits, make_child), mutates one trait at a time (mutate_trait) and refuses malformed profiles.

Most tests use a small profile for a target that isn't Regista (a standalone "haiku" skill), which also shows that
genome.py stays target-neutral. Everything lives in temporary folders; the repository is only read.

Interface assumed: problems from check_traits are strings naming the trait; GenomeError messages name each trait at
fault; load_profile takes the source folder as a second argument (named `source`) so it can tell whether the profile
lies inside the source's evolvable files (or as its second positional argument); genome.seed also refuses a source whose values break the profile.
A trait whose source is an evolvable file (the skill's front-matter tone, the prompt's JSON line count) has its value in
that file: make_child reads it from the child's final file and writes a value set through `traits` into that one line or
key; a trait whose source is a frozen file (settings.json) lives only in genome.json. NaN and infinity are plain problems.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_genome_traits -v"""
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_genome_seed import (EVOLVE, evolve_env, files_under, import_scripts, is_plain_error, output,  # noqa: E402
                              read_bytes, sha256, snapshot, write)

# The small target: one standalone skill with a settings file ---------------------------------------------------------

SKILL = "---\nname: haiku-writer\ndescription: Writes haiku.\ntone: warm\n---\n\n# Haiku writer\n\nWrite a haiku.\n"
SETTINGS = {"version": "0.3.0", "limits": {"depth": 3, "width": 10, "budget": 30}}
FROZEN = {"README.md": "# Haiku writer\n", "scripts/count.sh": "#!/bin/sh\nwc -w \"$1\"\n"}

# The project's own config, where a project keeps its values (first source found wins).
PROJECT_NEW = ".haiku/config.json"
PROJECT_OLD = ".old-haiku/settings.json"

PROFILE = {
    "name": "haiku-skill",
    "evolvable": ["SKILL.md", "prompts/*.json"],
    "exclude": ["tests/**"],
    "version": {"file": "settings.json", "key": "version"},
    "traits": {
        "limits.depth": {"json": "settings.json", "key": "limits.depth", "min": 1, "max": 5, "step": 1,
                         "looser": "higher",
                         "project": [{"json": PROJECT_NEW, "key": "limits.depth"}, {"json": PROJECT_OLD, "key": "depth"}]},
        "limits.width": {"json": "settings.json", "key": "limits.width", "min": 4, "max": 12, "step": 2},
        "limits.budget": {"json": "settings.json", "key": "limits.budget", "min": 10, "max": 50, "step": 10,
                          "looser": "lower", "project": [{"json": PROJECT_NEW, "key": "limits.budget"}]},
        "skill.tone": {"front_matter": "SKILL.md", "field": "tone", "choices": ["warm", "dry", "bright"]},
    },
    "relations": [["limits.depth", "<", "limits.width"], ["limits.width", "<=", "limits.budget"]],
}
GOOD = {"limits.depth": 3, "limits.width": 10, "limits.budget": 30, "skill.tone": "warm"}


def build_source(root, skill=SKILL, settings=None):
    for rel, text in {"SKILL.md": skill, "settings.json": json.dumps(settings or SETTINGS, indent=2) + "\n",
                      **FROZEN}.items():
        write(os.path.join(root, rel), text)
    return root


def write_json(path, data):
    write(path, json.dumps(data, indent=2) + "\n")
    return path


def joined(problems):
    return "\n".join(str(p) for p in problems)


class TraitsBase(unittest.TestCase):
    """A temporary folder with the small profile written to a file, outside any source."""

    def setUp(self):
        self.gn = import_scripts("genome")
        self.tmp = tempfile.mkdtemp(prefix="regista-genome-traits-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evo = os.path.join(self.tmp, "evolve-home")
        self.source = build_source(os.path.join(self.tmp, "haiku-skill"))
        self.profile_path = write_json(os.path.join(self.tmp, "profiles", "haiku.json"), PROFILE)

    def load(self, path=None, data=None, source=None, need_source=False):
        """load_profile on `path` (or on `data` written to a fresh file), told which source it is for when it takes
        one. need_source: the test is about where the profile lies, so load_profile must take the source."""
        if data is not None:
            path = write_json(os.path.join(tempfile.mkdtemp(dir=self.tmp), "profile.json"), data)
        fn = self.gn.load_profile
        names = [p.name for p in inspect.signature(fn).parameters.values()]
        if "source" in names:
            return fn(path or self.profile_path, source=source or self.source)
        if len(names) >= 2:
            return fn(path or self.profile_path, source or self.source)
        self.assertFalse(need_source, "load_profile needs the source folder to check where the profile lies")
        return fn(path or self.profile_path)

    def assert_genome_error(self, call, names, what):
        with self.assertRaises(Exception, msg=what) as caught:
            call()
        self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain GenomeError, got {caught.exception!r}")
        for name in names:
            self.assertIn(name, str(caught.exception), f"{what}: the message names {name}")
        return str(caught.exception)

    def genomes(self):
        return snapshot(os.path.join(self.evo, "genomes"))


class CheckTraits(TraitsBase):
    """check_traits: bounds, steps, choices, relations and the project's own values."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()

    def problems(self, changes, project_values=None, drop=()):
        traits = {k: v for k, v in {**GOOD, **changes}.items() if k not in drop}
        return self.gn.check_traits(self.profile, traits, project_values)

    def test_values_inside_bounds_steps_choices_and_relations_pass(self):  # covers: GEN-2
        self.assertEqual(self.problems({}), [], "the good traits have no problem")
        for changes in ({"limits.depth": 1}, {"limits.depth": 5, "limits.width": 6}, {"limits.width": 12},
                        {"limits.width": 4, "limits.budget": 10}, {"limits.budget": 50}, {"skill.tone": "bright"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.problems(changes), [], "boundaries and every listed choice are allowed")

    def test_out_of_bounds_off_step_wrong_choice_and_wrong_type_are_problems(self):  # covers: GEN-2
        cases = {"below min": {"limits.depth": 0}, "above max": {"limits.depth": 6},
                 "above max on step": {"limits.budget": 60}, "off step": {"limits.width": 7},
                 "off step from min": {"limits.budget": 25}, "not a choice": {"skill.tone": "loud"},
                 "a string for a number": {"limits.depth": "3"}}
        for what, changes in cases.items():
            with self.subTest(case=what):
                problems = self.problems(changes)
                self.assertTrue(problems, f"{what}: {changes} should be a problem")
                self.assertIn(next(iter(changes)), joined(problems), "the problem names the trait")

    def test_nan_and_infinite_values_are_problems_not_crashes(self):  # covers: GEN-6
        for value in (float("nan"), float("inf"), float("-inf")):
            for name in ("limits.depth", "limits.budget"):
                with self.subTest(trait=name, value=value):
                    project = {"limits.depth": 3, "limits.budget": 30}
                    self.assertIn(name, joined(self.problems({name: value}, project)),
                                  "a value that isn't a finite number is a problem naming the trait")

    def test_values_that_are_not_a_number_or_string_are_problems_not_type_errors(self):  # covers: GEN-6
        project = {"limits.depth": 3, "limits.budget": 30}
        for value in ([3], {"value": 3}, [], {}, None, True):
            for name in ("limits.depth", "limits.width", "limits.budget", "skill.tone"):
                with self.subTest(trait=name, value=value):
                    self.assertIn(name, joined(self.problems({name: value}, project)),
                                  "a value that isn't a JSON number or string is a plain problem naming the trait")

    def test_broken_relations_are_problems(self):  # covers: GEN-2
        for changes, names in (({"limits.depth": 4, "limits.width": 4}, ("limits.depth", "limits.width")),
                               ({"limits.depth": 5, "limits.width": 4}, ("limits.depth", "limits.width")),
                               ({"limits.width": 12, "limits.budget": 10}, ("limits.width", "limits.budget"))):
            with self.subTest(changes=changes):
                text = joined(self.problems(changes))
                for name in names:
                    self.assertIn(name, text, "a broken relation is a problem naming both traits")
        self.assertEqual(self.problems({"limits.width": 10, "limits.budget": 10}), [], "<= allows equal values")

    def test_values_looser_than_the_project_are_problems(self):  # covers: GEN-2
        project = {"limits.depth": 3, "limits.budget": 30}
        self.assertEqual(self.problems({}, project), [], "equal to the project's values is fine")
        self.assertEqual(self.problems({"limits.depth": 2, "limits.budget": 40}, project), [],
                         "stricter than the project is fine (lower when looser is higher, and the other way round)")
        self.assertIn("limits.depth", joined(self.problems({"limits.depth": 4}, project)),
                      "higher than the project when looser is higher")
        self.assertIn("limits.budget", joined(self.problems({"limits.budget": 20}, project)),
                      "lower than the project when looser is lower")
        self.assertEqual(self.problems({"limits.depth": 5, "limits.width": 6}, {}), [],
                         "without a project value, only the bounds apply")

    def test_unknown_and_missing_traits_are_problems(self):  # covers: GEN-5
        self.assertIn("limits.color", joined(self.problems({"limits.color": 2})), "a trait the profile doesn't define")
        self.assertIn("skill.tone", joined(self.problems({}, drop=("skill.tone",))), "a profile trait left out")

    def test_every_problem_is_listed(self):  # covers: GEN-2
        problems = self.problems({"limits.depth": 9, "limits.width": 7, "skill.tone": "loud", "limits.color": 1,
                                  "limits.budget": 20}, {"limits.budget": 30})
        text = joined(problems)
        for name in ("limits.depth", "limits.width", "skill.tone", "limits.color", "limits.budget"):
            self.assertIn(name, text, "check_traits lists every problem, not only the first")
        self.assertGreaterEqual(len(problems), 5)


class ProjectValues(TraitsBase):
    """project_values: the project's own value per trait, from the first of its sources that exists."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)

    def test_only_the_second_source_present_gives_its_value(self):  # covers: GEN-2
        write_json(os.path.join(self.project, PROJECT_OLD), {"depth": 2})
        self.assertEqual(self.gn.project_values(self.profile, self.project), {"limits.depth": 2},
                         "depth from the old file; budget has no file found, width and tone no project source")

    def test_first_source_found_wins(self):  # covers: GEN-2
        write_json(os.path.join(self.project, PROJECT_OLD), {"depth": 2})
        write_json(os.path.join(self.project, PROJECT_NEW), {"limits": {"depth": 4, "budget": 20}})
        self.assertEqual(self.gn.project_values(self.profile, self.project), {"limits.depth": 4, "limits.budget": 20})

    def test_no_project_file_gives_nothing(self):  # covers: GEN-2
        self.assertEqual(self.gn.project_values(self.profile, self.project), {})


class MutateTrait(TraitsBase):
    """mutate_trait over many seeded random draws."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()

    def mutations(self, traits, name, draws=200):
        results = []
        for n in range(draws):
            before = dict(traits)
            child = self.gn.mutate_trait(self.profile, traits, name, random.Random(n))
            self.assertEqual(traits, before, "the parent's traits are left unchanged")
            self.assertEqual({k: v for k, v in child.items() if k != name},
                             {k: v for k, v in traits.items() if k != name}, f"only {name} changes")
            results.append(child[name])
        return results

    def test_numeric_trait_moves_one_step_and_stays_in_bounds(self):  # covers: GEN-2
        cases = (("limits.depth", {}, {2, 4}), ("limits.depth", {"limits.depth": 1}, {2}),
                 ("limits.depth", {"limits.depth": 5, "limits.width": 12}, {4}),
                 ("limits.width", {}, {8, 12}), ("limits.width", {"limits.width": 4}, {6}),
                 ("limits.width", {"limits.width": 12, "limits.budget": 40}, {10}),
                 ("limits.budget", {"limits.budget": 10, "limits.width": 8}, {20}),
                 ("limits.budget", {"limits.budget": 50}, {40}))
        for name, changes, expected in cases:
            traits = {**GOOD, **changes}
            with self.subTest(trait=name, start=traits[name]):
                self.assertEqual(set(self.mutations(traits, name)), expected,
                                 "one step up or down, never past min or max, and both ways over many draws")

    def test_choice_trait_takes_a_different_choice(self):  # covers: GEN-2
        for value in self.mutations(GOOD, "skill.tone"):
            self.assertIn(value, ("dry", "bright"), "a different choice from the list")

    def test_unknown_trait_or_one_without_another_legal_value_is_refused(self):  # covers: GEN-2, GEN-5
        self.assert_genome_error(lambda: self.gn.mutate_trait(self.profile, GOOD, "limits.color", random.Random(1)),
                                 ["limits.color"], "mutating a trait the profile doesn't define")
        fixed = json.loads(json.dumps(PROFILE))
        fixed["traits"]["limits.depth"].update({"min": 3, "max": 3})
        fixed["traits"]["skill.tone"]["choices"] = ["warm"]
        profile = self.load(data=fixed)
        for name in ("limits.depth", "skill.tone"):
            with self.subTest(trait=name):
                self.assert_genome_error(lambda name=name: self.gn.mutate_trait(profile, GOOD, name, random.Random(1)),
                                         [name], f"mutating {name}, which has only one legal value")


class MakeChild(TraitsBase):
    """make_child from a seeded parent: a good candidate is written and expanded, a bad one leaves no trace."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()
        self.gn.seed(self.evo, self.profile, self.source, "seed")

    def genome_json(self, genome_id):
        with open(os.path.join(self.evo, "genomes", genome_id, "genome.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_bad_candidate_is_rejected_with_every_problem_and_nothing_written(self):  # covers: GEN-2, GEN-5
        cases = {
            "several problems": ({"limits.depth": 9, "limits.width": 7, "skill.tone": "loud", "limits.color": 1}, None,
                                 ("limits.depth", "limits.width", "skill.tone", "limits.color")),
            "a broken relation": ({"limits.depth": 4, "limits.width": 4}, None, ("limits.depth", "limits.width")),
            "looser than the project": ({"limits.depth": 4}, {"limits.depth": 3}, ("limits.depth",)),
        }
        for what, (traits, project, names) in cases.items():
            with self.subTest(case=what):
                before = self.genomes()
                self.assert_genome_error(
                    lambda traits=traits, project=project: self.gn.make_child(
                        self.evo, self.profile, self.source, "bad-child", ["seed"],
                        files={"SKILL.md": b"---\nname: haiku-writer\ntone: warm\n---\n\nNew.\n"},
                        traits=traits, project_values=project),
                    names, f"a child with {what}")
                self.assertEqual(self.genomes(), before, "nothing written under genomes/")

    def test_good_candidate_is_written_with_its_parents_and_expanded(self):  # covers: GEN-2
        new_skill = b"---\nname: haiku-writer\ndescription: Writes haiku.\ntone: dry\n---\n\nWrite a short haiku.\n"
        self.gn.make_child(self.evo, self.profile, self.source, "child-1", ["seed"], files={"SKILL.md": new_skill},
                           traits={"limits.depth": 4, "skill.tone": "dry"}, project_values={"limits.depth": 4})
        seed, child = self.genome_json("seed"), self.genome_json("child-1")
        self.assertEqual(set(child), set(seed), "a child's genome.json has the keys seed writes")
        self.assertEqual(child["id"], "child-1")
        self.assertEqual(child["parents"], ["seed"])
        self.assertEqual(child["profile"], "haiku-skill")
        self.assertEqual(child["files"], [{"path": "SKILL.md", "sha256": sha256(new_skill), "bytes": len(new_skill)}])
        self.assertEqual(child["traits"], {**GOOD, "limits.depth": 4, "skill.tone": "dry"})
        folder = os.path.join(self.evo, "genomes", "child-1")
        self.assertEqual(read_bytes(os.path.join(folder, "files", "SKILL.md")), new_skill)
        plugin = files_under(os.path.join(folder, "plugin"))
        self.assertEqual(sorted(plugin), sorted(["SKILL.md", "settings.json", *FROZEN]), "the child is expanded")
        self.assertEqual(read_bytes(plugin["SKILL.md"]), new_skill)
        self.gn.load_genome(self.evo, "child-1")
        self.gn.check_individual(self.evo, "child-1")
        # A second child of two parents starts from the first parent's files and traits.
        self.gn.make_child(self.evo, self.profile, self.source, "child-2", ["child-1", "seed"])
        second = self.genome_json("child-2")
        self.assertEqual(second["parents"], ["child-1", "seed"])
        self.assertEqual(second["files"], child["files"], "files start as the first parent's")
        self.assertEqual(second["traits"], child["traits"])


# A trait in an evolvable JSON file: the prompt's line count, in prompts/shape.json.
SHAPE = {"shape": {"lines": 3, "syllables": [5, 7, 5]}, "title": "Haiku"}
SHAPE_TEXT = json.dumps(SHAPE, indent=2) + "\n"
WITH_SHAPE = {**PROFILE, "traits": {**PROFILE["traits"], "prompt.lines": {"json": "prompts/shape.json",
                                                                          "key": "shape.lines", "min": 3, "max": 5,
                                                                          "step": 1}}}


def changed_lines(before, after):
    """The (old, new) pairs of lines that differ, when both texts have the same number of lines."""
    old, new = before.decode("utf-8").splitlines(), after.decode("utf-8").splitlines()
    if len(old) != len(new):
        raise AssertionError(f"the number of lines changed:\n{before!r}\n{after!r}")
    return [(a, b) for a, b in zip(old, new) if a != b]


# Front-matter tone fields the strict reader can't read (it isn't general YAML): each replaces "tone: warm" in SKILL.
UNREADABLE_TONES = {
    "a repeated field": "tone: warm\ntone: loud",
    "a repeated field with two legal values": "tone: warm\ntone: dry",
    "a double-quoted value": 'tone: "warm"',
    "an unclosed quote": 'tone: "warmX',
    "single quotes": "tone: 'warm'''",
    "an empty value": "tone:",
    "an indented continuation line": "tone: warm\n  and gentle",
}


class EvolvableTraitSources(TraitsBase):
    """A trait whose source is one of the genome's evolvable files lives in that file; one from a frozen file lives
    only in genome.json."""

    def setUp(self):
        super().setUp()
        write(os.path.join(self.source, "prompts", "shape.json"), SHAPE_TEXT)
        self.profile = self.load(data=WITH_SHAPE)
        self.gn.seed(self.evo, self.profile, self.source, "seed")

    def child(self, child_id, files=None, traits=None):
        self.gn.make_child(self.evo, self.profile, self.source, child_id, ["seed"], files=files, traits=traits)
        folder = os.path.join(self.evo, "genomes", child_id)
        with open(os.path.join(folder, "genome.json"), encoding="utf-8") as f:
            return json.load(f), folder

    def rejected(self, what, names, files=None, traits=None, child_id="bad-child"):
        before = self.genomes()
        self.assert_genome_error(lambda: self.gn.make_child(self.evo, self.profile, self.source, child_id, ["seed"],
                                                            files=files, traits=traits), names, what)
        self.assertEqual(self.genomes(), before, f"{what}: nothing written under genomes/")

    def test_out_of_bounds_value_in_a_replaced_evolvable_file_rejects_the_child(self):  # covers: GEN-6
        self.rejected("a SKILL.md whose front-matter tone isn't a choice", ["skill.tone"],
                      files={"SKILL.md": SKILL.replace("tone: warm", "tone: loud").encode()})

    def test_legal_value_in_a_replaced_evolvable_file_is_recorded(self):  # covers: GEN-6
        skill = SKILL.replace("tone: warm", "tone: dry").encode()
        genome, folder = self.child("dry-child", files={"SKILL.md": skill})
        self.assertEqual(genome["traits"]["skill.tone"], "dry", "genome.json records what the file says")
        self.assertEqual(read_bytes(os.path.join(folder, "files", "SKILL.md")), skill, "the file is kept as given")

    def test_trait_set_on_an_evolvable_source_is_written_into_its_file(self):  # covers: GEN-6
        cases = (("skill.tone", "bright", "SKILL.md", SKILL, ("tone: warm", "tone: bright")),
                 ("prompt.lines", 4, "prompts/shape.json", SHAPE_TEXT, ('    "lines": 3,', '    "lines": 4,')))
        for n, (name, value, rel, original, expected) in enumerate(cases):
            with self.subTest(trait=name):
                genome, folder = self.child(f"set-{n}", traits={name: value})
                self.assertEqual(genome["traits"][name], value, "genome.json records the new value")
                written = read_bytes(os.path.join(folder, "files", rel))
                self.assertEqual(changed_lines(original.encode(), written), [expected],
                                 "only the trait's line or key changes in the file")
                self.assertEqual(read_bytes(os.path.join(folder, "plugin", rel)), written, "the expanded plugin agrees")
                entry = [f for f in genome["files"] if f["path"] == rel]
                self.assertEqual(entry, [{"path": rel, "sha256": sha256(written), "bytes": len(written)}],
                                 "genome.json lists the changed file with its new hash")

    def test_trait_with_a_frozen_source_lives_only_in_genome_json(self):  # covers: GEN-6
        genome, folder = self.child("deeper", traits={"limits.depth": 4})
        self.assertEqual(genome["traits"]["limits.depth"], 4, "genome.json records the new value")
        self.assertNotIn("settings.json", files_under(os.path.join(folder, "files")), "a frozen file isn't a genome file")
        self.assertEqual(read_bytes(os.path.join(folder, "plugin", "settings.json")),
                         read_bytes(os.path.join(self.source, "settings.json")), "the frozen file is untouched")

    def test_nan_or_infinite_trait_values_reject_the_child_plainly(self):  # covers: GEN-6
        nan_shape = SHAPE_TEXT.replace('"lines": 3', '"lines": NaN').encode()
        self.rejected("a NaN width", ["limits.width"], traits={"limits.width": float("nan")})
        self.rejected("an infinite line count", ["prompt.lines"], traits={"prompt.lines": float("inf")})
        self.rejected("a prompt file whose line count is NaN", ["prompt.lines"], files={"prompts/shape.json": nan_shape})

    def test_unreadable_front_matter_field_in_a_replaced_file_rejects_the_child(self):  # covers: GEN-6
        for n, (what, tone) in enumerate(UNREADABLE_TONES.items()):
            with self.subTest(case=what):
                self.rejected(f"a SKILL.md with {what}", ["skill.tone", "SKILL.md"],
                              files={"SKILL.md": SKILL.replace("tone: warm", tone).encode()}, child_id=f"unreadable-{n}")

    def test_inline_comment_in_a_replaced_file_is_not_part_of_the_value(self):  # covers: GEN-6
        for n, value in enumerate(("warm", "dry")):
            with self.subTest(value=value):
                genome, _ = self.child(f"commented-{n}",
                                       files={"SKILL.md": SKILL.replace("tone: warm", f"tone: {value}  # note").encode()})
                self.assertEqual(genome["traits"]["skill.tone"], value, "the comment is dropped from the value")

    def test_traits_win_over_files_for_the_same_file_held_trait(self):  # covers: GEN-6
        given = SKILL.replace("tone: warm", "tone: dry").replace("Write a haiku.", "Write a short haiku.").encode()
        genome, folder = self.child("both", files={"SKILL.md": given}, traits={"skill.tone": "bright"})
        self.assertEqual(genome["traits"]["skill.tone"], "bright", "the value set through traits is recorded")
        written = read_bytes(os.path.join(folder, "files", "SKILL.md"))
        self.assertEqual(changed_lines(given, written), [("tone: dry", "tone: bright")],
                         "traits is written into the replaced file last; the rest of the given file is kept")

    def test_written_value_has_exactly_one_space_after_the_colon(self):  # covers: GEN-6
        given = SKILL.replace("tone: warm", "tone:  dry").encode()
        _, folder = self.child("spaced", files={"SKILL.md": given}, traits={"skill.tone": "bright"})
        written = read_bytes(os.path.join(folder, "files", "SKILL.md"))
        self.assertEqual(changed_lines(given, written), [("tone:  dry", "tone: bright")],
                         "the field's line becomes 'tone: bright', one space after the colon")

    def test_load_genome_refuses_a_file_held_trait_that_differs_from_its_file(self):  # covers: GEN-6
        path = os.path.join(self.evo, "genomes", "seed", "genome.json")
        with open(path, encoding="utf-8") as f:
            original = json.load(f)
        self.gn.load_genome(self.evo, "seed")  # the untouched seed loads
        for name, value in (("skill.tone", "dry"), ("prompt.lines", 4)):
            with self.subTest(trait=name):
                edited = json.loads(json.dumps(original))
                self.assertNotEqual(edited["traits"][name], value)
                edited["traits"][name] = value  # a legal value, but not what the file says; file hashes untouched
                write_json(path, edited)
                self.assert_genome_error(lambda: self.gn.load_genome(self.evo, "seed"), [name],
                                         f"a genome.json whose {name} differs from its file")
        write_json(path, original)

    def test_list_or_dict_trait_values_reject_the_child_plainly(self):  # covers: GEN-6
        for name, value in (("limits.width", [10]), ("limits.width", {"value": 10}), ("skill.tone", ["dry"]),
                            ("skill.tone", {"tone": "dry"}), ("prompt.lines", [4])):
            with self.subTest(trait=name, value=value):
                self.rejected(f"{name} set to {value!r}", [name], traits={name: value})


class GenomeJsonKeys(TraitsBase):
    def test_load_genome_refuses_a_genome_json_with_its_own_bounds(self):  # covers: GEN-5
        self.gn.seed(self.evo, self.load(), self.source, "carrier")
        path = os.path.join(self.evo, "genomes", "carrier", "genome.json")
        with open(path, encoding="utf-8") as f:
            genome = json.load(f)
        genome["bounds"] = {"limits.depth": {"min": 1, "max": 99, "step": 1}}
        write_json(path, genome)
        self.assert_genome_error(lambda: self.gn.load_genome(self.evo, "carrier"), ["bounds"],
                                 "a genome.json carrying a key seed doesn't write")


class LoadProfile(TraitsBase):
    """load_profile refuses each malformed profile with a plain message."""

    def broken(self, trait=None, **change):
        data = json.loads(json.dumps(PROFILE))
        if trait:
            data["traits"][trait] = {k: v for k, v in {**data["traits"][trait], **change}.items() if v is not None}
        else:
            data.update(change)
        return data

    def test_malformed_profiles_are_refused(self):  # covers: GEN-5
        cases = {
            "a trait without bounds": (self.broken("limits.width", min=None, max=None, step=None), "limits.width"),
            "a choice trait without choices": (self.broken("skill.tone", choices=None), "skill.tone"),
            "min above max": (self.broken("limits.width", min=12, max=4), "limits.width"),
            "a zero step": (self.broken("limits.budget", step=0), "limits.budget"),
            "a negative step": (self.broken("limits.budget", step=-10), "limits.budget"),
            "an empty choice list": (self.broken("skill.tone", choices=[]), "skill.tone"),
            "an unknown looser value": (self.broken("limits.depth", looser="wider"), "limits.depth"),
            "a relation naming an unknown trait": (self.broken(relations=[["limits.depth", "<", "limits.colour"]]),
                                                   "limits.colour"),
        }
        self.load()  # the unbroken profile loads
        for what, (data, name) in cases.items():
            with self.subTest(case=what):
                self.assert_genome_error(lambda data=data: self.load(data=data), [name], f"a profile with {what}")

    def test_nan_or_infinite_bounds_are_refused(self):  # covers: GEN-6
        nan, inf = float("nan"), float("inf")
        cases = {"a NaN min": self.broken("limits.width", min=nan), "an infinite max": self.broken("limits.width", max=inf),
                 "a minus infinity min": self.broken("limits.width", min=-inf),
                 "an infinite step": self.broken("limits.width", step=inf),
                 "a NaN step": self.broken("limits.width", step=nan)}
        for what, data in cases.items():
            with self.subTest(case=what):
                self.assert_genome_error(lambda data=data: self.load(data=data), ["limits.width"], f"a profile with {what}")

    def test_profile_inside_the_sources_evolvable_files_is_refused(self):  # covers: GEN-5
        inside = write_json(os.path.join(self.source, "prompts", "tuning.json"), PROFILE)
        self.load(path=self.profile_path, need_source=True)  # the same profile outside the source loads
        self.assert_genome_error(lambda: self.load(path=inside, need_source=True), [], "a profile a genome could change")


class SeedConsistency(TraitsBase):
    """seed refuses a source whose own values break the profile."""

    def test_source_breaking_bounds_or_a_relation_is_refused_and_nothing_written(self):  # covers: GEN-2
        profile = self.load()
        cases = {"out of bounds": ({"depth": 9}, ("limits.depth",)),
                 "off step": ({"width": 7}, ("limits.width",)),
                 "a broken relation": ({"depth": 5, "width": 4}, ("limits.depth", "limits.width"))}
        for n, (what, (limits, names)) in enumerate(cases.items()):
            with self.subTest(case=what):
                source = build_source(os.path.join(self.tmp, f"bad-source-{n}"),
                                      settings={**SETTINGS, "limits": {**SETTINGS["limits"], **limits}})
                self.assert_genome_error(lambda source=source: self.gn.seed(self.evo, profile, source, "bad"),
                                         names, f"seeding a source with {what}")
                self.assertFalse(os.path.exists(os.path.join(self.evo, "genomes", "bad")), "nothing written")


class FrontMatter(TraitsBase):
    """The front-matter reader stops at the closing --- and drops an inline # comment."""

    def seeded_tone(self, skill, genome_id):
        source = build_source(os.path.join(self.tmp, genome_id), skill=skill)
        self.gn.seed(self.evo, self.load(source=source), source, genome_id)
        with open(os.path.join(self.evo, "genomes", genome_id, "genome.json"), encoding="utf-8") as f:
            return json.load(f)["traits"]["skill.tone"]

    def test_inline_comment_is_not_part_of_the_value(self):  # covers: GNM-6
        self.assertEqual(self.seeded_tone("---\nname: haiku-writer\ntone: dry  # was warm\n---\n\nBody.\n", "commented"),
                         "dry")

    def test_no_closing_line_or_a_field_after_it_gives_no_trait(self):  # covers: GNM-6
        cases = {"no closing line": "---\nname: haiku-writer\n\n# Haiku writer\n\ntone: warm\n",
                 "the field only after the closing line": "---\nname: haiku-writer\n---\n\ntone: warm\n"}
        for n, (what, skill) in enumerate(cases.items()):
            with self.subTest(case=what):
                genome_id = f"front-{n}"
                with self.assertRaises(Exception) as caught:
                    self.seeded_tone(skill, genome_id)
                self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain error, got {caught.exception!r}")
                self.assertTrue("skill.tone" in str(caught.exception) or "tone" in str(caught.exception),
                                f"{what}: the message names the missing trait")
                self.assertFalse(os.path.exists(os.path.join(self.evo, "genomes", genome_id)), "nothing written")


class StrictFrontMatterAtSeed(TraitsBase):
    def test_unreadable_front_matter_field_refuses_the_seed(self):  # covers: GEN-6
        for n, (what, tone) in enumerate(UNREADABLE_TONES.items()):
            with self.subTest(case=what):
                genome_id = f"unreadable-{n}"
                source = build_source(os.path.join(self.tmp, genome_id), skill=SKILL.replace("tone: warm", tone))
                profile = self.load(source=source)
                self.assert_genome_error(lambda: self.gn.seed(self.evo, profile, source, genome_id),
                                         ["skill.tone", "SKILL.md"], f"seeding a SKILL.md with {what}")
                self.assertFalse(os.path.exists(os.path.join(self.evo, "genomes", genome_id)), "nothing written")


class SeedReservedId(TraitsBase):
    def test_seed_id_current_exits_1_and_writes_nothing(self):  # covers: GNM-6
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)

        def seed(genome_id):
            return subprocess.run([sys.executable, EVOLVE, "genome", "seed", "--id", genome_id, "--profile",
                                   self.profile_path, "--source", self.source], cwd=self.tmp, capture_output=True,
                                  text=True, encoding="utf-8", env=evolve_env(home, self.evo), timeout=300)

        r = seed("current")
        self.assertEqual(r.returncode, 1, f"seeding the reserved id current:\n{output(r)}")
        text = r.stdout + r.stderr
        self.assertTrue(text.strip(), "a message says why")
        self.assertNotIn("Traceback", text, "a plain message, not a crash")
        self.assertIn("current", text, "the message names the id")
        self.assertIn(self.genomes(), (None, {}), "nothing written under genomes/")
        self.assertFalse(os.path.exists(os.path.join(self.evo, "genomes", "current")))
        ok = seed("not-current")  # the same command with another id works, so the refusal is about the id
        self.assertEqual(ok.returncode, 0, f"seeding the small profile with another id:\n{output(ok)}")


if __name__ == "__main__":
    unittest.main()
