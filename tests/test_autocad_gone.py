"""AutoCAD closing mid-session must read as an error, never as an empty drawing.

2026-11-04: with AutoCAD closed, system(init) returned ok:true, backend ezdxf,
entity_count 0. A server that has never seen AutoCAD still reaches ezdxf.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from autocad_mcp import client, config, doc_switch, server
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


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
    monkeypatch.setattr(FileIPCBackend, "_hwnd_alive", lambda self: self._hwnd == state["hwnd"], raising=False)
    monkeypatch.setattr(FileIPCBackend, "preflight", AsyncMock(return_value=CommandResult(ok=False)))
    monkeypatch.setattr(FileIPCBackend, "load_libraries", AsyncMock(return_value=CommandResult(ok=True, payload={})))
    with patch("autocad_mcp.backends.file_ipc.find_autocad_window", side_effect=lambda: state["hwnd"]), \
            patch("autocad_mcp.backends.file_ipc.multi_window_warning", return_value=None), \
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
