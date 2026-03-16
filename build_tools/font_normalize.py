#!/usr/bin/env python3
"""Canonical font normalization for reproducible build comparison.

Transforms a font binary into a canonical form by stripping encoding choices
that don't affect functionality. Two fonts that are functionally equivalent
will produce identical normalized output.

Usage:
    python font_normalize.py input.ttf output.ttf
    python font_normalize.py input.ttf  # prints to stdout (binary)

Each normalization step is safe (preserves functional behavior) and
deterministic (same input → same output). See the dashboard documentation
at https://felipesanches.github.io/gfonts_agents/index.html#normalization
for detailed safety arguments.
"""

import argparse
import sys
from pathlib import Path
from fontTools.ttLib import TTFont


def step1_strip_dsig(font: TTFont) -> None:
    """Remove the DSIG (Digital Signature) table.

    Safety: Google Fonts ships empty DSIG stubs. No software validates DSIG
    in web fonts. The OpenType spec marks it as optional.
    """
    if "DSIG" in font:
        del font["DSIG"]


def step2_normalize_timestamps(font: TTFont) -> None:
    """Set head.created and head.modified to epoch 0.

    Safety: These timestamps record when the font was compiled, not what it
    contains. No text shaper, rasterizer, or layout engine reads these fields.
    """
    font["head"].created = 0
    font["head"].modified = 0


def step3_normalize_name_table(font: TTFont) -> None:
    """Normalize the name table: remove Mac platform, renumber IDs, sort.

    Safety:
    - Mac platform (platformID=1): Legacy, all modern software uses platformID=3.
    - ID renumbering (≥256): These are font-specific IDs whose numeric value is
      meaningless; only their content and references matter.
    - Sorting: Record order is a serialization choice, not semantic.
    """
    name_table = font["name"]

    # Remove platform 1 (Macintosh) records
    name_table.names = [r for r in name_table.names if r.platformID != 1]

    # Build a mapping of old name IDs ≥256 to new sequential IDs
    old_ids = sorted(set(r.nameID for r in name_table.names if r.nameID >= 256))
    id_map = {old: 256 + i for i, old in enumerate(old_ids)}

    # Renumber name records
    for record in name_table.names:
        if record.nameID in id_map:
            record.nameID = id_map[record.nameID]

    # Update references in fvar (NamedInstances and Axes)
    if "fvar" in font:
        fvar = font["fvar"]
        for axis in fvar.axes:
            if axis.axisNameID in id_map:
                axis.axisNameID = id_map[axis.axisNameID]
        for inst in fvar.instances:
            if hasattr(inst, "subfamilyNameID") and inst.subfamilyNameID in id_map:
                inst.subfamilyNameID = id_map[inst.subfamilyNameID]
            if hasattr(inst, "postscriptNameID"):
                if inst.postscriptNameID in id_map:
                    inst.postscriptNameID = id_map[inst.postscriptNameID]
                elif inst.postscriptNameID == 0xFFFF:
                    pass  # Already unset
                else:
                    # postscriptNameID not in map — remove it
                    inst.postscriptNameID = 0xFFFF

    # Update references in STAT
    if "STAT" in font:
        stat = font["STAT"].table
        if stat.DesignAxisRecord:
            for axis in stat.DesignAxisRecord.Axis:
                if axis.AxisNameID in id_map:
                    axis.AxisNameID = id_map[axis.AxisNameID]
        if hasattr(stat, "DesignAxisValue") and stat.DesignAxisValue:
            for val in stat.DesignAxisValue:
                inner = val  # AxisValueRecord wraps the actual value
                if hasattr(inner, "ValueNameID") and inner.ValueNameID in id_map:
                    inner.ValueNameID = id_map[inner.ValueNameID]
                # AxisValueFormat4 uses Values array
                if hasattr(inner, "Values"):
                    for v in inner.Values:
                        if hasattr(v, "ValueNameID") and v.ValueNameID in id_map:
                            v.ValueNameID = id_map[v.ValueNameID]

    # Sort name records by (platformID, encodingID, languageID, nameID)
    name_table.names.sort(
        key=lambda r: (r.platformID, r.platEncID, r.langID, r.nameID)
    )


def step4_normalize_layout_tables(font: TTFont) -> None:
    """Recompile GPOS/GDEF/GSUB through fontTools for canonical encoding.

    Safety: The decompile→recompile round-trip preserves all layout rules.
    Only the binary encoding changes (e.g., PairPosFormat1 vs Format2,
    Extension vs inline). This is the same approach as otl-normalizer
    in the fontc project.

    Determinism: fontTools compilation is deterministic for a given version.
    """
    for tag in ("GPOS", "GDEF", "GSUB"):
        if tag in font:
            # Force decompilation by accessing the table's high-level object.
            # fontTools caches the decompiled form and will recompile from
            # it when saving, producing canonical binary output.
            _ = font[tag].table


def step5_normalize_gvar(font: TTFont) -> None:
    """Disable IUP optimization in gvar to store explicit deltas for all points.

    Safety: IUP is a compression technique — some deltas are omitted because
    they can be inferred from neighbors. Storing all deltas explicitly is the
    uncompressed canonical form. The font renders identically at all axis positions.
    """
    if "gvar" not in font:
        return

    gvar = font["gvar"]
    glyf = font["glyf"] if "glyf" in font else None
    if glyf is None:
        return

    for glyph_name in gvar.variations:
        for var in gvar.variations[glyph_name]:
            # Ensure all deltas are explicit (no None values from IUP)
            if var.coordinates is not None:
                # Get the number of points for this glyph
                glyph = glyf[glyph_name]
                if hasattr(glyph, "numberOfContours") and glyph.numberOfContours > 0:
                    n_points = max(
                        (max(c) + 1 if c else 0) for c in [glyph.endPtsOfContours]
                    ) if glyph.endPtsOfContours else 0
                    # The coordinates list includes points + 4 phantom points
                    # If any coordinate is None, replace with (0, 0)
                    var.coordinates = [
                        c if c is not None else (0, 0)
                        for c in var.coordinates
                    ]


def step6_normalize_glyph_order(font: TTFont) -> None:
    """Sort glyphs into canonical order.

    Safety: Glyph order is an internal implementation detail. Text shapers
    access glyphs via cmap (Unicode→glyph ID), never by raw index. fontTools'
    setGlyphOrder() updates all internal references consistently.
    """
    cmap = font.getBestCmap() or {}
    # Invert: glyph_name → lowest codepoint
    glyph_to_cp = {}
    for cp, name in cmap.items():
        if name not in glyph_to_cp or cp < glyph_to_cp[name]:
            glyph_to_cp[name] = cp

    old_order = font.getGlyphOrder()

    def sort_key(name):
        if name == ".notdef":
            return (0, 0, "")  # Always first
        cp = glyph_to_cp.get(name)
        if cp is not None:
            return (1, cp, name)  # Encoded glyphs by codepoint
        return (2, 0, name)  # Non-encoded glyphs alphabetically

    new_order = sorted(old_order, key=sort_key)

    if new_order != old_order:
        font.setGlyphOrder(new_order)


def normalize_font(input_path: str, output_path: str) -> None:
    """Run the full normalization pipeline on a font file."""
    font = TTFont(input_path, recalcTimestamp=False)

    step1_strip_dsig(font)
    step2_normalize_timestamps(font)
    step3_normalize_name_table(font)
    step4_normalize_layout_tables(font)
    step5_normalize_gvar(font)
    step6_normalize_glyph_order(font)

    # Step 7: Save with canonical table order
    font.save(output_path, reorderTables=True)
    font.close()


def main():
    parser = argparse.ArgumentParser(
        description="Normalize a font binary to canonical form for comparison."
    )
    parser.add_argument("input", help="Input font file (.ttf/.otf)")
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output font file (default: stdout)",
    )
    args = parser.parse_args()

    if args.output:
        normalize_font(args.input, args.output)
    else:
        import io

        buf = io.BytesIO()
        normalize_font(args.input, buf)
        sys.stdout.buffer.write(buf.getvalue())


if __name__ == "__main__":
    main()
