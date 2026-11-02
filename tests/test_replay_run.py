"""One replay run headless (spec .regista/specs/replay-run.json, research .regista/research/m3-replay.md).

`evolve.py replay <case> --genome current` starts `claude` in a throwaway clone. Here `claude` is a stand-in
script (evolve.json `claude_command`) written by the tests into a temporary folder: it records its arguments, its
working folder and the NAMES of its environment variables, and plays a scenario read from a control file:

- success: init event, assistant events, a transcript with repeated message ids, an approving review and a backed
  passing gate run in the clone's .regista/, then it waits to be stopped (SIGINT ends it with a result).
- api_key: an init event whose apiKeySource is ANTHROPIC_API_KEY, then it waits.
- stubborn: waits and ignores SIGINT, so only SIGTERM ends it (the time limit test).
- rate_limit: a rejected rate_limit_event with a reset time, then an error result, exit 1.
- limit_text: only an error result whose text says the weekly limit was hit, exit 1.
- hang: an init event, then a `sleep` child in claude's process group, then it waits (the SIGTERM/SIGHUP tests).
- swap_clone: moves the clone aside, puts a symlink to a folder outside the run folder in its place, ends.
- evidence: like success, plus a compaction record and a refusal labelled real in the clone's hooks.log, then ends.
- log_truncate, log_rewrite: append a real push_policy refusal to the clone's hooks.log; on the evolver's stop
  signal, or once the evolver's copy (a hooks.log in the run folder, outside the clone) holds it, or after 30 s,
  truncate the log back to its size at the start, or rewrite that line in place (push_policy becomes gate_failed,
  same length), then end.
- log_junk: append 33 MB of padding lines and then a real push_policy refusal to hooks.log at once, then end.
- gate_rewrite: a commit backed by a Bash call in the transcript, a failing gate row for it and an approving review;
  once the evolver's copy of history.jsonl holds the row (or after 30 s), the row is rewritten in place to pass.
- gate_backdate: a backed commit, then 70 s later a passing gate row that claims the commit's time, and an
  approving review, then it ends.
- final_honest, final_fake, final_edit, final_swap, final_git (FIT-8): a backed commit, a passing gate row for every
  commit gate, gates/requirements.json passing every requirement and an approving review whose reviewer subagent
  ran review-record for it; then it waits (control `wait`, 60 s) for the early stop. What the commit holds: honest,
  double() done right; fake, only work.txt (the stub still raises); edit, double() returning 2n+1 and the frozen
  test rewritten to expect 9 (control file `edited_test`); swap, the stub and the clone's config.json pointing the
  tests gate at a command printing agent-gate-7f3a; git, double() done right, and when it stops it overwrites the
  clone's .git index with garbage.
- final_flaky (REP-10): like final_honest, but the committed src/double.py counts its imports in
  control/flaky_imports.txt and returns 2n+1 for the first `fail_first` (control file, default 1) of them, so the
  final run's tests gate fails that many times and then passes.
- resume_approve (REP-9): the first call ends at once with only msg_a in the transcript (no outcome); a call with
  --resume <the same session id> then does what final_honest does (backed commit, passing gate row, requirements,
  backed approving review) and waits for the early stop. Each part logs itself in control/parts.jsonl.
- no_outcome (REP-9): every call, first or resumed, ends at once with only msg_a in the transcript.
Called with --resume, every scenario but success, resume_approve and no_outcome only sends init and a result and ends.
- log_glued (FIT-8): appends one stray byte to hooks.log, then a refusal line (coverage, unsure) glued to it.
- final_env (FIT-9): like final_honest, but the committed src/double.py, when imported, writes .env at the
  clone's root and logs the time in control/env_writes.jsonl; only the evolver's final gate run imports it.
- gate_unbacked (FIT-6, the early stop): no commit; a passing gate row for HEAD dated now and a backed approving
  review, then it waits (control `wait`, 40 s). Control `backing`: empty, no call ran the gates; harness, a
  Bash call running `regista.py gate` at the row's time; own, Bash calls running each commit gate's command.
A control file `tamper` makes any scenario first point the clone's .regista/config.json fleet.setup (committed) and
any workspace.json inside the clone at another command, as a replay agent could.
Control files also set the api_key source (api_key_source), ignoring SIGINT (ignore_sigint) and which gates the
history covers (gates_only); the transcript goes under CLAUDE_CONFIG_DIR when it is set.
Each start records the time, its pid and, when the control file `watch` names a pid file, whether that pid runs.

COST-1: no API credential or parent-session variable reaches claude, no bypassPermissions unless evolve.json
asks, abort on an API key source, early stop (SIGINT, then SIGTERM after 30 s) with the stop reason and the
tokens (each message id counted once). COST-2: the usage limit pauses the replay (exit 3, workspace kept, reset
time recorded) and `replay --resume` continues it. REP-4: SIGTERM or SIGHUP to the evolver behaves like Ctrl-C,
setup's background leftovers are stopped before claude starts, and a clone swapped for a symlink isn't followed.
REP-5: the default permissions confine claude to its clone, the clone's records are kept, any key source but the
login stops the run at once, replay_env can't bring credentials back, partial gate runs don't pass, and
transcripts are found under CLAUDE_CONFIG_DIR. FIT-7: the evolver keeps its own append-only copy of hooks.log and
the gate history and checks it on every poll, so a rewrite, truncation or oversized growth during the run is the hard
failure 'records tampered', and a gate row first seen long after the time it claims doesn't count. REP-6: without Claude Code's sandbox runtime (bubblewrap and socat on
Linux) `replay` refuses to start (exit 2) unless evolve.json sets require_sandbox false, in which case the replay
says it ran unconfined; the allow list names only the git, regista.py and npm commands a build needs. REP-7: a
resumed replay runs the setup recorded when it first started, not what the agent wrote into the clone; npm -C is
refused like --prefix; require_sandbox accepts only true or false. REP-9: the brief says to run every agent in the
foreground, and a session that ends without an outcome is resumed (claude -p --resume <session id>, same arguments)
at most replay_resumes times, tokens summed across the parts and the resumes counted in result.json. REP-10: a gate
that fails in the final run is run once more; it passes only if the second run passes, and is marked flaky.

Every replay world puts stand-in `bwrap` and `socat` scripts first on PATH, so the sandbox counts as available
unless a test gives the world a PATH without them. The stand-in bwrap answers --version; otherwise it applies
--chdir, --setenv, --unsetenv and --clearenv, runs the command (after `--`, or after the last option) for real
outside any sandbox, exits with its exit code, and records its arguments, the command, the exit code and the text of
tests/unit/test_double.py as the command saw it in control/bwrap.jsonl. FIT-8: the final gate run restores the
frozen tests, runs the baseline's commit gates through bwrap with write access to the clone only, and alone decides
the gates and requirements axes; a hooks.log line glued to a stray byte, or git failing on the protected-path
check, is a hard failure. The FIT-8 tests assume each gate's command string appears in bwrap's arguments.

Run: env -u CLAUDE_PROJECT_DIR python3 -m unittest tests.test_replay_run -v"""
import datetime
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


GENOME = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # the genome folder
REGISTA_CHECKOUT = os.path.dirname(GENOME)  # the Regista checkout that holds the genome: reached only by path
EVOLVE = os.path.join(GENOME, "scripts", "evolve.py")
PY = sys.executable
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
STEP = "m1-double"
DEDUP_USED = 395      # input + cache creation + output, each message id once, main transcript plus subagent
DOUBLE_COUNTED = (510, 563)  # the same with the repeated lines counted again (main only, or main and subagent)

STANDIN = r'''#!@PYTHON@
import json, os, re, signal, subprocess, sys, time, uuid
CONTROL = @CONTROL@

def log(name, entry):
    with open(os.path.join(CONTROL, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def emit(event):
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except Exception:
        pass

def read(name, default=""):
    try:
        with open(os.path.join(CONTROL, name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default

args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

if not any(a in ("-p", "--print") for a in args) and any(a in ("--version", "-v", "--help", "-h") for a in args):
    log("probes.jsonl", {"argv": args, "time": time.time()})
    print("2.1.312 (Claude Code)" if any(a in ("--version", "-v") for a in args) else "Usage: claude [options] [prompt]")
    sys.exit(0)

scenario = read("scenario", "success")
cwd = os.getcwd()
sid = opt("--session-id") or opt("--resume") or str(uuid.uuid4())
def alive(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except Exception:
        return False
def watched():
    path = read("watch")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return {"pid": None, "alive": None}
    return {"pid": pid, "alive": alive(pid)}
def git(*a):
    return subprocess.run(["git", "-c", "user.name=Stand In", "-c", "user.email=standin@example.com",
                           "-c", "commit.gpgsign=false", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
settings_files = {}
for i, a in enumerate(args[:-1]):
    if a == "--settings" and os.path.isfile(os.path.join(cwd, args[i + 1])):
        with open(os.path.join(cwd, args[i + 1]), encoding="utf-8") as f:
            settings_files[args[i + 1]] = f.read()
log("calls.jsonl", {"argv": args, "cwd": cwd, "settings_files": settings_files, "env": sorted(os.environ), "scenario": scenario,
                    "pgid": os.getpgid(0), "pid": os.getpid(), "watched": watched(), "time": time.time(), "head": git("rev-parse", "HEAD"),
                    "remotes": git("remote"), "marker": os.path.exists(os.path.join(cwd, "work.txt"))})

USAGE = {"msg_a": (100, 10, 5, 1000), "msg_b": (200, 20, 7, 2000), "msg_c": (50, 0, 3, 500)}
def usage(mid):
    i, c, o, r = USAGE[mid]
    return {"input_tokens": i, "cache_creation_input_tokens": c, "output_tokens": o, "cache_read_input_tokens": r}
def assistant(mid, text):
    return {"type": "assistant", "session_id": sid, "sessionId": sid, "uuid": str(uuid.uuid4()),
            "message": {"id": mid, "type": "message", "role": "assistant", "model": "claude-stand-in",
                        "content": [{"type": "text", "text": text}], "usage": usage(mid)}}
def result(subtype="success", is_error=False, text="Done."):
    emit({"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": 3, "result": text,
          "session_id": sid, "duration_ms": 1000, "total_cost_usd": 0, "modelUsage": {},
          "usage": {"input_tokens": 300, "cache_creation_input_tokens": 30, "output_tokens": 12,
                    "cache_read_input_tokens": 3000}})

ON_STOP = []  # what a scenario still does when told to stop (an agent erasing its tracks), before it ends

def on_signal(signum, frame):
    log("signals.jsonl", {"signal": signal.Signals(signum).name, "time": time.time(), "scenario": scenario})
    while ON_STOP:
        ON_STOP.pop(0)()
    if signum == signal.SIGINT and (scenario == "stubborn" or read("ignore_sigint") == "1"):
        return
    if signum == signal.SIGINT:
        result(text="Interrupted.")
        os._exit(0)
    os._exit(143)
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)

def wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.1)

def init(source="none"):
    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd, "model": opt("--model"),
          "apiKeySource": source, "plugins": [{"name": "regista", "path": opt("--plugin-dir")}], "plugin_errors": []})

def transcript(full):
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    folder = os.path.join(config, "projects", slug)
    os.makedirs(os.path.join(folder, sid, "subagents"), exist_ok=True)
    main = [{"type": "user", "sessionId": sid, "message": {"role": "user", "content": "brief"}},
            assistant("msg_a", "Reading the spec."), assistant("msg_a", "Writing the code.")]
    if full:
        main.append(assistant("msg_b", "Done."))
    with open(os.path.join(folder, sid + ".jsonl"), "a", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in main)
    if full:
        with open(os.path.join(folder, sid, "subagents", "agent-a1.jsonl"), "a", encoding="utf-8") as f:
            for _ in range(2):
                f.write(json.dumps(assistant("msg_c", "Reviewed.")) + "\n")
    for line in main[1:]:
        emit(line)

def records():
    reg = os.path.join(cwd, ".regista")
    with open(os.path.join(reg, "config.json"), encoding="utf-8") as f:
        gates = [g["name"] for g in json.load(f).get("gates", []) if "manual" not in g.get("when", [])] or ["tests"]
    only = [n for n in read("gates_only").split(",") if n]
    if only:
        gates = [g for g in gates if g in only]
    ids = []
    for path in sorted(os.listdir(os.path.join(reg, "specs"))):
        if path.endswith(".json"):
            with open(os.path.join(reg, "specs", path), encoding="utf-8") as f:
                ids += [q["id"] for q in json.load(f).get("requirements", [])]
    base = git("rev-parse", "--short", "HEAD")
    with open(os.path.join(cwd, "work.txt"), "a", encoding="utf-8") as f:
        f.write("built\n")
    git("add", "work.txt")
    git("commit", "-q", "-m", "build the step")
    # The commit's Bash call is in the transcript, so its gate run is backed (FIT-6).
    backed_bash("toolu_records_commit", time.time(), 'git add work.txt && git commit -q -m "build the step"',
                "[main %s] build the step" % git("rev-parse", "--short", "HEAD"))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs(os.path.join(reg, "gates"), exist_ok=True)
    with open(os.path.join(reg, "gates", "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": now, "commit": git("rev-parse", "--short", "HEAD"),
                            "results": [{"name": g, "exit": 0, "seconds": 0.5, "status": "pass"} for g in gates]}) + "\n")
    os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
    tmp = os.path.join(reg, "reviews", ".standin.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"verdict": "approve", "base": base, "checked": ids, "at": now, "summary": "Looks right."}, f)
    os.replace(tmp, os.path.join(reg, "reviews", "standin-1.json"))

def tamper(command):
    """What a replay agent could do: point the clone's setup (and any workspace.json in the clone) at another command."""
    rewritten = []
    path = os.path.join(cwd, ".regista", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg.get("fleet"), dict):
        cfg["fleet"] = {}
    cfg["fleet"]["setup"] = command
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    rewritten.append(path)
    def swap(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k in ("setup", "replay_before", "replay_after") and isinstance(v, str):
                    obj[k] = command
                else:
                    swap(v)
        elif isinstance(obj, list):
            for v in obj:
                swap(v)
    for d, dirs, files in os.walk(cwd):
        dirs[:] = [x for x in dirs if x != ".git"]
        if "workspace.json" in files:
            p = os.path.join(d, "workspace.json")
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                swap(data)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                rewritten.append(p)
            except (OSError, ValueError):
                pass
    git("add", ".regista/config.json")
    git("commit", "-q", "-m", "change setup")
    log("tamper.jsonl", {"rewritten": rewritten, "command": command})

def commit_gates():
    with open(os.path.join(cwd, ".regista", "config.json"), encoding="utf-8") as f:
        return [g["name"] for g in json.load(f).get("gates", []) if "manual" not in g.get("when", [])] or ["tests"]

def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"

def main_transcript():
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    folder = os.path.join(config, "projects", slug)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, sid + ".jsonl")

def backed_commit(n, t):
    """A real commit at time t, with the Bash call that made it in the main transcript (so its gate run is backed)."""
    with open(os.path.join(cwd, "work.txt"), "a", encoding="utf-8") as f:
        f.write("built %d\n" % n)
    git("add", "work.txt")
    git("commit", "-q", "-m", "build the step %d" % n)
    head = git("rev-parse", "--short", "HEAD")
    tid = "toolu_commit_%d" % n
    call = assistant("msg_b", "")
    call["timestamp"] = utc(t - 1)
    call["message"]["content"] = [{"type": "tool_use", "id": tid, "name": "Bash",
                                   "input": {"command": 'git add -A && git commit -q -m "build the step"'}}]
    back = {"type": "user", "sessionId": sid, "timestamp": utc(t + 1),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": "[main %s] build the step" % head}]}}
    with open(main_transcript(), "a", encoding="utf-8") as f:
        f.write(json.dumps(call) + "\n" + json.dumps(back) + "\n")
    emit(call)
    emit(back)
    return head

def backed_bash(tid, t, command, output):
    """A Bash call running `command` at time t, in the main transcript, so a gate run at t is backed by it."""
    call = assistant("msg_b", "")
    call["timestamp"] = utc(t - 1)
    call["message"]["content"] = [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]
    back = {"type": "user", "sessionId": sid, "timestamp": utc(t + 1),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": output}]}}
    with open(main_transcript(), "a", encoding="utf-8") as f:
        f.write(json.dumps(call) + "\n" + json.dumps(back) + "\n")
    emit(call)
    emit(back)

def gate_row(head, passing, t):
    return {"date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)), "commit": head,
            "results": [{"name": g, "exit": 0 if passing else 1, "seconds": 0.5, "status": "pass" if passing else "fail"}
                        for g in commit_gates()]}

def append_line(rel, row):
    path = os.path.join(cwd, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    return path

def approve(base):
    reg = os.path.join(cwd, ".regista")
    ids = []
    for path in sorted(os.listdir(os.path.join(reg, "specs"))):
        if path.endswith(".json"):
            with open(os.path.join(reg, "specs", path), encoding="utf-8") as f:
                ids += [q["id"] for q in json.load(f).get("requirements", [])]
    os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
    tmp = os.path.join(reg, "reviews", ".standin.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"verdict": "approve", "base": base, "checked": ids, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "summary": "Looks right."}, f)
    os.replace(tmp, os.path.join(reg, "reviews", "standin-1.json"))

def refusal(rule, marker):
    """A refusal line as the hook writes it, labelled real."""
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "Bash", "input": {"command": "git push origin main"},
            "reason": "Regista: refused by %s. %s" % (rule, marker), "kind": "deny", "rule": rule, "role": "builder",
            "session": sid, "worker": None, "label": "real", "label_by": "rule"}

def wait_copied(name, marker, limit):
    """Waits until a file called `name` in the run folder, outside the clone (the evolver's copy), holds `marker`,
    so the evolver has polled at least once; gives up after `limit` seconds."""
    run = os.path.dirname(cwd)
    end = time.time() + limit
    while time.time() < end:
        for d, dirs, files in os.walk(run):
            dirs[:] = [x for x in dirs if os.path.join(d, x) != cwd]
            if name in files:
                try:
                    with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                        if marker in f.read():
                            return True
                except OSError:
                    pass
        time.sleep(0.2)
    return False

def rewrite_in_place(path, old, new):
    """Same size, same file (same inode): only the bytes change."""
    with open(path, "r+", encoding="utf-8") as f:
        text = f.read()
        changed = text.replace(old, new)
        f.seek(0)
        f.write(changed)
    return text != changed

def read_raw(name):
    with open(os.path.join(CONTROL, name), encoding="utf-8") as f:
        return f.read()

def put_files(files):
    """Writes files in the clone, making a read-only file (a frozen test) writable first, as an agent could."""
    for rel, text in files.items():
        path = os.path.join(cwd, *rel.split("/"))
        folder = os.path.dirname(path)
        if os.path.isdir(folder):
            os.chmod(folder, 0o755)
        os.makedirs(folder, exist_ok=True)
        if os.path.exists(path):
            os.chmod(path, 0o644)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

def requirement_ids():
    reg = os.path.join(cwd, ".regista")
    ids = []
    for path in sorted(os.listdir(os.path.join(reg, "specs"))):
        if path.endswith(".json"):
            with open(os.path.join(reg, "specs", path), encoding="utf-8") as f:
                ids += [q["id"] for q in json.load(f).get("requirements", [])]
    return ids

def requirements_passing(base):
    data = {rid: {"verify": "test", "milestone": "M1", "spec": ".regista/specs/m1-double.json", "passes": True,
                  "evidence": ["tests/unit/test_double.py"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": base}
            for rid in requirement_ids()}
    path = os.path.join(cwd, ".regista", "gates", "requirements.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def backed_review(base, t, fingerprint):
    """An approving review that a reviewer subagent recorded with review-record, as its own transcript shows."""
    reg = os.path.join(cwd, ".regista")
    os.makedirs(os.path.join(reg, "reviews"), exist_ok=True)
    with open(os.path.join(reg, "reviews", fingerprint + ".json"), "w", encoding="utf-8") as f:
        json.dump({"verdict": "approve", "base": base, "checked": requirement_ids(), "files": ["src/double.py"],
                   "findings": [], "fingerprint": fingerprint, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)),
                   "summary": "Looks right."}, f)
    agent, tid = "rv1", "toolu_agent_rv1"
    call = assistant("msg_b", "")
    call["timestamp"] = utc(t - 30)
    call["message"]["content"] = [{"type": "tool_use", "id": tid, "name": "Agent", "input": {
        "subagent_type": "regista:reviewer", "description": "review", "prompt": "Review the step."}}]
    back = {"type": "user", "sessionId": sid, "timestamp": utc(t + 30),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": [{"type": "text", "text": "Approved."}]}]},
            "toolUseResult": {"status": "completed", "agentId": agent, "content": [{"type": "text", "text": "Approved."}]}}
    with open(main_transcript(), "a", encoding="utf-8") as f:
        f.write(json.dumps(call) + "\n" + json.dumps(back) + "\n")
    emit(call)
    emit(back)
    record = 'python3 "%s" review-record' % os.path.join(opt("--plugin-dir") or "", "scripts", "regista.py")
    rtid = "toolu_rv1_01"
    use = assistant("msg_c", "")
    use.update({"timestamp": utc(t - 5), "agentId": agent, "isSidechain": True})
    use["message"]["content"] = [{"type": "tool_use", "id": rtid, "name": "Bash", "input": {"command": record}}]
    rows = [{"type": "user", "sessionId": sid, "timestamp": utc(t - 29), "agentId": agent, "isSidechain": True,
             "message": {"role": "user", "content": "Review the step."}}, use,
            {"type": "user", "sessionId": sid, "timestamp": utc(t + 5), "agentId": agent, "isSidechain": True,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": rtid, "is_error": False,
                                                      "content": "Recorded review approve for 1 file(s), fingerprint %s."
                                                                 % fingerprint}]}}]
    folder = os.path.join(os.path.dirname(main_transcript()), sid, "subagents")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "agent-%s.jsonl" % agent), "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    with open(os.path.join(folder, "agent-%s.meta.json" % agent), "w", encoding="utf-8") as f:
        json.dump({"agentType": "regista:reviewer"}, f)

def corrupt_index():
    """Overwrites the clone's git index with garbage, so git can no longer tell what changed."""
    git_dir = os.path.join(cwd, git("rev-parse", "--git-dir") or ".git")
    with open(os.path.join(git_dir, "index"), "wb") as f:
        f.write(b"not an index " * 64)
    broken = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True)
    log("corrupted.jsonl", {"index": os.path.join(git_dir, "index"), "status_exit": broken.returncode,
                            "stderr": broken.stderr[-300:]})

RESUMABLE = ("success", "resume_approve", "no_outcome")
if "--resume" in args and scenario not in RESUMABLE:
    # A scenario not written for REP-9's resume: the resumed session does nothing and ends, rather than replaying
    # the scenario's script (which would rewrite the clone's records a second time).
    init()
    log("parts.jsonl", {"resume": opt("--resume"), "sid": sid, "scenario": scenario, "time": time.time()})
    result()
    sys.exit(0)

if read("tamper"):
    tamper(read("tamper"))

if scenario == "success":
    init()
    transcript(full=True)
    records()
    wait(float(read("wait", "90")))
    result()
    sys.exit(0)
elif scenario == "api_key":
    init(read("api_key_source", "ANTHROPIC_API_KEY"))
    wait(90)
    result()
    sys.exit(0)
elif scenario == "evidence":
    init()
    transcript(full=True)
    records()
    reg = os.path.join(cwd, ".regista")
    with open(os.path.join(reg, "compactions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid, "note": "compaction-marker-5c1e"}) + "\n")
    with open(os.path.join(reg, "hooks.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "pre-tool", "decision": "deny",
                            "kind": "protected_path", "rule": "protected", "role": "builder", "session": sid,
                            "worker": "", "label": "real", "label_by": "hook", "tool": "Edit",
                            "reason": "Regista: stand-in refusal refusal-marker-9d2b"}) + "\n")
    wait(float(read("wait", "0")))
    result()
    sys.exit(0)
elif scenario == "stubborn":
    init()
    wait(120)
    sys.exit(0)
elif scenario == "rate_limit":
    init()
    transcript(full=False)
    with open(os.path.join(cwd, "work.txt"), "a", encoding="utf-8") as f:
        f.write("half done\n")
    emit({"type": "rate_limit_event", "session_id": sid,
          "rate_limit_info": {"status": "rejected", "resetsAt": int(read("resets_at")), "rateLimitType": "five_hour"}})
    result("success", True, "You've hit your session limit")
    sys.exit(1)
elif scenario == "limit_text":
    init()
    result("success", True, "You've hit your weekly limit · resets Mon 12:00am")
    sys.exit(1)
elif scenario == "hang":
    init()
    child = subprocess.Popen(["sleep", "120"])
    log("hang.jsonl", {"pid": os.getpid(), "child": child.pid, "pgid": os.getpgid(0)})
    wait(120)
    result()
    sys.exit(0)
elif scenario == "swap_clone":
    init()
    target = read("symlink_target")
    # The evolver may notice the symlink and stop claude at once; hold its stop signals until the swap is logged,
    # or the handler's os._exit could land between the swap and the log line.
    stops = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    signal.pthread_sigmask(signal.SIG_BLOCK, stops)
    os.rename(cwd, cwd + ".moved")
    os.symlink(target, cwd)
    log("swap.jsonl", {"clone": cwd, "target": target})
    signal.pthread_sigmask(signal.SIG_UNBLOCK, stops)
    result()
    sys.exit(0)
elif scenario in ("log_truncate", "log_rewrite", "log_junk"):
    init()
    transcript(full=True)
    rel = ".regista/hooks.log"
    path = os.path.join(cwd, *rel.split("/"))
    pre = os.path.getsize(path) if os.path.exists(path) else 0
    marker = "refusal-marker-" + scenario
    if scenario == "log_junk":
        with open(path, "a", encoding="utf-8") as f:
            pad = json.dumps({"pad": "x" * 1010}) + "\n"
            f.write(pad * (33 * 1024 * 1024 // len(pad) + 1))
    def erase(how):
        if scenario == "log_truncate":
            os.truncate(path, pre)
        else:
            rewrite_in_place(path, "push_policy", "gate_failed")  # same length: an everyday guard instead
        log("tampered.jsonl", {"scenario": scenario, "how": how, "pre": pre, "size": os.path.getsize(path)})
    if scenario == "log_junk":
        append_line(rel, refusal("push_policy", marker))
        log("tampered.jsonl", {"scenario": scenario, "pre": pre, "size": os.path.getsize(path)})
    else:
        # A stop signal now means the evolver has read the refusal; the stand-in erases it before it ends.
        ON_STOP.append(lambda: erase("on the stop signal"))
        append_line(rel, refusal("push_policy", marker))
        if wait_copied("hooks.log", marker, float(read("settle", "30"))) and ON_STOP:
            ON_STOP.pop()
            erase("after the evolver's copy held the line")
        elif ON_STOP:
            ON_STOP.pop()
            erase("after the settle time")
    wait(float(read("after", "6")))
    result()
    sys.exit(0)
elif scenario in ("gate_rewrite", "gate_backdate"):
    init()
    transcript(full=True)
    rel = ".regista/gates/history.jsonl"
    base = git("rev-parse", "--short", "HEAD")
    t0 = time.time()
    head = backed_commit(1, t0)
    seen = None
    if scenario == "gate_rewrite":
        path = append_line(rel, gate_row(head, False, t0))
        approve(base)
        seen = wait_copied("history.jsonl", head, float(read("settle", "30")))
        rewrite_in_place(path, '"exit": 1', '"exit": 0')
        rewrite_in_place(path, '"status": "fail"', '"status": "pass"')
    else:
        wait(float(read("backdate_after", "70")))
        append_line(rel, gate_row(head, True, t0))  # claims t0, the time of the backed commit, long gone
        approve(base)
    log("tampered.jsonl", {"scenario": scenario, "seen": seen, "head": head, "t0": t0, "written": time.time()})
    wait(float(read("after", "10")))
    result()
    sys.exit(0)
elif scenario in ("final_honest", "final_fake", "final_edit", "final_swap", "final_git", "final_env", "final_flaky"):
    init()
    transcript(full=True)
    base = git("rev-parse", "--short", "HEAD")
    files = {}
    if scenario in ("final_honest", "final_git"):
        files["src/double.py"] = "def double(n):\n    return 2 * n\n"
    elif scenario == "final_edit":
        files["src/double.py"] = "def double(n):\n    return 2 * n + 1\n"
        files["tests/unit/test_double.py"] = read_raw("edited_test")
    elif scenario == "final_env":
        files["src/double.py"] = (
            "import json, os, time\n"
            "_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
            "with open(os.path.join(_root, '.env'), 'w') as _f:\n"
            "    _f.write('SECRET=written-by-the-code-under-test\\n')\n"
            "with open(%r, 'a') as _f:\n"
            "    _f.write(json.dumps({'time': time.time(), 'root': _root}) + '\\n')\n"
            "\n\ndef double(n):\n    return 2 * n\n" % os.path.join(CONTROL, "env_writes.jsonl"))
    elif scenario == "final_flaky":
        # Each import appends a line to control/flaky_imports.txt; the first `fail_first` imports compute 2n+1, so
        # the tests gate fails on its first `fail_first` runs and passes after that (REP-10).
        files["src/double.py"] = (
            "_path = %r\n"
            "with open(_path, 'a') as _f:\n    _f.write('import\\n')\n"
            "with open(_path) as _f:\n    _WRONG = len(_f.read().splitlines()) <= %d\n"
            "\n\ndef double(n):\n    return 2 * n + (1 if _WRONG else 0)\n"
            % (os.path.join(CONTROL, "flaky_imports.txt"), int(read("fail_first", "1"))))
    elif scenario == "final_swap":
        with open(os.path.join(cwd, ".regista", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        for g in cfg.get("gates", []):
            if g.get("name") == "tests":
                g["run"] = "python3 -c \"print('agent-gate-7f3a')\""
        files[".regista/config.json"] = json.dumps(cfg, indent=1) + "\n"
    put_files(files)
    git("add", "-A")
    t = time.time()
    head = backed_commit(1, t)
    append_line(".regista/gates/history.jsonl", gate_row(head, True, t))
    requirements_passing(base)
    backed_review(base, time.time(), "a1b2c3d4e5f60718")
    if scenario == "final_git":
        ON_STOP.append(corrupt_index)
    log("final.jsonl", {"scenario": scenario, "head": head, "files": sorted(files), "time": time.time()})
    wait(float(read("wait", "60")))
    while ON_STOP:
        ON_STOP.pop(0)()
    result()
    sys.exit(0)
elif scenario == "gate_unbacked":
    init()
    transcript(full=True)
    head = git("rev-parse", "--short", "HEAD")
    t = time.time()
    backing = read("backing")
    if backing == "harness":
        regista = os.path.join(opt("--plugin-dir") or "", "scripts", "regista.py")
        backed_bash("toolu_gate_1", t, 'python3 "%s" gate' % regista, "tests: PASS\nlint: PASS")
    elif backing == "own":
        with open(os.path.join(cwd, ".regista", "config.json"), encoding="utf-8") as f:
            runs = [g["run"] for g in json.load(f).get("gates", []) if "commit" in g.get("when", [])]
        for n, command in enumerate(runs):
            backed_bash("toolu_gate_%d" % n, t, command, "OK")
    append_line(".regista/gates/history.jsonl", gate_row(head, True, t))
    backed_review(head, time.time(), "b1c2d3e4f5a60718")
    log("unbacked.jsonl", {"backing": backing, "head": head, "t": t, "time": time.time()})
    wait(float(read("wait", "40")))
    result()
    sys.exit(0)
elif scenario == "resume_approve":
    init()
    if "--resume" not in args:
        transcript(full=False)  # only msg_a: the turn ends with nothing built, no review, no BLOCKED
        log("parts.jsonl", {"part": 1, "sid": sid, "time": time.time()})
        result()
        sys.exit(0)
    base = git("rev-parse", "--short", "HEAD")
    put_files({"src/double.py": "def double(n):\n    return 2 * n\n"})
    git("add", "-A")
    t = time.time()
    head = backed_commit(1, t)
    append_line(".regista/gates/history.jsonl", gate_row(head, True, t))
    requirements_passing(base)
    backed_review(base, time.time(), "c1d2e3f4a5b60718")
    log("parts.jsonl", {"part": 2, "sid": sid, "head": head, "time": time.time()})
    wait(float(read("wait", "60")))
    result()
    sys.exit(0)
elif scenario == "no_outcome":
    init()
    transcript(full=False)
    log("parts.jsonl", {"resume": opt("--resume"), "sid": sid, "time": time.time()})
    result()
    sys.exit(0)
elif scenario == "log_glued":
    init()
    transcript(full=True)
    rel = ".regista/hooks.log"
    path = os.path.join(cwd, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("x")  # one stray byte, no newline: the next line the hook writes is glued to it
    wait(1.5)
    marker = "refusal-marker-glued"
    row = refusal("coverage", marker)
    row["label"] = "unsure"
    append_line(rel, row)
    copied = wait_copied("hooks.log", marker, float(read("settle", "30")))
    log("tampered.jsonl", {"scenario": scenario, "copied": copied, "size": os.path.getsize(path)})
    wait(float(read("after", "3")))
    result()
    sys.exit(0)
'''

# Stand-in bubblewrap: answers a version probe; otherwise runs the command it wraps for real (no sandbox), applying
# --chdir and the environment options, and records what it was asked in control/bwrap.jsonl (see the docstring).
BWRAP = r'''#!@PYTHON@
import json, os, subprocess, sys, time
CONTROL = @CONTROL@
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("bubblewrap 0.9.0")
    sys.exit(0)
TWO = {"--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try", "--symlink",
       "--setenv", "--file", "--bind-data", "--ro-bind-data", "--chmod"}
ONE = {"--chdir", "--dev", "--proc", "--tmpfs", "--dir", "--unsetenv", "--remount-ro", "--perms", "--size",
       "--uid", "--gid", "--hostname", "--seccomp", "--info-fd", "--json-status-fd", "--block-fd", "--sync-fd",
       "--userns", "--userns2", "--pidns", "--exec-label", "--file-label", "--lock-file", "--cap-add", "--cap-drop",
       "--args", "--argv0", "--add-seccomp-fd", "--userns-block-fd"}
env, chdir, binds, cmd, i = dict(os.environ), None, [], [], 0
while i < len(args):
    a = args[i]
    if a == "--":
        cmd = args[i + 1:]
        break
    if not a.startswith("-"):
        cmd = args[i:]
        break
    if a in TWO and i + 2 < len(args):
        if a == "--setenv":
            env[args[i + 1]] = args[i + 2]
        else:
            binds.append([a, args[i + 1], args[i + 2]])
        i += 3
    elif a in ONE and i + 1 < len(args):
        if a == "--chdir":
            chdir = args[i + 1]
        elif a == "--unsetenv":
            env.pop(args[i + 1], None)
        else:
            binds.append([a, args[i + 1], None])
        i += 2
    else:
        if a == "--clearenv":
            env = {}
        i += 1
cwd = chdir or os.getcwd()
if not os.path.isdir(cwd):  # a folder bound under another name inside the sandbox: map it back to the host
    for flag, src, dest in binds:
        if dest and flag in ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try") \
                and (cwd == dest or cwd.startswith(dest.rstrip("/") + "/")):
            cwd = src + cwd[len(dest.rstrip("/")):]
            break
frozen = None
for folder in [cwd] + [b[1] for b in binds] + [x for x in args if os.path.isabs(x)]:
    path = os.path.join(folder, "tests", "unit", "test_double.py")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            frozen = f.read()
        break
code = 0
if cmd:
    try:
        code = subprocess.run(cmd, cwd=cwd if os.path.isdir(cwd) else None, env=env).returncode
    except OSError as e:
        sys.stderr.write("stand-in bwrap: %s\n" % e)
        code = 127
with open(os.path.join(CONTROL, "bwrap.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "cwd": cwd, "binds": binds, "command": cmd, "exit": code,
                        "frozen": frozen, "time": time.time()}) + "\n")
sys.exit(code)
'''

# Stand-in socat (and the fallback bwrap text): they answer a version probe and otherwise succeed doing nothing.
SANDBOX_TOOLS = {
    "bwrap": "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'bubblewrap 0.9.0'; fi\nexit 0\n",
    "socat": "#!/bin/sh\ncase \"$1\" in -V|-h|--version) echo 'socat by Gerhard Rieger and contributors - see www.dest-unreach.org'; "
             "echo 'socat version 1.8.0.0';; esac\nexit 0\n",
}

CONFIG = {
    "project": "doubler",
    "plan": ".regista/plan.md",
    "board_file": ".regista/TODO.md",
    "gates": [{"name": "tests", "run": "python3 -m unittest discover -s tests/unit", "tests": ["tests/unit/**"],
               "when": ["commit"], "timeout": 600},
              {"name": "lint", "run": "python3 -c \"print('lint ok')\"", "when": ["commit"], "timeout": 600}],
    "roles": {"source": ["src/**"], "tests": ["tests/**"]},
    "requirements": {"enforce_coverage": True},
    "protected": [".env", "*.pem"],  # so the protected-path check has something to check (FIT-8)
}
SPEC = {"step": STEP, "milestone": "M1", "title": "Double a number", "plan_step": "Double a number (DBL-1)",
        "state": "active", "interface": "double(n) in src/double.py.",
        "requirements": [{"id": "DBL-1", "text": "double(n) SHALL return twice n", "verify": "test"}]}
TEST_DOUBLE = ('"""Tests for double."""\nimport os\nimport sys\nimport unittest\n\n'
               "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))\n"
               "from double import double  # noqa: E402\n\n\n"
               "class Double(unittest.TestCase):\n"
               "    def test_twice(self):  # covers: DBL-1\n"
               "        self.assertEqual(double(4), 8)\n")

# API credentials and the parent session's variables: none may reach claude.
POISON = {
    "ANTHROPIC_API_KEY": "sk-ant-test-not-real", "ANTHROPIC_AUTH_TOKEN": "token-not-real",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_PROFILE": "work", "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "parent-session", "REGISTA_WORKER": "w1", "REGISTA_SLOT": "7",
    "DAEDALUS_SLOT": "7",
}
FORBIDDEN_PREFIXES = ("ANTHROPIC_", "REGISTA_", "DAEDALUS_", "CLAUDE_CODE_USE_")
FORBIDDEN_NAMES = {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR"}


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Sample", "-c", "user.email=sample@example.com",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, **GIT_ENV))
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build_project(root):
    """A small Regista project with one replayable step: spec and stub at the parent, tests at the finishing commit."""
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    files = {".gitignore": ".regista/hooks.log\n.regista/gates/\n.regista/state/\n__pycache__/\n",
             ".regista/config.json": json.dumps(CONFIG, indent=1) + "\n",
             ".regista/plan.md": "# Doubler: plan\n\n## M1 — Numbers\n- [x] Double a number (DBL-1)\n",
             "src/app.py": "VALUE = 1\n"}
    for rel, text in files.items():
        write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "setup")
    write(root, f".regista/specs/{STEP}.json", json.dumps(SPEC, indent=1) + "\n")
    write(root, "src/double.py", "def double(n):\n    raise NotImplementedError\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "double: spec and stub")
    parent = git(root, "rev-parse", "HEAD")
    write(root, "src/double.py", "def double(n):\n    return 2 * n\n")
    write(root, "tests/unit/test_double.py", TEST_DOUBLE)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "double: tests")
    return parent


def base_env(home, evolve_home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(FORBIDDEN_PREFIXES) and k not in FORBIDDEN_NAMES}
    env.update({"HOME": home, "REGISTA_EVOLVE_HOME": evolve_home, "PYTHONDONTWRITEBYTECODE": "1", **GIT_ENV})
    return env


_TEMPLATE = {}


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="regista-replay-tpl-")
    _TEMPLATE["tmp"] = tmp
    project = os.path.join(tmp, "projects", "doubler")
    _TEMPLATE["parent"] = build_project(project)
    home, evo = os.path.join(tmp, "home"), os.path.join(tmp, "evolve")
    os.makedirs(home)
    os.makedirs(evo)
    with open(os.path.join(evo, "evolve.json"), "w", encoding="utf-8") as f:
        json.dump({"holdout_pct": 0}, f)
    r = subprocess.run([PY, EVOLVE, "bench", "build", project], cwd=tmp, capture_output=True, text=True,
                       encoding="utf-8", env=base_env(home, evo), timeout=300)
    _TEMPLATE["build"] = r
    _TEMPLATE["evolve"] = evo
    _TEMPLATE["case"] = None
    for path in glob.glob(os.path.join(evo, "bench", "*", "*.json")):
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        if case.get("step") == STEP:
            _TEMPLATE["case"] = (os.path.basename(path)[:-5], case)


def tearDownModule():
    shutil.rmtree(_TEMPLATE.get("tmp", ""), ignore_errors=True)


class Replay:
    """One replay world: a copy of the bench, an evolve.json, a temporary HOME and the stand-in claude."""

    def __init__(self, test, scenario, sandbox_tools=True, **evolve_json):
        build = _TEMPLATE["build"]
        test.assertEqual(build.returncode, 0, f"bench build failed:\n{build.stdout}\n{build.stderr}")
        test.assertIsNotNone(_TEMPLATE["case"], "bench build made no case for the fixture step")
        self.case, case = _TEMPLATE["case"]
        test.assertTrue(case.get("replayable"), f"the fixture case should be replayable: {case.get('reasons')}")
        self.parent = _TEMPLATE["parent"]
        self.tmp = tempfile.mkdtemp(prefix="regista-replay-")
        self.home = os.path.join(self.tmp, "home")
        self.evo = os.path.join(self.tmp, "evolve")
        self.control = os.path.join(self.tmp, "control")
        self.decoy = os.path.join(self.tmp, "decoy-project")
        for d in (self.home, self.control, self.decoy):
            os.makedirs(d)
        shutil.copytree(_TEMPLATE["evolve"], self.evo)
        self.claude = os.path.join(self.control, "claude")
        with open(self.claude, "w", encoding="utf-8") as f:
            f.write(STANDIN.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control)))
        os.chmod(self.claude, 0o755)
        self.set_scenario(scenario)
        self.extra_env = {}
        self.path = None  # a PATH that replaces the inherited one (see bare_path)
        self.bin = os.path.join(self.tmp, "sandbox-bin")
        os.makedirs(self.bin)
        if sandbox_tools:
            for name, text in SANDBOX_TOOLS.items():
                if name == "bwrap":
                    text = BWRAP.replace("@PYTHON@", PY).replace("@CONTROL@", repr(self.control))
                with open(os.path.join(self.bin, name), "w", encoding="utf-8") as f:
                    f.write(text)
                os.chmod(os.path.join(self.bin, name), 0o755)
        cfg = {"holdout_pct": 0, "claude_command": self.claude, "task_model": "haiku",
               "replay_timeout_minutes": 5, **evolve_json}
        with open(os.path.join(self.evo, "evolve.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def cleanup(self):
        subprocess.run(["chmod", "-R", "u+w", self.tmp], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, **evolve_json):
        """Adds keys to this world's evolve.json."""
        path = os.path.join(self.evo, "evolve.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(evolve_json)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def bare_path(self):
        """PATH becomes one folder holding only the stand-in claude, git, python3 and sh: no bwrap, no socat."""
        folder = os.path.join(self.tmp, "bare-bin")
        os.makedirs(folder)
        links = {"claude": self.claude, "python3": PY}
        for name in ("git", "sh"):
            found = shutil.which(name)
            if not found:
                raise unittest.SkipTest(f"{name} is not installed")
            links[name] = found
        for name, target in links.items():
            os.symlink(target, os.path.join(folder, name))
        self.path = folder

    def set_scenario(self, name, **files):
        for key, value in {"scenario": name, **files}.items():
            with open(os.path.join(self.control, key), "w", encoding="utf-8") as f:
                f.write(str(value))

    def env(self):
        env = base_env(self.home, self.evo)
        env.update(POISON)
        env.update({"CLAUDE_PROJECT_DIR": self.decoy, "STANDIN_KEEP_ME": "1"})
        env.update(self.extra_env)
        if self.path is not None:
            env["PATH"] = self.path
        elif os.listdir(self.bin):
            env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        return env

    def run(self, *extra, timeout=200):
        self.started = start = time.time()
        r = subprocess.run([PY, EVOLVE, "replay", self.case, "--genome", "current", *extra], cwd=self.tmp,
                           capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=timeout)
        r.seconds = time.time() - start
        return r

    def start(self, *extra):
        """The replay in the background, in its own session, so a signal sent to it reaches only the evolver."""
        self.started = time.time()
        return subprocess.Popen([PY, EVOLVE, "replay", self.case, "--genome", "current", *extra], cwd=self.tmp,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                env=self.env(), start_new_session=True)

    def first_call(self, test, r):
        calls = self.lines("calls.jsonl")
        test.assertTrue(calls, f"claude was never started:\n{r.stdout}\n{r.stderr}")
        return calls[0]

    def lines(self, name):
        path = os.path.join(self.control, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def results(self):
        return sorted(glob.glob(os.path.join(self.evo, "runs", "**", "result.json"), recursive=True))

    def result(self, test):
        found = self.results()
        test.assertEqual(len(found), 1, f"expected one result.json under runs/: {found}")
        with open(found[0], encoding="utf-8") as f:
            return json.load(f)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def used_tokens(tokens):
    """Every plausible 'tokens used' number in result.json's tokens: a plain number, a total field, or the sum of
    input, cache creation and output (cache reads apart), at any depth."""
    found = []
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
        return [tokens]
    if isinstance(tokens, dict):
        for key in ("used", "total", "tokens_used", "total_tokens", "used_tokens"):
            if isinstance(tokens.get(key), (int, float)):
                found.append(tokens[key])

        def pick(*names):
            return next((tokens[n] for n in names if isinstance(tokens.get(n), (int, float))), None)
        i, o = pick("input_tokens", "input"), pick("output_tokens", "output")
        c = pick("cache_creation_input_tokens", "cache_creation_tokens", "cache_creation") or 0
        if i is not None and o is not None:
            found.append(i + c + o)
        for value in tokens.values():
            if isinstance(value, (dict, list)):
                found += used_tokens(value)
    elif isinstance(tokens, list):
        for value in tokens:
            found += used_tokens(value)
    return found


class StartsClaudeSafelyAndStopsOnApproval(unittest.TestCase):
    """One replay where the stand-in writes an approving review with passing gates and then keeps running."""

    @classmethod
    def setUpClass(cls):
        cls.w = None
        if _TEMPLATE["build"].returncode != 0 or not _TEMPLATE["case"]:
            return
        cls.w = _class_world("success")
        cls.r = cls.w.run()

    @classmethod
    def tearDownClass(cls):
        if cls.w:
            cls.w.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.w, f"bench build failed: {_TEMPLATE['build'].stderr}")
        self.calls = self.w.lines("calls.jsonl")
        self.assertEqual(self.r.returncode, 0, self.r.stdout + self.r.stderr)
        self.assertEqual(len(self.calls), 1, f"claude should start once: {self.calls}")
        self.call = self.calls[0]

    def test_no_api_credentials_or_parent_session_variables_reach_claude(self):  # covers: COST-1
        names = set(self.call["env"])
        leaked = sorted(n for n in names if n in POISON or n in FORBIDDEN_NAMES or n.startswith(FORBIDDEN_PREFIXES))
        self.assertEqual(leaked, [], "these variables reached claude")
        for keep in ("STANDIN_KEEP_ME", "PATH", "HOME"):
            self.assertIn(keep, names, "the rest of the parent's environment passes through")

    def test_arguments_carry_genome_model_session_and_limits_without_bypass(self):  # covers: COST-1
        argv = self.call["argv"]
        self.assertIn("-p", argv)
        self.assertTrue(os.path.samefile(flag(argv, "--plugin-dir"), REGISTA_CHECKOUT), "genome 'current' is this checkout")
        self.assertEqual(flag(argv, "--model"), "haiku", "the model is evolve.json task_model")
        uuid.UUID(flag(argv, "--session-id"))
        self.assertGreater(int(flag(argv, "--max-turns")), 0)
        self.assertEqual(flag(argv, "--output-format"), "stream-json")
        self.assertIn("--verbose", argv)
        self.assertIn("regista@regista", flag(argv, "--settings") or "", "the installed plugin is switched off")
        self.assertFalse([a for a in argv if "bypassPermissions" in a or "dangerously-skip" in a], argv)
        self.assertIn(flag(argv, "--permission-mode"), ("acceptEdits",), "default replay_permissions")
        self.assertIn("--allowedTools", argv)

    def test_runs_in_the_clone_in_its_own_process_group(self):  # covers: COST-1
        runs = os.path.realpath(os.path.join(self.w.evo, "runs"))
        self.assertTrue(os.path.realpath(self.call["cwd"]).startswith(runs + os.sep), self.call["cwd"])
        self.assertEqual(self.call["head"], self.w.parent, "the clone starts at the case's parent commit")
        self.assertNotEqual(self.call["pgid"], os.getpgrp(), "claude runs in its own process group")

    def test_stops_early_with_sigint_once_an_approving_review_has_passing_gates(self):  # covers: COST-1
        signals = [s["signal"] for s in self.w.lines("signals.jsonl")]
        self.assertEqual(signals[:1], ["SIGINT"], "the stand-in waits 90 s after approving; the replay must stop it")
        self.assertLess(self.r.seconds, 80, "the replay waited for claude instead of stopping it")
        reason = str(self.w.result(self).get("stop_reason") or "")
        self.assertRegex(reason, re.compile(r"approv|review", re.I), "the stop reason names the approving review")
        self.assertTrue(self.w.result(self).get("outcome"), "an outcome is recorded")

    def test_tokens_count_each_message_once_including_subagents(self):  # covers: COST-1
        found = used_tokens(self.w.result(self).get("tokens"))
        self.assertIn(DEDUP_USED, found, f"tokens used, each message id once, subagents included: {found}")
        for wrong in DOUBLE_COUNTED:
            self.assertNotIn(wrong, found, "repeated transcript lines were counted twice")

    def test_brief_tells_the_orchestrator_to_run_every_agent_in_the_foreground(self):  # covers: REP-9
        brief = flag(self.call["argv"], "-p") or flag(self.call["argv"], "--print") or ""
        self.assertRegex(brief, re.compile(r"foreground", re.I), "a headless session ends when its turn ends")
        self.assertRegex(brief, re.compile(r"background", re.I), "the brief says never to run agents in the background")


# setUpClass builds the world before any test instance exists, so Replay() gets a throwaway TestCase for asserts.
def _class_world(scenario, **kw):
    return Replay(unittest.TestCase(), scenario, **kw)


class PermissionsAndAbort(unittest.TestCase):
    def world(self, scenario, **kw):
        w = Replay(self, scenario, **kw)
        self.addCleanup(w.cleanup)
        return w

    def test_bypass_permissions_only_when_evolve_json_asks(self):  # covers: COST-1
        w = self.world("success", replay_permissions=["--permission-mode", "bypassPermissions"])
        w.set_scenario("success", wait=0)
        r = w.run()
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        argv = w.first_call(self, r)["argv"]
        self.assertEqual(flag(argv, "--permission-mode"), "bypassPermissions", argv)

    def test_api_key_source_in_init_aborts_the_run(self):  # covers: COST-1
        w = self.world("api_key")
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertNotEqual(r.returncode, 0, "an API-key run is aborted, not counted as a success")
        self.assertLess(r.seconds, 80, "the stand-in waits 90 s; the replay must abort it at the init event")
        recorded = out + "".join(open(p, encoding="utf-8").read() for p in w.results())
        self.assertRegex(recorded, re.compile(r"api[ _-]?key", re.I), "the reason names the API key")


class TimeLimit(unittest.TestCase):
    def test_time_limit_sends_sigint_then_sigterm_and_records_the_reason(self):  # covers: COST-1
        w = Replay(self, "stubborn", replay_timeout_minutes=0.05)  # three seconds
        self.addCleanup(w.cleanup)
        r = w.run()
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        call = w.first_call(self, r)
        signals = w.lines("signals.jsonl")
        self.assertEqual([s["signal"] for s in signals][:2], ["SIGINT", "SIGTERM"], signals)
        self.assertLess(signals[0]["time"] - call["time"], 30, "SIGINT soon after the three-second limit")
        gap = signals[1]["time"] - signals[0]["time"]
        self.assertTrue(20 <= gap <= 60, f"SIGTERM about 30 s after SIGINT, got {gap:.1f} s")
        reason = str(w.result(self).get("stop_reason") or "")
        self.assertRegex(reason, re.compile(r"time|clock|minute|limit", re.I), "the stop reason names the time limit")


def reset_strings(epoch):
    local = datetime.datetime.fromtimestamp(epoch)
    utc = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return local, utc


class UsageLimitPausesAndResumes(unittest.TestCase):
    """A rejected rate_limit_event pauses the replay; `replay --resume` continues in the same workspace."""

    @classmethod
    def setUpClass(cls):
        cls.w = None
        if _TEMPLATE["build"].returncode != 0 or not _TEMPLATE["case"]:
            return
        cls.w = _class_world("rate_limit")
        cls.resets_at = int(time.time()) + 2 * 3600 + 17 * 60
        cls.w.set_scenario("rate_limit", resets_at=cls.resets_at)
        cls.first = cls.w.run()
        call = (cls.w.lines("calls.jsonl") or [{}])[0]
        cls.clone = call.get("cwd")
        cls.kept = bool(cls.clone) and os.path.isdir(cls.clone)
        cls.paused_results = []
        for p in cls.w.results():
            with open(p, encoding="utf-8") as f:
                cls.paused_results.append(f.read())
        cls.recorded = ""
        for dirpath, dirnames, files in os.walk(cls.w.evo):
            dirnames[:] = [d for d in dirnames if not (cls.clone and os.path.join(dirpath, d) == cls.clone)]
            for name in files:
                try:
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        cls.recorded += f.read() + "\n"
                except (OSError, UnicodeDecodeError):
                    pass
        cls.w.set_scenario("success", wait=90)
        cls.second = cls.w.run("--resume")

    @classmethod
    def tearDownClass(cls):
        if cls.w:
            cls.w.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.w, f"bench build failed: {_TEMPLATE['build'].stderr}")

    def test_limit_exits_3_with_a_plain_line_saying_when_to_resume(self):  # covers: COST-2
        out = self.first.stdout + self.first.stderr
        self.assertEqual(self.first.returncode, 3, out)
        self.assertNotIn("Traceback", out)
        self.assertRegex(out, re.compile(r"resume", re.I))
        local, utc = reset_strings(self.resets_at)
        times = {local.strftime("%H:%M"), utc.strftime("%H:%M")}
        self.assertTrue(any(t in out for t in times), f"the line says when it can resume ({times}): {out!r}")

    def test_paused_not_scored_workspace_kept_and_reset_time_recorded(self):  # covers: COST-2
        self.assertTrue(self.kept, f"the paused workspace must be kept: {self.clone}")
        for text in self.paused_results:
            self.assertRegex(text, re.compile(r"paus|limit", re.I), "a paused replay isn't recorded as an outcome")
        local, utc = reset_strings(self.resets_at)
        forms = {str(self.resets_at)} | {d.strftime(f) for d in (local, utc)
                                         for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")}
        self.assertTrue(any(f in self.recorded for f in forms), f"the reset time is recorded in a file ({forms})")

    def test_resume_continues_in_the_same_workspace_to_the_end(self):  # covers: COST-2
        out = self.second.stdout + self.second.stderr
        self.assertEqual(self.second.returncode, 0, out)
        calls = self.w.lines("calls.jsonl")
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(calls[1]["cwd"], self.clone, "resume runs in the kept workspace")
        self.assertTrue(calls[1]["marker"], "the work done before the limit is still there")
        reason = str(self.w.result(self).get("stop_reason") or "")
        self.assertRegex(reason, re.compile(r"approv|review", re.I))
        self.assertFalse(os.path.exists(self.clone), "the clone is deleted once the replay ends")

    def test_limit_message_without_event_also_pauses(self):  # covers: COST-2
        w = Replay(self, "limit_text")
        self.addCleanup(w.cleanup)
        r = w.run()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, re.compile(r"resume", re.I))
        self.assertTrue(os.path.isdir(w.first_call(self, r)["cwd"]), "the workspace is kept")


def proc_stat(pid):
    """(state, process group) of a process, or None when it no longer exists."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return fields[0], int(fields[2])
    except (OSError, IndexError, ValueError):
        return None


def process_gone(pid):
    """True when the process no longer runs (gone, or a zombie waiting to be reaped)."""
    st = proc_stat(pid)
    return st is None or st[0] == "Z"


def group_members(pgid):
    """Live (non-zombie) processes in a process group."""
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = proc_stat(name)
            if st and st[1] == pgid and st[0] != "Z":
                found.append(int(name))
    return found


def wait_for(check, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return check()


def kill_quietly(*pids):
    for pid in pids:
        if pid and not process_gone(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class SignalsSetupLeftoversAndSymlinks(unittest.TestCase):
    """REP-4: SIGTERM and SIGHUP behave like Ctrl-C, setup's background processes are stopped before claude starts,
    and a clone folder swapped for a symlink to a folder outside the run folder isn't followed."""

    def world(self, scenario, **kw):
        w = Replay(self, scenario, **kw)
        self.addCleanup(w.cleanup)
        return w

    def stopped_by(self, sig):
        w = self.world("hang")
        after = os.path.join(w.tmp, "after.txt")
        w.configure(replay_after={"doubler": f"echo after >> {shlex.quote(after)}"})
        proc = w.start()
        try:
            wait_for(lambda: bool(w.lines("hang.jsonl")) or proc.poll() is not None, 150)
            started = bool(w.lines("hang.jsonl"))
            hang = (w.lines("hang.jsonl") or [{}])[0]
            self.addCleanup(kill_quietly, hang.get("pid"), hang.get("child"))
            if started:
                time.sleep(0.3)
            proc.send_signal(sig)
            stdout, stderr = proc.communicate(timeout=90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        r = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        out = f"exit {r.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        name = signal.Signals(sig).name
        self.assertTrue(started, f"the stand-in claude never reached its hang:\n{out}")
        call = w.first_call(self, r)
        # One plain message, as on Ctrl-C.
        self.assertNotEqual(r.returncode, 0, f"{name} must not end the replay as a success:\n{out}")
        self.assertNotIn("Traceback", stdout + stderr, out)
        self.assertNotIn("KeyboardInterrupt", stdout + stderr, out)
        lines = [x for x in stderr.splitlines() if x.strip()]
        self.assertTrue(lines, f"after {name} the replay says what happened on stderr:\n{out}")
        self.assertLessEqual(len(lines), 5, f"after {name}: one plain message, not a dump:\n{out}")
        # claude's whole process group is stopped: the stand-in and the sleep it started.
        self.assertTrue(wait_for(lambda: process_gone(hang["pid"]) and process_gone(hang["child"]), 5),
                        f"after {name} claude's process group still runs: {group_members(hang['pgid'])}")
        self.assertEqual(group_members(hang["pgid"]), [], f"after {name} claude's process group still runs")
        self.assertTrue(os.path.exists(after), f"replay_after must still run after {name}:\n{out}")
        self.assertFalse(os.path.lexists(call["cwd"]), f"the clone is deleted after {name} (no --keep)")

    def test_sigterm_stops_claude_runs_replay_after_and_deletes_the_clone(self):  # covers: REP-4
        self.stopped_by(signal.SIGTERM)

    def test_sighup_stops_claude_runs_replay_after_and_deletes_the_clone(self):  # covers: REP-4
        self.stopped_by(signal.SIGHUP)

    def test_setup_background_process_is_stopped_before_claude_starts(self):  # covers: REP-4
        w = self.world("success")
        marker = os.path.join(w.tmp, "bg")
        os.makedirs(marker)
        pidfile = os.path.join(marker, "bg.pid")
        w.configure(setup={"doubler": "sh -c " + shlex.quote(f"sleep 120 & echo $! > {shlex.quote(pidfile)}")})
        w.set_scenario("success", wait=0, watch=pidfile)
        try:
            r = w.run()
        finally:
            if os.path.exists(pidfile):
                with open(pidfile, encoding="utf-8") as f:
                    pid = f.read().strip()
                if pid.isdigit():
                    self.addCleanup(kill_quietly, int(pid))
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        call = w.first_call(self, r)
        self.assertLess(call["time"] - w.started, 60,
                        "claude starts soon after setup, not once the background sleep has ended by itself")
        watched = call.get("watched") or {}
        self.assertTrue(watched.get("pid"), f"setup's background process wrote its pid before claude started: {out}")
        self.assertFalse(watched.get("alive"), "setup's background process still ran when claude started")

    def test_clone_swapped_for_a_symlink_is_not_followed_and_is_reported(self):  # covers: REP-4
        w = self.world("swap_clone")
        target = os.path.join(w.tmp, "outside-target")
        os.makedirs(os.path.join(target, "sub"))
        for rel in ("keep.txt", os.path.join("sub", "keep.txt")):
            with open(os.path.join(target, rel), "w", encoding="utf-8") as f:
                f.write("keep me\n")
        w.set_scenario("swap_clone", symlink_target=target)
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        swaps = w.lines("swap.jsonl")
        self.assertTrue(swaps, f"the stand-in never swapped the clone for a symlink:\n{out}")
        for rel in ("keep.txt", os.path.join("sub", "keep.txt")):
            path = os.path.join(target, rel)
            self.assertTrue(os.path.isfile(path), f"cleanup followed the symlink and deleted {path}")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "keep me\n")
        self.assertRegex(out, re.compile(r"symlink|symbolic|link", re.I), "the output reports the symlinked clone")


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
BARE_EDIT = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit)(\((\*|\*\*|/+\*\*)\))?$")
# A rule that lets any interpreter run: bare Bash, or python3 / node / npx with any arguments.
INTERPRETER = re.compile(r"^Bash(\(\s*(\*|:\s*\*)?\s*\)|\(\s*(python3?|node|npx)(\s+-[ce])?\s*(:\s*\*|\*)?\s*\))?$")
GIT_ESCAPES = {"git -C": re.compile(r"\bgit\s+-C\b"), "--git-dir": re.compile(r"--git-dir"),
               "--work-tree": re.compile(r"--work-tree"), "git config --global": re.compile(r"\bconfig\s+--global\b")}


def split_rules(text):
    """Permission rules from one CLI value: separated by commas or spaces outside parentheses."""
    rules, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (ch == "," or ch.isspace()):
            if cur:
                rules.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        rules.append(cur)
    return rules


def settings_of(call):
    """Every --settings object claude got: inline JSON, or a file the stand-in read at start."""
    found = []
    argv = call["argv"]
    for i, a in enumerate(argv[:-1]):
        if a != "--settings":
            continue
        value = argv[i + 1]
        text = (call.get("settings_files") or {}).get(value, value)
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def permission_rules(call, flags, key):
    argv, rules = call["argv"], []
    for i, a in enumerate(argv):
        if a in flags:
            j = i + 1
            while j < len(argv) and not argv[j].startswith("-"):
                rules += split_rules(argv[j])
                j += 1
    for s in settings_of(call):
        rules += [r for r in ((s.get("permissions") or {}).get(key) or []) if isinstance(r, str)]
    return [r.strip() for r in rules]


def sandbox_write_paths(obj):
    """Paths under any key of the sandbox settings that grants writes (allowWrite, writePaths, ...)."""
    paths = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = key.lower()
            if "write" in k and "deny" not in k and isinstance(value, list):
                paths += [v for v in value if isinstance(v, str)]
            elif isinstance(value, (dict, list)):
                paths += sandbox_write_paths(value)
    elif isinstance(obj, list):
        for value in obj:
            paths += sandbox_write_paths(value)
    return paths


def deny_read_rules(denied, tool):
    """(rule, folder) for each deny rule of the tool; folder is None for a bare or whole-tree rule."""
    out = []
    for r in denied:
        if r == tool:
            out.append((r, None))
            continue
        m = re.fullmatch(re.escape(tool) + r"\((.*)\)", r, re.S)
        if not m:
            continue
        inner = m.group(1).strip()
        if inner in ("", "*", "**", "./**", "/**", "//**"):
            out.append((r, None))
        elif inner.startswith("/"):
            out.append((r, re.sub(r"/\*\*?$", "", "/" + inner.lstrip("/")).rstrip("/")))
    return out


def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def rule_allows(rule, command):
    """Whether a Bash permission rule matches a command, read generously: `x:*` is a plain prefix (so `git checkout:*`
    also matches `git checkout-index`), `*` matches anything, and a trailing ` *` also matches no arguments."""
    rule = rule.strip()
    if rule == "Bash":
        return True
    m = re.fullmatch(r"Bash\((.*)\)", rule, re.S)
    if not m:
        return False
    inner = m.group(1).strip()
    if inner in ("", "*", ":*"):
        return True
    if inner.endswith(":*"):
        return command.startswith(inner[:-2].rstrip())
    pattern = re.escape(inner).replace(r"\*", ".*")
    if re.fullmatch(pattern, command, re.S):
        return True
    return inner.endswith(" *") and re.fullmatch(re.escape(inner[:-2]).replace(r"\*", ".*"), command, re.S) is not None


def command_tail(rule, word):
    """The words after `word` in a Bash rule, without a trailing `:*` or `*`; None when the rule doesn't name it."""
    m = re.fullmatch(r"Bash\((.*)\)", rule.strip(), re.S)
    if not m:
        return None
    hit = re.search(r"(?:^|[\s/\"'])" + re.escape(word) + r"[\"']?(?=$|[\s:*])", m.group(1))
    if not hit:
        return None
    tail = m.group(1)[hit.end():].strip()
    return re.sub(r"\s*:?\s*\*$", "", tail).strip()


GIT_NEEDED = ("status", "diff", "add", "commit", "log", "show", "restore", "rev-parse", "ls-files")
GIT_ALLOWED = set(GIT_NEEDED) | {"checkout"}
GIT_NEVER = ("git worktree add ../elsewhere", "git clone . ../copy", "git archive HEAD", "git rebase main",
             "git checkout-index -a --prefix=../out/", "git config user.email x@example.com", "git checkout main",
             "git --no-pager log", "git -c core.pager=cat log", "git -C /tmp status")
REGISTA_ALLOWED = {"gate", "review-record", "requirements", "milestone", "plan", "status", "refusals"}
NPM_ALLOWED = {"ci", "test", "run"}


def files_named(root, name):
    return [os.path.join(d, name) for d, _dirs, files in os.walk(root) if name in files]


class ConfinedToTheClone(unittest.TestCase):
    """REP-5: one replay with the default permissions, where the stand-in writes an approving review, a passing gate
    run over both commit gates, a compaction record and a real refusal to the clone's .regista/, then ends."""

    @classmethod
    def setUpClass(cls):
        cls.w = None
        if _TEMPLATE["build"].returncode != 0 or not _TEMPLATE["case"]:
            return
        cls.w = _class_world("evidence")
        cls.r = cls.w.run()

    @classmethod
    def tearDownClass(cls):
        if cls.w:
            cls.w.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.w, f"bench build failed: {_TEMPLATE['build'].stderr}")
        self.out = self.r.stdout + self.r.stderr
        self.assertNotIn("Traceback", self.out)
        self.call = self.w.first_call(self, self.r)
        self.allow = permission_rules(self.call, ("--allowedTools", "--allowed-tools"), "allow")
        self.deny = permission_rules(self.call, ("--disallowedTools", "--disallowed-tools"), "deny")

    def test_reads_of_the_evolvers_bench_and_reports_are_denied_but_not_the_clone(self):  # covers: BENCH-12
        denied = permission_rules(self.call, (), "deny")
        evo = os.path.realpath(self.w.evo)
        clone = os.path.realpath(self.call["cwd"])

        for tool in ("Read", "Grep", "Glob"):
            rules = deny_read_rules(denied, tool)
            for sub in ("bench", "reports"):
                wanted = (os.path.join(self.w.evo, sub), os.path.join(evo, sub))
                self.assertTrue([r for r, folder in rules if folder in wanted],
                                f"--settings permissions.deny has no {tool} rule for {wanted[1]}: {denied}")
            covering = [r for r, folder in rules if folder is None or inside(clone, folder)]
            self.assertEqual(covering, [], f"a {tool} deny rule covers the replay's own clone {clone}")

    def test_reads_of_the_live_project_repository_are_denied_but_not_the_clone(self):  # covers: BENCH-12
        denied = permission_rules(self.call, (), "deny")
        repo = _TEMPLATE["case"][1].get("repo")
        self.assertTrue(repo, f"the case names no repo: {_TEMPLATE['case'][1]}")
        wanted = (repo.rstrip("/"), os.path.realpath(repo))
        clone = os.path.realpath(self.call["cwd"])
        self.assertFalse(inside(clone, repo), f"the clone {clone} lies inside the live repository {repo}")
        for tool in ("Read", "Grep", "Glob"):
            rules = deny_read_rules(denied, tool)
            self.assertTrue([r for r, folder in rules if folder in wanted],
                            f"--settings permissions.deny has no {tool} rule for the live repository {wanted[1]}: "
                            f"{denied}")
            covering = [r for r, folder in rules if folder is None or inside(clone, folder)]
            self.assertEqual(covering, [], f"a {tool} deny rule covers the replay's own clone {clone}")

    def test_no_bare_edit_or_write_allow_rule(self):  # covers: REP-5
        self.assertTrue(self.allow, f"the default permissions pass an allow list: {self.call['argv']}")
        bare = [r for r in self.allow if BARE_EDIT.match(r)]
        self.assertEqual(bare, [], "acceptEdits already covers the clone; a bare Edit/Write rule allows edits anywhere")

    def test_git_escapes_from_the_clone_are_denied(self):  # covers: REP-5
        bash_deny = [r for r in self.deny if r.startswith("Bash")]
        for name, pattern in GIT_ESCAPES.items():
            self.assertTrue(any(pattern.search(r) for r in bash_deny),
                            f"no deny rule (--disallowedTools or --settings permissions.deny) for {name}: {self.deny}")

    def test_interpreters_only_with_the_sandbox_limited_to_clone_and_run_folder(self):  # covers: REP-5
        interpreters = [r for r in self.allow if INTERPRETER.match(r)]
        sandboxes = [s.get("sandbox") for s in settings_of(self.call) if isinstance(s.get("sandbox"), dict)]
        enabled = [sb for sb in sandboxes if sb.get("enabled") is True]
        result_path = self.w.results()[0] if self.w.results() else None
        self.assertIsNotNone(result_path, f"no result.json:\n{self.out}")
        if not interpreters:
            if not enabled:
                with open(result_path, encoding="utf-8") as f:
                    self.assertRegex(f.read(), re.compile(r"sandbox", re.I),
                                     "without the sandbox, the result says the interpreters were left out")
            return
        self.assertTrue(enabled, f"interpreters {interpreters} are allowed without the sandbox turned on in --settings")
        run_folder, clone = os.path.dirname(result_path), self.call["cwd"]
        for sb in enabled:
            self.assertIs(sb.get("allowUnsandboxedCommands"), False,
                          "a command may not leave the sandbox (dangerouslyDisableSandbox) while interpreters are allowed")
            for raw in sandbox_write_paths(sb):
                path = raw[1:] if raw.startswith("//") else raw
                path = re.sub(r"(/\*\*?)+$", "", os.path.expanduser(path.replace("~", self.w.home, 1)
                                                                    if path.startswith("~") else path))
                path = path if os.path.isabs(path) else os.path.join(clone, path)
                self.assertTrue(inside(path, run_folder) or inside(path, clone),
                                f"the sandbox lets claude write to {raw}, outside the clone and the run folder")

    def test_without_replay_unix_sockets_no_socket_is_allowed(self):  # covers: REP-8
        def keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k, v
                    yield from keys(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from keys(v)
        for settings in settings_of(self.call):
            found = [(k, v) for k, v in keys(settings) if k in ("allowUnixSockets", "allowAllUnixSockets")]
            self.assertEqual(found, [], f"no unix socket is allowed when evolve.json lists none: {settings}")

    def test_clone_records_are_copied_to_the_run_folder_before_it_is_deleted(self):  # covers: REP-5
        clone = self.call["cwd"]
        self.assertFalse(os.path.lexists(clone), "the clone is deleted at the end")
        runs = os.path.join(self.w.evo, "runs")

        def containing(name, marker, parent=None):
            hits = []
            for path in files_named(runs, name):
                if parent and os.path.basename(os.path.dirname(path)) != parent:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    if marker in f.read():
                        hits.append(path)
            return hits

        self.assertTrue(containing("hooks.log", "refusal-marker-9d2b"),
                        f"the real refusal in the clone's hooks.log is not kept in the run folder:\n{self.out}")
        self.assertTrue(containing("history.jsonl", '"lint"', parent="gates"), "gates/history.jsonl is not kept")
        self.assertTrue(containing("standin-1.json", "approve", parent="reviews"), "reviews/ is not kept")
        self.assertTrue(containing("compactions.jsonl", "compaction-marker-5c1e"), "compactions.jsonl is not kept")

    def test_git_allow_rules_name_only_the_subcommands_a_build_needs(self):  # covers: REP-6
        git_rules = [r for r in self.allow if r.startswith("Bash") and command_tail(r, "git") is not None]
        bare = [r for r in git_rules if not command_tail(r, "git") or rule_allows(r, "git anything-at-all")]
        self.assertEqual(bare, [], "a bare git rule lets the agent run any git command")
        for sub in GIT_NEEDED:
            self.assertTrue(any(rule_allows(r, f"git {sub}") or rule_allows(r, f"git {sub} HEAD") for r in git_rules),
                            f"no allow rule for git {sub}: {git_rules}")
        for r in git_rules:
            tail = command_tail(r, "git")
            sub = tail.split()[0].rstrip(":")
            self.assertIn(sub, GIT_ALLOWED, f"{r}: git {sub} is not a command a build needs")
            if sub == "checkout":
                self.assertIn("--", tail.split()[1:], f"{r}: git checkout is allowed only for paths (after --)")
        for command in GIT_NEVER:
            self.assertEqual([r for r in self.allow if rule_allows(r, command)], [], f"an allow rule lets `{command}` run")

    def test_git_global_options_before_a_subcommand_are_refused(self):  # covers: REP-6
        bash_deny = [r for r in self.deny if r.startswith("Bash")]
        for command in ("git --no-pager log", "git -c core.pager=cat log"):
            self.assertTrue(any(rule_allows(r, command) for r in bash_deny),
                            f"no deny rule (--disallowedTools or --settings permissions.deny) refuses `{command}`: "
                            f"{bash_deny}")

    def test_regista_rules_only_for_the_build_commands(self):  # covers: REP-6
        rules = [r for r in self.allow if r.startswith("Bash") and command_tail(r, "regista.py") is not None]
        subs = set()
        for r in rules:
            words = command_tail(r, "regista.py").split()
            self.assertTrue(words, f"{r}: a bare regista.py rule allows fleet, deploy, release and the rest")
            sub = words[0].rstrip(":")
            self.assertIn(sub, REGISTA_ALLOWED, f"{r}: regista.py {sub} is not a command a build needs")
            if sub == "plan":
                self.assertEqual(words[1:2], ["tick"], f"{r}: only `plan tick` is allowed")
            subs.add(sub)
        for needed in ("gate", "review-record"):
            self.assertIn(needed, subs, f"the build loop needs regista.py {needed}: {rules}")
        for never in ("fleet", "deploy", "release", "run", "migrate", "init", "tests cleanup"):
            self.assertEqual([r for r in rules if command_tail(r, "regista.py").startswith(never)], [],
                             f"regista.py {never} must not be allowed")

    def test_npm_rules_only_ci_test_and_run_without_prefix_or_global(self):  # covers: REP-6
        rules = [r for r in self.allow if r.startswith("Bash") and command_tail(r, "npm") is not None]
        for r in rules:
            words = command_tail(r, "npm").split()
            self.assertTrue(words, f"{r}: a bare npm rule allows npm install -g and the rest")
            self.assertIn(words[0].rstrip(":"), NPM_ALLOWED, f"{r}: only npm ci, test and run are allowed")
            self.assertFalse(re.search(r"(^|\s)(--prefix|-g|--global)\b", r[5:]), f"{r}: --prefix and -g are refused")
        if any(r.rstrip(")").endswith("*") for r in rules):
            bash_deny = [r for r in self.deny if r.startswith("Bash")]
            for command in ("npm run build --prefix /tmp/elsewhere", "npm ci -g", "npm test --global"):
                self.assertTrue(any(rule_allows(r, command) for r in bash_deny),
                                f"npm rules end in a wildcard, but nothing refuses `{command}`: {bash_deny}")

    def test_npm_dash_c_is_refused_like_prefix(self):  # covers: REP-7
        rules = [r for r in self.allow if r.startswith("Bash") and command_tail(r, "npm") is not None]
        for r in rules:
            self.assertFalse(re.search(r"(^|\s)-C(\s|=|$)", r[5:]), f"{r}: npm -C is --prefix by another name")
        bash_deny = [r for r in self.deny if r.startswith("Bash")]
        for command in ("npm -C /elsewhere ci", "npm ci -C /elsewhere", "npm test -C /elsewhere",
                        "npm run build -C /elsewhere"):
            allowed = [r for r in self.allow if rule_allows(r, command)]
            denied = [r for r in bash_deny if rule_allows(r, command)]
            self.assertTrue(not allowed or denied,
                            f"`{command}` is allowed by {allowed} and no deny rule refuses it: {bash_deny}")

    def test_project_gate_commands_are_allowed_as_exact_strings(self):  # covers: REP-6
        for gate in CONFIG["gates"]:
            self.assertIn(f"Bash({gate['run']})", self.allow, f"the {gate['name']} gate's command as an exact rule")


@unittest.skipUnless(sys.platform.startswith("linux"), "the sandbox runtime checked here is Linux's (bubblewrap, socat)")
class SandboxRequired(unittest.TestCase):
    """REP-6: without bwrap and socat on PATH, a replay refuses to start unless evolve.json opts out of the sandbox."""

    def world(self, **kw):
        w = Replay(self, "success", sandbox_tools=False, **kw)
        self.addCleanup(w.cleanup)
        w.set_scenario("success", wait=0)
        w.bare_path()
        return w

    def test_refuses_to_start_without_bubblewrap_and_socat(self):  # covers: REP-6
        w = self.world()
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertEqual(r.returncode, 2, out)
        lines = [x for x in out.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, f"one plain line, not a dump: {lines}")
        self.assertRegex(lines[0], re.compile(r"bubblewrap|bwrap", re.I), "the line names bubblewrap")
        self.assertRegex(lines[0], re.compile(r"socat", re.I), "the line names socat")
        self.assertEqual(w.lines("calls.jsonl"), [], "claude must not start without the sandbox")

    def test_require_sandbox_false_runs_and_says_unconfined(self):  # covers: REP-6
        w = self.world(require_sandbox=False)
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertEqual(r.returncode, 0, out)
        w.first_call(self, r)
        unconfined = re.compile(r"unconfined|without[ _-](the[ _-])?sandbox|no[ _-]sandbox", re.I)
        self.assertRegex(out, unconfined, "the output says the replay ran unconfined")
        result = w.result(self)
        said = (bool(unconfined.search(json.dumps(result))) or result.get("confined") is False
                or result.get("unconfined") is True or result.get("sandbox") in (False, "off", "none", "unconfined"))
        self.assertTrue(said, f"result.json says the replay ran unconfined: {result}")


class RequireSandboxOnlyTrueOrFalse(unittest.TestCase):
    """REP-7: require_sandbox set to anything but true or false is refused before anything starts."""

    def check(self, value, sandbox_tools):
        w = Replay(self, "success", sandbox_tools=sandbox_tools, require_sandbox=value)
        self.addCleanup(w.cleanup)
        w.set_scenario("success", wait=0)
        if not sandbox_tools:
            w.bare_path()
        before = os.path.join(w.tmp, "before-ran.txt")
        w.configure(replay_before={"doubler": "sh -c " + shlex.quote(f"echo before >> {shlex.quote(before)}")})
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        self.assertNotEqual(r.returncode, 0, f"require_sandbox={value!r} must be refused:\n{out}")
        lines = [x for x in out.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, f"one plain line, not a dump: {lines}")
        self.assertIn("require_sandbox", lines[0], "the line names the setting")
        self.assertRegex(lines[0], re.compile(r"\btrue\b.*\bfalse\b|\bfalse\b.*\btrue\b", re.I),
                         "the line says the setting takes only true or false")
        self.assertEqual(w.lines("calls.jsonl"), [], "claude must not start")
        self.assertFalse(os.path.exists(before), "nothing starts, not even replay_before")

    def test_values_other_than_true_or_false_are_refused_with_the_sandbox_tools(self):  # covers: REP-7
        for value in ("no", 1, None):
            with self.subTest(require_sandbox=value):
                self.check(value, sandbox_tools=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "the PATH without bwrap and socat is Linux's")
    def test_values_other_than_true_or_false_are_refused_without_the_sandbox_tools(self):  # covers: REP-7
        for value in ("no", 1, None):
            with self.subTest(require_sandbox=value):
                self.check(value, sandbox_tools=False)


class ReplayUnixSockets(unittest.TestCase):
    """REP-8: evolve.json replay_unix_sockets lets the replay's gates reach the listed local sockets. On Linux, where
    Claude Code ignores allowUnixSockets, all unix sockets are allowed and the known escape sockets are fenced off with
    denyRead and denyWrite instead; claude never gets the session bus or SSH agent addresses."""

    ESCAPES = ["/run/user", "/var/run/docker.sock", "/run/docker.sock"]

    @staticmethod
    def covered(required, entries):
        """Whether a denyRead or denyWrite list names the path itself or a folder holding it (`//` and `/**` read
        the way Claude Code reads them; no symlinks are followed, so /var/run and /run each need their own entry)."""
        required = os.path.normpath(required)
        for raw in entries:
            if not isinstance(raw, str):
                continue
            path = raw[1:] if raw.startswith("//") else raw
            path = os.path.normpath(re.sub(r"(/\*\*?)+$", "", path) or "/")
            if required == path or required.startswith(path.rstrip("/") + "/"):
                return True
        return False

    def test_listed_sockets_are_allowed_with_the_escape_sockets_fenced_off(self):  # covers: REP-8
        sockets = ["/var/run/postgresql/.s.PGSQL.5432"]
        w = Replay(self, "success", replay_unix_sockets={"doubler": sockets})
        self.addCleanup(w.cleanup)
        w.set_scenario("success", wait=0)
        agent_folder = os.path.join(w.tmp, "ssh-agent-fake")
        os.makedirs(agent_folder)
        w.extra_env.update({"SSH_AUTH_SOCK": os.path.join(agent_folder, "agent.4242"),
                            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus"})
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        call = w.first_call(self, r)
        for name in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"):
            self.assertNotIn(name, call["env"], f"claude's environment must not carry {name}")
        enabled = [s["sandbox"] for s in settings_of(call)
                   if isinstance(s.get("sandbox"), dict) and s["sandbox"].get("enabled") is True]
        self.assertTrue(enabled, f"the sandbox is turned on in --settings: {settings_of(call)}")
        for sb in enabled:
            network = sb.get("network") if isinstance(sb.get("network"), dict) else {}
            self.assertEqual(network.get("allowUnixSockets"), sockets,
                             f"sandbox.network.allowUnixSockets is exactly the listed sockets: {sb}")
            if not sys.platform.startswith("linux"):
                continue
            self.assertIs(network.get("allowAllUnixSockets"), True,
                          f"on Linux, sandbox.network.allowAllUnixSockets is true: {sb}")
            fs = sb.get("filesystem") if isinstance(sb.get("filesystem"), dict) else {}
            for key in ("denyRead", "denyWrite"):
                entries = fs.get(key) if isinstance(fs.get(key), list) else []
                for required in self.ESCAPES + [agent_folder]:
                    self.assertTrue(self.covered(required, entries),
                                    f"sandbox.filesystem.{key} fences off {required}: {entries}")


class ResumeRunsTheRecordedSetup(unittest.TestCase):
    """REP-7: the agent rewrites the clone's fleet.setup before hitting the usage limit; `replay --resume` still runs
    the setup recorded when the replay first started, never the clone's new one."""

    def test_resume_runs_the_recorded_setup_not_the_one_the_agent_wrote(self):  # covers: REP-7
        w = Replay(self, "rate_limit")
        self.addCleanup(w.cleanup)
        evil = os.path.join(w.tmp, "outside-the-clone.txt")
        w.configure(setup={"doubler": "sh -c " + shlex.quote('echo "$PWD" >> ../setup-ran.txt')})
        w.set_scenario("rate_limit", resets_at=int(time.time()) + 3600,
                       tamper="sh -c " + shlex.quote(f"echo tampered >> {shlex.quote(evil)}"))
        first = w.run()
        out = first.stdout + first.stderr
        self.assertEqual(first.returncode, 3, f"the usage limit pauses the replay:\n{out}")
        self.assertTrue(w.lines("tamper.jsonl"), f"the stand-in never rewrote the clone's setup:\n{out}")
        clone = w.first_call(self, first)["cwd"]
        marker = os.path.join(os.path.dirname(clone), "setup-ran.txt")
        self.assertTrue(os.path.isfile(marker), f"the recorded setup ran in the clone before claude:\n{out}")
        with open(marker, encoding="utf-8") as f:
            ran_before = len(f.read().splitlines())

        w.set_scenario("success", wait=0)
        second = w.run("--resume")
        out = second.stdout + second.stderr
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(evil), f"resume ran the setup the agent wrote into the clone:\n{out}")
        self.assertEqual(len(w.lines("calls.jsonl")), 2, f"claude starts again on resume:\n{out}")
        with open(marker, encoding="utf-8") as f:
            ran = f.read().splitlines()
        self.assertGreater(len(ran), ran_before, f"resume runs the originally recorded setup again:\n{out}")
        self.assertEqual(ran[-1], clone, "the recorded setup runs in the kept clone")


class LoginGatesAndTranscripts(unittest.TestCase):
    """REP-5: only the subscription login may run, replay_env can't bring credentials back, a gate run must cover
    every commit gate, and transcripts are found under CLAUDE_CONFIG_DIR."""

    def world(self, scenario, **kw):
        w = Replay(self, scenario, **kw)
        self.addCleanup(w.cleanup)
        return w

    def test_any_key_source_but_the_login_stops_the_run_at_once(self):  # covers: REP-5
        for source in ("ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"):
            with self.subTest(apiKeySource=source):
                w = self.world("api_key")
                w.set_scenario("api_key", api_key_source=source, ignore_sigint=1)  # only SIGTERM or SIGKILL ends it
                r = w.run()
                out = r.stdout + r.stderr
                self.assertNotIn("Traceback", out)
                call = w.first_call(self, r)
                self.assertNotEqual(r.returncode, 0, f"a run on {source} is aborted:\n{out}")
                after_start = w.started + r.seconds - call["time"]
                self.assertLess(after_start, 15, f"no 30 s grace period for {source}: ended {after_start:.1f} s "
                                                 "after claude started")
                recorded = out + "".join(open(p, encoding="utf-8").read() for p in w.results())
                self.assertRegex(recorded, re.compile(r"key|login|apiKeySource", re.I), "the reason names the key")

    def test_replay_env_never_brings_back_credentials_or_session_variables(self):  # covers: REP-5
        w = self.world("success")
        w.set_scenario("success", wait=0)
        w.configure(replay_env={"doubler": {"ANTHROPIC_API_KEY": "sk-ant-from-replay-env", "CLAUDECODE": "1",
                                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "REGISTA_WORKER": "w9",
                                            "STANDIN_FROM_REPLAY_ENV": "yes"}})
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        calls = w.lines("calls.jsonl")
        if not calls:  # refusing the whole replay is also acceptable
            self.assertNotEqual(r.returncode, 0, out)
            self.assertRegex(out, re.compile(r"ANTHROPIC_API_KEY|CLAUDECODE"), "the refusal names the variable")
            return
        names = set(calls[0]["env"])
        leaked = sorted(n for n in names if n in ("ANTHROPIC_API_KEY", "CLAUDECODE", "ANTHROPIC_BASE_URL",
                                                  "REGISTA_WORKER"))
        self.assertEqual(leaked, [], "replay_env brought these back into claude's environment")
        self.assertIn("STANDIN_FROM_REPLAY_ENV", names, "the other replay_env variables still reach claude")

    def test_a_gate_run_covering_only_some_commit_gates_does_not_pass(self):  # covers: REP-5
        w = self.world("success")
        w.set_scenario("success", wait=15, gates_only="tests")  # approving review, history has tests but not lint
        r = w.run()
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        signals = w.lines("signals.jsonl")
        self.assertEqual(signals, [], "the replay stopped claude as if the gates had passed, though lint never ran")
        outcome = str(w.result(self).get("outcome") or "")
        self.assertNotRegex(outcome, re.compile(r"^(approved?|pass(ed)?|success|succeeded|green|ok)$", re.I))

    def test_transcript_under_claude_config_dir_is_found_and_counted(self):  # covers: REP-5
        w = self.world("success")
        w.set_scenario("success", wait=0)
        config = os.path.join(w.tmp, "claude-config")
        os.makedirs(config)
        w.extra_env = {"CLAUDE_CONFIG_DIR": config}
        r = w.run()
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        call = w.first_call(self, r)
        self.assertIn("CLAUDE_CONFIG_DIR", call["env"], "the login lives in CLAUDE_CONFIG_DIR; claude needs it")
        sid = flag(call["argv"], "--session-id")
        self.assertTrue(glob.glob(os.path.join(config, "projects", "*", f"{sid}.jsonl")), "the stand-in wrote there")
        found = used_tokens(w.result(self).get("tokens"))
        self.assertIn(DEDUP_USED, found, f"tokens from the transcript under CLAUDE_CONFIG_DIR: {found}")


SCORED_AXES = ("gates", "requirements", "review_rounds", "finding_severity", "refusals", "correct_blocked", "tokens",
               "compactions")


def hard_failure_of(result):
    """result.json's hard_failure, at the top level or inside scores."""
    reason = result.get("hard_failure")
    if reason is None and isinstance(result.get("scores"), dict):
        reason = result["scores"].get("hard_failure")
    return reason


class EvolverKeepsItsOwnCopies(unittest.TestCase):
    """FIT-7: during the run the stand-in changes what the evolver has already copied from the clone's hooks.log or
    gate history. Assumes the evolver polls more often than every 30 s; when its copies sit in the run folder outside
    the clone (a file named hooks.log or history.jsonl), the stand-in waits only until they hold what it wrote."""

    def replay(self, scenario, **files):
        w = Replay(self, scenario)
        self.addCleanup(w.cleanup)
        # A line already in hooks.log before the run, as in a project with history; the setup runs in the clone.
        old = json.dumps({"at": "2026-10-28T16:00:00", "tool": "Bash", "input": {"command": "ls"},
                          "reason": "Regista: refused by role_guard.", "kind": "deny", "rule": "role_guard",
                          "role": "builder", "session": "earlier", "worker": None, "label": "unsure",
                          "label_by": "rule"})
        w.configure(setup={"doubler": "sh -c " + shlex.quote(f"printf '%s\\n' {shlex.quote(old)} >> .regista/hooks.log")})
        w.set_scenario(scenario, **files)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        self.assertTrue(w.lines("tampered.jsonl"), f"the stand-in never got to its change:\n{out}")
        return w, w.result(self), out

    def assert_tampered(self, result, out):
        reason = hard_failure_of(result)
        self.assertIsInstance(reason, str, f"expected the hard failure 'records tampered', got {result}\n{out}")
        self.assertIn("records tampered", reason.lower())
        scores = result.get("scores") or {}
        not_failed = [a for a in SCORED_AXES if not (scores.get(a) or {}).get("failed")]
        self.assertEqual(not_failed, [], f"a hard failure fails every axis: {scores}")

    def test_hooks_log_truncated_back_to_its_size_before_the_run_is_records_tampered(self):  # covers: FIT-7
        w, result, out = self.replay("log_truncate")
        self.assertGreater(w.lines("tampered.jsonl")[0]["pre"], 0, "the line from before the run is there")
        self.assert_tampered(result, out)

    def test_refusal_line_rewritten_in_place_is_records_tampered(self):  # covers: FIT-7
        _w, result, out = self.replay("log_rewrite")
        self.assert_tampered(result, out)

    def test_33_mb_written_to_hooks_log_between_polls_is_records_tampered_not_skipped(self):  # covers: FIT-7
        _w, result, out = self.replay("log_junk")
        self.assert_tampered(result, out)

    def test_failing_gate_row_rewritten_to_pass_is_records_tampered(self):  # covers: FIT-7
        _w, result, out = self.replay("gate_rewrite")
        self.assert_tampered(result, out)

    def test_passing_gate_row_backdated_into_an_earlier_commit_window_does_not_count(self):  # covers: FIT-7
        w, result, out = self.replay("gate_backdate")
        info = w.lines("tampered.jsonl")[0]
        self.assertGreater(info["written"] - info["t0"], 60, "the row appears over a minute after the time it claims")
        self.assertEqual(w.lines("signals.jsonl"), [],
                         f"the replay stopped claude as if the backdated pass counted:\n{out}")
        gates = (result.get("scores") or {}).get("gates") or {}
        self.assertIs(gates.get("failed"), True, f"the only passing row was backdated: {result}")


TESTS_GATE = CONFIG["gates"][0]["run"]  # the baseline's tests gate: python3 -m unittest discover -s tests/unit
AGENT_GATE = "agent-gate-7f3a"
WRITE_BINDS = ("--bind", "--bind-try", "--dev-bind", "--dev-bind-try")
UNVERIFIED = re.compile(r"unverified", re.I)
APPROVED = re.compile(r"^(approved?|pass(ed)?|success|succeeded|green|ok)$", re.I)


def gate_calls(w, command):
    """The stand-in bwrap's records of runs whose command (or arguments) include `command`."""
    return [c for c in w.lines("bwrap.jsonl")
            if command in " ".join(c.get("command") or []) or command in " ".join(c.get("argv") or [])]


def axis(result, name):
    return (result.get("scores") or {}).get(name) or {}


class FinalGateRunHonest(unittest.TestCase):
    """FIT-8: one replay where the code really passes the frozen tests; the evolver's own final gate run decides."""

    @classmethod
    def setUpClass(cls):
        cls.w = None
        if _TEMPLATE["build"].returncode != 0 or not _TEMPLATE["case"]:
            return
        cls.w = _class_world("final_honest")
        cls.r = cls.w.run(timeout=280)

    @classmethod
    def tearDownClass(cls):
        if cls.w:
            cls.w.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.w, f"bench build failed: {_TEMPLATE['build'].stderr}")
        self.out = self.r.stdout + self.r.stderr
        self.assertNotIn("Traceback", self.out)
        self.clone = self.w.first_call(self, self.r)["cwd"]
        self.result = self.w.result(self)

    def test_code_passing_the_frozen_tests_passes_gates_and_requirements_in_the_final_run(self):  # covers: FIT-8
        self.assertIsNone(hard_failure_of(self.result), f"an honest run broke no rule:\n{self.out}")
        self.assertIs(axis(self.result, "gates").get("failed"), False, f"the final run passed: {self.result}")
        self.assertIs(axis(self.result, "requirements").get("failed"), False, f"the final run passed: {self.result}")
        self.assertNotRegex(str(self.result.get("outcome") or ""), UNVERIFIED, "the final run passed")
        runs = gate_calls(self.w, TESTS_GATE)
        self.assertTrue(runs, f"the evolver never ran the tests gate itself through bwrap:\n{self.out}")
        self.assertEqual([c["exit"] for c in runs if c["exit"] != 0], [], "the final tests gate run passed")
        self.assertEqual(runs[-1]["frozen"], TEST_DOUBLE, "the final run saw the case's frozen test")
        self.assertTrue(gate_calls(self.w, "lint ok"), "every commit gate runs in the final run, lint too")

    def test_final_gate_run_goes_through_bubblewrap_with_write_access_to_the_clone_only(self):  # covers: FIT-8
        runs = gate_calls(self.w, TESTS_GATE) + gate_calls(self.w, "lint ok")
        self.assertTrue(runs, f"the final gate run was not started through bwrap:\n{self.out}")
        for call in runs:
            writable = [b for b in call["binds"] if b[0] in WRITE_BINDS
                        and not (b[0].startswith("--dev-bind") and inside(b[1], "/dev"))]
            self.assertTrue(any(inside(b[1], self.clone) for b in writable),
                            f"the gates need write access to the clone: {call['argv']}")
            outside = [b for b in writable if not inside(b[1], self.clone)]
            self.assertEqual(outside, [], f"bwrap gives write access outside the clone: {call['argv']}")


class FinalGateRunDecides(unittest.TestCase):
    """FIT-8: the gate history and records in the clone can't make a run pass; only the evolver's final run can."""

    def replay(self, scenario, **files):
        w = Replay(self, scenario)
        self.addCleanup(w.cleanup)
        w.set_scenario(scenario, **files)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        return w, w.result(self), out

    def test_passing_gate_rows_with_code_failing_the_frozen_tests_fail_gates_and_leave_it_unverified(self):  # covers: FIT-8
        w, result, out = self.replay("final_fake")
        self.assertTrue(w.lines("final.jsonl"), f"the stand-in never wrote its passing records:\n{out}")
        self.assertIsNone(hard_failure_of(result), f"no rule was broken; only the code is wrong:\n{out}")
        self.assertIs(axis(result, "gates").get("failed"), True,
                      f"backed passing gate rows, but the stub still fails the frozen test: {result}")
        self.assertIs(axis(result, "requirements").get("failed"), True,
                      f"requirements.json says every requirement passes, but the final run failed: {result}")
        outcome = str(result.get("outcome") or "")
        self.assertNotRegex(outcome, APPROVED, "an approved run whose final gate run fails is not approved")
        self.assertRegex(outcome, UNVERIFIED, f"approved becomes unverified when the final run fails: {result}")

    def test_frozen_test_edited_to_pass_is_restored_for_the_final_run(self):  # covers: FIT-8
        edited = TEST_DOUBLE.replace("double(4), 8)", "double(4), 9)")
        self.assertNotEqual(edited, TEST_DOUBLE)
        w, result, out = self.replay("final_edit", edited_test=edited)
        self.assertTrue(w.lines("final.jsonl"), f"the stand-in never edited the frozen test:\n{out}")
        reason = hard_failure_of(result)
        self.assertIsInstance(reason, str, f"changing a frozen test is still a hard failure: {result}")
        self.assertRegex(reason, re.compile(r"test_double\.py|frozen", re.I))
        self.assertIs(axis(result, "gates").get("failed"), True, result)
        runs = gate_calls(w, TESTS_GATE)
        self.assertTrue(runs, f"the evolver never ran the tests gate itself:\n{out}")
        self.assertEqual(runs[-1]["frozen"], TEST_DOUBLE, "the final run uses the case's copy, not the agent's edit")
        self.assertNotEqual(runs[-1]["exit"], 0, "2n+1 passes only the edited test, so the final run fails")

    def test_final_run_uses_the_baseline_gate_command_not_the_one_in_the_clone(self):  # covers: FIT-8
        w, result, out = self.replay("final_swap")
        self.assertTrue(w.lines("final.jsonl"), f"the stand-in never changed the clone's gate:\n{out}")
        self.assertTrue(gate_calls(w, TESTS_GATE), f"the baseline's tests gate never ran in the final run:\n{out}")
        self.assertEqual(gate_calls(w, AGENT_GATE), [], "the final run ran the gate the agent wrote into the clone")
        self.assertIs(axis(result, "gates").get("failed"), True, f"the stub fails the baseline's tests gate: {result}")

    def test_hooks_log_line_glued_to_a_stray_byte_is_records_tampered(self):  # covers: FIT-8
        w, result, out = self.replay("log_glued")
        self.assertTrue(w.lines("tampered.jsonl"), f"the stand-in never wrote the stray byte:\n{out}")
        reason = hard_failure_of(result)
        self.assertIsInstance(reason, str, f"a line that isn't valid JSON is a hard failure: {result}\n{out}")
        self.assertIn("records tampered", reason.lower())
        scores = result.get("scores") or {}
        self.assertEqual([a for a in SCORED_AXES if not (scores.get(a) or {}).get("failed")], [],
                         f"a hard failure fails every axis: {scores}")

    def test_protected_path_check_where_git_fails_is_reported_and_treated_as_changed(self):  # covers: FIT-8
        w, result, out = self.replay("final_git", wait=5)
        broken = w.lines("corrupted.jsonl")
        self.assertTrue(broken, f"the stand-in never broke the clone's git index:\n{out}")
        self.assertNotEqual(broken[0]["status_exit"], 0, f"git should fail on the broken index: {broken}")
        reason = hard_failure_of(result)
        self.assertIsInstance(reason, str, f"when git can't tell, protected paths count as changed: {result}\n{out}")
        self.assertRegex(reason, re.compile(r"protected|git", re.I))
        self.assertRegex(out + json.dumps(result), re.compile(r"\bgit\b", re.I), "the problem with git is reported")
        scores = result.get("scores") or {}
        self.assertEqual([a for a in SCORED_AXES if not (scores.get(a) or {}).get("failed")], [],
                         f"a hard failure fails every axis: {scores}")


class ChecksAfterTheFinalRun(unittest.TestCase):
    """FIT-9: protected paths are checked again after the evolver's own final gate run."""

    def test_protected_file_written_only_during_the_final_gate_run_is_a_hard_failure(self):  # covers: FIT-9
        w = Replay(self, "final_env")
        self.addCleanup(w.cleanup)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        final, writes = w.lines("final.jsonl"), w.lines("env_writes.jsonl")
        self.assertTrue(final, f"the stand-in never committed its code:\n{out}")
        self.assertTrue(writes, f"the final gate run never imported the code that writes .env:\n{out}")
        self.assertGreater(writes[0]["time"], final[0]["time"], ".env appears only after the agent's run ended")
        self.assertTrue(gate_calls(w, TESTS_GATE), f"the evolver never ran the tests gate itself:\n{out}")
        result = w.result(self)
        reason = hard_failure_of(result)
        self.assertIsInstance(reason, str, f"a protected file written during the final gate run: {result}\n{out}")
        self.assertRegex(reason, re.compile(r"\.env|protected", re.I))
        scores = result.get("scores") or {}
        self.assertEqual([a for a in SCORED_AXES if not (scores.get(a) or {}).get("failed")], [],
                         f"a hard failure fails every axis: {scores}")


class EarlyStopNeedsABackedGateRun(unittest.TestCase):
    """FIT-6 for the early stop: since FIT-8 the score ignores the gate history, but an approving review stops the
    run early only together with a passing gate run that a transcript call backs."""

    def replay(self, backing):
        w = Replay(self, "gate_unbacked", replay_resumes=0)  # about the early stop, not REP-9's resumes
        self.addCleanup(w.cleanup)
        w.set_scenario("gate_unbacked", backing=backing)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        self.assertTrue(w.lines("unbacked.jsonl"), f"the stand-in never wrote its gate row:\n{out}")
        return w, out

    def test_passing_gate_row_no_call_backs_does_not_stop_the_run(self):  # covers: FIT-6
        w, out = self.replay("")
        self.assertEqual(w.lines("signals.jsonl"), [],
                         f"nothing ran the gates when the passing row claims, yet the replay stopped claude:\n{out}")

    def test_passing_gate_row_backed_by_the_harness_or_the_gate_commands_stops_the_run(self):  # covers: FIT-6
        for backing in ("harness", "own"):
            with self.subTest(backing):
                w, out = self.replay(backing)
                signals = [s["signal"] for s in w.lines("signals.jsonl")]
                self.assertEqual(signals[:1], ["SIGINT"], f"a backed passing row and a backed approval:\n{out}")


def resumes_of(result):
    """Every whole number result.json records under a key naming resumes, at any depth."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if "resume" in str(key).lower() and isinstance(value, int) and not isinstance(value, bool):
                    found.append(value)
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(result)
    return found


class ResumesASessionThatEndsWithoutAnOutcome(unittest.TestCase):
    """REP-9: claude ending its turn with no approving review, no BLOCKED and no hard failure is resumed in the same
    session, at most replay_resumes times."""

    def world(self, scenario, **evolve_json):
        w = Replay(self, scenario, **evolve_json)
        self.addCleanup(w.cleanup)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        return w, w.lines("calls.jsonl"), out

    def assert_resumed(self, first, later):
        sid = flag(first["argv"], "--session-id")
        self.assertEqual(flag(later["argv"], "--resume"), sid, f"the same session is resumed: {later['argv']}")
        self.assertTrue({"-p", "--print"} & set(later["argv"]), later["argv"])
        self.assertEqual(later["cwd"], first["cwd"], "the resumed session runs in the same clone")
        for name in ("--model", "--plugin-dir", "--output-format", "--permission-mode", "--allowedTools"):
            self.assertEqual(flag(later["argv"], name), flag(first["argv"], name), f"same {name} on resume")
        self.assertEqual(settings_of(later), settings_of(first), "same settings on resume")

    def test_session_ending_without_an_outcome_is_resumed_and_ends_approved(self):  # covers: REP-9
        w, calls, out = self.world("resume_approve")
        self.assertEqual(len(calls), 2, f"claude ended without an outcome, so the evolver resumes it once:\n{out}")
        self.assert_resumed(calls[0], calls[1])
        result = w.result(self)
        self.assertIsNone(hard_failure_of(result), f"no rule was broken:\n{out}")
        self.assertRegex(str(result.get("outcome") or ""), APPROVED, f"the resumed part was approved: {result}")
        self.assertIn(1, resumes_of(result), f"result.json records one resume: {result}")
        found = used_tokens(result.get("tokens"))
        self.assertIn(DEDUP_USED, found, f"tokens of both parts, each message id once: {found}")
        self.assertNotIn(DEDUP_USED + 115, found, f"the first part's tokens were counted twice: {found}")

    def test_resumes_stop_after_replay_resumes(self):  # covers: REP-9
        w, calls, out = self.world("no_outcome", replay_resumes=2)
        self.assertEqual(len(calls), 3, f"the first call and two resumes, no more:\n{out}")
        for later in calls[1:]:
            self.assert_resumed(calls[0], later)
        result = w.result(self)
        self.assertNotRegex(str(result.get("outcome") or ""), APPROVED, f"nothing was approved: {result}")
        self.assertIn(2, resumes_of(result), f"result.json records two resumes: {result}")


def gate_records(result, name):
    """result.json's records of one gate: dicts named after it, or a dict under a key with its name, at any depth."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if name in (obj.get("name"), obj.get("gate")):
                found.append(obj)
            if isinstance(obj.get(name), dict):
                found.append(obj[name])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
    walk(result)
    return found


def is_flaky(record):
    return record.get("flaky") is True or any(isinstance(v, str) and v.lower() == "flaky" for v in record.values())


def runs_recorded(record):
    """The lists of two or more runs a gate record holds (both runs of a gate run again)."""
    return [v for v in record.values() if isinstance(v, list) and len(v) >= 2]


class FinalGateRunAgainOnFailure(unittest.TestCase):
    """REP-10: a gate failing in the evolver's final run is run once more and passes only if that run passes."""

    def replay(self, fail_first):
        w = Replay(self, "final_flaky")
        self.addCleanup(w.cleanup)
        w.set_scenario("final_flaky", fail_first=fail_first)
        r = w.run(timeout=280)
        out = r.stdout + r.stderr
        self.assertNotIn("Traceback", out)
        w.first_call(self, r)
        self.assertTrue(w.lines("final.jsonl"), f"the stand-in never committed its code:\n{out}")
        return w, w.result(self), out

    def test_gate_failing_once_then_passing_is_a_pass_marked_flaky(self):  # covers: REP-10
        w, result, out = self.replay(1)
        exits = [c["exit"] for c in gate_calls(w, TESTS_GATE)]
        self.assertEqual([e != 0 for e in exits], [True, False], f"the tests gate failed, then passed when run again:\n{out}")
        self.assertIsNone(hard_failure_of(result), f"no rule was broken:\n{out}")
        self.assertIs(axis(result, "gates").get("failed"), False, f"the second run passed, so the gate passes: {result}")
        self.assertNotRegex(str(result.get("outcome") or ""), UNVERIFIED, result)
        records = gate_records(result, "tests")
        self.assertTrue([g for g in records if is_flaky(g) and runs_recorded(g)],
                        f"the tests gate is marked flaky with both runs recorded: {records}")

    def test_gate_failing_twice_is_a_fail(self):  # covers: REP-10
        w, result, out = self.replay(2)
        exits = [c["exit"] for c in gate_calls(w, TESTS_GATE)]
        self.assertEqual([e != 0 for e in exits], [True, True], f"the tests gate runs twice and only twice:\n{out}")
        self.assertIs(axis(result, "gates").get("failed"), True, f"both runs failed: {result}")
        records = gate_records(result, "tests")
        self.assertTrue([g for g in records if runs_recorded(g)], f"both runs are recorded: {records}")
        self.assertEqual([g for g in records if is_flaky(g)], [], f"two failures agree, so it isn't flaky: {records}")


if __name__ == "__main__":
    unittest.main()
