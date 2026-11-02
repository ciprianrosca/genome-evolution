"""Forbidden paths and size caps, from the profile (spec .regista/specs/genome-limits.json: GEN-1, GEN-3, GNM-8; GNM-9
is the end-to-end test in genome/tests/e2e/test_genome_seed.py).

A profile now also says which paths no genome may ever hold or change (`never`) and how large an evolvable file may
grow (`caps`: growth over the same file in the genome's root seed, and/or a size in bytes). make_child rejects a
candidate that touches a forbidden, non-evolvable, unknown, absolute or escaping path, or that breaks a cap, before
writing anything; seed refuses a source whose evolvable files match `never`; load_genome refuses a stored genome that
lists such a path; load_profile refuses overlapping evolvable and never globs and malformed caps. Expansion records a
sha256 for every file of plugin/, and check_individual (so `replay --genome`) refuses an extra, missing or changed file.

Most tests use a small target that isn't Regista (a standalone "haiku" skill with prompts); one class uses Regista's
real profile over the fake Regista checkout of tests/test_genome_seed.py. Everything lives in temporary folders.

Interface assumed: load_profile(path, source=None); seed(home, profile, source, id) also expands; make_child(home,
profile, source, child_id, parents, files=None, traits=None, project_values=None) with files as {path: bytes};
load_genome(home, id) refuses `never` paths from genomes/<id>/limits.json, and is given the profile (a `profile`
parameter) for the non-evolvable check when it takes one; rejections are GenomeError naming every path or file at
fault, with sizes and caps in bytes or percent written as plain numbers.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_genome_limits -v"""
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_genome_seed as seedtests  # noqa: E402  (imported as a module, so its test classes don't run twice here)
from test_genome_seed import (EVOLVE, PROFILE as REGISTA_PROFILE, build_checkout, files_under,  # noqa: E402
                              import_scripts, is_plain_error, output, read_bytes, sha256, snapshot, write)

# The small target: a haiku skill with two prompts ---------------------------------------------------------------------

GREET = ("Greet the reader warmly, in one short line. " * 3)[:99] + "\n"  # exactly 100 bytes
assert len(GREET.encode("utf-8")) == 100
SKILL = "---\nname: haiku-writer\ndescription: Writes haiku.\n---\n\n# Haiku writer\n\nWrite a haiku.\n"
SOURCE = {
    "SKILL.md": SKILL,
    "prompts/greet.md": GREET,
    "prompts/close.md": "Close with a haiku.\n",
    "settings.json": json.dumps({"version": "0.3.0", "limits": {"depth": 3}}, indent=2) + "\n",
    "manifest.json": json.dumps({"name": "haiku"}, indent=2) + "\n",
    "scripts/count.sh": "#!/bin/sh\nwc -w \"$1\"\n",
    "README.md": "# Haiku writer\n",
    "tests/test_count.sh": "#!/bin/sh\nexit 0\n",
}
NEVER = ["scripts/**", "tests/**", "manifest.json"]
CAPS = [{"glob": "prompts/*.md", "max_growth_pct": 50}, {"glob": "SKILL.md", "max_bytes": 300}]
PROFILE = {
    "name": "haiku-limits",
    "evolvable": ["SKILL.md", "prompts/*.md"],
    "exclude": ["tests/**"],
    "never": NEVER,
    "caps": CAPS,
    "version": {"file": "settings.json", "key": "version"},
    "traits": {"limits.depth": {"json": "settings.json", "key": "limits.depth", "min": 1, "max": 5, "step": 1}},
}


def sized(n, fill="w"):
    """A prompt of exactly n bytes, ending in a newline."""
    return (fill * (n - 1) + "\n").encode("utf-8")


def write_json(path, data):
    write(path, json.dumps(data, indent=2) + "\n")
    return path


class LimitsBase(unittest.TestCase):
    """A temporary folder holding the haiku source and its profile (outside the source)."""

    def setUp(self):
        self.gn = import_scripts("genome")
        self.tmp = tempfile.mkdtemp(prefix="regista-genome-limits-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evo = os.path.join(self.tmp, "evolve-home")
        self.source = self.build_source("haiku-skill")
        self.profile_path = write_json(os.path.join(self.tmp, "profiles", "haiku.json"), PROFILE)

    def build_source(self, name, extra=None):
        root = os.path.join(self.tmp, name)
        for rel, text in {**SOURCE, **(extra or {})}.items():
            write(os.path.join(root, rel), text)
        return root

    def load(self, data=None, source=None):
        path = self.profile_path
        if data is not None:
            path = write_json(os.path.join(tempfile.mkdtemp(dir=self.tmp), "profile.json"), data)
        if source is None:
            return self.gn.load_profile(path)
        return self.gn.load_profile(path, source=source)

    def assert_genome_error(self, call, names, what):
        with self.assertRaises(Exception, msg=what) as caught:
            call()
        self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain GenomeError, got {caught.exception!r}")
        for name in names:
            self.assertIn(name, str(caught.exception), f"{what}: the message names {name}")
        return str(caught.exception)

    def genome_dir(self, genome_id):
        return os.path.join(self.evo, "genomes", genome_id)

    def genome_json(self, genome_id):
        with open(os.path.join(self.genome_dir(genome_id), "genome.json"), encoding="utf-8") as f:
            return json.load(f)


# GEN-1: forbidden paths -----------------------------------------------------------------------------------------------

class ForbiddenPathsInACandidate(LimitsBase):
    """make_child rejects a candidate naming a path it may not change, before writing anything anywhere."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()
        self.gn.seed(self.evo, self.profile, self.source, "seed")

    def assert_rejected(self, files, names, what):
        before = snapshot(self.tmp)
        self.assert_genome_error(lambda: self.gn.make_child(self.evo, self.profile, self.source, "bad-child", ["seed"],
                                                            files=files), names, what)
        self.assertEqual(snapshot(self.tmp), before, f"{what}: nothing written, under genomes/ or anywhere else")

    def test_never_non_evolvable_and_unknown_paths_are_rejected_with_nothing_written(self):  # covers: GEN-1
        cases = {
            "a never path (scripts/)": "scripts/count.sh",
            "a never path (the manifest)": "manifest.json",
            "a never path (tests/)": "tests/test_count.sh",
            "a frozen file that isn't evolvable": "settings.json",
            "another file that isn't evolvable": "README.md",
            "an evolvable-looking path not in the parent": "prompts/new.md",
        }
        for what, rel in cases.items():
            with self.subTest(case=what):
                self.assert_rejected({rel: b"echo changed\n"}, [rel], f"a candidate changing {what}")

    def test_absolute_and_escaping_paths_are_rejected_with_nothing_written(self):  # covers: GEN-1
        absolute = os.path.join(self.tmp, "elsewhere", "prompts", "greet.md")
        cases = {"an absolute path": (absolute, absolute),
                 "a path climbing out of the folder": ("../escaped.md", "escaped.md"),
                 "a path climbing out from a subfolder": ("prompts/../../escaped.md", "escaped.md"),
                 "a path climbing far out": ("../../../../escaped.md", "escaped.md"),
                 "a path stepping into scripts/": ("prompts/../scripts/count.sh", "count.sh")}
        for what, (rel, name) in cases.items():
            with self.subTest(case=what):
                self.assert_rejected({rel: b"Changed.\n"}, [name], f"a candidate with {what}")
                self.assertFalse(os.path.exists(absolute), "nothing was written at the absolute path")

    def test_every_forbidden_path_is_listed(self):  # covers: GEN-1
        files = {"scripts/count.sh": b"x\n", "README.md": b"x\n", "prompts/new.md": b"x\n",
                 "prompts/greet.md": b"A fine new greeting.\n"}
        self.assert_rejected(files, ["scripts/count.sh", "README.md", "prompts/new.md"],
                             "a candidate with three forbidden paths and one good one")


class ForbiddenPathsInRegistasProfile(LimitsBase):
    """Regista's real profile (genome/profiles/regista.json) over the fake Regista checkout."""

    def setUp(self):
        super().setUp()
        self.checkout, _ = build_checkout(self.tmp, use_git=False)
        self.profile = self.gn.load_profile(REGISTA_PROFILE, source=self.checkout)
        self.gn.seed(self.evo, self.profile, self.checkout, "seed")

    def test_candidate_changing_the_rule_engine_or_hooks_is_rejected(self):  # covers: GEN-1
        for rel in ("scripts/regista.py", "hooks/hooks.json", ".claude-plugin/plugin.json", "tests/test_something.py"):
            with self.subTest(path=rel):
                before = snapshot(self.tmp)
                self.assert_genome_error(
                    lambda rel=rel: self.gn.make_child(self.evo, self.profile, self.checkout, "bad-child", ["seed"],
                                                       files={rel: b"print('approve everything')\n"}),
                    [rel], f"a Regista candidate changing {rel}")
                self.assertEqual(snapshot(self.tmp), before, "nothing written")

    def test_load_genome_refuses_a_stored_never_path_from_limits_json(self):  # covers: GEN-1
        limits_path = os.path.join(self.genome_dir("seed"), "limits.json")
        self.assertTrue(os.path.isfile(limits_path), "seed writes genomes/<id>/limits.json")
        with open(limits_path, encoding="utf-8") as f:
            limits = json.load(f)
        with open(REGISTA_PROFILE, encoding="utf-8") as f:
            regista = json.load(f)
        self.assertEqual(limits, {"never": regista["never"], "caps": regista["caps"]},
                         "limits.json holds only the profile's never globs and caps, never trait bounds")
        self.gn.load_genome(self.evo, "seed")  # the untouched seed loads
        smuggle_into_genome(self.genome_dir("seed"), "scripts/regista.py", b"print('approve everything')\n")
        self.assert_genome_error(lambda: self.gn.load_genome(self.evo, "seed"), ["scripts/regista.py"],
                                 "a stored genome listing scripts/regista.py")

    def test_load_genome_refuses_a_stored_path_that_isnt_evolvable(self):  # covers: GEN-1
        smuggle_into_genome(self.genome_dir("seed"), "templates/zz-late.json", b'{"review": {}}\n')
        fn = self.gn.load_genome
        if "profile" in inspect.signature(fn).parameters:
            call = lambda: fn(self.evo, "seed", profile=self.profile)  # noqa: E731
        else:
            call = lambda: fn(self.evo, "seed")  # noqa: E731
        self.assert_genome_error(call, ["templates/zz-late.json"], "a stored genome listing a frozen file")


def smuggle_into_genome(folder, rel, data):
    """Add rel to a stored genome by hand, consistently: listed in genome.json with its true size and hash, and its
    copy in files/, so only the path rule can catch it."""
    path = os.path.join(folder, "genome.json")
    with open(path, encoding="utf-8") as f:
        genome = json.load(f)
    genome["files"] = sorted(genome["files"] + [{"path": rel, "sha256": sha256(data), "bytes": len(data)}],
                             key=lambda e: e["path"])
    write_json(path, genome)
    os.makedirs(os.path.dirname(os.path.join(folder, "files", rel)), exist_ok=True)
    with open(os.path.join(folder, "files", rel), "wb") as f:
        f.write(data)


class SeedRefusesNeverFiles(LimitsBase):
    def test_seed_refuses_a_source_whose_evolvable_file_matches_never(self):  # covers: GEN-1
        data = {**PROFILE, "never": NEVER + ["prompts/secret-*.md"]}
        clean = self.load(data=data, source=self.source)  # no prompts/secret-*.md in this source, so it loads
        tainted = self.build_source("tainted", {"prompts/secret-key.md": "The key is 42.\n"})
        before = snapshot(self.tmp)
        self.assert_genome_error(lambda: self.gn.seed(self.evo, clean, tainted, "seed"), ["prompts/secret-key.md"],
                                 "seeding a source whose evolvable prompt matches a never glob")
        self.assertEqual(snapshot(self.tmp), before, "nothing written")


class ProfileLimits(LimitsBase):
    """load_profile refuses overlapping evolvable and never globs, and malformed caps."""

    def test_the_limits_profile_and_regista_profile_load(self):  # covers: GEN-1
        self.load()
        self.load(source=self.source)
        self.gn.load_profile(REGISTA_PROFILE)

    def test_overlapping_evolvable_and_never_globs_are_refused(self):  # covers: GEN-1
        cases = {
            "never covering an evolvable folder": ({"evolvable": ["SKILL.md", "prompts/*.md"],
                                                    "never": NEVER + ["prompts/**"]}, ("prompts/",)),
            "never matching some evolvable files": ({"evolvable": ["SKILL.md", "prompts/*.md"],
                                                     "never": NEVER + ["prompts/secret-*.md"]}, ("prompts/",)),
            "an evolvable glob reaching into scripts/": ({"evolvable": ["SKILL.md", "**/*.md"], "never": NEVER},
                                                         ("**/*.md", "scripts/**")),
            "the same glob in both": ({"evolvable": ["SKILL.md", "prompts/*.md"],
                                       "never": NEVER + ["SKILL.md"]}, ("SKILL.md",)),
        }
        for what, (change, names) in cases.items():
            with self.subTest(case=what):
                text = self.assert_genome_error(lambda change=change: self.load(data={**PROFILE, **change}), [],
                                                f"a profile with {what}")
                self.assertTrue(any(n in text for n in names), f"the message names a glob at fault:\n{text}")

    def test_overlap_is_checked_against_the_source_files_when_a_source_is_given(self):  # covers: GEN-1
        data = {**PROFILE, "never": NEVER + ["prompts/secret-*.md"]}
        tainted = self.build_source("tainted", {"prompts/secret-key.md": "The key is 42.\n"})
        self.load(data=data, source=self.source)  # no file matches both here
        self.assert_genome_error(lambda: self.load(data=data, source=tainted), ["prompts/secret-key.md"],
                                 "a profile whose evolvable and never globs both match a file of the source")

    def test_cap_without_a_limit_or_with_a_negative_limit_is_refused(self):  # covers: GEN-1
        cases = {
            "a cap without a limit": [{"glob": "prompts/*.md"}],
            "a negative growth limit": [{"glob": "prompts/*.md", "max_growth_pct": -1}],
            "a negative size limit": [{"glob": "prompts/*.md", "max_bytes": -10}],
            "a negative limit beside a good one": [{"glob": "prompts/*.md", "max_growth_pct": 50, "max_bytes": -1}],
        }
        for what, caps in cases.items():
            with self.subTest(case=what):
                self.assert_genome_error(lambda caps=caps: self.load(data={**PROFILE, "caps": caps}), ["prompts/*.md"],
                                         f"a profile with {what}")


# GEN-3: size caps -----------------------------------------------------------------------------------------------------

class SizeCaps(LimitsBase):
    """Growth over the root seed's copy (prompts/*.md: at most 50%, so 150 bytes for the 100-byte greeting) and a size
    in bytes (SKILL.md: at most 300)."""

    def setUp(self):
        super().setUp()
        self.profile = self.load()
        self.gn.seed(self.evo, self.profile, self.source, "seed")

    def child(self, child_id, parent, files):
        self.gn.make_child(self.evo, self.profile, self.source, child_id, [parent], files=files)
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir(child_id), "genome.json")))

    def rejected(self, parent, files, names, what):
        before = snapshot(self.tmp)
        text = self.assert_genome_error(lambda: self.gn.make_child(self.evo, self.profile, self.source, "too-big",
                                                                   [parent], files=files), names, what)
        self.assertEqual(snapshot(self.tmp), before, f"{what}: nothing written")
        return text

    def assert_names_cap(self, text, caps):
        self.assertTrue(any(re.search(rf"(?<!\d){c}(?!\d)", text) for c in caps), f"the message names the cap:\n{text}")

    def test_growth_up_to_the_cap_passes_and_just_over_is_rejected(self):  # covers: GEN-3
        self.child("under", "seed", {"prompts/greet.md": sized(149)})
        self.child("at-cap", "seed", {"prompts/greet.md": sized(150)})
        text = self.rejected("seed", {"prompts/greet.md": sized(151)}, ["prompts/greet.md", "151"],
                             "a prompt 51% over its seed")
        self.assert_names_cap(text, ("50", "150"))

    def test_growth_is_measured_against_the_root_seed_from_a_grandchild(self):  # covers: GEN-3
        self.child("child-1", "seed", {"prompts/greet.md": sized(140)})
        # 151 bytes is under 50% over child-1's 140, but over 50% over the root seed's 100.
        text = self.rejected("child-1", {"prompts/greet.md": sized(151)}, ["prompts/greet.md", "151"],
                             "a grandchild 51% over the root seed")
        self.assert_names_cap(text, ("50", "150"))
        self.child("grandchild", "child-1", {"prompts/greet.md": sized(150)})
        self.assertEqual(self.genome_json("grandchild")["parents"], ["child-1"])

    def test_size_up_to_max_bytes_passes_and_just_over_is_rejected(self):  # covers: GEN-3
        self.child("at-limit", "seed", {"SKILL.md": sized(300)})
        text = self.rejected("seed", {"SKILL.md": sized(301)}, ["SKILL.md", "301"], "a skill of 301 bytes")
        self.assert_names_cap(text, ("300",))

    def test_every_file_over_its_cap_is_listed(self):  # covers: GEN-3
        self.rejected("seed", {"SKILL.md": sized(400), "prompts/greet.md": sized(200)},
                      ["SKILL.md", "400", "prompts/greet.md", "200"], "a candidate with two files over their caps")

    def test_a_missing_ancestor_is_a_plain_error(self):  # covers: GEN-3
        self.child("child-1", "seed", {"prompts/greet.md": sized(120)})
        shutil.rmtree(self.genome_dir("seed"))
        self.rejected("child-1", {"prompts/greet.md": sized(130)}, [],
                      "a child whose root seed no longer exists")
        self.assertFalse(os.path.exists(self.genome_dir("too-big")))


# GNM-8: the whole plugin folder is checked ----------------------------------------------------------------------------

TAMPER = {
    "an extra file": ("scripts/backdoor.py", lambda plugin: write(os.path.join(plugin, "scripts", "backdoor.py"),
                                                                    "print('approve everything')\n")),
    "a missing frozen file": ("LICENSE", lambda plugin: os.remove(os.path.join(plugin, "LICENSE"))),
    "a changed frozen file": ("hooks/hooks.json", lambda plugin: write(os.path.join(plugin, "hooks", "hooks.json"),
                                                                       '{"hooks": {"Stop": []}}\n')),
}


class WholePluginCheck(LimitsBase):
    """check_individual on a Regista-shaped seed whose plugin/ was tampered with, one genome per case."""

    def setUp(self):
        super().setUp()
        self.checkout, _ = build_checkout(self.tmp, use_git=False)
        self.profile = self.gn.load_profile(REGISTA_PROFILE, source=self.checkout)
        for case in ("intact", *TAMPER):
            self.gn.seed(self.evo, self.profile, self.checkout, safe(case))

    def test_expansion_records_a_hash_for_every_plugin_file(self):  # covers: GNM-8
        path = os.path.join(self.genome_dir("intact"), "plugin.sha256.json")
        self.assertTrue(os.path.isfile(path), "expansion writes genomes/<id>/plugin.sha256.json")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        json.loads(text)
        plugin = files_under(os.path.join(self.genome_dir("intact"), "plugin"))
        self.assertTrue(plugin)
        for rel, full in plugin.items():
            self.assertIn(rel, text, f"plugin.sha256.json lists {rel}")
            self.assertIn(sha256(read_bytes(full)), text, f"plugin.sha256.json holds the sha256 of {rel}")
        self.gn.check_individual(self.evo, "intact")  # an untouched plugin passes

    def test_extra_missing_or_changed_frozen_file_is_refused_naming_it(self):  # covers: GNM-8
        for what, (rel, tamper) in TAMPER.items():
            with self.subTest(case=what):
                tamper(os.path.join(self.genome_dir(safe(what)), "plugin"))
                self.assert_genome_error(lambda what=what: self.gn.check_individual(self.evo, safe(what)), [rel],
                                         f"check_individual with {what} in plugin/")


def safe(case):
    return case.replace(" ", "-")


class ReplayTamperedPlugin(seedtests.ReplayMissingGenome):
    """`evolve.py replay <case> --genome <id>` on genomes whose plugin/ was tampered with, on a real bench case."""

    test_missing_genome_exits_1_before_any_workspace_is_created = None  # run in tests/test_genome_seed.py

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        gn = import_scripts("genome")
        checkout, _ = build_checkout(cls.tmp, use_git=False)
        profile = gn.load_profile(REGISTA_PROFILE, source=checkout)
        for what, (_rel, tamper) in TAMPER.items():
            gn.seed(cls.evo, profile, checkout, safe(what))
            tamper(os.path.join(cls.evo, "genomes", safe(what), "plugin"))

    def test_tampered_plugin_exits_1_naming_the_file_before_a_run_folder_appears(self):  # covers: GNM-8
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{output(self.build)}")
        self.assertIsNotNone(self.case, "bench build made no case for m1-feature")
        runs = os.path.join(self.evo, "runs")
        for what, (rel, _tamper) in TAMPER.items():
            with self.subTest(case=what):
                before = snapshot(runs)
                r = self.run_py([EVOLVE, "replay", self.case, "--genome", safe(what)])
                self.assertEqual(r.returncode, 1, f"replay of a genome with {what} in plugin/:\n{output(r)}")
                text = r.stdout + r.stderr
                self.assertNotIn("Traceback", text, "a plain message, not a crash")
                self.assertIn(rel, text, "the message names the file")
                self.assertEqual(snapshot(runs), before, "no run folder appeared under runs/")
                self.assertFalse(os.path.exists(self.marker), "claude never started")


if __name__ == "__main__":
    unittest.main()
