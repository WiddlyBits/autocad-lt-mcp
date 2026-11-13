"""AutoCAD closing mid-session must read as an error, never as an empty drawing.

2026-11-04: with AutoCAD closed, system(init) returned ok:true, backend ezdxf,
entity_count 0. A server that has never seen AutoCAD still reaches ezdxf.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from autocad_lt_mcp import client, config, doc_switch, server
from autocad_lt_mcp.backends.base import CommandResult
from autocad_lt_mcp.backends.file_ipc import FileIPCBackend


@pytest.fixture
def world(monkeypatch):
    """A Windows box whose AutoCAD window is whatever state['hwnd'] says."""
    state = {"hwnd": None}

    async def initialize(self):
        self._hwnd = state["hwnd"]
        return CommandResult(ok=True)

    monkeypatch.setattr(config, "WIN32_AVAILABLE", True)
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
    monkeypatch.setattr(client, "_backend", None)
    monkeypatch.setattr(client, "_autocad_seen", False)
    monkeypatch.setattr(FileIPCBackend, "initialize", initialize)
    monkeypatch.setattr(FileIPCBackend, "_hwnd_alive", lambda self: self._hwnd == state["hwnd"])
    monkeypatch.setattr(FileIPCBackend, "preflight", AsyncMock(return_value=CommandResult(ok=False)))
    monkeypatch.setattr(FileIPCBackend, "load_libraries", AsyncMock(return_value=CommandResult(ok=True, payload={})))
    with patch("autocad_lt_mcp.backends.file_ipc.find_autocad_window", side_effect=lambda: state["hwnd"]), \
            patch("autocad_lt_mcp.backends.file_ipc.multi_window_warning", return_value=None), \
            patch.object(doc_switch, "find_autocad_frame", return_value=None):
        yield state


async def _system(operation):
    return json.loads(await server.system(operation=operation))


class TestAutoCADGone:
    @pytest.mark.asyncio
    async def test_init_refuses_instead_of_landing_on_ezdxf(self, world):
        world["hwnd"] = 111
        assert (await _system("init"))["payload"]["backend"] == "file_ipc"
        world["hwnd"] = None

        out = await _system("init")

        assert out["ok"] is False
        assert "AutoCAD not running" in out["error"]
        assert "payload" not in out

    @pytest.mark.asyncio
    async def test_a_second_init_and_status_do_not_reach_ezdxf_either(self, world):
        """The failed init drops the cached backend, so the next call re-detects."""
        world["hwnd"] = 111
        await _system("init")
        world["hwnd"] = None
        await _system("init")

        for op in ("init", "status"):
            out = await _system(op)
            assert out["ok"] is False and "AutoCAD not running" in out["error"], op
        assert client._backend is None

    @pytest.mark.asyncio
    async def test_init_recovers_when_autocad_is_reopened(self, world):
        world["hwnd"] = 111
        await _system("init")
        world["hwnd"] = None
        await _system("init")
        world["hwnd"] = 222

        out = await _system("init")

        assert out["ok"] is True
        assert out["payload"]["backend"] == "file_ipc"
        assert out["payload"]["hwnd"] == 222


class TestNeverSawAutoCAD:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("op", ["init", "status"])
    async def test_fresh_server_still_reaches_ezdxf(self, world, op):
        out = await _system(op)
        assert out["ok"] is True
        assert out["payload"]["backend"] == "ezdxf"

    @pytest.mark.asyncio
    async def test_ezdxf_server_moves_to_autocad_when_it_opens(self, world):
        """The startup race: auto picked ezdxf with AutoCAD still loading."""
        await _system("init")
        world["hwnd"] = 111
        assert (await _system("init"))["payload"]["backend"] == "file_ipc"


class TestStatusOnADeadWindow:
    """2026-11-04: status said file_ipc, idle, stale:false on a closed AutoCAD."""

    @staticmethod
    def _backend(tmp_path, monkeypatch, alive):
        fake = SimpleNamespace(IsWindow=lambda hwnd: alive)
        monkeypatch.setitem(sys.modules, "win32gui", fake)
        backend = FileIPCBackend()
        backend._hwnd = 111
        backend._status_file = tmp_path / "mcp_status.txt"
        backend._status_file.write_text("idle", encoding="utf-8")
        backend._dispatcher_loaded = 9e9  # fresh: would read stale:false
        return backend

    @pytest.mark.asyncio
    async def test_dead_window_is_not_idle_or_fresh(self, tmp_path, monkeypatch):
        result = await self._backend(tmp_path, monkeypatch, alive=False).status()

        assert result.ok is False
        assert "AutoCAD not running" in result.error
        assert result.payload["hwnd_alive"] is False
        assert result.payload["mcp_status"] != "idle"
        assert result.payload["dispatcher"]["stale"] is None

    @pytest.mark.asyncio
    async def test_server_status_passes_the_refusal_through(self, tmp_path, monkeypatch):
        backend = self._backend(tmp_path, monkeypatch, alive=False)
        monkeypatch.setattr(client, "_backend", backend)
        out = await _system("status")
        assert out["ok"] is False
        assert out["payload"]["hwnd_alive"] is False
        assert "preflight" not in out["payload"]

    @pytest.mark.asyncio
    async def test_live_window_reads_as_before(self, tmp_path, monkeypatch):
        with patch("autocad_lt_mcp.backends.file_ipc.multi_window_warning", return_value=None):
            result = await self._backend(tmp_path, monkeypatch, alive=True).status()
        assert result.ok is True
        assert result.payload["hwnd_alive"] is True
        assert result.payload["mcp_status"] == "idle"
        assert result.payload["dispatcher"]["stale"] is False


class TestWindowTitleFilter:
    """PR 20 live check: a .dxf document was missed, giving a false start_tab_active."""

    @pytest.mark.parametrize("title,found", [
        ("Autodesk AutoCAD LT 2026 - [plan.dxf]", True),
        ("Autodesk AutoCAD LT 2026 - [ecsi.dwt]", True),
        ("Autodesk AutoCAD LT 2026 - [plan.dwg]", True),
        ("Autodesk AutoCAD LT 2026 - [Drawing1]", True),
        ("Autodesk AutoCAD LT 2026 - Start", False),
    ])
    def test_document_titles(self, monkeypatch, title, found):
        from autocad_lt_mcp.backends import file_ipc

        fake = SimpleNamespace(
            IsWindowVisible=lambda h: True,
            GetWindowText=lambda h: title,
            EnumWindows=lambda cb, acc: cb(1, acc),
        )
        monkeypatch.setattr(file_ipc.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "win32gui", fake)
        assert (file_ipc.list_autocad_windows() == [(1, title)]) is found
