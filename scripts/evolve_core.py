#!/usr/bin/env python3
"""The target-neutral core of the evolver: one generation of genomes behind an adapter.

An adapter (a subclass of Adapter) says what a generation runs on: its cases (screening and held-out), how one case
is run with one individual, the axes a run is judged on, its score for parent selection and the failures a
reflection can aim at. This module knows nothing about any particular target: it screens the base, breeds children
aimed at one failure each through a pluggable reflection, keeps the Pareto front over the adapter's axes, runs the
best front members on the held-out cases and picks the winner from those runs only. Its state is
runs/<generation>/generation.json, rewritten after every action, so a stopped generation is continued by building
a new Generation from that file's content.

It also writes a generation's report (report_text) and promotes its winner as a pull request (promote), both in the
adapter's words.

Standard library only, plus genome.py beside this file (imported the way evolve.py imports it)."""
import contextlib
import datetime as dt
import difflib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import genome as genome_lib  # noqa: E402

DEFAULT_RUN_ESTIMATE = 155000  # tokens one run is estimated at while runs/ holds no finished run to take a mean of
REFLECTION_ESTIMATE = 30000  # tokens one reflection is estimated at
HOLDOUT_CANDIDATES = 3  # front members (besides the base) run on the held-out cases, highest score first
TRIES_PER_PICK = 3  # failures of one parent pick tried in turn while reflections fail or return near-copies
GENERATION_FILE = "generation.json"  # in runs/<generation>/: the generation's state, rewritten after every action
RESULT_FILE = "result.json"  # in runs/<generation>/<case>/<genome>/: what the adapter's run left
# generation.json's spent: {SPENT_RUNS: the runs' tokens, "reflections": ..., "total": ...}; the key keeps the name
# the first generations were written with, so their records still read the same.
SPENT_RUNS = "replays"
STOP_BUDGET = "budget"
STOP_NOTHING_LEFT = "nothing left to aim at"
STOP_PAUSED = "paused at the usage limit"
STOP_INTERRUPTED = "interrupted"
PAUSED = "paused"  # the outcome of a run a usage limit stopped: the generation stops, and resumes it when continued
UNFINISHED_OUTCOMES = ("paused", "interrupted", "aborted")  # runs that didn't finish: run again (paused: resumed)
PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class EvolveError(Exception):
    """A problem the user can fix; the command line prints its message as one line and exits 1."""


class AdapterError(EvolveError):
    """An adapter that breaks the contract (check_adapter): nothing runs and nothing is written."""


class ReflectionFailed(EvolveError):
    """One reflection failed in a way the next one may not (the model's run failed, timed out or answered without
    valid JSON, it returned the parent's text unchanged, or its rewrite broke a genome rule). tokens is what the run
    reported it spent ({"used": ...}), or None when it reported nothing. A generation records it as a discarded try
    and goes on."""

    def __init__(self, message, tokens=None):
        super().__init__(message)
        self.tokens = tokens


class ReflectionLimit(EvolveError):
    """The reflection hit a usage limit: every reflection would, until it resets. tokens is what was reported spent
    before the limit ({"used": ...}), or None when nothing was reported. A generation pauses on it."""

    def __init__(self, message, tokens=None):
        super().__init__(message)
        self.tokens = tokens


class GenerationStop(Exception):
    """Ends a generation's loop or its held-out phase; reason is one of the STOP_ strings."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- small helpers

def safe_name(name):
    return isinstance(name, str) and bool(PLAIN_NAME.fullmatch(name)) and ".." not in name


def shown(value):
    """A value as it reads in a sentence: a string as it is, anything else as its repr."""
    return value if isinstance(value, str) else repr(value)


def read_json(path):
    """The JSON object in path, or None when it is missing, unreadable or not an object."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    """Write JSON through a temporary file and a rename, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def names_list(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def tokens_text(n):
    return f"{int(n):,}"


def tokens_used(record):
    """tokens.used of a result.json or mutation.json (a whole number of at least 0), else 0."""
    tokens = record.get("tokens") if isinstance(record, dict) else None
    used = tokens.get("used") if isinstance(tokens, dict) else None
    return int(used) if genome_lib.is_number(used) and used >= 0 else 0


def attempt_key(result):
    """What tells one run attempt's result.json from the next one's in the same folder."""
    return [result.get("session_id"), result.get("started_at"), result.get("ended_at")]


def aggregate(adapter, results):
    """Per-axis means over an adapter's runs (each axis over the runs that have a value for it; None when none
    has)."""
    rows = [adapter.values(r) for r in results]
    out = {}
    for axis in adapter.axes:
        values = [row.get(axis) for row in rows if genome_lib.is_number(row.get(axis))]
        out[axis] = round(sum(values) / len(values), 6) if values else None
    return out


def axis_order(value, better):
    """A value's place on its axis, larger is better; a missing value is below every number."""
    if not genome_lib.is_number(value):
        return (0, 0)
    return (1, value if better == "higher" else -value)


# ---------------------------------------------------------------- runs on disk

def finished_record(folder):
    """result.json of a run folder when the run finished with an outcome, else None: still running (no
    result.json), no outcome, or an unfinished one (paused, interrupted or aborted)."""
    result = read_json(os.path.join(folder, RESULT_FILE))
    if result is None:
        return None
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or not outcome.strip() or outcome in UNFINISHED_OUTCOMES:
        return None
    return result


def run_folders(home, generation=None):
    """(generation, case, genome, folder) of every generation run folder in runs/, or in runs/<generation>/ only. A
    genome folder whose name holds `--` isn't one of the generation's runs and is skipped (an adapter may keep other
    runs of a case there)."""
    root = os.path.join(home, "runs")
    if generation is not None:
        generations = [generation]
    else:
        try:
            generations = sorted(os.listdir(root))
        except OSError:
            return
    for gen in generations:
        if not safe_name(gen) or not os.path.isdir(os.path.join(root, gen)):
            continue
        try:
            cases = sorted(os.listdir(os.path.join(root, gen)))
        except OSError:
            continue
        for case in cases:
            try:
                genomes = sorted(os.listdir(os.path.join(root, gen, case)))
            except OSError:
                continue
            for genome in genomes:
                folder = os.path.join(root, gen, case, genome)
                if safe_name(genome) and "--" not in genome and os.path.isdir(folder):
                    yield gen, case, genome, folder


def run_estimate(home):
    """(tokens one run is estimated at, how many finished runs that mean is over): the mean tokens.used of the
    finished runs anywhere in runs/, or DEFAULT_RUN_ESTIMATE when there are none."""
    used = []
    for _, _, _, folder in run_folders(home):
        result = finished_record(folder)
        tokens = result.get("tokens") if result else None
        value = tokens.get("used") if isinstance(tokens, dict) else None
        if genome_lib.is_number(value) and value >= 0:
            used.append(value)
    return (int(round(sum(used) / len(used))), len(used)) if used else (DEFAULT_RUN_ESTIMATE, 0)


def replaced_attempts(home, generation, abandoned):
    """The abandoned attempts (generation.json's `abandoned`) whose result.json a rerun has since replaced. One whose
    result.json is still in place (the generation stopped before running it again) is that folder's current result,
    so it is counted with the run folders, not again here."""
    current = {}
    for _, case, genome, folder in run_folders(home, generation):
        result = read_json(os.path.join(folder, RESULT_FILE))
        current[(case, genome)] = attempt_key(result) if result else None
    return [a for a in abandoned or [] if current.get((a.get("case"), a.get("genome"))) != a.get("attempt")]


# ---------------------------------------------------------------- the adapter contract

class Adapter:
    """What a generation runs on. Subclass it and set:
    - name: a plain name (what the cases come from, such as a project), stored as generation.json's `project`;
    - axes: {axis: "higher" | "lower"} in order, the selection axes, never collapsed into one number;
    - labels: {axis: short plain label} for printed text (an axis with no label reads with its underscores as spaces);
    - must_not_worsen: the axes a winner may not be worse than the base on, on the held-out runs.
    and implement cases, run, values, score and failures (see each method).

    The rest are the words the printed text, the report and promote use; each has a plain default:
    - run_word, run_plural and run_done: one run, several runs ("runs" when run_plural is None) and what was done
      to a genome on a case ("run", as in "the base wasn't run on every held-out case");
    - headers: {axis: the report's column header} (default: the label, capitalised); words: {axis: the words the
      report's sentences use} (default: the label); axes_sentence: the report's sentence saying which way is better
      on each axis (default: built from the axes and their words);
    - percent_axes: the axes whose values are shares from 0 to 1, shown as percentages, their gap in percentage
      points; share_words: what the report calls them together (default: their words); count_axes: the axes whose
      value is 1 or 0 per run, shown as "yes"/"no" for one run and "k of n" for several;
    - step_word: what the report calls one case where it counts held-out ones or the screening ones a child may be
      fitted to ("case"); cases_word: what the cases come from ("the cases"); name_words: what the adapter's name
      names, before it ("the project"); limit_words: what a pause waits for
      ("the usage limit"); target_word: what the target's files make up ("the target");
    - promote_words: how the report names the promote command; fix_words: how a red-team generation's findings are
      fixed ("by hand"); remote_words: what to do when promote's remote is missing ("name another remote");
    - hidden_variables: environment variables promote's git calls, checks and pull-request command never see.
    report_path, report_cases, light_total and continue_hint are methods with plain defaults too."""

    name = None
    axes = {}
    labels = {}
    must_not_worsen = ()
    run_word = "run"
    run_plural = None
    run_done = "run"
    headers = {}
    words = {}
    axes_sentence = None
    percent_axes = ()
    share_words = None
    count_axes = ()
    step_word = "case"
    cases_word = "the cases"
    name_words = "the project"
    limit_words = "the usage limit"
    target_word = "the target"
    promote_words = "the promote command"
    fix_words = "by hand"
    remote_words = "name another remote"
    hidden_variables = ()

    def cases(self):
        """{"screening": [case, ...], "holdout": [case, ...]}, each case a dict with at least an `id` (a plain folder
        name, unique within the adapter)."""
        raise NotImplementedError

    def run(self, case, genome, individual, folder, resume=None):
        """Run one case with one individual (genome its id, individual the absolute path of its expanded folder) and
        leave folder/result.json: a JSON object with at least `outcome` (a non-empty string) and `tokens`: {"used":
        whole number}. The outcome "paused" means a usage limit stopped it: the generation stops, and when continued
        calls run again with resume set to that result.json's content. "interrupted" and "aborted" are run again."""
        raise NotImplementedError

    def values(self, result):
        """{axis: number or None} for every axis in axes; a hard failure shows as the worst value on every axis."""
        raise NotImplementedError

    def score(self, result):
        """A number from 0 to 1 for parent selection (0 for a hard failure)."""
        raise NotImplementedError

    def failures(self, case, genome, folder, result):
        """The run's failures a reflection can aim at: [{"id", "kind", "case", "summary", "evidence"}], each id
        starting with the case's id and a slash (the report finds a failure again from its id)."""
        raise NotImplementedError

    def estimate(self, home):
        """Tokens one run is estimated at: the mean tokens.used of the finished runs under home/runs/, or
        DEFAULT_RUN_ESTIMATE when there are none."""
        return run_estimate(home)[0]

    def individual(self, home, genome, profile):
        """The absolute path of a stored genome's expanded folder, once it is checked against its genome.json."""
        try:
            return os.path.abspath(genome_lib.check_individual(home, genome, profile))
        except genome_lib.GenomeError as e:
            raise EvolveError(str(e)) from None

    def label(self, axis):
        labels = self.labels if isinstance(self.labels, dict) else {}
        return labels.get(axis) or axis.replace("_", " ")

    def runs_word(self):
        """The word for several runs: run_plural, else run_word with an s."""
        return self.run_plural or f"{self.run_word}s"

    def header(self, axis):
        """An axis's column header in the report: headers' entry, else its label with a capital first letter."""
        headers = self.headers if isinstance(self.headers, dict) else {}
        text = headers.get(axis) or self.label(axis)
        return text[:1].upper() + text[1:]

    def axis_words(self, axis):
        """An axis as the report's sentences name it: words' entry, else its label."""
        words = self.words if isinstance(self.words, dict) else {}
        return words.get(axis) or self.label(axis)

    def better_sentence(self):
        """The report's sentence saying which way is better on each axis: axes_sentence, else one built from the
        axes, such as "More cases passed are better; fewer errors and tokens are better."."""
        if self.axes_sentence:
            return self.axes_sentence
        higher = [self.axis_words(a) for a, way in self.axes.items() if way == "higher"]
        lower = [self.axis_words(a) for a, way in self.axes.items() if way == "lower"]
        parts = ([f"more {' and more '.join(higher)} are better"] if higher else []) + \
                ([f"fewer {names_list(lower)} are better"] if lower else [])
        text = "; ".join(parts)
        return text[:1].upper() + text[1:] + "."

    def report_path(self, gen_id):
        """Where promote puts the generation's report inside the target's repository."""
        return f"evolve-reports/{gen_id}.md"

    def report_cases(self, home, state):
        """(screening case ids, held-out case ids) the report lists, or None to list the cases the generation
        recorded results on. By default cases()' lists, when both are given; when cases() raises (the case list is
        gone or broken), None, so the report still renders from what the generation recorded."""
        try:
            cases = self.cases()
        except Exception:  # noqa: BLE001 - any failure of the adapter's own code falls back to the recorded cases
            return None
        if not isinstance(cases, dict):
            return None
        lists = []
        for key in ("screening", "holdout"):
            found = cases.get(key)
            if not isinstance(found, list) or not found:
                return None
            lists.append([c["id"] for c in found if isinstance(c, dict) and "id" in c])
        return lists[0], lists[1]

    def light_total(self, state):
        """How many held-out cases a light generation's (state's holdout_cases) were picked from, or None when that
        isn't recorded."""
        return None

    def continue_hint(self, gen_id):
        """How to continue an unfinished generation, for promote's refusal, when its state has no `continue`
        command."""
        return f"the command that started the generation {gen_id}, run again"


def check_adapter(adapter):
    """(screening cases, held-out cases) of a well-formed adapter, or a plain AdapterError naming what is wrong."""
    name = getattr(adapter, "name", None)
    if not safe_name(name):
        raise AdapterError(f"The adapter's name {name!r} isn't a plain name: use letters, digits, _ . and -, "
                           f"starting with a letter or digit.")
    axes = getattr(adapter, "axes", None)
    if not isinstance(axes, dict) or not axes:
        raise AdapterError(f"The adapter {name} has no axes: it needs at least one axis, each \"higher\" or "
                           f"\"lower\" (which way is better).")
    for axis, way in axes.items():
        if not isinstance(axis, str) or not axis:
            raise AdapterError(f"The adapter {name} has an axis named {shown(axis)!s}; each axis needs a plain name.")
        if way not in ("higher", "lower"):
            raise AdapterError(f"The adapter {name}'s axis {axis} goes {shown(way)!s}; each axis must be \"higher\" "
                               f"or \"lower\" (which way is better).")
    labels = getattr(adapter, "labels", None)
    if not isinstance(labels, dict):
        raise AdapterError(f"The adapter {name}'s labels must be a dict of axis names to short labels.")
    protected = getattr(adapter, "must_not_worsen", None)
    if not isinstance(protected, (list, tuple)):
        raise AdapterError(f"The adapter {name}'s must_not_worsen must be a list of its axis names.")
    for axis in protected:
        if axis not in axes:
            raise AdapterError(f"The adapter {name} lists {shown(axis)!s} in must_not_worsen, but it has no axis "
                               f"{shown(axis)!s}.")
    cases = adapter.cases()
    if not isinstance(cases, dict):
        raise AdapterError(f"The adapter {name}'s cases() must give a dict with a screening list and a holdout "
                           f"(held-out) list, not a {type(cases).__name__}.")
    lists = {}
    for key, words in (("screening", "screening"), ("holdout", "held-out")):
        found = cases.get(key)
        if not isinstance(found, list):
            raise AdapterError(f"The adapter {name}'s cases() has no {words} list ({key}).")
        lists[key] = found
    seen = {}
    for key, words in (("screening", "screening"), ("holdout", "held-out")):
        for case in lists[key]:
            if not isinstance(case, dict) or "id" not in case:
                raise AdapterError(f"A {words} case of the adapter {name} has no id; every case needs an id that is a "
                                   f"plain folder name.")
            cid = case["id"]
            if not safe_name(cid):
                raise AdapterError(f"The adapter {name}'s {words} case id {shown(cid)!s} isn't a plain folder name: "
                                   f"use letters, digits, _ . and -, starting with a letter or digit.")
            if cid in seen:
                if seen[cid] == key:
                    raise AdapterError(f"The adapter {name} lists the case {cid} twice in its {words} list; each case "
                                       f"id must be unique.")
                raise AdapterError(f"The adapter {name} has the case {cid} in both its screening and held-out lists; a "
                                   f"held-out case must never be screened on.")
            seen[cid] = key
    if not lists["screening"]:
        raise AdapterError(f"The adapter {name} has no screening case, so a generation would have nothing to screen "
                           f"on.")
    if not lists["holdout"]:
        raise AdapterError(f"The adapter {name} has no held-out case, so a generation couldn't pick a winner.")
    return list(lists["screening"]), list(lists["holdout"])


# ---------------------------------------------------------------- reflection and mutation

def next_child_id(home, parent):
    """<parent>-m<n>, n the first number from 1 that no genome has."""
    n = 1
    while os.path.lexists(os.path.join(home, "genomes", f"{parent}-m{n}")):
        n += 1
    return f"{parent}-m{n}"


def check_child_id(home, child_id):
    """A plain error, before any reflection runs, when child_id can't name a new genome."""
    if not isinstance(child_id, str) or not safe_name(child_id):
        raise EvolveError(f"The genome id {child_id!r} can't be used as a folder name: use letters, digits, _ . and "
                          f"-, starting with a letter or digit.")
    if os.path.lexists(os.path.join(home, "genomes", child_id)):
        raise EvolveError(f"The genome {child_id} already exists; pick another id for the child.")


def refuse_judge_target(profile, target):
    """A plain EvolveError when target is one of the profile's judge files: no reflection runs on it."""
    refusal = genome_lib.judge_refusal(profile, target)
    if refusal:
        raise EvolveError(f"{refusal} No reflection was run, and nothing was written.")


def duplicate_max_changed(config):
    """evolve.json's duplicate_max_changed (default genome.DEFAULT_MAX_CHANGED, 10), checked: a whole number of at
    least 0."""
    value = (config or {}).get("duplicate_max_changed", genome_lib.DEFAULT_MAX_CHANGED)
    try:
        return genome_lib.check_max_changed(value)
    except genome_lib.GenomeError as e:
        raise EvolveError(f"evolve.json: {e}")


def duplicate_check(home, profile, max_changed, child_id, parent, failure, kind):
    """make_child's check for one aimed mutation: discard the child when it is a near-duplicate."""
    def check(contents, traits):
        genome_lib.refuse_near_duplicate(home, profile, contents, traits, max_changed, child_id, parent, failure["id"],
                                         kind)
    return check


def mutate(home, config, profile, source, parent, failure, target, child_id, reflect):
    """Make one child of parent aimed at one failure: reflect(prompt) -> (reply text, model, tokens dict) explains
    why the run failed and rewrites the one evolvable file target against that failure; the child gets the rewrite
    through make_child (every genome rule applies) with genomes/<child>/mutation.json recording the parent, the
    target, the failure (id, kind, case, summary), the diagnosis, the model and the tokens.

    Before reflect is called, the target is checked against the profile's judges, then the failure, the child's id,
    the parent and the target (one of the parent's evolvable files). A reply without valid JSON, the parent's text
    returned unchanged or a rewrite that breaks a genome rule is a ReflectionFailed carrying the tokens, and nothing
    is written under genomes/; reflect itself raises ReflectionFailed or ReflectionLimit. A near-duplicate of a
    stored genome is discarded with genome.NearDuplicate (carrying the tokens) and a line in genomes/discarded.jsonl.
    Returns the mutation record."""
    refuse_judge_target(profile, target)
    genome_lib.check_failure(failure)
    max_changed = duplicate_max_changed(config)
    check_child_id(home, child_id)
    stored = genome_lib.load_genome(home, parent, profile)
    listed = {entry["path"]: entry for entry in stored["files"]}
    problem = genome_lib.candidate_path_problem(profile, target, listed, parent)
    if problem:
        raise EvolveError(f"{problem} No reflection was run, and nothing was written.")
    try:
        old = genome_lib.read_bytes(os.path.join(genome_lib.genome_dir(home, parent), "files", *target.split("/")))
        old_text = old.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise EvolveError(f"Can't read {target} of the genome {parent} as text ({getattr(e, 'strerror', None) or e}), "
                          f"so it can't be rewritten.")
    caps = genome_lib.file_caps(home, profile, parent, target)
    prompt = genome_lib.reflection_prompt(profile, target, old_text, failure, caps)
    reply, model, tokens = reflect(prompt)
    try:
        diagnosis, text = genome_lib.parse_reflection(reply)
    except genome_lib.GenomeError as e:
        raise ReflectionFailed(str(e), tokens) from e
    if text.rstrip() == old_text.rstrip():
        raise ReflectionFailed(f"The reflection returned {target} unchanged (identical to the parent {parent}'s "
                               f"text), so no child was made.", tokens)
    record = genome_lib.mutation_record(parent, "rewrite", failure, target=target, diagnosis=diagnosis, model=model,
                                        tokens=tokens)
    try:
        genome_lib.make_child(home, profile, source, child_id, [parent], files={target: text.encode("utf-8")},
                              records={genome_lib.MUTATION_FILE: record},
                              check=duplicate_check(home, profile, max_changed, child_id, parent, failure, "rewrite"))
    except genome_lib.NearDuplicate as e:
        e.tokens = tokens  # the reflection ran and spent these, though no child was kept: a generation counts them
        raise
    except genome_lib.GenomeError as e:  # the rewrite breaks a genome rule (a size cap, a trait out of bounds, ...)
        raise ReflectionFailed(str(e), tokens) from e
    return record


# ---------------------------------------------------------------- one generation

def new_state(gen_id, project, target, base, budget, seed):
    """A fresh generation.json state."""
    return {"id": gen_id, "project": project, "target": target, "base": base, "budget": budget, "seed": seed,
            "started_at": now_iso(), "updated_at": None, "spent": {SPENT_RUNS: 0, "reflections": 0, "total": 0},
            "stopped": None, "children": [], "screening": {}, "front": [], "holdout": {}, "winner": None,
            "why": None}


class Generation:
    """One generation: the base screened, children aimed at failures and screened, the Pareto front over the
    adapter's axes, the held-out runs and the winner. Its state is runs/<id>/generation.json, rewritten after every
    action, so a new Generation built from that file's content continues it.

    mutate is called as mutate(home, config, profile, source, parent, failure, target, child_id) and makes one child
    (raising genome.NearDuplicate, ReflectionFailed or ReflectionLimit when it can't). screening and holdout default
    to check_adapter(adapter)'s lists (the adapter is always checked first)."""

    def __init__(self, home, config, state, adapter, profile, source, population, mutate, screening=None,
                 holdout=None):
        checked_screening, checked_holdout = check_adapter(adapter)
        self.home, self.config, self.state, self.adapter = home, config, state, adapter
        self.screening = checked_screening if screening is None else list(screening)
        self.holdout = checked_holdout if holdout is None else list(holdout)
        self.profile, self.source, self.population, self.mutate = profile, source, population, mutate
        self.id, self.base, self.project = state["id"], state["base"], state["project"]
        if len(self.holdout) < len(checked_holdout):
            # A light generation: it checks only some of the adapter's held-out cases, so it records which, and the
            # report and promote treat it as finished once those ran. A full generation records neither key.
            state["light"] = True
            state["holdout_cases"] = [c["id"] for c in self.holdout]
        self.path = os.path.join(home, "runs", self.id, GENERATION_FILE)
        self.paused_reflection = None  # which reflection the usage limit stopped, when one did
        self.red_team = state.get("red_team") is True
        state.setdefault("discarded", [])
        state.setdefault("limited", [])  # reflections the usage limit stopped, kept for the tokens they spent
        state.setdefault("making", None)
        state.setdefault("abandoned", [])  # unfinished run attempts a rerun replaced, kept for the tokens they spent

    # Records ----------------------------------------------------------------------------------------------------------

    def folder(self, genome, case):
        return os.path.join(self.home, "runs", self.id, case["id"], genome)

    def results(self, genome, cases):
        """{case id: result.json or None} of a genome's runs on these cases in this generation."""
        return {c["id"]: read_json(os.path.join(self.folder(genome, c), RESULT_FILE)) for c in cases}

    def finished(self, genome, cases):
        """The finished runs of a genome on these cases, in their order."""
        return [r for r in (finished_record(self.folder(genome, c)) for c in cases) if r is not None]

    def complete(self, genome, cases):
        return len(self.finished(genome, cases)) == len(cases)

    def genomes(self):
        return [self.base] + list(self.state["children"])

    def mutation(self, child):
        return read_json(os.path.join(self.home, "genomes", child, genome_lib.MUTATION_FILE)) or {}

    def spent(self):
        """Tokens spent so far: every run's result.json, the abandoned attempts (an unfinished run whose result.json a
        rerun replaced) and every reflection, kept, discarded or stopped at the usage limit."""
        runs = sum(tokens_used(read_json(os.path.join(folder, RESULT_FILE)))
                   for _, _, _, folder in run_folders(self.home, self.id))
        # An abandoned attempt whose result.json is still in place is already counted above.
        runs += sum(tokens_used(a) for a in replaced_attempts(self.home, self.id, self.state["abandoned"]))
        reflections = sum(tokens_used(self.mutation(c)) for c in self.state["children"])
        reflections += sum(tokens_used(d) for d in self.state["discarded"])
        reflections += sum(tokens_used(p) for p in self.state["limited"])
        return {SPENT_RUNS: runs, "reflections": reflections, "total": runs + reflections}

    def outcomes(self, cases):
        out = {}
        for genome in self.genomes():
            found = {cid: r.get("outcome") for cid, r in self.results(genome, cases).items() if r is not None}
            if found:
                out[genome] = found
        return out

    def save(self):
        state = self.state
        state["updated_at"] = now_iso()
        state["spent"] = self.spent()
        state["screening"] = self.outcomes(self.screening)
        state["holdout"] = self.outcomes(self.holdout)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        write_json(self.path, state)

    # Axes -------------------------------------------------------------------------------------------------------------

    def aggregate(self, results):
        """Per-axis means over runs (each axis over the runs that have a value for it; None when none has)."""
        return aggregate(self.adapter, results)

    def mean_score(self, results):
        """The mean adapter score of finished runs (the score parent selection uses), or None for none."""
        return sum(self.adapter.score(r) for r in results) / len(results) if results else None

    # Actions ----------------------------------------------------------------------------------------------------------

    def check_budget(self, estimate, what):
        spent = self.spent()["total"]
        if spent + estimate > self.state["budget"]:
            print(f"The budget stops the generation: {tokens_text(spent)} tokens spent, and {what} is estimated at "
                  f"{tokens_text(estimate)} more, over the budget of {tokens_text(self.state['budget'])}.")
            raise GenerationStop(STOP_BUDGET)

    def run_case(self, genome, case, kind):
        """The finished run of genome on case in this generation: reused when it exists, resumed when it is paused,
        run otherwise (after the budget check)."""
        word = self.adapter.run_word
        folder = self.folder(genome, case)
        done = finished_record(folder)
        if done is not None:
            return done
        current = read_json(os.path.join(folder, RESULT_FILE))
        paused = current if current is not None and current.get("outcome") == PAUSED else None
        if current is not None and paused is None:
            self.abandon(genome, case, current)
        self.check_budget(self.adapter.estimate(self.home), f"the next {word}")
        individual = self.adapter.individual(self.home, genome, self.profile)
        print(f"{'Resuming the paused' if paused else 'Running the'} {word} of {genome} on the {kind} case "
              f"{case['id']}.", flush=True)
        self.adapter.run(case, genome, individual, folder, resume=paused)
        result = read_json(os.path.join(folder, RESULT_FILE))
        self.save()
        if result is None:
            raise EvolveError(f"The {word} of {genome} on {case['id']} left no readable {RESULT_FILE} in {folder}.")
        if result.get("outcome") == PAUSED:
            raise GenerationStop(STOP_PAUSED)
        print(f"  {case['id']} with {genome}: {str(result.get('outcome')).replace('_', ' ')}, "
              f"{tokens_text(tokens_used(result))} tokens.", flush=True)
        return result

    def abandon(self, genome, case, result):
        """Keep the tokens of an unfinished attempt (interrupted or aborted) in generation.json's `abandoned` before
        the run is started again and its result.json replaced, so they stay counted in spent (once per attempt)."""
        if tokens_used(result) <= 0:
            return
        key = attempt_key(result)
        if any(a.get("genome") == genome and a.get("case") == case["id"] and a.get("attempt") == key
               for a in self.state["abandoned"]):
            return
        self.state["abandoned"].append({"genome": genome, "case": case["id"], "outcome": result.get("outcome"),
                                        "attempt": key, "tokens": {"used": tokens_used(result)}})
        self.save()

    def screen(self, genome):
        for case in self.screening:
            self.run_case(genome, case, "screening")

    def aimed(self, parent):
        """The failure ids this generation already aimed at from parent: its children's and its discarded tries'."""
        ids = {(self.mutation(c).get("failure") or {}).get("id") for c in self.state["children"]
               if self.mutation(c).get("parent") == parent}
        ids |= {d.get("failure") for d in self.state["discarded"] if d.get("parent") == parent}
        return ids

    def open_failures(self, genome):
        """A fully screened genome's failures on the screening cases that none of its tries aimed at yet, in the
        order the adapter lists them, case by case."""
        if not self.complete(genome, self.screening):
            return []
        aimed = self.aimed(genome)
        found = []
        for case in self.screening:
            folder = self.folder(genome, case)
            listed = self.adapter.failures(case, genome, folder, finished_record(folder)) or []
            found += [f for f in listed if f["id"] not in aimed]
        return found

    def adopt_pending(self):
        """A child whose mutation finished just before a stop, before generation.json named it, is kept."""
        making = self.state.get("making")
        self.state["making"] = None
        if not isinstance(making, dict):
            return
        child = making.get("child")
        record = self.mutation(child) if isinstance(child, str) and safe_name(child) else {}
        if (record.get("parent") == making.get("parent")
                and (record.get("failure") or {}).get("id") == making.get("failure")
                and child not in self.state["children"]):
            self.mark_red_team(child)
            self.state["children"].append(child)

    def mark_red_team(self, child):
        """In a red-team generation, record "red_team": true in the child's mutation.json, so promotion refuses it and
        its descendants. Until it is marked, generation.json names it (making, then children)."""
        if not self.red_team:
            return
        path = os.path.join(self.home, "genomes", child, genome_lib.MUTATION_FILE)
        record = read_json(path)
        if not isinstance(record, dict):
            raise EvolveError(f"{path} isn't a mutation's record, so the red-team child {child} can't be marked.")
        if record.get("red_team") is not True:
            record["red_team"] = True
            write_json(path, record)

    def discard(self, parent, failure, kind, error, reason, **extra):
        """Record one try that made no child in generation.json's `discarded` (once per parent and failure), with the
        tokens its reflection reported, and save at once: that failure then counts as aimed at for that parent,
        also when the generation is stopped and run again."""
        tokens = getattr(error, "tokens", None)
        entry = {"parent": parent, "failure": failure["id"], "kind": kind, "reason": reason, **extra,
                 "tokens": tokens if isinstance(tokens, dict) else None}
        discarded = self.state["discarded"]
        if not any(d.get("parent") == parent and d.get("failure") == failure["id"] for d in discarded):
            discarded.append(entry)
        self.state["making"] = None
        self.save()

    def breed(self):
        """Make children until the population is reached, one aimed failure each, and screen each one. A near-copy or
        a failed reflection is recorded in `discarded` and the parent's next failure is tried; the usage limit
        pauses the generation, and any other plain error stops it."""
        children, discarded = self.state["children"], self.state["discarded"]
        while len(children) < self.population:
            entries, open_by = [], {}
            for genome in self.genomes():
                found = self.open_failures(genome)
                if found:
                    open_by[genome] = found
                    entries.append({"id": genome, "score": self.mean_score(self.finished(genome, self.screening)),
                                    "children": sum(1 for c in children if self.mutation(c).get("parent") == genome)})
            if not entries:
                raise GenerationStop(STOP_NOTHING_LEFT)
            # The random choice depends only on the seed and the state, so a continued generation picks as an
            # uninterrupted one would.
            rng = random.Random(f"{self.state['seed']}:{len(children)}:{len(discarded)}")
            parent = genome_lib.pick_parent(entries, rng)
            for failure in open_by[parent][:TRIES_PER_PICK]:
                self.check_budget(REFLECTION_ESTIMATE, "the next reflection")
                child = next_child_id(self.home, parent)
                self.state["making"] = {"child": child, "parent": parent, "failure": failure["id"]}
                self.save()
                print(f"Reflecting on {failure['id']} to make {child} from {parent}.", flush=True)
                try:
                    self.mutate(self.home, self.config, self.profile, self.source, parent, failure,
                                self.state["target"], child)
                except genome_lib.NearDuplicate as e:
                    self.discard(parent, failure, "near-copy", e,
                                 f"The rewrite was a near-copy of the stored genome {e.closest} ({e.changed} "
                                 f"characters changed), so it was discarded.", closest=e.closest, changed=e.changed)
                    print(f"  The rewrite was a near-copy of {e.closest} and was discarded; trying the next failure.",
                          flush=True)
                    continue
                except ReflectionFailed as e:
                    reason = " ".join(str(e).split())
                    self.discard(parent, failure, "reflection failed", e, reason)
                    print(f"  {reason} The try was discarded; trying the next failure.", flush=True)
                    continue
                except ReflectionLimit as e:
                    # Its tokens count as spent, but the failure isn't aimed at: a rerun tries it again first.
                    tokens = e.tokens if isinstance(e.tokens, dict) else None
                    self.state["limited"].append({"parent": parent, "failure": failure["id"], "child": child,
                                                  "at": now_iso(), "tokens": tokens})
                    self.state["making"] = None
                    self.save()
                    print(f"  {' '.join(str(e).split())}", flush=True)
                    self.paused_reflection = f"the reflection on {failure['id']} for {parent}"
                    raise GenerationStop(STOP_PAUSED)
                self.mark_red_team(child)
                children.append(child)
                self.state["making"] = None
                self.save()
                self.screen(child)
                break

    def select(self):
        """The Pareto front over the screened genomes, the held-out runs of the base and up to HOLDOUT_CANDIDATES
        front members, and the winner from the held-out runs only."""
        axes = self.adapter.axes
        screened = [g for g in self.genomes() if self.complete(g, self.screening)]
        entries = {g: self.aggregate(self.finished(g, self.screening)) for g in screened}
        front = genome_lib.pareto_front(entries, axes)
        self.state["front"] = front
        self.state["axes"] = {"screening": entries, "holdout": {}}
        scores = {g: self.mean_score(self.finished(g, self.screening)) for g in front}
        candidates = sorted((g for g in front if g != self.base), key=lambda g: (-scores[g], front.index(g)))
        candidates = candidates[:HOLDOUT_CANDIDATES]
        self.state["candidates"] = candidates
        self.save()
        try:
            # When no child is on the front there is no candidate (none finished screening, or the base beats every
            # one), and the base's held-out runs couldn't change the outcome, so none runs.
            for genome in ([self.base] + candidates) if candidates else []:
                for case in self.holdout:
                    self.run_case(genome, case, "held-out")
        except GenerationStop as e:
            if e.reason == STOP_PAUSED:
                raise
            self.state["stopped"] = e.reason
        self.state["axes"]["holdout"] = {g: self.aggregate(self.finished(g, self.holdout))
                                         for g in [self.base] + candidates if self.finished(g, self.holdout)}
        self.state["winner"], self.state["why"] = self.winner(candidates)
        self.save()

    def winner(self, candidates):
        """(winner or None, a plain sentence saying why) from the held-out runs only."""
        adapter = self.adapter
        axes, done = adapter.axes, adapter.run_done
        base, n = self.base, len(self.holdout)
        if not candidates:
            unscreened = [c for c in self.state["children"] if not self.complete(c, self.screening)]
            note = (f" ({names_list(unscreened)} didn't finish screening)" if unscreened else "")
            return None, (f"No child reached the front of the screened genomes{note}, so no candidate was compared "
                          f"with the base {base} on the held-out cases." if self.state["children"] else
                          "No child was made and screened, so there was no candidate to compare with the base.")
        if not self.complete(base, self.holdout):
            return None, (f"The base {base} wasn't {done} on every held-out case, so no candidate could be "
                          f"compared with it.")
        ref = self.aggregate(self.finished(base, self.holdout))
        qualified, reasons = [], []
        for genome in candidates:
            if not self.complete(genome, self.holdout):
                reasons.append(f"{genome} wasn't {done} on every held-out case")
                continue
            mine = self.aggregate(self.finished(genome, self.holdout))
            worse = [axis for axis in adapter.must_not_worsen
                     if axis_order(mine[axis], axes[axis]) < axis_order(ref[axis], axes[axis])]
            better = [axis for axis, way in axes.items() if axis_order(mine[axis], way) > axis_order(ref[axis], way)]
            if worse:
                reasons.append(f"{genome} did worse than the base on {names_list(adapter.label(a) for a in worse)}")
            elif not better:
                reasons.append(f"{genome} did no better than the base on any axis")
            else:
                qualified.append((genome, better))
        if not qualified:
            return None, (f"No candidate beat the base {base} on the {plural(n, 'held-out case')}: "
                          f"{'; '.join(reasons)}.")

        def rank(entry):
            genome = entry[0]
            results = self.finished(genome, self.holdout)
            used = [r["tokens"]["used"] for r in results if isinstance(r.get("tokens"), dict)
                    and genome_lib.is_number(r["tokens"].get("used"))]
            tokens = sum(used) / len(used) if used else float("inf")
            return (-self.mean_score(results), tokens, genome)

        genome, better = min(qualified, key=rank)
        others = f", the best held-out score of the {len(qualified)} that qualified" if len(qualified) > 1 else ""
        kept = list(adapter.must_not_worsen)
        lead = (f"was no worse than the base {base} on {names_list(adapter.label(a) for a in kept)} and better on"
                if kept else f"was better than the base {base} on")
        return genome, (f"On the {plural(n, 'held-out case')}, {genome} {lead} "
                        f"{names_list(adapter.label(a) for a in better)}{others}.")

    # The whole generation -----------------------------------------------------------------------------------------------

    def run(self):
        """Run (or continue) the generation; returns the stop reason, or None when the population was reached."""
        self.state["stopped"] = None
        self.state["winner"], self.state["why"] = None, None
        self.state["candidates"] = []
        self.adopt_pending()
        self.save()
        try:
            self.screen(self.base)
            for child in list(self.state["children"]):
                self.screen(child)
            self.breed()
        except GenerationStop as e:
            self.state["stopped"] = e.reason
            self.save()
            if e.reason == STOP_PAUSED:
                return STOP_PAUSED
        try:
            self.select()
        except GenerationStop as e:
            self.state["stopped"] = e.reason
            self.save()
        return self.state["stopped"]


# ---------------------------------------------------------------- the generation report

REPORT_DIFF_LINES = 200  # the most lines of the winner's diff a report shows; the rest is summarised in one line
REPORT_SUMMARY_CAP = 120  # the most characters of a failure's summary in the report's screening table
QUOTE_CAP = 200  # the most characters of a text quoted in a one-line summary


def one_line(text, cap=QUOTE_CAP):
    """text on one line (every run of whitespace one space), cut to cap characters with an ellipsis."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap - 3].rstrip() + "..."


def possessive(words):
    """"the cases" -> "the cases'", "the suite" -> "the suite's"."""
    return words + ("'" if words.endswith("s") else "'s")


def case_folder(home, gen_id, case, genome):
    """runs/<generation>/<case>/<genome>/: one genome's run on one case."""
    return os.path.join(home, "runs", gen_id, case, genome)


def stop_words(adapter):
    """{stop reason: the clause saying why a generation stopped}, in the adapter's words."""
    return {STOP_BUDGET: f"the budget would have run out before the next reflection or {adapter.run_word}, so "
                         f"nothing more was started",
            STOP_NOTHING_LEFT: "no genome had a failure left that none of its children had aimed at",
            STOP_PAUSED: f"a {adapter.run_word} was paused at {adapter.limit_words}",
            STOP_INTERRUPTED: "it was interrupted (Ctrl-C or a stop signal) before the end"}


def cell(text):
    """Text for one Markdown table cell: on one line, with its pipes escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")


def table(header, rows):
    lines = ["| " + " | ".join(cell(h) for h in header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]


def figure(value):
    """A mean as people read it: a whole number with thousands separators, else one decimal."""
    if not genome_lib.is_number(value):
        return "–"
    return f"{int(round(value)):,}" if abs(value - round(value)) < 0.05 else f"{value:,.1f}"


def axis_cells(adapter, results):
    """One cell per axis of a genome's finished runs (their means), or dashes when none finished: a count axis as
    yes or no (one run) or "k of n", a share axis as a percentage, any other as figure() writes it."""
    if not results:
        return ["–"] * len(adapter.axes)
    means, n = aggregate(adapter, results), len(results)
    cells = []
    for axis in adapter.axes:
        value = means[axis]
        if axis in adapter.count_axes:
            k = int(round((value or 0) * n))
            cells.append(("yes" if k else "no") if n == 1 else f"{k} of {n}")
        elif axis in adapter.percent_axes:
            cells.append(f"{value * 100:.0f}%" if genome_lib.is_number(value) else "–")
        else:
            cells.append(figure(value))
    return cells


def light_cases(state):
    """The held-out cases a light generation recorded, or None for a full generation."""
    cases = state.get("holdout_cases")
    if state.get("light") is not True or not isinstance(cases, list):
        return None
    return [str(c) for c in cases]


def report_cases(home, state, adapter):
    """(screening case ids, held-out case ids) of a generation: the adapter's, else the ones it recorded results
    on; a light generation's held-out cases are the ones it recorded."""
    found = adapter.report_cases(home, state)
    if found is None:
        screening, holdout = [sorted({s for by in (state.get(key) or {}).values() for s in (by or {})})
                              for key in ("screening", "holdout")]
    else:
        screening, holdout = list(found[0]), list(found[1])
    light = light_cases(state)
    return screening, holdout if light is None else light


def report_failure_text(home, gen_id, adapter, failure_id, parent):
    """A failure's summary, found again from parent's run on the case its id starts with; else that case."""
    case = str(failure_id).partition("/")[0]
    try:
        if safe_name(case) and safe_name(parent):
            folder = case_folder(home, gen_id, case, parent)
            result = finished_record(folder)
            for f in (adapter.failures({"id": case}, parent, folder, result) or []) if result is not None else []:
                if f["id"] == failure_id:
                    return f"“{one_line(f['summary'], REPORT_SUMMARY_CAP)}” (case {case})"
    except (EvolveError, genome_lib.GenomeError, OSError, ValueError, KeyError, TypeError):
        pass
    return f"a failure on the case {case}"


def diff_range(start, end):
    """A hunk header's range, written as difflib's unified diff writes it."""
    length = end - start
    if length == 1:
        return f"{start + 1}"
    return f"{start if length == 0 else start + 1},{length}"


def paired_diff(old, new, fromfile, tofile, context=3):
    """A unified diff of two lists of lines (difflib's hunks, with 3 lines of context) in which each block of changed
    lines alternates its removed and added lines, one of each in turn: a diff cut short then still shows what was
    removed next to what replaced it, not only the start of a long removal."""
    lines = [f"--- {fromfile}", f"+++ {tofile}"]
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(context):
        lines.append(f"@@ -{diff_range(group[0][1], group[-1][2])} +{diff_range(group[0][3], group[-1][4])} @@")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [" " + line for line in old[i1:i2]]
                continue
            removed, added = old[i1:i2], new[j1:j2]
            for k in range(max(len(removed), len(added))):
                lines += (["-" + removed[k]] if k < len(removed) else []) + (["+" + added[k]] if k < len(added) else [])
    return lines if len(lines) > 2 else []


def winner_diff(home, base, winner, target):
    """The lines of the report's diff section: the winner's target file against the base's, as a unified diff of at
    most REPORT_DIFF_LINES lines in a fenced block, then one line saying how many were left out."""
    texts = []
    for genome in (base, winner):
        path = os.path.join(home, "genomes", genome, "files", *target.split("/"))
        try:
            texts.append(genome_lib.read_bytes(path).decode("utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            return [f"The diff can't be shown: {genome}'s {target} can't be read ({getattr(e, 'strerror', None) or e})."]
    diff = paired_diff(texts[0].splitlines(), texts[1].splitlines(), f"{base}/{target}", f"{winner}/{target}")
    if not diff:
        return [f"{winner}'s {target} is the same as the base's."]
    shown_lines, rest = diff[:REPORT_DIFF_LINES], len(diff) - REPORT_DIFF_LINES
    fence = "~~~" if any(line[1:].lstrip().startswith("```") for line in shown_lines) else "```"
    lines = [f"How {winner} changed {target}, against the base {base}:", "", fence + "diff", *shown_lines, fence]
    if rest > 0:
        lines += ["", f"The diff has {plural(len(diff), 'line')}; the other {rest:,} lines are not shown."]
    return lines


def gap_value(adapter, axis, value):
    """One axis mean as the gap section shows it: a percentage for a share, else as figure() writes it."""
    if not genome_lib.is_number(value):
        return "–"
    return f"{value * 100:.0f}%" if axis in adapter.percent_axes else figure(value)


def gap_text(adapter, axis, screened, held):
    """(the gap cell, the signed amount alone, whether held out is worse than screening): the held-out mean minus the
    screening one, signed, in percentage points for a share, with whether that is better or worse on that axis."""
    if not (genome_lib.is_number(screened) and genome_lib.is_number(held)):
        return "–", "–", False
    diff = held - screened
    if axis in adapter.percent_axes:
        points = round(diff * 100)
        amount = f"{points:+d} points" if points else "0 points"
    else:
        figured = figure(abs(diff))
        amount = "0" if figured == "0" else f"{'+' if diff > 0 else '-'}{figured}"
    if amount in ("0", "0 points"):
        return f"{amount}, the same", amount, False
    worse = (diff < 0) if adapter.axes[axis] == "higher" else (diff > 0)
    return f"{amount}, {'worse' if worse else 'better'} held out", amount, worse


def gap_section(adapter, base, winner, runs):
    """The report's section comparing, for the winner and the base, each axis on the screening cases with the same
    axis on the held-out cases. runs is {genome: (finished screening runs, finished held-out runs)}."""
    steps = f"screening {adapter.step_word}s"
    shares = ""
    percent = [a for a in adapter.axes if a in adapter.percent_axes]
    if percent:
        words = adapter.share_words or names_list(adapter.axis_words(a) for a in percent)
        shares = (f"; {words} are shares, so their gap is in percentage points" if len(percent) > 1 else
                  f"; {words} is a share, so its gap is in percentage points")
    out = ["## Screening against held-out: the gap", "",
           "How the winner and the base did on the screening cases, where the children were made and the front was "
           "picked, against the held-out cases, which no child was shaped on. The gap is the held-out figure minus "
           f"the screening figure{shares}. A big gap for the winner, above all one worse than the base's, means it "
           f"may have fitted the {steps} rather than got better in general. A gap the base shows too comes from the "
           "cases being harder or easier, not from the winner's change.", ""]
    rows, worse_parts = [], []
    means = {g: tuple(aggregate(adapter, r) if r else {a: None for a in adapter.axes} for r in runs[g])
             for g in (winner, base)}
    for genome in (winner, base):
        screened, held = means[genome]
        name = f"{genome} (the winner)" if genome == winner else f"{genome} (the base)"
        for axis in adapter.axes:
            gap, amount, worse = gap_text(adapter, axis, screened[axis], held[axis])
            rows.append([name, adapter.header(axis), gap_value(adapter, axis, screened[axis]),
                         gap_value(adapter, axis, held[axis]), gap])
            if genome == winner and worse:
                base_amount = gap_text(adapter, axis, means[base][0][axis], means[base][1][axis])[1]
                worse_parts.append(f"{adapter.axis_words(axis)} ({amount}; the base: {base_amount})")
    out += table(["Genome", "Axis", "Screening", "Held out", "Gap"], rows) + [""]
    if worse_parts:
        out += [f"Held out, {winner} did worse than on screening on {names_list(worse_parts)}. Where the base's gap is "
                f"as large, the cases explain it; where the winner's is much larger, its change may be fitted to the "
                f"{steps}.", ""]
    else:
        out += [f"{winner} did no worse on the held-out cases than on screening on any axis, so nothing here suggests "
                f"it fitted the {steps}.", ""]
    return out


def report_text(home, state, adapter):
    """A generation's report, in the adapter's words: Markdown for a person reading it cold."""
    gen_id, base, target, project = state["id"], state.get("base"), state.get("target"), state.get("project")
    children, front = list(state.get("children") or []), list(state.get("front") or [])
    discarded, limited = list(state.get("discarded") or []), list(state.get("limited") or [])
    stopped, winner = state.get("stopped"), state.get("winner")
    why = " ".join(str(state.get("why") or "").split())
    finished = bool(winner or why)
    front_known = finished or isinstance(state.get("axes"), dict)
    screening, holdout = report_cases(home, state, adapter)
    genomes = [base] + children
    mutations = {c: read_json(os.path.join(home, "genomes", c, genome_lib.MUTATION_FILE)) or {} for c in children}
    run, runs, done = adapter.run_word, adapter.runs_word(), adapter.run_done
    stops = stop_words(adapter)
    step = adapter.step_word

    def finished_on(genome, cases):
        return [r for r in (finished_record(case_folder(home, gen_id, s, genome)) for s in cases) if r is not None]

    # Children on the screening front that the held-out check (the base's and their held-out runs) was to compare
    # with the base; when it stopped before all of those runs finished, that, not the front, is why there is no
    # winner.
    candidates = [g for g in state.get("candidates") or [] if g in children]
    held_total = (len(candidates) + 1) * len(holdout) if candidates else 0
    held_done = sum(len(finished_on(g, holdout)) for g in [base] + candidates) if candidates else 0
    held_unfinished = bool(candidates) and not winner and held_done < held_total
    on_front = (f"{names_list(candidates)} {'is' if len(candidates) == 1 else 'are'} on the screening front, but "
                f"{f'only {held_done}' if held_done else 'none'} of the {held_total} held-out {runs} that compare "
                f"{'it' if len(candidates) == 1 else 'them'} with the base {base} finished")

    light = light_cases(state) is not None
    source_held = adapter.light_total(state)
    of_source = (f" of {possessive(adapter.cases_word)} {source_held}"
                 if isinstance(source_held, int) and not isinstance(source_held, bool) else "")
    light_note = (f"This is a light run: its held-out check covers only {plural(len(holdout), f'held-out {step}')}"
                  f"{of_source} ({', '.join(holdout) or 'none'}), so its result rests on less evidence than a full "
                  f"generation's. " if light else "")
    out = [f"# Generation {gen_id}", ""]
    if state.get("red_team") is True:
        out = [f"# Red-team generation {gen_id}", "",
               light_note + f"This is a red-team generation, whose results can never become a pull request: "
               f"{adapter.promote_words} refuses its genomes and every genome descended from them. It looks for routes "
               f"past the guards; each route found is fixed {adapter.fix_words}, never promoted as a genome.", ""]
        light_note = ""

    # The outcome first.
    if not finished:
        lead = {STOP_PAUSED: f"it is paused at {adapter.limit_words}",
                STOP_INTERRUPTED: "it was interrupted (Ctrl-C or a stop signal) before the end"}.get(
            stopped, "it is still running, or it was stopped before it could record why")
        if held_unfinished:
            first = (f"Generation {gen_id} stopped before the held-out check finished: {lead}. {on_front}, so there "
                     f"is no winner yet; this report shows what exists so far.")
        else:
            first = (f"Generation {gen_id} hasn't finished: {lead}. There is no winner yet; this report shows what "
                     f"exists so far ({plural(len(children), 'child', 'children')} made).")
        if state.get("continue"):
            first += f" The command below continues it; every finished {run} and child is reused."
    elif held_unfinished and stopped:
        first = (f"Generation {gen_id} stopped before the held-out check finished: {stops.get(stopped, stopped)}. "
                 f"{on_front}, so there is no winner.")
    else:
        first = f"Generation {gen_id} stopped early: {stops.get(stopped, stopped)}. " if stopped else ""
        if winner:
            first += f"{winner} won: its change to {target} beat the base. {why}"
        else:
            first += f"There is no winner. {why}"
    out += [(light_note + first).strip(), ""]
    if not finished and state.get("continue"):
        out += ["```", state["continue"], "```", ""]

    # What was evolved.
    light_order = (f", the first{of_source} in {possessive(adapter.cases_word)} stable order, as it is a light run"
                   if light else "")
    out += ["## What was evolved", "",
            f"The generation evolved {target}, starting from the base genome {base}, on {adapter.name_words} "
            f"{project}. Each genome was "
            f"screened on the {plural(len(screening), 'screening case')} ({', '.join(screening) or 'none'}); after "
            f"screening, the base and up to {HOLDOUT_CANDIDATES} genomes of the front are {done} on the "
            f"{plural(len(holdout), 'held-out case')} ({', '.join(holdout) or 'none'}){light_order}, and the winner "
            f"is picked from those {runs} alone.", ""]

    # Budget, spend and why it stopped.
    spent = state.get("spent") or {}
    replaced = replaced_attempts(home, gen_id, state.get("abandoned"))
    attempts = sum(1 for _ in run_folders(home, gen_id)) + len(replaced)
    reflections = len(children) + len(discarded) + len(limited)
    budget_lines = [f"The budget was {tokens_text(state.get('budget') or 0)} tokens. It spent "
                    f"{tokens_text(spent.get('total', 0))}: {tokens_text(spent.get(SPENT_RUNS, 0))} on "
                    f"{plural(attempts, run, runs)} and {tokens_text(spent.get('reflections', 0))} on "
                    f"{plural(reflections, 'reflection')}."]
    stopped_short = [r for r in (read_json(os.path.join(folder, RESULT_FILE))
                                 for _, _, _, folder in run_folders(home, gen_id))
                     if isinstance(r, dict) and r.get("outcome") in UNFINISHED_OUTCOMES and tokens_used(r)]
    if stopped_short:
        budget_lines.append(f"{plural(len(stopped_short), run, runs)} stopped before finishing and used "
                            f"{tokens_text(sum(tokens_used(r) for r in stopped_short))} of those tokens; "
                            f"{'its result is' if len(stopped_short) == 1 else 'their results are'} left out of the "
                            f"tables.")
    abandoned = sum(tokens_used(a) for a in replaced)
    if abandoned:
        budget_lines.append(f"The {runs}' share includes {tokens_text(abandoned)} tokens of "
                            f"{plural(len(replaced), 'unfinished attempt')} that a later {run} replaced.")
    if stopped:
        budget_lines.append(f"It stopped because {stops.get(stopped, stopped)}.")
    elif finished:
        budget_lines.append(f"It finished after making its full population of "
                            f"{plural(state.get('population') or len(children), 'child', 'children')}.")
    out += ["## Budget and spend", "", " ".join(budget_lines), ""]

    # Screening.
    headers = [adapter.header(a) for a in adapter.axes]
    header = ["Genome", "Parent", "Aimed at"] + (["Front"] if front_known else []) + headers
    rows = []
    for genome in genomes:
        results = finished_on(genome, screening)
        name = genome if len(results) == len(screening) else f"{genome} ({len(results)} of {len(screening)} finished)"
        if genome == base:
            parent, aimed = "none (the base)", "–"
        else:
            m = mutations.get(genome) or {}
            parent = m.get("parent") or "unknown"
            aimed = one_line((m.get("failure") or {}).get("summary") or "unknown", REPORT_SUMMARY_CAP)
        mark = (["yes" if genome in front else "no"] if front_known else [])
        rows.append([name, parent, aimed] + mark + axis_cells(adapter, results))
    out += ["## Screening", "",
            f"Each figure is a genome's mean over its finished screening {runs}. {adapter.better_sentence()} Aimed "
            f"at is the parent's failure that the child was made to fix."
            + (" The Front column marks the Pareto front: the genomes no other one beats on every axis."
               if front_known else " The front isn't known yet: it is picked once every child has been screened."),
            ""]
    out += table(header, rows) + [""]

    # Held-out.
    held = [g for g in [base] + list(state.get("candidates") or []) + sorted(state.get("holdout") or {})
            if g in (state.get("holdout") or {})]
    held = list(dict.fromkeys(held))
    out += ["## Held-out results", ""]
    if held:
        out += [f"The same axes, as means over the {plural(len(holdout), 'held-out case')} "
                f"({', '.join(holdout)}). The winner is picked from these alone.", ""]
        rows = []
        for genome in held:
            results = finished_on(genome, holdout)
            name = genome if len(results) == len(holdout) else f"{genome} ({len(results)} of {len(holdout)} finished)"
            rows.append([name] + axis_cells(adapter, results))
        out += table(["Genome"] + headers, rows) + [""]
    elif finished and candidates:
        out += [f"Nothing was {done} on the held-out cases ({', '.join(holdout)}): the generation stopped before "
                f"the first one. {names_list(candidates)} {'was' if len(candidates) == 1 else 'were'} to be "
                f"compared with the base there.", ""]
    elif finished:
        out += [f"Nothing was {done} on the held-out cases ({', '.join(holdout)}): no child reached the front, so "
                f"none could beat the base there.", ""]
    else:
        out += [f"None yet: the held-out cases ({', '.join(holdout)}) are {done} once screening is over.", ""]

    # Screening against held-out.
    if winner and base:
        out += gap_section(adapter, base, winner, {g: (finished_on(g, screening), finished_on(g, holdout))
                                                   for g in (winner, base)})

    # Lineage.
    out += ["## Lineage", "", "Each child is listed under the genome it was made from.", ""]
    by_parent = {}
    for child in children:
        by_parent.setdefault((mutations.get(child) or {}).get("parent"), []).append(child)

    def lineage(genome, depth, seen):
        notes = (["the base"] if genome == base else []) + (["on the front"] if genome in front else []) \
            + (["the winner"] if genome == winner else [])
        lines = [f"{'  ' * depth}- {genome}" + (f" ({', '.join(notes)})" if notes else "")]
        for child in by_parent.get(genome, []):
            if child not in seen:
                seen.add(child)
                lines += lineage(child, depth + 1, seen)
        return lines

    seen = {base}
    out += lineage(base, 0, seen)
    for child in children:  # a child whose parent isn't in this generation
        if child not in seen:
            seen.add(child)
            out += lineage(child, 0, seen)
    out += [""]

    # Discarded tries and paused reflections.
    out += ["## Discarded tries", ""]
    if discarded:
        out += [f"These reflections made no child, so nothing was {done} for them:", ""]
        for d in discarded:
            aimed = report_failure_text(home, gen_id, adapter, d.get("failure"), d.get("parent"))
            out.append(f"- From {d.get('parent')}, aimed at {aimed}: "
                       f"{' '.join(str(d.get('reason') or d.get('kind') or 'no reason recorded').split())}")
    else:
        out.append("None: every reflection made a child.")
    if limited:
        out += ["", "Reflections stopped by the usage limit, tried again when the generation continues:", ""]
        for p in limited:
            aimed = report_failure_text(home, gen_id, adapter, p.get("failure"), p.get("parent"))
            out.append(f"- From {p.get('parent')}, aimed at {aimed}.")
    out += [""]

    # The winner.
    if winner:
        diagnosis = " ".join(str((read_json(os.path.join(home, "genomes", winner, genome_lib.MUTATION_FILE))
                                  or {}).get("diagnosis") or "").split())
        out += [f"## The winner: {winner}", "",
                f"The reflection's diagnosis: {diagnosis}" if diagnosis else "The reflection recorded no diagnosis.",
                ""]
        out += winner_diff(home, base, winner, target) + [""]
        out += [f"The winner reaches {adapter.target_word} only through a pull request that a person reviews and "
                f"merges ({adapter.promote_words}); nothing is merged automatically.", ""]
    elif finished:
        out += ["## No winner", "", why or "No reason was recorded.", ""]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- processes and git, for promote

STOP_GRACE = 3  # seconds a stopped command's process group gets between TERM and KILL
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)  # what the evolver treats as Ctrl-C
# Settings for every git call on a repository, so a read runs nothing from the repository's own git setup and never
# writes: no fsmonitor program, no hooks, no external diff program, no signature checks (gpg), no automatic
# maintenance; a submodule's change shows as one line, never as a diff run inside the submodule (whose own config
# could name a diff program); a partial clone never fetches a missing object (protocol.allow=never, with
# GIT_NO_LAZY_FETCH=1 in the environment), so reading one fails instead; the default log format is a built-in one,
# never an alias that could ask for a signature; and diffs keep their a/ and b/ prefixes.
SAFE_GIT_CONFIG = ("core.fsmonitor=false", f"core.hooksPath={os.devnull}", "diff.external=",
                   "log.showSignature=false", "core.untrackedCache=false", "core.splitIndex=false", "gc.auto=0",
                   "maintenance.auto=false", "diff.submodule=short", "protocol.allow=never",
                   "format.pretty=medium", "diff.noprefix=false", "diff.mnemonicPrefix=false")
# The options every diff-taking git call gets: no diff driver's command, no textconv program from the repository's
# .gitattributes, no submodule's own diff, and the same a/ b/ prefixes everywhere.
DIFF_SAFETY = ("--no-ext-diff", "--no-textconv", "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/")
COMMIT_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # a full SHA-1 or SHA-256 commit name
# Variables that would make git work on another repository than the one it is run in.
GIT_LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                          "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
# Variables that would add config to every git call.
GIT_CONFIG_VARIABLES = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: something in the group still exists
        return True
    return True


@contextlib.contextmanager
def signals_held():
    """Ignore Ctrl-C, SIGTERM and SIGHUP for the block (a few seconds of cleanup that must not be cut short)."""
    previous = {}
    try:
        for sig in STOP_SIGNALS:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
    except ValueError:  # not the main thread: signals can't be changed there, and they don't arrive there
        pass
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_group(proc):
    """Stop a command started in its own session, with everything it started: TERM to its process group, then
    KILL to whatever is left after STOP_GRACE seconds. Reaps the command itself. A further Ctrl-C, SIGTERM or
    SIGHUP meanwhile is held back until the group is stopped (a few seconds at most), so it can't leave the
    group running."""
    with signals_held():
        _stop_group(proc)


def _stop_group(proc):
    for sig, grace in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, STOP_GRACE)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        end = time.monotonic() + grace
        while True:
            proc.poll()
            if proc.returncode is not None and not group_alive(proc.pid):
                return
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
    try:
        proc.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        pass


def gate_shell(command):
    """A check's command line run as a shell would: bash -lc, or sh -c without bash."""
    bash = shutil.which("bash")
    return [bash, "-lc", command] if bash else ["/bin/sh", "-c", command]


# ---------------------------------------------------------------- promote: the winner as a pull request

PROMOTE_REMOTE = "origin"  # where promote pushes the branch, unless told otherwise
PR_COMMAND = ("gh", "pr", "create")  # promote adds --base, --head, --title and --body-file
PROMOTE_CHECK_TIMEOUT = 3600  # seconds one check may run
PR_TIMEOUT = 300  # seconds the pull-request command may take
CHECK_TAIL_LINES = 40  # the lines of a failing check's output printed
PROMOTE_PREFIX = "evolve-promote-"  # the temporary folder holding promote's worktree
REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# SAFE_GIT_CONFIG adapted for promote's writes in the target repository: still no hooks, fsmonitor, external diff or
# automatic maintenance, and no signing program (commits, tags and pushes are never signed, so no gpg runs); a push
# sends only the refspec it names (no tags followed, no submodules pushed), and a new branch tracks nothing.
# protocol.allow=never (no fetch, and no lazy fetch in a partial clone) is kept for every call but the push.
PROMOTE_GIT_CONFIG = tuple(s for s in SAFE_GIT_CONFIG if s != "protocol.allow=never") + (
    "commit.gpgSign=false", "tag.gpgSign=false", "push.gpgSign=false", "push.followTags=false",
    "push.recurseSubmodules=no", "submodule.recurse=false", "branch.autoSetupMerge=false")


class CheckFailed(EvolveError):
    """A promote check failed; its output tail has been printed."""


def promote_env(network=False, hidden=()):
    """The environment of promote's git calls, checks and pull-request command: the user's own (the commit identity,
    credentials and ssh settings stay), without the variables that aim git at another repository or add config, nor
    the hidden ones (the adapter's hidden_variables), and with git never prompting on the terminal."""
    drop = set(GIT_LOCATION_VARIABLES) | set(GIT_CONFIG_VARIABLES) | set(hidden or ())
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not network:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def promote_git(repo, *args, network=False, hidden=(), raw=False):
    """Run one git command for promote in repo (found through PATH) and return the CompletedProcess. Only the push
    passes network=True; every other call may not fetch anything. raw=True keeps the output as bytes (a blob's
    exact content)."""
    config = PROMOTE_GIT_CONFIG + (() if network else ("protocol.allow=never",))
    cmd = ["git", "--no-optional-locks"]
    for setting in config:
        cmd += ["-c", setting]
    cmd += ["-C", repo, *[str(a) for a in args]]
    env = promote_env(network, hidden)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        if raw:
            return subprocess.run(cmd, capture_output=True, env=env, stdin=subprocess.DEVNULL)
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              stdin=subprocess.DEVNULL)
    except OSError as e:
        raise EvolveError(f"Can't run git: {e.strerror or e}.")


def git_error(r):
    """git's own error line of a failed call (not a warning before it)."""
    err = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    main = [ln for ln in err if ln.startswith(("fatal:", "error:"))] or err
    return main[0] if main else f"exit {r.returncode}"


def promote_git_out(repo, *args, what, hidden=()):
    """git's output of a call that must succeed; else EvolveError saying what failed."""
    r = promote_git(repo, *args, hidden=hidden)
    if r.returncode != 0:
        raise EvolveError(f"{what} failed in {repo}: {git_error(r)}")
    return r.stdout


def literal(paths):
    """Pathspecs that match exactly these paths, never as patterns."""
    return [f":(literal){p}" for p in paths]


def read_generation(home, gen_id):
    """runs/<gen_id>/generation.json, checked to be a generation's record."""
    path = os.path.join(home, "runs", gen_id, GENERATION_FILE) if safe_name(gen_id) else None
    if path is None or not os.path.isfile(path):
        raise EvolveError(f"There is no generation {gen_id}: "
                          f"{path + ' does not exist' if path else 'that is not a generation id'}.")
    state = read_json(path)
    if state is None or not isinstance(state.get("children"), list) or not isinstance(state.get("id"), str):
        raise EvolveError(f"{path} isn't a generation's record.")
    return state


def red_team_children(home):
    """{genome: the red-team generation that made it} over every runs/<g>/generation.json marked red-team: its
    children and the child it was making when it stopped."""
    found = {}
    runs = os.path.join(home, "runs")
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        state = read_json(os.path.join(runs, name, GENERATION_FILE)) if safe_name(name) else None
        if not isinstance(state, dict) or state.get("red_team") is not True:
            continue
        making = state.get("making") if isinstance(state.get("making"), dict) else {}
        for child in list(state.get("children") or []) + [making.get("child")]:
            if isinstance(child, str):
                found.setdefault(child, name)
    return found


def red_team_refusal(home, state, genome, adapter):
    """Why genome may never be promoted, or None: the generation state is a red-team one, or genome or one of its
    ancestors (every parent, walked through genome.json) was made in one, as its mutation.json or a red-team
    generation.json records."""
    if state.get("red_team") is True:
        return (f"The generation {state['id']} is a red-team generation, so {genome} can never become a pull request: "
                f"a red-team generation looks for routes past the guards, and its results are never promoted as a "
                f"genome. Fix each route it found {adapter.fix_words} instead.")
    made_in = red_team_children(home)
    seen, queue = set(), [genome]
    while queue:
        current = queue.pop(0)
        if current in seen or not isinstance(current, str) or not safe_name(current):
            continue
        seen.add(current)
        record = read_json(os.path.join(home, "genomes", current, genome_lib.MUTATION_FILE)) or {}
        if record.get("red_team") is True or current in made_in:
            where = (f"the red-team generation {made_in[current]}" if current in made_in else
                     "a red-team generation")
            what = (f"{genome} was made in {where}" if current == genome else
                    f"{genome} descends from {current}, which was made in {where}")
            return (f"{what}, so it can never become a pull request: red-team results are never promoted as a genome, "
                    f"and neither is anything bred from them.")
        try:
            parents = genome_lib.read_genome(home, current).get("parents") or []
        except genome_lib.GenomeError:
            continue  # a missing ancestor: the integrity check refuses what can't be read
        queue.extend(p for p in parents if isinstance(p, str))
    return None


def generation_unfinished(home, state, adapter):
    """None when the generation finished; else a plain clause saying why it hasn't: it was interrupted or paused, it
    recorded no outcome (still running, or stopped before it could), or its held-out check isn't complete (the base
    or a candidate lacks a finished run on a held-out case)."""
    stopped = state.get("stopped")
    if stopped == STOP_INTERRUPTED:
        return "it was interrupted (Ctrl-C or a stop signal) before the end"
    if stopped == STOP_PAUSED:
        return f"it is paused at {adapter.limit_words}"
    if not (state.get("winner") or state.get("why")):
        return "it is still running, or it was stopped before it could record its outcome"
    base = state.get("base")
    candidates = [g for g in state.get("candidates") or [] if isinstance(g, str)]
    if state.get("winner") and state["winner"] not in candidates:
        candidates.append(state["winner"])
    if not candidates:
        return None
    _, holdout = report_cases(home, state, adapter)
    missing = [g for g in [base] + candidates
               if not holdout or len([s for s in holdout
                                      if finished_record(case_folder(home, state["id"], s, g)) is not None])
               < len(holdout)]
    if missing:
        return (f"its held-out check isn't complete: {names_list(missing)} "
                f"{'was' if len(missing) == 1 else 'were'} not {adapter.run_done} on every held-out case")
    return None


def profile_checks(profile):
    """The profile's checks: the commands promote runs before pushing."""
    checks = profile.get("checks") or []
    if not checks:
        raise EvolveError(f"the profile {profile['name']} names no check, and a winner is promoted only after its "
                          f"checks pass; add the commands to run (its tests, its validation) first.")
    if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks):
        raise EvolveError(f"The checks of the profile {profile['name']} must be a list of commands (strings).")
    return list(checks)


def command_argv(value, what="The pull-request command"):
    """A command as argv: a list of program and arguments, or a string that is one program or, when it holds spaces
    and names no file, a command line split as a shell would."""
    if isinstance(value, str) and value.strip():
        if os.path.exists(value) or not any(c.isspace() for c in value.strip()):
            return [value]
        try:
            argv = shlex.split(value)
        except ValueError as e:
            raise EvolveError(f"{what} can't be read as a command: {e}.")
        if argv:
            return argv
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value):
        return list(value)
    raise EvolveError(f"{what} must be a command (a string) or a list of program and arguments.")


def tail_lines(path, n=CHECK_TAIL_LINES, cap=1 << 20):
    """The last n lines of a file (read from its last cap bytes)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - cap))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]


def run_check(tree, command, log_path, hidden=()):
    """Run one check in the temporary worktree (bash -lc), its output in log_path; returns its exit code, or None
    when it ran past PROMOTE_CHECK_TIMEOUT. Its whole process group is stopped when it times out or promote is
    interrupted."""
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(gate_shell(command), cwd=tree, env=promote_env(hidden=hidden),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as e:
            raise CheckFailed(f"The check `{command}` couldn't start: {e.strerror or e}; nothing was pushed and the "
                              f"branch was deleted.")
        try:
            code = proc.wait(timeout=PROMOTE_CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            stop_group(proc)
            return None
        except BaseException:
            stop_group(proc)
            raise
        if group_alive(proc.pid):
            stop_group(proc)
        return code


def write_into_tree(tree, rel, data):
    """Write data at rel inside the worktree, never through a symlink (one in the branch's files is refused)."""
    current = tree
    for part in rel.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current) or (os.path.exists(current) and not os.path.isdir(current)):
            raise EvolveError(f"{rel} can't be written on the branch: {os.path.relpath(current, tree)} there isn't a "
                              f"plain folder.")
    dest = os.path.join(tree, *rel.split("/"))
    if os.path.islink(dest) or (os.path.exists(dest) and not os.path.isfile(dest)):
        raise EvolveError(f"{rel} can't be written on the branch: it is a symlink or not a plain file there.")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)


def remove_worktree(repo, tree, parent, hidden=()):
    """Remove the temporary worktree (and its folder); a failure only prints how to finish it by hand."""
    r = promote_git(repo, "worktree", "remove", "--force", tree, hidden=hidden)
    if r.returncode != 0 and os.path.isdir(tree):
        print(f"The temporary worktree {tree} couldn't be removed ({git_error(r)}); remove it with "
              f"`git -C {shlex.quote(repo)} worktree remove --force {shlex.quote(tree)}`.", file=sys.stderr)
        return
    shutil.rmtree(parent, ignore_errors=True)


def write_text(path, text):
    """Write text through a temporary file and a rename, like write_json."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def report_path_problem(rel):
    """Why rel can't be the report's path inside the target's repository, or None: it must be a non-empty relative
    path of a file, with no `..` or `.git` part, other than the repository's top folder ('.') or a folder (a path
    ending with /)."""
    if not isinstance(rel, str):
        return "isn't a text path"
    if not rel.strip():
        return "is empty"
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or re.match(r"[A-Za-z]:[/\\]", rel):
        return "is absolute"
    parts = re.split(r"[/\\]", rel)
    if any(part == ".." for part in parts):
        return "has a `..` part, so it could point outside the repository"
    if "\0" in rel:
        return "holds a NUL character"
    if all(part in ("", ".") for part in parts):
        return "names the repository's top folder, not a file"
    if rel.endswith(("/", "\\")):
        return "names a folder (it ends with a slash), not a file"
    if parts[-1] == ".":
        return "names a folder (it ends with /.), not a file"
    if any(part.strip().lower() == ".git" for part in parts):
        return "has a `.git` part, so it would write into git's own files"
    return None


def report_path_clash(rel, promoted):
    """Why the report path rel clashes with one of the promoted files (repository paths), or None: rel is that file's
    path (ignoring case and `.` parts, as a case-insensitive file system would), or one names a folder of the
    other."""
    def parts(path):
        return [p.casefold() for p in re.split(r"[/\\]", path) if p not in ("", ".")]
    mine = parts(rel)
    for path in promoted:
        theirs = parts(path)
        if mine == theirs:
            return f"is the path of the promoted file {path}, so the report would overwrite it"
        shorter = min(len(mine), len(theirs))
        if mine[:shorter] == theirs[:shorter]:
            return (f"{'names a folder holding' if len(mine) < len(theirs) else 'lies under'} the promoted file "
                    f"{path}, so the report and the file can't both be written")
    return None


def promote_source(repo, source, git):
    """(the source folder's full path, its path relative to the repository's top with / and no trailing slash, '' at
    the top) for promote: the folder a genome's file paths are relative to. source None is the repository's top;
    otherwise it must be an existing folder inside the repository (repo, its top folder) and in no other git
    repository nested in it, or it is refused with an EvolveError before anything is created."""
    if source is None:
        return repo, ""
    shown = os.path.abspath(source)
    if not os.path.isdir(shown):
        raise EvolveError(f"The source folder {shown} doesn't exist, so its files have no place in {repo}; nothing "
                          f"is promoted.")
    real, top = os.path.realpath(shown), os.path.realpath(repo)
    rel = os.path.relpath(real, top)
    inside = rel == "." or not (rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel))
    if not inside:
        raise EvolveError(f"The source folder {shown} is outside the repository {repo}, so its files have no place "
                          f"there; nothing is promoted. Promote into the repository that holds the source folder.")
    rel = "" if rel == "." else rel.replace(os.sep, "/")
    if any(part.lower() == ".git" for part in rel.split("/")):
        raise EvolveError(f"The source folder {shown} lies in git's own files of {repo}, not in its working tree; "
                          f"nothing is promoted.")
    own = git(real, "rev-parse", "--show-toplevel")
    own_top = own.stdout.strip() if own.returncode == 0 else ""
    if own_top and os.path.realpath(own_top) != top:
        raise EvolveError(f"The source folder {shown} isn't part of the repository {repo}: it belongs to another git "
                          f"repository nested in it ({own_top}); nothing is promoted. Promote into that repository.")
    return real, rel


def root_seed(home, genome, stored, profile):
    """(id, stored record) of genome's root seed: the ancestor with no parents, reached by following each genome's
    first parent (the one whose base and files a child starts from), loaded with its integrity checked. A missing or
    broken ancestor, or parents that loop, is refused with an EvolveError."""
    current, record, seen = genome, stored, {genome}
    while True:
        parents = record.get("parents") or []
        if not isinstance(parents, list):
            raise EvolveError(f"The genome {current}'s parents aren't a list, so the seed {genome} descends from "
                              f"can't be found; nothing is promoted.")
        if not parents:
            return current, record
        parent = parents[0]
        if not isinstance(parent, str) or not safe_name(parent) or parent in seen:
            raise EvolveError(f"The genome {current} names a parent {parent!r} that isn't a stored genome or loops "
                              f"back, so the seed {genome} descends from can't be found; nothing is promoted.")
        seen.add(parent)
        try:
            record = genome_lib.load_genome(home, parent, profile)
        except genome_lib.GenomeError as e:
            raise EvolveError(f"{genome}'s ancestor {parent} fails its integrity check, so the folder {genome} was "
                              f"seeded from can't be told; nothing is promoted: {e}")
        current = parent


def seeded_path_refusal(genome, stored, under, source_dir, repo):
    """Refuse with an EvolveError when genome's base records the path of the folder it was seeded from (relative to
    its repository's top, '.' for the top) and promote maps its files under another one. A genome seeded before seed
    recorded the path has none, and relies on seeded_folder_refusal alone."""
    recorded_base = stored.get("base") if isinstance(stored.get("base"), dict) else {}
    if "path" not in recorded_base:
        return
    recorded = recorded_base["path"]
    if not isinstance(recorded, str) or not recorded.strip():
        raise EvolveError(f"{genome}'s base records the folder it was seeded from as {recorded!r}, which isn't a "
                          f"path, so whether --source is that folder can't be told; nothing is promoted.")
    wanted = recorded.strip().strip("/")
    wanted = "" if wanted in ("", ".") else wanted
    if wanted != under:
        raise EvolveError(f"{genome} was seeded from {wanted or '.'} in its repository, but the source folder "
                          f"{source_dir} is {under or '.'} in {repo}; the paths differ, so promoting there would put "
                          f"its files where they don't belong. Nothing is promoted. Give --source the folder "
                          f"{wanted or '.'} of that repository.")


def seeded_folder_refusal(repo, root, root_stored, commit, under, source_dir, git):
    """Refuse with an EvolveError unless the root seed root (whose files seed took from the source folder at its
    recorded commit) holds exactly the files at <that commit>:<under>/<file> in repo, so the genome was seeded from
    the folder promote maps its files under. commit is the promoted genome's base commit, used when the root seed
    records none. The root seed, not the generation's base genome: a base that is itself an evolved genome differs
    from the source in the files it changed."""
    recorded = (root_stored.get("base") or {}).get("commit") if isinstance(root_stored.get("base"), dict) else None
    seed_commit = recorded if isinstance(recorded, str) and COMMIT_HASH.fullmatch(recorded) else commit
    where = f"{source_dir} ({under}/ in {repo})" if under else f"the top folder of {repo}"
    differing = []
    for entry in root_stored["files"]:
        rel = entry["path"]
        path = f"{under}/{rel}" if under else rel
        blob = git(repo, "rev-parse", "--verify", "-q", f"{seed_commit}:{path}")
        oid = blob.stdout.strip() if blob.returncode == 0 else ""
        kind = git(repo, "cat-file", "-t", oid).stdout.strip() if oid else ""
        shown = git(repo, "cat-file", "blob", oid, raw=True) if kind == "blob" else None
        if shown is None or shown.returncode != 0:
            differing.append(f"{path} (missing)")
        elif genome_lib.sha256_bytes(shown.stdout) != entry["sha256"]:
            differing.append(f"{path} (different text)")
    if differing:
        verb = "doesn't" if len(differing) == 1 else "don't"
        raise EvolveError(f"The genome wasn't seeded from the source folder {where}: at the seed commit "
                          f"{seed_commit[:12]}, {names_list(differing)} {verb} match the files of {root}, the seed "
                          f"genome it descends from, so promoting there would put its files where they don't belong; "
                          f"nothing is promoted. Give --source the folder the genome was seeded from.")


def setting(value, config, profile):
    """One of promote's settings: value itself, or value(config, profile) when it is a function."""
    return value(config, profile) if callable(value) else value


def promote(home, config, state, genome, adapter, profile, repo, onto=None, checks=None, remote=PROMOTE_REMOTE,
            pr_command=PR_COMMAND, open_pr=True, prefix=PROMOTE_PREFIX, source=None):
    """Put a generation's winner on its own branch of the target repository, run the checks there and open its pull
    request; returns 0. It works in a temporary worktree, pushes only its own branch and never merges.

    It is refused with an EvolveError before anything is created when the generation is a red-team one or genome
    descends from a red-team child, when genome isn't the finished generation's winner, fails its integrity check, or
    its files have uncommitted changes in the repository or changed on onto since its base commit, or when source isn't
    the folder genome was seeded from (another path than its base's recorded one, or files that differ from its root
    seed's at that seed's commit).

    - state: the generation's generation.json; adapter: the adapter it ran with (its words, report_path and
      hidden_variables); repo: a folder in the target's git repository; onto: the branch the pull request targets
      (None: the repository's current branch).
    - source: the target's source folder, the folder the genome's file paths are relative to (None: the repository's
      top). It must lie inside the repository (refused before anything is created otherwise); each genome file goes
      to <source relative to the repository's top>/<file> on the branch, and the report to the adapter's
      report_path, relative to the repository's top. The report path is refused when it is empty, absolute, the top
      folder, a folder, has a `..` or `.git` part, or clashes with a promoted file's path.
    - profile: the target's profile, or a function of the source folder (the repository's top folder when source is
      None) that loads it.
    - checks: the commands run in the worktree before anything is pushed (None: the profile's checks); remote: the
      git remote the branch is pushed to; pr_command: the pull-request command (a list, or a string as command_argv
      reads it), to which --base, --head, --title and --body-file are added. Each of these three may also be a
      function of (config, profile), called once genome passed its integrity check.
    - open_pr: False stops after the checks, keeps the branch, pushes nothing and prints the commands to run by hand.
    - prefix: the name prefix of the temporary folder holding the worktree."""
    gen_id = state.get("id")
    if not safe_name(gen_id):
        raise EvolveError(f"The generation id {gen_id!r} isn't a plain folder name (letters, digits, _ . and -, "
                          f"starting with a letter or digit), so nothing is promoted from it.")
    report_rel = adapter.report_path(gen_id)
    problem = report_path_problem(report_rel)
    if problem:
        raise EvolveError(f"The adapter's report path {report_rel!r} {problem}, so the report can't be written on the "
                          f"branch and nothing is promoted.")
    hidden = tuple(adapter.hidden_variables or ())
    if not safe_name(genome):
        raise EvolveError(f"{genome!r} isn't a stored genome's id, so it can't be promoted.")
    refusal = red_team_refusal(home, state, genome, adapter)
    if refusal:
        raise EvolveError(refusal)

    # The generation and its winner.
    unfinished = generation_unfinished(home, state, adapter)
    if unfinished:
        how = state.get("continue") or adapter.continue_hint(gen_id)
        raise EvolveError(f"The generation {gen_id} hasn't finished, so it has no winner to promote yet: {unfinished}. "
                          f"To finish it, continue it with{':' if state.get('continue') else ''} {how}")
    winner, base = state.get("winner"), state.get("base")
    if not winner:
        why = " ".join(str(state.get("why") or "").split())
        raise EvolveError(f"The generation {gen_id} has no winner, so {genome} can't be promoted"
                          f"{': ' + why if why else '.'}")
    if genome != winner:
        raise EvolveError(f"{genome} isn't the winner of the generation {gen_id} (its winner is {winner}); only a "
                          f"generation's winner is promoted.")

    def git(*args, **kwargs):
        return promote_git(*args, hidden=hidden, **kwargs)

    def git_out(*args, what):
        return promote_git_out(*args, what=what, hidden=hidden)

    # The genome's integrity, against the target's profile.
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        raise EvolveError(f"The repository {repo} doesn't exist.")
    top = git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        raise EvolveError(f"{repo} isn't a git repository: {git_error(top)}")
    repo = top.stdout.strip()
    source_dir, under = promote_source(repo, source, git)
    profile = profile(source_dir) if callable(profile) else profile
    try:
        genome_lib.check_individual(home, genome, profile)
        stored = genome_lib.load_genome(home, genome, profile)
    except genome_lib.GenomeError as e:
        raise EvolveError(f"The genome {genome} fails its integrity check, so it isn't promoted: {e}")
    try:
        base_stored = genome_lib.load_genome(home, base, profile)
    except genome_lib.GenomeError as e:
        raise EvolveError(f"The base genome {base} fails its integrity check, so the files {genome} changes can't be "
                          f"told: {e}")
    seeded_path_refusal(genome, stored, under, source_dir, repo)
    mine = {e["path"]: e["sha256"] for e in stored["files"]}
    theirs = {e["path"]: e["sha256"] for e in base_stored["files"]}
    removed = sorted(set(theirs) - set(mine))
    if removed:
        raise EvolveError(f"{genome} lacks {names_list(removed)}, which the base {base} holds; promote writes changed "
                          f"files and never removes one.")
    changed_files = sorted(p for p, digest in mine.items() if theirs.get(p) != digest)
    if not changed_files:
        raise EvolveError(f"{genome} holds the same files as the base {base}, so there is nothing to promote.")
    # Each changed genome file's path in the repository: under the source folder's path from the repository's top.
    mapped = {rel: f"{under}/{rel}" if under else rel for rel in changed_files}
    changed = sorted(mapped.values())
    clash = report_path_clash(report_rel, changed)
    if clash:
        raise EvolveError(f"The adapter's report path {report_rel!r} {clash}; nothing is promoted.")

    # The settings, before anything is created.
    checks = setting(checks, config, profile)
    checks = profile_checks(profile) if checks is None else list(checks)
    remote = setting(remote, config, profile)
    if not isinstance(remote, str) or not REMOTE_NAME.fullmatch(remote):
        raise EvolveError(f"{remote!r} isn't a git remote's name, such as origin, so nothing can be pushed to it.")
    pr_argv = command_argv(setting(pr_command, config, profile))

    # onto, the base commit and the user's checkout.
    if onto is None:
        head = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
        onto = head.stdout.strip() if head.returncode == 0 else ""
        if not onto:
            raise EvolveError(f"{repo} has no current branch (its HEAD is detached); name the branch the pull request "
                              f"targets with --onto.")
    if onto.startswith("-") or git(repo, "check-ref-format", f"refs/heads/{onto}").returncode != 0:
        raise EvolveError(f"{onto!r} isn't a branch name.")
    found = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{onto}^{{commit}}")
    if found.returncode != 0:
        raise EvolveError(f"{repo} has no branch {onto}; name an existing branch with --onto.")
    onto_commit = found.stdout.strip()
    commit = (stored.get("base") or {}).get("commit") if isinstance(stored.get("base"), dict) else None
    if not isinstance(commit, str) or not COMMIT_HASH.fullmatch(commit):
        raise EvolveError(f"{genome} records no base commit, so whether its files changed on {onto} since can't be "
                          f"told; it isn't promoted.")
    if git(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        raise EvolveError(f"The base commit of {genome}, {commit[:12]}, isn't in {repo}, so whether its files changed "
                          f"on {onto} since can't be told; fetch it first.")
    root, root_stored = root_seed(home, genome, stored, profile)
    seeded_folder_refusal(repo, root, root_stored, commit, under, source_dir, git)
    status = git_out(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *literal(changed),
                     what="git status")
    dirty = sorted({entry[3:] for entry in status.split("\0") if len(entry) > 3 and entry[2] == " "} & set(changed))
    if status.strip("\0") and not dirty:
        dirty = changed
    if dirty:
        raise EvolveError(f"{repo} has uncommitted changes in {names_list(dirty)}, which {genome} changes; commit or "
                          f"stash them first, so the pull request doesn't hide them.")
    moved = git_out(repo, "diff", *DIFF_SAFETY, "--name-only", "--no-renames", commit, onto_commit, "--",
                    *literal(changed), what="git diff").split()
    if moved:
        raise EvolveError(f"{names_list(sorted(moved))} {'was' if len(moved) == 1 else 'were'} also changed on {onto} "
                          f"since {genome}'s base commit {commit[:12]}; promote never merges prompt text, so it isn't "
                          f"promoted. Seed a new generation from {onto}, or bring the change over by hand.")
    branch = f"genome/{gen_id}-{genome}"
    if git(repo, "check-ref-format", f"refs/heads/{branch}").returncode != 0:
        raise EvolveError(f"{branch} isn't a valid branch name, so {genome} can't be promoted under it.")
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        raise EvolveError(f"{repo} already has the branch {branch}; delete it (git branch -D {branch}) to promote "
                          f"{genome} again.")
    if open_pr:
        remotes = git_out(repo, "remote", what="git remote").split()
        if remote not in remotes:
            raise EvolveError(f"{repo} has no remote {remote} to push {branch} to; add it, or "
                              f"{adapter.remote_words}.")
        mirror = git(repo, "config", "--bool", "--get", f"remote.{remote}.mirror")
        if mirror.stdout.strip() == "true":
            raise EvolveError(f"The remote {remote} is set up as a mirror, so a push to it could change other "
                              f"branches; promote pushes only its own branch, so it doesn't push there.")

    # The report, written before it is committed (it is also the pull request's body).
    report = report_text(home, state, adapter)
    report_path = os.path.join(home, "reports", f"{gen_id}.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    write_text(report_path, report)
    parent = (read_json(os.path.join(home, "genomes", genome, genome_lib.MUTATION_FILE)) or {}).get(
        "parent") or names_list(stored.get("parents") or []) or "none recorded"
    title = f"Genome {genome}, the winner of generation {gen_id}: {names_list(changed)}"
    message = (f"{title}\n\nGeneration {gen_id} picked {genome} as its winner; its parent is {parent} and the "
               f"generation's base genome is {base}. It changes {names_list(changed)}. The generation report is "
               f"{report_rel}.\n")
    pr_full = pr_argv + ["--base", onto, "--head", branch, "--title", title, "--body-file", report_path]

    print(f"Promoting {genome}, the winner of generation {gen_id}, onto {onto} in {repo}.", flush=True)
    parent_dir = tempfile.mkdtemp(prefix=prefix)
    tree = os.path.join(parent_dir, "worktree")
    created = keep = False
    try:
        added = git(repo, "worktree", "add", "--quiet", "--no-track", "-b", branch, tree, onto_commit)
        if added.returncode != 0:
            raise EvolveError(f"git worktree add failed in {repo}: {git_error(added)}")
        created = True
        for rel in changed_files:
            write_into_tree(tree, mapped[rel], genome_lib.read_bytes(os.path.join(home, "genomes", genome, "files",
                                                                                  *rel.split("/"))))
        write_into_tree(tree, report_rel, report.encode("utf-8"))
        git_out(tree, "add", "--", *literal(changed + [report_rel]), what="git add")
        git_out(tree, "commit", "--quiet", "--no-verify", "-m", message, what="git commit")
        print(f"Made the branch {branch}: {names_list(changed)} and {report_rel}, one commit on {onto}.", flush=True)

        log_path = os.path.join(parent_dir, "check.log")
        for n, command in enumerate(checks, 1):
            print(f"Running check {n} of {len(checks)} in a temporary worktree: {command}", flush=True)
            code = run_check(tree, command, log_path, hidden=hidden)
            if code != 0:
                print("The output's last lines:")
                for line in tail_lines(log_path):
                    print(f"  {line}")
                ended = "ran past its time limit" if code is None else f"failed (exit {code})"
                raise CheckFailed(f"The check `{command}` {ended}, so {genome} isn't promoted: nothing was pushed and "
                                  f"the branch {branch} was deleted.")
        print("The check passed." if len(checks) == 1 else f"All {len(checks)} checks passed.", flush=True)
        keep = True

        push_line = f"cd {shlex.quote(repo)} && git push {shlex.quote(remote)} {shlex.quote(branch)}"
        pr_line = " ".join(shlex.quote(a) for a in pr_full)
        if not open_pr:
            print(f"The branch {branch} is ready in {repo}; nothing was pushed. To push it and open the pull request "
                  f"by hand:")
            print(f"  {push_line}")
            print(f"  {pr_line}")
            return 0

        pushed = git(repo, "push", "--porcelain", "--recurse-submodules=no", remote,
                     f"refs/heads/{branch}:refs/heads/{branch}", network=True)
        if pushed.returncode != 0:
            raise EvolveError(f"git push of {branch} to {remote} failed: {git_error(pushed)}. The branch stays in "
                              f"{repo}; push it and open the pull request by hand: {push_line} ; then {pr_line}")
        print(f"Pushed {branch} to {remote}.", flush=True)
        try:
            opened = subprocess.run(pr_full, cwd=tree, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", env=promote_env(hidden=hidden), stdin=subprocess.DEVNULL,
                                    timeout=PR_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            why = getattr(e, "strerror", None) or ("it timed out" if isinstance(e, subprocess.TimeoutExpired) else e)
            raise EvolveError(f"The pull-request command couldn't run ({why}). {branch} is pushed; open the pull "
                              f"request by hand: {pr_line}")
        text = (opened.stdout or "") + (opened.stderr or "")
        if opened.returncode != 0:
            for line in text.splitlines()[-CHECK_TAIL_LINES:]:
                print(f"  {line}")
            raise EvolveError(f"The pull-request command failed (exit {opened.returncode}). {branch} is pushed; open "
                              f"the pull request by hand: {pr_line}")
        urls = re.findall(r"https?://\S+", text)
        if urls:
            print(f"Opened the pull request: {urls[-1]}")
        else:
            print(f"The pull-request command printed no address; its output: {one_line(text, 400) or 'none'}")
        print("Nothing was merged: read the pull request, then merge or close it.")
        return 0
    finally:
        with signals_held():
            if created:
                remove_worktree(repo, tree, parent_dir, hidden=hidden)
                if not keep:
                    gone = git(repo, "branch", "-D", branch)
                    if gone.returncode != 0:
                        print(f"The branch {branch} couldn't be deleted ({git_error(gone)}); delete it with "
                              f"`git -C {shlex.quote(repo)} branch -D {branch}`.", file=sys.stderr)
            else:
                shutil.rmtree(parent_dir, ignore_errors=True)
