#!/usr/bin/env python3
"""Suite files: the cases a target's files are judged on, written by the target's maintainer as JSON.

A suite file names the suite, where an individual's files go in a run's workspace, how a run is started (a runner)
and its cases. Each case has a prompt, optional fixture files copied into the workspace, optional frozen globs the
agent may not change, and checks of three kinds, in order of trust: a command with an expected exit code, a file
assertion (exists, contains, matches a regular expression) and a judge (a rubric read by a fixed model).

Paths in a suite file are relative to the suite file's folder (suite-relative), except the paths inside a run's
workspace (workspace-relative): the install folder, the fixtures' destinations, frozen globs, the file a file check
reads and the files a judge reads.

load_suite reads and checks a suite file and returns it with every default filled in, plus its split: which cases
train (the first screen_cases of them screen) and which are held out. The split is stable: a case's own split
wins, and the others are ordered by the sha256 of "<suite name>:<case id>", the first round(n * holdout_pct / 100)
of them held out. Every problem raises SuiteError with one plain sentence naming the suite file and, when known,
the case and the check.

run_case runs one case with one individual (a folder of the target's files) in a fresh throwaway workspace, through
the suite's runner (runners.py), and writes the run folder's result.json.

This module is standard library only, plus genome.py (paths and globs), evolve_core.py (the error class) and
runners.py (how a case's agent is started). It names no target: everything specific to one lives in the suite file
and the target's profile."""
import hashlib
import json
import math
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import genome as genome_lib  # noqa: E402
import evolve_core  # noqa: E402
import runners  # noqa: E402

DEFAULT_RUNNER = runners.DEFAULT_RUNNER  # the default runner's name (runners.py runs it)
TOKEN_READERS = ("none", "claude-json")  # how a custom runner's output tells its tokens: not at all, or as JSON
PLACEHOLDERS = ("workspace", "individual", "prompt_file", "prompt", "model")  # what a runner template may use
PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")  # never left in a workspace
CHECKS_PLACEHOLDER = "{checks}"  # in a command check: the folder the suite's check scripts are copied to

TOP_KEYS = ("name", "describe", "install", "runner", "model", "timeout_minutes", "holdout_pct", "screen_cases",
            "checks_dir", "sandbox", "cases")
REQUIRED_TOP = ("name", "install", "cases")
DEFAULTS = {"describe": None, "runner": DEFAULT_RUNNER, "model": "sonnet", "timeout_minutes": 15, "holdout_pct": 40,
            "screen_cases": None, "checks_dir": "checks", "sandbox": True}
CASE_KEYS = ("id", "split", "prompt", "prompt_file", "files", "frozen", "checks")
SPLITS = ("train", "holdout")
RUNNER_KEYS = ("command", "tokens")

# The keys of each check kind; a check's keys (besides id) say its kind, and keys of two kinds are refused.
KIND_KEYS = {
    "command": ("run", "exit", "timeout"),
    "file": ("file", "exists", "contains", "not_contains", "matches", "not_matches"),
    "judge": ("judge", "rubric_file", "model", "files"),
}
FILE_ASSERTIONS = ("exists", "contains", "not_contains", "matches", "not_matches")
COMMAND_DEFAULTS = {"exit": 0, "timeout": 60}


class SuiteError(evolve_core.EvolveError):
    """A problem in a suite file, said in one plain sentence naming the file, the case and the check."""


# ---------------------------------------------------------------- small helpers

def is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and value > 0)


def is_text(value):
    return isinstance(value, str) and bool(value.strip())


def is_model(value):
    """A model name: non-empty, one word (it goes into a command line as one argument)."""
    return isinstance(value, str) and bool(re.fullmatch(r"[^\s\x00]+", value))


def plain_name(value):
    return evolve_core.safe_name(value)


def workspace_path(rel):
    """A workspace-relative path: no leading /, no '..' part (nor empty or '.' parts)."""
    return genome_lib.safe_path(rel)


def shown(value):
    return json.dumps(value) if not isinstance(value, str) else value


def stable_key(name, case_id):
    return hashlib.sha256(f"{name}:{case_id}".encode("utf-8")).hexdigest()


class Where:
    """Where a problem is: the suite file, then the case and the check when known."""

    def __init__(self, path, case=None, check=None):
        self.path, self.case, self.check = path, case, check

    def at(self, case=None, check=None):
        return Where(self.path, case if case is not None else self.case, check)

    def __str__(self):
        text = f"The suite file {self.path}"
        if self.case is not None:
            text += f", case {self.case}"
        if self.check is not None:
            text += f", check {self.check}"
        return text

    def refuse(self, problem):
        raise SuiteError(" ".join(f"{self}: {problem}".split()))


# ---------------------------------------------------------------- suite-relative paths

def suite_relative(where, root, rel, what, kind="file"):
    """The absolute path of a suite-relative path that stays inside the suite's folder and exists as `kind` ('file',
    'folder', 'any', or None when it needn't exist). Refuses an absolute path, one that climbs out (also through a
    symlink) and a missing one."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        where.refuse(f"{what} must be a path relative to the suite file's folder (a non-empty string).")
    if os.path.isabs(rel) or rel.startswith(("/", "\\")):
        where.refuse(f"{what} {rel} is an absolute path; give it relative to the suite file's folder.")
    norm = os.path.normpath(rel)
    if norm == ".." or norm.startswith(".." + os.sep):
        where.refuse(f"{what} {rel} climbs out of the suite file's folder; keep it inside that folder.")
    full = os.path.normpath(os.path.join(root, norm))
    top, real = os.path.realpath(root), os.path.realpath(full)
    if real != top and not real.startswith(top + os.sep):
        where.refuse(f"{what} {rel} leads out of the suite file's folder (through a link); keep it inside that "
                     f"folder.")
    if kind == "file" and not os.path.isfile(full):
        where.refuse(f"{what} {rel} doesn't exist or isn't a file.")
    if kind == "folder" and not os.path.isdir(full):
        where.refuse(f"{what} {rel} doesn't exist or isn't a folder.")
    if kind == "any" and not os.path.exists(full):
        where.refuse(f"{what} {rel} doesn't exist.")
    return full


# ---------------------------------------------------------------- the top level

def read_suite(path):
    where = Where(path)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        where.refuse(f"can't be read ({e.strerror or e}).")
    except UnicodeDecodeError:
        where.refuse("isn't UTF-8 text.")
    try:
        data = json.loads(text)
    except ValueError as e:
        where.refuse(f"isn't valid JSON ({e}).")
    if not isinstance(data, dict):
        where.refuse("must hold a JSON object.")
    return data


def check_runner(where, runner):
    if runner == DEFAULT_RUNNER:
        return runner
    if isinstance(runner, str) or not isinstance(runner, dict):
        where.refuse(f"runner {shown(runner)} isn't a runner: give \"{DEFAULT_RUNNER}\" (the default) or an object "
                     f"{{\"command\": [...], \"tokens\": ...}}.")
    unknown = sorted(set(runner) - set(RUNNER_KEYS))
    if unknown:
        where.refuse(f"runner has keys a runner doesn't take ({', '.join(unknown)}); it takes "
                     f"{', '.join(RUNNER_KEYS)}.")
    command = runner.get("command")
    if not (isinstance(command, list) and command and all(isinstance(a, str) for a in command)
            and is_text(command[0])):
        where.refuse("runner command must be a non-empty list of strings: the program, then its arguments.")
    known = ", ".join("{" + p + "}" for p in PLACEHOLDERS)
    for arg in command:
        try:
            fields = list(string.Formatter().parse(arg))
        except ValueError:
            where.refuse(f"the runner command's argument {arg} has a stray {{ or }}; write {{{{ or }}}} for a "
                         f"literal brace. Its placeholders are {known}.")
        for _text, field, spec, conversion in fields:
            if field is None:
                continue
            if field not in PLACEHOLDERS or spec or conversion:
                bad = "{" + field + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
                where.refuse(f"the runner command uses the placeholder {bad}, which isn't one it knows; its "
                             f"placeholders are {known}.")
    if "tokens" not in runner:
        where.refuse(f"runner has no tokens: say how its output tells the tokens it used "
                     f"({' or '.join(TOKEN_READERS)}).")
    if runner["tokens"] not in TOKEN_READERS:
        where.refuse(f"runner tokens {shown(runner['tokens'])} isn't one of {', '.join(TOKEN_READERS)}.")
    return {"command": list(command), "tokens": runner["tokens"]}


def check_top(where, data):
    unknown = sorted(set(data) - set(TOP_KEYS))
    if unknown:
        where.refuse(f"has keys a suite file doesn't take ({', '.join(unknown)}); it takes {', '.join(TOP_KEYS)}.")
    missing = [k for k in REQUIRED_TOP if k not in data]
    if missing:
        where.refuse(f"has no {', '.join(missing)}, which every suite file needs.")
    suite = dict(DEFAULTS)
    suite.update({k: v for k, v in data.items() if k != "cases"})
    if not plain_name(suite["name"]):
        where.refuse(f"name {shown(suite['name'])} isn't a plain name (letters, digits, _ . -, starting with a "
                     f"letter or digit).")
    if suite["describe"] is not None:
        if not is_text(suite["describe"]):
            where.refuse("describe must be one sentence saying what the suite tests.")
        suite["describe"] = " ".join(suite["describe"].split())
    install = suite["install"]
    if not (install == "." or workspace_path(install)):
        where.refuse(f"install {shown(install)} must be a folder relative to the workspace, or . for the "
                     f"workspace itself: no leading /, no '..' part.")
    suite["runner"] = check_runner(where, suite["runner"])
    if not is_model(suite["model"]):
        where.refuse(f"model {shown(suite['model'])} must be a model name (one word).")
    if not is_positive_number(suite["timeout_minutes"]):
        where.refuse(f"timeout_minutes {shown(suite['timeout_minutes'])} must be a number of minutes above 0.")
    pct = suite["holdout_pct"]
    if not (is_whole(pct) and 1 <= pct <= 99):
        where.refuse(f"holdout_pct {shown(pct)} must be a whole number from 1 to 99.")
    screen = suite["screen_cases"]
    if screen is not None and not (is_whole(screen) and screen >= 1):
        where.refuse(f"screen_cases {shown(screen)} must be a whole number of at least 1.")
    if not isinstance(suite["sandbox"], bool):
        where.refuse(f"sandbox {shown(suite['sandbox'])} must be true or false (a JSON boolean, not a string).")
    if not isinstance(data["cases"], list) or not data["cases"]:
        where.refuse("cases must be a non-empty list of cases.")
    return suite


# ---------------------------------------------------------------- cases

def check_case(where, root, suite, raw, place, seen):
    """One case, normalised. `place` is its 1-based place in the file, `seen` the ids met so far."""
    if not isinstance(raw, dict):
        where.at(case=place).refuse("must be an object with an id, a prompt and checks.")
    cid = raw.get("id")
    here = where.at(case=cid if plain_name(cid) else place)
    unknown = sorted(set(raw) - set(CASE_KEYS))
    if unknown:
        here.refuse(f"has keys a case doesn't take ({', '.join(unknown)}); it takes {', '.join(CASE_KEYS)}.")
    if "id" not in raw:
        here.refuse("has no id; give every case a plain name as its id.")
    if not plain_name(cid):
        here.refuse(f"id {shown(cid)} isn't a plain name (letters, digits, _ . -, starting with a letter or digit).")
    if cid in seen:
        here.refuse("repeats an id another case has; every case needs its own id.")
    seen.add(cid)
    split = raw.get("split")
    if split is not None and split not in SPLITS:
        here.refuse(f"split {shown(split)} must be train or holdout (or left out, to let the hash decide).")
    has_prompt, has_file = "prompt" in raw, "prompt_file" in raw
    if has_prompt == has_file:
        here.refuse("needs exactly one of prompt (the text) and prompt_file (a file holding it)" +
                    ("; it has both." if has_prompt else "; it has neither."))
    if has_prompt and not is_text(raw["prompt"]):
        here.refuse("prompt must be a non-empty string.")
    if has_file:
        suite_relative(here, root, raw["prompt_file"], "prompt_file")
    files = raw.get("files", {})
    if not isinstance(files, dict):
        here.refuse("files must be an object of workspace path to the source path in the suite's folder.")
    for dest, src in files.items():
        if not (dest == "." or workspace_path(dest)):
            here.refuse(f"the fixture's workspace path {dest} must stay inside the workspace: no leading /, no '..' "
                        f"part (. for the workspace itself).")
        full = suite_relative(here, root, src, f"the fixture source for {dest}", kind="any")
        if dest == "." and not os.path.isdir(full):
            here.refuse(f"the fixture source {src} for . must be a folder, whose contents go into the workspace.")
    frozen = raw.get("frozen", [])
    if not isinstance(frozen, list) or not all(isinstance(g, str) for g in frozen):
        here.refuse("frozen must be a list of workspace globs.")
    for glob in frozen:
        if not workspace_path(glob):
            here.refuse(f"the frozen glob {glob} must stay inside the workspace: no leading /, no '..' part.")
    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        here.refuse("has no checks; every case needs a non-empty list of checks.")
    ids, normalised = set(), []
    for n, check in enumerate(checks, 1):
        normalised.append(check_check(here, root, suite, check, n, ids))
    return {"id": cid, "split": split, "prompt": raw.get("prompt"), "prompt_file": raw.get("prompt_file"),
            "files": dict(files), "frozen": list(frozen), "checks": normalised}


# ---------------------------------------------------------------- checks

def strings_or_list(value):
    if isinstance(value, str):
        return [value] if value else None
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return list(value)
    return None


def check_check(where, root, suite, raw, place, ids):
    """One check, normalised with its kind and id. `ids` holds the case's check ids so far."""
    if not isinstance(raw, dict):
        where.at(check=place).refuse("must be an object: a command check (run), a file check (file) or a judge "
                                     "(judge or rubric_file).")
    given = raw.get("id")
    here = where.at(check=given if plain_name(given) else place)
    keys = set(raw) - {"id"}
    every = {k for kind_keys in KIND_KEYS.values() for k in kind_keys}
    unknown = sorted(keys - every)
    kinds = [kind for kind, kind_keys in KIND_KEYS.items() if keys & set(kind_keys)]
    if unknown:
        here.refuse(f"has keys a check doesn't take ({', '.join(unknown)}); a check is a command check (run), a "
                    f"file check (file) or a judge (judge or rubric_file).")
    if not kinds:
        here.refuse("is of no known kind; a check is a command check (run), a file check (file) or a judge (judge "
                    "or rubric_file).")
    if len(kinds) > 1:
        here.refuse(f"mixes the keys of a {' and a '.join(kinds)} check; split it into one check of each kind.")
    kind = kinds[0]
    if "id" in raw and not plain_name(given):
        here.refuse(f"id {shown(given)} isn't a plain name (letters, digits, _ . -, starting with a letter or "
                    f"digit).")
    cid = given if "id" in raw else f"{kind}-{place}"
    if cid in ids:
        here.at(check=cid).refuse("repeats a check id of its case; every check in a case needs its own id.")
    ids.add(cid)
    here = where.at(check=cid)
    check = {"kind": kind, "id": cid}
    if kind == "command":
        check.update(command_check(here, root, suite, raw))
    elif kind == "file":
        check.update(file_check(here, raw))
    else:
        check.update(judge_check(here, root, raw))
    return check


def command_check(where, root, suite, raw):
    run = raw.get("run")
    if not is_text(run):
        where.refuse("run must be the command to run (a non-empty string).")
    out = dict(COMMAND_DEFAULTS)
    out.update({k: raw[k] for k in KIND_KEYS["command"] if k in raw})
    if not is_whole(out["exit"]):
        where.refuse(f"exit {shown(out['exit'])} must be a whole number, the exit code that passes.")
    if not is_positive_number(out["timeout"]):
        where.refuse(f"timeout {shown(out['timeout'])} must be a number of seconds above 0.")
    if CHECKS_PLACEHOLDER in run:
        suite_relative(where, root, suite["checks_dir"],
                       f"the command uses {CHECKS_PLACEHOLDER}, but checks_dir", kind="folder")
    return out


def file_check(where, raw):
    rel = raw.get("file")
    if not workspace_path(rel):
        where.refuse(f"file {shown(rel)} must be a path inside the workspace: no leading /, no '..' part.")
    present = [k for k in FILE_ASSERTIONS if k in raw]
    if not present:
        where.refuse(f"has no assertion; give at least one of {', '.join(FILE_ASSERTIONS)}.")
    out = {"file": rel}
    if "exists" in raw:
        if not isinstance(raw["exists"], bool):
            where.refuse(f"exists {shown(raw['exists'])} must be true or false.")
        out["exists"] = raw["exists"]
    for key in ("contains", "not_contains"):
        if key in raw:
            texts = strings_or_list(raw[key])
            if texts is None:
                where.refuse(f"{key} must be a non-empty string or a non-empty list of non-empty strings.")
            out[key] = texts
    for key in ("matches", "not_matches"):
        if key in raw:
            pattern = raw[key]
            if not isinstance(pattern, str):
                where.refuse(f"{key} must be a regular expression (a string).")
            try:
                re.compile(pattern)
            except re.error as e:
                where.refuse(f"{key} {pattern} isn't a regular expression that compiles ({e}).")
            out[key] = pattern
    return out


def judge_check(where, root, raw):
    has_text, has_file = "judge" in raw, "rubric_file" in raw
    if has_text == has_file:
        where.refuse("a judge needs exactly one rubric: judge (the text) or rubric_file (a file holding it)" +
                     ("; it has both." if has_text else "; it has neither."))
    if has_text and not is_text(raw["judge"]):
        where.refuse("judge must be the rubric (a non-empty string).")
    if has_file:
        suite_relative(where, root, raw["rubric_file"], "rubric_file")
    if "model" not in raw:
        where.refuse("a judge needs a model, the fixed model that reads the rubric; it is never defaulted.")
    if not is_model(raw["model"]):
        where.refuse(f"model {shown(raw['model'])} must be a model name (one word).")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        where.refuse("a judge needs files, a non-empty list of the workspace files it reads.")
    for rel in files:
        if not workspace_path(rel):
            where.refuse(f"the judge's file {shown(rel)} must be a path inside the workspace: no leading /, no '..' "
                         f"part.")
    out = {"model": raw["model"], "files": list(files)}
    out.update({"judge": raw["judge"]} if has_text else {"rubric_file": raw["rubric_file"]})
    return out


# ---------------------------------------------------------------- the split

def split_cases(where, suite, cases):
    """{'train', 'holdout', 'screening'}: case ids, each list in the stable order (see the module's docstring)."""
    order = sorted((c["id"] for c in cases), key=lambda cid: stable_key(suite["name"], cid))
    own = {c["id"]: c["split"] for c in cases if c["split"] is not None}
    free = [cid for cid in order if cid not in own]
    count = round(len(free) * suite["holdout_pct"] / 100)
    if free and count == 0 and "holdout" not in own.values():
        count = 1
    held = set(free[:count]) | {cid for cid, s in own.items() if s == "holdout"}
    train = [cid for cid in order if cid not in held]
    holdout = [cid for cid in order if cid in held]
    if not holdout:
        where.refuse("has no held-out case, so a winner couldn't be chosen on cases the search never saw; add "
                     "cases or set a case's split to holdout.")
    if not train:
        where.refuse("has no training case, so the search would have nothing to learn from; add cases or set a "
                     "case's split to train.")
    screen = suite["screen_cases"]
    return {"train": train, "holdout": holdout, "screening": train[:screen] if screen else list(train)}


# ---------------------------------------------------------------- the checks folder

def checks_dir_entries(checks_dir):
    """Every entry under checks_dir, following links into folders, as (path, real path, is a folder), sorted. A
    folder whose real path was already visited isn't entered again, so a loop of links ends."""
    visited = {os.path.realpath(checks_dir)}
    stack = [checks_dir]
    while stack:
        folder = stack.pop()
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        below = []
        for name in names:
            path = os.path.join(folder, name)
            real = os.path.realpath(path)
            is_folder = os.path.isdir(path)
            yield path, real, is_folder
            if is_folder and real not in visited:
                visited.add(real)
                below.append(path)
        stack.extend(reversed(below))


def check_checks_dir(where, root, rel):
    """Refuse an entry under checks_dir whose real path leaves the suite's folder (through a link): a command's
    {checks} must only reach scripts the suite owns."""
    checks_dir = os.path.join(root, rel)
    if not os.path.isdir(checks_dir):
        return
    top = os.path.realpath(root)
    for path, real, _is_folder in checks_dir_entries(checks_dir):
        if real != top and not real.startswith(top + os.sep):
            shown_rel = os.path.relpath(path, root).replace(os.sep, "/")
            where.refuse(f"{shown_rel} under checks_dir leads out of the suite file's folder (through a link); "
                         f"keep the check scripts inside that folder.")


# ---------------------------------------------------------------- the profile's evolvable files

def check_frozen(where, suite, profile, source):
    """Refuse a suite file, prompt_file, rubric_file, check script or checks folder that a genome may change: inside
    the source folder and matching the profile's evolvable globs. The search must never change its own tests or
    judge."""
    top = os.path.realpath(source)
    evolvable = genome_lib.matcher(profile["evolvable"])

    def refuse_if_evolvable(here, path, what):
        real = os.path.realpath(path)
        if not real.startswith(top + os.sep):
            return
        rel = os.path.relpath(real, top).replace(os.sep, "/")
        if evolvable(rel):
            here.refuse(f"{what} is {rel} in the source {source}, among the files the profile calls evolvable, so "
                        f"a genome could change the tests it is judged by. Keep it outside the evolvable files.")

    root = suite["root"]
    refuse_if_evolvable(where, suite["path"], "the suite file")
    for case in suite["cases"]:
        here = where.at(case=case["id"])
        if case["prompt_file"] is not None:
            refuse_if_evolvable(here, os.path.join(root, case["prompt_file"]), f"its prompt_file {case['prompt_file']}")
        for check in case["checks"]:
            if check.get("rubric_file") is not None:
                refuse_if_evolvable(here.at(check=check["id"]), os.path.join(root, check["rubric_file"]),
                                    f"its rubric_file {check['rubric_file']}")
    checks_dir = os.path.join(root, suite["checks_dir"])
    if os.path.isdir(checks_dir):
        # The scripts first, so the message names the one at fault; then the folders, empty ones included, since
        # a genome could add a script to an evolvable folder later.
        folders = [checks_dir]
        for path, _real, is_folder in checks_dir_entries(checks_dir):
            if is_folder:
                folders.append(path)
            else:
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                refuse_if_evolvable(where, path, f"the check script {rel}")
        for path in folders:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            refuse_if_evolvable(where, path, f"the checks_dir folder {rel}")


# ---------------------------------------------------------------- loading

def load_suite(path, profile=None, source=None):
    """Read and check the suite file at path; return it with every default filled in and its split.

    With profile (genome.load_profile's dict) and source (the target's folder), a suite file, prompt_file,
    rubric_file or check script among the files a genome may change is refused. Raises SuiteError."""
    path = os.path.abspath(path)
    where = Where(path)
    if (profile is None) != (source is None):
        where.refuse("can be checked against a profile only with both the profile and the target's source folder.")
    data = read_suite(path)
    suite = check_top(where, data)
    root = os.path.dirname(os.path.realpath(path))
    suite_relative(where, root, suite["checks_dir"], "checks_dir", kind=None)
    check_checks_dir(where, root, suite["checks_dir"])
    seen, cases = set(), []
    for place, raw in enumerate(data["cases"], 1):
        cases.append(check_case(where, root, suite, raw, place, seen))
    suite = {"path": path, "root": root, **suite, "cases": cases}
    suite["split"] = split_cases(where, suite, cases)
    if profile is not None:
        check_frozen(where, suite, profile, source)
    return suite


def summary_lines(suite):
    """The suite in plain English, a line at a time: its name, its cases, which train (screening marked) and which
    are held out, and each case's number of checks of each kind."""
    cases = {c["id"]: c for c in suite["cases"]}
    split = suite["split"]
    screening = set(split["screening"])
    lines = [f"Suite {suite['name']}: {evolve_core.plural(len(cases), 'case')}, "
             f"{len(split['train'])} training ({len(screening)} of them screening) and {len(split['holdout'])} "
             f"held out."]
    if suite["describe"]:
        lines.append(suite["describe"])
    if suite["sandbox"] and isinstance(suite["runner"], dict):
        lines.append("Its runner is the suite's own command, which the evolver doesn't sandbox: the agent's commands "
                     "can reach anything your user can.")

    def counts(cid):
        kinds = [c["kind"] for c in cases[cid]["checks"]]
        return "checks: " + ", ".join(f"{kinds.count(kind)} {kind}" for kind in KIND_KEYS)

    lines.append("Training cases:")
    for cid in split["train"]:
        mark = "train, screening" if cid in screening else "train"
        lines.append(f"  {cid} ({mark}): {counts(cid)}")
    lines.append("Held-out cases:")
    for cid in split["holdout"]:
        lines.append(f"  {cid} (held out): {counts(cid)}")
    return lines


# ---------------------------------------------------------------- running one case

RESULT_NAME = "result.json"
PROMPT_NAME = "prompt.txt"
OUTPUT_NAME = "output.log"
CHECKS_NAME = "checks"
KEPT_NAME = "workspace"
WORKSPACE_PREFIX = "suite-workspace-"


class PrepareFailed(Exception):
    """The workspace couldn't be prepared; the message is the result's problem sentence."""


def real_inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def find_case(suite, case_id):
    for case in suite["cases"]:
        if case["id"] == case_id:
            return case
    raise SuiteError(f"The suite {suite['path']} has no case {case_id}; its cases are "
                     f"{', '.join(c['id'] for c in suite['cases'])}.")


def case_prompt(suite, case):
    """The case's prompt: its prompt, or its prompt_file's text."""
    if case["prompt"] is not None:
        return case["prompt"]
    with open(os.path.join(suite["root"], case["prompt_file"]), encoding="utf-8", newline="") as f:
        return f.read()


def link_leading_out(folder):
    """The first entry under folder (relative, with /) whose real path leaves folder through a link, or None."""
    top = os.path.realpath(folder)
    for path, real, _is_folder in checks_dir_entries(folder):
        if real != top and not real.startswith(top.rstrip(os.sep) + os.sep):
            return os.path.relpath(path, folder).replace(os.sep, "/")
    return None


def copy_into(src, dest):
    """Copy a file or a folder's contents to dest, following links (the copy holds no link back to the source)."""
    if os.path.isdir(src):
        shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)


def frozen_hashes(workspace, globs):
    """{workspace path: sha256} of every file in the workspace matching the case's frozen globs."""
    if not globs:
        return {}
    matches = genome_lib.matcher(globs)
    found = {}
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, workspace).replace(os.sep, "/")
            if os.path.isfile(path) and matches(rel):
                found[rel] = genome_lib.sha256_bytes(genome_lib.read_bytes(path))
    return found


def prepare_workspace(suite, case, individual, workspace):
    """Copy the case's fixtures into the workspace, then the individual's files at the install path, then remove
    the project settings files either brought (PROJECT_SETTINGS); return the frozen fixtures' hashes. Raises
    PrepareFailed with a plain sentence."""
    root = suite["root"]
    for dest, src in case["files"].items():
        source = os.path.join(root, src)
        target = workspace if dest == "." else os.path.join(workspace, *dest.split("/"))
        leak = link_leading_out(source) if os.path.isdir(source) else None
        if leak is not None:
            raise PrepareFailed(f"The fixture folder {src} holds {leak}, a link leading out of that folder; copy "
                                f"the file into the fixture instead of linking to it.")
        try:
            copy_into(source, target)
        except (OSError, shutil.Error) as e:
            raise PrepareFailed(f"The fixture {src} couldn't be copied into the workspace at {dest} "
                                f"({getattr(e, 'strerror', None) or e}).") from None
    install = suite["install"]
    target = workspace if install == "." else os.path.join(workspace, *install.split("/"))
    leak = link_leading_out(individual)
    if leak is not None:
        raise PrepareFailed(f"The individual {individual} holds {leak}, a link leading out of that folder; copy the "
                            f"file into the individual instead of linking to it.")
    try:
        copy_into(individual, target)
    except (OSError, shutil.Error) as e:
        raise PrepareFailed(f"The individual {individual} couldn't be installed in the workspace at {install} "
                            f"({getattr(e, 'strerror', None) or e}).") from None
    remove_project_settings(workspace)
    try:
        return frozen_hashes(workspace, case["frozen"])
    except OSError as e:
        raise PrepareFailed(f"The frozen fixtures couldn't be read in the workspace ({e.strerror or e}).") from None


def private_parts(suite):
    """The suite's files the agent may never read, listed one by one so they stay denied when the suite's folder
    can't be denied whole (a suite file directly in the home folder): the suite file, its checks_dir, every case's
    prompt_file, rubric_file and fixtures."""
    root = suite["root"]
    parts = [suite["path"], os.path.join(root, suite["checks_dir"])]
    for case in suite["cases"]:
        if case["prompt_file"] is not None:
            parts.append(os.path.join(root, case["prompt_file"]))
        parts += [os.path.join(root, src) for src in case["files"].values()]
        parts += [os.path.join(root, check["rubric_file"]) for check in case["checks"]
                  if check.get("rubric_file") is not None]
    return parts


def repository_git_dirs(folder):
    """The git directories of the repository holding folder, whose history holds the suite's committed files: its
    own git directory and the shared one (the same folder for a plain repository; a worktree's own folder and the
    main repository's .git for a worktree). Found with git itself, run so it executes nothing from the repository's
    setup (no hooks, no fsmonitor, no external diff, no GIT_* variable from the environment). When git is missing or
    refuses the repository (another user's, say), the first .git found walking up from folder, and the folder a .git
    file points to. [] when folder lies in no repository."""
    cmd = ["git", "--no-optional-locks"]
    for setting in evolve_core.SAFE_GIT_CONFIG:
        cmd += ["-c", setting]
    cmd += ["-C", folder, "rev-parse", "--git-dir", "--git-common-dir"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    try:
        r = subprocess.run(cmd, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
        lines = [ln for ln in os.fsdecode(r.stdout).split("\n") if ln] if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    if lines:
        return list(dict.fromkeys(os.path.abspath(os.path.join(folder, ln)) for ln in lines))
    return dotgit_walk(folder)


def dotgit_walk(folder):
    """The first .git above or at folder, and the git directory a .git file points to (gitdir: ...); [] if none."""
    here = os.path.abspath(folder)
    while True:
        dotgit = os.path.join(here, ".git")
        if os.path.lexists(dotgit):
            found = [dotgit]
            if os.path.isfile(dotgit):
                try:
                    with open(dotgit, encoding="utf-8", errors="replace") as f:
                        first = f.readline().strip()
                except OSError:
                    first = ""
                if first.startswith("gitdir:"):
                    found.append(os.path.abspath(os.path.join(here, first[len("gitdir:"):].strip())))
            return found
        parent = os.path.dirname(here)
        if parent == here:
            return []
        here = parent


def remove_project_settings(workspace):
    """Remove .claude/settings.json and .claude/settings.local.json from the workspace: settings a fixture or the
    individual brought would load in Claude Code (hooks running outside the sandbox, sandbox lists merged). Returns
    the workspace paths removed."""
    removed = []
    for name in PROJECT_SETTINGS:
        path = os.path.join(workspace, *name.split("/"))
        if os.path.lexists(path):
            try:
                remove_tree(path)
            except OSError as e:
                raise PrepareFailed(f"{name}, which a fixture or the individual put in the workspace, couldn't be "
                                    f"removed ({e.strerror or e}); a run never starts with project settings of "
                                    f"its own.") from None
            removed.append(name)
    return removed


def remove_tree(path):
    """Remove a folder even where the agent left read-only folders behind."""
    def retry(func, target, _info):
        try:
            os.chmod(os.path.dirname(target), stat.S_IRWXU)
            os.chmod(target, stat.S_IRWXU)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, onerror=retry)
        else:
            os.unlink(path)


def copy_checks(suite, folder, result):
    """Copy the suite's checks_dir fresh into folder/checks/. When that fails, the run can't be checked: the
    outcome becomes aborted and result's problem says why (the partial copy is removed)."""
    checks = os.path.join(suite["root"], suite["checks_dir"])
    if not os.path.isdir(checks):
        return
    dest = os.path.join(folder, CHECKS_NAME)
    try:
        shutil.copytree(checks, dest, symlinks=False)
    except (OSError, shutil.Error) as e:
        if isinstance(e, shutil.Error) and e.args and isinstance(e.args[0], list) and e.args[0]:
            first = e.args[0][0]
            reason = f"{first[0]}: {first[2]}" if len(first) == 3 else str(first)
            if len(e.args[0]) > 1:
                reason += f", and {len(e.args[0]) - 1} more"
        else:
            reason = getattr(e, "strerror", None) or str(e)
            if getattr(e, "filename", None):
                reason = f"{e.filename}: {reason}"
        remove_tree(dest)
        result.update(outcome="aborted", problem=f"The check scripts in {checks} couldn't be copied into the run "
                                                 f"folder after the runner ended ({reason}), so the run can't be "
                                                 f"checked.")


def run_case(suite, case, individual, folder, config, keep=False, deny_read=()):
    """Run the case (its id) of the suite (load_suite's dict) with the individual (a folder of the target's files)
    and write the run folder's prompt.txt, output.log, checks/ and result.json; return the result.

    The workspace is a new temporary folder outside the suite's folder, the individual and the run folder. It gets
    the case's fixtures, then the individual's files at the suite's install path; the frozen fixtures are hashed
    before the runner starts. After the runner ends, the suite's checks_dir is copied fresh into folder/checks/
    (never into the workspace). The workspace is then removed, or with keep moved to folder/workspace/. The suite's
    folder and the individual are only read. result.json: case, outcome (done, failed, timeout or paused, from the
    runner; aborted with a problem sentence when the workspace couldn't be prepared or the runner couldn't start),
    tokens {used}, runner {exit, seconds}, started_at, ended_at, frozen {path: sha256}, workspace (kept path or
    null). deny_read lists more folders the default runner's agent may not read (the evolver's working folder,
    with earlier runs' check scripts and other individuals); the suite's folder (or, when it holds the home folder,
    its suite file, checks_dir, prompt, rubric and fixture files one by one), the individual and the run folder are
    always denied, and so are the git directories of a repository holding the suite file (its history holds the
    check scripts); a run whose private paths can't all be denied is aborted with a problem sentence. The
    workspace never holds .claude/settings.json or .claude/settings.local.json (removed after the copy). A check
    scripts copy that fails gives the outcome aborted with a problem sentence (the workspace is
    still removed). The default runner refused for a missing sandbox raises (runners.SandboxRefused) before
    anything is written; so does a case the suite doesn't have, or a run folder overlapping the suite's folder or
    the individual."""
    entry = find_case(suite, case)
    runners.check_ready(suite["runner"], config, suite["sandbox"])
    individual = os.path.abspath(individual)
    folder = os.path.abspath(folder)
    if not os.path.isdir(individual):
        raise evolve_core.EvolveError(f"The individual {individual} isn't a folder; give the folder holding the "
                                      f"target's files.")
    for name, other in (("the suite's folder", suite["root"]), ("the individual", individual)):
        if real_inside(folder, other) or (os.path.exists(folder) and real_inside(other, folder)):
            raise evolve_core.EvolveError(f"The run folder {folder} overlaps {name} ({other}), which a run must "
                                          f"never change; run it elsewhere.")
    os.makedirs(folder, exist_ok=True)
    for name in (RESULT_NAME, OUTPUT_NAME, PROMPT_NAME, CHECKS_NAME, KEPT_NAME):
        remove_tree(os.path.join(folder, name))
    result = {"case": entry["id"], "outcome": None, "tokens": {"used": 0}, "runner": {"exit": None, "seconds": 0},
              "started_at": evolve_core.now_iso(), "ended_at": None, "frozen": {}, "workspace": None}
    workspace = None
    try:
        try:
            prompt = case_prompt(suite, entry)
        except (OSError, UnicodeDecodeError) as e:
            raise PrepareFailed(f"The prompt file {entry['prompt_file']} couldn't be read "
                                f"({getattr(e, 'strerror', None) or e}).") from None
        prompt_file = os.path.join(folder, PROMPT_NAME)
        with open(prompt_file, "w", encoding="utf-8", newline="") as f:
            f.write(prompt)
        workspace = os.path.realpath(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX))
        private = [("the suite's folder", suite["root"]), ("the individual", individual), ("the run folder", folder)]
        private += [("a folder the agent may not read", other) for other in deny_read]
        for name, other in private:
            if real_inside(workspace, other) or real_inside(other, workspace):
                raise PrepareFailed(f"The temporary folder for the workspace ({workspace}) overlaps {name} "
                                    f"({other}); set TMPDIR to a folder outside both.")
        result["frozen"] = prepare_workspace(suite, entry, individual, workspace)
        installed = workspace if suite["install"] == "." else os.path.join(workspace, *suite["install"].split("/"))
        # The suite's folder whole when it can be denied (it doesn't hold the home folder), and its private files
        # one by one in any case; runners.run refuses the run when one of these can't be denied.
        private_reads = [suite["root"]] if not runners.holds_home(suite["root"]) else []
        private_reads += private_parts(suite) + [individual, folder, *deny_read]
        # The git directories of a repository holding the suite: its history holds the committed check scripts.
        git_dirs = repository_git_dirs(os.path.dirname(os.path.abspath(suite["path"])))
        if suite["runner"] == DEFAULT_RUNNER:
            for git_dir in git_dirs:
                why = runners.undeniable(git_dir, workspace)
                if why is not None:
                    raise PrepareFailed(f"The agent can't be kept from reading {git_dir}, the git folder of the "
                                        f"repository holding the suite, whose history holds its check scripts: "
                                        f"{why}. Keep the suite in a repository whose git folder holds neither, "
                                        f"or outside any repository, then run again.")
        private_reads += git_dirs
        ran = runners.run(suite["runner"], workspace, installed, prompt, suite["model"],
                          suite["timeout_minutes"] * 60, config=config, output=os.path.join(folder, OUTPUT_NAME),
                          prompt_file=prompt_file, sandbox=suite["sandbox"], deny_read=private_reads)
        result.update(outcome=ran["outcome"], tokens=dict(ran["tokens"]),
                      runner={"exit": ran["exit"], "seconds": ran["seconds"]})
    except (PrepareFailed, runners.RunnerError) as e:
        if isinstance(e, runners.SandboxRefused):
            raise
        result.update(outcome="aborted", problem=" ".join(str(e).split()))
    finally:
        try:
            if result["outcome"] is not None:
                copy_checks(suite, folder, result)
        finally:
            if workspace is not None:
                if keep and result["outcome"] is not None:
                    kept = os.path.join(folder, KEPT_NAME)
                    shutil.move(workspace, kept)
                    result["workspace"] = kept
                else:
                    remove_tree(workspace)
    result["ended_at"] = evolve_core.now_iso()
    evolve_core.write_json(os.path.join(folder, RESULT_NAME), result)
    return result
