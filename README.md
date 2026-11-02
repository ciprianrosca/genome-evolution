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
