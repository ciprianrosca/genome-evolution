"""The genome README's section on evolving any skill or agent (spec .regista/specs/genome-readme.json: EX-4).

genome/README.md gains a section headed 'Evolving any skill or agent'. These tests read the README as plain text,
find that section (from its heading to the next heading of the same or higher level, ignoring lines inside fenced
code blocks), and check that it names everything a reader needs: the suite file's top-level and case keys, the three
check kinds and their keys, the runner placeholders and tokens values, the score axes, each safety rail by a key
phrase, and how the genome fits with an assistant's self-evolve habit. The README as a whole must hold no real
person's name or employer. Each test reports every missing name at once.

Names are matched case-insensitively, with runs of whitespace (line wraps) treated as one space; keys must appear as
whole words (so 'id' isn't satisfied by 'idea').

Run: cd genome/tests && env -u CLAUDE_PROJECT_DIR python3 -m unittest test_genome_readme -v"""
import os
import re
import unittest

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
SECTION_TITLE = "Evolving any skill or agent"

SUITE_KEYS = ["name", "describe", "install", "runner", "model", "timeout_minutes", "holdout_pct",
              "screen_cases", "checks_dir", "sandbox", "cases"]
CASE_KEYS = ["id", "split", "prompt", "prompt_file", "files", "frozen", "checks"]
COMMAND_CHECK_KEYS = ["run", "exit", "timeout"]
FILE_CHECK_KEYS = ["file", "exists", "contains", "not_contains", "matches", "not_matches"]
JUDGE_CHECK_KEYS = ["judge", "rubric_file", "model", "files"]
PLACEHOLDERS = ["{workspace}", "{individual}", "{prompt_file}", "{prompt}", "{model}"]
TOKENS_VALUES = ["none", "claude-json"]
# Each axis may be written as its key or in words.
AXES = [("cases_passed", "cases passed"), ("checks_passed", "checks passed"), ("tokens",)]
RAIL_PHRASES = ["sandbox", "API key", "frozen judge", "held-out", "size caps", "never", "budget", "pull request"]
GUARDS = ["Ciprian", "Mambu", "Playtika"]

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def read_readme():
    with open(README, encoding="utf-8") as handle:
        return handle.read()


def find_section(text, title):
    """The section's lines, from its heading to the next heading of the same or higher level; None if absent."""
    lines = text.splitlines()
    start = level = None
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        depth = len(match.group(1))
        if start is None:
            if match.group(2).strip().lower() == title.lower():
                start, level = index, depth
        elif depth <= level:
            return "\n".join(lines[start:index])
    if start is None:
        return None
    return "\n".join(lines[start:])


def normalise(text):
    return re.sub(r"\s+", " ", text).lower()


def has_phrase(text, phrase):
    return normalise(phrase) in normalise(text)


def has_word(text, word):
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(word.lower()) + r"(?![A-Za-z0-9_])", text.lower()) is not None


class GenomeReadmeSection(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(README), "genome/README.md doesn't exist")
        self.text = read_readme()
        self.section = find_section(self.text, SECTION_TITLE)
        self.assertIsNotNone(self.section,
                             "genome/README.md has no section headed '%s'" % SECTION_TITLE)

    def assertAllPresent(self, what, missing):
        self.assertEqual([], missing, "The section '%s' doesn't name these %s: %s"
                         % (SECTION_TITLE, what, ", ".join(missing)))

    def test_names_every_suite_and_case_key(self):
        """covers: EX-4. Every suite top-level key and every case key appears as a whole word in the section."""
        missing = ["suite key " + key for key in SUITE_KEYS if not has_word(self.section, key)]
        missing += ["case key " + key for key in CASE_KEYS if not has_word(self.section, key)]
        self.assertAllPresent("suite file keys", missing)

    def test_names_the_three_check_kinds_and_their_keys(self):
        """covers: EX-4. Command, file and judge checks, each kind named, with every key of each kind."""
        missing = []
        for kind in ("command", "file", "judge"):
            if not re.search(r"\b" + kind + r"\b[^.\n]{0,20}\bchecks?\b", self.section, re.IGNORECASE) and \
                    not has_phrase(self.section, "the " + kind):
                missing.append("check kind '%s'" % kind)
        for kind, keys in (("command", COMMAND_CHECK_KEYS), ("file", FILE_CHECK_KEYS), ("judge", JUDGE_CHECK_KEYS)):
            missing += ["%s check key %s" % (kind, key) for key in keys if not has_word(self.section, key)]
        if "{checks}" not in self.section:
            missing.append("command check placeholder {checks}")
        self.assertAllPresent("check kinds or keys", missing)

    def test_names_runner_placeholders_and_tokens_values(self):
        """covers: EX-4. The five template-runner placeholders, literally, and both tokens values."""
        missing = ["placeholder " + p for p in PLACEHOLDERS if p not in self.section]
        missing += ["tokens value '%s'" % value for value in TOKENS_VALUES if not has_word(self.section, value)]
        self.assertAllPresent("runner placeholders or tokens values", missing)

    def test_names_the_score_axes(self):
        """covers: EX-4. Cases passed, checks passed and tokens, as keys or in words."""
        missing = [forms[0] for forms in AXES if not any(has_phrase(self.section, form) for form in forms)]
        self.assertAllPresent("score axes", missing)

    def test_names_each_safety_rail_by_key_phrase(self):
        """covers: EX-4. Sandbox, API key, frozen judge, held-out, size caps, never, budget, pull request."""
        missing = ["'%s'" % phrase for phrase in RAIL_PHRASES if not has_phrase(self.section, phrase)]
        self.assertAllPresent("safety rail key phrases", missing)

    def test_explains_the_fit_with_self_evolve(self):
        """covers: EX-4. The section says how the genome fits with an assistant's self-evolve habit."""
        self.assertTrue(has_phrase(self.section, "self-evolve"),
                        "The section '%s' doesn't mention self-evolve" % SECTION_TITLE)


class GenomeReadmeHoldsNoRealPeople(unittest.TestCase):
    def test_no_real_names_or_employers(self):
        """covers: EX-4. The whole README holds none of the made-up-data guard strings."""
        self.assertTrue(os.path.isfile(README), "genome/README.md doesn't exist")
        text = read_readme()
        found = [guard for guard in GUARDS if guard.lower() in text.lower()]
        self.assertEqual([], found, "genome/README.md contains real people's data: %s" % ", ".join(found))


class SectionFinder(unittest.TestCase):
    """The helper itself, on small made-up texts, so a README failure can't be a finder bug."""

    def test_section_ends_at_same_or_higher_heading_and_ignores_code_fences(self):
        """covers: EX-4. Subheadings and '#' lines in code stay inside; the next same-level heading ends it."""
        text = "# Top\nintro\n## Evolving any skill or agent\nbody\n```sh\n# a shell comment\n```\n" \
               "### Sub\nmore\n## Next\noutside\n"
        section = find_section(text, SECTION_TITLE)
        self.assertIn("# a shell comment", section)
        self.assertIn("more", section)
        self.assertNotIn("outside", section)
        self.assertIsNone(find_section("# Top\nnothing here\n", SECTION_TITLE))


if __name__ == "__main__":
    unittest.main()
