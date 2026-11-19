"""system(operation="apply_batch"): a whole text batch in one MCP call.

Why: every model turn re-reads the whole chat, so a batch done as eight
execute_lisp calls costs eight context re-reads. Here Claude writes one spec
and the server does guard -> match -> edit -> read-back -> save in one call.

The matcher is pure Python over a snapshot of (handle, type, layer, text, x, y)
records, so the ezdxf tests exercise the same code the AutoCAD path runs.

Spec (JSON, passed inline as data.spec):
  dwg    str, required. Drawing name or its suffix, matched by mcp:guard.
  rules  list. Each: layer? str, exactly one of text (exact) / range [lo, hi]
         (whole-integer text only), expect int (required), and exactly one of
         values [str] (assigned top-to-bottom by Y, X breaks ties; length must
         equal expect) / add int (range rules only: 400 -> 400 + add).
  mtext  list. Each: layer str, height num, x num, y num, text str.
  save   bool, default false.

Model space only: shortcut, add a tab key when a batch needs paper space.
"""

from __future__ import annotations

import os
import re

from autocad_lt_mcp.backends.base import CommandResult

_INT = re.compile(r"-?\d+")


def parse_spec(spec) -> dict:
    """Validate the spec. Raises ValueError naming the first problem."""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    unknown = set(spec) - {"dwg", "rules", "mtext", "save"}
    if unknown:
        raise ValueError(f"unknown spec keys: {sorted(unknown)}")
    if not isinstance(spec.get("dwg"), str) or not spec["dwg"].strip():
        raise ValueError("spec.dwg is required: the drawing the batch is for")
    rules, adds = spec.get("rules", []), spec.get("mtext", [])
    if not isinstance(rules, list) or not isinstance(adds, list):
        raise ValueError("spec.rules and spec.mtext must be lists")
    if not rules and not adds:
        raise ValueError("spec has no rules and no mtext: nothing to do")
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            raise ValueError(f"{where} must be an object")
        bad = set(r) - {"layer", "text", "range", "expect", "values", "add"}
        if bad:
            raise ValueError(f"{where}: unknown keys {sorted(bad)}")
        if ("text" in r) == ("range" in r):
            raise ValueError(f"{where}: give exactly one of text or range")
        if "range" in r:
            rg = r["range"]
            if not (isinstance(rg, list) and len(rg) == 2 and all(type(v) is int for v in rg)
                    and rg[0] <= rg[1]):
                raise ValueError(f"{where}.range must be [lo, hi] integers, lo <= hi")
        elif not isinstance(r["text"], str):
            raise ValueError(f"{where}.text must be a string")
        if type(r.get("expect")) is not int or r["expect"] < 1:
            raise ValueError(f"{where}.expect is required: the count you expect to match")
        if ("values" in r) == ("add" in r):
            raise ValueError(f"{where}: give exactly one of values or add")
        if "values" in r:
            v = r["values"]
            if not (isinstance(v, list) and all(isinstance(s, str) for s in v)):
                raise ValueError(f"{where}.values must be a list of strings")
            if len(v) != r["expect"]:
                raise ValueError(f"{where}: {len(v)} values for expect {r['expect']}")
        else:
            if "range" not in r:
                raise ValueError(f"{where}: add needs a range rule (numeric text)")
            if type(r["add"]) is not int:
                raise ValueError(f"{where}.add must be an integer")
        if "layer" in r and not isinstance(r["layer"], str):
            raise ValueError(f"{where}.layer must be a string")
    for i, a in enumerate(adds):
        where = f"mtext[{i}]"
        if not isinstance(a, dict) or set(a) != {"layer", "height", "x", "y", "text"}:
            raise ValueError(f"{where} needs exactly layer, height, x, y, text")
        if not (isinstance(a["layer"], str) and isinstance(a["text"], str)):
            raise ValueError(f"{where}: layer and text must be strings")
        for k in ("height", "x", "y"):
            if not isinstance(a[k], (int, float)) or isinstance(a[k], bool):
                raise ValueError(f"{where}.{k} must be a number")
        if a["height"] <= 0:
            raise ValueError(f"{where}.height must be positive")
    # shortcut: one DXF group 1 holds 250 chars; longer text needs group 3 chunks, add when a batch needs it.
    longest = max([len(s) for r in rules for s in r.get("values", [])] + [len(a["text"]) for a in adds] + [0])
    if longest > 250:
        raise ValueError("a value or mtext text is over 250 characters")
    if not isinstance(spec.get("save", False), bool):
        raise ValueError("spec.save must be true or false")
    return {"dwg": spec["dwg"], "rules": rules, "mtext": adds, "save": spec.get("save", False)}


def _matches(rule: dict, ent: dict) -> bool:
    if "layer" in rule and ent["layer"].upper() != rule["layer"].upper():
        return False
    if "text" in rule:
        return ent["text"] == rule["text"]
    s = ent["text"].strip()
    return bool(_INT.fullmatch(s)) and rule["range"][0] <= int(s) <= rule["range"][1]


def resolve(rules: list[dict], ents: list[dict]) -> tuple[list[dict], list[dict], list[int]]:
    """Plan every edit before any is made.

    Returns (edits, mismatches, counts). edits: {"h", "old", "new"}.
    A rule whose count is not its expect, or an entity two rules both claim,
    is a mismatch, and any mismatch means the caller makes no edits at all.
    """
    edits, mismatches, counts, claimed = [], [], [], {}
    for i, rule in enumerate(rules):
        hits = sorted((e for e in ents if _matches(rule, e)), key=lambda e: (-e["y"], e["x"]))
        counts.append(len(hits))
        if len(hits) != rule["expect"]:
            mismatches.append({"rule": i, "expected": rule["expect"], "found": len(hits)})
            continue
        for n, e in enumerate(hits):
            if e["h"] in claimed:
                mismatches.append({"rule": i, "overlap_with_rule": claimed[e["h"]], "h": e["h"]})
                continue
            claimed[e["h"]] = i
            new = rule["values"][n] if "values" in rule else str(int(e["text"].strip()) + rule["add"])
            edits.append({"h": e["h"], "old": e["text"], "new": new})
    return edits, mismatches, counts


def _fail(error: str, **payload) -> CommandResult:
    return CommandResult(ok=False, error=error, payload=payload or None)


def _file_state(path: str | None):
    try:
        st = os.stat(path)
        return [st.st_size, int(st.st_mtime)]
    except (OSError, TypeError):
        return None


async def run(backend, spec, dry_run: bool) -> CommandResult:
    """parse -> snapshot -> resolve -> (dry run | apply -> save -> DBMOD check)."""
    try:
        s = parse_spec(spec)
    except ValueError as e:
        return _fail("BAD-SPEC", detail=str(e))

    snap = await backend.batch_snapshot(s["dwg"])
    if not snap.ok:
        return snap
    snap = snap.payload
    # DBMOD is bit-coded: anything but 0 is someone's unsaved work the batch's save would take with it.
    if snap.get("dbmod"):
        return _fail("DIRTY-START", dbmod=snap["dbmod"], dwg=snap.get("dwg"),
                     detail="the drawing has unsaved changes; save or discard them first")

    edits, mismatches, counts = resolve(s["rules"], snap["ents"])
    if mismatches:
        return _fail("COUNT-MISMATCH", mismatches=mismatches, counts=counts,
                     detail="no edits were made")
    if dry_run:
        return CommandResult(ok=True, payload={
            "dry_run": True, "dwg": snap.get("dwg"), "counts": counts,
            "mtext": len(s["mtext"]), "changes": edits, "save": s["save"]})

    res = await backend.batch_apply(s["dwg"], edits, s["mtext"])
    if not res.ok:
        return res
    applied = res.payload
    readback_bad = [e["h"] for e, rb in zip(edits, applied["readback"]) if rb != e["new"]]
    if len(applied["readback"]) != len(edits) or readback_bad or len(applied["added"]) != len(s["mtext"]):
        # The apply layer compares too and rolls back; reaching here means the two disagree.
        return _fail("READBACK-MISMATCH", handles=readback_bad, detail="edits were applied; check them")
    out = {"dwg": snap.get("dwg"), "counts": counts, "mtext": len(applied["added"]),
           "added": applied["added"], "readback": f"{len(edits)}/{len(edits)} match"}
    if not s["save"]:
        out["save"] = "not requested"
        return CommandResult(ok=True, payload=out)

    before = _file_state(snap.get("path"))
    saved = await backend.drawing_save(None)
    after_vars = await backend.drawing_get_variables(["DBMOD"])
    dbmod_after = after_vars.payload.get("DBMOD") if after_vars.ok and isinstance(after_vars.payload, dict) else None
    proof = {"saved": saved.ok, "dbmod_before_save": applied.get("dbmod"),
             "dbmod_after_save": dbmod_after,
             # Supporting evidence only: size and mtime can hold still or move for other reasons.
             "file_before": before, "file_after": _file_state(snap.get("path"))}
    out["save"] = proof
    if not saved.ok:
        return _fail("SAVE-FAILED", **out, detail=f"{saved.error}; edits are in the drawing, unsaved")
    if applied.get("dbmod") is not None and not (applied["dbmod"] and dbmod_after in (0, "0")):
        return _fail("SAVE-UNPROVEN", **out, detail="DBMOD did not go from nonzero to 0")
    return CommandResult(ok=True, payload=out)
