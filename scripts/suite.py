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

This module is standard library only, plus genome.py (paths and globs) and evolve_core.py (the error class). It
names no target: everything specific to one lives in the suite file and the target's profile."""
import hashlib
import json
import math
import os
import re
import string
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import genome as genome_lib  # noqa: E402
import evolve_core  # noqa: E402

DEFAULT_RUNNER = "claude"  # the default runner's name; the runner itself is built in a later step
TOKEN_READERS = ("none", "claude-json")  # how a custom runner's output tells its tokens: not at all, or as JSON
PLACEHOLDERS = ("workspace", "individual", "prompt_file", "prompt", "model")  # what a runner template may use
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
