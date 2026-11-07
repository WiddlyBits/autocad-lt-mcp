"""Dispatch claim, .dwt SAVEAS format, and plot_pdf DBMOD reporting.

The DO Draft 2 build (2026-10-08) hit three dispatcher behaviours that each
cost hand-fired LISP to work around:

- every execute_lisp timeout came back may_have_applied:true, including the
  read-only (mcp:sys-snapshot), because Python could not tell "never started"
  from "still running";
- drawing(save, path) from an open .dwt answered the format prompt with "",
  which keeps the Template format;
- plot_pdf left a saved drawing at DBMOD 5 and said nothing.

The IPC tests run the real _dispatch_unlocked against a temp dir, with the
keystroke trigger replaced by a fake LISP side. The LISP checks are structural.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_probes_lisp import _defun_body as defun_body
from tests.test_probes_lisp import strip_lisp

DISPATCH_LSP = Path(__file__).parent.parent / "lisp-code" / "mcp_dispatch.lsp"


def _backend(tmp_path, version):
    from autocad_lt_mcp.backends.file_ipc import FileIPCBackend

    backend = FileIPCBackend()
    backend._ipc_dir = tmp_path
    backend._status_file = tmp_path / "mcp_status.txt"
    backend._dispatcher_version = version
    return backend


class TestDrawingCreateIsRefused:
    """drawing(create) ran ERASE and PURGE on the active document and returned
    ok:true: on 2026-11-04 it took a working file's Layout1 from 26 entities to 1."""

    @pytest.mark.asyncio
    async def test_refused_before_anything_reaches_autocad(self, tmp_path):
        backend = _backend(tmp_path, 2)
        backend._dispatch = AsyncMock(side_effect=AssertionError("dispatcher used"))
        result = await backend.drawing_create("scratch")
        assert not result.ok
        assert "nothing was changed" in result.error

    def test_the_erasing_handler_is_gone(self):
        assert "drawing-create" not in DISPATCH_LSP.read_text(encoding="utf-8")


async def _run(backend, command, on_trigger=lambda d: None):
    """Dispatch with the trigger replaced; returns (result, trigger count)."""
    calls = []

    def trigger(inject_esc=False):
        calls.append(inject_esc)
        on_trigger(backend._ipc_dir)

    with patch.object(sys, "platform", "linux"), \
            patch("autocad_lt_mcp.backends.file_ipc.TIMEOUT", 0.3), \
            patch.object(backend, "_type_dispatch_trigger", side_effect=trigger):
        result = await backend._dispatch_unlocked(command, {"code": "(mcp:sys-snapshot)"})
    return result, len(calls)


def _claim(ipc_dir):
    """What the v2 LISP does first: rename cmd_<id> to run_<id>, then hang."""
    for f in ipc_dir.glob("autocad_lt_mcp_cmd_*.json"):
        f.rename(f.with_name(f.name.replace("_cmd_", "_run_")))


async def _dispatch_answered(backend, ipc_dir, skip=None):
    """Dispatch with a trigger that answers at once; returns its inject_esc values."""
    esc = []

    def answer(inject_esc=False):
        esc.append(inject_esc)
        mine = next(f for f in ipc_dir.glob("autocad_lt_mcp_cmd_*.json") if f != skip)
        rid = json.loads(mine.read_text())["request_id"]
        (ipc_dir / f"autocad_lt_mcp_result_{rid}.json").write_text(
            json.dumps({"request_id": rid, "ok": True})
        )

    with patch.object(sys, "platform", "linux"), \
            patch.object(backend, "_type_dispatch_trigger", side_effect=answer):
        result = await backend._dispatch_unlocked("execute-lisp", {"code": "(+ 1 2)"})
    assert result.ok
    return esc


class TestTimeoutClaim:
    @pytest.mark.asyncio
    async def test_unclaimed_is_not_dispatched_and_retried_once(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        result, triggers = await _run(backend, "execute-lisp")
        assert result.error == "timeout_not_dispatched"
        assert result.payload["may_have_applied"] is False
        assert triggers == 2
        assert not list(tmp_path.glob("autocad_lt_mcp_cmd_*.json"))

    @pytest.mark.asyncio
    async def test_unclaimed_then_claimed_on_retry_stays_mutating(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        state = {"n": 0}

        def second_time_claims(d):
            state["n"] += 1
            if state["n"] == 2:
                _claim(d)

        result, triggers = await _run(backend, "execute-lisp", second_time_claims)
        assert result.error == "timeout_mutating"
        assert triggers == 2

    @pytest.mark.asyncio
    async def test_claimed_keeps_timeout_mutating(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        result, triggers = await _run(backend, "execute-lisp", _claim)
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert triggers == 1

    @pytest.mark.asyncio
    async def test_old_dispatcher_keeps_old_behaviour(self, tmp_path):
        backend = _backend(tmp_path, version=1)
        result, triggers = await _run(backend, "execute-lisp")
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert triggers == 1

    def test_run_files_count_as_stale(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        run = tmp_path / "autocad_lt_mcp_run_abc.json"
        run.write_text("{}")
        os.utime(run, (time.time() - 3600,) * 2)
        assert [p.name for p in backend._check_stale_ipc_files()] == ["autocad_lt_mcp_run_abc.json"]

    @pytest.mark.asyncio
    async def test_another_servers_in_flight_file_is_left_alone(self, tmp_path):
        """No ESC (it would clear grips) and no delete (it would time them out)."""
        backend = _backend(tmp_path, version=2)
        theirs = tmp_path / "autocad_lt_mcp_cmd_otherserver.json"
        theirs.write_text("{}")
        esc = await _dispatch_answered(backend, tmp_path, skip=theirs)
        assert esc == [False]
        assert theirs.exists()

    @pytest.mark.asyncio
    async def test_aged_file_still_triggers_esc_recovery(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        old = tmp_path / "autocad_lt_mcp_run_timedout.json"
        old.write_text("{}")
        os.utime(old, (time.time() - 3600,) * 2)
        esc = await _dispatch_answered(backend, tmp_path)
        assert esc == [True]
        assert not old.exists()

    def test_withdraw_reports_whether_file_existed(self, tmp_path):
        from autocad_lt_mcp.backends.file_ipc import FileIPCBackend

        f = tmp_path / "autocad_lt_mcp_cmd_x.json"
        f.write_text("{}")
        assert FileIPCBackend._withdraw(f) is True
        assert FileIPCBackend._withdraw(f) is False


class TestDispatcherLisp:
    text = DISPATCH_LSP.read_text(encoding="utf-8")

    def test_claim_rename_precedes_dispatch(self):
        body = strip_lisp(defun_body(self.text, "c:mcp-dispatch"))
        rename = body.index("vl-file-rename")
        assert rename < body.index("mcp-read-file-lines")
        assert rename < body.index("'mcp-dispatch-command")

    def test_run_file_name_slices_after_cmd_prefix(self):
        # substr is 1-based: "autocad_lt_mcp_cmd_" is 19 chars, so the id starts at 20.
        assert len("autocad_lt_mcp_cmd_") == 19
        assert '"autocad_lt_mcp_run_" (substr (car cmd-files) 20)' in self.text

    def test_ping_reports_dispatcher_version(self):
        assert re.search(r"\(setq \*mcp-dispatcher-version\* 2\)", self.text)
        assert '\\"dispatcher\\":" (itoa *mcp-dispatcher-version*)' in self.text

    def test_saveas_from_template_names_a_dwg_format(self):
        body = defun_body(self.text, "mcp-dispatch-command")
        call = body[body.index('(list "_.SAVEAS"'):]
        call = call[:call.index('"_Y"')]
        assert '(wcmatch (strcase (getvar "DWGNAME")) "*.DWT") "2018" ""' in call
        assert call.index("DWGNAME") < call.index("path")

    def test_plot_pdf_reports_dbmod(self):
        body = defun_body(self.text, "mcp-cmd-drawing-plot-pdf")
        assert body.count('(getvar "DBMOD")') >= 2
        for key in ("dbmod_before", "dbmod_after", "dbmod_restored", "save after plotting"):
            assert key in body

    def test_plot_pdf_push_pop_brackets_the_entmods(self):
        body = strip_lisp(defun_body(self.text, "mcp-cmd-drawing-plot-pdf"))
        assert body.index("(acad-push-dbmod)") < body.index("(entmod")
        assert body.rindex("(entmod") < body.index("(acad-pop-dbmod)")


class TestPingVersion:
    @pytest.mark.parametrize("payload,version", [
        ("pong", 1),
        ({"pong": True, "dispatcher": 2}, 2),
    ])
    @pytest.mark.asyncio
    async def test_initialize_reads_version(self, tmp_path, payload, version):
        from autocad_lt_mcp.backends.base import CommandResult

        # tmp_path, not the default C:/temp: execute_lisp writes a .lsp there,
        # which only works on a machine where C:/temp already exists.
        backend = _backend(tmp_path, version=None)
        replies = [CommandResult(ok=True, payload=payload), CommandResult(ok=True, payload="ACADLT")]
        with patch("autocad_lt_mcp.backends.file_ipc.find_autocad_window", return_value=1), \
                patch.object(backend, "_find_command_line_hwnd", return_value=None), \
                patch.object(backend, "_cleanup_stale_files"), \
                patch.object(backend, "_dispatch", side_effect=replies):
            result = await backend.initialize()
        assert result.ok
        assert backend._dispatcher_version == version


class TestSaveVerification:
    """drawing(save, path) wrote x.dwg from a .dwt and still came back
    save_unverified: AutoCAD stamped the mtime 12:14:20.000000, inside the
    second the call began, and the check compared it to time.time()."""

    @staticmethod
    def _save(backend, path, write):
        from autocad_lt_mcp.backends.base import CommandResult

        async def dispatch(cmd, data):
            write()
            return CommandResult(ok=True, payload={"path": path})

        return patch.object(backend, "_dispatch", side_effect=dispatch)

    @pytest.mark.asyncio
    async def test_new_file_stamped_to_the_whole_second_is_verified(self, tmp_path):
        import os
        import time

        backend = _backend(tmp_path, version=2)
        out = tmp_path / "x.dwg"
        whole = int(time.time())

        def write():
            out.write_bytes(b"dwg")
            os.utime(out, (whole, whole))

        with self._save(backend, str(out), write):
            result = await backend.drawing_save(str(out))
        assert result.ok, result.error

    @pytest.mark.asyncio
    async def test_rewrite_with_new_size_is_verified(self, tmp_path):
        import os

        backend = _backend(tmp_path, version=2)
        out = tmp_path / "x.dwg"
        out.write_bytes(b"old")
        stamp = out.stat().st_mtime

        def write():
            out.write_bytes(b"longer")
            os.utime(out, (stamp, stamp))

        with self._save(backend, str(out), write):
            result = await backend.drawing_save(str(out))
        assert result.ok, result.error

    @pytest.mark.asyncio
    async def test_untouched_existing_file_is_unverified(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        out = tmp_path / "x.dwg"
        out.write_bytes(b"old")
        with self._save(backend, str(out), lambda: None):
            result = await backend.drawing_save(str(out))
        assert result.error == "save_unverified"
        assert result.payload["detail"] == "timestamp unchanged"

    @pytest.mark.asyncio
    async def test_missing_file_is_unverified(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        out = tmp_path / "x.pdf"
        with self._save(backend, str(out), lambda: None):
            result = await backend.drawing_plot_pdf(str(out))
        assert result.error == "save_unverified"
        assert result.payload["detail"] == "file not found"


class TestPendingPrompt:
    """Every IPC timeout carries the prompt AutoCAD is waiting on (R1 idea 1),
    read through the UIA command-line reader, which works while the dispatcher
    is blocked. Mocked here: the UIA read itself needs a live AutoCAD."""

    PROMPT = "LINE Specify next point or [Undo]:"

    def _reader(self, ok=True):
        from autocad_lt_mcp.backends.base import CommandResult

        r = (CommandResult(ok=True, payload={"prompt": self.PROMPT}) if ok
             else CommandResult(ok=False, error="command_line needs Windows UI Automation"))
        return patch("autocad_lt_mcp.command_line.read_command_line", AsyncMock(return_value=r))

    @pytest.mark.asyncio
    async def test_timeout_mutating_keeps_its_fields_and_adds_the_prompt(self, tmp_path):
        backend = _backend(tmp_path, version=1)
        with self._reader() as m:
            result, _ = await _run(backend, "execute-lisp")
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert result.payload["pending_prompt"] == self.PROMPT
        m.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plain_timeout_gets_a_payload(self, tmp_path):
        backend = _backend(tmp_path, version=1)
        with self._reader():
            result, _ = await _run(backend, "drawing-get-variables")
        assert result.error.startswith("Timeout waiting for result")
        assert result.payload == {"pending_prompt": self.PROMPT}

    @pytest.mark.asyncio
    async def test_not_dispatched_reads_once_after_the_retry(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        with self._reader() as m:
            result, triggers = await _run(backend, "execute-lisp")
        assert result.error == "timeout_not_dispatched"
        assert result.payload["pending_prompt"] == self.PROMPT
        assert triggers == 2
        m.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_read_is_reported_not_raised(self, tmp_path):
        backend = _backend(tmp_path, version=1)
        with self._reader(ok=False):
            result, _ = await _run(backend, "execute-lisp")
        assert result.error == "timeout_mutating"
        assert "pending_prompt" not in result.payload
        assert "Windows UI Automation" in result.payload["pending_prompt_error"]

    @pytest.mark.asyncio
    async def test_non_timeout_errors_skip_the_read(self, tmp_path):
        from autocad_lt_mcp.backends.base import CommandResult

        backend = _backend(tmp_path, version=2)
        busy = CommandResult(ok=False, error="autocad_busy")
        with self._reader() as m, \
                patch.object(backend, "_dispatch_once", AsyncMock(return_value=busy)):
            result = await backend._dispatch_unlocked("execute-lisp", {})
        assert result is busy
        m.assert_not_awaited()
