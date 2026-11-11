# Screenshots and DXF probes

Read before any L4 or L5 capture, or before parsing a DXF export. The ladder itself, and when to
climb it, stays in Rule 3 of `SKILL.md`.

## L3b: `probe_dxf`

**L3b is the rung for "what is actually in this drawing".** Parsing happens on this machine,
so only the answer enters the conversation. Run from your clone of this repo under `uv run`:

```
python -m autocad_lt_mcp.probe_dxf out.dxf                 # the grid snapshot
python -m autocad_lt_mcp.probe_dxf out.dxf --text          # every string, with layer and point
python -m autocad_lt_mcp.probe_dxf out.dxf --spaces        # the layout names
python -m autocad_lt_mcp.probe_dxf out.dxf --text --space Layout1 --layer TITLE
                                    # title-block read-back: handle, height, MTEXT width + attach
```

`--text` reads model space unless `--space` names a layout; an unknown name lists the real ones.

## Screenshot parameters

**Screenshot parameters, in order of effect.** `region=[left,top,right,bottom]` crops
*before* the downscale, so a 500x400 detail crop is ~270 tokens and *sharper* than the
whole window — **always pass it for a detail check.** `save_to="….png"` writes to disk and
attaches nothing. `max_dimension` (default 1280, clamped 64–2576); halving it roughly
quarters the cost. Every capture reports `est_tokens`. Non-levers: `quality` (JPEG) shrinks
bytes, not tokens, and nothing above 2576 px survives the vision API's own downscale.

**Finding the crop without a full-window capture, in this order.** Coordinates known: use
`region_dwg`, always straight after `zoom_window` with the same box. Not known: use `grid=true`.
Still too coarse: use `grid=true` plus `region`, which redraws the rules on the crop. Measured
on one 44x9 band: `region_dwg` 460 tokens, `region="canvas"` 1,702. A string region's resolved
px come back in `region`.

- `region_dwg=[x1,y1,x2,y2]`: crop by drawing coordinates from `(mcp:sel-dump)` or
  `entity(get)`, in the current view. Overrides `region`. AutoCAD backend only; refused on
  twisted views, active layout viewports and tiled viewports, so use `grid=true` there.
- `grid=true`: a preview (longest side always 560; `max_dimension` is ignored) with red rules
  labelled in native window px.
  Read the labels and pass them back as `region` for the sharp crop.
- `region="changed"`: crops to the canvas pixels that changed since the previous capture of
  this window. Plan it before the edit: `save_to` capture as the 0-token baseline in the same
  view, edit, capture. It also shows nothing else moved, which `entity(get)` can't. With no
  baseline it errors and that capture becomes one; with no change it errors, which is itself the
  answer. For local edits: a pan or zoom changes every pixel with something drawn on it, which is
  nearly the whole canvas on a full drawing but only the UCS icon on an empty one.
- `region="canvas"`: only the drawing area, no ribbon, tabs, palettes or command line. Never use
  it to check a band or row; it pays for the empty canvas.

A minimized window is refused, because it captures black at full price. Restore it and retry.

Reserve a full-window capture for what only pixels answer — layout, overlap, match to a
reference photo — and batch it to a checkpoint after a group of edits, not after each one.

Reference photos climb the same ladder: crop to the area you need before a full-size read. Skip a photo whose name shows it doesn't cover the area,
and say you skipped it.

`view(zoom_window)` takes **drawing coordinates, not screen pixels**; pixel-like guesses
don't move the view and cost another screenshot to notice. Prefer `(mcp:sel-show)`, or
compute the window from entity coordinates.
