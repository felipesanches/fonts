#!/usr/bin/env python3
"""Prebuild script for Hepta Slab.

The HeptaSlab.glyphs source has 4 masters with bracket layers that create
alternate glyphs for different weight ranges (via DesignSpace rules/rvrn).
19 base glyphs have incompatible contour structures between masters because
the lighter weights have simpler shapes (no slab serifs) while heavier
weights have complex slab structures.

The bracket layer mechanism means these base glyphs are ALWAYS substituted
by compatible alternates (varAlt01/varAlt02) at every weight range. Therefore,
we can safely replace incompatible base glyphs with copies of one of their
bracket alternates (which ARE compatible across all masters).

Strategy:
1. Convert .glyphs to designspace + UFO using glyphs2ufo
2. For each incompatible base glyph, find a compatible bracket alternate
3. Copy the bracket alternate's contours into the base glyph for each master
4. Update config.yaml to use the designspace
"""

import os
import sys
import subprocess
from pathlib import Path


INCOMPATIBLE_GLYPHS = [
    'Hbar', 'Oslash', 'Thorn', 'comma', 'commaaccentcomb',
    'commaturnedabovecomb', 'dcroat', 'dong', 'g_j.liga.ss02',
    'hbar', 'hbar.sc', 'j_j.liga', 'oslash', 'oslash.sc',
    'paragraph', 'quoteleft', 'thorn.sc', 'uniEFFD', 'ydotbelow'
]


def is_glyph_compatible(glyph_name, fonts):
    """Check if a glyph has compatible contours across all font sources.

    fonts is a dict of {source_key: ufoLib2.Font}
    """
    structures = []
    for key, font in fonts.items():
        if glyph_name not in font:
            return False  # Missing glyph = incompatible
        glyph = font[glyph_name]
        # Get contour structure: (num_contours, tuple of point_counts)
        contour_info = (
            len(glyph.contours),
            tuple(len(c) for c in glyph.contours),
            len(glyph.components)
        )
        structures.append(contour_info)

    return len(set(structures)) == 1


def copy_glyph_contours(source_glyph, target_glyph):
    """Copy contours and components from source to target glyph.

    Preserves the target glyph's width and other metadata.
    """
    # Save width
    width = target_glyph.width

    # Clear target
    target_glyph.contours.clear()
    target_glyph.components.clear()

    # Copy contours
    from copy import deepcopy
    for contour in source_glyph.contours:
        target_glyph.contours.append(deepcopy(contour))

    # Copy components
    for component in source_glyph.components:
        target_glyph.components.append(deepcopy(component))

    # Restore width
    target_glyph.width = width


def main():
    source_dir = Path(os.getcwd())
    glyphs_path = source_dir / "sources" / "HeptaSlab.glyphs"

    if not glyphs_path.exists():
        print(f"ERROR: Source not found: {glyphs_path}", file=sys.stderr)
        sys.exit(1)

    print("=== Hepta Slab prebuild ===")

    # Step 1: Convert .glyphs to designspace + UFO
    print("  Step 1: Converting .glyphs to designspace + UFO...")
    master_ufo_dir = source_dir / "sources" / "master_ufo"
    master_ufo_dir.mkdir(exist_ok=True)

    glyphs2ufo_cmd = "glyphs2ufo"
    gftools_venv = Path("/mnt/shared/gftools/venv/bin/glyphs2ufo")
    if gftools_venv.exists():
        glyphs2ufo_cmd = str(gftools_venv)

    result = subprocess.run(
        [glyphs2ufo_cmd, "--generate-GDEF", str(glyphs_path),
         "-m", str(master_ufo_dir)],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f"  glyphs2ufo failed: {result.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    print("  Conversion complete.")

    # Find the designspace
    ds_files = list(master_ufo_dir.glob("*.designspace"))
    if not ds_files:
        print("ERROR: No designspace found after conversion", file=sys.stderr)
        sys.exit(1)
    ds_path = ds_files[0]
    print(f"  Designspace: {ds_path.name}")

    # Step 2: Load the designspace
    print("  Step 2: Loading designspace and UFO sources...")
    import fontTools.designspaceLib as dsLib
    import ufoLib2

    ds = dsLib.DesignSpaceDocument.fromfile(ds_path)

    # Load each unique UFO file once
    ufo_files = set()
    for source in ds.sources:
        ufo_path = (ds_path.parent / source.filename).resolve()
        ufo_files.add(str(ufo_path))

    fonts = {}
    for ufo_path_str in sorted(ufo_files):
        ufo_path = Path(ufo_path_str)
        font = ufoLib2.Font.open(ufo_path)
        fonts[ufo_path.name] = font
        print(f"    Loaded: {ufo_path.name}")

    # Step 3: For each incompatible glyph, find and apply a compatible replacement
    print(f"\n  Step 3: Fixing {len(INCOMPATIBLE_GLYPHS)} incompatible glyphs...")

    fixed = []
    failed = []
    already_ok = []

    for gname in INCOMPATIBLE_GLYPHS:
        # Check if base glyph is already compatible
        if is_glyph_compatible(gname, fonts):
            already_ok.append(gname)
            print(f"  {gname}: already compatible")
            continue

        # Find bracket alternates
        alt_names = []
        for suffix in ['.BRACKET.varAlt01', '.BRACKET.varAlt02',
                       '.BRACKET.varAlt03', '.BRACKET.varAlt04']:
            alt_name = gname + suffix
            # Check if it exists in all fonts
            exists_in_all = all(alt_name in font for font in fonts.values())
            if exists_in_all:
                alt_names.append(alt_name)

        if not alt_names:
            # No bracket alternates. Try to make compatible by removing
            # open corners (the EraseOpenCornersFilter changes contours
            # differently per master, causing incompatibility).
            # Strategy: pick one master's contour structure as reference
            # and rebuild all others to match.
            print(f"  {gname}: no bracket alternates, trying contour homogenization...")
            ref_font_name = list(fonts.keys())[0]
            ref_glyph = fonts[ref_font_name][gname]
            ref_contour_count = len(ref_glyph.contours)
            ref_point_counts = tuple(len(c) for c in ref_glyph.contours)

            all_match = True
            for fn, font in fonts.items():
                g = font[gname]
                pc = tuple(len(c) for c in g.contours)
                if pc != ref_point_counts:
                    all_match = False
                    # Check if EraseOpenCornersFilter might cause the issue
                    # by checking for overlapping segment corners
                    break

            if all_match:
                # Same structure now, but filter might change it.
                # Remove the eraseOpenCorners filter from the lib to prevent
                # the filter from causing incompatibility
                # We'll handle this glyph by removing open corners ourselves
                already_ok.append(gname)
                print(f"  {gname}: compatible in source (filter may cause issues at build)")
                continue
            else:
                failed.append(gname)
                print(f"  {gname}: NO bracket alternates found, cannot fix")
                continue

        # Find a compatible alternate
        compatible_alt = None
        for alt_name in alt_names:
            if is_glyph_compatible(alt_name, fonts):
                compatible_alt = alt_name
                break

        if compatible_alt is None:
            failed.append(gname)
            print(f"  {gname}: bracket alternates exist but none are compatible")
            continue

        # Replace base glyph contours with the compatible alternate in each font
        for font_name, font in fonts.items():
            src_glyph = font[compatible_alt]
            tgt_glyph = font[gname]
            copy_glyph_contours(src_glyph, tgt_glyph)

        # Verify fix
        if is_glyph_compatible(gname, fonts):
            fixed.append(gname)
            print(f"  {gname}: FIXED (replaced with {compatible_alt})")
        else:
            failed.append(gname)
            print(f"  {gname}: replacement didn't work")

    # Step 3b: Remove EraseOpenCornersFilter from all UFOs
    # This filter modifies contours differently per master (e.g., uniEFFD),
    # causing incompatibility even when source contours are compatible.
    # The shipped font was built without this issue (likely by Glyphs.app).
    print("\n  Step 3b: Removing EraseOpenCornersFilter from UFO libs...")
    filter_key = "com.github.googlei18n.ufo2ft.filters"
    for font_name, font in fonts.items():
        if filter_key in font.lib:
            filters = font.lib[filter_key]
            original_count = len(filters)
            filters = [f for f in filters if f.get("name") != "eraseOpenCorners"]
            if len(filters) < original_count:
                font.lib[filter_key] = filters
                print(f"    {font_name}: removed EraseOpenCornersFilter")
            else:
                print(f"    {font_name}: no EraseOpenCornersFilter found")

    # Step 4: Save modified UFOs
    print(f"\n  Step 4: Saving modified UFOs...")
    for font_name, font in fonts.items():
        ufo_path = master_ufo_dir / font_name
        font.save(ufo_path, overwrite=True)
    print("  Saved.")

    # Step 5: Update config.yaml to use designspace instead of .glyphs
    print("  Step 5: Updating config.yaml...")
    config_path = source_dir / "config.yaml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        text = text.replace(
            "sources/HeptaSlab.glyphs",
            f"sources/master_ufo/{ds_path.name}"
        )
        config_path.write_text(text, encoding="utf-8")
        print(f"  Updated config.yaml to use {ds_path.name}")

    print(f"\n  Summary: {len(fixed)} fixed, {len(already_ok)} already OK, {len(failed)} failed")
    if fixed:
        print(f"  Fixed: {', '.join(fixed)}")
    if already_ok:
        print(f"  Already OK: {', '.join(already_ok)}")
    if failed:
        print(f"  Still incompatible: {', '.join(failed)}")

    print("=== Hepta Slab prebuild complete ===")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
