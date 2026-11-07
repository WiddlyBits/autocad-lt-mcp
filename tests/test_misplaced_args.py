"""Calls shaped the way Antigravity CLI's model sent them (port pass, 2026-11-07).

That client calls MCP tools through a generic call_mcp_tool without seeing their
schema, so it guessed argument placement. Each case below came back either as a
silent success or as a bare KeyError ("'cx'") with an "AutoCAD is responsive" hint.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOCAD_MCP_BACKEND", "ezdxf") != "ezdxf", reason="needs the ezdxf backend"
)


async def _call(tool, args):
    from autocad_mcp.server import mcp

    result = await mcp.call_tool(tool, args)
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


async def test_data_key_at_top_level_names_the_missing_argument():
    r = await _call("entity", {"operation": "create_circle", "cx": 10, "cy": 10, "radius": 5})
    assert r["ok"] is False
    assert r["error"] == "[entity.create_circle] missing argument 'cx'"
    assert "inside data" in r["hint"] and "AutoCAD" not in r["hint"]


async def test_polyline_without_points_is_refused_not_created_empty():
    await _call("drawing", {"operation": "create"})
    r = await _call("entity", {"operation": "create_polyline", "data": {"closed": True}})
    assert r["ok"] is False and "points" in r["error"]
    assert (await _call("entity", {"operation": "count"}))["payload"]["count"] == 0


async def test_layer_and_points_inside_data_are_used():
    await _call("drawing", {"operation": "create"})
    await _call("layer", {"operation": "create", "data": {"name": "P1"}})
    r = await _call("entity", {"operation": "create_polyline", "data": {"points": [[0, 0], [10, 0]], "layer": "P1"}})
    assert r["ok"] is True
    got = await _call("entity", {"operation": "get", "entity_id": r["payload"]["handle"]})
    assert got["payload"]["layer"] == "P1"


async def test_entity_id_inside_data_is_named_not_reported_as_entity_none():
    r = await _call("entity", {"operation": "get", "data": {"handle": "30"}})
    assert r["ok"] is False
    assert "None" not in r["error"] and "entity_id is required" in r["error"]
