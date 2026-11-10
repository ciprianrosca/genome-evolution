# The genome: evolving the text files an agent reads

The genome improves the text files an agent reads (a skill, an agent file, a rules file) by testing the agent on a
suite of cases you write and keeping the rewrites that do better. The tool is `scripts/evolve.py`, a standard-library
Python script. It only reads your files and never changes them: a winning rewrite reaches your repository only as a
pull request that you read and merge yourself. Its working files (runs, reports, genomes) live in
`~/.regista-evolve/`, or in the folder named by the environment variable `REGISTA_EVOLVE_HOME`, with its settings
in `evolve.json` there. (The folder's name is a leftover from the project this grew out of.)

## Getting started

Clone this repository anywhere and run everything from its top folder. You need Python 3, git and Claude Code
(logged in to your subscription). To check that your copy works, run the tests:

```
python3 -m unittest discover -s tests -t tests
python3 -m unittest discover -s tests/e2e
```

`python3 scripts/evolve.py config` prints the settings in use.

## Evolving any skill or agent

The same evolver can improve any set of text files an agent reads: a skill folder, an agent file, a rules file.
You write a suite of test cases for it, and the evolver runs the agent on those cases with the text as it is, then
makes rewritten versions (children), each aimed at one check that failed, and runs them too. The winner is chosen
only by how it does on held-out cases, which no child was aimed at, and it reaches your repository only as a pull
request that you read and merge yourself.

A few words used below. A genome is one stored version of the files the evolver may change. An individual is a
complete copy of the target's files built from a genome, the thing an agent actually reads during a run. A run is
one case played once by one individual, in a throwaway folder called the workspace.

### How to evolve a skill

The example evolves a made-up skill, `standup-summary`, which tells an assistant how to turn a week of notes into a
short standup summary. It lives in `skills/standup-summary/` of a git repository at `~/assistants`, and its profile
and suite live beside it in `evolve/`, outside the skill folder. The commands run from the top folder of this repository, and these shell variables keep
them short:

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
   python3 scripts/evolve.py suite check "$suite" --profile "$profile" --source "$skill"
   ```

4. Try one case with the skill as it is now. This runs the agent for real, on your Claude subscription, on a copy
   of the skill folder (the folder itself is never changed). It prints the outcome, the tokens used and one line
   per check, and writes `result.json` under `runs/adhoc/<case>/individual/` in the evolver's working folder. Its
   exit code is 0 when the agent finished, 3 when it paused at the subscription's usage limit and 1 otherwise; add
   `--keep` to keep the workspace for a look.

   ```sh
   python3 scripts/evolve.py suite run "$suite" quiet-week --individual "$skill"
   ```

5. Seed the first genome from the skill folder. The default id is `seed`; give another one with `--id` when the
   working folder already holds a seed for another target.

   ```sh
   python3 scripts/evolve.py genome seed --id standup-seed --profile "$profile" --source "$skill"
   ```

6. Run a generation on one evolvable file (`--target`). It prints its plan and token estimate and asks you to
   confirm (`--yes` skips the question). `--budget` is the most tokens it may spend (default: `budget_tokens` in
   `evolve.json`, 20 million). `--from` names the genome it starts from (default `seed`). `--light` makes a quick
   run with 2 children and only the first 3 held-out cases. `--id <generation>` names it, and running the same
   command with the same id continues a generation that paused or was interrupted.

   ```sh
   python3 scripts/evolve.py generation --suite "$suite" --profile "$profile" --source "$skill" \
       --target SKILL.md --from standup-seed --budget 2000000 --light
   ```

7. Read the report. It prints the generation's report (the winner or why there is none, each candidate's scores,
   the spend) and saves it under `reports/` in the evolver's working folder.

   ```sh
   python3 scripts/evolve.py report <generation>
   ```

8. Promote the winner. This creates a new branch in the repository holding the skill with the winner's changed
   files at their place under `skills/standup-summary/` and the report at `evolve-reports/<generation>.md`, runs
   the profile's checks in a temporary worktree, pushes only that branch and opens a pull request against the
   current branch (or `--onto <branch>`); `--no-pr` stops after the checks. It never merges, and it refuses a
   `--source` other than the folder the genome was seeded from.

   ```sh
   python3 scripts/evolve.py promote <winner> --generation <generation> --profile "$profile" --source "$skill"
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
| `home` | none | A folder in the workspace the agent gets as its home folder (`HOME`), for a skill that works in files under `~`. Fixtures can be placed under it like anywhere else. Claude Code still uses your real configuration folder and login, and nothing else in your real home folder is readable to the agent. |
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
- Nor can it read anything else at the top of your real home folder, such as your other projects, except Claude
  Code's configuration folder (`~/.claude`, which its shell needs). When the suite gives the agent no home folder of
  its own, the shell's usual files (`.bashrc`, `.profile`, `.gitconfig`, `.cache`, `.local` and the like) stay
  readable too, so commands keep working. This keeps a run from reading your real files by their full path, even
  when the skill names a folder under `~` that also exists in your real home.
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
