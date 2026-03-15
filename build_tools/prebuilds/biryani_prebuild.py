#!/usr/bin/env python3
"""Prebuild script for Biryani.

The Biryani .glyphs source has a corrupt descender value: it is stored as
a quoted string ("-500") instead of an unquoted integer (-500). In the
.glyphs plist format, unquoted values are parsed as numbers, while quoted
values are parsed as strings.

This causes glyphsLib to load the descender as a Python str instead of int,
which then fails during interpolation with:
    "can't multiply sequence by non-int of type 'float'"

Fix: rewrite quoted negative-number metric values (descender, etc.) in the
.glyphs file to unquoted integers so glyphsLib parses them correctly.
"""

import os
import re
import sys
from pathlib import Path


# Metric keys that appear at the master level in .glyphs files and must be
# numeric.  The bug manifests as negative numbers being quoted (e.g. "-500")
# because the plist writer treated them as strings.
METRIC_KEYS = [
    "descender",
    "ascender",
    "capHeight",
    "xHeight",
    "italicAngle",
]


def fix_quoted_metrics(glyphs_path: Path) -> int:
    """Fix quoted numeric values for metric keys in a .glyphs file.

    Returns the number of replacements made.
    """
    text = glyphs_path.read_text(encoding="utf-8")
    count = 0

    for key in METRIC_KEYS:
        # Match: key = "number";  where number can be negative / decimal
        # Replace with: key = number;
        pattern = rf'({key}\s*=\s*)"(-?\d+\.?\d*)"(\s*;)'
        replacement = rf'\1\2\3'

        text, n = re.subn(pattern, replacement, text)
        if n > 0:
            print(f"  Fixed {n} quoted {key} value(s)")
            count += n

    if count > 0:
        glyphs_path.write_text(text, encoding="utf-8")

    return count


def main():
    source_dir = Path(os.getcwd())
    glyphs_file = source_dir / "Source Files" / "Biryani 20150307.glyphs"

    if not glyphs_file.exists():
        print(f"ERROR: Source file not found: {glyphs_file}", file=sys.stderr)
        sys.exit(1)

    print("=== Biryani prebuild: fix quoted metric values ===")

    count = fix_quoted_metrics(glyphs_file)

    if count == 0:
        print("  No quoted metrics found (already fixed or different issue)")
    else:
        print(f"  Fixed {count} total quoted metric value(s)")

    print("=== Biryani prebuild complete ===")


if __name__ == "__main__":
    main()
