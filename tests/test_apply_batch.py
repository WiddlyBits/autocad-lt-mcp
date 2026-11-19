"""system(operation="apply_batch") on the ezdxf backend: spec parsing, matching,
ordering, the all-or-nothing refusals and the 16/16/1 batch from the cost test.

What this cannot prove: the LISP half (undo group, rollback, QSAVE, DBMOD) runs
only in AutoCAD, so the live arm C run is its integration check.
"""

import random

import ezdxf
import pytest

from autocad_lt_mcp import batch
from autocad_lt_mcp.backends.base import CommandResult
from autocad_lt_mcp.backends.ezdxf_backend import EzdxfBackend
from autocad_lt_mcp.backends.file_ipc import FileIPCBackend

NAMES = ["PUMP 1 RUN", "PUMP 1 FAULT RESET", "PUMP 2 RUN", "PUMP 2 FAULT RESET",
         "PUMP 3 RUN", "PUMP 3 FAULT RESET", "MIXER RUN", "CHLORINE FEED ENABLE",
         "GENERATOR START", "HORN", "BEACON", "VALVE 1 OPEN", "VALVE 1 CLOSE",
         "VALVE 2 OPEN", "VALVE 2 CLOSE", "SPARE"]
MTEXT = {"layer": "LABEL", "height": 0.12, "x": -9.5, "y": 31.2,
         "text": "DIGITAL OUTPUT MODULE 1 - SLOT 3"}


def do_spec(**over):
    spec = {"dwg": "Operator Agent Test",
            "rules": [{"layer": "LABEL", "text": "DESC", "expect": 16, "values": NAMES},
                      {"layer": "WIRENO", "range": [400, 415], "expect": 16, "add": 100}],
            "mtext": [MTEXT], "save": False}
    spec.update(over)
    return spec


@pytest.fixture
async def do_drawing():
    """A DO sheet in miniature: row i (0 = top) has DESC and wire 400+i.
    Inserted in shuffled order, so only the Y sort can put them right."""
    b = EzdxfBackend()
    await b.initialize()
    rows = list(range(16))
    random.Random(7).shuffle(rows)
    for i in rows:
        b._msp.add_text("DESC", dxfattribs={"layer": "LABEL", "insert": (0, 30 - i)})
        b._msp.add_text(str(400 + i), dxfattribs={"layer": "WIRENO", "insert": (-5, 30 - i)})
    # Distractors: right text, wrong layer; right text, paper space; just outside the range.
    b._msp.add_text("DESC", dxfattribs={"layer": "OTHER", "insert": (9, 9)})
    b._doc.layouts.get("Layout1").add_text("DESC", dxfattribs={"layer": "LABEL"})
    b._msp.add_text("399", dxfattribs={"layer": "WIRENO", "insert": (-5, 40)})
    b._msp.add_text("416", dxfattribs={"layer": "WIRENO", "insert": (-5, 10)})
    return b


def texts(b, layer):
    """Texts on a layer, top to bottom."""
    ents = sorted((e for e in b._msp.query("TEXT MTEXT") if e.dxf.layer == layer),
                  key=lambda e: -e.dxf.insert.y)
    return [b._text_of(e) for e in ents]


def all_texts(b):
    return sorted((e.dxf.handle, b._text_of(e)) for e in b._msp.query("TEXT MTEXT"))


class TestFullBatch:
    async def test_16_16_1_and_saved(self, do_drawing, tmp_path):
        b = do_drawing
        b._save_path = str(tmp_path / "do.dxf")
        r = await batch.run(b, do_spec(save=True), dry_run=False)
        assert r.ok, r.error
        assert r.payload["counts"] == [16, 16] and r.payload["mtext"] == 1
        assert r.payload["readback"] == "32/32 match"
        assert r.payload["save"]["saved"] is True
        # ezdxf has no DBMOD: reported as not measured, never faked as 0.
        assert r.payload["save"]["dbmod_before_save"] is None

        doc = ezdxf.readfile(b._save_path)
        msp = doc.modelspace()
        label = sorted((e for e in msp.query("TEXT") if e.dxf.layer == "LABEL"),
                       key=lambda e: -e.dxf.insert.y)
        assert [e.dxf.text for e in label] == NAMES
        wires = sorted((e for e in msp.query("TEXT") if e.dxf.layer == "WIRENO"),
                       key=lambda e: -e.dxf.insert.y)
        assert [e.dxf.text for e in wires] == ["399"] + [str(500 + i) for i in range(16)] + ["416"]
        [m] = msp.query("MTEXT")
        assert m.text == MTEXT["text"] and m.dxf.layer == "LABEL"
        assert m.dxf.char_height == pytest.approx(0.12)
        assert (m.dxf.insert.x, m.dxf.insert.y) == pytest.approx((-9.5, 31.2))

    async def test_distractors_untouched(self, do_drawing):
        b = do_drawing
        assert (await batch.run(b, do_spec(), dry_run=False)).ok
        assert texts(b, "OTHER") == ["DESC"]
        assert [e.dxf.text for e in b._doc.layouts.get("Layout1").query("TEXT")] == ["DESC"]


class TestDryRun:
    async def test_changes_nothing_and_lists_old_new(self, do_drawing):
        b = do_drawing
        before = all_texts(b)
        r = await batch.run(b, do_spec(), dry_run=True)
        assert r.ok and r.payload["dry_run"] is True
        assert all_texts(b) == before
        changes = r.payload["changes"]
        assert len(changes) == 32
        assert changes[0]["old"] == "DESC" and changes[0]["new"] == "PUMP 1 RUN"
        assert changes[15]["new"] == "SPARE"
        assert (changes[16]["old"], changes[16]["new"]) == ("400", "500")
        assert (changes[31]["old"], changes[31]["new"]) == ("415", "515")


class TestAllOrNothing:
    async def test_count_mismatch_makes_no_edits(self, do_drawing):
        b = do_drawing
        before = all_texts(b)
        spec = do_spec()
        spec["rules"][0] = {"layer": "LABEL", "text": "DESC", "expect": 15, "values": NAMES[:15]}
        r = await batch.run(b, spec, dry_run=False)
        assert not r.ok and r.error == "COUNT-MISMATCH"
        assert r.payload["mismatches"] == [{"rule": 0, "expected": 15, "found": 16}]
        # The other rule matched fine and must not have run either; no MTEXT added.
        assert all_texts(b) == before

    async def test_overlapping_rules_refused(self, do_drawing):
        b = do_drawing
        before = all_texts(b)
        spec = do_spec(rules=[{"layer": "WIRENO", "range": [400, 415], "expect": 16, "add": 100},
                              {"range": [400, 400], "expect": 1, "add": 1}], mtext=[])
        r = await batch.run(b, spec, dry_run=False)
        assert not r.ok and r.error == "COUNT-MISMATCH"
        assert r.payload["mismatches"][0]["overlap_with_rule"] == 0
        assert all_texts(b) == before

    async def test_stale_target_refused_before_any_edit(self, do_drawing):
        b = do_drawing
        snap = (await b.batch_snapshot("x")).payload
        edits, _, _ = batch.resolve(do_spec()["rules"], snap["ents"])
        edits[-1]["old"] = "something else"
        before = all_texts(b)
        r = await b.batch_apply("x", edits, [MTEXT])
        assert not r.ok and r.error == "STALE" and r.payload["handles"] == [edits[-1]["h"]]
        assert all_texts(b) == before


class _Fake:
    """A backend whose snapshot and save report what AutoCAD would."""

    def __init__(self, dbmod=0, dbmod_after=0):
        self.dbmod, self.dbmod_after, self.applied = dbmod, dbmod_after, False

    async def batch_snapshot(self, dwg):
        return CommandResult(ok=True, payload={"dwg": "x.dwg", "path": None, "dbmod": self.dbmod,
                                               "ents": [{"h": "A", "type": "TEXT", "layer": "L",
                                                         "text": "DESC", "x": 0, "y": 0}]})

    async def batch_apply(self, dwg, edits, mtext):
        self.applied = True
        return CommandResult(ok=True, payload={"readback": [e["new"] for e in edits],
                                               "added": [], "dbmod": 1})

    async def drawing_save(self, path=None):
        return CommandResult(ok=True, payload={"saved": True})

    async def drawing_get_variables(self, names=None):
        return CommandResult(ok=True, payload={"DBMOD": self.dbmod_after})


ONE = {"dwg": "x", "rules": [{"text": "DESC", "expect": 1, "values": ["NEW"]}], "save": True}


class TestDbmod:
    async def test_dirty_start_refused_before_apply(self):
        f = _Fake(dbmod=4)
        r = await batch.run(f, ONE, dry_run=False)
        assert not r.ok and r.error == "DIRTY-START" and r.payload["dbmod"] == 4
        assert f.applied is False

    async def test_save_proof_is_dbmod_nonzero_to_zero(self):
        r = await batch.run(_Fake(), ONE, dry_run=False)
        assert r.ok
        assert (r.payload["save"]["dbmod_before_save"], r.payload["save"]["dbmod_after_save"]) == (1, 0)

    async def test_dbmod_still_set_after_save_is_not_success(self):
        r = await batch.run(_Fake(dbmod_after=1), ONE, dry_run=False)
        assert not r.ok and r.error == "SAVE-UNPROVEN"


class TestSpecValidation:
    @pytest.mark.parametrize("spec, says", [
        ({"rules": [{"text": "A", "expect": 1, "values": ["B"]}]}, "dwg"),
        ({"dwg": "x"}, "nothing to do"),
        ({"dwg": "x", "rules": [{"text": "A", "range": [1, 2], "expect": 1, "add": 1}]}, "exactly one of text"),
        ({"dwg": "x", "rules": [{"text": "A", "values": ["B"]}]}, "expect"),
        ({"dwg": "x", "rules": [{"text": "A", "expect": 2, "values": ["B"]}]}, "1 values for expect 2"),
        ({"dwg": "x", "rules": [{"text": "A", "expect": 1, "add": 1}]}, "range"),
        ({"dwg": "x", "rules": [{"range": [5, 1], "expect": 1, "add": 1}]}, "lo <= hi"),
        ({"dwg": "x", "rules": [{"text": "A", "expect": 1, "values": ["B"], "order": "x"}]}, "unknown"),
        ({"dwg": "x", "mtext": [{"layer": "L", "height": 0, "x": 0, "y": 0, "text": "T"}]}, "positive"),
        ({"dwg": "x", "mtext": [{"layer": "L", "x": 0, "y": 0, "text": "T"}]}, "exactly"),
        ({"dwg": "x", "rules": [{"text": "A", "expect": 1, "values": ["B" * 251]}]}, "250"),
        ({"dwg": "x", "rules": [], "mtext": [MTEXT], "save": "yes"}, "save"),
    ])
    async def test_bad_spec_touches_nothing(self, do_drawing, spec, says):
        before = all_texts(do_drawing)
        r = await batch.run(do_drawing, spec, dry_run=False)
        assert not r.ok and r.error == "BAD-SPEC" and says in r.payload["detail"]
        assert all_texts(do_drawing) == before


class TestMatching:
    def test_ties_on_y_go_left_to_right(self):
        ents = [{"h": h, "layer": "L", "text": "D", "x": x, "y": 1} for h, x in (("B", 5), ("A", 1))]
        edits, _, _ = batch.resolve([{"text": "D", "expect": 2, "values": ["1st", "2nd"]}], ents)
        assert [(e["h"], e["new"]) for e in edits] == [("A", "1st"), ("B", "2nd")]

    def test_range_matches_whole_integers_only(self):
        ents = [{"h": str(i), "layer": "L", "text": t, "x": 0, "y": -i}
                for i, t in enumerate(["400", " 401 ", "402A", "W403", "4.5"])]
        edits, _, counts = batch.resolve([{"range": [0, 999], "expect": 2, "add": 1}], ents)
        assert counts == [2] and [e["new"] for e in edits] == ["401", "402"]

    def test_layer_match_ignores_case(self):
        ents = [{"h": "A", "layer": "label", "text": "D", "x": 0, "y": 0}]
        _, mism, _ = batch.resolve([{"layer": "LABEL", "text": "D", "expect": 1, "values": ["X"]}], ents)
        assert mism == []


class TestLispCall:
    async def test_apply_quotes_strings_for_autolisp(self, monkeypatch):
        sent = []

        async def fake_exec(self, code, expected_doc=None):
            sent.append(code)
            return CommandResult(ok=True, payload='{"ok":true,"readback":[],"added":[],"dbmod":1}')

        monkeypatch.setattr(FileIPCBackend, "execute_lisp", fake_exec)
        b = FileIPCBackend.__new__(FileIPCBackend)
        r = await b.batch_apply('a"b', [{"h": "1F", "old": 'say "hi"', "new": "C:\\x"}],
                                [{"layer": "L", "height": 0.12, "x": -9.5, "y": 31.2, "text": "T"}])
        assert r.ok
        assert sent == ['(mcp:batch-apply "a\\"b" \'(("1F" "say \\"hi\\"" "C:\\\\x")) '
                        '\'(("L" 0.1200000000 -9.5000000000 31.2000000000 "T")))']

    async def test_lisp_refusal_becomes_ok_false(self, monkeypatch):
        async def fake_exec(self, code, expected_doc=None):
            return CommandResult(ok=True, payload='{"ok":false,"error":"ROLLED-BACK","rolled_back":true}')

        monkeypatch.setattr(FileIPCBackend, "execute_lisp", fake_exec)
        b = FileIPCBackend.__new__(FileIPCBackend)
        r = await b.batch_apply("d", [], [])
        assert not r.ok and r.error == "ROLLED-BACK" and r.payload["rolled_back"] is True
