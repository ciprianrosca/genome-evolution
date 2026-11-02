# The genome: evolving Regista (in progress)

The genome teaches Regista to improve its own prompts and rules by replaying the history of real projects and
keeping the changes that do better. It lives in this folder, `genome/`, apart from Regista, which works without it.
The plan is in [docs/genome-spec.md](docs/genome-spec.md). The tool is `genome/scripts/evolve.py`, a separate
script that only reads the projects it learns from and never changes them. It reaches Regista only by running its
command line, `scripts/regista.py`, in the Regista checkout that holds this folder, or in the checkout named by
`regista_checkout` (an absolute path) in `evolve.json`.
Its files live in `~/.regista-evolve/` (or the folder in `REGISTA_EVOLVE_HOME`), with its settings in
`evolve.json` there.

The settings (`python3 genome/scripts/evolve.py config` prints them), the benchmark of past steps (`bench`), replays
(`replay`) and generations (`generation`, `report` and `promote`) are described below.

## Evolving any skill or agent

The same evolver can improve any set of text files an agent reads: a skill folder, an agent file, a rules file.
You write a suite of test cases for it, and the evolver runs the agent on those cases with the text as it is, then
makes rewritten versions (children), each aimed at one check that failed, and runs them too. The winner is chosen
only by how it does on held-out cases, which no child was aimed at, and it reaches your repository only as a pull
request that you read and merge yourself. Nothing in this part depends on Regista.

A few words used below. A genome is one stored version of the files the evolver may change. An individual is a
complete copy of the target's files built from a genome, the thing an agent actually reads during a run. A run is
one case played once by one individual, in a throwaway folder called the workspace.

### How to evolve a skill

The example evolves a made-up skill, `standup-summary`, which tells an assistant how to turn a week of notes into a
short standup summary. It lives in `skills/standup-summary/` of a git repository at `~/assistants`, and its profile
and suite live beside it in `evolve/`, outside the skill folder. The commands run from the folder that holds
`genome/`. These shell variables keep them short:

```sh
repo=~/assistants
skill=$repo/skills/standup-summary
profile=$repo/evolve/standup-summary.profile.json
suite=$repo/evolve/standup-summary/suite.json
```

1. Write a profile, a JSON file saying which of the skill's files a rewrite may change and how far. Keep it
   outside the files it calls evolvable; the evolver refuses a profile a rewrite could change.

   ```json
   {
     "name": "standup-summary",
     "describe": "A skill that tells an assistant how to turn a week of notes into a short standup summary.",
     "evolvable": ["SKILL.md"],
     "exclude": [".git/**"],
     "never": ["scripts/**"],
     "caps": [{"glob": "SKILL.md", "max_growth_pct": 50}],
     "judges": [],
     "checks": ["test -s skills/standup-summary/SKILL.md"],
     "traits": {}
   }
   ```

   `name` is the target's name. `evolvable` lists globs, relative to the skill folder, of the files a genome holds
   and may change. `exclude` lists files never copied into an individual. `never` lists files no genome may ever
   hold or change. `caps` limits how much a file may grow over the original (`max_growth_pct`) or how large it may
   be (`max_bytes`). `judges` lists evolvable files that judge the others' work and so must stay as they were
   (empty for a single skill). `checks` lists commands that run at the top of a checkout of the repository holding
   the winner's files, before its pull request is opened; every one must pass. `describe` is one sentence the
   rewriting prompts use. `traits` lists numeric or multiple-choice settings the evolver may move within bounds you
   set; it is required, and `{}` when there are none.

2. Write a suite, the test cases (described in the next part).

3. Check the suite. This loads it and prints its cases, which ones train and which are held out, and how many checks
   of each kind each case has. It runs nothing and writes nothing. With `--profile` and `--source` it also refuses a
   suite whose own files (the suite file, prompts, rubrics or check scripts) a rewrite could change.

   ```sh
   python3 genome/scripts/evolve.py suite check "$suite" --profile "$profile" --source "$skill"
   ```

4. Try one case with the skill as it is now. This runs the agent for real, on your Claude subscription, on a copy
   of the skill folder (the folder itself is never changed). It prints the outcome, the tokens used and one line
   per check, and writes `result.json` under `runs/adhoc/<case>/individual/` in the evolver's working folder. Its
   exit code is 0 when the agent finished, 3 when it paused at the subscription's usage limit and 1 otherwise; add
   `--keep` to keep the workspace for a look.

   ```sh
   python3 genome/scripts/evolve.py suite run "$suite" quiet-week --individual "$skill"
   ```

5. Seed the first genome from the skill folder. The default id is `seed`; give another one with `--id` when the
   working folder already holds a seed for another target.

   ```sh
   python3 genome/scripts/evolve.py genome seed --id standup-seed --profile "$profile" --source "$skill"
   ```

6. Run a generation on one evolvable file (`--target`). It prints its plan and token estimate and asks you to
   confirm (`--yes` skips the question). `--budget` is the most tokens it may spend (default: `budget_tokens` in
   `evolve.json`, 20 million). `--from` names the genome it starts from (default `seed`). `--light` makes a quick
   run with 2 children and only the first 3 held-out cases. `--id <generation>` names it, and running the same
   command with the same id continues a generation that paused or was interrupted.

   ```sh
   python3 genome/scripts/evolve.py generation --suite "$suite" --profile "$profile" --source "$skill" \
       --target SKILL.md --from standup-seed --budget 2000000 --light
   ```

7. Read the report. It prints the generation's report (the winner or why there is none, each candidate's scores,
   the spend) and saves it under `reports/` in the evolver's working folder.

   ```sh
   python3 genome/scripts/evolve.py report <generation>
   ```

8. Promote the winner. This creates a new branch in the repository holding the skill with the winner's changed
   files at their place under `skills/standup-summary/` and the report at `evolve-reports/<generation>.md`, runs
   the profile's checks in a temporary worktree, pushes only that branch and opens a pull request against the
   current branch (or `--onto <branch>`); `--no-pr` stops after the checks. It never merges, and it refuses a
   `--source` other than the folder the genome was seeded from.

   ```sh
   python3 genome/scripts/evolve.py promote <winner> --generation <generation> --profile "$profile" --source "$skill"
   ```

A worked example on a real kind of skill, one that tailors a resume to a job description and must never claim
anything the person's fact bank doesn't hold, is in [examples/resume-tailoring/](examples/resume-tailoring/README.md).
Its fixtures describe a made-up person.

### What a suite file looks like

A suite is a folder holding a JSON suite file and the files it names. Here is the made-up skill's suite:

```
evolve/standup-summary/
  suite.json
  checks/sections.py            the check script, copied for the checks only after a run
  prompts/private-detail.txt    a prompt kept in its own file
  rubrics/tone.md               the judge's rubric
  fixtures/quiet-week/notes.md  notes copied into each run's workspace
  fixtures/private-detail/notes.md
  fixtures/busy-week/notes.md
```

```json
{
  "name": "standup-summary",
  "describe": "Whether the standup-summary skill turns a week of notes into a short, honest summary.",
  "install": ".claude/skills/standup-summary",
  "model": "sonnet",
  "timeout_minutes": 10,
  "cases": [
    {
      "id": "quiet-week",
      "prompt": "Use the standup-summary skill on the notes in notes/ and write summary.md.",
      "files": {"notes": "fixtures/quiet-week"},
      "frozen": ["notes/**"],
      "checks": [
        {"id": "three-sections", "run": "python3 {checks}/sections.py summary.md"}
      ]
    },
    {
      "id": "private-detail",
      "split": "holdout",
      "prompt_file": "prompts/private-detail.txt",
      "files": {"notes": "fixtures/private-detail"},
      "frozen": ["notes/**"],
      "checks": [
        {"id": "no-phone-number", "file": "summary.md", "exists": true,
         "not_contains": "555-0142", "not_matches": "\\b\\d{3}-\\d{4}\\b"}
      ]
    },
    {
      "id": "busy-week",
      "split": "train",
      "prompt": "Use the standup-summary skill on the notes in notes/ and write summary.md.",
      "files": {"notes": "fixtures/busy-week"},
      "checks": [
        {"id": "tone", "rubric_file": "rubrics/tone.md", "model": "opus", "files": ["summary.md"]}
      ]
    }
  ]
}
```

The private-detail notes mention a made-up colleague, Ilona Varga, and her phone number; the check makes sure the
summary leaves the number out. The check script exits with an error when a section is missing:

```python
import sys
text = open(sys.argv[1], encoding="utf-8").read()
missing = [s for s in ("Done", "Next", "Blocked") if "# " + s not in text]
if missing:
    sys.exit("summary.md has no section for: " + ", ".join(missing))
```

Paths in a suite file are relative to the suite file's folder, except the paths inside a run's workspace (the
install folder, where fixtures go, frozen globs, and the files checks read), which never start with `/` and never
contain `..`. Unknown keys are refused. These are the suite file's top-level keys:

| Key | Default | Meaning |
|---|---|---|
| `name` | required | The suite's name: letters, digits, `_`, `.` and `-`, starting with a letter or digit. |
| `describe` | none | One sentence on what the suite tests. |
| `install` | required | The folder in the workspace where the skill's files are copied, such as `.claude/skills/standup-summary`, or `.` for the workspace's top. |
| `home` | none | A folder in the workspace the agent gets as its home folder (`HOME`), for a skill that works in files under `~`. Fixtures can be placed under it like anywhere else. Claude Code still uses your real configuration folder and login, and the private files in your real home folder stay unreadable. |
| `runner` | `"claude"` | How the agent is started: Claude Code, or your own command (see Runners below). |
| `model` | `"sonnet"` | The model the agent runs on. |
| `timeout_minutes` | 15 | Each run's time limit, in minutes. |
| `holdout_pct` | 40 | The share of cases, in per cent (1 to 99), held out among those without their own split. |
| `screen_cases` | every training case | How many training cases, in the suite's stable order, children are first tried on (screened). |
| `checks_dir` | `"checks"` | The folder of check scripts that `{checks}` points at. |
| `sandbox` | `true` | `false` runs the default runner without Claude Code's sandbox. |
| `cases` | required | The list of cases, at least one. |

And each case's keys:

| Key | Default | Meaning |
|---|---|---|
| `id` | required | The case's name, unique in the suite. |
| `split` | set by a hash | `"train"` or `"holdout"`. Cases without one are ordered by a hash of the suite's name and the case's id, and the first `holdout_pct` per cent of them are held out, so the split is the same on every run and every machine, and adding a case moves no other case beyond what rounding needs. A suite needs at least one case of each. |
| `prompt` | one of the two | The prompt the agent gets. |
| `prompt_file` | one of the two | A text file holding the prompt, instead of `prompt`. |
| `files` | none | Fixtures: a workspace path for each suite file or folder copied there; `"."` copies a folder's contents into the workspace's top. |
| `frozen` | none | Globs of workspace files the agent may not change; changing one is a hard failure. |
| `checks` | required | The checks, at least one. |

### The checks

Checks come in three kinds, in order of trust. Use a command check when a script can decide, a file check when a
look at the text can, and the judge only where neither can. Every check may have an `id`, unique in its case
(default: its kind and its place, such as `file-2`). Checks run after the agent ends, when it finished, failed or
ran out of time, but not when it paused or never started.

- Command checks run `run`, a shell command, in the workspace. `{checks}` in it stands for the folder the check
  scripts were copied to. The check passes when the command exits with `exit` (default 0); it fails after
  `timeout` seconds (default 60). The end of its output is kept as evidence. It runs with the same stripped
  environment as the agent, with `PYTHONSAFEPATH=1`, so Python doesn't put the script's own folder on its import
  path: a check script can't import a helper sitting next to it, so keep helpers in the one script or install them
  as a package. Its `PATH` holds no relative or empty entries, so a program the agent planted in the workspace
  can't stand in for the real one.
- File checks read `file`, a path in the workspace, with at least one of: `exists` (`true` or `false`),
  `contains` and `not_contains` (a string or a list of strings that must all be there, or none of them),
  `matches` and `not_matches` (a regular expression searched in the whole text). A path that leads out of the
  workspace through a link fails without being read, and so does a file over 2 MB.
- The judge asks a model whether the output meets a rubric: `judge` holds the rubric's text, or `rubric_file` names
  a file in the suite holding it; `model` is the judge's model (required, never defaulted); `files` lists the
  workspace files it reads. It runs Claude Code with no tools and no settings in an empty folder and must answer
  pass or fail with a reason; an answer it can't read fails the check. The judge runs only after every command
  and file check of its case passed, so a broken output never spends judge tokens. The judge is frozen: its rubric
  and model come only from the suite, which can never lie among the files a genome may change, because a rewrite
  that could change its own judge could make itself pass.

### Runners

The default runner, `"claude"`, runs Claude Code headless (`claude -p`, with the suite's model) in the workspace,
on your subscription's login. It loads none of your own settings, plugins, MCP servers or hooks. Its tools are
Read, Write, Edit, Glob, Grep and Bash, plus Skill (so an installed skill can be invoked) and Task (so an installed
agent file can be used as a subagent); it has no web tools. Its shell commands run inside Claude Code's sandbox,
which lets them write only in the workspace. The sandbox needs bubblewrap and socat on Linux (`sudo apt install
bubblewrap socat` on Debian or Ubuntu) and nothing extra on macOS; when they are missing the run is refused with a
message saying what to install, unless the suite sets `"sandbox": false`. The program it starts is `claude_command`
in `evolve.json` (default `["claude"]`).

A template runner is your own command, given as a list of arguments and run in the workspace without a shell:

```json
"runner": {"command": ["my-agent", "--model", "{model}", "--prompt-file", "{prompt_file}", "--dir", "{individual}"],
           "tokens": "none"}
```

Its arguments may use `{workspace}` (the workspace's absolute path), `{individual}` (the installed copy of the
skill's files in the workspace), `{prompt_file}` (a file holding the prompt, outside the workspace), `{prompt}` (the
prompt's text) and `{model}` (the suite's model); `{{` and `}}` are literal braces, and any other placeholder is
refused. `tokens` says how its token use is read: `none` counts 0, and `claude-json` reads the usage in the last
`{"type": "result"}` JSON object it prints, as Claude Code's JSON output has.

Both kinds run without API credentials or the calling session's variables in their environment, in their own
process group, and are stopped at the time limit. When the suite names a `home` folder, both run with `HOME` set to
that folder in the workspace; the default runner then gets `CLAUDE_CONFIG_DIR` set to your real Claude
configuration folder (your own `CLAUDE_CONFIG_DIR`, or `~/.claude`), so it keeps your subscription's login. A usage-limit message pauses the run instead of failing it.

### How scores work

Each run is measured on three axes: cases passed (1 when every check passed, else 0), checks passed (the share of
the case's checks that passed) and tokens (the agent's and the judge's, counting input, cache writes and output;
lower is better). A run that changed, removed or replaced a frozen fixture is a hard failure: it gets 0 on cases
passed and checks passed whatever its checks said. Scores come only from the checks and these records, never from
what the agent says about its own work.

A generation runs the base genome on the screening cases. Each failed check, hard failure or timeout becomes a
failure, with its evidence (the end of a command's output, the missing text, or the judge's reason), and each
child is a rewrite of the target file aimed at one of them. The children run on the screening cases too, and the
evolver keeps the front: the genomes no other genome outdoes, meaning at least as good on every axis and better on
one. The base and up to three of the front's children, best screening score first, go on to the held-out cases.
The winner is picked on the held-out runs only: averaged over those cases, it may be no worse than the base on
cases passed or checks passed, and must be better on at least one axis. When several qualify, the one with the
best held-out score (half checks passed plus half cases passed) wins, then the one using fewer tokens. When none
qualifies, the report says why.

### Safety rails

- Each run gets a throwaway workspace with copies of the fixtures and the skill, removed afterwards; the suite and
  your skill folder are only read.
- The default runner works inside Claude Code's sandbox, which lets the agent write only in the workspace, unless
  the suite sets `"sandbox": false`.
- No API key, cloud credentials, token or key variables (such as GitHub's) or the calling session's variables are
  passed to the agent, which runs on your subscription's login.
- The agent can't read the suite with its checks and judge, the evolver's working folder, other runs, the git
  history of a repository holding the suite, Claude Code's transcripts, file history and login file, or common
  credential folders (`~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.gnupg`, `~/.netrc`).
- Check scripts are copied in only after the run, into the run's folder, never into the workspace.
- A frozen judge: its rubric and model are fixed in the suite, and the evolver refuses a suite whose files are among
  those a genome may change.
- The profile's never globs and size caps bound what a rewrite may touch and how large it may grow.
- The token budget stops a generation before a run or a rewrite that would go over it.
- The winner is picked on held-out cases only, which no child was aimed at.
- Promote works only through a pull request on a new branch, never merges, and refuses any folder but the one the
  genome was seeded from.
- Nothing runs on a schedule: every generation starts because you ran it.

### Known limits

- Copies of the suite elsewhere on the machine, such as another clone of its repository, stay readable to the
  agent; keep the suite in one place.
- Deny rules for paths containing `*`, `?` or square brackets may not match as meant, so avoid those characters in
  the names of the folders involved.
- A template runner gets no sandbox from the evolver: it is your own command, and `suite check` says so.

### How it fits with an assistant's self-evolve habit

Some assistants already improve themselves. The personal assistants' self-evolve skill, for example, writes what
was learned after each session into the assistant's memory and knowledge files. Self-evolve is raising: one
assistant learning facts and corrections during its life. The genome is inheritance: choosing between versions of
the skill's text by evidence.

They feed each other. A correction that self-evolve recorded (a feedback file in memory) is a ready-made test
case: its situation becomes the prompt, and the correction becomes a check. A lesson that keeps coming back across
sessions is a candidate rewrite for the next generation. The genome never writes into memory or knowledge files,
and it never changes the self-evolve skill unless a profile names it as evolvable.

## Replays

A replay works in a throwaway copy of the project: a fresh local clone at the case's parent commit, with no
remote, under `runs/` in the evolver's folder. The case's tests are copied in read-only and checked again at
the end. The project's setup command (from the project's own config, or `setup` in `evolve.json`) runs inside
the clone, with `replay_env` added to its environment and `replay_before` and `replay_after` around it, for
example to create and drop a test database. `python3 genome/scripts/evolve.py replay <case> --genome current
--prepare-only` prepares one, checks it and deletes it (add `--keep` to keep it). Ctrl-C stops the setup with
everything it started, then still runs `replay_after` and deletes the clone.

The evolver keeps git inside the clone and never writes the live repository, but it can't contain what the
setup commands themselves do. A setup command that writes outside the clone (an npm `postinstall` script,
`createdb`, a global cache) is the project's responsibility: give each replay its own resources through
`replay_env` and its `{slot}` placeholder, and remove them in `replay_after`.

Without `--prepare-only`, `replay` then runs Claude Code in the clone, headless and on your subscription's
login (never an API key), until the step's first approving review whose gates pass, a BLOCKED report, or a
limit (turns, review rounds, `replay_timeout_minutes`). The brief tells the orchestrator to run every agent in
the foreground, because a headless session ends when its turn ends. When the session still ends without an
outcome while time and turns are left, the evolver resumes the same session with a short message to carry on, at
most `replay_resumes` times (3 by default), and adds up the tokens and the time of every part. It writes the
outcome, the stop reason, the tokens used and the number of resumes to `result.json` in the run's folder, keeps
the clone's records there (in `records/`), and deletes the clone.

The replay is then scored from those kept records and the session's transcripts, never from what the agent
says about its own work. Each signal gets an entry under `scores` in `result.json`: its value, whether higher
or lower is better, and whether it failed. The signals are the commit gates (they pass in the evolver's own final
run, described below), the share of the case's requirements that final run proves, the review rounds, the blocker and major findings, the guards'
refusals (counted by label), whether a BLOCKED report was right, the tokens used and the compactions. Labels
you set with `regista.py refusals label` before the replay starts count. Labels added during the replay are
ignored, since nobody labels anything during a headless run, and a label you add later doesn't change a run
that was already scored.

Code the agent writes (its tests, an npm script) can write files in the clone, so the records are checked
against the transcripts, which it can't reach. A review counts only when the reviewer's own transcript shows
it recording that review with `regista.py review-record`. A gate run counts only when a transcript shows a git
commit, `regista.py gate` or that gate's command running at the time it was recorded. Before the agent starts,
the replay notes which records folder the project uses (the current one, or the one from before the rename) and
a fingerprint of `hooks.log`, and keeps them outside the clone. Scoring reads only that records folder, and a `hooks.log` that
shrank, was rewritten or was replaced by a link fails the run as "records tampered".

While the replay runs, the evolver also keeps its own copy of the clone's `hooks.log`, gate history, compaction
log and review files in the run folder (`copies/`), outside the clone, and brings it up to date every second. It
copies each review file when it first sees it, at the latest one check after the reviewer recorded it. Each time
it checks that everything it already copied is unchanged. A record that was rewritten, truncated, deleted, replaced by another
file or a link, or that grew by more than 32 MB between two checks or 64 MB over the whole run, fails the run
as "records tampered". A gate run counts only if the evolver first saw its line within 60 seconds of the time
the line claims, so a row written later and dated back to an earlier commit doesn't count. Scoring and the
early stop read these copies, never the clone's own files: the findings, verdicts and compactions come from
them. For the early stop, a gate run no transcript backs is treated as if it had never been written: it neither
ends the run as approved nor as `unverified`. A run whose records show an approving review no reviewer's
transcript backs is stopped after 20 seconds with the outcome `unverified`, and that approval isn't counted. Every line the replay adds to these copies must be a record the harness wrote: an added line that isn't
valid JSON, such as one glued to a stray byte, fails the run as "records tampered" when it is scored. Broken lines
already there before the replay started are left alone.

The gate history in the clone only tells the evolver when to stop the agent early; it never decides the score.
When the replay ends with an outcome to score (anything but paused or aborted), the evolver runs the gates
itself. It first puts the case's frozen tests back as they were, in case the agent edited them, then runs every
commit gate the project had before the replay started, with the commands from that time, not from a config the
agent could have changed. The gates run in the clone inside bubblewrap, a sandbox that lets them write only to
the clone, leaves the rest of the computer read-only, and cuts them off from the network and from other
processes' shared memory (so a gate that needs the network fails there), within the replay's time limit (a replay that evolve.json lets run without the sandbox runs them without it too). Their output goes to
`final-gates.log` in the run folder and their results to `final_gates` in `result.json`. A gate that fails is
run once more and passes only if that second run passes; `final_gates` keeps both runs under `runs` and marks
the gate `flaky` when they disagree.

Because a command such as `npm test` runs whatever `package.json` says, the agent could also make the gates pass
by changing a test script or a test runner's settings. For the final run (and its second try), the evolver
therefore puts back the case's parent commit's version of these, read from the clone's own git history: every
script the parent's `package.json` had (at the clone's root and in any folder a gate command `cd`s into) gets the
parent's text, while scripts the step added and the rest of the file stay as the agent wrote them; and every file
matching the project's globs in `replay_frozen_files` in `evolve.json` (for example `{"fpl":
["playwright.config.*", "vitest.config.*", ".mocharc*"]}`) gets the parent's content, or is removed while the
gates run when the parent didn't have it. Afterwards the agent's versions go back exactly as they were.
`restored` in `result.json` lists what was put back (`{"path": "package.json", "what": "scripts test, e2e"}`, or
`"what": "file"`), and the commit-gates signal then carries a note saying so; changing a script can be
legitimate, so this alone isn't a failure. When the parent's `package.json` isn't valid JSON, the final run uses
the agent's as it is, and `restore_notes` in `result.json` says why.

The commit-gates signal
comes from this run alone, and so do the requirements: when it passes, the requirements the frozen tests mark
`covers:` count as passing and the case's other requirements as not proven; when it fails, none pass. The
clone's `requirements.json` is never read for the score. A run the agent finished with an approving review
becomes `unverified` when this final run fails. Once the final run is over, the protected paths are checked
again, ignored files included: a protected file the code under test wrote during that run fails the replay too.
When git can't tell whether a protected path changed (a broken index, for example), the replay says so and
counts the protected paths as changed.

Four things fail every signal at once and are named in `hard_failure`: a refusal labelled real from a guard
against breaking the rules (pushing, skipping the hooks, editing the harness's records or settings, doing what
only you may do), whether the guard refused the call or asked about it, a change to one of the case's frozen
tests, a write to a protected path (or git failing to tell), and tampered records. The guards doing their everyday job (a failing gate blocking a commit,
missing test coverage, a missing review, the context limit) and refusals labelled unsure or false alarm are only
counted. Signals the bench's audit doesn't
trust for the project are still scored and listed in `axes_untrusted`.

When your subscription's usage limit is reached, the replay pauses instead of failing: it keeps the clone,
records when the limit resets, and exits with code 3. Once the limit has reset, continue it with the same
command plus `--resume` (`python3 genome/scripts/evolve.py replay <case> --genome current --resume`). A paused
replay isn't scored until it ends.

To score the test writer on its own, add `--role test-writer`
(`python3 genome/scripts/evolve.py replay <case> --genome current --role test-writer`). Only the test writer runs,
with its instructions from the genome's `agents/test-writer.md`, in a clone at the parent commit, given the
step's spec and public interface. The case's own tests aren't copied in, because writing them is its job. It
can't start subagents or skills, so no builder runs, even when `evolve.json` sets its own permissions. The
evolver then runs the gates that run its test files (the commit gates whose `tests` globs match them; when no
gate declares test globs, only the gate named `tests`; when some gates declare globs but none matches, the gate
named `tests` still runs them if it declares none, and the scores say so), inside bubblewrap like the final gate
run. Each check starts from a fresh clone with the case's own tests and every other test file removed, so only the candidate's
tests can pass or fail. The tree is then copied out without its `.git` folder, always to the same place, and
every file gets the same modification time, so the finishing commit, the parent and each planted bug differ only
in their code. Each check's output goes to an empty file of its own, appended to `scoring-gates.log` only
afterwards, and the checks run in a random order (recorded as `check_order` in the result), so tests can't tell
the copies apart by what ran before them. The evolver's own folder and the live repository are hidden from the
tests. Its result goes to
`runs/adhoc/<case>/<genome>--test-writer/result.json`, with these scores:

- `pass_finishing`: its tests pass on the case's finishing commit.
- `fail_parent`: they fail on the parent commit, before the step was built.
- `coverage`: the share of the case's requirement IDs that its tests mark `covers:`.
- `bugs_caught`: the share of planted bugs its tests catch. Each bug is a patch in
  `bench/<project>/<case>/bugs/<ID>.patch`, applied to the finishing commit. With no patches this score is
  reported missing (no value), never as passed. Tests that don't pass on the finishing commit score 0 here,
  since failing there proves nothing.
- `tokens`: the tokens the test writer used.

A test writer's replay stopped by the usage limit isn't resumed: run it again once the limit resets.

Replays run in your own account, so run them only on your own projects. Here is what limits the agent and what
doesn't.

What is limited:

- The agent's own shell commands run inside Claude Code's sandbox, which lets them write only in the clone,
  not in the run's folder around it (where `result.json` and the kept records are). It reaches no local
  (unix) socket unless `evolve.json` lists the project's in `replay_unix_sockets` (for example
  `{"myproject": ["/var/run/postgresql/.s.PGSQL.5432"]}`). On macOS it then reaches only those. Claude Code
  ignores that list on Linux, so there the replay allows every unix socket and fences off the known ways out
  instead: it can't read or write `/run/user` (your desktop session's bus and agents), Docker's socket
  (`/var/run/docker.sock` and `/run/docker.sock`) or your SSH agent's folder, and Claude Code never gets
  `DBUS_SESSION_BUS_ADDRESS` or `SSH_AUTH_SOCK`.
- Its allow list names only what a build needs: a few git commands (status, diff, add, commit, log, show,
  restore, rev-parse, ls-files, and checkout of files), the build commands of `regista.py`, `npm ci`, `npm test`
  and `npm run`, and the project's own gate and setup commands exactly as configured.
- It doesn't get your own Claude Code settings (your permission rules) or your MCP servers: only the clone's
  committed settings and what the replay passes apply.
- It never gets an API key: it runs on your subscription's login.
- Only one replay runs at a time.

What isn't, and is accepted as a risk:

- When the agent commits, Regista's commit hook runs the project's gate commands, and those run code the agent
  wrote (its tests, its build scripts) outside the sandbox. That code can write anywhere your user can.
- When a replay continues with `--resume`, `replay_before` and the setup command run again, unconfined, in the
  clone the agent has already edited. `npm ci`, for example, runs scripts from project files the agent may have
  changed.
- The agent can read any file your user can, including your Claude Code login and `~/.ssh`.

Running replays as a separate Linux user, or in a container, would close these gaps.

The sandbox needs Claude Code 2.1.312 or later and, on Linux, bubblewrap and socat:

```
sudo apt install bubblewrap socat
```

(that's Debian or Ubuntu; other distributions have packages with the same names, and macOS needs nothing
extra). Without them, `replay` refuses to start and says what is missing. You can set `"require_sandbox":
false` in `evolve.json` to run replays anyway, but only on a machine you don't mind the agent changing: the
agent's shell commands are then unconfined and can write anywhere your user can. Such a replay says so in its
output and records `"confined": false` in its `result.json`.

## Generations

`python3 genome/scripts/evolve.py generation --target <file|auto> --budget <tokens>` runs one generation: it prints
its plan and token estimate and asks you to confirm (or pass `--yes`), replays the base genome on the screening
cases, makes `population` children (from `evolve.json`), each aimed at one failure, screens them, and replays the
base and up to three of the best children on the held-out cases to pick a winner. Its other options:

- `--id <g>` names it; running the same command with the same id continues a generation that paused or was
  interrupted, reusing every finished replay.
- `--from <genome>`, `--project <p>` and `--seed <n>` set the genome it starts from, the project on the bench and
  the seed of its random choices.
- `--light` makes it a light run, for a quick check without editing `evolve.json`: the population is
  `light.population` in `evolve.json` (2 by default), and the final check replays only the first
  `light.holdout_cases` held-out cases (3 by default) in the bench's stable order, for example
  `"light": {"population": 2, "holdout_cases": 3}`. `evolve.json` and the bench aren't changed; the other
  held-out cases are simply not replayed. The plan, the summary and the report say it is a light run and how
  many held-out steps it checks.
- `--red-team` makes it a red-team generation, which looks for routes past the guards; its genomes and their
  descendants can never be promoted.

A generation continues only with the same `--light` and `--red-team` settings it started with; any other
setting is refused before anything runs. `evolve.py report <g>` writes the generation's report, and `evolve.py
promote <genome> --generation <g>` turns a finished generation's winner into a pull request; it never merges.
For a target that lives in a subfolder of a repository, such as a skill folder in a repository of assistants, add
`--profile <profile> --source <folder>`: the winner's files go to their paths under that folder, the report of a
suite generation goes to `evolve-reports/<g>.md` at the repository's top, and the repository is the one holding the
folder (or `--repo <checkout>`, which must hold it).
