"""The adapter contract and the core module (spec .regista/specs/adapter-core.json: ADP-1, ADP-3, ADP-5, ADP-7).

scripts/evolve_core.py holds the target-neutral generation loop behind an adapter: evolve_core.Adapter (name, axes,
labels, must_not_worsen; cases, run, values, score, failures, estimate), check_adapter(adapter) -> (screening,
holdout) or AdapterError, mutate(..., reflect) with a pluggable reflection that may raise ReflectionFailed or
ReflectionLimit, new_state(...) and Generation(home, config, state, adapter, profile, source, population, mutate,
screening=None, holdout=None). scripts/evolve.py keeps the command line and becomes the home of RegistaAdapter.

Most tests here drive the core with a toy adapter written in this file, so no claude runs and no tokens are spent:
- The target is a temporary folder with one evolvable text file, prompt.md, and a frozen settings.json, described by
  a small profile with no traits, seeded as `seed` with genome.seed.
- The toy adapter's run writes folder/result.json from a table of planned outcomes and axis values per (genome role,
  case kind): role "base" for the seed, 1, 2, ... for the children in the order they were made; kind "screen" or
  "holdout". Its axes are passed (higher), errors (lower) and tokens (lower); a winner must not worsen passed or errors.
  Each planned result names how many failures adapter.failures lists for it.
- The core's mutate argument is a function here that calls evolve_core.mutate(..., reflect=<stand-in>). The stand-in
  reflect returns a reply genome.parse_reflection accepts (a ```json block with diagnosis and text, as
  tests/test_reflect_mutate.py builds it) with a distinct rewrite of prompt.md each time, a near-copy of the seed
  when told to, or raises ReflectionFailed / ReflectionLimit.
The Regista tests (ADP-5, and the command's refusal of a broken adapter) reuse tests/test_generation.py's fixture,
imported as a module: evolve.main in-process with a fake replay_run and the real mutate around a stand-in claude.
REGISTA_EVOLVE_HOME and HOME are temporary folders.

Interface assumed:
- config is evolve.load_config(home) (a dict; its `population` is the number of children); the light settings are the
  same dict with a smaller population and a shorter holdout list passed to Generation.
- The core writes runs/<generation id>/generation.json with today's keys (screening, holdout, front, children,
  discarded, winner, why, stopped, spent {replays, reflections, total}); a stopped generation is continued by a new
  Generation built from that file's content.
- A run's tokens are result.json tokens.used; a reflection's are the `used` of the tokens dict reflect returns (or of
  the exception's .tokens), which the stand-in keeps equal to input + output tokens, with no cache tokens.
- adapter.failures(case, genome, folder, result) gets the case dict; mutation.json's `failure` is the failure dict.
- An AdapterError's message is one or two plain sentences that name the adapter (when its name is usable) and the
  offending axis or case id.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_adapter_core -v"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_genome_seed as gs  # noqa: E402  (imported as modules, so their test classes don't run twice here)
import test_generation as tg  # noqa: E402

GEN = "toy-gen"
NAME = "toy-helper"
TARGET = "prompt.md"
LAST_LINE = "Keep each answer short.\n"
SEED_PROMPT = "You answer questions.\n" + LAST_LINE
# Each child's prompt replaces the seed's last line with one of these. Every pair (and each against the seed) differs
# by far more than the default near-duplicate limit of 10 changed characters, and none grows the file much.
REWRITES = ["Check every fact before replying.\n", "Quote the question in each reply.\n",
            "Say so when you are unsure of it.\n", "Give one example for each claim.\n",
            "Use plain words and never jargon.\n", "End with a one-line summary now.\n"]
NEAR_COPY = SEED_PROMPT.replace("short.", "short!")  # one character from the seed
PROFILE = {"name": NAME, "evolvable": [TARGET], "version": {"file": "settings.json", "key": "version"},
           "traits": {}}
AXES = {"passed": "higher", "errors": "lower", "tokens": "lower"}
LABELS = {"passed": "cases passed", "errors": "errors found"}
MUST_NOT_WORSEN = ("passed", "errors")
RUN_ESTIMATE = 10000
REFLECTION_USED = 1500
BIG_BUDGET = 10 ** 10
PAUSED = "paused at the usage limit"
MUTATE_ARGS = ("home", "config", "profile", "source", "parent", "failure", "target", "child_id")
REGISTA_AXES = [("requirements", "higher"), ("approved", "higher"), ("review_rounds", "lower"), ("findings", "lower"),
                ("tokens", "lower")]

# Planned results: (role, kind) -> what the toy run writes. A child's entry falls back to ("child", kind).
DEFAULT_PLAN = {
    ("base", "screen"): {"passed": 0.5, "errors": 2, "used": 10000, "score": 0.5, "failures": 2},
    ("child", "screen"): {"passed": 1.0, "errors": 0, "used": 10000, "score": 0.9, "failures": 0},
    ("base", "holdout"): {"passed": 0.5, "errors": 2, "used": 10000, "score": 0.5, "failures": 0},
    ("child", "holdout"): {"passed": 1.0, "errors": 0, "used": 10000, "score": 0.9, "failures": 0},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def files_under(root):
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        found.extend(os.path.join(dirpath, n) for n in filenames)
    return found


def case_id(case):
    return case.get("id") if isinstance(case, dict) else str(case)


def with_tokens(cls, message, tokens):
    """An instance of cls (ReflectionFailed or ReflectionLimit) carrying tokens, whatever its constructor takes."""
    try:
        exc = cls(message, tokens=tokens)
    except TypeError:
        exc = cls(message)
    exc.tokens = tokens
    return exc


def toy_adapter_class(core):
    class ToyAdapter(core.Adapter):
        """Runs nothing: run writes result.json from the harness's plan."""

        def __init__(self, harness, cases, name=NAME, axes=None, labels=None, must_not_worsen=MUST_NOT_WORSEN):
            self.harness = harness
            self._cases = cases
            self.name = name
            self.axes = dict(AXES) if axes is None else axes
            self.labels = dict(LABELS) if labels is None else labels
            self.must_not_worsen = must_not_worsen

        def cases(self):
            return self._cases

        def run(self, case, genome, individual, folder, resume=None):
            self.harness.toy_run(case, genome, individual, folder, resume)

        def values(self, result):
            return dict(result["values"])

        def score(self, result):
            return result["score"]

        def failures(self, case, genome, folder, result):
            cid = case_id(case)
            return [{"id": f"{cid}/problem-{k}", "kind": "gate_failure", "case": cid,
                     "summary": f"Check {k} failed on {cid}.",
                     "evidence": f"marker-{cid}-{k}: the check wanted a short, sourced answer."}
                    for k in range(1, int(result.get("failures") or 0) + 1)]

        def estimate(self, home):
            return RUN_ESTIMATE

    return ToyAdapter


class Base(unittest.TestCase):
    """A temporary evolver home with the toy target seeded as `seed`, the toy adapter and the stand-in reflect."""

    def setUp(self):
        self.gn = gs.import_scripts("genome")
        self.ev = gs.import_scripts("evolve")
        self.tmp = tempfile.mkdtemp(prefix="regista-adapter-core-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home_dir = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve-home")
        os.makedirs(self.home_dir)
        os.makedirs(self.evo)
        env = mock.patch.dict(os.environ, {"HOME": self.home_dir, "REGISTA_EVOLVE_HOME": self.evo})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.source = os.path.join(self.tmp, "toy-target")
        write(os.path.join(self.source, TARGET), SEED_PROMPT)
        write(os.path.join(self.source, "settings.json"), json.dumps({"version": "1.0.0"}) + "\n")
        profile_path = os.path.join(self.tmp, "profiles", f"{NAME}.json")
        write(profile_path, json.dumps(PROFILE, indent=2) + "\n")
        self.profile = self.gn.load_profile(profile_path, source=self.source)
        self.gn.seed(self.evo, self.profile, self.source, "seed")
        write(os.path.join(self.evo, "evolve.json"), json.dumps({"population": 2, "require_sandbox": False}) + "\n")
        self.config = self.ev.load_config(self.evo)
        self.core = gs.import_scripts("evolve_core")
        self.Toy = toy_adapter_class(self.core)
        self.runs = []  # every toy run: {case, genome, role, kind, individual, folder, resume, text, paused, run}
        self.mutations = []  # every mutate call: {parent, failure, target, child, run}
        self.reflections = []  # every reflect call: {prompt, run}
        self.made = []  # the children made, in order
        self.plan = dict(DEFAULT_PLAN)
        self.pause = None  # (role, kind, nth) -> True to pause that run
        self.reflect_plan = {}  # reflect call number -> "near", "failed" or "limit"
        self.reflection_used = REFLECTION_USED
        self.run_number = 0
        self.set_cases()

    # Fixture pieces -------------------------------------------------------------------------------------------------

    def set_cases(self, screening=2, holdout=2):
        self.screening = [{"id": f"s{i}", "prompt": f"Question {i}"} for i in range(1, screening + 1)]
        self.holdout = [{"id": f"h{i}", "prompt": f"Held-out question {i}"} for i in range(1, holdout + 1)]
        self.kind = {**{c["id"]: "screen" for c in self.screening}, **{c["id"]: "holdout" for c in self.holdout}}

    def adapter(self, **overrides):
        cases = overrides.pop("cases", {"screening": list(self.screening), "holdout": list(self.holdout)})
        return self.Toy(self, cases, **overrides)

    def role(self, genome):
        if genome == "seed":
            return "base"
        return self.made.index(genome) + 1 if genome in self.made else genome

    def toy_run(self, case, genome, individual, folder, resume):
        cid = case_id(case)
        role, kind = self.role(genome), self.kind.get(cid)
        nth = 1 + sum(1 for r in self.runs if r["role"] == role and r["kind"] == kind and not r["resume"])
        prompt_file = os.path.join(str(individual), TARGET)
        text = None
        if os.path.isfile(prompt_file):
            with open(prompt_file, encoding="utf-8") as f:
                text = f.read()
        call = {"case": cid, "genome": genome, "role": role, "kind": kind, "individual": individual,
                "folder": folder, "resume": resume, "text": text, "paused": False, "run": self.run_number}
        self.runs.append(call)
        os.makedirs(folder, exist_ok=True)
        if self.pause and not resume and self.pause(role, kind, nth):
            call["paused"] = True
            result = {"outcome": "paused", "case": cid, "genome": genome, "tokens": {"used": 500},
                      "note": f"paused-marker-{cid}-{genome}"}
        else:
            spec = self.plan.get((role, kind)) or self.plan[("base" if role == "base" else "child", kind)]
            result = {"outcome": spec.get("outcome", "done"), "case": cid, "genome": genome,
                      "tokens": {"used": spec["used"]},
                      "values": {"passed": spec["passed"], "errors": spec["errors"], "tokens": spec["used"]},
                      "score": spec["score"], "failures": spec["failures"]}
        write(os.path.join(folder, "result.json"), json.dumps(result, indent=2) + "\n")

    def tokens(self, used):
        return {"input_tokens": used - 500, "output_tokens": 500, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "used": used}

    def reflect(self, prompt):
        n = len(self.reflections) + 1
        self.reflections.append({"prompt": prompt, "run": self.run_number})
        what = self.reflect_plan.get(n)
        if what == "failed":
            raise with_tokens(self.core.ReflectionFailed, "The reflection ended without a JSON reply.",
                              self.tokens(self.reflection_used))
        if what == "limit":
            raise with_tokens(self.core.ReflectionLimit, "The usage limit was reached during the reflection.",
                              self.tokens(self.reflection_used))
        text = NEAR_COPY if what == "near" else SEED_PROMPT.replace(LAST_LINE, REWRITES[len(self.made)])
        body = json.dumps({"diagnosis": f"The prompt never asked for checks, so answer {n} went wrong.",
                           "text": text}, indent=2)
        return f"Here is my analysis.\n\n```json\n{body}\n```\n", "stand-in-model", self.tokens(self.reflection_used)

    def mutate(self, *args, **kwargs):
        b = {**dict(zip(MUTATE_ARGS, args)), **kwargs}
        self.mutations.append({"parent": b["parent"], "failure": b["failure"]["id"], "target": b["target"],
                               "child": b["child_id"], "run": self.run_number})
        made = self.core.mutate(b["home"], b["config"], b["profile"], b["source"], b["parent"], b["failure"],
                                b["target"], b["child_id"], reflect=self.reflect)
        self.made.append(b["child_id"])
        return made

    def generation(self, adapter=None, budget=BIG_BUDGET, config=None, state=None, **lists):
        """Build a Generation over the toy adapter and run it; returns run()'s stop reason."""
        self.run_number += 1
        adapter = adapter or self.adapter()
        config = self.config if config is None else config
        if state is None:
            state = self.core.new_state(GEN, adapter.name, TARGET, "seed", budget, 7)
        gen = self.core.Generation(self.evo, config, state, adapter, self.profile, self.source,
                                   config["population"], self.mutate, **lists)
        return gen.run()

    def state(self):
        path = os.path.join(self.evo, "runs", GEN, "generation.json")
        self.assertTrue(os.path.isfile(path), "runs/<generation>/generation.json is written")
        return read_json(path)

    def mutation(self, child):
        return read_json(os.path.join(self.evo, "genomes", child, "mutation.json"))

    def base_failures(self, case):
        """The failures the toy adapter lists for the base on one screening case, in its order."""
        n = self.plan[("base", "screen")]["failures"]
        return [f"{case}/problem-{k}" for k in range(1, n + 1)]

    def calls(self, run=None):
        return [(r["case"], r["genome"]) for r in self.runs if run is None or r["run"] == run]

    def finished(self, run):
        return {(r["case"], r["genome"]) for r in self.runs if r["run"] == run and not r["paused"]}


# ADP-1: the adapter contract ----------------------------------------------------------------------------------------

class AdapterContract(Base):
    def assert_refused(self, adapter, mention, label):
        with self.assertRaises(self.core.AdapterError, msg=f"{label}: check_adapter refuses it") as caught:
            self.core.check_adapter(adapter)
        message = str(caught.exception).strip()
        self.assertTrue(message, f"{label}: the error has a message")
        self.assertNotIn("Traceback", message)
        self.assertLessEqual(len(message), 400, f"{label}: one or two sentences: {message!r}")
        self.assertNotRegex(message, r"^\s*[\[{]", f"{label}: plain words, not a data dump: {message!r}")
        if mention:
            self.assertRegex(message, mention, f"{label}: the message names what is wrong: {message!r}")
        if isinstance(adapter.name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", adapter.name):
            self.assertIn(adapter.name, message, f"{label}: the message names the adapter: {message!r}")

    def test_a_well_formed_adapter_gives_its_two_case_lists(self):  # covers: ADP-1
        screening, holdout = self.core.check_adapter(self.adapter())
        self.assertEqual([case_id(c) for c in screening], ["s1", "s2"])
        self.assertEqual([case_id(c) for c in holdout], ["h1", "h2"])
        self.assertTrue(issubclass(self.Toy, self.core.Adapter))

    def test_each_broken_adapter_is_refused_with_a_plain_message(self):  # covers: ADP-1
        s = [{"id": "s1"}, {"id": "s2"}]
        h = [{"id": "h1"}, {"id": "h2"}]
        broken = [
            ("a name with a slash", {"name": "toy/../helper"}, r"(?i)name"),
            ("an empty name", {"name": ""}, r"(?i)name"),
            ("no axes", {"axes": {}}, r"(?i)ax[ie]s"),
            ("a direction other than higher or lower", {"axes": {"passed": "higher", "errors": "down"}},
             r"errors"),
            ("must_not_worsen names an axis it doesn't have", {"must_not_worsen": ("passed", "speed")}, r"speed"),
            ("cases() isn't a dict", {"cases": s + h}, r"(?i)case|screening|held[- ]?out|holdout"),
            ("no held-out list", {"cases": {"screening": s}}, r"(?i)held[- ]?out|holdout"),
            ("no screening list", {"cases": {"holdout": h}}, r"(?i)screening"),
            ("a case without an id", {"cases": {"screening": [{"prompt": "x"}, {"id": "s2"}], "holdout": h}},
             r"(?i)\bid\b"),
            ("an id with a path in it", {"cases": {"screening": [{"id": "../escape"}, {"id": "s2"}], "holdout": h}},
             re.escape("../escape")),
            ("an id starting with a dot", {"cases": {"screening": s, "holdout": [{"id": ".hidden"}]}},
             re.escape(".hidden")),
            ("an id with a space", {"cases": {"screening": [{"id": "s 1"}], "holdout": h}}, re.escape("s 1")),
            ("an id repeated in screening", {"cases": {"screening": [{"id": "s1"}, {"id": "s1"}], "holdout": h}},
             r"\bs1\b"),
            ("an id repeated in the held-out list",
             {"cases": {"screening": s, "holdout": [{"id": "h1"}, {"id": "h1"}]}},
             r"\bh1\b"),
            ("an id in both lists", {"cases": {"screening": s, "holdout": [{"id": "s2"}, {"id": "h1"}]}}, r"\bs2\b"),
            ("an empty screening list", {"cases": {"screening": [], "holdout": h}}, r"(?i)screening"),
            ("an empty held-out list", {"cases": {"screening": s, "holdout": []}}, r"(?i)held[- ]?out|holdout"),
        ]
        for label, overrides, mention in broken:
            with self.subTest(broken=label):
                self.assert_refused(self.adapter(**overrides), mention, label)

    def test_a_broken_adapter_runs_nothing_and_writes_nothing_under_runs(self):  # covers: ADP-1
        broken = [("an id in both lists", {"cases": {"screening": [{"id": "s1"}, {"id": "s2"}],
                                                     "holdout": [{"id": "s1"}]}}),
                  ("a direction other than higher or lower", {"axes": {"passed": "more"}}),
                  ("an empty held-out list", {"cases": {"screening": [{"id": "s1"}], "holdout": []}})]
        for label, overrides in broken:
            with self.subTest(broken=label):
                with self.assertRaises(self.core.AdapterError, msg=f"{label}: the generation refuses to start"):
                    self.generation(adapter=self.adapter(**overrides))
                self.assertEqual(self.runs, [], f"{label}: no run started")
                self.assertEqual(self.mutations, [], f"{label}: no reflection started")
                self.assertEqual(files_under(os.path.join(self.evo, "runs")), [],
                                 f"{label}: nothing is written under runs/")


# ADP-3: the winner comes from the held-out runs only ------------------------------------------------------------------

class HeldOutWinner(Base):
    """Two children of the base. On screening child 1 is the best on passed and errors and child 2 only uses fewer
    tokens: both are on the front, and child 2 dominates the base."""

    def setUp(self):
        super().setUp()
        self.plan.update({
            ("base", "screen"): {"passed": 0.5, "errors": 2, "used": 10000, "score": 0.5, "failures": 2},
            (1, "screen"): {"passed": 1.0, "errors": 0, "used": 20000, "score": 1.0, "failures": 0},
            (2, "screen"): {"passed": 0.5, "errors": 2, "used": 5000, "score": 0.4, "failures": 0},
            ("base", "holdout"): {"passed": 0.5, "errors": 2, "used": 10000, "score": 0.5, "failures": 0}})

    def run_and_check(self, holdout_plan):
        self.plan.update(holdout_plan)
        stop = self.generation()
        self.assertIsNone(stop, f"the generation finished: {self.state()}")
        self.assertEqual(len(self.made), 2, f"two children: {self.mutations}")
        state = self.state()
        self.assertEqual(sorted(state.get("front") or []), sorted(self.made),
                         f"fixture: both children on the front: {state}")
        for genome in ["seed", *self.made]:
            ran = sorted(r["case"] for r in self.runs if r["genome"] == genome and r["kind"] == "holdout")
            self.assertEqual(ran, ["h1", "h2"], f"{genome} ran on every held-out case: {self.calls()}")
        return state

    def test_the_held_out_runs_pick_the_winner_not_the_screening_runs(self):  # covers: ADP-3
        state = self.run_and_check({
            (1, "holdout"): {"passed": 0.5, "errors": 2, "used": 12000, "score": 0.45, "failures": 0},
            (2, "holdout"): {"passed": 1.0, "errors": 0, "used": 10000, "score": 1.0, "failures": 0}})
        best_on_screening, best_on_holdout = self.made
        self.assertEqual(state.get("winner"), best_on_holdout,
                         f"child 1 leads on screening, child 2 on the held-out runs, and child 2 wins: {state}")
        self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip(), state)

    def test_a_child_worse_than_the_base_on_a_protected_lower_axis_never_wins(self):  # covers: ADP-3
        # Child 1 has the best held-out score, passes more and spends less, but has more errors than the base.
        state = self.run_and_check({
            (1, "holdout"): {"passed": 1.0, "errors": 3, "used": 5000, "score": 0.95, "failures": 0},
            (2, "holdout"): {"passed": 0.5, "errors": 1, "used": 10000, "score": 0.6, "failures": 0}})
        self.assertNotEqual(state.get("winner"), self.made[0], f"more errors than the base: never the winner: {state}")
        self.assertEqual(state.get("winner"), self.made[1], state)

    def test_no_winner_when_every_candidate_worsens_a_protected_axis(self):  # covers: ADP-3
        state = self.run_and_check({
            (1, "holdout"): {"passed": 0.4, "errors": 0, "used": 5000, "score": 0.9, "failures": 0},
            (2, "holdout"): {"passed": 1.0, "errors": 3, "used": 5000, "score": 0.9, "failures": 0}})
        self.assertIn("winner", state)
        self.assertIsNone(state["winner"], f"child 1 passes less and child 2 errs more than the base: {state}")
        self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip(), f"the reason is recorded: {state}")


# ADP-7: a whole generation through the core ---------------------------------------------------------------------------

class ToyGeneration(Base):
    def test_screens_the_base_then_makes_children_aimed_at_one_failure_each(self):  # covers: ADP-7
        stop = self.generation()
        self.assertIsNone(stop, f"a finished generation returns None: {self.state()}")
        self.assertEqual({(c, g) for c, g in self.calls()[:2]}, {("s1", "seed"), ("s2", "seed")},
                         f"the base is screened first: {self.calls()}")
        self.assertEqual(len(self.made), 2, f"population 2: {self.mutations}")
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        base_failures = self.base_failures("s1") + self.base_failures("s2")
        aimed = []
        for i, child in enumerate(self.made):
            record = self.mutation(child)
            failure = record.get("failure") or {}
            aimed.append(failure.get("id"))
            self.assertIn(failure.get("id"), base_failures, f"{child} aims at one of the base's failures: {record}")
            self.assertEqual(self.mutations[i]["failure"], failure.get("id"), "mutation.json records the failure")
            self.assertEqual(self.mutations[i]["parent"], "seed")
            self.assertEqual(self.mutations[i]["target"], TARGET)
            marker = "marker-" + failure["id"].replace("/problem-", "-")
            self.assertIn(marker, self.reflections[i]["prompt"], "the reflection prompt holds the failure's evidence")
            individual = os.path.join(self.evo, "genomes", child, "plugin")
            screened = [r for r in self.runs if r["genome"] == child and r["kind"] == "screen"]
            self.assertEqual(sorted(r["case"] for r in screened), ["s1", "s2"], f"{child} is screened")
            for r in screened:
                self.assertEqual(os.path.realpath(r["individual"]), os.path.realpath(individual))
                self.assertTrue(os.path.isabs(str(r["individual"])), "the individual's path is absolute")
                self.assertEqual(r["text"], SEED_PROMPT.replace(LAST_LINE, REWRITES[i]), "the child's own rewrite")
        self.assertEqual(len(set(aimed)), 2, f"the siblings aim at different failures: {aimed}")
        for r in self.runs:
            expected = os.path.join(self.evo, "runs", GEN, r["case"], r["genome"])
            self.assertEqual(os.path.realpath(r["folder"]), os.path.realpath(expected), "runs/<g>/<case>/<genome>/")
            self.assertTrue(os.path.isfile(os.path.join(expected, "result.json")))
        screening = {g: dict(by_case) for g, by_case in (state.get("screening") or {}).items()}
        self.assertEqual(screening.get("seed"), {"s1": "done", "s2": "done"}, state)
        self.assertIn(state.get("winner"), self.made, f"a child beats the base on the held-out runs: {state}")

    def test_a_near_copy_is_discarded_and_the_parents_next_failure_is_tried(self):  # covers: ADP-7
        self.reflect_plan = {1: "near"}
        stop = self.generation(config=dict(self.config, population=1))
        self.assertIsNone(stop, self.state())
        self.assertEqual(len(self.mutations), 2, f"one discarded attempt, then one child: {self.mutations}")
        first, second = self.mutations
        self.assertEqual(second["parent"], first["parent"], "the same parent")
        case = first["failure"].split("/")[0]
        listed = self.base_failures(case)
        self.assertEqual(listed.index(second["failure"]), listed.index(first["failure"]) + 1,
                         f"the parent's next failure, in the order failures() lists them: {listed}")
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.mutation(self.made[0])["failure"]["id"], second["failure"])
        with open(os.path.join(self.evo, "genomes", "discarded.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len([line for line in f if line.strip()]), 1, "the near-copy is logged once")
        discarded = self.state().get("discarded")
        self.assertIsInstance(discarded, list)
        self.assertEqual(len(discarded), 1, f"generation.json counts the discarded near-copy: {discarded}")

    def test_a_failed_reflection_is_discarded_counted_and_the_next_failure_tried(self):  # covers: ADP-7
        self.reflect_plan = {1: "failed"}
        stop = self.generation(config=dict(self.config, population=1))
        self.assertIsNone(stop, f"a failed reflection doesn't stop the generation: {self.state()}")
        self.assertEqual(len(self.mutations), 2, self.mutations)
        self.assertNotEqual(self.mutations[1]["failure"], self.mutations[0]["failure"])
        self.assertEqual(len(self.made), 1)
        state = self.state()
        failed = self.mutations[0]["failure"]
        self.assertEqual(len([e for e in state.get("discarded") or [] if failed in json.dumps(e)]), 1,
                         f"the failed reflection is recorded once in `discarded`: {state}")
        self.assertEqual((state.get("spent") or {}).get("reflections"), 2 * REFLECTION_USED,
                         f"the failed reflection's tokens count too: {state.get('spent')}")

    def test_a_usage_limit_in_a_reflection_pauses_the_generation(self):  # covers: ADP-7
        self.reflect_plan = {1: "limit"}
        stop = self.generation()
        self.assertEqual(stop, PAUSED)
        state = self.state()
        self.assertEqual(state.get("stopped"), PAUSED, state)
        self.assertEqual(self.made, [])
        self.assertEqual(len(self.mutations), 1, "nothing starts after the limit")
        self.assertEqual((state.get("spent") or {}).get("reflections"), REFLECTION_USED,
                         f"the tokens reported before the limit are counted: {state.get('spent')}")

    # The budget: each run costs 10,000 tokens and the adapter estimates 10,000; a reflection is estimated at 30,000.

    def assert_budget_stop(self, stop, replays, reflections):
        self.assertEqual(stop, "budget")
        state = self.state()
        self.assertEqual(state.get("stopped"), "budget", state)
        self.assertEqual(state.get("spent"), {"replays": replays, "reflections": reflections,
                                              "total": replays + reflections}, state)
        self.assertIsNone(state.get("winner"), state)

    def test_the_budget_stops_before_a_reflection(self):  # covers: ADP-7
        # Two base runs: 20,000 spent. A reflection would need 20,000 + 30,000 > 25,000.
        stop = self.generation(budget=25000)
        self.assertEqual(sorted(self.calls()), [("s1", "seed"), ("s2", "seed")],
                         f"only the base's screening ran: {self.calls()}")
        self.assertEqual(self.mutations, [], "no reflection started")
        self.assert_budget_stop(stop, replays=20000, reflections=0)

    def test_the_budget_stops_before_a_run(self):  # covers: ADP-7
        # Base: 20,000. The reflection's check is 20,000 + 30,000 = 50,000, not over 50,000: it runs and reports
        # 25,000. The child's first run would need 45,000 + 10,000 > 50,000: it never starts.
        self.reflection_used = 25000
        stop = self.generation(budget=50000)
        self.assertEqual(len(self.mutations), 1, self.mutations)
        self.assertEqual([g for _, g in self.calls()], ["seed", "seed"],
                         f"the child's run didn't start: {self.calls()}")
        self.assert_budget_stop(stop, replays=20000, reflections=25000)
        self.assertEqual(self.state().get("children"), self.made, "the child made before the stop is recorded")

    def test_a_paused_run_stops_it_and_a_second_run_finishes_it(self):  # covers: ADP-7
        self.pause = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 2
        stop = self.generation()
        self.assertEqual(stop, PAUSED)
        state = self.state()
        self.assertEqual(state.get("stopped"), PAUSED, state)
        self.assertEqual(len(self.made), 1, "nothing started after the pause")
        self.assertEqual(state.get("children"), self.made)
        self.assertEqual(len(self.runs), 4, f"base twice, child once and the paused run: {self.calls()}")
        paused = [r for r in self.runs if r["paused"]]
        self.assertEqual(len(paused), 1)
        paused_result = read_json(os.path.join(paused[0]["folder"], "result.json"))
        first_finished = self.finished(1)

        stop = self.generation(state=state)
        self.assertIsNone(stop, f"the second run finishes the generation: {self.state()}")
        second = [r for r in self.runs if r["run"] == 2]
        self.assertEqual((second[0]["case"], second[0]["genome"]), (paused[0]["case"], paused[0]["genome"]),
                         "the paused case comes first")
        self.assertEqual(second[0]["resume"], paused_result, "it is resumed with the paused result.json's content")
        self.assertEqual(first_finished & self.finished(2), set(), "no finished run runs again")
        self.assertEqual([m["child"] for m in self.mutations if m["run"] == 2], self.made[1:],
                         "the existing child isn't made again")
        self.assertEqual(len(self.made), 2)
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        self.assertNotEqual(state.get("stopped"), PAUSED)

    def test_light_settings_use_a_smaller_population_and_a_shorter_held_out_list(self):  # covers: ADP-7
        self.set_cases(screening=2, holdout=3)
        adapter = self.adapter()
        _screening, holdout = self.core.check_adapter(adapter)
        stop = self.generation(adapter=adapter, config=dict(self.config, population=1), holdout=holdout[:1])
        self.assertIsNone(stop, self.state())
        self.assertEqual(len(self.made), 1, f"population 1: {self.mutations}")
        held_out = sorted({r["case"] for r in self.runs if r["kind"] == "holdout"})
        self.assertEqual(held_out, ["h1"], f"only the first held-out case runs: {self.calls()}")
        self.assertEqual(sorted(r["genome"] for r in self.runs if r["kind"] == "holdout"), sorted(["seed", *self.made]))
        state = self.state()
        for genome, by_case in (state.get("holdout") or {}).items():
            self.assertEqual(sorted(by_case), ["h1"], f"{genome}: only h1 in generation.json: {state}")
        self.assertEqual(state.get("winner"), self.made[0], state)


# ADP-5: Regista's generation goes through RegistaAdapter and evolve_core.Generation ----------------------------------

class RegistaThroughTheCore(tg.Base):
    """tests/test_generation.py's fixture: evolve.main in-process, fake replay_run, real mutate around a stand-in."""

    def setUp(self):
        super().setUp()
        self.core = gs.import_scripts("evolve_core")
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=1)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2}}
        self.inside = {"generation": 0, "adapter": 0}
        self.seen = []  # (inside Generation.run, inside RegistaAdapter.run) for every replay
        self.generations = []
        self.adapters = []

    def fake_replay(self, *args, **kwargs):
        self.seen.append((self.inside["generation"], self.inside["adapter"]))
        return super().fake_replay(*args, **kwargs)

    def spied(self, cls, name, seen, key):
        original = getattr(cls, name)
        inside = self.inside

        def spy(obj, *args, **kwargs):
            seen.append(obj)
            inside[key] += 1
            try:
                return original(obj, *args, **kwargs)
            finally:
                inside[key] -= 1
        return mock.patch.object(cls, name, spy)

    def test_a_regista_generation_runs_through_its_adapter_and_the_core(self):  # covers: ADP-5
        self.assertTrue(issubclass(self.ev.RegistaAdapter, self.core.Adapter))
        self.assertIs(self.ev.ReflectionFailed, self.core.ReflectionFailed)
        self.assertIs(self.ev.ReflectionLimit, self.core.ReflectionLimit)
        with self.spied(self.core.Generation, "run", self.generations, "generation"), \
                self.spied(self.ev.RegistaAdapter, "run", self.adapters, "adapter"):
            code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.generations), 1, "one evolve_core.Generation ran")
        self.assertIsInstance(self.generations[0], self.core.Generation)
        self.assertTrue(self.replays, out)
        self.assertEqual(self.seen, [(1, 1)] * len(self.replays),
                         "every replay ran inside Generation.run, through RegistaAdapter.run")
        adapter = self.adapters[0]
        self.assertTrue(all(a is adapter for a in self.adapters), "one adapter for the generation")
        self.assertIsInstance(adapter, self.core.Adapter)
        self.assertEqual(adapter.name, tg.PROJECT)
        self.assertEqual(list(adapter.axes.items()), REGISTA_AXES)
        self.assertEqual(tuple(adapter.must_not_worsen), ("requirements", "approved"))
        for r in self.replays:
            self.assertTrue(os.path.isfile(os.path.join(self.evo, "runs", tg.GEN, r["case"], r["genome"],
                                                        "result.json")), f"runs/<g>/<step>/<genome>/: {r}")
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        self.assertEqual(state.get("project"), tg.PROJECT)
        self.assertIn("winner", state)

    def test_the_command_refuses_a_broken_adapter_before_anything_runs(self):  # covers: ADP-1
        overlap = self.screening[0]
        broken = {"screening": [{"id": overlap, "step": overlap}],
                  "holdout": [{"id": overlap, "step": overlap}]}
        with mock.patch.object(self.ev.RegistaAdapter, "cases", lambda adapter_self: broken):
            code, out = self.generation()
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn(overlap, out, "the message names the case in both lists")
        self.assertEqual(self.replays, [], "no replay started")
        self.assertEqual(self.mutations, [], "no reflection started")
        self.assertEqual(files_under(os.path.join(self.evo, "runs")), [], "nothing is written under runs/")


if __name__ == "__main__":
    unittest.main()
