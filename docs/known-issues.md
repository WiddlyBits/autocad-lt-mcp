# Known issues

Open defects and LT limits on `main`, each with its evidence. Resolved items are deleted, not
struck through — git history keeps them. Any claim about LT capability carries a date or says
UNTESTED.

## Open

**The crossing grid treats a solid HATCH as a filled region; AutoCAD does not.** Measured
2026-10-29: `ssget "_C"` hits a solid hatch along its boundary and along its fill's internal
triangulation edges, and misses windows inside it that touch neither. No group code describes
that triangulation, so the Python grid marks the whole interior and the live grid marks a
diagonal pattern. Snapshots of a drawing with a hatch differ in those rows only.

**Live snapshots are not byte-identical to an ezdxf-generated golden.** Two causes, neither
in the bbox code (2026-10-29, `bbox_types.dxf`, every `layer` line identical): AutoCAD writes
`$EXTMIN`/`$EXTMAX` on open, so `extents-header` is set live and `none` from ezdxf; and it
creates the layout's ID 1 viewport on open, which adds an unmeasured entity on layer `0`.

**`expected_doc` is backend-only.** No tool sets the `expected_doc` argument of
`_dispatch`/`execute_lisp`; only backend code can. A tool caller gets the same check by calling
`system(assert_doc)` first, at the cost of one extra round trip.

**`redo` is refused through dispatch, cause unmeasured.** The refusal points at the command line.
Measured 2026-10-28: `U`, then a dispatched `entity(count)`, then `REDO` at the command line
answered "Everything has been redone", so a read does not clear the redo stack. Still unmeasured:
a mutating dispatch in between, and `REDO` sent through a dispatch.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server-side `vlax-` guard was removed.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `(mcp:whoami)` adds `DWGPREFIX`.
- LT registers no COM class: `GetActiveObject("AutoCAD.Application")` raises
  `-2147221005 Invalid class string` (2026-10-28). `_is_autocad_busy` treats that as not busy,
  so `autocad_busy` never fires on LT; `command_active` covers the busy-prompt case.
- Trigger keystrokes posted while a modal dialog is open are queued, not dropped, and run
  when it closes (2026-10-28).
