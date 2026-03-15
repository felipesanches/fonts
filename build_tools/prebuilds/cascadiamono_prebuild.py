#!/usr/bin/env python3
"""Prebuild script for Cascadia Mono.

Cascadia Code uses a custom build.py that produces 4 font families from the
same sources: Cascadia Code, Cascadia Mono, Cascadia Code NF, Cascadia Mono NF.
The Mono variants strip programming ligatures via different feature files and
rename the family.

This prebuild transforms the Cascadia Code sources so that gftools-builder
can build the Cascadia Mono variant:
  1. Replace features in each master UFO with the Mono feature set
  2. Rename the family from "Cascadia Code" to "Cascadia Mono"
  3. Update designspace family names and instance names
  4. Remove ligature glyph substitution rules from the designspace
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def build_feature_set(features_dir: Path, italic: bool) -> str:
    """Concatenate feature files in the order used by the upstream build.py."""
    if italic:
        feature_list = [
            "header_italic",
            "aalt_italic",
            "ccmp",
            "locl_italic",
            "calt_italic_mono",  # Mono uses *_mono variant
            "figures_italic",
            "frac",
            "ordn",
            "case",
            "salt",
            "ss01",
            "ss02",
            "ss03",
            "ss19",
            "ss20",
            "rclt",
            "zero",
        ]
    else:
        feature_list = [
            "header",
            "aalt",
            "ccmp",
            "locl",
            "calt_mono",  # Mono uses calt_mono instead of calt
            "figures",
            "frac",
            "ordn",
            "case",
            "ss02",
            "ss19",
            "ss20",
            "rclt",
            "zero",
            "init",
            "medi",
            "fina",
            "rlig",
        ]

    result = ""
    for item in feature_list:
        fea_path = features_dir / f"{item}.fea"
        if not fea_path.exists():
            print(f"  WARNING: Feature file not found: {fea_path}", file=sys.stderr)
            continue
        result += fea_path.read_text(encoding="utf-8")
    return result


def patch_ufo_features(ufo_path: Path, feature_text: str) -> None:
    """Replace features.fea inside a UFO with the given text."""
    fea_file = ufo_path / "features.fea"
    fea_file.write_text(feature_text, encoding="utf-8")
    print(f"  Replaced features in {ufo_path.name}")


def patch_ufo_family_name(ufo_path: Path, old_name: str, new_name: str) -> None:
    """Rename the family in a UFO's fontinfo.plist."""
    fontinfo = ufo_path / "fontinfo.plist"
    if not fontinfo.exists():
        return
    text = fontinfo.read_text(encoding="utf-8")
    if old_name in text:
        text = text.replace(old_name, new_name)
        fontinfo.write_text(text, encoding="utf-8")
        print(f"  Renamed family in {ufo_path.name}/fontinfo.plist")


def patch_designspace(ds_path: Path, old_name: str, new_name: str) -> None:
    """Rename family in designspace and remove ligature substitution rules."""
    tree = ET.parse(ds_path)
    root = tree.getroot()

    # Remove designspace <rules> that reference .liga glyphs
    # (these are bracket layer rules for programming ligatures)
    rules_elem = root.find("rules")
    if rules_elem is not None:
        to_remove = []
        for rule in rules_elem.findall("rule"):
            subs = rule.findall("sub")
            for sub in subs:
                name = sub.get("name", "")
                with_val = sub.get("with", "")
                if ".liga" in name or ".liga" in with_val:
                    to_remove.append(rule)
                    break
        for rule in to_remove:
            rules_elem.remove(rule)
            print(f"  Removed ligature rule: {rule.get('name', '?')}")

    # Rename family in sources
    for source in root.iter("source"):
        fn = source.get("familyname", "")
        if old_name in fn:
            source.set("familyname", fn.replace(old_name, new_name))
        nm = source.get("name", "")
        if old_name in nm:
            source.set("name", nm.replace(old_name, new_name))

    # Rename family in instances
    for instance in root.iter("instance"):
        for attr in ["name", "familyname", "stylemapfamilyname"]:
            val = instance.get(attr, "")
            if old_name in val:
                instance.set(attr, val.replace(old_name, new_name))

    tree.write(ds_path, xml_declaration=True, encoding="UTF-8")
    print(f"  Patched designspace: {ds_path.name}")


def main():
    source_dir = Path(os.getcwd())
    features_dir = source_dir / "sources" / "features"

    if not features_dir.exists():
        print(f"ERROR: Features directory not found: {features_dir}", file=sys.stderr)
        sys.exit(1)

    print("=== Cascadia Mono prebuild ===")

    # Build feature sets for roman and italic
    roman_features = build_feature_set(features_dir, italic=False)
    italic_features = build_feature_set(features_dir, italic=True)

    # Patch master UFOs
    sources_dir = source_dir / "sources"
    for ufo_dir in sorted(sources_dir.glob("CascadiaCode-*.ufo")):
        is_italic = "Italic" in ufo_dir.name
        features = italic_features if is_italic else roman_features
        patch_ufo_features(ufo_dir, features)
        patch_ufo_family_name(ufo_dir, "Cascadia Code", "Cascadia Mono")

    # Patch designspaces (before renaming)
    for ds_file in sorted(sources_dir.glob("CascadiaCode*.designspace")):
        patch_designspace(ds_file, "Cascadia Code", "Cascadia Mono")

    # Rename designspace files: strip "_variable" so output filenames match
    # what METADATA.pb expects (CascadiaMono[wght].ttf, CascadiaMono-Italic[wght].ttf)
    rename_map = {
        "CascadiaCode_variable.designspace": "CascadiaMono.designspace",
        "CascadiaCode_variable_italic.designspace": "CascadiaMono-Italic.designspace",
    }
    for old_fn, new_fn in rename_map.items():
        old_path = sources_dir / old_fn
        new_path = sources_dir / new_fn
        if old_path.exists():
            old_path.rename(new_path)
            print(f"  Renamed {old_fn} -> {new_fn}")

    # Update config.yaml to point to renamed designspaces
    config_path = source_dir / "config.yaml"
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        text = text.replace(
            "sources/CascadiaCode_variable.designspace",
            "sources/CascadiaMono.designspace"
        )
        text = text.replace(
            "sources/CascadiaCode_variable_italic.designspace",
            "sources/CascadiaMono-Italic.designspace"
        )
        config_path.write_text(text, encoding="utf-8")
        print(f"  Updated config.yaml sources")

    print("=== Cascadia Mono prebuild complete ===")


if __name__ == "__main__":
    main()
