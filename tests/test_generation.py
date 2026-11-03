"""One generation: screening, Pareto selection, held-out winner and the token budget (spec
.regista/specs/generation.json: LOOP-1, LOOP-2, LOOP-3, COST-3, LOOP-15; LOOP-16 is the end-to-end test in
genome/tests/e2e/test_generation.py).

`evolve.py generation --target <file> --budget <tokens> [--id <g>] [--from <genome>] [--project <p>] [--seed <n>]
[--yes]` replays the base genome on the screening cases, makes `population` children aimed at one failure each
(mutate), replays them on the screening cases, keeps the Pareto front, replays the base and up to 3 front members on
the held-out cases and picks the winner from the held-out replays only. runs/<g>/generation.json holds its state, so
running the same command again continues it.

The unit tests run the command in-process (evolve.main) with two fakes, so no real replay runs:
- evolve.replay_run is replaced by a fake that writes the replay's result.json under runs/<generation>/<case>/<genome>/
  in the shape real replays leave (outcome, tokens.used, scores, copies/reviews with findings; tests/test_reflect_mutate.py's
  write_replay), with the outcome and token count each test plans, and returns that result. A paused replay is
  outcome "paused" with scores null, as the real one at the usage limit.
- evolve.mutate is wrapped: the stand-in claude (evolve.json `claude_command`) is told what to answer (a distinct
  rewrite of agents/builder.md, or a near-copy of the seed to force NearDuplicate), then the real mutate makes the
  child and its mutation.json. A test can then rewrite mutation.json's tokens.used to control the reflection's cost.
The genome is Regista's real profile over the fake Regista checkout seeded as `seed` (tests/test_genome_seed.py), and
the bench is hand-built case files under bench/<project>/, split by evolve.assign_split with the test's settings.
REGISTA_EVOLVE_HOME and HOME are temporary folders.

Interface assumed: evolve.main(argv) runs the command and returns (or exits with) its exit code; replay_run takes a
`generation` argument and its other arguments keep their names (case, genome, resume); the generation reads a replay's
outcome from its result.json; a paused replay is resumed by calling replay_run with a true `resume`; generation.json's
case keys are the case names (step names, optionally prefixed with "<project>/"); the `stopped` reasons are the
spec's exact strings ("budget", "paused at the usage limit", "nothing left to aim at"); numbers in the printed text may
be written with or without thousands separators, or in millions.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_generation -v"""
import contextlib
import inspect
import io
import json
import os
import pty
import re
import shlex
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from regista_location import REGISTA_CHECKOUT, requires_regista  # noqa: E402  the Regista checkout (or None): reached only by path
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_reflect_mutate as rm  # noqa: E402  (imported as a module, so its test classes don't run twice here)

EVOLVE = os.path.join(ROOT, "scripts", "evolve.py")
PY = sys.executable
GEN = "gen-test"
PROJECT = "doubler"
TARGET = "agents/builder.md"
BUILDER = rm.BUILDER
LINE = "You are the builder.\n"
# Each child's builder prompt replaces the seed's last line with one of these sentences. Every pair of rewrites (and
# each against the seed) differs by at least 22 characters, so none is a near-duplicate (default limit 10), and each
# stays under the 111-byte cap of the 74-byte seed prompt.
SENTENCES = ["Write a failing test before any code.\n", "Run every gate twice before review.\n",
             "Read the whole spec, then plan.\n", "Keep commits small and focused.\n",
             "Handle empty input explicitly.\n", "Name each requirement in commits.\n",
             "Ask when the spec is unclear.\n", "Check units on every number.\n"]
NEAR_COPY = BUILDER.replace("builder.\n", "builder!\n")  # one character from the seed: a NearDuplicate of it
IDS = ["DBL-1", "DBL-2", "DBL-3"]
DEFAULT_ESTIMATE = 155000
REFLECTION_ESTIMATE = 30000
BIG_BUDGET = 10 ** 10
LIMIT_TEXT = "You've hit your session limit · resets 3pm"
# A claude that hits the subscription's usage limit, as the real one does: in stream-json a rejected rate_limit_event and
# an error result, in json the error result alone, in plain text the limit message; every form reports the tokens it
# used before the limit (input 1200, cache creation 60, output 340, cache reads 5000, as the usual stand-in).
LIMIT_STANDIN = r'''#!@PYTHON@
import json, os, sys, time, uuid
CONTROL = @CONTROL@
args = sys.argv[1:]
def opt(name):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None
if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v", "--help", "-h") for a in args):
    print("2.1.312 (Claude Code)")
    sys.exit(0)
with open(os.path.join(CONTROL, "limit-calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "time": time.time()}) + "\n")
sid = str(uuid.uuid4())
usage = {"input_tokens": 1200, "cache_creation_input_tokens": 60, "output_tokens": 340,
         "cache_read_input_tokens": 5000}
text = @TEXT@
result = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "result": text,
          "session_id": sid, "duration_ms": 900, "total_cost_usd": 0, "modelUsage": {}, "usage": usage}
fmt = opt("--output-format")
if fmt == "stream-json":
    for event in ({"type": "system", "subtype": "init", "session_id": sid, "model": opt("--model"),
                   "apiKeySource": "none", "tools": []},
                  {"type": "assistant", "session_id": sid,
                   "message": {"id": "msg_limit", "type": "message", "role": "assistant", "model": "stand-in",
                               "content": [{"type": "text", "text": "Looking at the failure."}], "usage": usage}},
                  {"type": "rate_limit_event", "session_id": sid,
                   "rate_limit_info": {"status": "rejected", "resetsAt": int(time.time()) + 3600,
                                       "rateLimitType": "five_hour"}},
                  result):
        sys.stdout.write(json.dumps(event) + "\n")
elif fmt == "json":
    sys.stdout.write(json.dumps(result) + "\n")
else:
    sys.stdout.write(text + "\n")
sys.stdout.flush()
sys.exit(1)
'''


def bind(func, args, kwargs):
    """The call's arguments by name, read from the real function's signature."""
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
        named = dict(bound.arguments)
        for name, param in inspect.signature(func).parameters.items():
            if param.kind == param.VAR_KEYWORD:
                named.update(named.pop(name, {}) or {})
        return named
    except TypeError:
        return {**dict(zip(["home", "config", "project", "case", "genome", "genome_dir"], args)), **kwargs}


def case_name(case):
    if isinstance(case, dict):
        case = case.get("step") or case.get("case") or case.get("name")
    return str(case).replace("\\", "/").rsplit("/", 1)[-1]


def genome_name(genome):
    if isinstance(genome, dict):
        genome = genome.get("id")
    return str(genome)


def number_forms(n):
    """The ways a plain summary may write the whole number n."""
    grouped = f"{n:,}"
    forms = {str(n), grouped, grouped.replace(",", " "), grouped.replace(",", " "), grouped.replace(",", " ")}
    if n >= 1_000_000:
        for d in (1, 2, 3):
            forms.add(f"{n / 1e6:.{d}f} million")
            forms.add(f"{n / 1e6:.{d}f}M")
        forms.add(f"{n / 1e6:g} million")
    if n >= 1000 and n % 1000 == 0:
        forms.update({f"{n // 1000}k", f"{n // 1000:,}k", f"{n // 1000} thousand", f"{n // 1000:,} thousand"})
    return forms


def mentions(text, n):
    return any(re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", text) for form in number_forms(n))


def norm_cases(by_case):
    return {case_name(k): v for k, v in (by_case or {}).items()}


@requires_regista
class Base(rm.Base):
    """A temporary evolver home with the fake Regista checkout seeded as `seed`, the stand-in claude, a hand-built
    bench and the two fakes."""

    def setUp(self):
        self.replays = []  # every fake replay_run call: {case, genome, generation, resume, config, run}
        self.mutations = []  # every mutate call: {parent, failure, target, child, config, run}
        self.made = []  # the children mutate made, in order
        self.plan = {}  # (role, kind) -> replay spec; role "base" or the child's number (1, 2, ...), kind screen/holdout
        self.pause = None  # a function (role, kind, nth call for that role and kind) -> True to pause that replay
        self.reflection_used = None  # rewrite each new mutation.json's tokens.used to this
        self.duplicates = set()  # mutate call numbers answered with a near-copy of the seed
        self.interrupt_mutation = None  # mutate call number that raises KeyboardInterrupt (a stop)
        self.bad_answers = {}  # mutate call number -> rm.Base.answer kwargs that make that reflection fail
        self.limit_mutations = set()  # mutate call numbers whose claude hits the usage limit (LIMIT_STANDIN)
        self.interrupt = None  # a function (role, kind, nth) -> True to raise KeyboardInterrupt in that replay (Ctrl-C)
        self.active = 0
        self.max_active = 0
        self.run_number = 0
        self.kind = {}
        super().setUp()
        self.orig_replay = self.ev.replay_run
        self.orig_mutate = self.ev.mutate
        self.repo = os.path.join(self.tmp, "projects", PROJECT)
        rm.write(os.path.join(self.repo, ".regista", "config.json"), json.dumps(
            {"project": "Doubler", "gates": [{"name": "tests", "run": "true", "when": ["commit"]}]}) + "\n")
        rm.write(os.path.join(self.repo, "double.py"), "def double(n):\n    return 2 * n\n")
        git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
            subprocess.run(git + cmd, cwd=self.repo, check=True, capture_output=True, env=env)

    def configure(self, **extra):
        cfg = {"claude_command": self.claude, "require_sandbox": False, "task_model": "haiku",
               "reflection_model": rm.REFLECTION_MODEL, "population": 2, "screen_cases": 2, "holdout_pct": 50,
               **extra}
        cfg = {k: v for k, v in cfg.items() if v is not None}
        rm.write_json(os.path.join(self.evo, "evolve.json"), cfg)
        self.config = self.ev.load_config(self.evo)

    # The bench ------------------------------------------------------------------------------------------------------

    def bench(self, n=4, holdout_pct=50, screen_cases=2):
        """n replayable cases, split by assign_split with these settings. Returns (screening, holdout) case names."""
        built = []
        for i in range(1, n + 1):
            step = f"m{i}-case{i}"
            case = {"project": PROJECT, "repo": self.repo, "step": step, "milestone": f"M{i}", "parent": "a" * 40,
                    "finishing_commit": "b" * 40, "spec": {"step": step, "requirements": [
                        {"id": r, "text": f"{r} SHALL hold", "verify": "test"} for r in IDS]},
                    "scoring_ids": IDS, "tests": [], "outside_files": [], "brief": f"Build step {step}.",
                    "baseline": {"review_rounds": n - i, "gate_failures": 0, "refusals": 0, "compactions": 0,
                                 "logs_complete": True},
                    "replayable": True, "reasons": ["Replayable for the test."], "split": None, "screening": False}
            built.append((case, [], 10 * i))
        self.ev.assign_split(PROJECT, built, {"holdout_pct": holdout_pct, "screen_cases": screen_cases})
        for case, _, _ in built:
            rm.write_json(os.path.join(self.evo, "bench", PROJECT, case["step"] + ".json"), case)
        screening = [c["step"] for c, _, _ in built if c.get("screening")]
        holdout = [c["step"] for c, _, _ in built if c.get("split") == "holdout"]
        self.assertEqual(len(screening), min(screen_cases, n - len(holdout)), "fixture: assign_split's screening")
        self.assertTrue(holdout, "fixture: at least one held-out case")
        self.kind = {c["step"]: ("screen" if c.get("screening") else c.get("split") or "none") for c, _, _ in built}
        self.screening, self.holdout = screening, holdout
        return screening, holdout

    # Replay records ---------------------------------------------------------------------------------------------------

    def write_result(self, generation, case, genome, outcome="approved", used=10000, not_proven=(), findings=0):
        """A replay's result.json (and its review copies) as the replay leaves it; returns the result."""
        folder = os.path.join(self.evo, "runs", generation, case, genome)
        if outcome == "paused":
            result = {"project": PROJECT, "case": case, "genome": genome, "generation": generation, "model": "haiku",
                      "outcome": "paused", "stop_reason": "the subscription's usage limit was reached (five_hour)",
                      "tokens": {"used": used}, "scores": None, "started_at": "2026-10-31T10:00:00",
                      "pauses": [{"at": "2026-10-31T10:05:00", "resets_at": "2026-10-31T15:00:00"}],
                      "resets_at": "2026-10-31T15:00:00", "ended_at": "2026-10-31T10:05:00"}
            rm.write_json(os.path.join(folder, "result.json"), result)
            return result
        if findings:
            reviews = {"r1": {"verdict": "changes", "findings": [
                rm.finding("major", f"Problem {k + 1} of {genome} on {case}: double('') crashes.", 10 + k)
                for k in range(findings)]}}
        elif outcome == "approved":
            reviews = {"r1": {"verdict": "approve", "findings": []}}
        else:
            reviews = {}
        rm.write_replay(self.evo, case, genome, outcome=outcome, not_proven=tuple(not_proven), reviews=reviews,
                        generation=generation)
        path = os.path.join(folder, "result.json")
        with open(path, encoding="utf-8") as f:
            result = json.load(f)
        result["tokens"]["used"] = used
        result["scores"]["finding_severity"] = {"value": findings, "blocker": 0, "major": findings,
                                                "failed": bool(findings), "better": "lower"}
        result["scores"]["tokens"] = {"value": used, "failed": False, "better": "lower"}
        rm.write_json(path, result)
        return result

    def role(self, genome):
        if genome == "seed":
            return "base"
        return self.made.index(genome) + 1 if genome in self.made else genome

    def fake_replay(self, *args, **kwargs):
        b = bind(self.orig_replay, args, kwargs)
        case, genome = case_name(b.get("case")), genome_name(b.get("genome"))
        generation = b.get("generation") or "adhoc"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            role, kind = self.role(genome), self.kind.get(case)
            nth = 1 + sum(1 for r in self.replays if r["role"] == role and r["kind"] == kind)
            call = {"case": case, "genome": genome, "generation": generation, "resume": b.get("resume"),
                    "config": b.get("config"), "run": self.run_number, "role": role, "kind": kind, "paused": False}
            self.replays.append(call)
            if self.interrupt and not call["resume"] and self.interrupt(role, kind, nth):
                call["paused"] = True  # not finished: a rerun may start it again
                raise KeyboardInterrupt
            if self.pause and not call["resume"] and self.pause(role, kind, nth):
                call["paused"] = True
                return self.write_result(generation, case, genome, outcome="paused", used=500)
            spec = self.plan.get((role, kind), self.plan.get(role, {}))
            return self.write_result(generation, case, genome, **spec)
        finally:
            self.active -= 1

    def fake_mutate(self, *args, **kwargs):
        b = bind(self.orig_mutate, args, kwargs)
        n = len(self.mutations) + 1
        failure = b["failure"]
        self.mutations.append({"parent": b["parent"], "failure": failure["id"], "target": b["target"],
                               "child": b["child_id"], "config": b["config"], "run": self.run_number})
        if self.interrupt_mutation == n:
            raise KeyboardInterrupt
        if n in self.limit_mutations:
            # This reflection's claude hits the usage limit: the real mutate runs it and reads what it reports.
            with open(self.claude, encoding="utf-8") as f:
                usual = f.read()
            rm.write(self.claude, LIMIT_STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))
                     .replace("@TEXT@", repr(LIMIT_TEXT)))
            try:
                return self.orig_mutate(b["home"], b["config"], self.profile, self.checkout, b["parent"], failure,
                                        b["target"], b["child_id"])
            finally:
                rm.write(self.claude, usual)
        if n in self.bad_answers:
            # A reflection that fails: the stand-in answers badly and the real mutate raises its plain error.
            self.answer(**self.bad_answers[n])
            return self.orig_mutate(b["home"], b["config"], self.profile, self.checkout, b["parent"], failure,
                                    b["target"], b["child_id"])
        text = NEAR_COPY if n in self.duplicates else BUILDER.replace(LINE, SENTENCES[len(self.made)])
        self.answer(text=text)
        made = self.orig_mutate(b["home"], b["config"], self.profile, self.checkout, b["parent"], failure,
                                b["target"], b["child_id"])
        if self.reflection_used is not None:
            path = os.path.join(self.genome_dir(b["child_id"]), "mutation.json")
            record = self.read_json(path)
            record["tokens"]["used"] = self.reflection_used
            rm.write_json(path, record)
        self.made.append(b["child_id"])
        return made

    # Running the command ----------------------------------------------------------------------------------------------

    def generation(self, budget=BIG_BUDGET, gen=GEN, yes=True, extra=()):
        """Run `evolve.py generation` in-process with the fakes; returns (exit code, printed text)."""
        argv = ["generation", "--target", TARGET, "--budget", str(budget), "--id", gen, "--seed", "7",
                *(["--yes"] if yes else []), *extra]
        return self.invoke(argv)

    def invoke(self, argv):
        """Run evolve.main(argv) in-process with the fakes; returns (exit code, printed text)."""
        self.run_number += 1
        buf = io.StringIO()
        with mock.patch.object(self.ev, "replay_run", self.fake_replay), \
                mock.patch.object(self.ev, "mutate", self.fake_mutate), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                code = self.ev.main(argv)
            except SystemExit as e:
                code = e.code
            except KeyboardInterrupt:
                code = "interrupted"
        return (0 if code is None else code), buf.getvalue()

    def state(self, gen=GEN):
        path = os.path.join(self.evo, "runs", gen, "generation.json")
        self.assertTrue(os.path.isfile(path), f"runs/{gen}/generation.json is written")
        return self.read_json(path)

    def past_replay(self, generation, case, genome, used, outcome="approved"):
        self.write_result(generation, case, genome, outcome=outcome, used=used)

    def calls_in(self, run):
        return [(r["case"], r["genome"]) for r in self.replays if r["run"] == run]

    def finished_calls(self, run):
        return {(r["case"], r["genome"]) for r in self.replays if r["run"] == run and not r["paused"]}


# pareto_front (genome.py) -------------------------------------------------------------------------------------------

class ParetoFront(unittest.TestCase):
    def setUp(self):
        self.gn = rm.import_scripts("genome")

    def test_front_keeps_the_entries_no_other_dominates(self):  # covers: LOOP-2
        axes = {"requirements": "higher", "tokens": "lower"}
        entries = {"a": {"requirements": 1.0, "tokens": 300}, "b": {"requirements": 0.5, "tokens": 100},
                   "c": {"requirements": 0.5, "tokens": 200},  # b is as good on requirements and better on tokens
                   "d": {"requirements": 1.0, "tokens": 300},  # the same as a: neither dominates the other
                   "e": {"requirements": 0.2, "tokens": 400}}
        front = self.gn.pareto_front(entries, axes)
        self.assertIsInstance(front, list)
        self.assertEqual(sorted(front), ["a", "b", "d"])
        self.assertEqual(self.gn.pareto_front(entries, axes), front, "the same input gives the same order")
        reversed_entries = dict(reversed(list(entries.items())))
        self.assertEqual(sorted(self.gn.pareto_front(reversed_entries, axes)), ["a", "b", "d"])
        self.assertEqual(self.gn.pareto_front({"x": {"requirements": 0, "tokens": 9}}, axes), ["x"])
        self.assertEqual(self.gn.pareto_front({}, axes), [])
        # "higher" and "lower" are honoured per axis. Flipping both: e has the fewest requirements (0.2) and the
        # most tokens (400), so it is strictly better on both axes than every other entry and is the whole front.
        flipped = {"requirements": "lower", "tokens": "higher"}
        self.assertEqual(self.gn.pareto_front(entries, flipped), ["e"])
        # Flipping only tokens: a and d have the most requirements, e the most tokens; b and c lose to a on both.
        mixed = {"requirements": "higher", "tokens": "higher"}
        self.assertEqual(sorted(self.gn.pareto_front(entries, mixed)), ["a", "d", "e"])


# LOOP-1: the budget ---------------------------------------------------------------------------------------------------

class Budget(Base):
    """Every replay costs 10,000 tokens and so did the past replays in runs/, so the replay estimate is 10,000."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.past_replay("old-gen", self.screening[0], "seed", 10000)
        self.past_replay("old-gen", self.holdout[0], "seed", 10000)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "used": 10000},
                     "base": {"used": 10000}, 1: {"used": 10000}, 2: {"used": 10000}}

    def assert_stopped_for_budget(self, code, out, replays, reflections, budget):
        state = self.state()
        self.assertEqual(state.get("stopped"), "budget", state)
        self.assertEqual(state.get("spent"), {"replays": replays, "reflections": reflections,
                                              "total": replays + reflections}, state)
        self.assertNotIn("Traceback", out)
        self.assertRegex(out, r"(?i)budget", out)
        self.assertTrue(mentions(out, replays + reflections), f"prints the tokens spent ({replays + reflections}):\n{out}")
        self.assertTrue(mentions(out, budget), f"prints the budget ({budget}):\n{out}")
        self.assertIsNone(state.get("winner"), "nothing was replayed on the held-out cases, so no winner")

    def test_budget_stops_before_a_reflection(self):  # covers: LOOP-1
        # Two base replays: 10,000 then 20,000 spent (each check 10,000 + spent <= 25,000). The reflection would need
        # 20,000 + 30,000 > 25,000, and a held-out replay 20,000 + 10,000 > 25,000: nothing more starts.
        code, out = self.generation(budget=25000)
        self.assertEqual(len(self.replays), 2, f"only the base's screening replays ran: {self.calls_in(1)}\n{out}")
        self.assertEqual({r["genome"] for r in self.replays}, {"seed"})
        self.assertEqual(self.mutations, [], "no reflection started")
        self.assertEqual(self.made, [])
        self.assert_stopped_for_budget(code, out, replays=20000, reflections=0, budget=25000)

    def test_budget_stops_before_a_replay(self):  # covers: LOOP-1
        # Base: 20,000. The reflection's check is 20,000 + 30,000 = 50,000, not over 50,000, so it runs and records
        # 25,000. The child's first replay would need 45,000 + 10,000 > 50,000: it never starts, nor does any other.
        self.reflection_used = 25000
        code, out = self.generation(budget=50000)
        self.assertEqual(len(self.mutations), 1, f"one reflection: {self.mutations}\n{out}")
        self.assertEqual(len(self.made), 1)
        self.assertEqual([r["genome"] for r in self.replays], ["seed", "seed"],
                         f"the child's replay didn't start: {self.calls_in(1)}\n{out}")
        self.assert_stopped_for_budget(code, out, replays=20000, reflections=25000, budget=50000)
        self.assertEqual(self.state().get("children"), self.made, "the child made before the stop is recorded")


class NoCandidateNoHeldOut(Base):
    """Every reflection fails, so no child is made and none finishes screening."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 1}}  # one failure per screening case

    def test_without_a_screened_child_the_base_is_not_replayed_on_the_held_out_cases(self):  # covers: LOOP-1
        self.bad_answers = {n: {"reply": "I think the builder should be more careful.\n"} for n in range(1, 21)}
        code, out = self.generation()
        self.assertNotIn("Traceback", out)
        self.assertTrue(self.mutations, f"fixture: reflections were tried: {self.calls_in(1)}\n{out}")
        self.assertEqual(self.made, [], "fixture: every reflection failed")
        held_out = [(r["case"], r["genome"]) for r in self.replays if r["kind"] == "holdout"]
        self.assertEqual(held_out, [], f"no candidate, so nothing (not even the base) runs on the held-out cases:\n{out}")
        self.assertEqual({r["genome"] for r in self.replays}, {"seed"})
        self.assertEqual(sorted(r["case"] for r in self.replays), sorted(self.screening), "only the base's screening")
        state = self.state()
        self.assertFalse(state.get("holdout"), f"no held-out results: {state}")
        self.assertIsNone(state.get("winner"), state)
        why = state.get("why")
        self.assertTrue(isinstance(why, str) and why.strip(), f"the reason there is no winner is recorded: {state}")

    def test_tokens_a_reflection_reported_before_the_usage_limit_are_counted(self):  # covers: LOOP-1
        self.limit_mutations = {1}
        code, out = self.generation()
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.mutations), 1, f"the usage limit stops the generation: {self.mutations}\n{out}")
        self.assertEqual(self.made, [])
        spent = self.state().get("spent") or {}
        self.assertGreater(spent.get("reflections", 0), 0,
                           f"the tokens claude reported before the limit are in spent.reflections: {spent}")
        self.assertEqual(spent.get("total"), spent.get("replays", 0) + spent.get("reflections", 0), spent)


# LOOP-2: the winner comes from the held-out replays only --------------------------------------------------------------

class Winner(Base):
    """Two children of the base. Child 1 is the best on screening (approved, every requirement) and child 2 only
    uses fewer tokens; both are on the front and the base is dominated by child 2."""

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 10000},
                     (1, "screen"): {"outcome": "approved", "used": 20000},
                     (2, "screen"): {"outcome": "ended", "findings": 2, "not_proven": ["DBL-3"], "used": 5000}}

    def check_common(self, code, out):
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.made), 2, f"two children: {self.mutations}\n{out}")
        state = self.state()
        self.assertEqual(sorted(state.get("front") or []), sorted(self.made), f"the front: {state}")
        holdout = {g: norm_cases(by_case) for g, by_case in (state.get("holdout") or {}).items()}
        for genome in ["seed", *self.made]:
            self.assertEqual(sorted(holdout.get(genome, {})), sorted(self.holdout),
                             f"{genome} was replayed on every held-out case: {state.get('holdout')}")
        screened = {r["genome"] for r in self.replays if r["kind"] == "screen"}
        self.assertEqual(screened, {"seed", *self.made})
        return state

    def test_winner_is_chosen_on_held_out_replays_only(self):  # covers: LOOP-2
        self.plan.update({("base", "holdout"): {"outcome": "approved", "not_proven": ["DBL-3"], "used": 10000},
                          (1, "holdout"): {"outcome": "approved", "not_proven": ["DBL-2", "DBL-3"], "used": 10000},
                          (2, "holdout"): {"outcome": "approved", "used": 10000}})
        code, out = self.generation()
        state = self.check_common(code, out)
        best_on_screening, best_on_holdout = self.made
        self.assertGreater(self.ev.genome_score(self.evo, GEN, best_on_screening) or 0,
                           self.ev.genome_score(self.evo, GEN, best_on_holdout) or 0,
                           "fixture: child 1 scores higher on screening")
        self.assertEqual(state.get("winner"), best_on_holdout,
                         "child 1 is better on screening but proves fewer requirements than the base on the "
                         f"held-out cases; child 2 proves more and wins: {state}")
        self.assertTrue(isinstance(state.get("why"), str) and state["why"].strip(), state)
        self.assertIn(best_on_holdout, out, "the summary names the winner")

    def test_no_qualifying_candidate_means_no_winner_and_a_reason(self):  # covers: LOOP-2
        # Child 1 ties the base on every held-out axis (better on none); child 2 uses fewer tokens but isn't approved.
        self.plan.update({("base", "holdout"): {"outcome": "approved", "used": 10000},
                          (1, "holdout"): {"outcome": "approved", "used": 10000},
                          (2, "holdout"): {"outcome": "ended", "used": 5000}})
        code, out = self.generation()
        state = self.check_common(code, out)
        self.assertIn("winner", state)
        self.assertIsNone(state["winner"], f"no candidate qualifies, so there is no winner: {state}")
        why = state.get("why")
        self.assertTrue(isinstance(why, str) and why.strip(), f"the reason is recorded: {state}")
        self.assertNotRegex(why.strip(), r"^[\[{]", "the reason is a plain sentence")
        self.assertTrue(why.strip()[:40] in out or re.search(r"(?i)no winner", out),
                        f"the summary says there is no winner, and why:\n{out}")


# LOOP-3 and COST-3: the estimate, the confirmation and the defaults --------------------------------------------------

class Confirmation(Base):
    """No evolve.json override of population, screen_cases or the models; ten cases, 30% held out."""

    def setUp(self):
        super().setUp()
        self.configure(task_model=None, reflection_model=None, population=None, screen_cases=None, holdout_pct=30)
        self.bench(n=10, holdout_pct=30, screen_cases=4)
        self.replay_count = 4 + 6 * 4 + 4 * len(self.holdout)

    def subprocess_generation(self, stdin):
        return subprocess.run([PY, EVOLVE, "generation", "--target", TARGET, "--budget", "50000000", "--id", GEN,
                               "--seed", "7"], cwd=self.tmp, stdin=stdin, capture_output=True, text=True,
                              encoding="utf-8", env={k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"},
                              timeout=120)

    def assert_nothing_ran(self, out):
        cases = os.path.join(self.evo, "runs", GEN)
        folders = [d for d in (os.listdir(cases) if os.path.isdir(cases) else [])
                   if os.path.isdir(os.path.join(cases, d))]
        self.assertEqual(folders, [], f"no replay started:\n{out}")
        self.assertEqual(sorted(g for g in os.listdir(os.path.join(self.evo, "genomes")) if g != "discarded.jsonl"),
                         ["seed"], f"no child made:\n{out}")
        self.assertEqual(self.calls(), [], "claude was never started")

    def assert_estimate(self, out, per_replay):
        tokens = self.replay_count * per_replay + 6 * REFLECTION_ESTIMATE
        self.assertTrue(mentions(out, self.replay_count), f"prints the estimated replays ({self.replay_count}):\n{out}")
        self.assertTrue(mentions(out, tokens), f"prints the estimated tokens ({tokens}):\n{out}")
        self.assertIn(TARGET, out)
        self.assertRegex(out, r"(?<![\w-])seed(?![\w-])", "names the base")
        for case in self.screening + self.holdout:
            self.assertIn(case, out, "names the screening and held-out cases")

    def test_without_a_terminal_or_yes_it_prints_the_estimate_and_refuses(self):  # covers: LOOP-3, COST-3
        # runs/ is empty, so each replay is estimated at 155,000 tokens.
        r = self.subprocess_generation(subprocess.DEVNULL)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("--yes", out, "the refusal says how to confirm without a terminal")
        self.assert_estimate(out, DEFAULT_ESTIMATE)
        self.assertTrue(mentions(out, 50000000), f"prints the budget:\n{out}")
        self.assert_nothing_ran(out)

    def test_answering_no_on_the_terminal_runs_nothing(self):  # covers: LOOP-3
        master, slave = pty.openpty()
        try:
            os.write(master, b"n\n")
            r = self.subprocess_generation(slave)
        finally:
            os.close(slave)
            os.close(master)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertTrue(mentions(out, self.replay_count), f"the estimate comes before the question:\n{out}")
        self.assert_nothing_ran(out)

    def test_defaults_population_screening_models_and_the_mean_of_past_replays(self):  # covers: COST-3
        # Finished past replays used 100,000 and 200,000 tokens (mean 150,000); a paused one doesn't count.
        self.past_replay("adhoc", self.screening[0], "current", 100000)
        self.past_replay("old-gen", self.holdout[0], "seed", 200000)
        self.write_result("adhoc", self.screening[1], "current", outcome="paused", used=7)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2, "used": 150000}}
        for child in range(1, 9):
            self.plan[child] = {"used": 150000}
        self.plan["base"] = {"used": 150000}
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assert_estimate(out, 150000)
        self.assertEqual(len(self.made), 6, f"population 6: {self.mutations}\n{out}")
        self.assertEqual(self.state().get("children"), self.made)
        for genome in ["seed", *self.made]:
            cases = sorted(r["case"] for r in self.replays if r["genome"] == genome and r["kind"] == "screen")
            self.assertEqual(cases, sorted(self.screening), f"{genome} ran on the 4 screening cases once each")
        self.assertEqual(len(self.screening), 4)
        self.assertEqual(self.max_active, 1, "one replay at a time")
        for r in self.replays:
            self.assertEqual((r["config"] or {}).get("task_model", "sonnet"), "sonnet", "candidates run on Sonnet")
        models = [self.model_of(c["argv"]) for c in self.calls()]
        self.assertEqual(len(models), 6, "one reflection per child")
        self.assertEqual(set(models), {"opus"}, "reflection runs on Opus")

    @staticmethod
    def model_of(argv):
        for i, a in enumerate(argv):
            if a == "--model" and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith("--model="):
                return a.split("=", 1)[1]
        return None


@requires_regista
class NothingElseStartsAGeneration(unittest.TestCase):
    """Only the user starts a generation: the hooks, the skills and regista.py never run `evolve.py generation`."""

    def test_no_hook_skill_or_regista_command_starts_a_generation(self):  # covers: LOOP-3
        files = [os.path.join(REGISTA_CHECKOUT, "hooks", "hooks.json"),
                 os.path.join(REGISTA_CHECKOUT, "hooks", "codex-hooks.json"),
                 os.path.join(REGISTA_CHECKOUT, "scripts", "regista.py")]
        for dirpath, _, names in os.walk(os.path.join(REGISTA_CHECKOUT, "skills")):
            files += [os.path.join(dirpath, n) for n in names]
        starts = re.compile(r"evolve(?:\.py)?[\"']?(?:\s+[^\s|;&]+)*?\s+[\"']?generation\b|cmd_generation")
        checked = 0
        for path in files:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            checked += 1
            for n, line in enumerate(text.splitlines(), 1):
                self.assertIsNone(starts.search(line), f"{os.path.relpath(path, REGISTA_CHECKOUT)}:{n} starts a generation: {line}")
        self.assertGreaterEqual(checked, 3, "the hooks, regista.py and the skills were read")


# LOOP-15: resume, reuse and aiming --------------------------------------------------------------------------------------

class Resume(Base):
    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2}}

    def test_a_paused_replay_is_resumed_and_nothing_finished_runs_twice(self):  # covers: LOOP-15
        # Child 1's second screening replay pauses at the usage limit.
        self.pause = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 2
        code, out = self.generation()
        self.assertEqual(code, self.ev.PAUSED_EXIT, out)
        self.assertNotIn("Traceback", out)
        state = self.state()
        self.assertEqual(state.get("stopped"), "paused at the usage limit", state)
        self.assertEqual(state.get("children"), self.made)
        self.assertEqual(len(self.made), 1, "nothing started after the pause")
        self.assertEqual(len(self.replays), 4, f"base twice, child once and the paused one: {self.calls_in(1)}")
        paused = [(r["case"], r["genome"]) for r in self.replays if r["paused"]]
        first_finished = self.finished_calls(1)

        code, out = self.generation()
        self.assertEqual(code, 0, out)
        second = [r for r in self.replays if r["run"] == 2]
        self.assertEqual([(second[0]["case"], second[0]["genome"])], paused, "the paused replay comes first")
        self.assertTrue(second[0]["resume"], "and it is resumed, not started again")
        repeated = first_finished & self.finished_calls(2)
        self.assertEqual(repeated, set(), "no finished replay runs again")
        self.assertEqual([m["child"] for m in self.mutations if m["run"] == 2], self.made[1:],
                         "the existing child isn't made again; only the missing one is")
        self.assertEqual(len(self.made), 2)
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        self.assertNotEqual(state.get("stopped"), "paused at the usage limit")

        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls_in(3), [], "a finished generation runs nothing more")
        self.assertEqual([m for m in self.mutations if m["run"] == 3], [])
        self.assertEqual(self.state().get("children"), self.made)
        self.assertEqual(self.state().get("winner"), state.get("winner"))

    def test_a_stopped_generation_continues_with_its_children(self):  # covers: LOOP-15
        self.interrupt_mutation = 2  # stopped (Ctrl-C) during the second reflection
        self.generation()
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.state().get("children"), self.made, "generation.json was written before the stop")
        first_finished = self.finished_calls(1)
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertEqual(first_finished & self.finished_calls(2), set(), "no finished replay runs again")
        self.assertNotIn(self.made[0], [m["child"] for m in self.mutations if m["run"] == 2],
                         "the child made before the stop is reused")
        self.assertEqual(len(self.made), 2)
        self.assertEqual(self.state().get("children"), self.made)

    def test_the_base_genomes_finished_baseline_replays_are_reused(self):  # covers: LOOP-15
        for case in self.screening:
            self.write_result(GEN, case, "seed", outcome="ended", findings=2)
        self.configure(population=1)
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        base_screening = [r for r in self.replays if r["genome"] == "seed" and r["kind"] == "screen"]
        self.assertEqual(base_screening, [], "the base's screening replays already in runs/<g>/ are reused")
        self.assertTrue(self.replays, "the child still runs")
        self.assertEqual({r["generation"] for r in self.replays}, {GEN}, "replays are filed under this generation")
        screening = norm_cases((self.state().get("screening") or {}).get("seed"))
        self.assertEqual(screening, {case: "ended" for case in self.screening})

    def test_siblings_target_different_failures_until_nothing_is_left(self):  # covers: LOOP-15
        # One review finding on each screening case: the base has two failures; the children have none.
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 1}}
        self.configure(population=4)
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        base_failures = [f["id"] for case in self.screening for f in self.ev.failures(self.evo, GEN, case, "seed")]
        self.assertEqual(len(base_failures), 2, "fixture: one failure per screening case")
        aimed = [self.read_json(self.genome_dir(c), "mutation.json")["failure"]["id"] for c in self.made]
        self.assertEqual(sorted(aimed), sorted(base_failures), "each sibling aims at a failure no other one did")
        self.assertEqual({m["parent"] for m in self.mutations}, {"seed"})
        self.assertEqual({m["target"] for m in self.mutations}, {TARGET})
        state = self.state()
        self.assertEqual(state.get("stopped"), "nothing left to aim at", state)
        self.assertEqual(state.get("children"), self.made)

    def test_a_near_duplicate_makes_it_try_the_parents_next_failure(self):  # covers: LOOP-15
        self.configure(population=1)
        self.duplicates = {1}
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.mutations), 2, f"one discarded attempt, then one child: {self.mutations}")
        first, second = self.mutations
        self.assertEqual(second["parent"], first["parent"], "the same parent")
        self.assertNotEqual(second["failure"], first["failure"], "its next failure")
        case = next(c for c in self.screening if first["failure"].startswith(c + "/"))
        listed = [f["id"] for f in self.ev.failures(self.evo, GEN, case, first["parent"])]
        self.assertEqual(listed.index(second["failure"]), listed.index(first["failure"]) + 1,
                         f"the next failure in the order failures() lists them: {listed}")
        self.assertEqual(len(self.made), 1)
        self.assertEqual(self.read_json(self.genome_dir(self.made[0]), "mutation.json")["failure"]["id"],
                         second["failure"])
        with open(os.path.join(self.evo, "genomes", "discarded.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len([line for line in f if line.strip()]), 1)
        self.assertRegex(out, r"(?i)(\b(1|one)\b[^\n]*(near|discard|duplicate))|((near|discard|duplicate)[^\n]*\b(1|one)\b)",
                         f"the summary counts the discarded near-copy:\n{out}")

    # A failed reflection --------------------------------------------------------------------------------------------

    def discarded_for(self, failure_id):
        """generation.json's `discarded` entries that name this failure (as its id, or inside a failure record)."""
        discarded = self.state().get("discarded")
        self.assertIsInstance(discarded, list, f"generation.json has a `discarded` list: {self.state()}")
        return [e for e in discarded if failure_id in json.dumps(e)]

    def assert_plain_reason(self, entry, pattern):
        sentences = [v for v in (entry.values() if isinstance(entry, dict) else [])
                     if isinstance(v, str) and re.search(r"\s", v.strip())]
        self.assertTrue(sentences, f"the discarded entry has its reason in plain words: {entry}")
        self.assertTrue(any(re.search(pattern, s) and not s.strip().startswith(("{", "[", "Traceback"))
                            for s in sentences), f"the reason says what went wrong ({pattern}): {entry}")

    def test_a_failed_reflection_is_discarded_and_the_parents_next_failure_is_tried(self):  # covers: LOOP-15
        self.configure(population=1)
        self.bad_answers = {1: {"reply": "I think the builder should be more careful.\n"}}  # no JSON at all
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(self.mutations), 2, f"the failed reflection, then one that made a child: {self.mutations}")
        first, second = self.mutations
        self.assertEqual(second["parent"], first["parent"], "the same parent")
        self.assertNotEqual(second["failure"], first["failure"], "its next failure")
        case = next(c for c in self.screening if first["failure"].startswith(c + "/"))
        listed = [f["id"] for f in self.ev.failures(self.evo, GEN, case, first["parent"])]
        self.assertEqual(listed.index(second["failure"]), listed.index(first["failure"]) + 1,
                         f"the next failure in the order failures() lists them: {listed}")
        self.assertEqual(len(self.made), 1, "the generation went on and made its child")
        if first["child"] not in self.made:  # the child id may be reused for the next try
            self.assertFalse(os.path.exists(self.genome_dir(first["child"])), "the failed reflection left no child")
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        self.assertIsNone(state.get("stopped"), f"the failed reflection didn't stop the generation: {state}")
        entries = self.discarded_for(first["failure"])
        self.assertEqual(len(entries), 1, f"the failed reflection is recorded once in `discarded`: {state}")
        self.assert_plain_reason(entries[0], r"(?i)json")
        self.assertEqual(self.discarded_for(second["failure"]), [], "the successful reflection isn't discarded")
        child_tokens = self.read_json(self.genome_dir(self.made[0]), "mutation.json")["tokens"]["used"]
        self.assertGreater((state.get("spent") or {}).get("reflections", 0), child_tokens,
                           f"the failed reflection's reported tokens are counted too: {state.get('spent')}")

    def test_a_rerun_does_not_retry_a_failure_whose_reflection_failed(self):  # covers: LOOP-15
        self.configure(population=2)
        self.bad_answers = {1: {"exit_code": 3}}  # claude exits non-zero
        self.interrupt_mutation = 2  # then the generation is stopped (Ctrl-C) during the next reflection
        self.generation()
        failed = self.mutations[0]["failure"]
        self.assertEqual(len(self.discarded_for(failed)), 1, "recorded before the stop")
        self.assert_plain_reason(self.discarded_for(failed)[0], r"(?i)exit|fail")
        code, out = self.generation()
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        again = [m for m in self.mutations if m["run"] == 2 and m["parent"] == self.mutations[0]["parent"]]
        self.assertTrue(again, f"the rerun made the missing children: {self.mutations}")
        self.assertNotIn(failed, [m["failure"] for m in again],
                         "the failure whose reflection failed counts as already aimed at")
        self.assertEqual(again[0]["failure"], self.mutations[1]["failure"],
                         "the rerun starts with the failure the stopped reflection aimed at")
        self.assertEqual(len(self.made), 2)
        state = self.state()
        self.assertEqual(state.get("children"), self.made)
        self.assertEqual(len(self.discarded_for(failed)), 1, f"the discarded try is kept once: {state}")


class ContinueCommand(Base):
    """A generation started without --id gets a dated id; every message about continuing it prints the exact command,
    with that id and the target, budget and base it was started with, and running that command continues it."""

    BUDGET = 987654321

    def setUp(self):
        super().setUp()
        self.bench(n=4, holdout_pct=50, screen_cases=2)
        self.configure(population=2)
        self.plan = {("base", "screen"): {"outcome": "ended", "findings": 2}}

    def start(self):
        """Start the generation without --id; returns (exit code, printed text, the generation's id)."""
        code, out = self.invoke(["generation", "--target", TARGET, "--budget", str(self.BUDGET), "--from", "seed",
                              "--seed", "7", "--yes"])
        self.assertNotIn("Traceback", out)
        generations = self.generations()
        self.assertEqual(len(generations), 1, f"one generation folder with its generation.json: {generations}\n{out}")
        return code, out, generations[0]

    def generations(self):
        runs = os.path.join(self.evo, "runs")
        return sorted(d for d in (os.listdir(runs) if os.path.isdir(runs) else [])
                      if os.path.isfile(os.path.join(runs, d, "generation.json")))

    @staticmethod
    def printed_commands(out):
        """Every `evolve.py generation ...` command the text prints, as argv (after the script) and its options."""
        found = []
        for line in out.splitlines():
            m = re.search(r"evolve(?:\.py)?[\"']?\s+(generation\s.*)$", line)
            if not m:
                continue
            text = m.group(1).split("`")[0]
            try:
                tokens = shlex.split(text)
            except ValueError:
                continue
            argv, opts, i = ["generation"], {}, 1
            while i < len(tokens) and tokens[i].startswith("--"):
                tok = tokens[i].rstrip(".,;)")
                if "=" in tok:
                    name, value = tok.split("=", 1)
                    argv.append(tok)
                    opts[name] = value
                    i += 1
                elif tok == "--yes":
                    argv.append(tok)
                    opts[tok] = True
                    i += 1
                elif i + 1 < len(tokens):
                    value = tokens[i + 1].rstrip(".,;)")
                    argv += [tok, value]
                    opts[tok] = value
                    i += 2
                else:
                    break
            found.append((argv, opts))
        return found

    def continue_argv(self, out, gen):
        """The printed command to continue: it names the id, target, budget and base. Returns its argv."""
        commands = [(argv, opts) for argv, opts in self.printed_commands(out) if "--id" in opts]
        self.assertTrue(commands, f"prints the command to continue, with --id:\n{out}")
        argv, opts = commands[-1]
        self.assertEqual(opts.get("--id"), gen, f"the command names this generation's id ({gen}):\n{out}")
        self.assertEqual(opts.get("--target"), TARGET, f"and the target it was started with:\n{out}")
        self.assertEqual(str(opts.get("--budget", "")).replace(",", "").replace("_", ""), str(self.BUDGET),
                         f"and its budget:\n{out}")
        self.assertEqual(opts.get("--from"), "seed", f"and its base:\n{out}")
        return argv if "--yes" in argv else argv + ["--yes"]  # --yes stands in for answering y on the terminal

    def assert_continued(self, out, gen, run=2):
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.generations(), [gen], "running the printed command started no new generation")
        later = [r for r in self.replays if r["run"] == run]
        self.assertEqual({r["generation"] for r in later}, {gen}, "its replays are filed under the same generation")
        finished_before = set().union(*(self.finished_calls(n) for n in range(1, run)))
        self.assertEqual(finished_before & self.finished_calls(run), set(), "no finished replay runs again")
        self.assertEqual([r for r in later if r["genome"] == "seed" and r["kind"] == "screen"], [],
                         "the base's screening replays aren't repeated")
        self.assertEqual(len(self.made), 2, f"the generation reached its population: {self.mutations}\n{out}")
        self.assertEqual(self.state(gen).get("children"), self.made)

    def test_a_replay_paused_at_the_usage_limit_prints_the_exact_command_with_its_id(self):  # covers: LOOP-15
        self.pause = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 2
        code, out, gen = self.start()
        self.assertEqual(code, self.ev.PAUSED_EXIT, out)
        self.assertEqual(self.state(gen).get("stopped"), "paused at the usage limit")
        argv = self.continue_argv(out, gen)
        paused = [(r["case"], r["genome"]) for r in self.replays if r["paused"]]
        code, out = self.invoke(argv)
        self.assertEqual(code, 0, out)
        second = [r for r in self.replays if r["run"] == 2]
        self.assertEqual([(second[0]["case"], second[0]["genome"])], paused, "the paused replay comes first")
        self.assertTrue(second[0]["resume"], "and it is resumed, not started again")
        self.assert_continued(out, gen)

    def test_a_reflection_paused_at_the_usage_limit_prints_the_exact_command_with_its_id(self):  # covers: LOOP-15
        self.limit_mutations = {1}
        code, out, gen = self.start()
        self.assertEqual(len(self.mutations), 1, f"the usage limit stopped the generation: {self.mutations}\n{out}")
        self.assertEqual(self.made, [])
        self.assertEqual(self.state(gen).get("stopped"), "paused at the usage limit")
        argv = self.continue_argv(out, gen)
        code, out = self.invoke(argv)
        self.assertEqual(code, 0, out)
        again = [m for m in self.mutations if m["run"] == 2]
        self.assertEqual(again[0]["failure"], self.mutations[0]["failure"],
                         "the reflection the limit stopped is tried again first")
        self.assert_continued(out, gen)

    def test_an_interrupted_replay_prints_the_exact_command_with_its_id(self):  # covers: LOOP-15
        self.interrupt = lambda role, kind, nth: role == 1 and kind == "screen" and nth == 1  # Ctrl-C
        code, out, gen = self.start()
        self.assertNotEqual(code, 0, out)
        self.assertEqual(len(self.made), 1, "fixture: interrupted during the first child's first replay")
        argv = self.continue_argv(out, gen)
        code, out = self.invoke(argv)
        self.assertEqual(code, 0, out)
        self.assertEqual([m["child"] for m in self.mutations if m["run"] == 2], self.made[1:],
                         "the child made before the interruption is reused")
        self.assert_continued(out, gen)


if __name__ == "__main__":
    unittest.main()
