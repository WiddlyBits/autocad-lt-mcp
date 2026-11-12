"""Stop-hook gate: runs scripts\\check.py when a gated checkout has changes, blocks the stop on FAIL.

Gated only while <project>\\.claude\\gate.on exists (it holds the HEAD sha from when the gate was switched on).
Fails open on any crash or checker error, and gives up after 3 blocks in one turn.
`--selftest` checks the reason builder only.
"""
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

MAX_TRIES = 3
CHECK_TIMEOUT = 280  # seconds; hook timeout in settings is 300


def build_reason(tries, out):
    last = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        failed = [c["name"] for c in json.loads(last)["checks"] if not c["ok"]]
    except Exception:  # last line was not the JSON summary
        failed = ["see output"]
    tail = "\n".join(out.strip().splitlines()[-30:])
    return (f"Gate: check.py FAILED ({', '.join(failed)}).\n{tail}\n"
            f"Gate attempt {tries} of {MAX_TRIES}. Fix the cause; do not weaken or delete tests. "
            f"On attempt {MAX_TRIES}, if it still fails, stop and report the failure verbatim.")


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


def main():
    data = json.load(sys.stdin)
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data["cwd"])
    flag, state_f, log_f = (root / ".claude" / n for n in ("gate.on", "gate.state.json", "gate.log"))

    def log(msg):
        with log_f.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")

    if not flag.exists():
        return 0
    base = flag.read_text().strip()
    if not git(root, "status", "--porcelain") and git(root, "rev-parse", "HEAD") == base:
        return 0  # nothing changed since the gate went on

    sid = data.get("session_id")
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    tries = state.get("tries", 0) if data.get("stop_hook_active") and state.get("session_id") == sid else 0

    try:
        r = subprocess.run([sys.executable, str(root / "scripts" / "check.py"), "--base", base],
                           cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("ERROR check.py timed out, failing open")
        return 0
    if r.returncode == 0:
        state_f.unlink(missing_ok=True)
        log("PASS")
        return 0
    if r.returncode != 1:  # 3 = checker broke; 2 = python could not find the file
        log(f"ERROR check.py exit {r.returncode}: {r.stderr.strip()[-300:]}, failing open")
        return 0
    tries += 1
    state_f.write_text(json.dumps({"session_id": sid, "tries": tries}))
    if tries > MAX_TRIES:
        log("GAVE_UP")
        return 0
    log(f"BLOCK {tries}")
    print(json.dumps({"decision": "block", "reason": build_reason(tries, r.stdout + r.stderr)}))
    return 0


def selftest():
    ok = build_reason(1, 'x\n{"verdict":"FAIL","checks":[{"name":"C1 tests","ok":false,"detail":""},'
                         '{"name":"C6 lint","ok":true,"detail":""}],"seconds":1}')
    head = ok.splitlines()[0]  # the tail repeats the JSON, so only the header names failures
    assert "C1 tests" in head and "C6 lint" not in head and "attempt 1 of 3" in ok
    assert "see output" in build_reason(2, "garbage")
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # never trap a session behind a broken gate
        try:
            p = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / ".claude" / "gate.log"
            p.parent.mkdir(exist_ok=True)
            p.open("a", encoding="utf-8").write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ERROR {traceback.format_exc()}\n")
        except Exception:
            pass
        sys.exit(0)
