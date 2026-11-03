# Example: evolving a resume-tailoring skill

This folder holds everything needed to evolve a skill that tailors a resume to a job description: a profile, a
suite of five test cases, a check script and the files the cases work on. It is a starting example, written for the
resume-tailoring skill of the Artemis assistant in the personal assistants, but nothing in the evolver depends on
it, and you can copy it as a pattern for any other skill.

Everything in the fixtures is made up. The person is Dana Ilescu, an invented engineering leader from Cluj, and her
employers (Northwind Ledger, Copperline Games, Brightkettle Labs), the companies hiring, every number and every date
are invented too. No real CV, job application or personal file is used or needed.

## What the skill does, and the rule the example tests

When asked `artemis tailor <job description>`, the skill reads the job description, writes a fit note, a resume and
sometimes a cover letter in `applications/<date>_<company>_<role>/`, starting from the latest approved CV in
`cvs/`, and renders the resume with `tools/render_resume.py`. Its core rule is that every claim on a resume traces
to a line in the fact bank, `profile/master_cv.md`. It must also leave no review markers behind (`[GAP]`,
`[VERIFY]`, `[CONF]`, `{{` placeholders or `**` bold markup) and speak in the first person, never calling its owner
"he", "she", "his", "her" and so on.

A script can decide almost all of that, so the suite relies on a script rather than a model's opinion. A model (the
judge) is asked only one thing no script can decide: whether the summary speaks in the first person and fits the
job.

## How a run is set up

The skill works in `~/personal-assistants/artemis/`. So that it finds its files there without touching your real
home folder, the suite gives the agent a home folder of its own inside each run's throwaway folder (the `home` key
in `suite.json`). That home holds a made-up `personal-assistants/artemis/` with:

- `profile/master_cv.md`: Dana's fact bank. One of its lines is marked `[VERIFY]` (a fact not yet confirmed) and one
  uses `**` bold, so a skill that copies lines carelessly leaves a marker behind.
- `cvs/`: one approved CV and a `README.md` explaining that the newest one is the starting point.
- `jobs/`: the job description for the case.
- `tools/render_resume.py`: a stand-in renderer that writes placeholder `.docx` and `.pdf` files and always
  succeeds, so a run never depends on the real one.

The fact bank, the CV, the job descriptions and the renderer are frozen: a run that changes one of them fails,
whatever its checks say. Claude Code still uses your real configuration folder and subscription login, and nothing
else in your real home folder is readable to the agent, so it can't reach your real fact bank at
`/home/<you>/personal-assistants/artemis/` by its full path.

Each prompt asks for the application the way you would ask the skill, then names the application folder, so the
checks know where to look:

```
artemis tailor ~/personal-assistants/artemis/jobs/bluewater-pay_head-of-engineering.md

Put this application in ~/personal-assistants/artemis/applications/2026-10-15_bluewater-pay_head-of-engineering/.
```

## The cases

| Case | Used for | What it tests |
|---|---|---|
| `payments-lead` | training | A head of payments engineering role that matches Dana's experience well. The only case with a judge, which reads the resume's summary next to the job description (rubric in `rubrics/summary.md`). |
| `games-manager` | training | A games backend manager role, which should lead with her years at Copperline Games. |
| `stretch-role` | training | A VP role asking for much more than the fact bank holds (15 years as a director, 80 engineers, Go, a certification). The resume must not stretch to meet it; gaps belong in the fit note, which must exist. |
| `with-cover-letter` | held out | A role that asks for a cover letter too; the letter's claims are traced like the resume's. |
| `one-page-director` | held out | A director role asking for a one-page resume, where the temptation is to merge or reword jobs. |

The two held-out cases are never shown to the rewrites; the winner is chosen on them alone.

## What the check enforces

Each case's command check runs `checks/trace_claims.py` with the application folder its prompt names, for example
`applications/2026-10-15_bluewater-pay_head-of-engineering`. The script reads the fact bank and, in that folder only,
`resume.md` and `cover.md` if there is one. A resume anywhere else doesn't count, and a missing one fails. Lines of the
fact bank marked `[GAP]`, `[VERIFY]` or `[CONF]` are unconfirmed, so nothing traces to them.

It fails, printing one plain line per problem, when the resume or the cover letter:

- has a number the fact bank doesn't hold with the same unit. 35% traces only to 35%, 12+ only to 12+ (not to 12),
  250k only to 250k. A bare number traces only to the same number followed by the same word: "35 engineers" needs
  "35 engineers" in the fact bank, and 35% elsewhere doesn't help. Numbers written as words (one to twenty, the tens
  up to ninety, and pairs such as forty-two) are read as numbers, and "percent" as %. A year traces to the same year
  anywhere in the fact bank, and a full date such as "2 November 2026" isn't checked;
- has an experience heading at any level (`#` to `####`), or a plain line or bullet written the same way
  (`<title> | <employer> | <start> – <end>`), whose title, employer and date range don't appear together on one
  experience heading of the fact bank. A heading with a year in it that is written another way fails unless the
  fact bank has the same line;
- names, in any other line, an employer the fact bank doesn't hold. The cover letter may also name the company the
  application is for, taken from the folder's name (`<date>_<company>_<role>`), and nothing else;
- has a `[GAP]`, `[VERIFY]` or `[CONF]` marker, a `{{` placeholder or `**` bold markup left in;
- uses he, his, him, himself, she, her, hers or herself as a word, in any case;
- doesn't exist (the resume), or the fact bank doesn't.

Spotting an employer in running text is a heuristic, not a parser. A name counts as an employer when it is two or
more capitalised words ending in a company word (Analytics, Bank, Games, Labs, Ledger, Pay, Systems, Software,
Group, Inc, Ltd, GmbH, SRL and others; the list is `EMPLOYER_WORDS` in the script), or capitalised words right after
"at", "for" or "with", at least as many as the fact bank's shortest employer name. Such a name passes when it is one
of the fact bank's employers or appears word for word on a confirmed line of the fact bank.

What it lets through, because it isn't a claim:

- a number or a name quoted from the case's job ad (the frozen file under `jobs/`, never a copy the agent wrote) in a
  sentence about the employer: one that says "you" or "your", names the company, or says what the person has not or
  never done. "Your team of 8" and "I have not led 80 engineers" pass; "Led 80 engineers" still fails. A name from
  the job ad also passes where it isn't introduced by "at", "for" or "with", such as the role's title used as the
  resume's headline;
- "4 became team leads" when the fact bank says "4 of them became team leads": small linking words after a number
  (of, them, the, who and the like) are skipped. "One" on its own isn't read as a number, since it is usually "no
  one" or "one of";
- a fact bank heading written with a dash or commas in place of `|`.

A sentence that quotes the job ad without saying whose numbers they are ("Growing from 40 to 120 engineers is a
hiring problem") still fails.

What the check can't see:

- a claim with no number, employer, title or date in it: "I led the payments migration" passes whether or not the
  fact bank says so. Only the judge, in one case, reads the summary's meaning;
- an employer written in a way the heuristic misses: one word after "at" ("at Acme"), or a name without a company
  word that doesn't follow "at", "for" or "with";
- a number reused with the same unit or word but about something else: "Hired 14 engineers" passes because the fact
  bank says "Led 14 engineers";
- a claim stretched in wording but not in figures, such as "led" where the fact bank says "helped".

The heuristic can also fail a phrase that isn't an employer: "Built Payment Systems" is read as one unless the fact
bank has the words "payment systems" somewhere. Each case also has a file check that looks for the markers and pronouns directly, so a
failure's evidence says exactly what was left behind. Run it by hand on a folder like Artemis's (without the second
argument it checks the newest application):

```sh
python3 genome/examples/resume-tailoring/checks/trace_claims.py ~/personal-assistants/artemis applications/<folder>
```

## Running it on your own copy of the skill

A real run starts Claude Code on your subscription and spends tokens: each case is a full agent session, and a
generation runs many of them. Don't start one until you have decided to spend that budget. A light generation
(below, with a budget of 2 million tokens) is the smallest useful run.

The commands run from the folder that holds `genome/`. The skill's folder must sit in a git repository, because
the winner comes back only as a pull request against that repository.

```sh
example=genome/examples/resume-tailoring
skill=~/personal-assistants/artemis/skills/resume-tailoring
```

1. Check the suite against the profile and your skill. This runs nothing and spends nothing.

   ```sh
   python3 genome/scripts/evolve.py suite check "$example/suite.json" --profile "$example/profile.json" --source "$skill"
   ```

2. Optionally, try one case with the skill as it is now (one agent session).

   ```sh
   python3 genome/scripts/evolve.py suite run "$example/suite.json" payments-lead --individual "$skill"
   ```

3. Seed the first genome from the skill folder.

   ```sh
   python3 genome/scripts/evolve.py genome seed --id resume-seed --profile "$example/profile.json" --source "$skill"
   ```

4. Run a light generation on `SKILL.md`. It prints its plan and a token estimate and asks before it starts.

   ```sh
   python3 genome/scripts/evolve.py generation --suite "$example/suite.json" --profile "$example/profile.json" \
       --source "$skill" --target SKILL.md --from resume-seed --budget 2000000 --light
   ```

5. Read the report: the winner, or why there is none, with each candidate's scores and the tokens spent.

   ```sh
   python3 genome/scripts/evolve.py report <generation>
   ```

6. If you want the winner, promote it. This opens a pull request against the repository holding the skill, which
   you read and merge yourself; nothing is merged for you.

   ```sh
   python3 genome/scripts/evolve.py promote <winner> --generation <generation> --profile "$example/profile.json" --source "$skill"
   ```

## What a rewrite may change

The profile lets a rewrite change only the skill's `SKILL.md`, and only grow it by 30% over the original, so the
skill can't win by piling on rules. It can never hold or change a Python file (`never: ["**/*.py"]`), so the skill's
own tools stay as they are. The judge's rubric and model are fixed in the suite, outside the skill's folder, so a
rewrite can never change what it is judged by.
