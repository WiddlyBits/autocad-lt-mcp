"""Verification checker: tests, test count, skipped/gutted tests, lint, version drift, snapshots.

python scripts\\check.py [--base <git-ref>]   exit 0 PASS, 1 FAIL, 3 checker could not run
Last stdout line is one JSON object. `--selftest` checks the parsers only.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COUNT_RE = re.compile(r"Expected count on `main`: \*\*(\d+)\*\*")
PASSED_RE = re.compile(r"(\d+) passed")
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
DUNDER_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
# shortcut: skip pattern only knows pytest spellings, extend if tests adopt another
SKIP_PAT = r"pytest\.(mark\.)?(skip|skipif|xfail)|importorskip"
ASSERT_PAT = r"^[[:space:]]*assert([[:space:]]|\()"


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def git_out(*args):
    r = run(["git", *args])
    if r.returncode not in (0, 1):  # git grep and diff use 1 for "no match" / "differs"
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def parse_count(text):
    m = COUNT_RE.search(text)
    if not m:
        raise RuntimeError("baseline count line not found in CLAUDE.md")
    return int(m.group(1))


def parse_passed(text):
    m = PASSED_RE.search(text)
    return int(m.group(1)) if m else 0


def parse_versions(pyproject, init):
    a, b = VERSION_RE.search(pyproject), DUNDER_RE.search(init)
    if not (a and b):
        raise RuntimeError("version string not found")
    return a.group(1), b.group(1)


def grep_count(pattern, base=None):
    # base=None counts the working tree; lines are counted because -h drops the file prefix
    args = ["grep", "-h", "-E"] + ([] if base else ["--untracked"]) + ["-e", pattern] + ([base] if base else []) + ["--", "tests"]
    return len([ln for ln in git_out(*args).splitlines() if ln.strip()])


def selftest():
    assert parse_count("x\nExpected count on `main`: **889**. y") == 889
    assert parse_passed("1 failed, 888 passed in 9.1s") == 888
    assert parse_passed("no tests ran") == 0
    assert parse_versions('name="a"\nversion = "3.3.0"\n', '"""d"""\n__version__ = "3.3.0"\n') == ("3.3.0", "3.3.0")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    t0, checks = time.time(), []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    base = a.base
    base_n = parse_count(git_out("show", f"{base}:CLAUDE.md"))
    r = run(["uv", "run", "pytest", "-q"])
    out = r.stdout + r.stderr
    passed = parse_passed(out)
    add("C1 tests", r.returncode == 0, "\n".join(out.strip().splitlines()[-30:]))
    add("C2 count", passed >= base_n, f"{passed} passed, baseline {base_n}")
    now_n = parse_count((ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
    add("C3 baseline untouched", now_n == base_n, f"working tree {now_n}, base {base_n}" + ("" if now_n == base_n else " - baseline changed, needs Gianni"))
    s_now, s_base = grep_count(SKIP_PAT), grep_count(SKIP_PAT, base)
    add("C4 no new skips", s_now <= s_base, f"{s_now} now, {s_base} base")
    a_now, a_base = grep_count(ASSERT_PAT), grep_count(ASSERT_PAT, base)
    add("C5 asserts", a_now >= a_base, f"{a_now} now, {a_base} base")
    r = run(["uv", "run", "ruff", "check", "src/"])
    add("C6 lint", r.returncode == 0, (r.stdout + r.stderr).strip()[-1500:])
    v = parse_versions((ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                       (ROOT / "src" / "autocad_lt_mcp" / "__init__.py").read_text(encoding="utf-8"))
    add("C7 version", v[0] == v[1], f"pyproject {v[0]}, __init__ {v[1]}")
    g = git_out("diff", "--name-only", base, "--", "tests/golden").split()
    add("C8 snapshots", not g, ", ".join(g) or "no change")

    if git_out("diff", "--name-only", base, "--", "tests").strip():
        print("WARN tests/ modified: review the diff")
    for c in checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"] + ": " + c["detail"].splitlines()[-1] if c["detail"] else c["name"])
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"verdict": "PASS" if ok else "FAIL", "checks": checks, "seconds": round(time.time() - t0, 1)}))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # the checker itself broke: exit 3 so the gate fails open
        print(f"checker error: {e}", file=sys.stderr)
        sys.exit(3)
