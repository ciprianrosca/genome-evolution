#!/usr/bin/env python3
"""Checks that every claim on a tailored resume, and on its cover letter, traces to the fact bank.

Usage: python3 trace_claims.py [ARTEMIS_FOLDER [APPLICATION_FOLDER]]

ARTEMIS_FOLDER is the assistant's folder (default: ~/personal-assistants/artemis). Its fact bank is
profile/master_cv.md. APPLICATION_FOLDER is the application to check, relative to ARTEMIS_FOLDER (for example
applications/2026-10-15_bluewater-pay_head-of-engineering) or absolute; the check reads resume.md and, if there is
one, cover.md in that folder and nowhere else. A suite passes the folder its case's prompt names. Without it (by
hand), the newest applications/*/resume.md is checked (by the date its folder's name starts with, then by when it
was last changed).

The fact bank's lines marked [GAP], [VERIFY] or [CONF] are unconfirmed: nothing traces to them. It prints one plain
line per problem and exits 1 when, in resume.md or cover.md:
- a number isn't in the fact bank with the same unit: 35% traces only to 35%, 12+ only to 12+, 250k only to 250k, €40
  only to €40; a bare number traces only to the same number followed by the same word ('35 engineers' to '35
  engineers'), or with no word after it when it has none. Numbers written as words (one to twenty, the tens up to
  ninety, and the two together, such as forty-two) count as numbers, and 'percent' counts as %. A year (a bare
  number from 1900 to 2099) traces to the same year anywhere in the fact bank. A full date such as '2 November 2026'
  or 2026-11-02 isn't a claim and isn't checked;
- an experience heading, at any heading level or as a plain line or bullet (a line with '|' separators and a year
  in it, written 'title | employer | start – end'), names a job title, an employer or a date range the fact bank's
  experience headings don't, or puts them together in a way no fact bank heading does; a heading of any level with a
  year in it that is written another way fails unless the fact bank has the same line;
- any other line names an employer the fact bank doesn't hold (see EMPLOYER_WORDS below for how a name is spotted);
  the cover letter may also name the company the application is for, read from the application folder's name
  (<date>_<company>_<role>);
- a [GAP], [VERIFY] or [CONF] marker, a '{{' placeholder or '**' bold markup is left in;
- he, his, him, himself, she, her, hers or herself appears as a word, in any case (the resume speaks for its owner,
  in the first person);
- there is no resume in the application folder, or no fact bank.
It exits 0, printing one line, when every claim it can see traces. It can't see a claim with no number, employer,
title or date in it ('I led the payments migration' passes whether or not the fact bank says so).

Standard library only, and no helper next to it: suites run it with PYTHONSAFEPATH=1."""
import os
import re
import sys

MARKERS = ("[GAP]", "[VERIFY]", "[CONF]", "{{", "**")
UNCONFIRMED = re.compile(r"\[(GAP|VERIFY|CONF)\]", re.I)
PRONOUNS = re.compile(r"\b(he|his|him|himself|she|her|hers|herself)\b", re.I)

# A number: an optional currency sign, digits (1,200 counts as one number), an optional decimal part, then any
# letters glued to it as its unit (k, M, x, bn), then optional + and %. Digits glued to a letter or a dot before them
# (S3, B2B, OAuth2, v1.2) are part of a name, not a claim.
NUMBER = re.compile(r"(?<![A-Za-z0-9_.,])([$€£]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?([A-Za-z]*)(\+?)(%?)")
SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen twenty").split()
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
WORD_VALUES = {w: i for i, w in enumerate(SMALL)} | TENS
NUMBER_WORD = re.compile(r"\b(" + "|".join(sorted(TENS, key=len, reverse=True)) + r")[-‐‑ ](one|two|three|four|five|"
                         r"six|seven|eight|nine)\b|\b(" + "|".join(sorted(WORD_VALUES, key=len, reverse=True)) + r")\b",
                         re.I)
FOLLOWING = re.compile(r"[ \t]*-?[ \t]*([A-Za-z][A-Za-z'’]*)")
PERCENT = re.compile(r"[ \t]*(?:percent|per cent)\b", re.I)
MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
         r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
FULL_DATE = re.compile(rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}(?:st|nd|rd|th)? {MONTHS},? \d{{4}}\b|"
                       rf"\b{MONTHS} \d{{1,2}}(?:st|nd|rd|th)?, \d{{4}}\b")
YEAR = re.compile(r"^(19|20)\d\d$")
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})")
DASHES = re.compile(r"\s*[-‐‑‒–—−]+\s*")
LEADER = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)?")
HEADING = re.compile(r"^\s*#{1,6}\s")

# How an employer is spotted outside an experience heading. This is a heuristic, not a parser:
# - a run of two or more capitalised words ending in one of these company words ('Bluefjord Analytics',
#   'Northwind Ledger'), or
# - a run of capitalised words right after 'at', 'for' or 'with' with at least as many words as the fact bank's
#   shortest employer ('at Bluefjord Analytics', 'with Apache Kafka').
# Such a name passes when it is one of the fact bank's employers or appears, word for word, on a confirmed line of the
# fact bank; otherwise it fails.
EMPLOYER_WORDS = {w.casefold() for w in (
    "Analytics", "Bank", "Capital", "Consulting", "Corp", "Corporation", "Digital", "Finance", "Financial", "Foods",
    "Freight", "Games", "GmbH", "Group", "Health", "Holdings", "Inc", "Insurance", "Interactive", "Labs", "Ledger",
    "LLC", "Logistics", "Ltd", "Media", "Networks", "Partners", "Pay", "Payments", "Robotics", "Software",
    "Solutions", "SRL", "Studio", "Studios", "Systems", "Technologies", "Technology", "Telecom", "Ventures", "Works")}
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&'’.-]*")
INTRO = {"at", "for", "with"}


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def plain(text):
    return " ".join(text.split()).casefold()


def date_range(text):
    """A date range, written the same whatever dash and spacing it uses: '2019 – 2022' and '2019-2022' are equal."""
    return DASHES.sub(" – ", " ".join(text.split())).casefold()


def unconfirmed(line):
    return bool(UNCONFIRMED.search(line))


def following_word(text, end):
    m = FOLLOWING.match(text, end)
    if not m:
        return ""
    word = m.group(1).casefold()
    for tail in ("'s", "’s"):
        if word.endswith(tail):
            word = word[:-2]
    return word.strip("'’")


def numbers(line):
    """(the number as written, the key it traces by) for every number in line. The key is the value, the unit and,
    for a bare number, the word after it; a year's key is the year alone."""
    text = FULL_DATE.sub(lambda m: " " * len(m.group(0)), line)
    found = []
    for m in NUMBER.finditer(text):
        currency, whole, decimal, letters, plus, percent = m.groups()
        value = whole.replace(",", "") + (decimal or "")
        end = m.end()
        if not percent and PERCENT.match(text, end):
            percent, end = "%", PERCENT.match(text, end).end()
        unit = currency + letters.casefold() + plus + percent
        if not unit and YEAR.match(value) and "," not in whole:
            found.append((m.group(0), ("year", value)))
        elif unit:
            found.append((m.group(0), (value, unit, "")))
        else:
            found.append((m.group(0), (value, "", following_word(text, end))))
    for m in NUMBER_WORD.finditer(text):
        if m.group(1):
            value = TENS[m.group(1).casefold()] + WORD_VALUES[m.group(2).casefold()]
        else:
            value = WORD_VALUES[m.group(3).casefold()]
        end = m.end()
        if PERCENT.match(text, end):
            found.append((m.group(0), (str(value), "%", "")))
        else:
            found.append((m.group(0), (str(value), "", following_word(text, end))))
    return found


def content(line):
    """The line without its heading marks or bullet."""
    return line[LEADER.match(line).end():].strip()


def has_year(text):
    return bool(re.search(r"(?<!\d)(19|20)\d\d(?!\d)", text))


def experience_parts(line):
    """For a line written as an experience heading (with '|' separators and a year): its parts; otherwise None."""
    text = content(line)
    if "|" not in text or not has_year(text):
        return None
    return [p.strip() for p in text.split("|")]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def capitalised_runs(line):
    """Runs of capitalised words joined by single spaces, as (a list of (word, where it starts), the word before the
    run, casefolded). A possessive ('Ledger's') or a word ending in punctuation ends its run."""
    runs, run, before, last, prev_end = [], [], "", "", None
    for m in WORD.finditer(line):
        raw = m.group(0)
        word = raw[:-2] if raw.endswith(("'s", "’s")) else raw.rstrip(".-'’")
        if word[:1].isupper():
            if not (run and line[prev_end:m.start()] == " "):
                if run:
                    runs.append((run, before))
                run, before = [], last
            run.append((word, m.start()))
            if word != raw:  # 'Ledger's', 'Analytics.': the name ends here
                runs.append((run, before))
                run = []
        elif run:
            runs.append((run, before))
            run = []
        last, prev_end = raw.casefold(), m.end()
    if run:
        runs.append((run, before))
    return runs


def sentence_start(line, start):
    """True when the word at start begins the line's content or a sentence."""
    head = line[:start].rstrip()
    return head == "" or LEADER.match(line).end() >= start or head.endswith((".", "!", "?", ":", ";"))


class Bank:
    def __init__(self, text):
        confirmed = [line for line in text.splitlines() if not unconfirmed(line)]
        self.keys = set()
        for line in confirmed:
            self.keys.update(key for _written, key in numbers(line))
        self.lines = {plain(content(line)) for line in confirmed if HEADING.match(line) or experience_parts(line)}
        self.triples = set()
        for line in confirmed:
            parts = experience_parts(line)
            if parts and len(parts) == 3 and all(parts):
                self.triples.add((plain(parts[0]), plain(parts[1]), date_range(parts[2])))
        self.titles = {t for t, _e, _d in self.triples}
        self.employers = {e for _t, e, _d in self.triples}
        self.ranges = {d for _t, _e, d in self.triples}
        self.text = " " + " ".join(plain(line) for line in confirmed) + " "
        counts = [len(e.split()) for e in self.employers]
        self.min_words = min(counts) if counts else 2

    def holds_name(self, name):
        name = plain(name)
        return name in self.employers or re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])",
                                                    self.text) is not None


def unknown_employers(line, bank, company):
    """The names in line that look like an employer and aren't in the fact bank (company: an extra allowed slug)."""
    def known(words):
        name = " ".join(words)
        return bank.holds_name(name) or (company and slug(name) == company)

    found = []
    for run, before in capitalised_runs(line):
        if run and run[0][0].casefold() in INTRO:  # 'At Northwind Ledger', capitalised at a sentence's start
            before, run = run[0][0].casefold(), run[1:]
        while run and run[-1][0] == "I":  # 'At Northwind Ledger I cut': the pronoun isn't part of the name
            run = run[:-1]
        words = [w for w, _s in run]
        for i, word in enumerate(words):
            if word.casefold() not in EMPLOYER_WORDS or i == 0:
                continue
            candidate = words[:i + 1]
            if any(known(candidate[j:]) for j in range(len(candidate) - 1)):
                continue
            if len(candidate) > 2 and sentence_start(line, run[0][1]):
                candidate = candidate[1:]
            found.append(" ".join(candidate))
        if before in INTRO and len(words) >= bank.min_words:
            if any(known(words[:k]) for k in range(bank.min_words, len(words) + 1)):
                continue
            name = " ".join(words)
            if not any(name in f or f in name for f in found):
                found.append(name)
    return found


def heading_problems(line, bank):
    """The problems with an experience heading (or a dated heading) in line."""
    parts = experience_parts(line)
    if parts is None:
        if HEADING.match(line) and has_year(line) and plain(content(line)) not in bank.lines:
            return ["this heading isn't in the fact bank and isn't written as 'title | employer | start – end'"]
        return []
    if plain(content(line)) in bank.lines:
        return []
    if len(parts) != 3 or not all(parts):
        return ["this experience line isn't written as 'title | employer | start – end'"]
    title, employer, dates = parts
    missing = []
    if plain(title) not in bank.titles:
        missing.append(f"the job title '{title}' isn't in the fact bank")
    if plain(employer) not in bank.employers:
        missing.append(f"the employer '{employer}' isn't in the fact bank")
    if date_range(dates) not in bank.ranges:
        missing.append(f"the date range '{dates}' isn't in the fact bank")
    if not missing and (plain(title), plain(employer), date_range(dates)) not in bank.triples:
        missing.append("no fact bank heading has this title, employer and date range together")
    return missing


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


def company_slug(folder):
    """The company part of an application folder named <date>_<company>_<role>, or ''."""
    parts = os.path.basename(os.path.normpath(folder)).split("_")
    return slug(parts[1]) if len(parts) >= 3 and DATED.match(parts[0]) else ""


def check_file(path, where, bank, company):
    problems = []
    for number, line in enumerate(read(path).splitlines(), 1):
        at = f"{where}, line {number}"
        text = line.strip()
        for written, key in numbers(line):
            if key not in bank.keys:
                if key[0] == "year":
                    why = "the year isn't in the fact bank"
                elif key[1]:
                    why = f"the fact bank has no {key[0]} with the unit '{key[1]}'"
                elif key[2]:
                    why = f"the fact bank has no {key[0]} followed by '{key[2]}'"
                else:
                    why = f"the fact bank has no bare {key[0]}"
                problems.append(f"{at}: the number {written} doesn't trace ({why}): {text}")
        if experience_parts(line) is not None or HEADING.match(line):
            for what in heading_problems(line, bank):
                problems.append(f"{at}: {what}: {text}")
        if experience_parts(line) is None:
            for name in unknown_employers(line, bank, company):
                problems.append(f"{at}: the employer '{name}' isn't in the fact bank: {text}")
        for marker in MARKERS:
            if marker in line or (marker.startswith("[") and marker.casefold() in line.casefold()):
                problems.append(f"{at}: the marker {marker} is left in: {text}")
        for m in PRONOUNS.finditer(line):
            problems.append(f"{at}: the third-person pronoun '{m.group(0)}' is used; write in the first "
                            f"person: {text}")
    return problems


def check(artemis, application, shown):
    """The problem lines for the application (a folder, or None for the newest) under artemis, and the resume's
    path; shown(path) is how a path is printed."""
    bank_path = os.path.join(artemis, "profile", "master_cv.md")
    if not os.path.isfile(bank_path):
        return [f"No fact bank at {shown(bank_path)}, so no claim on the resume can be traced."], None
    if application is None:
        resume_path = newest_resume(artemis)
        if resume_path is None:
            return [f"No resume found: there is no applications/<folder>/resume.md under {shown(artemis)}."], None
    else:
        resume_path = os.path.join(application, "resume.md")
        if not os.path.isfile(resume_path):
            return [f"No resume found: there is no {shown(resume_path)}."], None
    bank = Bank(read(bank_path))
    folder = os.path.dirname(resume_path)
    problems = check_file(resume_path, shown(resume_path), bank, "")
    cover_path = os.path.join(folder, "cover.md")
    if os.path.isfile(cover_path):
        problems += check_file(cover_path, shown(cover_path), bank, company_slug(folder))
    return problems, resume_path


def main(argv):
    if len(argv) > 2:
        print("Usage: trace_claims.py [ARTEMIS_FOLDER [APPLICATION_FOLDER]]")
        return 2
    artemis = argv[0] if argv else os.path.join(os.path.expanduser("~"), "personal-assistants", "artemis")
    application = os.path.join(artemis, argv[1]) if len(argv) > 1 else None
    cwd = os.getcwd()

    def shown(path):
        rel = os.path.relpath(os.path.abspath(path), cwd)
        return path if rel.startswith("..") else rel.replace(os.sep, "/")

    problems, resume_path = check(artemis, application, shown)
    for line in problems:
        print(line)
    if problems:
        return 1
    print(f"Every claim the check can see in {shown(resume_path)} traces to the fact bank.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
