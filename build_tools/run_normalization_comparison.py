#!/usr/bin/env python3
"""Run font normalizer across all compiler-version/timestamp-diff/name-table families
and compare normalized reference vs built fonts byte-for-byte.

Usage:
    /mnt/shared/gftools/venv/bin/python3 run_normalization_comparison.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PYTHON = "/mnt/shared/gftools/venv/bin/python3"
NORMALIZER = "/mnt/shared/google/fonts/build_tools/font_normalize.py"
REGISTRY = "/mnt/shared/google/fonts/build_tools/build_registry.json"
OFL_DIR = Path("/mnt/shared/google/fonts/ofl")
REPRO_DIR = Path("/mnt/shared/gfonts-repro-builds")
OUTPUT = "/mnt/shared/google/fonts/build_tools/normalization_results.json"
TARGET_STATUSES = {"compiler-version", "timestamp-diff", "name-table"}


def find_built_font(family_dir: Path, filename: str) -> Path | None:
    """Find the built font matching filename in the repro-builds family dir.
    Excludes venv directories. Returns the first match or None."""
    if not family_dir.is_dir():
        return None
    for root, dirs, files in os.walk(family_dir):
        # Skip venv directories
        dirs[:] = [d for d in dirs if d != "venv"]
        if filename in files:
            return Path(root) / filename
    return None


def normalize_font(input_path: str, output_path: str) -> bool:
    """Run the normalizer via subprocess. Returns True on success."""
    result = subprocess.run(
        [PYTHON, NORMALIZER, input_path, output_path],
        capture_output=True,
        timeout=120,
    )
    return result.returncode == 0


def compare_files(path_a: str, path_b: str) -> tuple[bool, int]:
    """Compare two files byte-for-byte. Returns (match, diff_bytes).
    diff_bytes is the count of differing bytes (up to the shorter file length),
    plus any length difference."""
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        data_a = fa.read()
        data_b = fb.read()

    if data_a == data_b:
        return True, 0

    # Count differing bytes
    min_len = min(len(data_a), len(data_b))
    diff_count = sum(1 for i in range(min_len) if data_a[i] != data_b[i])
    diff_count += abs(len(data_a) - len(data_b))
    return False, diff_count


def drop_caches():
    """Drop filesystem caches to avoid memory pressure."""
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/local/sbin/drop-caches"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def main():
    with open(REGISTRY) as f:
        registry = json.load(f)

    families = registry.get("families", {})

    # Filter to target statuses
    target_families = sorted(
        k for k, v in families.items()
        if isinstance(v, dict) and v.get("reproducible_build") in TARGET_STATUSES
    )

    print(f"Found {len(target_families)} families to test", flush=True)
    print(f"  compiler-version: {sum(1 for k in target_families if families[k]['reproducible_build'] == 'compiler-version')}", flush=True)
    print(f"  timestamp-diff: {sum(1 for k in target_families if families[k]['reproducible_build'] == 'timestamp-diff')}", flush=True)
    print(f"  name-table: {sum(1 for k in target_families if families[k]['reproducible_build'] == 'name-table')}", flush=True)

    results = {
        "total_tested": 0,
        "normalized_match": 0,
        "normalized_differ": 0,
        "errors": 0,
        "matches": [],
        "close_matches": [],
        "per_family": {},
    }

    start_time = time.time()

    for idx, family in enumerate(target_families):
        if (idx + 1) % 50 == 0:
            elapsed = time.time() - start_time
            rate = (idx + 1) / elapsed if elapsed > 0 else 0
            eta = (len(target_families) - idx - 1) / rate if rate > 0 else 0
            print(f"[{idx + 1}/{len(target_families)}] "
                  f"matches={results['normalized_match']} "
                  f"differs={results['normalized_differ']} "
                  f"errors={results['errors']} "
                  f"elapsed={elapsed:.0f}s ETA={eta:.0f}s", flush=True)
            drop_caches()

        ofl_family_dir = OFL_DIR / family
        repro_family_dir = REPRO_DIR / family

        if not ofl_family_dir.is_dir():
            results["errors"] += 1
            results["per_family"][family] = {"status": "error", "reason": "no ofl dir"}
            continue

        ttf_files = list(ofl_family_dir.glob("*.ttf"))
        if not ttf_files:
            results["errors"] += 1
            results["per_family"][family] = {"status": "error", "reason": "no ttf files"}
            continue

        family_all_match = True
        family_max_diff = 0
        files_tested = 0
        family_error = False
        file_details = []

        for ttf in ttf_files:
            built = find_built_font(repro_family_dir, ttf.name)
            if built is None:
                # Try without exact name match - skip this file
                continue

            # Create temp files for normalized output
            norm_ref = f"/tmp/norm_ref_{os.getpid()}.ttf"
            norm_built = f"/tmp/norm_built_{os.getpid()}.ttf"

            try:
                ok_ref = normalize_font(str(ttf), norm_ref)
                if not ok_ref:
                    family_error = True
                    file_details.append({"file": ttf.name, "status": "norm_error_ref"})
                    continue

                ok_built = normalize_font(str(built), norm_built)
                if not ok_built:
                    family_error = True
                    file_details.append({"file": ttf.name, "status": "norm_error_built"})
                    continue

                match, diff_bytes = compare_files(norm_ref, norm_built)
                files_tested += 1

                if match:
                    file_details.append({"file": ttf.name, "status": "match"})
                else:
                    family_all_match = False
                    family_max_diff = max(family_max_diff, diff_bytes)
                    file_details.append({
                        "file": ttf.name,
                        "status": "differ",
                        "diff_bytes": diff_bytes,
                    })
            except Exception as e:
                family_error = True
                file_details.append({"file": ttf.name, "status": "exception", "error": str(e)})
            finally:
                # Clean up temp files
                for tmp in (norm_ref, norm_built):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        if files_tested == 0:
            if family_error:
                results["errors"] += 1
                results["per_family"][family] = {
                    "status": "error",
                    "reason": "all normalizations failed",
                    "files": file_details,
                }
            else:
                results["errors"] += 1
                results["per_family"][family] = {
                    "status": "error",
                    "reason": "no built fonts found",
                }
            continue

        results["total_tested"] += 1

        if family_all_match and not family_error:
            results["normalized_match"] += 1
            results["matches"].append(family)
            results["per_family"][family] = {
                "status": "match",
                "files_tested": files_tested,
            }
        else:
            results["normalized_differ"] += 1
            results["per_family"][family] = {
                "status": "differ",
                "files_tested": files_tested,
                "max_diff_bytes": family_max_diff,
                "files": file_details,
            }
            if family_max_diff > 0 and family_max_diff <= 1024:
                results["close_matches"].append({
                    "family": family,
                    "max_diff_bytes": family_max_diff,
                })

    # Sort close matches by diff size
    results["close_matches"].sort(key=lambda x: x["max_diff_bytes"])

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.0f}s")
    print(f"  Total tested: {results['total_tested']}")
    print(f"  Normalized match: {results['normalized_match']}")
    print(f"  Normalized differ: {results['normalized_differ']}")
    print(f"  Errors: {results['errors']}")
    print(f"  Close matches (<=1KB diff): {len(results['close_matches'])}")

    with open(OUTPUT, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {OUTPUT}")


if __name__ == "__main__":
    main()
