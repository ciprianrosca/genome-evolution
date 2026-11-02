"""The genome module, the seed genome and its expansion to a plugin folder (spec .regista/specs/genome-seed.json:
GEN-4, GNM-2, GNM-3, GNM-4, GNM-5; GNM-1 is the end-to-end test in genome/tests/e2e/test_genome_seed.py).

scripts/genome.py holds the genome code and knows nothing about Regista: everything target-specific comes from a
profile (Regista's is genome/profiles/regista.json). evolve.py adds the CLI: `genome seed [--id] [--profile] [--source]`,
`genome expand <id>`, `genome show <id>`, and genome_folder for `replay --genome <id>`.

Sources are small fake folders in a temporary folder: a Regista-shaped checkout seeded with Regista's real profile,
and a folder holding one standalone skill with its own profile. The evolver home (REGISTA_EVOLVE_HOME) and HOME are
temporary folders too; the repository is only read, and no bytecode is written into it.

Interface assumed: seed (CLI and genome.seed) also expands the genome to plugin/; genome.seed and genome.expand take
the object load_profile returns; load_genome and check_individual raise GenomeError naming the file on a mismatch;
genome_folder takes the evolver home and the id (or reads REGISTA_EVOLVE_HOME, which is set while it runs).

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_genome_seed -v"""
import ast
import hashlib
import importlib
import inspect
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
REGISTA_CHECKOUT = os.path.dirname(ROOT)  # the Regista checkout that holds the genome: reached only by path
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
SCRIPTS = os.path.join(ROOT, "scripts")
EVOLVE = os.path.join(SCRIPTS, "evolve.py")
GENOME_PY = os.path.join(SCRIPTS, "genome.py")
PROFILE = os.path.join(ROOT, "profiles", "regista.json")

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC"}

# The fake Regista checkout ------------------------------------------------------------------------------------------

VERSION = "9.8.7"
# Inside the bounds of Regista's profile (spec genome-traits), so seeding is allowed.
TRAITS = {"context.warn_pct": 50, "context.limit_pct": 70, "review.min_changed_lines": 25, "review.max_rounds": 4,
          "reviewer.model": "sonnet"}
CONFIG = {"project": "", "gates": [],
          "context": {"warn_pct": 50, "limit_pct": 70, "window": "auto"},
          "review": {"required_for": [], "min_changed_lines": 25, "max_rounds": 4}}

EVOLVABLE = {
    "agents/builder.md": "---\nname: builder\ndescription: Builds one step.\n---\n\nYou are the builder.\n",
    "agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews one step.\nmodel: sonnet\n---\n\nYou review.\n",
    "agents/test-writer.md": "---\nname: test-writer\ndescription: Writes tests.\n---\n\nYou write tests.\n",
    "skills/build/SKILL.md": "---\nname: build\ndescription: Build a step.\n---\n\n# Build\n\nDo the step.\n",
    "skills/review/SKILL.md": "---\nname: review\ndescription: Review.\n---\n\n# Review\n\nCheck it, într-adevăr.\n",
    "skills/x/SKILL.md": "---\nname: x\ndescription: The x skill.\n---\n\n# X\n",
}

FROZEN = {
    ".claude-plugin/plugin.json": json.dumps({"name": "regista", "version": VERSION,
                                              "description": "A fake Regista for tests."}, indent=2) + "\n",
    ".codex-plugin/plugin.json": json.dumps({"name": "regista", "version": VERSION}, indent=2) + "\n",
    "hooks/hooks.json": json.dumps({"hooks": {}}, indent=2) + "\n",
    "templates/config.json": json.dumps(CONFIG, indent=2) + "\n",
    "templates/zz-late.json": "{}\n",
    "skills/x/agents/openai.yaml": "interface:\n  display_name: X\n",
    "scripts/regista.py": "print('the fake rule engine')\n",
    "scripts/regista": "#!/bin/sh\nexec python3 \"$(dirname \"$0\")/regista.py\" \"$@\"\n",
    "LICENSE": "MIT License, for tests.\n",
    "README.md": "# Regista (fake)\n",
}

# Files the Regista profile excludes: they must never reach files/ or plugin/.
EXCLUDED = {
    "tests/test_something.py": "import unittest\n",
    "docs/notes.md": "# Notes\n",
    ".regista/config.json": "{}\n",
    ".claude/settings.json": "{}\n",
    "genome/other.json": "{}\n",
    "scripts/__pycache__/regista.cpython-312.pyc": "not really bytecode\n",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def git(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git", "-c", "user.name=Sample", "-c", "user.email=sample@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def files_under(root):
    """Every file under root, as {path relative to root with / separators: absolute path}."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            found[rel] = os.path.join(dirpath, name)
    return found


def snapshot(root, skip_git=False):
    """Every folder and file under root (contents included), or None when root doesn't exist."""
    if not os.path.lexists(root):
        return None
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if skip_git and rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for d in dirnames:
            snap[os.path.join(rel_dir, d) + "/"] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            snap[os.path.join(rel_dir, name)] = read_bytes(path) if not os.path.islink(path) else os.readlink(path)
    return snap


def make_writable(folder):
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                try:
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR * os.path.isdir(p))
                except OSError:
                    pass


def build_checkout(parent, name="checkout", changes=None, use_git=True):
    """A fake Regista checkout: evolvable files, the frozen layer and excluded folders. `changes` replaces files
    (a value of None removes the file). With use_git it is a git repository with one commit. Returns (root, head)."""
    root = os.path.join(parent, name)
    files = {**EVOLVABLE, **FROZEN, **EXCLUDED}
    for rel, text in (changes or {}).items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    for rel, text in files.items():
        write(os.path.join(root, rel), text)
    if not use_git:
        return root, None
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fake checkout")
    return root, git(root, "rev-parse", "HEAD")


def evolve_env(home, evolve_home):
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_PROJECT_DIR" and not k.startswith(("REGISTA_", "DAEDALUS_"))}
    env.update({"HOME": home, "REGISTA_EVOLVE_HOME": evolve_home, "PYTHONDONTWRITEBYTECODE": "1", **GIT_ENV})
    return env


def output(result):
    return f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def import_scripts(name):
    """Import scripts/<name>.py as a module (evolve.py imports genome from the same folder), writing no bytecode."""
    if not os.path.isfile(os.path.join(SCRIPTS, f"{name}.py")):
        raise AssertionError(f"scripts/{name}.py doesn't exist")
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = old


def is_plain_error(exc):
    return any(c.__name__ in ("GenomeError", "EvolveError") for c in type(exc).__mro__)


class GenomeFixture(unittest.TestCase):
    """A temporary HOME and evolver home, and a fake Regista checkout seeded with Regista's profile."""

    @classmethod
    def make_fixture(cls, prefix):
        cls.tmp = tempfile.mkdtemp(prefix=prefix)
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.evo = os.path.join(cls.tmp, "evolve-home")
        cls.checkout, cls.head = build_checkout(cls.tmp)
        cls.checkout_before = snapshot(cls.checkout, skip_git=True)

    @classmethod
    def tearDownClass(cls):
        make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def evolve(cls, *args):
        return subprocess.run([sys.executable, EVOLVE, *args], cwd=cls.tmp, capture_output=True, text=True,
                              encoding="utf-8", env=evolve_env(cls.home, cls.evo), timeout=300)

    @classmethod
    def seed(cls, genome_id=None, source=None):
        return cls.evolve("genome", "seed", *(["--id", genome_id] if genome_id else []),
                          "--profile", PROFILE, "--source", source or cls.checkout)

    def genome_dir(self, genome_id):
        return os.path.join(self.evo, "genomes", genome_id)

    def plugin(self, genome_id="seed"):
        return os.path.join(self.genome_dir(genome_id), "plugin")

    def genome_json(self, genome_id):
        with open(os.path.join(self.genome_dir(genome_id), "genome.json"), encoding="utf-8") as f:
            return json.load(f)

    def assert_seeded(self, result, genome_id):
        self.assertEqual(result.returncode, 0, f"genome seed --id {genome_id} failed:\n{output(result)}")
        self.assertTrue(os.path.isfile(os.path.join(self.genome_dir(genome_id), "genome.json")), output(result))

    def assert_plain_failure(self, result, what):
        self.assertEqual(result.returncode, 1, f"{what}: expected exit 1\n{output(result)}")
        text = result.stdout + result.stderr
        self.assertTrue(text.strip(), f"{what}: no message")
        self.assertNotIn("Traceback", text, f"{what}: a crash, not a plain message\n{text}")


class RegistaSeed(GenomeFixture):
    """`genome seed` with Regista's profile on the fake checkout: the default id, then --id alpha."""

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-seed-")
        cls.default = cls.seed()
        cls.alpha = cls.seed("alpha")

    def test_genome_json_records_no_parents_and_each_file_path_size_and_hash(self):  # covers: GEN-4
        self.assert_seeded(self.default, "seed")
        genome = self.genome_json("seed")
        self.assertEqual(genome.get("parents"), [], "the seed has no parents")
        listed = genome.get("files")
        self.assertIsInstance(listed, list)
        self.assertEqual([f.get("path") for f in listed], sorted(EVOLVABLE), "one entry per evolvable file, by path")
        for entry in listed:
            data = EVOLVABLE[entry["path"]].encode("utf-8")
            self.assertEqual(entry.get("bytes"), len(data), f"{entry['path']}: size in bytes")
            self.assertEqual(entry.get("sha256"), sha256(data), f"{entry['path']}: sha256")

    def test_files_holds_byte_identical_copies_of_exactly_the_evolvable_files(self):  # covers: GNM-2
        for genome_id, result in (("seed", self.default), ("alpha", self.alpha)):
            with self.subTest(genome=genome_id):
                self.assert_seeded(result, genome_id)
                stored = files_under(os.path.join(self.genome_dir(genome_id), "files"))
                self.assertEqual(sorted(stored), sorted(EVOLVABLE),
                                 "files/ holds exactly agents/*.md and skills/*/SKILL.md, and no frozen-layer file "
                                 "(scripts/, hooks/, manifests, templates/, skills/*/agents/) or excluded file")
                for rel, path in stored.items():
                    self.assertEqual(read_bytes(path), read_bytes(os.path.join(self.checkout, rel)),
                                     f"files/{rel} is a byte-identical copy")

    def test_genome_json_holds_profile_name_base_and_the_five_traits(self):  # covers: GNM-2
        self.assert_seeded(self.default, "seed")
        with open(PROFILE, encoding="utf-8") as f:
            profile_name = json.load(f)["name"]
        genome = self.genome_json("seed")
        self.assertEqual(genome.get("id"), "seed", "the default id is seed")
        self.assertEqual(self.genome_json("alpha").get("id"), "alpha")
        self.assertEqual(genome.get("version"), 1)
        self.assertEqual(genome.get("profile"), profile_name, "genome.json names the profile it was seeded with")
        self.assertTrue(genome.get("created_at"), "genome.json records when it was created")
        self.assertEqual(genome.get("base"), {"target_version": VERSION, "commit": self.head, "path": "."},
                         "base: the source's .claude-plugin/plugin.json version, its HEAD commit and its path in "
                         "the repository ('.', the top)")
        self.assertEqual(genome.get("traits"), TRAITS,
                         "the four numbers from templates/config.json and the reviewer's model from its front matter")

    def test_existing_id_is_refused_and_nothing_is_written(self):  # covers: GNM-2
        self.assert_seeded(self.default, "seed")
        before = snapshot(self.evo)
        again = self.seed()
        self.assert_plain_failure(again, "seeding an id that already exists")
        self.assertIn("seed", again.stdout + again.stderr, "the message names the id")
        self.assertEqual(snapshot(self.evo), before, "nothing in the evolver home changed")
        self.assertEqual(snapshot(self.checkout, skip_git=True), self.checkout_before, "the source is never written")

    def test_unsafe_ids_are_refused_and_nothing_is_written(self):  # covers: GNM-2
        self.assert_seeded(self.default, "seed")
        absolute = os.path.join(self.tmp, "abs-genome")
        for bad in ("../escape", "a/b", "..", absolute):
            with self.subTest(id=bad):
                before, around = snapshot(self.evo), snapshot(self.tmp)
                result = self.seed(bad)
                self.assert_plain_failure(result, f"seeding the unsafe id {bad!r}")
                self.assertEqual(snapshot(self.evo), before, "nothing in the evolver home changed")
                self.assertEqual(snapshot(self.tmp), around, "nothing appeared anywhere else either")
                self.assertFalse(os.path.exists(absolute))


class MissingTraitSource(GenomeFixture):
    """A source whose trait source is missing: a JSON key, a front-matter field, or the whole file."""

    CASES = {
        "a missing JSON key": ({"templates/config.json": json.dumps(
            {**CONFIG, "review": {"required_for": [], "min_changed_lines": 25}}, indent=2) + "\n"},
            ("review.max_rounds", "max_rounds")),
        "a missing front-matter field": ({"agents/reviewer.md": EVOLVABLE["agents/builder.md"].replace(
            "builder", "reviewer")}, ("reviewer.model", "model")),
        "a missing file": ({"templates/config.json": None}, ("templates/config.json", "config.json", "warn_pct")),
    }

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-trait-")
        cls.sources = {what: build_checkout(cls.tmp, f"source-{n}", changes, use_git=False)[0]
                       for n, (what, (changes, _names)) in enumerate(cls.CASES.items())}

    def test_missing_trait_source_exits_1_naming_it_and_writes_nothing(self):  # covers: GNM-2
        for what, (_changes, names) in self.CASES.items():
            with self.subTest(case=what):
                before = snapshot(self.evo)
                result = self.seed("trait-case", source=self.sources[what])
                self.assert_plain_failure(result, f"seeding from a source with {what}")
                text = result.stdout + result.stderr
                self.assertTrue(any(n in text for n in names), f"the message names the trait or file:\n{text}")
                self.assertEqual(snapshot(self.evo), before, "nothing in the evolver home changed")
                self.assertFalse(os.path.exists(self.genome_dir("trait-case")))


class RegistaExpand(GenomeFixture):
    """The plugin folder a seed expands to, and `genome expand` rebuilding it."""

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-expand-")
        cls.result = cls.seed()

    def test_plugin_holds_the_frozen_layer_and_the_genome_files_and_nothing_excluded(self):  # covers: GNM-3
        self.assert_seeded(self.result, "seed")
        self.assertTrue(os.path.isdir(self.plugin()), "seeding expands the genome to plugin/")
        present = files_under(self.plugin())
        expected = {rel: text.encode("utf-8") for rel, text in {**FROZEN, **EVOLVABLE}.items()}
        self.assertEqual(sorted(present), sorted(expected),
                         "plugin/ is the frozen layer plus the evolvable files, and nothing the profile excludes "
                         "(tests/, docs/, .regista/, .git/, .claude/, genome/, __pycache__/)")
        for rel, data in expected.items():
            self.assertEqual(read_bytes(present[rel]), data, f"plugin/{rel} unchanged")
        for name in ("tests", "docs", ".regista", ".git", ".claude", "genome", "scripts/__pycache__"):
            self.assertFalse(os.path.lexists(os.path.join(self.plugin(), name)), f"plugin/ has no {name}")


class RegistaExpandRebuild(GenomeFixture):
    """`genome expand` after the source changed. It edits its fake checkout, so it has its own fixture and
    doesn't depend on the order the tests run in."""

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-expand-rebuild-")
        cls.result = cls.seed()

    def test_expand_uses_the_genome_files_over_the_source_and_rebuilds_the_folder(self):  # covers: GNM-3
        self.assert_seeded(self.result, "seed")
        builder = os.path.join(self.checkout, "agents", "builder.md")
        hooks = os.path.join(self.checkout, "hooks", "hooks.json")
        new_hooks = b'{"hooks": {"Stop": []}}\n'
        stray = os.path.join(self.plugin(), "agents", "stray.md")
        write(builder, "You are a changed builder in the source.\n")
        with open(hooks, "wb") as f:
            f.write(new_hooks)
        write(stray, "left over from an earlier expansion\n")
        r = self.evolve("genome", "expand", "seed", "--profile", PROFILE, "--source", self.checkout)
        self.assertEqual(r.returncode, 0, f"genome expand failed:\n{output(r)}")
        self.assertEqual(read_bytes(os.path.join(self.plugin(), "agents", "builder.md")),
                         EVOLVABLE["agents/builder.md"].encode("utf-8"),
                         "the genome's own file replaces the source's evolvable file")
        self.assertEqual(read_bytes(os.path.join(self.plugin(), "hooks", "hooks.json")), new_hooks,
                         "the frozen layer is copied from the source as it is now")
        self.assertFalse(os.path.exists(stray), "expand rebuilds plugin/ rather than adding to it")


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads unreadable files, so the copy can't fail")
class RegistaExpandFailure(GenomeFixture):
    """An unreadable frozen file in the source makes `genome expand` fail part-way."""

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-expand-fail-")
        cls.result = cls.seed()

    def failing_expand(self):
        late = os.path.join(self.checkout, "templates", "zz-late.json")
        os.chmod(late, 0)
        try:
            return self.evolve("genome", "expand", "seed", "--profile", PROFILE, "--source", self.checkout)
        finally:
            os.chmod(late, stat.S_IRUSR | stat.S_IWUSR)

    def test_failed_expansion_leaves_the_previous_plugin_or_none(self):  # covers: GNM-3
        self.assert_seeded(self.result, "seed")
        self.assertTrue(os.path.isdir(self.plugin()), "seeding expands the genome to plugin/")
        for previous in ("an earlier plugin/", "no plugin/"):
            with self.subTest(previous=previous):
                if previous == "no plugin/":
                    shutil.rmtree(self.plugin())
                before_plugin = snapshot(self.plugin())
                before_genome = sorted(os.listdir(self.genome_dir("seed")))
                r = self.failing_expand()
                self.assertNotEqual(r.returncode, 0, f"expansion with an unreadable file should fail:\n{output(r)}")
                self.assertNotIn("Traceback", r.stdout + r.stderr, "a plain message, not a crash")
                self.assertEqual(snapshot(self.plugin()), before_plugin, f"{previous} left exactly as it was")
                self.assertEqual(sorted(os.listdir(self.genome_dir("seed"))), before_genome,
                                 "no half-built plugin folder left beside it")


class GenomeChecks(GenomeFixture):
    """load_genome, check_individual and genome_folder on intact and tampered genomes, one genome per case."""

    CASES = ("intact", "changed", "resized", "missing", "unlisted", "expanded", "folder-tampered")

    @classmethod
    def setUpClass(cls):
        cls.make_fixture("regista-genome-checks-")
        cls.seeds = {case: cls.seed(case) for case in cls.CASES}

    def setUp(self):
        for case, result in self.seeds.items():
            self.assert_seeded(result, case)
        self.gn = import_scripts("genome")

    def stored(self, case, rel):
        return os.path.join(self.genome_dir(case), "files", rel)

    def assert_refused_naming(self, call, rel, what):
        with self.assertRaises(Exception, msg=what) as caught:
            call()
        self.assertTrue(is_plain_error(caught.exception), f"{what}: a plain GenomeError, got {caught.exception!r}")
        self.assertIn(rel, str(caught.exception), f"{what}: the message names the file")

    def genome_folder(self, genome_id):
        ev = import_scripts("evolve")
        fn = getattr(ev, "genome_folder", None)
        self.assertTrue(callable(fn), "evolve.py has no genome_folder")
        names = [p.name for p in inspect.signature(fn).parameters.values()
                 if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        with mock.patch.dict(os.environ, {"REGISTA_EVOLVE_HOME": self.evo, "HOME": self.home}):
            if len(names) == 1:
                return fn(genome_id)
            if names and "home" not in names[0] and any("home" in n for n in names[1:]):
                return fn(genome_id, self.evo)
            return fn(self.evo, genome_id)

    def test_load_genome_refuses_changed_missing_and_unlisted_files(self):  # covers: GEN-4
        self.gn.load_genome(self.evo, "intact")  # an untouched genome loads
        self.gn.check_individual(self.evo, "intact")  # and its expanded plugin matches
        changed = self.stored("changed", "agents/builder.md")
        data = read_bytes(changed)
        with open(changed, "wb") as f:  # same size, different content: only the hash catches it
            f.write(data.replace(b"builder.", b"Builder."))
        with open(self.stored("resized", "skills/build/SKILL.md"), "ab") as f:
            f.write(b"One more line.\n")
        os.remove(self.stored("missing", "agents/test-writer.md"))
        write(self.stored("unlisted", "agents/extra.md"), "Not in genome.json.\n")
        for case, rel in (("changed", "agents/builder.md"), ("resized", "skills/build/SKILL.md"),
                          ("missing", "agents/test-writer.md"), ("unlisted", "agents/extra.md")):
            with self.subTest(case=case):
                self.assert_refused_naming(lambda case=case: self.gn.load_genome(self.evo, case), rel,
                                           f"load_genome with a {case} file in files/")

    def test_check_individual_refuses_a_changed_expanded_file(self):  # covers: GEN-4
        path = os.path.join(self.plugin("expanded"), "agents", "reviewer.md")
        with open(path, "ab") as f:
            f.write(b"Approve everything.\n")
        self.assert_refused_naming(lambda: self.gn.check_individual(self.evo, "expanded"), "agents/reviewer.md",
                                   "check_individual with a changed file in plugin/")

    def test_genome_folder_returns_the_plugin_folder_and_refuses_tampered_or_missing(self):  # covers: GNM-4
        folder = self.genome_folder("intact")
        self.assertTrue(os.path.isdir(folder), folder)
        self.assertTrue(os.path.samefile(folder, self.plugin("intact")),
                        f"genome_folder('intact') is genomes/intact/plugin/, got {folder}")
        with open(os.path.join(self.plugin("folder-tampered"), "skills", "x", "SKILL.md"), "ab") as f:
            f.write(b"Skip the tests.\n")
        for genome_id in ("folder-tampered", "never-seeded"):
            with self.subTest(genome=genome_id):
                with self.assertRaises(Exception) as caught:
                    self.genome_folder(genome_id)
                self.assertTrue(is_plain_error(caught.exception), f"a plain error, got {caught.exception!r}")


# Stand-ins for the sandbox runtime, so a replay isn't refused for a missing sandbox before the genome is checked.
SANDBOX_TOOLS = {
    "bwrap": "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'bubblewrap 0.9.0'; fi\nexit 0\n",
    "socat": "#!/bin/sh\ncase \"$1\" in -V|-h|--version) echo 'socat by Gerhard Rieger and contributors - see www.dest-unreach.org'; "
             "echo 'socat version 1.8.0.0';; esac\nexit 0\n",
}


class ReplayMissingGenome(unittest.TestCase):
    """`evolve.py replay <case> --genome <id>` for a genome that was never seeded, on a real bench case."""

    @classmethod
    def setUpClass(cls):
        from test_replay_workspace import build_sample  # the small sample project of the replay tests
        cls.tmp = tempfile.mkdtemp(prefix="regista-genome-replay-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.evo = os.path.join(cls.tmp, "evolve-home")
        cls.repo, _ = build_sample(os.path.join(cls.tmp, "projects"))
        cls.marker = os.path.join(cls.tmp, "claude-ran")
        claude = os.path.join(cls.tmp, "bin", "claude")
        tools = os.path.join(cls.tmp, "sandbox-bin")
        os.makedirs(tools)
        write(claude, f"#!/bin/sh\ntouch {shlex.quote(cls.marker)}\nexit 1\n")
        os.chmod(claude, 0o755)
        for name, text in SANDBOX_TOOLS.items():
            write(os.path.join(tools, name), text)
            os.chmod(os.path.join(tools, name), 0o755)
        os.makedirs(cls.evo)
        with open(os.path.join(cls.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump({"holdout_pct": 0, "claude_command": claude, "replay_timeout_minutes": 1}, f)
        cls.env = evolve_env(cls.home, cls.evo)
        cls.env["PATH"] = tools + os.pathsep + cls.env.get("PATH", "")
        cls.build = cls.run_py([EVOLVE, "bench", "build", cls.repo.root])
        cls.case = None
        folder = os.path.join(cls.evo, "bench", "sample")
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                if name.endswith(".json"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        if json.load(f).get("step") == "m1-feature":
                            cls.case = name[:-5]

    @classmethod
    def run_py(cls, argv):
        return subprocess.run([sys.executable, *argv], cwd=cls.tmp, capture_output=True, text=True,
                              encoding="utf-8", env=cls.env, timeout=300)

    @classmethod
    def tearDownClass(cls):
        make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_missing_genome_exits_1_before_any_workspace_is_created(self):  # covers: GNM-4
        self.assertEqual(self.build.returncode, 0, f"bench build failed:\n{output(self.build)}")
        self.assertIsNotNone(self.case, "bench build made no case for m1-feature")
        runs = os.path.join(self.evo, "runs")
        before = snapshot(runs)
        r = self.run_py([EVOLVE, "replay", self.case, "--genome", "never-seeded"])
        self.assertEqual(r.returncode, 1, f"replay of a missing genome:\n{output(r)}")
        text = r.stdout + r.stderr
        self.assertNotIn("Traceback", text, "a plain message, not a crash")
        self.assertIn("never-seeded", text, "the message names the missing genome")
        self.assertEqual(snapshot(runs), before, "no workspace folder appeared under runs/")
        self.assertFalse(os.path.exists(self.marker), "claude never started")


# A target that isn't Regista: one standalone skill with its own profile ---------------------------------------------

SKILL = "---\nname: haiku-writer\ndescription: Writes haiku.\ntone: warm\n---\n\n# Haiku writer\n\nWrite a haiku.\n"
SKILL_FROZEN = {
    "settings.json": json.dumps({"version": "0.3.0", "limits": {"depth": 3, "width": 9}}, indent=2) + "\n",
    "scripts/count.sh": "#!/bin/sh\nwc -w \"$1\"\n",
    "README.md": "# Haiku writer\n",
}
SKILL_EXCLUDED = {"tests/test_count.sh": "#!/bin/sh\nexit 0\n", "notes/draft.md": "A draft.\n"}
SKILL_PROFILE = {
    "name": "haiku-skill",
    "evolvable": ["SKILL.md"],
    "exclude": ["tests/**", "notes/**"],
    "version": {"file": "settings.json", "key": "version"},
    # Every trait has bounds (spec genome-traits).
    "traits": {"limits.depth": {"json": "settings.json", "key": "limits.depth", "min": 1, "max": 5, "step": 1},
               "skill.tone": {"front_matter": "SKILL.md", "field": "tone", "choices": ["warm", "dry"]}},
}


class StandaloneSkillGenome(unittest.TestCase):
    """genome.py seeding and expanding a standalone skill from its own profile, called as a module."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="regista-genome-skill-")
        cls.evo = os.path.join(cls.tmp, "evolve-home")
        cls.source = os.path.join(cls.tmp, "haiku-skill")
        for rel, text in {"SKILL.md": SKILL, **SKILL_FROZEN, **SKILL_EXCLUDED}.items():
            write(os.path.join(cls.source, rel), text)
        cls.profile_path = os.path.join(cls.tmp, "profiles", "haiku-skill.json")
        write(cls.profile_path, json.dumps(SKILL_PROFILE, indent=2) + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_seed_and_expand_build_the_genome_and_individual_from_the_profile_alone(self):  # covers: GNM-5
        gn = import_scripts("genome")
        profile = gn.load_profile(self.profile_path)
        gn.seed(self.evo, profile, self.source, "solo")
        gn.expand(self.evo, "solo", profile, self.source)
        folder = os.path.join(self.evo, "genomes", "solo")
        with open(os.path.join(folder, "genome.json"), encoding="utf-8") as f:
            genome = json.load(f)
        data = SKILL.encode("utf-8")
        self.assertEqual(genome.get("id"), "solo")
        self.assertEqual(genome.get("profile"), "haiku-skill")
        self.assertEqual(genome.get("parents"), [])
        self.assertEqual(genome.get("base"), {"target_version": "0.3.0", "commit": None},
                         "the version from settings.json; no commit, because the source isn't a git repository")
        self.assertEqual(genome.get("files"), [{"path": "SKILL.md", "sha256": sha256(data), "bytes": len(data)}])
        self.assertEqual(genome.get("traits"), {"limits.depth": 3, "skill.tone": "warm"},
                         "one trait from a JSON file, one from front matter")
        self.assertEqual(sorted(files_under(os.path.join(folder, "files"))), ["SKILL.md"])
        plugin = files_under(os.path.join(folder, "plugin"))
        expected = {"SKILL.md": SKILL, **SKILL_FROZEN}
        self.assertEqual(sorted(plugin), sorted(expected), "the frozen layer plus SKILL.md, without tests/ or notes/")
        for rel, text in expected.items():
            self.assertEqual(read_bytes(plugin[rel]), text.encode("utf-8"), f"plugin/{rel}")
        gn.load_genome(self.evo, "solo")
        gn.check_individual(self.evo, "solo")

    def test_genome_py_imports_neither_evolver_nor_rule_engine_and_names_nothing_of_regista(self):  # covers: GNM-5
        self.assertTrue(os.path.isfile(GENOME_PY), "scripts/genome.py doesn't exist")
        with open(GENOME_PY, encoding="utf-8") as f:
            source = f.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
                if node.level:
                    imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                    "import_module", "__import__", "spec_from_file_location"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        imported.add(os.path.basename(arg.value).split(".")[0])
        for module in ("evolve", "regista"):
            self.assertNotIn(module, imported, f"genome.py imports {module}")
        lowered = source.lower()
        for name in ("regista", "agents/", "skills/", "warn_pct", "limit_pct", "min_changed_lines", "max_rounds",
                     "reviewer", ".claude-plugin", "plugin.json", "templates/"):
            self.assertNotIn(name.lower(), lowered, f"genome.py mentions the Regista-specific name {name!r}")


if __name__ == "__main__":
    unittest.main()
