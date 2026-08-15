#!/usr/bin/env python3

"""
Bidirectional license header management for forktex-core.

Scans all source files and can add, remove, or verify the AGPL-3.0 + Commercial
dual-license header. Idempotent -- always produces the same result regardless
of current state.

Usage:
    python scripts/license_headers.py check    # exit 1 if any file is missing/outdated
    python scripts/license_headers.py fix      # add or update headers everywhere
    python scripts/license_headers.py strip     # remove headers from all files
"""

import os
import re
import sys

# ── Configuration ────────────────────────────────────────────────────────────

COPYRIGHT_HOLDER = "FORKTEX S.R.L."
COPYRIGHT_YEAR = "2026"
CONTACT_EMAIL = "info@forktex.com"

HEADER_LINES = [
    f"Copyright (C) {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}",
    "",
    "SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial",
    "",
    "This file is part of forktex-core.",
    "",
    "For commercial licensing -- including use in proprietary products, SaaS",
    "deployments, or any context where AGPL obligations cannot be met -- you",
    f"MUST obtain a commercial license from {COPYRIGHT_HOLDER} ({CONTACT_EMAIL}).",
    "",
    "This program is free software: you can redistribute it and/or modify",
    "it under the terms of the GNU Affero General Public License as published by",
    "the Free Software Foundation, either version 3 of the License, or",
    "(at your option) any later version.",
    "",
    "This program is distributed in the hope that it will be useful,",
    "but WITHOUT ANY WARRANTY; without even the implied warranty of",
    "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the",
    "GNU Affero General Public License for more details.",
    "",
    "You should have received a copy of the GNU Affero General Public License",
    "along with this program. If not, see <https://www.gnu.org/licenses/>.",
]

# Used to detect ANY version of our header (current or outdated)
SENTINEL = "SPDX-License-Identifier"
END_MARKER = "along with this program"

# This script contains SENTINEL as data; skip self to avoid false matches
SELF_PATH = os.path.abspath(__file__)
# Fallback end markers for older header versions
ALT_END_MARKERS = ["See the LICENSE and NOTICE files for full terms"]

# Comment styles per extension
HASH_EXTS = {".py", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".conf"}
SLASH_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
CSS_EXTS = {".css"}
HTML_EXTS = {".html"}

SOURCE_EXTS = HASH_EXTS | SLASH_EXTS | CSS_EXTS | HTML_EXTS

SKIP_DIRS = {
    "node_modules",
    ".venv",
    "__pycache__",
    ".expo",
    ".git",
    "api_client",
    ".next",
    "dist",
    "build",
    ".mypy_cache",
    "redis-data",
    "network-db-data",
    "minio-data",
}


# ── Header formatting ───────────────────────────────────────────────────────


def _comment_lines(prefix: str) -> list[str]:
    return [f"{prefix} {line}" if line else prefix for line in HEADER_LINES]


def make_header(ext: str) -> str:
    """Build the comment-wrapped header block for a given file extension."""
    if ext in HASH_EXTS:
        return "\n".join(_comment_lines("#")) + "\n\n"
    if ext in SLASH_EXTS:
        return "\n".join(_comment_lines("//")) + "\n\n"
    if ext in CSS_EXTS:
        inner = "\n".join(f" * {line}" if line else " *" for line in HEADER_LINES)
        return f"/*\n{inner}\n */\n\n"
    if ext in HTML_EXTS:
        inner = "\n".join(f"  {line}" for line in HEADER_LINES)
        return f"<!--\n{inner}\n-->\n\n"
    return ""


def has_current_header(content: str) -> bool:
    """Check if the file has the CURRENT version of the header."""
    head = "\n".join(content.split("\n")[:40])
    return SENTINEL in head and END_MARKER in head


def has_any_header(content: str) -> bool:
    """Check if the file has ANY version of our header (current or outdated)."""
    head = "\n".join(content.split("\n")[:40])
    if SENTINEL not in head:
        return False
    for marker in [END_MARKER] + ALT_END_MARKERS:
        if marker in head:
            return True
    return False


# ── Strip logic ──────────────────────────────────────────────────────────────


def strip_header(filepath: str) -> bool:
    """Remove the license header from a file. Returns True if modified."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError, PermissionError:
        return False

    if not has_any_header(content):
        return False

    lines = content.split("\n")
    ext = os.path.splitext(filepath)[1]

    # Find header boundaries
    start = None
    end = None
    shebang = None

    # Handle shebang
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        search_lines = lines[1:]
        offset = 1
    else:
        search_lines = lines
        offset = 0

    # Find the first line with SENTINEL
    for i, line in enumerate(search_lines):
        if SENTINEL in line:
            # Walk backward to find the start of the comment block
            start = i
            while start > 0:
                prev = search_lines[start - 1].strip()
                # Stop at non-comment, non-blank lines
                if ext in HASH_EXTS and not prev.startswith("#") and prev != "":
                    break
                if ext in SLASH_EXTS and not prev.startswith("//") and prev != "":
                    break
                if ext in CSS_EXTS and prev == "/*":
                    start -= 1
                    break
                if ext in HTML_EXTS and prev == "<!--":
                    start -= 1
                    break
                if prev == "":
                    start -= 1
                    continue
                start -= 1
            break

    if start is None:
        return False

    # Find the end: look for end markers
    all_markers = [END_MARKER] + ALT_END_MARKERS
    for i in range(start, min(len(search_lines), start + 40)):
        line = search_lines[i]
        if any(m in line for m in all_markers):
            end = i
            break

    if end is None:
        return False

    # Include closing comment markers (CSS: " */", HTML: "-->")
    if end + 1 < len(search_lines):
        next_stripped = search_lines[end + 1].strip()
        if next_stripped in ("*/", "-->"):
            end += 1

    # Skip trailing blank lines after the header
    while end + 1 < len(search_lines) and search_lines[end + 1].strip() == "":
        end += 1

    # Reconstruct
    before = search_lines[:start]
    after = search_lines[end + 1 :]

    # Clean leading blank lines from 'before'
    while before and before[0].strip() == "":
        before.pop(0)

    if shebang is not None:
        new_lines = [shebang]
        if after:
            new_lines.append("")
        new_lines.extend(before + after)
    else:
        new_lines = before + after

    new_content = "\n".join(new_lines)
    if new_content != content:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    return False


# ── Add logic ────────────────────────────────────────────────────────────────


def add_header(filepath: str) -> bool:
    """Add the license header to a file. Returns True if modified."""
    ext = os.path.splitext(filepath)[1]
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError, PermissionError:
        return False

    if has_current_header(content):
        return False

    header = make_header(ext)
    if not header:
        return False

    if content.startswith("#!"):
        first_newline = content.index("\n") + 1
        shebang = content[:first_newline]
        rest = content[first_newline:].lstrip("\n")
        new_content = shebang + "\n" + header + rest
    else:
        new_content = header + content

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


# ── File collection ──────────────────────────────────────────────────────────


def collect_files(root: str) -> list[str]:
    """Walk the tree and collect all source files to process."""
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1]
            fullpath = os.path.join(dirpath, fname)
            if ext in SOURCE_EXTS and os.path.abspath(fullpath) != SELF_PATH:
                result.append(fullpath)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    commands = ("check", "fix", "strip")
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"Usage: python scripts/license_headers.py [{'/'.join(commands)}]")
        sys.exit(1)

    mode = sys.argv[1]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = collect_files(root)

    if mode == "strip":
        removed = 0
        for filepath in files:
            if strip_header(filepath):
                print(f"  - {os.path.relpath(filepath, root)}")
                removed += 1
        if removed:
            print(f"\n  Stripped headers from {removed} file(s).")
        else:
            print(f"  No headers found in {len(files)} file(s).")
        return

    if mode == "fix":
        # First strip any outdated headers, then add current ones
        updated = 0
        for filepath in files:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError, PermissionError:
                continue

            changed = False
            # Strip outdated header if present but not current
            if has_any_header(content) and not has_current_header(content):
                strip_header(filepath)
                changed = True

            # Add current header if missing
            if add_header(filepath):
                changed = True

            if changed:
                print(f"  + {os.path.relpath(filepath, root)}")
                updated += 1

        if updated:
            print(f"\n  Updated {updated} file(s).")
        else:
            print(f"  All {len(files)} source files already have current headers.")
        return

    if mode == "check":
        missing = []
        outdated = []
        for filepath in files:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError, PermissionError:
                continue
            if has_current_header(content):
                continue
            if has_any_header(content):
                outdated.append(filepath)
            else:
                missing.append(filepath)

        if missing or outdated:
            total = len(missing) + len(outdated)
            print(f"  {total} file(s) need attention:\n")
            if missing:
                print(f"  Missing header ({len(missing)}):")
                for f in missing:
                    print(f"    {os.path.relpath(f, root)}")
            if outdated:
                print(f"\n  Outdated header ({len(outdated)}):")
                for f in outdated:
                    print(f"    {os.path.relpath(f, root)}")
            print(f"\n  Run 'make license-fix' to fix automatically.")
            sys.exit(1)
        else:
            print(f"  All {len(files)} source files have current license headers.")
            sys.exit(0)


if __name__ == "__main__":
    main()
