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

    # Check specific categories
    remaining = set(differing_tables)

    # Pure timestamp diff
    if remaining <= TIMESTAMP_TABLES:
        return "timestamp-diff"

    # DSIG only
    if remaining <= DSIG_TABLES:
        return "dsig-diff"

    # Hinting tables (possibly with timestamps)
    if remaining <= (HINTING_TABLES | TIMESTAMP_TABLES):
        return "hinting-diff"

    # Name table (possibly with timestamps)
    if remaining <= (NAME_TABLES | TIMESTAMP_TABLES):
        return "name-table"

    # Check for table ordering (all tables present but different serialization)
    # This is harder to detect without deeper analysis, so we flag broadly
    if remaining <= {"GlyphOrder", "loca", "glyf", "hmtx", "GPOS", "GSUB",
                     "cmap", "post", "OS/2"} | TIMESTAMP_TABLES:
        return "compiler-version"

    # If glyf or CFF tables differ, it's a source mismatch
    if "glyf" in remaining or "CFF " in remaining or "CFF2" in remaining:
        return "source-mismatch"

    return "compiler-version"


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


def main():
    parser = argparse.ArgumentParser(description="Reproducible Font Build System")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--family", help="Build a single family")
    group.add_argument("--all", action="store_true", help="Build all enabled families")
    parser.add_argument("--force", action="store_true", help="Rebuild even if output exists")

    args = parser.parse_args()

    registry = load_registry()

    families_to_process = []
    if args.all:
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

    print(f"Processing {len(families_to_process)} family(ies): {', '.join(families_to_process)}")

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
