#!/usr/bin/env python3
"""Genomes: the evolvable part of a target (a plugin, a skill or any folder of prompt files), stored apart from
it, and the expansion of a genome back into a complete copy of the target, its individual.

The design is in docs/genome-spec.md, sections "The genome" and "CLI and files". This module is standard library
only and knows nothing about any one target: everything target-specific comes from a profile, a JSON file the
target's maintainer keeps. The evolver imports it and adds the command line.

A profile holds:
  name        the target's name, recorded in every genome seeded with it
  evolvable   globs of the files a genome holds and may change, relative to the source folder
  exclude     globs of files never copied into an individual (tests, docs, version control, caches)
  version     optional {"file", "key"}: the JSON file and dotted key holding the target's version
  traits      {trait name: spec}. Each spec has a source, {"json": <file>, "key": <dotted key>} or
              {"front_matter": <markdown file>, "field": <name>}, and bounds: {"min", "max", "step"} for a number
              (a value lies in [min, max] and is min plus a whole number of steps) or {"choices": [...]}. A numeric
              trait may also have {"looser": "higher"|"lower", "project": [{"json": <file>, "key": <dotted key>},
              ...]}: the first of those files, relative to a project, that has the key holds the project's own
              value, and a genome's value may never be looser than it. Numbers, bounds included, must be finite.
              A trait whose source is one of the evolvable files has its value in that file, so a genome's file and
              its genome.json agree; one whose source is a frozen file lives only in genome.json.
  relations   optional list of [trait, "<" or "<=", trait] that every genome's values must satisfy
  never       optional globs of files no genome may ever hold or change (the target's engine, its hooks, its
              manifests, its tests). They may not overlap the evolvable globs.
  caps        optional list of {"glob", "max_growth_pct" and/or "max_bytes"}: how large a genome's file matching
              the glob may be, as growth over the same file in the genome's root seed (the ancestor with no parents;
              exactly the limit is allowed) or as a size in bytes
  validate    optional command that checks an expanded individual, with {plugin} standing for its folder
  describe    optional: one sentence saying what the target's files are, for the prompts that rewrite them
              (without it, a prompt says only that the files are the instructions of <name>)
  checks      optional list of commands run in a checkout of the target holding a winner's files before its pull
              request is opened (`evolve.py promote`); every one must pass
  judges      optional list of exact paths (not globs) of evolvable files that judge the others' work. A child's
              judge files must stay byte for byte as its root seed's copies (traits held in them included), so no
              candidate is ever judged by a judge it changed

The maintainer decides which traits exist and each one's range; a genome only picks values inside them, so the
profile must lie outside the files a genome may change, and a genome.json carries nothing but what seed writes.

Every file of the source folder that is neither evolvable nor excluded is the frozen layer: an individual gets it
unchanged. Globs are anchored at the source folder: `*` and `?` stay inside one folder, `**/` stands for any
number of folders (none included), and `<folder>/**` covers the folder itself and everything under it.

Storage, under the evolver's working folder (`home` below):
  genomes/<id>/genome.json    id, profile, parents, base, the evolvable files' paths, sizes and hashes, traits
  genomes/<id>/sources.json   where each trait held in the genome's own files is read (no bounds), so loading a
                              genome can check that genome.json and those files agree
  genomes/<id>/limits.json    the profile's never globs and caps when the genome was made (never its bounds), so
                              loading a genome without its profile still refuses a path no genome may hold
  genomes/<id>/files/<path>   the genome's evolvable files
  genomes/<id>/plugin/        the expanded individual
  genomes/<id>/plugin.sha256.json  every file of plugin/ with its sha256, so a changed, missing or extra file in
                              the individual, frozen ones included, is caught before it runs
  genomes/discarded.jsonl     one line per candidate discarded as a near-duplicate of a stored genome (its parent,
                              the failure it aimed at, the closest genome, the changed characters and the mutation's kind)
"""
import collections
import datetime as dt
import difflib
import fractions
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

GENOME_FORMAT = 1  # genome.json's "version": the format of the file, not the target's version
PROFILE_KEYS = {"name", "evolvable", "exclude", "version", "traits", "relations", "validate", "never", "caps",
                "describe", "checks", "judges"}
REQUIRED_KEYS = ("name", "evolvable", "traits")
GENOME_KEYS = {"id", "version", "profile", "parents", "created_at", "base", "files", "traits"}  # what seed writes
FILE_KEYS = {"path", "sha256", "bytes"}
SOURCES_FILE = "sources.json"  # beside genome.json: where each trait held in the genome's own files is read
LIMITS_FILE = "limits.json"  # beside genome.json: the profile's never globs and caps, never its bounds
PLUGIN_HASHES = "plugin.sha256.json"  # beside plugin/: every file of the individual with its sha256
RESERVED_RECORDS = ("genome.json", SOURCES_FILE, LIMITS_FILE, PLUGIN_HASHES)  # no extra record takes these names
CAP_KEYS, CAP_LIMITS = {"glob", "max_growth_pct", "max_bytes"}, ("max_growth_pct", "max_bytes")
NUMBER_BOUNDS, CHOICE_BOUNDS, LOOSER_KEYS = {"min", "max", "step"}, {"choices"}, {"looser", "project"}
RELATIONS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
MISSING = object()  # a key a JSON file doesn't have


class GenomeError(Exception):
    """A problem the user can fix, said in one plain sentence or two."""


# ---------------------------------------------------------------- names, paths and globs

def safe_id(name):
    """A genome id that is a plain folder name: letters, digits, _ . -, never starting with a dot, never `..`."""
    return isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)) and ".." not in name


def safe_path(rel):
    """A relative path with / separators that stays inside its folder: no leading /, no `..`, no empty parts."""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def glob_regex(pattern):
    """The regular expression of one profile glob (see the module's docstring for the rules)."""
    whole = pattern.endswith("/**")
    pat = pattern[:-3] if whole else pattern
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pat[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(out + ("(?:/.*)?" if whole else "") + r"\Z")


def matcher(globs):
    regexes = [glob_regex(g) for g in globs]
    return lambda rel: any(r.match(rel) for r in regexes)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_json(path, data):
    """Write JSON through a temporary file and a rename, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def remove_tree(path):
    """Delete a folder this module built, even when copied files inside it are read-only."""
    if os.path.lexists(path):
        for dirpath, dirnames, _ in os.walk(path):
            for n in dirnames:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    os.chmod(p, os.stat(p).st_mode | 0o700)
        shutil.rmtree(path)


# ---------------------------------------------------------------- the profile

def load_profile(path, source=None):
    """Read and check a profile; return it as a dict with every optional key filled in.

    With source (the folder the profile is for), a profile that lies among the source's evolvable files is refused:
    a genome could then change its own bounds."""
    try:
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
    except OSError as e:
        raise GenomeError(f"Can't read the profile {path}: {e.strerror or e}.")
    except ValueError as e:
        raise GenomeError(f"The profile {path} isn't valid JSON: {e}.")
    where = f"The profile {path}"
    if not isinstance(profile, dict):
        raise GenomeError(f"{where} must hold a JSON object.")
    unknown = sorted(set(profile) - PROFILE_KEYS)
    if unknown:
        raise GenomeError(f"{where} has keys a profile doesn't take ({', '.join(unknown)}); it takes "
                          f"{', '.join(sorted(PROFILE_KEYS))}.")
    missing = [k for k in REQUIRED_KEYS if k not in profile]
    if missing:
        raise GenomeError(f"{where} has no {', '.join(missing)}.")
    if not isinstance(profile["name"], str) or not profile["name"].strip():
        raise GenomeError(f"{where} needs a name (a non-empty string).")
    for key in ("evolvable", "exclude", "never"):
        globs = profile.get(key, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) and safe_path(g) for g in globs):
            raise GenomeError(f"{where}: {key} must be a list of globs relative to the source folder "
                              f"(no leading /, no `..`).")
    if not profile["evolvable"]:
        raise GenomeError(f"{where}: evolvable names no files, so a genome would hold nothing.")
    version = profile.get("version")
    if version is not None and not (isinstance(version, dict) and set(version) == {"file", "key"}
                                    and safe_path(version["file"]) and isinstance(version["key"], str)
                                    and version["key"]):
        raise GenomeError(f"{where}: version must be {{\"file\": <relative path>, \"key\": <dotted key>}}.")
    traits = profile["traits"]
    if not isinstance(traits, dict):
        raise GenomeError(f"{where}: traits must be an object of trait name to its source and bounds.")
    for name, spec in traits.items():
        check_trait_spec(where, name, spec)
    relations = profile.get("relations", [])
    if not isinstance(relations, list):
        raise GenomeError(f"{where}: relations must be a list of [trait, \"<\" or \"<=\", trait].")
    for relation in relations:
        if not (isinstance(relation, list) and len(relation) == 3 and relation[1] in RELATIONS
                and all(isinstance(t, str) for t in (relation[0], relation[2]))):
            raise GenomeError(f"{where}: the relation {json.dumps(relation)} must be [trait, \"<\" or \"<=\", "
                              f"trait].")
        for name in (relation[0], relation[2]):
            if name not in traits:
                raise GenomeError(f"{where}: the relation {json.dumps(relation)} names {name}, which isn't one of "
                                  f"its traits.")
            if "choices" in traits[name]:
                raise GenomeError(f"{where}: the relation {json.dumps(relation)} names {name}, which is a choice "
                                  f"trait; relations compare numbers only.")
    validate = profile.get("validate")
    if validate is not None and not (isinstance(validate, str) and "{plugin}" in validate):
        raise GenomeError(f"{where}: validate must be a command with {{plugin}} where the individual's folder goes.")
    caps = profile.get("caps", [])
    check_caps(where, caps)
    describe = profile.get("describe")
    if describe is not None and not (isinstance(describe, str) and describe.strip()):
        raise GenomeError(f"{where}: describe must be one sentence saying what the target's files are.")
    checks = profile.get("checks", [])
    if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks):
        raise GenomeError(f"{where}: checks must be a list of commands (strings) run before a winner is promoted.")
    judges = profile.get("judges", [])
    check_judges(where, profile, judges)
    profile = {"name": profile["name"], "evolvable": list(profile["evolvable"]),
               "exclude": list(profile.get("exclude", [])), "version": version,
               "traits": {name: dict(spec) for name, spec in traits.items()},
               "relations": [list(r) for r in relations], "validate": validate,
               "never": list(profile.get("never", [])), "caps": [dict(c) for c in caps],
               "describe": " ".join(describe.split()) if describe is not None else None,
               "checks": list(checks), "judges": list(judges)}
    if source is None:
        check_never_globs(where, profile)
    else:
        check_profile_place(path, profile, source)
        check_never_files(where, profile, source)
    return profile


def check_judges(where, profile, judges):
    """The profile's judges: a list of exact paths (not globs) of evolvable files that judge the others' work. Each
    must be a relative path the profile lets a genome hold: evolvable, not excluded and on no never glob. Raises
    GenomeError naming the file."""
    if not isinstance(judges, list) or not all(isinstance(j, str) and safe_path(j) for j in judges):
        raise GenomeError(f"{where}: judges must be a list of file paths relative to the source folder (no leading /, "
                          f"no `..`), each one a file that judges the others' work.")
    if len(set(judges)) != len(judges):
        raise GenomeError(f"{where}: judges lists a file twice.")
    for judge in judges:
        glob = never_glob(profile.get("never", []), judge)
        if glob is not None:
            raise GenomeError(f"{where}: the judge {judge} matches the never glob {glob}; a judge must be one of the "
                              f"evolvable files, kept as its root seed's copy.")
        if not matcher(profile["evolvable"])(judge) or matcher(profile.get("exclude", []))(judge):
            raise GenomeError(f"{where}: the judge {judge} isn't one of the files the profile calls evolvable; a "
                              f"judge must be an evolvable file, kept as its root seed's copy.")


def judge_refusal(profile, rel):
    """Why rel may not be a mutation's target, when it is one of the profile's judges (a plain sentence naming it);
    else None. Callers check this before anything runs."""
    if rel not in profile.get("judges", []):
        return None
    return (f"{rel} judges the others' work (the profile {profile['name']} lists it among its judges), so no "
            f"candidate may change it: a candidate judged by a judge it changed couldn't be scored fairly.")


def judge_trait_refusal(profile, name):
    """Why the trait name may not be moved, when its source is one of the profile's judge files; else None."""
    spec = profile["traits"].get(name)
    rel = spec.get("json", spec.get("front_matter")) if isinstance(spec, dict) else None
    if rel is None or rel not in profile.get("judges", []):
        return None
    return f"The trait {name} is held in {rel}. {judge_refusal(profile, rel)}"


def judge_problems(home, profile, parent, contents):
    """A plain problem for each judge file of the profile (see check_judges) whose bytes in the child (contents,
    {path: bytes}) differ from its root seed's copy: a judge judges the others' work, so a candidate that changed it
    couldn't be scored fairly. A judge the child doesn't hold has nothing to compare."""
    judges = [j for j in profile.get("judges", []) if j in contents]
    if not judges:
        return []
    try:
        root = root_seed(home, parent, profile)
    except GenomeError as e:
        return [f"The judge files ({', '.join(judges)}) can't be compared with the root seed's copies: {e}"]
    hashes = {entry["path"]: entry["sha256"] for entry in root["files"]}
    problems = []
    for judge in judges:
        if hashes.get(judge) != sha256_bytes(contents[judge]):
            problems.append(f"{judge} differs from the root seed {root['id']}'s copy, and it judges the others' work: "
                            f"a candidate that changed its own judge couldn't be scored fairly, so no candidate may "
                            f"change it (its traits included).")
    return problems


def check_caps(where, caps):
    """The profile's caps: a list of {"glob", "max_growth_pct" and/or "max_bytes"}, each limit a finite number of 0
    or more. Raises GenomeError naming the cap's glob."""
    if not isinstance(caps, list):
        raise GenomeError(f"{where}: caps must be a list of {{\"glob\", \"max_growth_pct\" and/or \"max_bytes\"}}.")
    for cap in caps:
        glob = cap.get("glob") if isinstance(cap, dict) else None
        if not isinstance(glob, str) or not safe_path(glob):
            raise GenomeError(f"{where}: the cap {shown(cap)} needs a glob relative to the source folder (no leading /, "
                              f"no `..`).")
        about = f"{where}: the cap on {glob}"
        extra = sorted(set(cap) - CAP_KEYS)
        if extra:
            raise GenomeError(f"{about} has keys a cap doesn't take ({', '.join(extra)}); it takes glob, "
                              f"max_growth_pct and max_bytes.")
        limits = [k for k in CAP_LIMITS if k in cap]
        if not limits:
            raise GenomeError(f"{about} has no limit: give it max_growth_pct (growth over the root seed's copy, in "
                              f"percent), max_bytes, or both.")
        for key in limits:
            if not is_number(cap[key]):
                raise GenomeError(f"{about} has {key} {shown(cap[key])}; a limit must be a finite number.")
            if cap[key] < 0:
                raise GenomeError(f"{about} has {key} {shown(cap[key])}; a limit can't be negative.")


def check_never_globs(where, profile):
    """Without a source to look at: refuse a profile where an evolvable glob and a never glob could match the same
    file, naming both globs and an example of such a file."""
    for evolvable in profile["evolvable"]:
        for never in profile["never"]:
            example = globs_overlap(evolvable, never)
            if example is not None:
                raise GenomeError(f"{where}: the evolvable glob {evolvable} and the never glob {never} can match the "
                                  f"same file (for example {example}), so a genome could hold a file it must never "
                                  f"change. Narrow one of them.")


def check_never_files(where, profile, source):
    """With a source: refuse a profile where one of the source's files is both evolvable and never, naming it."""
    evolvable, _frozen = source_files(profile, source)
    problems = never_problems(profile["never"], evolvable, f"in the source {source}, is evolvable")
    if problems:
        raise GenomeError(f"{where} calls files both evolvable and never: {' '.join(problems)} Narrow the evolvable "
                          f"or the never globs.")


def never_glob(globs, rel):
    """The first of globs that matches rel, or None."""
    return next((g for g in globs if glob_regex(g).match(rel)), None)


def never_problems(globs, paths, what):
    """A plain problem for each path that one of the never globs matches."""
    problems = []
    for rel in paths:
        glob = never_glob(globs, rel)
        if glob is not None:
            problems.append(f"{rel}, {what} but matches the never glob {glob}.")
    return problems


def glob_automaton(pattern):
    """A glob (the rules of glob_regex) as a small automaton: (moves, empties, accepting). moves[state] lists
    (what, next state), what being one character, "part" (any character but /) or "any"; empties[state] lists the
    states reached without reading a character."""
    moves, empties = [[]], [[]]

    def new():
        moves.append([])
        empties.append([])
        return len(moves) - 1

    whole = pattern.endswith("/**")
    pat = pattern[:-3] if whole else pattern
    cur, i = 0, 0
    while i < len(pat):
        if pat.startswith("**/", i):
            inside, after = new(), new()
            moves[cur].append(("part", inside))
            moves[inside] += [("part", inside), ("/", cur)]
            empties[cur].append(after)
            cur, i = after, i + 3
            continue
        nxt = new()
        if pat.startswith("**", i):
            moves[cur].append(("any", cur))
            empties[cur].append(nxt)
            i += 2
        elif pat[i] == "*":
            moves[cur].append(("part", cur))
            empties[cur].append(nxt)
            i += 1
        else:
            moves[cur].append(("part" if pat[i] == "?" else pat[i], nxt))
            i += 1
        cur = nxt
    accepting = {cur}
    if whole:
        below = new()
        moves[cur].append(("/", below))
        moves[below].append(("any", below))
        accepting.add(below)
    return moves, empties, accepting


# A path of real files: parts of one or more characters, joined by single slashes.
PATH_AUTOMATON = ([[("part", 1)], [("part", 1), ("/", 0)]], [[], []], {1})


def globs_overlap(first, second):
    """A path both globs match (one example), or None when they can never match the same path."""
    automata = [glob_automaton(first), glob_automaton(second), PATH_AUTOMATON]
    letters = ["\x00"] + sorted(set(first + second) - set("*?/")) + ["/"]  # \x00: any other character

    def closure(automaton, states):
        _moves, empties, _accepting = automaton
        out, todo = set(states), list(states)
        while todo:
            for s in empties[todo.pop()]:
                if s not in out:
                    out.add(s)
                    todo.append(s)
        return frozenset(out)

    def step(automaton, states, letter):
        moves = automaton[0]
        return closure(automaton, {n for s in states for what, n in moves[s]
                                   if what == letter or what == "any" or (what == "part" and letter != "/")})

    start = tuple(closure(a, {0}) for a in automata)
    seen, todo = {start}, [(start, "")]
    while todo:
        states, text = todo.pop(0)
        if all(s & a[2] for s, a in zip(states, automata)):
            return text.replace("\x00", "x")
        for letter in letters:
            nxt = tuple(step(a, s, letter) for a, s in zip(automata, states))
            if all(nxt) and nxt not in seen:
                seen.add(nxt)
                todo.append((nxt, text + letter))
    return None


def is_number(value):
    """A finite number: not a bool, not NaN, not infinite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isinstance(value, int) or math.isfinite(value)


def check_trait_spec(where, name, spec):
    """One trait of a profile: a source, then bounds for a number or a list of choices, then the optional pair
    looser and project. Raises GenomeError naming the trait."""
    trait = f"{where}: the trait {name!r}"
    if not name or not isinstance(spec, dict):
        raise GenomeError(f"{trait} needs an object with its source and bounds.")
    if "json" in spec:
        source_keys = {"json", "key"}
    elif "front_matter" in spec:
        source_keys = {"front_matter", "field"}
    else:
        source_keys = set()
    file, field = spec.get("json", spec.get("front_matter")), spec.get("key", spec.get("field"))
    if not source_keys <= set(spec) or not source_keys or not safe_path(file) or not isinstance(field, str) \
            or not field:
        raise GenomeError(f"{trait} needs a source {{\"json\": <file>, \"key\": <dotted key>}} or "
                          f"{{\"front_matter\": <markdown file>, \"field\": <name>}}, with a file relative to the "
                          f"source folder.")
    rest = set(spec) - source_keys
    if NUMBER_BOUNDS <= rest:
        bounds = NUMBER_BOUNDS
        low, high, step = spec["min"], spec["max"], spec["step"]
        if not all(is_number(v) for v in (low, high, step)):
            raise GenomeError(f"{trait} needs finite numbers for min, max and step (not NaN or infinity).")
        if low > high:
            raise GenomeError(f"{trait} has min {low} above max {high}.")
        if step <= 0:
            raise GenomeError(f"{trait} has the step {step}; a step must be above 0.")
    elif CHOICE_BOUNDS <= rest:
        bounds = CHOICE_BOUNDS
        choices = spec["choices"]
        if not isinstance(choices, list) or not choices or len(set(map(json.dumps, choices))) != len(choices) \
                or not all(isinstance(c, str) or is_number(c) for c in choices):
            raise GenomeError(f"{trait} needs a non-empty list of different choices (strings or finite numbers).")
    else:
        raise GenomeError(f"{trait} has no bounds: give it min, max and step for a number, or a list of choices.")
    extra = rest - bounds - LOOSER_KEYS
    if extra:
        raise GenomeError(f"{trait} has keys a trait doesn't take ({', '.join(sorted(extra))}); a trait takes its "
                          f"source, then min, max and step or choices, then looser and project.")
    if rest & LOOSER_KEYS:
        if bounds is CHOICE_BOUNDS:
            raise GenomeError(f"{trait} is a choice trait, so it can't be looser or stricter than a project's value; "
                              f"remove looser and project.")
        if not LOOSER_KEYS <= rest:
            raise GenomeError(f"{trait} needs both looser and project, or neither.")
        if spec["looser"] not in ("higher", "lower"):
            raise GenomeError(f"{trait} has looser {json.dumps(spec['looser'])}; it must be \"higher\" or "
                              f"\"lower\".")
        sources = spec["project"]
        if not isinstance(sources, list) or not sources or not all(
                isinstance(s, dict) and set(s) == {"json", "key"} and safe_path(s["json"])
                and isinstance(s["key"], str) and s["key"] for s in sources):
            raise GenomeError(f"{trait} needs project as a non-empty list of {{\"json\": <file relative to a "
                              f"project>, \"key\": <dotted key>}}.")


def check_profile_place(path, profile, source):
    """Refuse a profile that is one of the source's evolvable files."""
    top, here = os.path.realpath(source), os.path.realpath(path)
    if not here.startswith(top + os.sep):
        return
    rel = os.path.relpath(here, top).replace(os.sep, "/")
    if matcher(profile["evolvable"])(rel):
        raise GenomeError(f"The profile {path} is {rel} in the source {source}, one of the files the profile calls "
                          f"evolvable, so a genome could change its own bounds. Keep the profile outside the "
                          f"evolvable files.")


# ---------------------------------------------------------------- reading the source

def source_files(profile, source):
    """The source's files that aren't excluded, as (evolvable, frozen): two sorted lists of relative paths.

    A symlink is refused: an individual is a plain copy, and a link could reach outside it."""
    if not os.path.isdir(source):
        raise GenomeError(f"The source {source} isn't a folder.")
    excluded, evolvable = matcher(profile["exclude"]), matcher(profile["evolvable"])
    found = []
    for dirpath, dirnames, filenames in os.walk(source, onerror=walk_error):
        rel_dir = os.path.relpath(dirpath, source).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for name in list(dirnames):
            rel = prefix + name
            if excluded(rel):
                dirnames.remove(name)
            elif os.path.islink(os.path.join(dirpath, name)):
                raise GenomeError(f"{rel} in {source} is a symlink; a genome's source must hold plain files only.")
        dirnames.sort()
        for name in sorted(filenames):
            rel = prefix + name
            if excluded(rel):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                raise GenomeError(f"{rel} in {source} isn't a plain file; a genome's source must hold plain files "
                                  f"only.")
            found.append(rel)
    found.sort()
    return [r for r in found if evolvable(r)], [r for r in found if not evolvable(r)]


def walk_error(e):
    raise GenomeError(f"Can't read the folder {e.filename}: {e.strerror or e}.")


def source_text(source, rel, why):
    path = os.path.join(source, *rel.split("/"))
    if not os.path.isfile(path):
        raise GenomeError(f"{why} reads {rel}, which doesn't exist in {source}.")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError) as e:
        raise GenomeError(f"{why} reads {rel}, which can't be read: {getattr(e, 'strerror', None) or e}.")


def json_value(source, rel, key, why):
    return json_text_value(source_text(source, rel, why), rel, key, why)


def json_text_value(text, rel, key, why):
    """The value at a dotted key of the JSON text of the file rel."""
    try:
        value = json.loads(text)
    except ValueError as e:
        raise GenomeError(f"{why} reads {rel}, which isn't valid JSON: {e}.")
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise GenomeError(f"{why} reads the key {key} of {rel}, which has no {key}.")
        value = value[part]
    return value


def front_matter_value(source, rel, field, why):
    return front_matter_text_value(source_text(source, rel, why), rel, field, why)


def front_matter_line(text, rel, field, why):
    """The one front-matter line that sets a field, as (line number, the text's lines with their endings).

    The front matter is the lines between a first line `---` and the next `---` line; without that closing line
    there is none. The field is read strictly, not as general YAML: it must appear exactly once (a line naming it
    with quotes, spaces or indentation counts too), start its line (`field:`), and the next line that isn't blank
    must not be indented, which would continue the value. Anything else raises GenomeError naming the file."""
    lines = text.splitlines(keepends=True)
    bare = [line.splitlines()[0] if line.splitlines() else "" for line in lines]
    closing = next((i for i, line in enumerate(bare) if i and line.strip() == "---"), None)
    about = f"{why} reads the front-matter field {field} of {rel}"
    if not lines or bare[0].strip() != "---" or closing is None:
        raise GenomeError(f"{about}, which has no front matter (a first line --- and a closing --- line).")
    naming = re.compile(r"\s*([\"']?)" + re.escape(field) + r"\1\s*:")
    found = [n for n in range(1, closing) if naming.match(bare[n])]
    if not found:
        raise GenomeError(f"{about}, whose front matter has no {field}.")
    if len(found) > 1:
        raise GenomeError(f"{about}, whose front matter names {field} {len(found)} times; it must appear once.")
    n = found[0]
    if not bare[n].startswith(field + ":"):
        raise GenomeError(f"{about}, which must be written `{field}: <value>` at the start of its line, but the "
                          f"line is {bare[n].strip()!r}.")
    after = next((line for line in bare[n + 1:closing] if line.strip()), "")
    if after[:1].isspace():
        raise GenomeError(f"{about}, whose value goes on over an indented line ({after.strip()!r}); it must fit "
                          f"on one line.")
    return n, lines


FRONT_MATTER_TOKEN = re.compile(r"-?[0-9]+(?:\.[0-9]+)?|[A-Za-z0-9_.][A-Za-z0-9_.-]*")
FRONT_MATTER_REST = re.compile(r" +(?P<value>[^ #]+)(?: +#.*| *)")
FRONT_MATTER_INT, FRONT_MATTER_FLOAT = re.compile(r"-?(?:0|[1-9][0-9]*)"), re.compile(r"-?(?:0|[1-9][0-9]*)\.[0-9]+")


def front_matter_token(token):
    """A plain front-matter value as a trait value: a whole or decimal number as a number, any other plain token
    (letters, digits, dot, underscore, hyphen; a leading minus only for a number) as a string; None for anything
    else, such as a quoted or empty value."""
    if not FRONT_MATTER_TOKEN.fullmatch(token):
        return None
    if FRONT_MATTER_INT.fullmatch(token):
        return int(token)
    if FRONT_MATTER_FLOAT.fullmatch(token):
        return float(token)
    return token


def front_matter_text_value(text, rel, field, why):
    """A field of a markdown text's front matter (see front_matter_line): its value is one plain token (see
    front_matter_token), optionally followed by spaces and an inline ` # comment`, which isn't part of it."""
    n, lines = front_matter_line(text, rel, field, why)
    rest = lines[n].splitlines()[0][len(field) + 1:]
    match = FRONT_MATTER_REST.fullmatch(rest)
    value = front_matter_token(match["value"]) if match else None
    about = f"{why} reads the front-matter field {field} of {rel}"
    if not rest.strip():
        raise GenomeError(f"{about}, which has no value.")
    if not rest[:1].isspace():
        raise GenomeError(f"{about}, which needs a space between the colon and its value ({field}: <value>).")
    if value is None:
        raise GenomeError(f"{about}, whose value {rest.strip()!r} isn't one plain word (letters, digits, dot, "
                          f"underscore and hyphen, no quotes, at most an inline # comment after it).")
    return value


def front_matter_written(value):
    """How a value is written into a front-matter field so that front_matter_text_value reads back the same value
    of the same type, or None when it can't be."""
    if is_number(value):
        token = shown(value)
    elif isinstance(value, str):
        token = value
    else:
        return None
    back = front_matter_token(token)
    return token if back is not None and type(back) is type(value) and back == value else None


def with_front_matter_value(text, rel, field, value, why):
    """The markdown text with one front-matter field's value replaced in place: the line becomes `field: value`
    (one space after the colon), an inline ` # comment` on it and its line ending are kept, and every other line
    stays as it was."""
    written = front_matter_written(value)
    if written is None:
        raise GenomeError(f"{why} is {shown(value)}, which can't be written into the front-matter field {field} of "
                          f"{rel} (one plain word or number, without quotes).")
    n, lines = front_matter_line(text, rel, field, why)
    line = lines[n]
    bare = line.splitlines()[0]
    comment = re.search(r" +#.*$", bare[len(field) + 1:])
    lines[n] = f"{field}: {written}{comment.group(0) if comment else ''}{line[len(bare):]}"
    return "".join(lines)


def json_value_span(text, key):
    """Where the value at a dotted key lies in a JSON text, as (start, end), or None when the text has no such key.
    Like json.loads, the last of duplicate keys counts."""
    decoder, ws = json.JSONDecoder(), " \t\n\r"

    def skip(i):
        while i < len(text) and text[i] in ws:
            i += 1
        return i

    i, span = skip(0), None
    for depth, part in enumerate(key.split(".")):
        if depth and span is None:
            return None
        if depth:
            i = span[0]
        if i >= len(text) or text[i] != "{":
            return None
        i, span = skip(i + 1), None
        while i < len(text) and text[i] != "}":
            if text[i] != '"':
                return None
            name, i = json.decoder.scanstring(text, i + 1)
            i = skip(i)
            if i >= len(text) or text[i] != ":":
                return None
            i = skip(i + 1)
            _value, end = decoder.raw_decode(text, i)
            if name == part:
                span = (i, end)
            i = skip(end)
            if i < len(text) and text[i] == ",":
                i = skip(i + 1)
    return span


def with_json_value(text, rel, key, value, why):
    """The JSON text with the value at one dotted key replaced in place, every other character left as it was."""
    try:
        json.loads(text)
        span = json_value_span(text, key)
    except ValueError as e:
        raise GenomeError(f"{why} is written into {rel}, which isn't valid JSON: {e}.")
    if span is None:
        raise GenomeError(f"{why} is written into the key {key} of {rel}, which has no {key}.")
    try:
        written = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise GenomeError(f"{why} is {value!r}, which can't be written as JSON into {rel}.")
    return text[:span[0]] + written + text[span[1]:]


def read_traits(profile, source):
    traits = {}
    for name, spec in profile["traits"].items():
        why = f"The trait {name}"
        if "json" in spec:
            traits[name] = json_value(source, spec["json"], spec["key"], why)
        else:
            traits[name] = front_matter_value(source, spec["front_matter"], spec["field"], why)
    return traits


def shown(value):
    """A trait value in a message: strings quoted, so "3" and 3 look different. A value JSON can't hold (bytes,
    a set, any other object) is shown as Python writes it."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def on_step(spec, value):
    """Whether a number lies in [min, max] and is min plus a whole number of steps (with a little slack for
    floats)."""
    if value < spec["min"] or value > spec["max"]:
        return False
    if all(isinstance(v, int) for v in (value, spec["min"], spec["step"])):
        return (value - spec["min"]) % spec["step"] == 0  # exact, and no overflow for very large whole numbers
    steps = (value - spec["min"]) / spec["step"]
    return abs(steps - round(steps)) <= 1e-9 * max(1.0, abs(steps))


def check_traits(profile, traits, project_values=None):
    """Every problem of a set of trait values against the profile, as plain sentences naming the trait at fault
    (an empty list when there is none). project_values ({trait: the project's own value}, see project_values) adds
    the rule that a value may never be looser than the project's."""
    problems, specs, project_values = [], profile["traits"], project_values or {}
    if not isinstance(traits, dict):
        return ["The traits must be an object of trait name to value."]
    for name in traits:
        if name not in specs:
            problems.append(f"The trait {name} isn't one the profile {profile['name']} defines.")
    for name, spec in specs.items():
        if name not in traits:
            problems.append(f"The trait {name} is missing; the profile {profile['name']} defines it.")
            continue
        value = traits[name]
        if "choices" in spec:
            if not any(type(value) is type(c) and value == c for c in spec["choices"]):
                problems.append(f"The trait {name} is {shown(value)}, which isn't one of its choices "
                                f"({', '.join(shown(c) for c in spec['choices'])}).")
            continue
        if not is_number(value):
            problems.append(f"The trait {name} is {shown(value)}, but it must be a finite number.")
            continue
        if not on_step(spec, value):
            problems.append(f"The trait {name} is {shown(value)}, but it must be {describe_range(spec)}.")
        if name in project_values and "looser" in spec:
            own = project_values[name]
            if not is_number(own):
                problems.append(f"The project's own value of the trait {name} is {shown(own)}, which isn't a finite "
                                f"number, so no value can be checked against it.")
            elif (value > own) if spec["looser"] == "higher" else (value < own):
                problems.append(f"The trait {name} is {shown(value)}, looser than the project's own {shown(own)}: it "
                                f"may not be {spec['looser']} than that.")
    for left, op, right in profile.get("relations", []):
        a, b = traits.get(left), traits.get(right)
        if is_number(a) and is_number(b) and not RELATIONS[op](a, b):
            problems.append(f"The trait {left} is {shown(a)} and the trait {right} is {shown(b)}, but the profile "
                            f"requires {left} {op} {right}.")
    return problems


def describe_range(spec):
    """A trait's range in words, for example `45 to 60 in steps of 5` or `one of "red" or "blue"`."""
    if "choices" in spec:
        choices = [shown(c) for c in spec["choices"]]
        if len(choices) == 1:
            return f"only {choices[0]}"
        return f"one of {', '.join(choices[:-1])} or {choices[-1]}"
    return f"{shown(spec['min'])} to {shown(spec['max'])} in steps of {shown(spec['step'])}"


def describe_bounds(spec):
    """A trait's range in words, then the rule about the project's own value when it has one."""
    text = describe_range(spec)
    if "looser" in spec:
        files = [s["json"] for s in spec["project"]]
        text += f"; never {spec['looser']} than the project's own value, read from {' or else '.join(files)}"
    return text


def project_values(profile, project_folder):
    """The project's own value of each trait that has project sources: {trait: value} from the first source file
    that exists and has the key. Traits without project sources, or with none found, are left out."""
    values = {}
    for name, spec in profile["traits"].items():
        for src in spec.get("project", []):
            path = os.path.join(project_folder, *src["json"].split("/"))
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    value = json.load(f)
            except (OSError, ValueError) as e:
                raise GenomeError(f"The trait {name} reads the project's own value from {src['json']} in "
                                  f"{project_folder}, which can't be read as JSON: "
                                  f"{getattr(e, 'strerror', None) or e}.")
            for part in src["key"].split("."):
                value = value.get(part, MISSING) if isinstance(value, dict) else MISSING
            if value is not MISSING:
                if isinstance(value, float) and not math.isfinite(value):
                    raise GenomeError(f"The trait {name} reads the project's own value from the key {src['key']} of "
                                      f"{src['json']} in {project_folder}, which is {shown(value)}; it must be a "
                                      f"finite number.")
                values[name] = value
                break
    return values


def mutate_trait(profile, traits, name, rng):
    """A copy of traits where only `name` has changed: a number moves one step up or down, staying inside its
    bounds; a choice becomes a different choice. A move that would break one of the profile's relations isn't
    taken. rng is a random.Random. Raises GenomeError when the profile doesn't define the trait or no other legal
    value is left."""
    spec = profile["traits"].get(name)
    if spec is None:
        raise GenomeError(f"The trait {name} isn't one the profile {profile['name']} defines, so it can't be mutated.")
    value = traits.get(name)
    if "choices" in spec:
        options = [c for c in spec["choices"] if not (type(c) is type(value) and c == value)]
    elif is_number(value):
        options = []
        for move in (-1, 1):
            steps = round((value - spec["min"]) / spec["step"]) + move
            new = spec["min"] + steps * spec["step"]
            new = round(new, 12) if isinstance(new, float) else new  # 0.1 + 2 * 0.1 is 0.30000000000000004
            if on_step(spec, new) and on_step(spec, value):
                options.append(new)
    else:
        options = []
    relations = [r for r in profile.get("relations", []) if name in (r[0], r[2])]
    legal = []
    for option in options:
        child = {**traits, name: option}
        if all(not (is_number(child.get(a)) and is_number(child.get(b))) or RELATIONS[op](child[a], child[b])
               for a, op, b in relations):
            legal.append(option)
    if not legal:
        within = ", within the profile's relations" if relations else ""
        raise GenomeError(f"The trait {name} is {shown(value)} and has no other legal value ({describe_range(spec)}"
                          f"{within}), so it can't be mutated.")
    return {**traits, name: rng.choice(legal)}


def target_version(profile, source):
    spec = profile["version"]
    if spec is None:
        return None
    return json_value(source, spec["file"], spec["key"], f"The profile {profile['name']}'s version")


def source_commit(source):
    """The source folder's git HEAD when the folder is the top of a git working tree, else None."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        top = subprocess.run(["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"], cwd=source,
                             capture_output=True, text=True, env=env, timeout=30)
        if top.returncode != 0 or not top.stdout.strip() or not os.path.samefile(top.stdout.strip(), source):
            return None  # not a git working tree, or a folder inside one: its HEAD isn't the source's own
        head = subprocess.run(["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "-q", "HEAD"],
                              cwd=source, capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    commit = head.stdout.strip()
    return commit if head.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", commit) else None


# ---------------------------------------------------------------- storage

def genome_dir(home, genome_id):
    if not safe_id(genome_id):
        raise GenomeError(f"The genome id {genome_id!r} can't be used as a folder name: use letters, digits, "
                          f"_ . and -, starting with a letter or digit.")
    return os.path.join(home, "genomes", genome_id)


def seed(home, profile, source, genome_id):
    """Create genomes/<id>/ from the source: genome.json, files/ (the evolvable files) and the expanded plugin/.

    Everything is read and checked before anything is written, including the source's own trait values against the
    profile's bounds and relations; the genome is built in a temporary folder beside the others and renamed into
    place, so a failure leaves no genome behind. Returns the genome's folder."""
    folder = genome_dir(home, genome_id)
    if os.path.lexists(folder):
        raise GenomeError(f"The genome {genome_id} already exists ({folder}); pick another id, or delete that "
                          f"folder to seed it again.")
    source = os.path.abspath(source)
    evolvable, _frozen = source_files(profile, source)
    if not evolvable:
        raise GenomeError(f"{source} has no file the profile {profile['name']} calls evolvable "
                          f"({', '.join(profile['evolvable'])}), so the genome would be empty.")
    problems = never_problems(profile["never"], evolvable, "evolvable")
    if problems:
        raise GenomeError(f"The source {source} has evolvable files that the profile {profile['name']} says no "
                          f"genome may ever hold, so nothing was written: {' '.join(problems)}")
    traits = read_traits(profile, source)
    problems = check_traits(profile, traits)
    if problems:
        raise GenomeError(f"The source {source} has trait values the profile {profile['name']} doesn't allow, so "
                          f"its maintainer's config and the profile disagree: {' '.join(problems)}")
    version = target_version(profile, source)
    contents = {}
    for rel in evolvable:
        try:
            contents[rel] = read_bytes(os.path.join(source, *rel.split("/")))
        except OSError as e:
            raise GenomeError(f"Can't read {rel} in {source}: {e.strerror or e}.")
    genome = new_genome(genome_id, profile, [], {"target_version": version, "commit": source_commit(source)},
                        contents, traits)
    modes = {rel: os.path.join(source, *rel.split("/")) for rel in contents}
    return store_genome(home, profile, source, genome, contents, modes, "seeded")


def make_child(home, profile, source, child_id, parents, files=None, traits=None, project_values=None,
               records=None, check=None):
    """Create genomes/<child_id>/ from its parent(s), like seed: genome.json (with the parents), files/ and plugin/.

    The files start as the first parent's, and `files` ({path: bytes}) replaces some of them; the traits start as
    the first parent's, and `traits` ({name: value}) replaces some of them. A trait whose source is one of the
    genome's files has its value in that file: one set through `traits` is written into it (after `files`, so
    `traits` wins), and genome.json records what the child's file then says (see traits_in_files).

    A candidate is refused with every problem in one GenomeError, and nothing is written, when check_traits finds a
    problem (given project_values, the project's own values), when `files` names a path the child may not set (see
    candidate_path_problem: absolute, escaping, never, not evolvable, not in the first parent) or when a file is over
    one of the profile's caps (see cap_problems). A child whose judge files differ from its root seed's copies is
    refused first, on that alone (see judge_problems). records ({file name: JSON data}) are extra records written beside
    genome.json in the same temporary folder, such as mutation.json, so the child appears with them or not at all.
    check, when given, is called with the child's final files ({path: bytes}) and traits once every rule above has
    passed and before anything is written; whatever it raises stops the child (refuse_near_duplicate is one).
    Returns the child's folder."""
    folder = genome_dir(home, child_id)
    if os.path.lexists(folder):
        raise GenomeError(f"The genome {child_id} already exists ({folder}); pick another id for the child.")
    if not isinstance(parents, list) or not parents or not all(isinstance(p, str) for p in parents):
        raise GenomeError(f"The genome {child_id} needs a list of one or more parent ids.")
    if len(set(parents)) != len(parents) or child_id in parents:
        raise GenomeError(f"The genome {child_id} lists a parent twice, or itself, among {', '.join(parents)}.")
    loaded = []
    for parent in parents:
        made_with = read_genome(home, parent).get("profile")
        if made_with != profile["name"]:
            raise GenomeError(f"The parent {parent} was made with the profile {made_with}, not {profile['name']}; a "
                              f"child's parents share its profile.")
        loaded.append(load_genome(home, parent, profile))
    first, stored = loaded[0], os.path.join(genome_dir(home, parents[0]), "files")
    contents, modes = {}, {}
    for entry in first["files"]:
        path = os.path.join(stored, *entry["path"].split("/"))
        try:
            contents[entry["path"]] = read_bytes(path)
        except OSError as e:
            raise GenomeError(f"Can't read files/{entry['path']} of the genome {parents[0]}: {e.strerror or e}.")
        modes[entry["path"]] = path
    problems = []
    if files is not None and not isinstance(files, dict):
        problems.append("The child's files must be an object of path to bytes.")
        files = {}
    for rel, data in (files or {}).items():
        problem = candidate_path_problem(profile, rel, contents, parents[0])
        if problem:
            problems.append(problem)
        elif not isinstance(data, bytes):
            problems.append(f"The new content of {rel} must be bytes.")
        else:
            contents[rel] = data
    if traits is not None and not isinstance(traits, dict):
        problems.append("The child's traits must be an object of trait name to value.")
        traits = {}
    child_traits = {**first["traits"], **(traits or {})}
    problems += traits_in_files(profile, contents, traits or {}, child_traits)
    judged = judge_problems(home, profile, parents[0], contents)
    if judged:
        raise GenomeError(f"The candidate {child_id} was refused, and nothing was written: {' '.join(judged)}")
    problems += check_traits(profile, child_traits, project_values)
    problems += cap_problems(home, profile, parents[0], contents)
    for name in records or {}:
        if name in RESERVED_RECORDS or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", str(name)):
            problems.append(f"The record {name!r} can't be written beside genome.json.")
    if problems:
        raise GenomeError(f"The candidate {child_id} was refused, and nothing was written: {' '.join(problems)}")
    if check is not None:
        check(dict(contents), dict(child_traits))
    genome = new_genome(child_id, profile, list(parents), first.get("base"), contents, child_traits)
    return store_genome(home, profile, os.path.abspath(source), genome, contents, modes, "made", records)


def candidate_path_problem(profile, rel, contents, parent):
    """Why a candidate may not set the file rel (a plain sentence naming it as written), or None when it may: rel
    must be a relative path inside the genome's folder, match no never glob, be evolvable under the profile and be
    one of the first parent's files (contents)."""
    if not isinstance(rel, str):
        return f"The path {rel!r} isn't a string; a candidate names its files by their relative paths."
    if not safe_path(rel):
        return (f"The path {rel} is absolute or leads out of the genome's folder; a candidate may only replace its "
                f"parent's files, by their paths relative to the folder.")
    glob = never_glob(profile["never"], rel)
    if glob is not None:
        return f"The path {rel} matches the never glob {glob} of the profile {profile['name']}: no genome may change it."
    if not matcher(profile["evolvable"])(rel) or matcher(profile["exclude"])(rel):
        return f"The path {rel} isn't one of the files the profile {profile['name']} lets a genome change."
    if rel not in contents:
        return f"The file {rel} isn't one of the parent {parent}'s files, so the child can't add it."
    return None


def root_seed(home, genome_id, profile):
    """The root seed of a genome: the ancestor with no parents, found by following each first parent, loaded and
    checked. A missing ancestor or a loop of parents is a GenomeError."""
    seen, current = [genome_id], read_genome(home, genome_id)
    while current["parents"]:
        parent = current["parents"][0]
        if not isinstance(parent, str) or parent in seen:
            raise GenomeError(f"The genome {seen[-1]} lists the parent {parent!r}, which loops back through "
                              f"{', '.join(seen)}; its root seed can't be found.")
        try:
            current = read_genome(home, parent)
        except GenomeError as e:
            raise GenomeError(f"The genome {seen[-1]} has the parent {parent}, which can't be read, so its root seed "
                              f"can't be found: {e}")
        seen.append(parent)
    return load_genome(home, seen[-1], profile)


def cap_problems(home, profile, parent, contents):
    """A plain problem for each of the child's files (contents, {path: bytes}) over a cap of the profile, naming the
    file, its size and its cap. Growth is measured against the same path in the root seed of the parent."""
    problems, root = [], None
    for rel, data in sorted(contents.items()):
        size = len(data)
        for cap in profile["caps"]:
            if not glob_regex(cap["glob"]).match(rel):
                continue
            if "max_bytes" in cap and size > cap["max_bytes"]:
                problems.append(f"{rel} is {size} bytes, over its cap of {shown(cap['max_bytes'])} bytes (the cap on "
                                f"{cap['glob']}).")
            if "max_growth_pct" not in cap:
                continue
            if root is None:
                try:
                    root = root_seed(home, parent, profile)
                except GenomeError as e:
                    problems.append(f"The growth of {rel} can't be measured: {e}")
                    return problems
            sizes = {entry["path"]: entry["bytes"] for entry in root["files"]}
            pct = cap["max_growth_pct"]
            if rel not in sizes:
                problems.append(f"{rel} has no copy in the root seed {root['id']}, so its growth can't be measured "
                                f"against the cap on {cap['glob']} ({shown(pct)}% at most).")
                continue
            allowed = fractions.Fraction(sizes[rel]) * (100 + fractions.Fraction(pct)) / 100
            if size > allowed:
                problems.append(f"{rel} is {size} bytes, over its cap: at most {shown(pct)}% over the {sizes[rel]} "
                                f"bytes of the root seed {root['id']}, so at most {int(allowed)} bytes (the cap on "
                                f"{cap['glob']}).")
    return problems


def describe_cap(cap):
    """A cap in words, for example `at most 50% over the root seed's copy` or `at most 300 bytes`."""
    parts = []
    if "max_growth_pct" in cap:
        parts.append(f"at most {shown(cap['max_growth_pct'])}% over the root seed's copy")
    if "max_bytes" in cap:
        parts.append(f"at most {shown(cap['max_bytes'])} bytes")
    return " and ".join(parts)


def traits_in_files(profile, contents, chosen, traits):
    """Bring the traits whose source is one of the genome's own files in line with those files, and return the
    problems found (plain sentences naming the trait).

    contents ({path: bytes}, the child's final files) and traits ({name: value}) are updated in place. A trait set
    in chosen is first written into its file, changing only its front-matter line or JSON value; then every such
    trait takes the value its file holds, so genome.json records what the file says. Traits from frozen files keep
    their value: they live only in genome.json."""
    problems = []
    for name, spec in profile["traits"].items():
        rel = spec.get("json", spec.get("front_matter"))
        if rel not in contents:
            continue
        why = f"The trait {name}"
        try:
            text = contents[rel].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{why} is read from {rel}, which isn't UTF-8 text.")
            continue
        try:
            if name in chosen:
                if "json" in spec:
                    text = with_json_value(text, rel, spec["key"], chosen[name], why)
                else:
                    text = with_front_matter_value(text, rel, spec["field"], chosen[name], why)
                contents[rel] = text.encode("utf-8")
            if "json" in spec:
                traits[name] = json_text_value(text, rel, spec["key"], why)
            else:
                traits[name] = front_matter_text_value(text, rel, spec["field"], why)
        except GenomeError as e:
            problems.append(str(e))
    return problems


def file_trait_sources(profile, contents):
    """{trait: its source} for the traits whose source is one of the genome's files (contents, {path: bytes}). Kept
    in sources.json beside genome.json so load_genome can check those traits without the profile; it records where
    a value is read, never a bound."""
    return {name: {k: spec[k] for k in (("json", "key") if "json" in spec else ("front_matter", "field"))}
            for name, spec in profile["traits"].items() if spec.get("json", spec.get("front_matter")) in contents}


def new_genome(genome_id, profile, parents, base, contents, traits):
    """A genome.json record: exactly the keys in GENOME_KEYS."""
    return {
        "id": genome_id, "version": GENOME_FORMAT, "profile": profile["name"], "parents": parents,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base": base,
        "files": [{"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)}
                  for rel, data in sorted(contents.items())],
        "traits": traits,
    }


def store_genome(home, profile, source, genome, contents, modes, how, records=None):
    """Write genomes/<id>/ (files/, genome.json, any extra records ({file name: JSON data}) and the expanded plugin/)
    through a temporary folder and a rename. modes maps each file to the file whose permissions it takes. Returns the
    genome's folder."""
    genome_id = genome["id"]
    folder = genome_dir(home, genome_id)
    parent = os.path.dirname(folder)
    try:
        os.makedirs(parent, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".new-{genome_id}-", dir=parent)
    except OSError as e:
        raise GenomeError(f"Can't create the genome's folder in {parent}: {e.strerror or e}.")
    try:
        for rel, data in contents.items():
            path = os.path.join(tmp, "files", *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            shutil.copymode(modes[rel], path)
        write_json(os.path.join(tmp, "genome.json"), genome)
        write_json(os.path.join(tmp, SOURCES_FILE), file_trait_sources(profile, contents))
        write_json(os.path.join(tmp, LIMITS_FILE), {"never": profile["never"], "caps": profile["caps"]})
        for name, data in (records or {}).items():
            write_json(os.path.join(tmp, name), data)
        build_plugin(tmp, os.path.join(tmp, "files"), genome, profile, source)
        if os.path.lexists(folder):
            raise GenomeError(f"The genome {genome_id} appeared while it was being {how} ({folder}); it was left "
                              f"as it is.")
        os.rename(tmp, folder)
    except OSError as e:
        remove_tree(tmp)
        raise GenomeError(f"Can't write the genome {genome_id}: {e.strerror or e}"
                          f"{f' ({e.filename})' if e.filename else ''}.")
    except BaseException:
        remove_tree(tmp)
        raise
    return folder


def expand(home, genome_id, profile, source):
    """(Re)build genomes/<id>/plugin/: the source's frozen layer as it is now, plus the genome's files.

    The new folder is built and checked beside the old one and then swapped in, so a failure part-way leaves the
    previous plugin/ (or none) as it was. Returns the plugin folder."""
    made_with = read_genome(home, genome_id).get("profile")
    if made_with != profile["name"]:
        raise GenomeError(f"The genome {genome_id} was seeded with the profile {made_with}, not "
                          f"{profile['name']}; expand it with its own profile.")
    genome = load_genome(home, genome_id, profile)
    folder = genome_dir(home, genome_id)
    plugin, hashes = os.path.join(folder, "plugin"), os.path.join(folder, PLUGIN_HASHES)
    try:
        build = tempfile.mkdtemp(prefix=".plugin-new-", dir=folder)
    except OSError as e:
        raise GenomeError(f"Can't create a folder in {folder}: {e.strerror or e}.")
    try:
        build_plugin(build, os.path.join(folder, "files"), genome, profile, os.path.abspath(source))
        old = None
        if os.path.lexists(plugin):
            old = os.path.join(folder, f".plugin-old-{os.path.basename(build)[len('.plugin-new-'):]}")
            os.rename(plugin, old)
        try:
            os.rename(os.path.join(build, "plugin"), plugin)
            os.replace(os.path.join(build, PLUGIN_HASHES), hashes)  # the new plugin's hashes, after it is in place
        except BaseException:
            if os.path.lexists(plugin) and not os.path.lexists(os.path.join(build, "plugin")):
                os.rename(plugin, os.path.join(build, "plugin"))
            if old:
                os.rename(old, plugin)
            raise
    except OSError as e:
        remove_tree(build)
        raise GenomeError(f"Can't expand the genome {genome_id}: {e.strerror or e}"
                          f"{f' ({e.filename})' if e.filename else ''}; its plugin folder was left as it was.")
    except BaseException:
        remove_tree(build)
        raise
    remove_tree(build)
    if old:
        remove_tree(old)
    return plugin


def build_plugin(folder, stored, genome, profile, source):
    """Write folder/plugin/: the source's frozen layer, then the genome's files from stored, and check it; then
    folder/plugin.sha256.json, every file of plugin/ with its sha256.

    A genome file the profile doesn't call evolvable, that it excludes or that matches a never glob is refused: a
    genome never replaces a frozen file."""
    evolvable, excluded, never = matcher(profile["evolvable"]), matcher(profile["exclude"]), matcher(profile["never"])
    for entry in genome["files"]:
        if not evolvable(entry["path"]) or excluded(entry["path"]) or never(entry["path"]):
            raise GenomeError(f"The genome {genome['id']} holds {entry['path']}, which the profile "
                              f"{profile['name']} doesn't let a genome change.")
    _evolvable, frozen = source_files(profile, source)
    plugin = os.path.join(folder, "plugin")
    os.makedirs(plugin)
    for rel in frozen:
        copy_file(os.path.join(source, *rel.split("/")), plugin, rel, f"{rel} from {source}")
    for entry in genome["files"]:
        copy_file(os.path.join(stored, *entry["path"].split("/")), plugin, entry["path"],
                  f"the genome's {entry['path']}")
    check_files(genome, plugin, "plugin/")
    hashes = {}
    for rel, path in folder_files(plugin):
        if os.path.islink(path) or not os.path.isfile(path):
            raise GenomeError(f"plugin/{rel} of the genome {genome['id']} isn't a plain file.")
        hashes[rel] = sha256_bytes(read_bytes(path))
    write_json(os.path.join(folder, PLUGIN_HASHES), {"files": hashes})


def folder_files(root):
    """Every entry under root that isn't a folder (files, links, a link to a folder included), as sorted
    (relative path, full path) pairs."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in filenames + links:
            found.append((prefix + name, os.path.join(dirpath, name)))
    return sorted(found)


def copy_file(src, plugin, rel, what):
    dest = os.path.join(plugin, *rel.split("/"))
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)
    except OSError as e:
        raise GenomeError(f"Can't copy {what}: {e.strerror or e}.")


# ---------------------------------------------------------------- checks

def read_genome(home, genome_id):
    """genome.json of a genome, with its shape checked but its files not (see load_genome). A genome.json with any
    key seed doesn't write is refused, so a genome can't carry bounds of its own."""
    folder = genome_dir(home, genome_id)
    path = os.path.join(folder, "genome.json")
    if not os.path.isdir(folder) or os.path.islink(folder) or not os.path.isfile(path):
        raise GenomeError(f"There is no genome {genome_id}: {path} doesn't exist.")
    try:
        with open(path, encoding="utf-8") as f:
            genome = json.load(f)
    except (OSError, ValueError) as e:
        raise GenomeError(f"Can't read genomes/{genome_id}/genome.json: {getattr(e, 'strerror', None) or e}.")
    extra = sorted(set(genome) - GENOME_KEYS) if isinstance(genome, dict) else []
    if extra:
        raise GenomeError(f"genomes/{genome_id}/genome.json has keys a genome doesn't hold ({', '.join(extra)}); a "
                          f"genome holds only {', '.join(sorted(GENOME_KEYS))}, and its traits' bounds come from the "
                          f"profile alone.")
    files = genome.get("files") if isinstance(genome, dict) else None
    if not isinstance(files, list) or not isinstance(genome.get("traits"), dict) or genome.get("id") != genome_id:
        raise GenomeError(f"genomes/{genome_id}/genome.json isn't a genome's record (it needs this id, a files list "
                          f"and a traits object).")
    seen = set()
    for entry in files:
        ok = (isinstance(entry, dict) and set(entry) == FILE_KEYS and safe_path(entry.get("path"))
              and isinstance(entry.get("sha256"), str) and isinstance(entry.get("bytes"), int) and not isinstance(entry.get("bytes"), bool))
        if not ok or entry["path"] in seen:
            raise GenomeError(f"genomes/{genome_id}/genome.json lists a file wrongly or twice: {entry!r}.")
        seen.add(entry["path"])
    return genome


def check_files(genome, root, where):
    """Each file genome.json lists is a plain file under root with its recorded size and sha256."""
    top = os.path.realpath(root)
    if not os.path.isdir(root) or os.path.islink(root):
        raise GenomeError(f"The genome {genome['id']} has no {where} folder.")
    for entry in genome["files"]:
        rel = entry["path"]
        path = os.path.join(root, *rel.split("/"))
        named = f"{where}{rel} of the genome {genome['id']}"
        if not os.path.realpath(path).startswith(top + os.sep) or os.path.islink(path):
            raise GenomeError(f"{named} is a symlink or leads outside {where}; it doesn't match genome.json.")
        if not os.path.isfile(path):
            raise GenomeError(f"{named} is missing, but genome.json lists it.")
        try:
            data = read_bytes(path)
        except OSError as e:
            raise GenomeError(f"Can't read {named}: {e.strerror or e}.")
        if len(data) != entry["bytes"]:
            raise GenomeError(f"{named} has {len(data)} bytes, but genome.json records {entry['bytes']}: it was "
                              f"changed after the genome was written.")
        if sha256_bytes(data) != entry["sha256"]:
            raise GenomeError(f"{named} doesn't match its sha256 in genome.json: it was changed after the genome "
                              f"was written.")


def check_file_traits(home, genome, stored):
    """Each trait held in one of the genome's files (as sources.json records) has in genome.json the value its file
    in stored says."""
    genome_id = genome["id"]
    path = os.path.join(genome_dir(home, genome_id), SOURCES_FILE)
    named = f"genomes/{genome_id}/{SOURCES_FILE}"
    try:
        with open(path, encoding="utf-8") as f:
            sources = json.load(f)
    except FileNotFoundError:
        raise GenomeError(f"{named} is missing; it records which traits live in the genome's own files. Seed or make "
                          f"the genome again.")
    except (OSError, ValueError) as e:
        raise GenomeError(f"Can't read {named}: {getattr(e, 'strerror', None) or e}.")
    listed = {entry["path"] for entry in genome["files"]}
    if not isinstance(sources, dict):
        raise GenomeError(f"{named} must hold an object of trait name to its source.")
    for name, spec in sources.items():
        rel = spec.get("json", spec.get("front_matter")) if isinstance(spec, dict) else None
        shape = isinstance(spec, dict) and set(spec) in ({"json", "key"}, {"front_matter", "field"}) \
            and all(isinstance(v, str) and v for v in spec.values())
        if not shape or rel not in listed or name not in genome["traits"]:
            raise GenomeError(f"{named} records the trait {name} wrongly: it must be one of genome.json's traits, "
                              f"with a source in one of the genome's files.")
        why = f"The trait {name}"
        try:
            text = read_bytes(os.path.join(stored, *rel.split("/"))).decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise GenomeError(f"{why} is read from files/{rel} of the genome {genome_id}, which can't be read as "
                              f"UTF-8 text: {getattr(e, 'strerror', None) or e}.")
        if "json" in spec:
            value = json_text_value(text, rel, spec["key"], why)
        else:
            value = front_matter_text_value(text, rel, spec["field"], why)
        recorded = genome["traits"][name]
        if type(value) is not type(recorded) or value != recorded:
            raise GenomeError(f"genomes/{genome_id}/genome.json records the trait {name} as {shown(recorded)}, but "
                              f"the genome's {rel} says {shown(value)}: the two must agree, so genome.json was "
                              f"changed by hand. Put back {shown(value)} or make a new genome.")


def read_limits(home, genome_id):
    """genomes/<id>/limits.json, checked: {"never": [globs], "caps": [caps]}, as the profile had them."""
    named = f"genomes/{genome_id}/{LIMITS_FILE}"
    try:
        with open(os.path.join(genome_dir(home, genome_id), LIMITS_FILE), encoding="utf-8") as f:
            limits = json.load(f)
    except FileNotFoundError:
        raise GenomeError(f"{named} is missing; it records which paths no genome may hold. Seed or make the genome "
                          f"again.")
    except (OSError, ValueError) as e:
        raise GenomeError(f"Can't read {named}: {getattr(e, 'strerror', None) or e}.")
    never = limits.get("never") if isinstance(limits, dict) else None
    if not isinstance(limits, dict) or set(limits) != {"never", "caps"} or not isinstance(never, list) \
            or not all(isinstance(g, str) and safe_path(g) for g in never):
        raise GenomeError(f"{named} must hold only never (a list of globs) and caps, as the profile had them.")
    check_caps(named, limits["caps"])
    return limits


def path_problems(genome, limits, profile):
    """A plain problem for each file genome.json lists that matches a never glob (of limits.json, or of the
    profile when given) or, with a profile, isn't one of the files it lets a genome change."""
    problems, never = [], limits["never"] + (profile["never"] if profile else [])
    for entry in genome["files"]:
        rel, glob = entry["path"], never_glob(never, entry["path"])
        if glob is not None:
            problems.append(f"{rel} matches the never glob {glob}: no genome may hold it.")
        elif profile and (not matcher(profile["evolvable"])(rel) or matcher(profile["exclude"])(rel)):
            problems.append(f"{rel} isn't one of the files the profile {profile['name']} lets a genome change.")
    return problems


def load_genome(home, genome_id, profile=None):
    """genome.json, after checking that it lists no path a genome may not hold (the never globs in limits.json and,
    given the genome's profile, its never and evolvable globs), that files/ holds exactly the files it lists, each
    unchanged, and that every trait held in those files has the value its file says."""
    genome = read_genome(home, genome_id)
    if profile is not None and genome.get("profile") != profile["name"]:
        raise GenomeError(f"The genome {genome_id} was made with the profile {genome.get('profile')}, not "
                          f"{profile['name']}; load it with its own profile.")
    problems = path_problems(genome, read_limits(home, genome_id), profile)
    if problems:
        raise GenomeError(f"genomes/{genome_id}/genome.json lists files no genome may hold: {' '.join(problems)}")
    stored = os.path.join(genome_dir(home, genome_id), "files")
    check_files(genome, stored, "files/")
    listed = {entry["path"] for entry in genome["files"]}
    for dirpath, dirnames, filenames in os.walk(stored):
        rel_dir = os.path.relpath(dirpath, stored).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for name in sorted(filenames) + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            if prefix + name not in listed:
                raise GenomeError(f"files/{prefix}{name} of the genome {genome_id} isn't listed in genome.json; "
                                  f"remove it, or seed a new genome.")
    check_file_traits(home, genome, stored)
    return genome


def check_individual(home, genome_id, profile=None):
    """The genome's plugin/ folder, after loading the genome (see load_genome), checking that its evolvable files
    match genome.json and that the whole folder, frozen files included, matches plugin.sha256.json: no file extra,
    missing or changed."""
    genome = load_genome(home, genome_id, profile)
    folder = genome_dir(home, genome_id)
    plugin = os.path.join(folder, "plugin")
    check_files(genome, plugin, "plugin/")
    named = f"genomes/{genome_id}/{PLUGIN_HASHES}"
    try:
        with open(os.path.join(folder, PLUGIN_HASHES), encoding="utf-8") as f:
            recorded = json.load(f)
    except FileNotFoundError:
        raise GenomeError(f"{named} is missing; it records every file of the genome's plugin/. Expand the genome "
                          f"again.")
    except (OSError, ValueError) as e:
        raise GenomeError(f"Can't read {named}: {getattr(e, 'strerror', None) or e}.")
    hashes = recorded.get("files") if isinstance(recorded, dict) else None
    if not isinstance(hashes, dict) or not all(isinstance(v, str) for v in hashes.values()):
        raise GenomeError(f"{named} must hold the files of plugin/ with their sha256.")
    problems, found = [], {}
    for rel, path in folder_files(plugin):
        if os.path.islink(path) or not os.path.isfile(path):
            problems.append(f"plugin/{rel} isn't a plain file.")
        elif rel not in hashes:
            problems.append(f"plugin/{rel} is an extra file the genome's expansion didn't write.")
        else:
            found[rel] = path
    for rel, digest in sorted(hashes.items()):
        if rel not in found and not any(p.startswith(f"plugin/{rel} ") for p in problems):
            problems.append(f"plugin/{rel} is missing.")
        elif rel in found:
            try:
                same = sha256_bytes(read_bytes(found[rel])) == digest
            except OSError as e:
                problems.append(f"Can't read plugin/{rel}: {e.strerror or e}.")
                continue
            if not same:
                problems.append(f"plugin/{rel} was changed after the genome was expanded.")
    if problems:
        raise GenomeError(f"The plugin folder of the genome {genome_id} doesn't match what its expansion wrote, so it "
                          f"can't be used: {' '.join(problems)} Expand the genome again.")
    return plugin



# ---------------------------------------------------------------- aimed mutations: the reflection's prompt and reply

FAILURE_KEYS = ("id", "kind", "case", "summary")  # what a mutation record keeps of the failure it targets
MUTATION_FILE = "mutation.json"  # beside genome.json: how a child was made from its parent, and against what


def check_failure(failure):
    """A failure (from the evolver's list of a replay's failures) must have a non-empty string id, kind, case and
    summary, and its evidence, when given, must be a string. Raises GenomeError otherwise."""
    if not isinstance(failure, dict):
        raise GenomeError("A mutation needs the one failure it targets, as an object with an id, a kind, a case and "
                          "a summary.")
    missing = [k for k in FAILURE_KEYS if not (isinstance(failure.get(k), str) and failure[k].strip())]
    if missing:
        raise GenomeError(f"The failure a mutation targets has no {', '.join(missing)}; it needs an id, a kind, a "
                          f"case and a summary.")
    if not isinstance(failure.get("evidence", ""), str):
        raise GenomeError(f"The evidence of the failure {failure['id']} must be text.")


def file_caps(home, profile, parent, rel):
    """The size caps that apply to the file rel of a child of parent, as plain sentences with the concrete numbers
    (for a growth cap, the most bytes it allows over the root seed's copy). [] when no cap matches."""
    sentences, root = [], None
    for cap in profile["caps"]:
        if not glob_regex(cap["glob"]).match(rel):
            continue
        if "max_bytes" in cap:
            sentences.append(f"at most {shown(cap['max_bytes'])} bytes (the cap on {cap['glob']})")
        if "max_growth_pct" in cap:
            root = root if root is not None else root_seed(home, parent, profile)
            sizes = {entry["path"]: entry["bytes"] for entry in root["files"]}
            pct = cap["max_growth_pct"]
            if rel in sizes:
                allowed = int(fractions.Fraction(sizes[rel]) * (100 + fractions.Fraction(pct)) / 100)
                sentences.append(f"at most {shown(pct)}% over the {sizes[rel]} bytes of the first version of this "
                                 f"file, so at most {allowed} bytes (the cap on {cap['glob']})")
            else:
                sentences.append(f"at most {shown(pct)}% over the first version of this file (the cap on "
                                 f"{cap['glob']})")
    return sentences


def reflection_prompt(profile, target, text, failure, caps):
    """The prompt for one reflection: why one run failed, and a rewrite of one file (target, whose current text is
    text) against that one failure. failure holds the id, kind, case, summary and evidence; caps are the plain
    sentences file_caps gives for the target. The answer asked for is one JSON object {"diagnosis", "text"}."""
    size = len(text.encode("utf-8"))
    limit = (" and ".join(caps) + ". A rewrite over this limit is rejected.") if caps else \
        "No size cap applies to this file, but keep it about as long as it is."
    evidence = (failure.get("evidence") or "").strip() or "(none recorded)"
    describe = (profile.get("describe") or "").strip().rstrip(".")
    what = (f"You are improving one file of {profile['name']}, {describe}." if describe else
            f"You are improving one file of the instructions of {profile['name']}.")
    return "\n".join([
        f"{what} A run that used this file failed in the one way described below.",
        "",
        "Do two things:",
        "1. Explain in plain language, in two to five sentences, why the run failed and what in the file let it "
        "happen (or failed to prevent it).",
        "2. Rewrite the file so that this one failure is less likely next time. Change as little else as possible: "
        "keep its structure, its front matter (the lines between the --- markers at the top, if any) and every "
        "instruction that has nothing to do with this failure. Don't try to fix other problems.",
        "",
        f"The failure (kind {failure['kind']}, id {failure['id']}, in the case {failure['case']}):",
        failure["summary"],
        "",
        "Its evidence:",
        "<<<EVIDENCE",
        evidence,
        "EVIDENCE>>>",
        "",
        f"The size limit for {target}: {limit} It is {size} bytes now.",
        "",
        f"The current text of {target}:",
        "<<<FILE",
        text,
        "FILE>>>",
        "",
        "Answer with one JSON object and nothing else (a ```json block around it is fine):",
        '{"diagnosis": "<why the run failed, in plain language>", "text": "<the whole new file>"}',
        "The text must be the complete new file, not a diff or an excerpt.",
    ])


FENCED_BLOCK = re.compile(r"```[A-Za-z]*[ \t]*\n(.*?)\n[ \t]*```", re.S)


def parse_reflection(reply):
    """(diagnosis, text) from the reflection's answer: one JSON object {"diagnosis": str, "text": str}, alone or in
    a ```json block, or failing those the first JSON object in the answer that has either key. Raises GenomeError
    saying what is wrong: no JSON, broken JSON, or a missing or empty field."""
    if not isinstance(reply, str) or not reply.strip():
        raise GenomeError("The reflection answered with nothing, so there is no JSON with a diagnosis and a text.")
    candidates, broken = [], None
    for chunk in [reply.strip()] + [m.group(1) for m in FENCED_BLOCK.finditer(reply)]:
        try:
            candidates.append(json.loads(chunk))
        except ValueError as e:
            if chunk is not reply.strip():
                broken = broken or str(e)
    found = next((c for c in candidates if isinstance(c, dict)), None)
    if found is None:
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", reply):
            try:
                value, _end = decoder.raw_decode(reply, m.start())
            except ValueError:
                continue
            if isinstance(value, dict) and ("diagnosis" in value or "text" in value):
                found = value
                break
    if found is None:
        if broken:
            raise GenomeError(f"The reflection's ```json block isn't valid JSON ({broken}), so no child was made.")
        raise GenomeError("The reflection's answer holds no JSON object with a diagnosis and a text, so no child "
                          "was made.")
    problems = [f'"{k}"' for k in ("diagnosis", "text") if not (isinstance(found.get(k), str) and found[k].strip())]
    if problems:
        raise GenomeError(f"The reflection's JSON answer has no {' or '.join(problems)} string (it needs a "
                          f"diagnosis and the whole new text), so no child was made.")
    return found["diagnosis"].strip(), found["text"]


def mutation_record(parent, kind, failure, created_at=None, **extra):
    """genomes/<child>/mutation.json: the parent, the kind of mutation (rewrite or trait), the failure it targets
    (its id, kind, case and summary) and the rest given (target or trait, diagnosis, model, tokens)."""
    return {"parent": parent, "kind": kind, **extra, "failure": {k: failure[k] for k in FAILURE_KEYS},
            "created_at": created_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- choosing a parent

def parent_weights(entries, sharpness=10, midpoint=0.5):
    """{id: probability of being picked as the next parent} for entries, a list of {"id", "score" (0 to 1),
    "children" (a whole number)}: each weight is sigmoid(sharpness * (score - midpoint)) / (1 + children), so it rises
    with the score and falls with the children already made, and the weights are scaled to sum to 1 (as in the
    Darwin Godel Machine). An empty list, a score outside 0 to 1 or a negative child count is a GenomeError."""
    if not isinstance(entries, list) or not entries:
        raise GenomeError("There is no candidate parent to pick from: every genome needs a score from a finished "
                          "run first.")
    if not (is_number(sharpness) and is_number(midpoint)):
        raise GenomeError("The sharpness and midpoint of the parent weights must be finite numbers.")
    raw = {}
    for entry in entries:
        gid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(gid, str) or not gid:
            raise GenomeError(f"A candidate parent needs an id, a score and a child count: {entry!r}.")
        score, kids = entry.get("score"), entry.get("children")
        if not is_number(score) or not 0 <= score <= 1:
            raise GenomeError(f"The candidate parent {gid} has the score {score!r}; a score is a number from 0 to 1.")
        if isinstance(kids, bool) or not isinstance(kids, int) or kids < 0:
            raise GenomeError(f"The candidate parent {gid} has {kids!r} children; a child count is a whole number, 0 "
                              f"or more.")
        if gid in raw:
            raise GenomeError(f"The candidate parent {gid} is listed twice.")
        x = sharpness * (score - midpoint)
        sigmoid = 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))
        raw[gid] = sigmoid / (1 + kids)
    total = sum(raw.values())
    if not total > 0:
        raise GenomeError("Every candidate parent has a weight of zero, so none can be picked.")
    return {gid: w / total for gid, w in raw.items()}


def pick_parent(entries, rng):
    """One id drawn from entries (see parent_weights) with their probabilities, using rng (a random.Random)."""
    weights = parent_weights(entries)
    ids = list(weights)
    return rng.choices(ids, weights=[weights[i] for i in ids], k=1)[0]


def pareto_front(entries, axes):
    """The ids of entries ({id: {axis: value}}) that no other entry dominates, in the entries' own order. axes is
    {axis: "higher" | "lower"}, saying which way each axis is better; the axes are never collapsed into one number.
    One entry dominates another when it is at least as good on every axis and better on at least one. A missing or
    non-numeric value is worse than any number on its axis (and as good as another missing one). Identical entries
    don't dominate each other, so both stay on the front."""
    for axis, better in axes.items():
        if better not in ("higher", "lower"):
            raise GenomeError(f"The axis {axis} must be \"higher\" or \"lower\" (which way is better), not {better!r}.")

    def key(values, axis):
        """(known, value) oriented so that larger is better: a missing value sorts below every number."""
        value = values.get(axis) if isinstance(values, dict) else None
        if not is_number(value):
            return (0, 0)
        return (1, value if axes[axis] == "higher" else -value)

    keyed = {gid: {axis: key(values, axis) for axis in axes} for gid, values in entries.items()}

    def dominates(a, b):
        return all(a[x] >= b[x] for x in axes) and any(a[x] > b[x] for x in axes)

    return [gid for gid in keyed if not any(dominates(keyed[other], keyed[gid]) for other in keyed if other != gid)]


def stored_genomes(home):
    """The genome.json of every stored genome under home/genomes/, by id, sorted; a folder that isn't a readable
    genome (a temporary folder, a broken record) is left out."""
    root = os.path.join(home, "genomes")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return {}
    found = {}
    for name in names:
        if not safe_id(name) or not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            found[name] = read_genome(home, name)
        except GenomeError:
            continue
    return found


def children(home, genome_id):
    """How many stored genomes list genome_id among their parents."""
    return sum(1 for genome in stored_genomes(home).values()
               if isinstance(genome.get("parents"), list) and genome_id in genome["parents"])


# ---------------------------------------------------------------- near-duplicates

DEFAULT_MAX_CHANGED = 10  # the most changed characters, over all files, at which a candidate is a stored genome's twin
DISCARDED_FILE = "discarded.jsonl"  # in genomes/: one line per candidate dropped as a near-duplicate


class NearDuplicate(GenomeError):
    """A candidate nearly identical to a stored genome: it was discarded, and a fresh one can be asked for."""

    def __init__(self, message, closest, changed):
        super().__init__(message)
        self.closest, self.changed = closest, changed


def check_max_changed(value, name="duplicate_max_changed"):
    """value when it is a whole number of at least 0 (not a boolean); a GenomeError naming it otherwise."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GenomeError(f"{name} must be a whole number of at least 0 (the most characters, summed over the files, "
                          f"by which a candidate may differ from a stored genome with the same traits and still be "
                          f"discarded as its near-duplicate; 0 discards only exact copies), not {value!r}.")
    return value


def as_text(data):
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)


def changed_bound(old, new):
    """A lower bound on changed_chars of two texts, cheap to compute: each changed region covers at least the
    characters of the longer text that the other can't match, even ignoring order."""
    common = sum((collections.Counter(old) & collections.Counter(new)).values())
    return max(len(old), len(new)) - common


def changed_chars(old, new, limit=None):
    """How many characters differ between two versions of one file (bytes, decoded as UTF-8): over difflib's
    SequenceMatcher opcodes on the decoded texts, the sum for every non-equal opcode of the larger of its two spans;
    0 for identical bytes. When limit is given and a cheap lower bound already exceeds it, that bound is returned
    instead of the exact count (it is above limit either way)."""
    if old == new:
        return 0
    old_text, new_text = as_text(old), as_text(new)
    if limit is not None:
        bound = changed_bound(old_text, new_text)
        if bound > limit:
            return bound
    return sum(max(i2 - i1, j2 - j1)
               for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_text, new_text).get_opcodes()
               if tag != "equal")


def near_duplicate(home, profile, contents, traits, max_changed):
    """(id, changed) of the stored genome closest to a candidate, or None when no stored genome is that close.

    contents ({path: bytes}) are every evolvable file of the candidate and traits its trait values. A stored genome
    of the same profile counts when its traits equal the candidate's and the characters that differ between its
    files and the candidate's (see changed_chars), summed over every file either of them holds (a file only one
    holds counts whole), are at most max_changed. The closest genome is the one with the fewest changed
    characters; a genome whose files can't be read doesn't count."""
    max_changed = check_max_changed(max_changed, "max_changed")
    best = None
    for gid, genome in stored_genomes(home).items():
        if genome.get("profile") != profile["name"] or genome.get("traits") != traits:
            continue
        limit = max_changed if best is None else min(max_changed, best[1] - 1)
        if limit < 0:
            break
        paths = sorted(set(contents) | {entry["path"] for entry in genome["files"]})
        listed = {entry["path"] for entry in genome["files"]}
        total = 0
        for rel in paths:
            if rel in listed:
                try:
                    stored = read_bytes(os.path.join(genome_dir(home, gid), "files", *rel.split("/")))
                except OSError:
                    total = None
                    break
            else:
                stored = b""
            total += changed_chars(stored, contents.get(rel, b""), limit=limit - total)
            if total > limit:
                break
        if total is not None and total <= limit:
            best = (gid, total)
    return best


def refuse_near_duplicate(home, profile, contents, traits, max_changed, child_id, parent, failure_id, kind):
    """Raise NearDuplicate, after appending one line to genomes/discarded.jsonl ({"at", "parent", "failure" (its
    id), "closest", "changed", "kind"}), when the candidate child_id (its files and traits) is a near-duplicate of a
    stored genome (see near_duplicate); return quietly otherwise. Meant as make_child's check."""
    found = near_duplicate(home, profile, contents, traits, max_changed)
    if found is None:
        return
    closest, changed = found
    line = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "parent": parent,
            "failure": failure_id, "closest": closest, "changed": changed, "kind": kind}
    path = os.path.join(home, "genomes", DISCARDED_FILE)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except OSError as e:
        raise GenomeError(f"Can't record the discarded candidate {child_id} in {path}: {e.strerror or e}.")
    how = f"differ from its files by {changed} character{'' if changed == 1 else 's'} in total"
    if changed == 0:
        how += " (an exact copy)"
    raise NearDuplicate(f"The candidate {child_id} was discarded before any run: its traits are those of the stored "
                        f"genome {closest} and its files {how}, at most the limit of {max_changed}. "
                        f"Nothing was written for it; ask for a fresh mutation.", closest, changed)
