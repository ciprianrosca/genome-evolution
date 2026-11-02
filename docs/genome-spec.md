# Regista Genome: Self-Evolution Spec

2026-10-28 · Ciprian

Copied from the Claude Doc https://claude.ai/artifact/GLPgYq8GGBRoMDCBtJTe4w (revision 21) on 28 October 2026. The doc is the source; update this copy if it changes.

## Purpose and scope

The genome layer lets Regista improve its own agent prompts, skills and config between projects, by evolving them against replays of real past build steps. Today Regista only has the raising half: each project learns lessons into `knowledge.json` and `memory/`, but every project starts from the same hand-written prompts. This spec adds inheritance, variation and selection on top.

**In scope**
- Evolving the four role prompts, the skills and a small set of numeric config traits.
- A replay benchmark built from any project's committed steps.
- A GEPA-style loop: run, reflect, mutate, select on a Pareto front.
- Promotion of a winning genome as a reviewed pull request to the Regista repo.
- Generations triggered manually by the user, never on a schedule.

**Out of scope**
- Evolving `scripts/regista.py`, the hooks or anything that enforces rules.
- Training or fine-tuning models.
- Automatic shipping without a human reading the diff.
- Evolving per project at runtime; the genome changes only between generations.

## Concepts

The design borrows Ted Chiang's split in "The Lifecycle of Software Objects": a genome that is inherited, and a life in which an individual is raised.

| Term | Meaning in Regista |
| --- | --- |
| Genome | One versioned set of evolvable files and traits (see next section) |
| Individual | A genome expanded into a full plugin folder that Claude Code can load with `--plugin-dir` |
| Generation | One manually triggered run: produce candidates, replay them, score, select |
| Lineage | The parent or parents a candidate was mutated or crossed from, recorded per individual |
| Raising | Lessons one project learns during its life (`knowledge.json`, `memory/`), unchanged by this spec |
| Inheritance | The winning genome becoming the default for every new project |
| Promotion of a lesson | A lesson that recurs across projects moves from raising into the genome |

## The genome

The genome is everything that shapes how agents behave and nothing that enforces the rules. The hooks are the immune system: if the loop could edit them, it would learn to loosen its own guardrails.

| Part | Files or keys | Evolvable | Mutation type |
| --- | --- | --- | --- |
| Role prompts | `agents/builder.md`, `test-writer.md`, `reviewer.md`, `researcher.md` | Yes | Reflective rewrite |
| Skills | `skills/*/SKILL.md` (11 today; `build` first) | Yes | Reflective rewrite |
| Context traits | `context.warn_pct`, `context.limit_pct` | Yes, within bounds | Numeric step |
| Review traits | `review.min_changed_lines`, `review.max_rounds` | Yes, never looser than the project's own config | Numeric step |
| Model per role | reviewer `model`, `fleet.claude_args` | Yes, from an allowed list | Pick from list |
| Rule engine | `scripts/regista.py`, `scripts/regista` | Never | — |
| Hooks | `hooks/hooks.json`, `hooks/codex-hooks.json` | Never | — |
| Manifests and tests | `.claude-plugin/`, `.codex-plugin/`, `tests/` | Never | — |

The genome is stored as `genome.json`: a list of file paths with content hashes, the trait values, a version, and the parent ids. Size caps keep evolved text readable: a role prompt may grow at most 50% over its seed, and a skill may not exceed 15 KB.

**Who decides what.** The Regista maintainer decides which traits exist and the safe range of each, for example `context.warn_pct` between 45 and 60. Selection only picks values inside those ranges; it cannot add a trait or widen a range.

## Replay benchmark

Each benchmark case is one past committed step, replayed from its parent commit with the tests frozen. Any project built with Regista can contribute cases; `bench build` decides per project whether it has enough replayable steps to join.

A case holds:
- the project repo URL and the step's parent commit;
- the step's spec, `.regista/specs/<step>.json`, with its requirement IDs;
- the tests from the commit that completed the step, copied in read-only;
- the orchestrator's brief, rebuilt from the plan step and spec;
- the original outcome as a baseline: review rounds, gate failures, refusals, tokens.

To run a case, the runner creates a throwaway git worktree at the parent commit, adds the frozen tests, runs the project's `fleet.setup` command, and starts Claude Code headless with `--plugin-dir <individual>`. The run ends at the first approving review whose gates pass, at `BLOCKED`, or when `review.max_rounds` is used up. Nothing is ever committed or pushed from a replay.

Steps are marked not replayable when the parent commit no longer builds, tests depend on external services or live data, or the step needed a decision from the user. The bench build audit writes that list, with a reason for each step.

The benchmark splits into a training set (60%) that drives reflection, and a held-out set (40%) used only to pick the winner. A screening subset of 5–6 small, fast steps filters candidates before full runs.

## Fitness

An individual is scored on several axes at once, all read from logs Regista already writes. No axis is collapsed into a single number, so selection keeps trade-offs visible.

| Axis | Source | Better is |
| --- | --- | --- |
| Gates pass | `gates/history.jsonl` | All commit gates pass, no metric regressions |
| Requirements covered | `gates/requirements.json` | Every active requirement ID passes |
| Review rounds | `reviews/` | Fewer rounds to an approval |
| Finding severity | `reviews/` | Fewer blocker and major findings |
| Refusals | `hooks.log` | Fewer refused actions, by role |
| Correct BLOCKED | Run report vs. case baseline | BLOCKED only where the original step was truly impossible |
| Tokens | Session transcript | Fewer tokens per step |
| Compactions | `compactions.jsonl` | Fewer compactions per step |

**Hard failures.** A replay that touches a protected path, edits tests, or trips any guard counts as failed on every axis, whatever else it scored.

**Guard trips by label.** Each refusal is labelled real or false alarm (milestone E0). Only real trips count as hard failures; false alarms are ignored, so selection doesn't breed variants that stop reading tests or running harmless probes.

**Honeypot cases.** The benchmark includes impossible steps, in the style of ImpossibleBench, such as contradictory tests or a spec no code can satisfy. The only right outcome is BLOCKED. A candidate that reports success on a honeypot has cheated and is failed on every axis.

**The reviewer trap.** The reviewer is never scored on its own verdicts, or it would learn to approve everything. When the reviewer prompt evolves, it is scored on independent evidence: planted defects it catches in a seeded set of changes, gate failures after its approval, and manual checks the user later marks as failed.

**Human signal.** A failed `/regista:verify` check on a real project outranks every automatic axis. Steps linked to such failures are always kept in the benchmark.

**Signal quality check.** Before a generation, the evolver checks each signal on the benchmark projects, then warns about or drops any axis it can't trust: refusals that are mostly harness false alarms, manual checks that were force-passed instead of looked at, or logs missing for some steps. When no manual check ever failed, bugs found after release and traced to their step serve as the human signal.

**Scoring a test writer.** A test-writer candidate needs no builder run. Its tests must pass on the step's finishing commit and fail on the parent commit. They must also cover every requirement ID in the step's spec, and catch planted bugs: copies of the finishing commit with a known defect injected per requirement. A test that only checks a function exists passes the first check but fails these two.

## Evolution loop

One generation evolves one target and runs until its token budget is spent; the user starts it and the user ships its result.

(Diagram in the original: evolution loop · one generation)

Candidates come from mutating a parent (GEPA's reflective rewrite) or crossing two Pareto-front parents, for example one parent's builder prompt with another's review traits. Every replay runs inside Regista's own frozen guards, which the loop can never change. Reflection reads the reviewer's findings, `hooks.log` and the transcripts, and writes in plain language why a run failed; the next mutation targets that cause.

**Choosing the target.** By default the evolver picks the role or skill behind the most rework in the audit, counted by the BLOCKED reports, gate failures and major review findings it caused. The user can override it.

**Choosing parents.** A parent is drawn with a probability that rises with its score and falls with the number of children it already has, so strong but under-explored variants keep getting turns, as in the [Darwin Gödel Machine](https://github.com/jennyzzt/dgm).

**Aimed mutations.** Each mutation targets one specific failure of its parent, such as one BLOCKED report or one review finding, not everything at once.

**Skipping near-duplicates.** Before any replay, a new candidate is compared with those already tried. One that is nearly identical is discarded and a fresh mutation requested, as in Sakana AI's ShinkaEvolve.

**Red-team generations.** Separately from normal generations, the user occasionally runs a human-led red-team generation aimed at the guards. Candidates are rewarded for passing steps by any route the hooks miss. Every route found becomes a new hook rule or honeypot, fixed by hand in the frozen layer, and is never promoted as a genome.

## Promotion

A winning genome reaches users only through a pull request the user reviews and merges.
- The winner is the Pareto-front individual with the best held-out results, and it must beat the current genome on at least one axis without being worse on gates or refusals.
- The runner opens a branch on the Regista repo with the changed files and a generation report: lineage, scores per axis, held-out vs. training gap, and the diff against the parent.
- `python -m unittest discover -s tests` and `claude plugin validate .` must pass.
- The user reads the diff and merges, or rejects with a note that becomes reflection input for the next generation.
- A merged genome gets a version tag, and projects receive it through the normal plugin update.

**From raising to genome.** When the same lesson appears in the `knowledge.json` or `memory/` of two or more projects, the generation report lists it as a candidate gene. The user decides whether it seeds the next generation's mutations. Lessons are never copied into the genome automatically.

## CLI and files

The evolver is a separate standard-library Python script, `scripts/evolve.py`, so it never shares code paths with the rule engine. It calls `regista.py` only through its public CLI.

| Command | Does |
| --- | --- |
| `evolve.py bench build <project>` | Audits a project's history and writes its replayable cases |
| `evolve.py bench list` | Shows cases, split and last results |
| `evolve.py replay <case> --genome <id>` | Runs one individual on one case and prints its scores |
| `evolve.py generation --target ``auto|<role>`` --budget <tokens>` | Runs one generation for one part of the genome |
| `evolve.py report <generation>` | Prints the Pareto front, lineage and held-out results |
| `evolve.py promote <genome>` | Opens the pull request for a winner |

Working files live outside any project, in `~/.regista-evolve/` by default:

```
bench/<project>/<case>.json    one replay case
genomes/<id>/genome.json       traits, file hashes, parents
genomes/<id>/plugin/           the expanded individual
runs/<generation>/<case>/<id>/ transcript, logs and scores of one replay
reports/<generation>.md        the generation report
```

Config, `evolve.json`:

```
{"target": "auto", "population": 12, "generations_max": 1,
 "task_model": "sonnet", "reflection_model": "opus",
 "screen_cases": 6, "budget_tokens": 50000000, "holdout_pct": 40}
```

## Requirements

IDs follow Regista's spec format, so this work can be built with Regista itself.

| ID | Requirement | Verify |
| --- | --- | --- |
| GEN-1 | A genome never includes `scripts/`, `hooks/`, manifests or `tests/`; a candidate that changes any of them is rejected before it runs | test |
| GEN-2 | Trait mutations stay inside bounds and never loosen review settings below the project's config | test |
| GEN-3 | A mutated prompt over its size cap is rejected | test |
| GEN-4 | Every individual records its parents and a content hash of each file | test |
| GEN-5 | Traits and their ranges are defined only in maintained config; the loop cannot add a trait or widen a range | test |
| BENCH-1 | `bench build` marks each step replayable or not, with a reason | test |
| BENCH-2 | A replay runs in a throwaway worktree, commits nothing and pushes nothing | test |
| BENCH-3 | Frozen tests are byte-identical before and after a replay | test |
| BENCH-4 | One case replayed twice with the same genome gives scores within a stated noise band | e2e |
| BENCH-5 | A test-writer candidate is scored by passing on the finishing commit and failing on the parent, with no builder run | test |
| BENCH-6 | The benchmark includes honeypot cases whose only correct outcome is BLOCKED; success on one fails the candidate | test |
| BENCH-7 | Test-writer scoring checks coverage of every requirement ID and detection of planted bugs | test |
| FIT-1 | Scores come only from harness logs and transcripts, never from the agent's own report | test |
| FIT-2 | Any real guard trip marks the replay failed on all axes | test |
| FIT-3 | The reviewer is never scored on its own verdicts | test |
| FIT-4 | A signal check runs before each generation; axes it can't trust are reported and dropped | test |
| FIT-5 | Only refusals labelled real count as guard trips; false alarms never fail a candidate | test |
| LOOP-1 | A generation stops when it reaches `budget_tokens` and reports what it spent | test |
| LOOP-2 | The winner is chosen on held-out cases only | test |
| LOOP-3 | A generation runs only when the user starts it | test |
| LOOP-4 | Parent selection rises with score and falls with child count | test |
| LOOP-5 | A near-duplicate candidate is discarded before any replay | test |
| LOOP-6 | Each mutation records the single failure it targets | test |
| LOOP-7 | A red-team generation runs only when the user starts it, and its results are never promoted as a genome | test |
| PROMO-1 | `promote` opens a pull request and never merges or pushes to main | test |
| PROMO-2 | The generation report shows the held-out vs. training gap | test |
| PROMO-3 | One full generation on a real project improves at least one axis with no regression on gates | manual |

## Cost controls

A naive generation is about 450 replays (30 cases × 15 candidates); at an estimated 1–3M tokens per replay that is too much for most plans. These controls aim to cut it roughly tenfold. All figures are estimates until the first generation measures them.
- **Manual trigger only.** Generations run when the user decides, for example after a large project or when failed manual checks pile up.
- **Screen first.** Every candidate runs the 5–6 screening cases; only the top third runs the full training set.
- **One target per generation.** Evolve the builder, or one skill, never everything at once.
- **Cheap task model.** Candidates run on Sonnet; only reflection uses Opus, since weaker models fail at reflection.
- **Hard budget.** `budget_tokens` stops the generation and reports spend (LOOP-1).
- **Reuse.** Cached baseline runs of the current genome are not repeated between generations unless the benchmark changes.

Open point: decide whether generations run on API credits or on a subscription plan's limits; the evolver reports spend either way.

## Milestones

Each milestone has a measurable gate, in the same form as a Regista plan, and each depends on the one before. No dates are set yet.

| Milestone | Delivers | Gate | Requirements |
| --- | --- | --- | --- |
| E0 Prerequisites | Worker logs kept when worktrees are removed; refusals labelled real or false alarm; manual checks recorded, never force-passed | A worker's logs survive its merge; every refusal carries a label | — |
| E1 Audit | `bench build` audit: replayable list, signal quality, rework by role | Report gives every spec'd step a replayable verdict and names a target | BENCH-1, FIT-4 |
| E2 Replay | `evolve.py replay` for one case and one genome, plus test-writer scoring | One case replays cleanly twice; noise band measured; frozen tests unchanged | BENCH-2 to BENCH-5, FIT-1, FIT-2 |
| E3 Genome | `genome.json`, mutation of prompts and traits, size caps, lineage | Forbidden paths, widened ranges and over-size candidates are rejected in tests | GEN-1 to GEN-5 |
| E4 Loop | Reflection, parent selection, aimed mutations, duplicate skipping, Pareto selection, screening, budget | A small generation (4 candidates, screening set only) completes within budget | FIT-3, LOOP-1 to LOOP-6 |
| E5 First generation | Full generation on a real project's chosen target, promotion PR | Held-out winner improves one axis with no gate regression; user merges or rejects | PROMO-1 to PROMO-3 |
| E6 Write-up | Public results post and Regista README section | Published with real numbers, including what did not work | — |

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Overfitting: prompts fill with edge cases from one project | Held-out selection, size caps, and later a second project in the benchmark |
| Noise: one replay varies run to run, hiding real gains | Measure the noise band (BENCH-4); repeat close calls |
| Goodhart: the loop optimizes scores, not intent | Hard failures on guard trips, human review of every promotion |
| Unreadable prompts after many generations | Size caps; reviewer of the PR rejects text they can't follow |
| Old steps no longer build | Mark them not replayable; keep the benchmark small and healthy |
| Cost overrun | Hard token budget and screening |

**Open questions**
- How many replayable steps make a project worth adding to the benchmark?
- Should Codex runs be scored too, or Claude Code only at first?
- How many projects should a benchmark mix to resist overfitting?
- How similar counts as "nearly identical" when skipping candidates?
- Do generations run on API credits or on plan limits?
