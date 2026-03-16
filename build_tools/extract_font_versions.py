#!/usr/bin/env python3
"""Extract build tool version info from reference font files in google/fonts.

For every non-byte-identical family with status in (compiler-version,
timestamp-diff, name-table), extract version strings, ttfautohint info,
head timestamps, and font revision from the .ttf files on disk.

Output: build_tools/font_version_archaeology.json
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

REPO = "/mnt/shared/google/fonts"
REGISTRY = os.path.join(REPO, "build_tools/build_registry.json")
OUTPUT = os.path.join(REPO, "build_tools/font_version_archaeology.json")

TARGET_STATUSES = {"compiler-version", "timestamp-diff", "name-table"}


def parse_ttfautohint_version(version_string: str) -> str | None:
    """Extract ttfautohint version from a name ID 5 version string."""
    m = re.search(r"ttfautohint\s*\(v?([^)]+)\)", version_string)
    if m:
        v = m.group(1).strip()
        return f"v{v}" if not v.startswith("v") else v
    return None


def parse_fontmake_version(name_records: dict[int, str]) -> str | None:
    """Look for fontmake version in name table records."""
    for nid in (10, 5, 13):  # description, version, license description
        text = name_records.get(nid, "")
        m = re.search(r"fontmake\s*[v(]*([\d.]+)", text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def get_name_records(font: TTFont) -> dict[int, str]:
    """Get relevant name table records (prefer platformID=3 Windows, then 1 Mac)."""
    records: dict[int, str] = {}
    if "name" not in font:
        return records
    for rec in font["name"].names:
        nid = rec.nameID
        if nid in (5, 10, 13):
            try:
                text = rec.toUnicode()
            except Exception:
                continue
            # Prefer Windows (platformID 3) over Mac (platformID 1)
            if nid not in records or rec.platformID == 3:
                records[nid] = text
    return records


def get_git_date(family_dir: str) -> str | None:
    """Get the git commit date for when .ttf files were last modified."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO, "log", "-1", "--format=%ci", "--",
             f"{family_dir}/*.ttf"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            # Return just the date portion (YYYY-MM-DD)
            return result.stdout.strip().split(" ")[0]
    except Exception:
        pass
    return None


def process_family(family_name: str) -> dict | None:
    """Process a single font family and extract version info."""
    family_path = os.path.join(REPO, "ofl", family_name)
    if not os.path.isdir(family_path):
        return None

    ttf_files = sorted([
        f for f in os.listdir(family_path)
        if f.lower().endswith(".ttf")
    ])
    if not ttf_files:
        return None

    result = {
        "git_date": None,
        "ttfautohint_version": None,
        "fontmake_version": None,
        "font_revision": None,
        "head_created": None,
        "head_modified": None,
        "version_string": None,
        "name_id_10": None,
        "files": ttf_files,
    }

    # Get git date
    rel_dir = os.path.relpath(family_path, REPO)
    result["git_date"] = get_git_date(rel_dir)

    # Extract info from the first TTF (they typically share version info)
    primary_font_path = os.path.join(family_path, ttf_files[0])
    try:
        font = TTFont(primary_font_path)
    except Exception as e:
        result["error"] = str(e)
        return result

    # Name table records
    name_records = get_name_records(font)
    version_string = name_records.get(5, "")
    result["version_string"] = version_string or None
    result["name_id_10"] = name_records.get(10) or None

    # ttfautohint version
    if version_string:
        result["ttfautohint_version"] = parse_ttfautohint_version(version_string)

    # fontmake version
    result["fontmake_version"] = parse_fontmake_version(name_records)

    # head table
    if "head" in font:
        head = font["head"]
        result["font_revision"] = round(head.fontRevision, 4)
        result["head_created"] = head.created
        result["head_modified"] = head.modified

    font.close()
    return result


def main():
    with open(REGISTRY) as f:
        registry = json.load(f)

    families = {
        name: info
        for name, info in registry["families"].items()
        if info.get("reproducible_build") in TARGET_STATUSES
    }

    print(f"Processing {len(families)} families...")

    results = {}
    errors = 0
    for i, (family_name, _info) in enumerate(sorted(families.items()), 1):
        if i % 100 == 0:
            print(f"  {i}/{len(families)}...")
        data = process_family(family_name)
        if data:
            results[family_name] = data
        else:
            errors += 1

    with open(OUTPUT, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Processed {len(results)} families, {errors} skipped.")
    print(f"Output: {OUTPUT}")

    # Quick summary
    has_ttfah = sum(1 for v in results.values() if v.get("ttfautohint_version"))
    has_fontmake = sum(1 for v in results.values() if v.get("fontmake_version"))
    print(f"\nWith ttfautohint version: {has_ttfah}")
    print(f"With fontmake version: {has_fontmake}")


if __name__ == "__main__":
    main()
