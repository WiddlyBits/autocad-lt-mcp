"""The pure logic in scripts/install.ps1: Startup Suite merge, trusted paths, client configs.

The registry, AutoCAD and client steps need a Windows machine and stay out of CI. These
tests dot-source the script (which defines its functions without running the install)
and call the helpers through whichever PowerShell is present: powershell.exe 5.1 locally,
pwsh 7 on the ubuntu runner.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install.ps1"
SHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(SHELL is None, reason="no PowerShell on PATH")

LISP = r"C:\Users\andy\autocad-lt-mcp\lisp-code"
PY = r"C:\Users\andy\autocad-lt-mcp\.venv\Scripts\python.exe"


def ps(expr: str, **values):
    """Run `expr` after dot-sourcing the script; values arrive as $v.<name>. Returns JSON-decoded output."""
    # Values go through the environment: quotes on a Windows command line get mangled.
    cmd = (
        f"$ErrorActionPreference='Stop'; . '{SCRIPT}'; "
        "$v = $env:INSTALL_TEST_VALUES | ConvertFrom-Json; "
        f"ConvertTo-Json -Compress -Depth 10 -InputObject $({expr})"
    )
    out = subprocess.run(
        [SHELL, "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "INSTALL_TEST_VALUES": json.dumps(values)},
    )
    assert out.returncode == 0, out.stderr
    # PowerShell 5.1 prints nothing for $null where pwsh prints "null".
    return json.loads(out.stdout) if out.stdout.strip() else None


def merge_startup(entries):
    return ps("Merge-StartupList @($v.entries) $v.lisp", entries=entries, lisp=LISP)


def test_startup_adds_dispatch_to_empty_list():
    assert merge_startup([]) == [LISP + r"\mcp_dispatch.lsp"]


def test_startup_replaces_entries_from_renamed_clone_and_keeps_others():
    old = r"C:\Users\andy\autocad-mcp\lisp-code"
    got = merge_startup([
        r"C:\tools\other.lsp",
        old + r"\mcp_dispatch.lsp",
        old + r"\mcp_probes.lsp",
        r"D:\x\mcp_dispatch_notes.lsp",
    ])
    assert got == [r"C:\tools\other.lsp", r"D:\x\mcp_dispatch_notes.lsp", LISP + r"\mcp_dispatch.lsp"]


def test_startup_keeps_current_entries_and_is_idempotent():
    current = [LISP + r"\mcp_dispatch.lsp", LISP + r"\mcp_probes.lsp"]
    assert merge_startup(current) == current
    # Different case doesn't make it look stale.
    assert merge_startup([LISP.upper() + r"\MCP_DISPATCH.LSP"]) == [LISP.upper() + r"\MCP_DISPATCH.LSP"]


def test_startup_entry_with_env_var_is_current():
    # APPLOAD writes %USERPROFILE%\... (REG_EXPAND_SZ); the entry must survive as written.
    entry = r"%INSTALL_TEST_HOME%\autocad-lt-mcp\lisp-code\mcp_dispatch.lsp"
    got = ps(r"$env:INSTALL_TEST_HOME = 'C:\Users\andy'; Merge-StartupList @($v.e) $v.lisp", e=entry, lisp=LISP)
    assert got == [entry]


@pytest.mark.parametrize(
    ("current", "secure", "want"),
    [
        ("", "0", ""),  # SECURELOAD off: nothing to trust
        ("", "1", LISP),
        (r"C:\a;C:\b", "2", r"C:\a;C:\b;" + LISP),
        (LISP + "\\", "1", LISP + "\\"),  # already there
    ],
)
def test_trusted_path(current, secure, want):
    assert ps("Add-TrustedPath $v.cur $v.lisp $v.sec", cur=current, lisp=LISP, sec=secure) == want


def merge_json(text):
    return json.loads(ps("Merge-McpJson $v.text $v.py", text=text, py=PY))


def test_json_upgrade_from_old_server_keeps_other_servers():
    old = {"mcpServers": {
        "autocad-mcp": {"command": r"C:\old\python.exe", "args": ["-m", "autocad_mcp"]},
        "other": {"command": "x", "args": [], "env": {"K": "1"}},
    }}
    got = merge_json(json.dumps(old))["mcpServers"]
    assert set(got) == {"other", "autocad-lt-mcp"}
    assert got["other"] == {"command": "x", "args": [], "env": {"K": "1"}}
    assert got["autocad-lt-mcp"] == {
        "command": PY, "args": ["-m", "autocad_lt_mcp"], "env": {"AUTOCAD_MCP_BACKEND": "auto"}}


def test_json_existing_entry_keeps_its_env_and_run_twice_is_stable():
    start = json.dumps({"mcpServers": {"autocad-lt-mcp": {
        "command": r"C:\moved\python.exe", "args": ["-m", "autocad_lt_mcp"],
        "env": {"AUTOCAD_MCP_BACKEND": "ezdxf"}}}})
    once = ps("Merge-McpJson $v.text $v.py", text=start, py=PY)
    twice = ps("Merge-McpJson $v.text $v.py", text=once, py=PY)
    assert once == twice
    entry = json.loads(once)["mcpServers"]["autocad-lt-mcp"]
    assert entry["command"] == PY and entry["env"] == {"AUTOCAD_MCP_BACKEND": "ezdxf"}
    # Key order kept, or the next run sees a "change" and rewrites the file.
    assert list(entry) == ["command", "args", "env"]


def test_json_upgrade_then_rerun_is_byte_identical():
    old = json.dumps({"mcpServers": {"autocad-mcp": {"command": "x", "args": []}}})
    once = ps("Merge-McpJson $v.text $v.py", text=old, py=PY)
    assert ps("Merge-McpJson $v.text $v.py", text=once, py=PY) == once


def test_json_from_empty_file():
    assert merge_json("")["mcpServers"]["autocad-lt-mcp"]["command"] == PY


CODEX = (
    "model = 'x'\n\n[mcp_servers.autocad-lt-mcp]\n"
    "command = 'C:\\Users\\andy\\autocad-lt-mcp\\.venv\\Scripts\\python.exe'\n"
    'args = ["-m", "autocad_lt_mcp"]\n\n'
    '[mcp_servers.autocad-lt-mcp.env]\nAUTOCAD_MCP_BACKEND = "ezdxf"\n'
)


def test_codex_reads_command_and_ignores_env_table():
    assert ps("Get-CodexCommand $v.t 'autocad-lt-mcp'", t=CODEX) == PY
    assert ps("Get-CodexCommand $v.t 'autocad-mcp'", t=CODEX) is None


def test_codex_new_block_round_trips():
    block = ps("New-CodexBlock $v.py", py=PY)
    assert ps("Get-CodexCommand $v.t 'autocad-lt-mcp'", t=block) == PY
    assert 'AUTOCAD_MCP_BACKEND = "auto"' in block


def test_dot_sourcing_does_not_run_the_install():
    out = subprocess.run(
        [SHELL, "-NoProfile", "-NonInteractive", "-Command", f". '{SCRIPT}'; 'loaded'"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip() == "loaded", out.stdout + out.stderr
