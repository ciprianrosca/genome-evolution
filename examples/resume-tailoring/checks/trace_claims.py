#!/usr/bin/env python3
"""Checks that every claim on a tailored resume traces to the fact bank.

Usage: python3 trace_claims.py [ARTEMIS_FOLDER]

ARTEMIS_FOLDER is the assistant's folder (default: ~/personal-assistants/artemis). Its fact bank is
profile/master_cv.md; the resume checked is applications/*/resume.md, the newest one when there are several (by the
date its folder's name starts with, then by when it was last changed), with the cover.md beside it if there is one.

It prints one plain line per problem and exits 1 when:
- a number on the resume (digits, with or without %, +, k or a decimal part) isn't a number in the fact bank;
- an experience heading on the resume (a line starting '### ', written 'title | employer | start – end') names a
  job title, an employer or a date range the fact bank's headings don't, or puts them together in a way no fact
  bank heading does;
- a [GAP], [VERIFY] or [CONF] marker, a '{{' placeholder or '**' bold markup is left in resume.md or cover.md;
- 'he', 'his' or 'him' appears as a word in resume.md or cover.md (the resume speaks for its owner);
- there is no resume, or no fact bank.
It exits 0, printing one line, when every claim traces.

Standard library only, and no helper next to it: suites run it with PYTHONSAFEPATH=1."""
import os
import re
import sys

MARKERS = ("[GAP]", "[VERIFY]", "[CONF]", "{{", "**")
PRONOUNS = re.compile(r"\b(he|his|him)\b", re.I)
# A number: digits (1,200 counts as one number), an optional decimal part, then optional k, + and %. Digits glued to
# a letter before them (S3, B2B, OAuth2) are part of a name, not a claim.
NUMBER = re.compile(r"(?<![A-Za-z0-9_.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:[kK])?\+?%?")
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})")
DASHES = re.compile(r"\s*[-‐‑‒–—−]+\s*")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def numbers(text):
    """(the number as written, its value as a plain string: no thousands commas) for every number in text."""
    found = []
    for m in NUMBER.finditer(text):
        found.append((m.group(0), m.group(1).replace(",", "") + (m.group(2) or "")))
    return found


def plain(text):
    return " ".join(text.split()).casefold()


def date_range(text):
    """A date range, written the same whatever dash and spacing it uses: '2019 – 2022' and '2019-2022' are equal."""
    return DASHES.sub(" – ", " ".join(text.split())).casefold()


def heading_parts(line):
    """(title, employer, dates) of an experience heading, or None when it isn't written that way."""
    parts = [p.strip() for p in line[4:].split("|")]
    if len(parts) != 3 or not all(parts):
        return None
    return parts


def newest_resume(artemis):
    """The newest applications/*/resume.md, or None."""
    apps = os.path.join(artemis, "applications")
    try:
        names = os.listdir(apps)
    except OSError:
        return None
    found = []
    for name in names:
        path = os.path.join(apps, name, "resume.md")
        if os.path.isfile(path):
            dated = DATED.match(name)
            found.append(((dated.group(1) if dated else ""), os.path.getmtime(path), name, path))
    return max(found)[3] if found else None


def check(artemis, shown):
    """The problem lines for the resume under artemis; shown(path) is how a path is printed."""
    bank_path = os.path.join(artemis, "profile", "master_cv.md")
    if not os.path.isfile(bank_path):
        return [f"No fact bank at {shown(bank_path)}, so no claim on the resume can be traced."], None
    resume_path = newest_resume(artemis)
    if resume_path is None:
        return [f"No resume found: there is no applications/<folder>/resume.md under {shown(artemis)}."], None
    bank = read(bank_path)
    bank_numbers = {value for _written, value in numbers(bank)}
    bank_headings = [heading_parts(line) for line in bank.splitlines() if line.startswith("### ")]
    bank_lines = {plain(line) for line in bank.splitlines() if line.startswith("### ")}
    triples = {(plain(h[0]), plain(h[1]), date_range(h[2])) for h in bank_headings if h}
    titles = {t for t, _e, _d in triples}
    employers = {e for _t, e, _d in triples}
    ranges = {d for _t, _e, d in triples}

    problems = []
    files = [resume_path]
    cover_path = os.path.join(os.path.dirname(resume_path), "cover.md")
    if os.path.isfile(cover_path):
        files.append(cover_path)
    for path in files:
        is_resume = path == resume_path
        where = shown(path)
        for number, line in enumerate(read(path).splitlines(), 1):
            at = f"{where}, line {number}"
            text = line.strip()
            if is_resume:
                for written, value in numbers(line):
                    if value not in bank_numbers:
                        problems.append(f"{at}: the number {written} isn't in the fact bank: {text}")
                if line.startswith("### ") and plain(line) not in bank_lines:
                    parts = heading_parts(line)
                    if parts is None:
                        problems.append(f"{at}: this heading isn't in the fact bank and isn't written as "
                                        f"'title | employer | start – end': {text}")
                    else:
                        title, employer, dates = parts
                        missing = []
                        if plain(title) not in titles:
                            missing.append(f"the job title '{title}' isn't in the fact bank")
                        if plain(employer) not in employers:
                            missing.append(f"the employer '{employer}' isn't in the fact bank")
                        if date_range(dates) not in ranges:
                            missing.append(f"the date range '{dates}' isn't in the fact bank")
                        for what in missing:
                            problems.append(f"{at}: {what}: {text}")
                        if not missing and (plain(title), plain(employer), date_range(dates)) not in triples:
                            problems.append(f"{at}: no fact bank heading has this title, employer and date range "
                                            f"together: {text}")
            for marker in MARKERS:
                if marker in line or (marker.startswith("[") and marker.casefold() in line.casefold()):
                    problems.append(f"{at}: the marker {marker} is left in: {text}")
            for m in PRONOUNS.finditer(line):
                problems.append(f"{at}: the third-person pronoun '{m.group(0)}' is used; write in the first "
                                f"person: {text}")
    return problems, resume_path


def main(argv):
    if len(argv) > 1:
        print("Usage: trace_claims.py [ARTEMIS_FOLDER]")
        return 2
    artemis = argv[0] if argv else os.path.join(os.path.expanduser("~"), "personal-assistants", "artemis")
    cwd = os.getcwd()

    def shown(path):
        rel = os.path.relpath(os.path.abspath(path), cwd)
        return path if rel.startswith("..") else rel.replace(os.sep, "/")

    problems, resume_path = check(artemis, shown)
    for line in problems:
        print(line)
    if problems:
        return 1
    print(f"Every claim in {shown(resume_path)} traces to the fact bank.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
