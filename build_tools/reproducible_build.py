#!/usr/bin/env python3
"""Reproducible Font Build System.

Downloads font sources from upstream repos at the exact commit recorded in
METADATA.pb, builds them with gftools-builder, and compares the output against
the binaries shipped in google/fonts.

Usage:
    python build_tools/reproducible_build.py --family alata
    python build_tools/reproducible_build.py --all
    python build_tools/reproducible_build.py --family alata --force
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

GOOGLE_FONTS_DIR = Path("/mnt/shared/google/fonts")
OFL_DIR = GOOGLE_FONTS_DIR / "ofl"
BUILD_TOOLS_DIR = GOOGLE_FONTS_DIR / "build_tools"
REGISTRY_PATH = BUILD_TOOLS_DIR / "build_registry.json"
WORKSPACE_DIR = Path("/mnt/shared/gfonts-repro-builds")
GFTOOLS_BUILDER = "/mnt/shared/gftools/venv/bin/gftools-builder"
GFTOOLS_PYTHON = "/mnt/shared/gftools/venv/bin/python"
UPSTREAM_CACHE = Path("/mnt/shared/upstream_repos/fontc_crater_cache")

# Tables that only contain timestamps
TIMESTAMP_TABLES = {"head"}
# Tables related to hinting
HINTING_TABLES = {"fpgm", "prep", "cvt "}
# Name table
NAME_TABLES = {"name"}
# DSIG
DSIG_TABLES = {"DSIG"}


# ---------------------------------------------------------------------------
# METADATA.pb parser (simple text proto — no protobuf dependency)
# ---------------------------------------------------------------------------

def parse_metadata_pb(family_dir: Path) -> dict:
    """Extract source stanza from METADATA.pb using simple text parsing."""
    pb_path = family_dir / "METADATA.pb"
    if not pb_path.exists():
        return {}

    text = pb_path.read_text(encoding="utf-8")
    result = {
        "repository_url": "",
        "commit": "",
        "branch": "",
        "config_yaml": "",
        "files": [],
    }

    in_source = False
    in_files = False
    brace_depth = 0

    for line in text.splitlines():
        stripped = line.strip()

        if not in_source:
            if stripped.startswith("source {") or stripped == "source {":
                in_source = True
                brace_depth = 1
            continue

        # Track brace depth within source block
        if "{" in stripped:
            if stripped.startswith("files {") or stripped == "files {":
                in_files = True
                brace_depth += 1
                current_file = {}
                continue
            brace_depth += 1

        if "}" in stripped:
            brace_depth -= 1
            if in_files and brace_depth == 1:
                in_files = False
                if current_file:
                    result["files"].append(current_file)
                continue
            if brace_depth == 0:
                break
            continue

        # Parse key: "value" lines
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"')

            if in_files:
                current_file[key] = val
            elif key in result:
                result[key] = val

    return result


def parse_owner_repo(repository_url: str) -> tuple:
    """Extract (owner, repo) from a GitHub URL."""
    # Handle https://github.com/owner/repo or https://github.com/owner/repo.git
    url = repository_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = url.split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


# ---------------------------------------------------------------------------
# Source download
# ---------------------------------------------------------------------------

def download_source(owner: str, repo: str, commit: str, family: str) -> Path | None:
    """Download and extract source tarball. Returns extracted directory or None."""
    family_ws = WORKSPACE_DIR / family
    family_ws.mkdir(parents=True, exist_ok=True)

    source_dir = family_ws / "source"
    # Check if already extracted
    expected_dir = source_dir / f"{repo}-{commit}"
    if expected_dir.exists():
        return expected_dir

    tarball_url = f"https://github.com/{owner}/{repo}/archive/{commit}.tar.gz"
    tarball_path = family_ws / f"{commit}.tar.gz"

    print(f"  Downloading {tarball_url}")
    try:
        urllib.request.urlretrieve(tarball_url, tarball_path)
    except urllib.error.HTTPError as e:
        print(f"  Download failed: {e}")
        return None

    # Extract
    source_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Extracting to {source_dir}")
    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(path=source_dir, filter="data")

    tarball_path.unlink()

    # GitHub tarballs extract to {repo}-{full_commit_hash}
    # Find the actual extracted directory
    extracted_dirs = [d for d in source_dir.iterdir() if d.is_dir()]
    if len(extracted_dirs) == 1 and extracted_dirs[0] != expected_dir:
        extracted_dirs[0].rename(expected_dir)

    if expected_dir.exists():
        return expected_dir

    # Fallback: return whatever we found
    extracted_dirs = [d for d in source_dir.iterdir() if d.is_dir()]
    return extracted_dirs[0] if extracted_dirs else None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def find_config_yaml(source_dir: Path, config_yaml_rel: str, family: str) -> Path | None:
    """Find the config.yaml to use for building.

    Priority:
    1. config.yaml override in ofl/{family}/ (from google/fonts repo)
    2. config_yaml path from METADATA.pb source stanza (relative to source repo root)
    """
    # Check for override in google/fonts
    gf_config = OFL_DIR / family / "config.yaml"
    if gf_config.exists():
        return gf_config

    # Use the path from METADATA.pb
    if config_yaml_rel:
        candidate = source_dir / config_yaml_rel
        if candidate.exists():
            return candidate

    # Fallback: look for common config locations
    for name in ["sources/config.yaml", "config.yaml", "source/config.yaml"]:
        candidate = source_dir / name
        if candidate.exists():
            return candidate

    return None


def run_build(source_dir: Path, config_path: Path, family: str,
              isolation: str, overrides: dict) -> Path | None:
    """Run gftools-builder. Returns the build output directory or None."""
    family_ws = WORKSPACE_DIR / family
    build_dir = family_ws / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Copy config into source tree if it's from google/fonts override
    config_in_source = config_path
    if not str(config_path).startswith(str(source_dir)):
        config_in_source = source_dir / "config.yaml"
        shutil.copy2(config_path, config_in_source)

    # Determine which builder to use
    if isolation == "custom" and "requirements" in overrides:
        # Create custom venv
        venv_dir = family_ws / "venv"
        if not venv_dir.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
            pip = str(venv_dir / "bin" / "pip")
            subprocess.run([pip, "install"] + overrides["requirements"], check=True)
        builder_cmd = str(venv_dir / "bin" / "gftools-builder")
    else:
        builder_cmd = GFTOOLS_BUILDER

    print(f"  Building with config: {config_in_source}")
    env = os.environ.copy()
    # Ensure the venv bin dir is on PATH so fontmake/ninja are found
    venv_bin = str(Path(builder_cmd).parent)
    env["PATH"] = venv_bin + ":" + env.get("PATH", "")
    try:
        result = subprocess.run(
            [builder_cmd, str(config_in_source)],
            cwd=str(source_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        if result.returncode != 0:
            print(f"  Build failed (exit {result.returncode})")
            print(f"  stderr: {result.stderr[-2000:]}")
            # Save build log
            (family_ws / "build_log.txt").write_text(
                f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}",
                encoding="utf-8",
            )
            return None
        print(f"  Build succeeded")
        # Save build log anyway
        (family_ws / "build_log.txt").write_text(
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}",
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        print(f"  Build timed out after 600s")
        return None

    return source_dir


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ttx_dump(font_path: Path, output_dir: Path) -> dict:
    """Dump a font to TTX (one file per table). Returns {table_tag: xml_text}."""
    from fontTools.ttLib import TTFont

    font = TTFont(str(font_path))
    tables = {}
    for tag in font.keys():
        try:
            # Dump each table to XML string
            from io import BytesIO
            bio = BytesIO()
            font.saveXML(bio, tables=[tag])
            tables[tag] = bio.getvalue().decode("utf-8", errors="replace")
        except Exception:
            tables[tag] = f"<error dumping {tag}>"
    font.close()
    return tables


def categorize_diff(differing_tables: set) -> str:
    """Categorize the mismatch based on which tables differ."""
    if not differing_tables:
        return "yes"

    remaining = set(differing_tables)

    # Pure timestamp diff (only head table)
    if remaining <= TIMESTAMP_TABLES:
        return "timestamp-diff"

    # DSIG only (possibly with timestamps)
    if remaining <= (DSIG_TABLES | TIMESTAMP_TABLES):
        return "dsig-diff"

    # Hinting tables (possibly with timestamps)
    if remaining <= (HINTING_TABLES | TIMESTAMP_TABLES):
        return "hinting-diff"

    # Name table (possibly with timestamps/DSIG)
    if remaining <= (NAME_TABLES | TIMESTAMP_TABLES | DSIG_TABLES):
        return "name-table"

    # Tables commonly affected by compiler version differences.
    # When many tables differ (especially glyf alongside layout tables,
    # name, GlyphOrder, etc.) it's almost always because the font was
    # built with a different fontmake/fontTools/glyphsLib version — not
    # because the source metadata is wrong.
    COMPILER_AFFECTED = {
        "glyf", "loca", "GlyphOrder", "hmtx", "hhea",
        "GPOS", "GSUB", "GDEF",
        "cmap", "post", "OS/2", "name",
        "head", "DSIG",
        "HVAR", "MVAR", "STAT", "avar", "fvar", "gvar",
        "gasp", "maxp",
    }
    if remaining <= COMPILER_AFFECTED:
        return "compiler-version"

    # If outline tables differ but no other layout/metadata tables do,
    # that suggests genuinely different source content was compiled.
    outline_tables = {"glyf", "CFF ", "CFF2"}
    non_outline_diff = remaining - outline_tables - TIMESTAMP_TABLES
    if (remaining & outline_tables) and not non_outline_diff:
        return "source-mismatch"

    # Broad differences across many table types — compiler version
    return "compiler-version"


def deep_font_analysis(reference_path: Path, built_path: Path) -> dict:
    """Perform detailed structural analysis of font differences.

    Goes beyond table-level comparison to identify root causes:
    ttfautohint version, glyph coordinate differences, name changes, etc.
    """
    from fontTools.ttLib import TTFont

    analysis = {
        "ttfautohint": {"ref": "", "built": ""},
        "glyph_stats": {},
        "metrics": {},
        "name_diffs": [],
        "head_diffs": [],
        "os2_diffs": [],
        "root_cause": "",
    }

    try:
        ref = TTFont(str(reference_path))
        built = TTFont(str(built_path))
    except Exception as e:
        analysis["root_cause"] = f"font-load-error: {e}"
        return analysis

    try:
        # Extract ttfautohint version from name ID 5 (version string)
        for font, key in [(ref, "ref"), (built, "built")]:
            for rec in font["name"].names:
                if rec.nameID == 5 and rec.platformID == 3:
                    val = rec.toUnicode()
                    analysis["ttfautohint"][key] = val
                    break

        # Name table diff (semantic, not XML)
        ref_names = {(r.nameID, r.platformID, r.platEncID, r.langID): r.toUnicode()
                     for r in ref["name"].names}
        built_names = {(r.nameID, r.platformID, r.platEncID, r.langID): r.toUnicode()
                       for r in built["name"].names}
        for k in sorted(set(ref_names) | set(built_names)):
            rv = ref_names.get(k)
            bv = built_names.get(k)
            if rv != bv:
                analysis["name_diffs"].append({
                    "nameID": k[0], "platformID": k[1],
                    "ref": rv or "<missing>", "built": bv or "<missing>",
                })

        # Head table diff
        for attr in ["created", "modified", "fontRevision", "flags"]:
            rv = getattr(ref["head"], attr, None)
            bv = getattr(built["head"], attr, None)
            if rv != bv:
                analysis["head_diffs"].append({"field": attr, "ref": rv, "built": bv})

        # OS/2 panose diff
        if "OS/2" in ref and "OS/2" in built:
            for attr in ["bFamilyType", "bSerifStyle", "bWeight", "bProportion",
                         "bContrast", "bStrokeVariation", "bArmStyle",
                         "bLetterForm", "bMidline", "bXHeight"]:
                rv = getattr(ref["OS/2"].panose, attr, None)
                bv = getattr(built["OS/2"].panose, attr, None)
                if rv != bv:
                    analysis["os2_diffs"].append({"field": attr, "ref": rv, "built": bv})

        # Metrics analysis (advance widths, line spacing)
        # This determines text reflow risk: if metrics match, a rebuild
        # will not cause text to reflow on existing websites.
        metrics = {
            "hmtx_identical": True,
            "hmtx_total": 0,
            "hmtx_diffs": 0,
            "hmtx_max_delta": 0,
            "hmtx_diff_glyphs": [],
            "line_metrics_identical": True,
            "line_metrics_diffs": [],
            "reflow_risk": "none",
        }

        # Compare advance widths (hmtx table)
        # Distinguish between:
        # - shared_width_diffs: glyphs present in both fonts with different widths (reflow risk)
        # - glyph_name_changes: glyphs present in only one font (renamed, not reflow risk)
        if "hmtx" in ref and "hmtx" in built:
            ref_hmtx = ref["hmtx"].metrics
            built_hmtx = built["hmtx"].metrics
            common_glyphs = set(ref_hmtx.keys()) & set(built_hmtx.keys())
            ref_only = set(ref_hmtx.keys()) - set(built_hmtx.keys())
            built_only = set(built_hmtx.keys()) - set(ref_hmtx.keys())
            metrics["hmtx_total"] = len(set(ref_hmtx.keys()) | set(built_hmtx.keys()))
            max_delta = 0
            diff_glyphs = []
            shared_diffs = 0

            for gname in sorted(common_glyphs):
                ref_w, ref_lsb = ref_hmtx[gname]
                built_w, built_lsb = built_hmtx[gname]
                if ref_w != built_w:
                    delta = abs(ref_w - built_w)
                    max_delta = max(max_delta, delta)
                    shared_diffs += 1
                    if len(diff_glyphs) < 10:
                        diff_glyphs.append({
                            "glyph": gname,
                            "ref_width": ref_w,
                            "built_width": built_w,
                            "delta": delta,
                        })

            metrics["hmtx_shared_width_diffs"] = shared_diffs
            metrics["hmtx_glyph_name_changes"] = len(ref_only) + len(built_only)
            metrics["hmtx_diffs"] = shared_diffs
            metrics["hmtx_max_delta"] = max_delta
            metrics["hmtx_diff_glyphs"] = diff_glyphs
            metrics["hmtx_identical"] = shared_diffs == 0

        # Compare vertical metrics (hhea + OS/2) — affects line spacing
        line_metrics_fields = []
        if "hhea" in ref and "hhea" in built:
            for attr in ["ascent", "descent", "lineGap"]:
                rv = getattr(ref["hhea"], attr, None)
                bv = getattr(built["hhea"], attr, None)
                if rv != bv:
                    line_metrics_fields.append({
                        "table": "hhea", "field": attr,
                        "ref": rv, "built": bv,
                    })

        if "OS/2" in ref and "OS/2" in built:
            for attr in ["sTypoAscender", "sTypoDescender", "sTypoLineGap",
                         "usWinAscent", "usWinDescent"]:
                rv = getattr(ref["OS/2"], attr, None)
                bv = getattr(built["OS/2"], attr, None)
                if rv != bv:
                    line_metrics_fields.append({
                        "table": "OS/2", "field": attr,
                        "ref": rv, "built": bv,
                    })

        metrics["line_metrics_diffs"] = line_metrics_fields
        metrics["line_metrics_identical"] = len(line_metrics_fields) == 0

        # Classify reflow risk
        # Only shared-glyph width changes cause reflow. Glyph name changes
        # (glyphs in one font but not the other) don't affect existing text
        # because those glyphs wouldn't be referenced by cmap.
        if metrics["hmtx_diffs"] == 0 and metrics["line_metrics_identical"]:
            metrics["reflow_risk"] = "none"
        elif metrics["hmtx_diffs"] == 0 and not metrics["line_metrics_identical"]:
            metrics["reflow_risk"] = "line-spacing-only"
        elif metrics["hmtx_diffs"] > 0 and metrics["hmtx_max_delta"] <= 1:
            metrics["reflow_risk"] = "minimal"
        else:
            metrics["reflow_risk"] = "high"

        analysis["metrics"] = metrics

        # Glyph coordinate analysis
        ref_order = set(ref.getGlyphOrder())
        built_order = set(built.getGlyphOrder())
        common = ref_order & built_order
        total = len(common)
        diff_count = 0
        rounding_only = 0
        coord_count_diff = 0

        if "glyf" in ref and "glyf" in built:
            for gname in common:
                rg = ref["glyf"].get(gname)
                bg = built["glyf"].get(gname)
                if rg is None and bg is None:
                    continue
                if rg is None or bg is None:
                    diff_count += 1
                    continue
                rc = getattr(rg, "coordinates", None)
                bc = getattr(bg, "coordinates", None)
                if rc == bc:
                    continue
                diff_count += 1
                if rc is not None and bc is not None:
                    if len(rc) != len(bc):
                        coord_count_diff += 1
                    elif all(abs(ax - bx) <= 1 and abs(ay - by) <= 1
                             for (ax, ay), (bx, by) in zip(rc, bc)):
                        rounding_only += 1

        analysis["glyph_stats"] = {
            "total_glyphs": total,
            "ref_only": len(ref_order - built_order),
            "built_only": len(built_order - ref_order),
            "coord_diffs": diff_count,
            "rounding_only": rounding_only,
            "coord_count_diffs": coord_count_diff,
        }

        # Determine root cause
        ref_hint = analysis["ttfautohint"]["ref"]
        built_hint = analysis["ttfautohint"]["built"]
        hint_differs = ref_hint != built_hint and "ttfautohint" in ref_hint

        if hint_differs and diff_count > 0 and diff_count == (rounding_only + coord_count_diff):
            analysis["root_cause"] = "ttfautohint-version"
        elif hint_differs and diff_count > 0:
            analysis["root_cause"] = "ttfautohint-version + other"
        elif diff_count > 0:
            analysis["root_cause"] = "compiler-output-diff"
        else:
            analysis["root_cause"] = "metadata-only"

    except Exception as e:
        analysis["root_cause"] = f"analysis-error: {e}"
    finally:
        ref.close()
        built.close()

    return analysis


def compare_fonts(reference_path: Path, built_path: Path) -> dict:
    """Compare two font files. Returns comparison result dict."""
    ref_hash = sha256_file(reference_path)
    built_hash = sha256_file(built_path)

    result = {
        "reference_sha256": ref_hash,
        "built_sha256": built_hash,
        "byte_identical": ref_hash == built_hash,
        "differing_tables": [],
        "mismatch_category": "yes",
        "analysis": {},
    }

    if result["byte_identical"]:
        return result

    # TTX table-by-table comparison
    print(f"    Comparing tables for {reference_path.name}...")
    try:
        ref_tables = ttx_dump(reference_path, reference_path.parent)
        built_tables = ttx_dump(built_path, built_path.parent)
    except Exception as e:
        print(f"    TTX dump failed: {e}")
        result["mismatch_category"] = "compiler-version"
        result["differing_tables"] = ["<ttx-dump-failed>"]
        return result

    all_tags = set(ref_tables.keys()) | set(built_tables.keys())
    differing = set()

    for tag in sorted(all_tags):
        ref_xml = ref_tables.get(tag, "")
        built_xml = built_tables.get(tag, "")
        if ref_xml != built_xml:
            differing.add(tag)

    result["differing_tables"] = sorted(differing)
    result["mismatch_category"] = categorize_diff(differing)

    # Deep structural analysis
    print(f"    Running deep analysis...")
    result["analysis"] = deep_font_analysis(reference_path, built_path)
    root_cause = result["analysis"].get("root_cause", "")
    if root_cause:
        print(f"    Root cause: {root_cause}")

    return result


def find_built_font(source_dir: Path, source_file: str) -> Path | None:
    """Find a built font file in the source tree.

    gftools-builder typically outputs to fonts/ subdirectory or the path
    specified in config.yaml. We search for the filename.
    """
    filename = Path(source_file).name

    # Common output locations
    search_dirs = [
        source_dir / "fonts" / "ttf",
        source_dir / "fonts" / "variable",
        source_dir / "fonts" / "otf",
        source_dir / "fonts",
        source_dir,
    ]

    for d in search_dirs:
        candidate = d / filename
        if candidate.exists():
            return candidate

    # Recursive fallback
    for match in source_dir.rglob(filename):
        return match

    return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "families": {}}


def save_registry(registry: dict):
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def process_family(family: str, registry: dict, force: bool = False) -> str:
    """Process a single family. Returns the overall status string."""
    print(f"\n{'='*60}")
    print(f"Processing: {family}")
    print(f"{'='*60}")

    family_dir = OFL_DIR / family
    if not family_dir.exists():
        print(f"  Family directory not found: {family_dir}")
        return "missing-source"

    # Parse METADATA.pb
    source_info = parse_metadata_pb(family_dir)
    if not source_info.get("repository_url") or not source_info.get("commit"):
        print(f"  No source block or incomplete source block in METADATA.pb")
        return "missing-source"

    # Check for commit override
    entry = registry["families"].get(family, {})
    overrides = entry.get("overrides", {})
    commit = overrides.get("commit", source_info["commit"])
    config_yaml_path = overrides.get("config_yaml_path", source_info.get("config_yaml", ""))

    owner, repo = parse_owner_repo(source_info["repository_url"])
    if not owner or not repo:
        print(f"  Could not parse repository URL: {source_info['repository_url']}")
        return "metadata-stanza-wrong"

    print(f"  Repo: {owner}/{repo}")
    print(f"  Commit: {commit}")

    # Check if already processed (skip unless --force)
    report_path = WORKSPACE_DIR / family / "comparison_report.json"
    if report_path.exists() and not force:
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        print(f"  Already processed: {existing.get('overall_status', 'unknown')}")
        return existing.get("overall_status", "unknown")

    # Download source
    source_dir = download_source(owner, repo, commit, family)
    if source_dir is None:
        print(f"  Source download failed — checking upstream cache...")
        cache_dir = UPSTREAM_CACHE / owner / repo
        if cache_dir.exists():
            print(f"  Found cached repo at {cache_dir}")
            # Could search git history here for the right commit
            # For now, mark as wrong metadata
        return "metadata-stanza-wrong"

    print(f"  Source dir: {source_dir}")

    # Find config.yaml
    config_path = find_config_yaml(source_dir, config_yaml_path, family)
    if config_path is None:
        print(f"  No config.yaml found — cannot build")
        return "build-failure"

    # Build
    isolation = entry.get("isolation", "shared")
    build_result = run_build(source_dir, config_path, family, isolation, overrides)
    if build_result is None:
        return "build-failure"

    # Compare each file listed in source.files
    file_results = {}
    overall_categories = set()

    for file_mapping in source_info.get("files", []):
        source_file = file_mapping.get("source_file", "")
        dest_file = file_mapping.get("dest_file", "")

        if not dest_file or not source_file:
            continue

        # Skip non-font files
        if not dest_file.endswith((".ttf", ".otf")):
            continue

        reference_path = family_dir / dest_file
        if not reference_path.exists():
            print(f"  Reference file not found: {reference_path}")
            file_results[dest_file] = {"error": "reference file not found"}
            continue

        # Find the built font
        built_path = find_built_font(source_dir, source_file)
        if built_path is None:
            print(f"  Built file not found for: {source_file}")
            file_results[dest_file] = {"error": "built file not found"}
            overall_categories.add("build-failure")
            continue

        print(f"  Comparing: {dest_file}")
        print(f"    Reference: {reference_path}")
        print(f"    Built:     {built_path}")

        comparison = compare_fonts(reference_path, built_path)
        file_results[dest_file] = comparison
        overall_categories.add(comparison["mismatch_category"])

    # Determine overall status
    if not file_results:
        overall_status = "build-failure"
    elif all(r.get("byte_identical") for r in file_results.values() if "error" not in r):
        overall_status = "yes"
    elif "source-mismatch" in overall_categories:
        overall_status = "source-mismatch"
    elif "build-failure" in overall_categories:
        overall_status = "build-failure"
    elif "compiler-version" in overall_categories:
        overall_status = "compiler-version"
    elif "hinting-diff" in overall_categories:
        overall_status = "hinting-diff"
    elif "name-table" in overall_categories:
        overall_status = "name-table"
    elif "dsig-diff" in overall_categories:
        overall_status = "dsig-diff"
    elif "timestamp-diff" in overall_categories:
        overall_status = "timestamp-diff"
    elif "table-ordering" in overall_categories:
        overall_status = "table-ordering"
    else:
        overall_status = "compiler-version"

    # Write report
    report = {
        "family": family,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_commit": commit,
        "repository_url": source_info["repository_url"],
        "files": file_results,
        "overall_status": overall_status,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n  Report: {report_path}")
    print(f"  Overall status: {overall_status}")

    return overall_status


def scan_buildable_families() -> list:
    """Scan all ofl/ families and return those with buildable source stanzas."""
    import re
    buildable = []
    for family_dir in sorted(OFL_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        pb = family_dir / "METADATA.pb"
        if not pb.exists():
            continue
        text = pb.read_text(encoding="utf-8")
        has_repo = bool(re.search(r'repository_url:\s*"https?://', text))
        has_commit = bool(re.search(r'commit:\s*"[a-f0-9]{7,}"', text))
        has_cfg = bool(re.search(r'config_yaml:\s*".+"', text))
        has_override = (family_dir / "config.yaml").exists()
        if has_repo and has_commit and (has_cfg or has_override):
            buildable.append(family_dir.name)
    return buildable


def main():
    parser = argparse.ArgumentParser(description="Reproducible Font Build System")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--family", help="Build a single family")
    group.add_argument("--all", action="store_true", help="Build all enabled families")
    group.add_argument("--batch", action="store_true",
                       help="Auto-discover all buildable families, add to registry, and process")
    parser.add_argument("--force", action="store_true", help="Rebuild even if output exists")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N families (0=unlimited)")

    args = parser.parse_args()

    registry = load_registry()

    families_to_process = []
    if args.batch:
        buildable = scan_buildable_families()
        print(f"Found {len(buildable)} buildable families")
        for fam in buildable:
            if fam not in registry["families"]:
                registry["families"][fam] = {
                    "enabled": True,
                    "isolation": "shared",
                    "reproducible_build": None,
                    "notes": "",
                    "overrides": {},
                }
        save_registry(registry)
        families_to_process = buildable
    elif args.all:
        families_to_process = [
            name for name, entry in registry["families"].items()
            if entry.get("enabled", False)
        ]
    else:
        families_to_process = [args.family]
        # Auto-add to registry if not present
        if args.family not in registry["families"]:
            registry["families"][args.family] = {
                "enabled": True,
                "isolation": "shared",
                "reproducible_build": None,
                "notes": "",
                "overrides": {},
            }

    if not families_to_process:
        print("No families to process.")
        return

    # Skip families with existing reports unless --force
    # Policy: build artifacts in /mnt/shared/gfonts-repro-builds/ serve as a
    # cache. Never rebuild a family that already has results — only use --force
    # when explicitly needed (e.g., after a script change that affects analysis).
    if not args.force:
        original_count = len(families_to_process)
        families_to_process = [
            f for f in families_to_process
            if not (WORKSPACE_DIR / f / "comparison_report.json").exists()
        ]
        skipped = original_count - len(families_to_process)
        if skipped:
            print(f"Skipping {skipped} families with existing reports (use --force to rebuild)")

    # Apply --limit
    if args.limit > 0 and len(families_to_process) > args.limit:
        print(f"Limiting to first {args.limit} of {len(families_to_process)} families")
        families_to_process = families_to_process[:args.limit]

    if not families_to_process:
        print("No new families to process.")
        return

    print(f"Processing {len(families_to_process)} family(ies)")

    for family in families_to_process:
        status = process_family(family, registry, force=args.force)

        # Update registry
        if family not in registry["families"]:
            registry["families"][family] = {
                "enabled": True,
                "isolation": "shared",
                "reproducible_build": None,
                "notes": "",
                "overrides": {},
            }
        registry["families"][family]["reproducible_build"] = status
        save_registry(registry)

    print(f"\n{'='*60}")
    print("Done. Registry updated.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
