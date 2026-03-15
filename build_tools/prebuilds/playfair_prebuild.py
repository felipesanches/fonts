#!/usr/bin/env python3
"""Prebuild script for Playfair (used by playfairdisplay and playfairdisplaysc).

Fixes two issues in the Roman .glyphspackage source:

1. Duplicate smart component layers: Several glyphs have layers with
   timestamp-suffixed names that duplicate existing layers' locations,
   causing VariationModel "Locations must be unique" error.

2. Missing smart component axis mappings: Some smart component layers
   are missing axis entries in their partSelection (e.g., 'Thinning'),
   causing KeyError during anchor propagation.

Both fixes edit the raw .glyph plist files in the .glyphspackage directory.
"""

import os
import re
import sys
from pathlib import Path


TIMESTAMP_PATTERN = re.compile(r'\d{1,2}\s+\w{3}\s+\d{2,4}\s+at\s+\d{1,2}:\d{2}')


def find_duplicate_layer_ids(font):
    """Find layerIds of duplicate smart component layers.

    Returns a dict mapping glyph name -> set of layerIds to remove.
    """
    master_ids = {m.id for m in font.masters}
    result = {}

    for glyph in font.glyphs:
        if not (hasattr(glyph, 'smartComponentAxes')
                and glyph.smartComponentAxes
                and len(glyph.smartComponentAxes) > 0):
            continue

        layer_groups = {}
        for layer in glyph.layers:
            if layer.layerId in master_ids:
                assoc = layer.layerId
            else:
                assoc = getattr(layer, 'associatedMasterId', None) or layer.layerId

            mapping = {}
            if hasattr(layer, 'smartComponentPoleMapping') and layer.smartComponentPoleMapping:
                mapping = dict(layer.smartComponentPoleMapping)

            key = (assoc, tuple(sorted(mapping.items())))
            if key not in layer_groups:
                layer_groups[key] = []
            layer_groups[key].append(layer)

        ids_to_remove = set()
        for key, layers in layer_groups.items():
            if len(layers) <= 1:
                continue

            clean = [l for l in layers if not TIMESTAMP_PATTERN.search(l.name)]
            timestamped = [l for l in layers if TIMESTAMP_PATTERN.search(l.name)]

            if clean and timestamped:
                for l in timestamped:
                    ids_to_remove.add(l.layerId)
            elif not clean and len(timestamped) > 1:
                for l in timestamped[1:]:
                    ids_to_remove.add(l.layerId)

        if ids_to_remove:
            result[glyph.name] = ids_to_remove
            for lid in ids_to_remove:
                layer = next(l for l in glyph.layers if l.layerId == lid)
                print(f"    Will remove layer '{layer.name}' from '{glyph.name}'")

    return result


def find_missing_axis_mappings(font):
    """Find smart component layers missing axis entries in partSelection.

    Returns a dict: glyph_name -> {layer_id: {axis_name: default_value}}

    The default value for a missing axis is determined by the first master
    layer's mapping for that axis. This ensures the missing axis gets a
    normalized location of 0.0 (same as default), not 1.0 (which would
    create duplicate locations).

    If no master layer has a mapping for an axis (i.e., all master layers
    are missing it), the default is Pole.MIN (1), following Glyphs convention.
    """
    master_ids = {m.id for m in font.masters}
    result = {}

    for glyph in font.glyphs:
        if not (hasattr(glyph, 'smartComponentAxes')
                and glyph.smartComponentAxes
                and len(glyph.smartComponentAxes) > 0):
            continue

        axis_names = {ax.name for ax in glyph.smartComponentAxes}

        # Find the reference mapping: the first master layer that has a mapping
        # This determines what the "default" pole is for each axis
        reference_mapping = {}
        for layer in glyph.layers:
            if layer.layerId in master_ids:
                if hasattr(layer, 'smartComponentPoleMapping') and layer.smartComponentPoleMapping:
                    reference_mapping = dict(layer.smartComponentPoleMapping)
                    break

        glyph_fixes = {}
        for layer in glyph.layers:
            mapping = {}
            if hasattr(layer, 'smartComponentPoleMapping') and layer.smartComponentPoleMapping:
                mapping = dict(layer.smartComponentPoleMapping)

            # For master layers with empty mappings, add ALL axes at reference values
            # This ensures they appear in the smart_layers list
            if not mapping and layer.layerId in master_ids:
                if reference_mapping:
                    glyph_fixes[layer.layerId] = dict(reference_mapping)
                else:
                    # No reference mapping found - use MIN (1) for all axes
                    glyph_fixes[layer.layerId] = {ax: 1 for ax in axis_names}
                continue

            if not mapping:
                continue

            missing = axis_names - set(mapping.keys())
            if missing:
                # Set missing axes to the same value as the reference (first master)
                # This ensures normalized_location = 0.0 for missing axes
                fixes = {}
                for ax in missing:
                    fixes[ax] = reference_mapping.get(ax, 1)  # default to MIN (1)
                glyph_fixes[layer.layerId] = fixes

        if glyph_fixes:
            result[glyph.name] = glyph_fixes
            for lid, fixes in glyph_fixes.items():
                layer = next((l for l in glyph.layers if l.layerId == lid), None)
                if layer:
                    print(f"    Will add missing axes {fixes} to layer '{layer.name}' in '{glyph.name}'")

    return result


def find_wrong_pole_layers(font):
    """Find smart component layers with wrong pole values causing duplicate locations.

    After adding missing axes, some layers may still create duplicate locations
    because their partSelection has the wrong pole value. For example, a layer
    named "Thinning" with partSelection {Thinning: 1} (MIN) when it should be
    {Thinning: 2} (MAX).

    This function detects such cases by simulating the normalized location
    computation and finding layers with identical locations but different names.

    Returns a dict: glyph_name -> {layer_id: {axis_name: correct_value}}
    """
    master_ids = {m.id for m in font.masters}
    result = {}

    for glyph in font.glyphs:
        if not (hasattr(glyph, 'smartComponentAxes')
                and glyph.smartComponentAxes
                and len(glyph.smartComponentAxes) > 0):
            continue

        axis_names = [ax.name for ax in glyph.smartComponentAxes]

        # Get smart layers per master (layers with smartComponentPoleMapping)
        master_smart_layers = {}
        for layer in glyph.layers:
            if not (hasattr(layer, 'smartComponentPoleMapping') and layer.smartComponentPoleMapping):
                continue
            mapping = dict(layer.smartComponentPoleMapping)
            if layer.layerId in master_ids:
                assoc = layer.layerId
            else:
                assoc = getattr(layer, 'associatedMasterId', None) or layer.layerId
            master_smart_layers.setdefault(assoc, []).append(layer)

        glyph_fixes = {}

        for master_id, layers in master_smart_layers.items():
            if len(layers) < 2:
                continue

            # Find base layer (first in the list)
            base = layers[0]
            base_mapping = dict(base.smartComponentPoleMapping) if base.smartComponentPoleMapping else {}

            # Compute normalized locations
            locs = {}
            for layer in layers:
                mapping = dict(layer.smartComponentPoleMapping) if layer.smartComponentPoleMapping else {}
                loc = {}
                for ax_name in axis_names:
                    cur = mapping.get(ax_name, base_mapping.get(ax_name, 1))
                    base_val = base_mapping.get(ax_name, 1)
                    if cur == base_val:
                        loc[ax_name] = 0.0
                    elif base_val == 1 and cur == 2:  # MIN -> MAX
                        loc[ax_name] = 1.0
                    elif base_val == 2 and cur == 1:  # MAX -> MIN
                        loc[ax_name] = -1.0
                    else:
                        loc[ax_name] = 0.0
                loc_key = tuple(sorted(loc.items()))
                locs.setdefault(loc_key, []).append(layer)

            # Find duplicate locations
            for loc_key, dup_layers in locs.items():
                if len(dup_layers) <= 1:
                    continue

                # Try to fix: if a layer's name matches an axis name,
                # and that axis is at the wrong pole, fix it
                for layer in dup_layers:
                    mapping = dict(layer.smartComponentPoleMapping)
                    layer_name = layer.name.strip()

                    for ax_name in axis_names:
                        if layer_name == ax_name and ax_name in mapping:
                            base_val = base_mapping.get(ax_name, 1)
                            cur_val = mapping[ax_name]
                            # The layer is named after this axis but has same value as base
                            if cur_val == base_val:
                                # Flip to the opposite pole
                                correct_val = 2 if cur_val == 1 else 1
                                if layer.layerId not in glyph_fixes:
                                    glyph_fixes[layer.layerId] = {}
                                glyph_fixes[layer.layerId][ax_name] = correct_val
                                print(f"    Will fix pole {ax_name}: {cur_val}->{correct_val} in layer '{layer.name}' of '{glyph.name}'")

        if glyph_fixes:
            result[glyph.name] = glyph_fixes

    return result


def find_layer_blocks(content):
    """Parse old-style plist to find all layer block boundaries.

    Returns a list of (start, end) tuples for each layer block within
    the layers = (...) array.
    """
    layers_match = re.search(r'layers\s*=\s*\(', content)
    if not layers_match:
        return [], -1, -1

    layers_start = layers_match.end()

    pos = layers_start
    paren_depth = 1
    while pos < len(content) and paren_depth > 0:
        ch = content[pos]
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1
        elif ch == '"':
            pos += 1
            while pos < len(content) and content[pos] != '"':
                if content[pos] == '\\':
                    pos += 1
                pos += 1
        pos += 1
    layers_end = pos

    blocks = []
    pos = layers_start
    while pos < layers_end:
        ch = content[pos]
        if ch == '{':
            block_start = pos
            depth = 1
            pos += 1
            while pos < layers_end and depth > 0:
                c = content[pos]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                elif c == '"':
                    pos += 1
                    while pos < layers_end and content[pos] != '"':
                        if content[pos] == '\\':
                            pos += 1
                        pos += 1
                pos += 1
            block_end = pos
            blocks.append((block_start, block_end))
        else:
            pos += 1

    return blocks, layers_start, layers_end


def remove_layers_from_glyph_file(glyph_file, layer_ids_to_remove):
    """Remove specific layers from a .glyph file by layerId.

    Returns the number of layers removed.
    """
    with open(glyph_file, 'r', encoding='utf-8') as f:
        content = f.read()

    blocks, layers_start, layers_end = find_layer_blocks(content)
    if not blocks:
        return 0

    blocks_to_remove = []
    for block_start, block_end in blocks:
        block_text = content[block_start:block_end]
        for layer_id in layer_ids_to_remove:
            if f'layerId = "{layer_id}"' in block_text:
                blocks_to_remove.append((block_start, block_end))
                break

    if not blocks_to_remove:
        return 0

    blocks_to_remove.sort(reverse=True)
    for block_start, block_end in blocks_to_remove:
        trim_start = block_start
        while trim_start > 0 and content[trim_start - 1] in ' \t\n\r':
            trim_start -= 1

        trim_end = block_end
        while trim_end < len(content) and content[trim_end] in ' \t\n\r,':
            trim_end += 1

        content = content[:trim_start] + '\n' + content[trim_end:]

    with open(glyph_file, 'w', encoding='utf-8') as f:
        f.write(content)

    return len(blocks_to_remove)


def add_missing_axes_to_glyph_file(glyph_file, layer_axis_fixes):
    """Add missing axis entries to partSelection blocks in a .glyph file.

    layer_axis_fixes: dict of layer_id -> {axis_name: value}

    For each layer, finds the partSelection block (or creates one if absent)
    and adds the missing axis entries.

    Returns the number of layers fixed.
    """
    with open(glyph_file, 'r', encoding='utf-8') as f:
        content = f.read()

    fixed = 0
    for layer_id, axis_fixes in layer_axis_fixes.items():
        id_str = f'layerId = "{layer_id}"'
        id_pos = content.find(id_str)
        if id_pos == -1:
            continue

        # Find the layer block boundaries
        block_start = id_pos
        depth = 0
        while block_start > 0:
            block_start -= 1
            if content[block_start] == '}':
                depth += 1
            elif content[block_start] == '{':
                if depth == 0:
                    break
                depth -= 1

        block_end = id_pos
        depth = 0
        in_block = False
        while block_end < len(content):
            if content[block_end] == '{':
                depth += 1
                in_block = True
            elif content[block_end] == '}':
                depth -= 1
                if in_block and depth == 0:
                    block_end += 1
                    break
            block_end += 1

        block_text = content[block_start:block_end]

        # Check if partSelection exists in this block
        ps_match = re.search(r'partSelection\s*=\s*\{', block_text)
        if ps_match:
            # Find the closing } of partSelection
            ps_start = ps_match.end()
            ps_depth = 1
            ps_pos = ps_start
            while ps_pos < len(block_text) and ps_depth > 0:
                if block_text[ps_pos] == '{':
                    ps_depth += 1
                elif block_text[ps_pos] == '}':
                    ps_depth -= 1
                ps_pos += 1
            # ps_pos is now after the closing }
            # Insert before the closing }
            insert_pos = block_start + ps_pos - 1
            new_entries = ''
            for ax_name, ax_val in axis_fixes.items():
                new_entries += f'{ax_name} = {ax_val};\n'
            content = content[:insert_pos] + new_entries + content[insert_pos:]
            fixed += 1
        else:
            # No partSelection block - create one before the layerId line
            entries = ';\n'.join(f'{ax} = {val}' for ax, val in axis_fixes.items())
            ps_block = f'partSelection = {{\n{entries};\n}};\n'
            content = content[:id_pos] + ps_block + content[id_pos:]
            fixed += 1

    if fixed > 0:
        with open(glyph_file, 'w', encoding='utf-8') as f:
            f.write(content)

    return fixed


def fix_partselection_values(glyph_file, layer_fixes):
    """Fix incorrect partSelection values in a .glyph file.

    layer_fixes: dict of layer_id -> {axis_name: correct_value}

    For each layer, finds the partSelection block and updates the specified
    axis values.

    Returns the number of layers fixed.
    """
    with open(glyph_file, 'r', encoding='utf-8') as f:
        content = f.read()

    fixed = 0
    for layer_id, axis_fixes in layer_fixes.items():
        for axis_name, correct_value in axis_fixes.items():
            # Find the layerId
            id_str = f'layerId = "{layer_id}"'
            id_pos = content.find(id_str)
            if id_pos == -1:
                continue

            # Find the layer block boundaries
            block_start = id_pos
            depth = 0
            while block_start > 0:
                block_start -= 1
                if content[block_start] == '}':
                    depth += 1
                elif content[block_start] == '{':
                    if depth == 0:
                        break
                    depth -= 1

            block_end = id_pos + len(id_str)
            depth = 0
            started = False
            pos = block_start
            while pos < len(content):
                if content[pos] == '{':
                    depth += 1
                    started = True
                elif content[pos] == '}':
                    depth -= 1
                    if started and depth == 0:
                        block_end = pos + 1
                        break
                pos += 1

            block_text = content[block_start:block_end]

            # Find and replace the axis value in partSelection
            # Pattern: "axis_name = old_value;" within the partSelection block
            ps_match = re.search(r'partSelection\s*=\s*\{', block_text)
            if ps_match:
                ps_start = ps_match.end()
                ps_end = block_text.find('}', ps_start)
                ps_text = block_text[ps_start:ps_end]

                # Replace the specific axis value
                old_pattern = re.compile(rf'{re.escape(axis_name)}\s*=\s*\d+')
                new_text = old_pattern.sub(f'{axis_name} = {correct_value}', ps_text)

                if new_text != ps_text:
                    new_block = block_text[:ps_start] + new_text + block_text[ps_end:]
                    content = content[:block_start] + new_block + content[block_end:]
                    fixed += 1

    if fixed > 0:
        with open(glyph_file, 'w', encoding='utf-8') as f:
            f.write(content)

    return fixed


def glyph_name_to_filename(name):
    """Convert a glyph name to its .glyph filename in a .glyphspackage."""
    parts = []
    for char in name:
        if char.isupper():
            parts.append('_' + char)
        elif char == '/':
            parts.append('_')
        else:
            parts.append(char)
    return ''.join(parts) + '.glyph'


def find_glyph_file(glyphs_dir, glyph_name):
    """Find the .glyph file for a given glyph name."""
    filename = glyph_name_to_filename(glyph_name)
    glyph_file = glyphs_dir / filename

    if glyph_file.exists():
        return glyph_file

    # Fallback: scan directory
    for f in glyphs_dir.iterdir():
        if f.suffix == '.glyph':
            with open(f, 'r') as fh:
                first_lines = fh.read(500)
            if f'glyphname = "{glyph_name}"' in first_lines:
                return f

    return None


def main():
    source_dir = Path(os.getcwd())

    sources = [
        source_dir / "sources" / "Playfair-2_2-Roman.glyphspackage",
        source_dir / "sources" / "Playfair-2_2-Italic.glyphspackage",
    ]

    try:
        import glyphsLib
    except ImportError:
        print("ERROR: glyphsLib not available", file=sys.stderr)
        return 1

    total_changes = 0

    for src in sources:
        if not src.exists():
            print(f"  Skipping {src.name} (not found)")
            continue

        print(f"  Loading {src.name}...")
        font = glyphsLib.load(str(src))
        glyphs_dir = src / "glyphs"

        # Fix 1: Remove duplicate smart component layers
        print(f"  Checking for duplicate smart component layers...")
        duplicates = find_duplicate_layer_ids(font)
        source_removed = 0
        for glyph_name, layer_ids in duplicates.items():
            glyph_file = find_glyph_file(glyphs_dir, glyph_name)
            if glyph_file is None:
                print(f"    WARNING: Could not find glyph file for '{glyph_name}'")
                continue
            removed = remove_layers_from_glyph_file(glyph_file, layer_ids)
            if removed > 0:
                print(f"    Removed {removed} layers from {glyph_file.name}")
                source_removed += removed

        if source_removed > 0:
            print(f"  Removed {source_removed} duplicate layers from {src.name}")
            total_changes += source_removed

            # Reload after removing layers (positions changed)
            print(f"  Reloading {src.name} after duplicate removal...")
            font = glyphsLib.load(str(src))

        # Fix 2: Add missing smart component axis mappings
        print(f"  Checking for missing smart component axis mappings...")
        missing_axes = find_missing_axis_mappings(font)
        source_fixed = 0
        for glyph_name, layer_fixes in missing_axes.items():
            glyph_file = find_glyph_file(glyphs_dir, glyph_name)
            if glyph_file is None:
                print(f"    WARNING: Could not find glyph file for '{glyph_name}'")
                continue
            fixed = add_missing_axes_to_glyph_file(glyph_file, layer_fixes)
            if fixed > 0:
                print(f"    Fixed {fixed} layers in {glyph_file.name}")
                source_fixed += fixed

        if source_fixed > 0:
            print(f"  Fixed {source_fixed} layers with missing axes in {src.name}")
            total_changes += source_fixed

            # Reload after adding missing axes
            print(f"  Reloading {src.name} after axis fixes...")
            font = glyphsLib.load(str(src))

        # Fix 3: Fix wrong pole values that create duplicate locations
        print(f"  Checking for wrong pole values...")
        wrong_poles = find_wrong_pole_layers(font)
        source_pole_fixes = 0
        for glyph_name, layer_fixes in wrong_poles.items():
            glyph_file = find_glyph_file(glyphs_dir, glyph_name)
            if glyph_file is None:
                print(f"    WARNING: Could not find glyph file for '{glyph_name}'")
                continue
            fixed = fix_partselection_values(glyph_file, layer_fixes)
            if fixed > 0:
                print(f"    Fixed {fixed} pole values in {glyph_file.name}")
                source_pole_fixes += fixed

        if source_pole_fixes > 0:
            print(f"  Fixed {source_pole_fixes} wrong pole values in {src.name}")
            total_changes += source_pole_fixes

    if total_changes > 0:
        print(f"  Grand total: {total_changes} fixes applied")

        # Verify the fix by reloading all modified sources
        print("  Verifying fix...")
        for src in sources:
            if not src.exists():
                continue
            try:
                font = glyphsLib.load(str(src))
                print(f"    {src.name}: loads OK after fix")
            except Exception as e:
                print(f"    ERROR: {src.name} failed to load after fix: {e}")
                return 1
    else:
        print("  No fixes needed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
