"""Agent-facing Knowledge Base write tool for Myah.

The dashboard wiki API already exposes ``/wiki/import`` for platform callers, but
LLM agents do not reliably discover or authenticate against that HTTP route.
Without a native tool they tend to call the generic ``write_file`` tool with
wiki-relative paths such as ``tests/agent-write/foo.md``. In the hosted agent
container those relative paths resolve under ``/root`` instead of ``WIKI_PATH``,
which creates false-positive readbacks: the agent can read its own file, while
the Knowledge Base tree remains unchanged.

This module provides a small, path-sandboxed tool that writes markdown files
straight into the configured Knowledge Base root and a guard that blocks the
known false-positive generic write pattern.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from myah_hermes_plugin.myah_admin.dashboard._wiki_paths import (
    MAX_IMPORT_FILES,
    MAX_IMPORT_TOTAL_BYTES,
    MAX_READ_BYTES,
    WikiPathError,
    markdown_only,
    normalized_import_relative_path,
    wiki_root,
)

def _normalized_import_relative_path(root: Path, target_dir: str, file_path: str):
    return normalized_import_relative_path(
        root,
        target_dir,
        file_path,
        markdown_error_label="writable",
    )

SCHEMA = {
    "name": "knowledge_base_write",
    "description": (
        "Write one or more markdown files into the Myah Knowledge Base/wiki. "
        "Use this instead of generic write_file when the user asks to create, "
        "update, or test Knowledge Base notes. Paths are wiki-relative and are "
        "safely sandboxed under WIKI_PATH."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "Markdown files to write into the Knowledge Base.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Wiki-relative markdown path, e.g. tests/agent-write/stories/story-map.md.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Complete markdown file content to write.",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "minItems": 1,
                "maxItems": MAX_IMPORT_FILES,
            },
            "target_dir": {
                "type": "string",
                "description": (
                    "Optional wiki-relative directory prepended to every file path. "
                    "Leave empty when file paths already include their full wiki-relative location."
                ),
                "default": "",
            },
        },
        "required": ["files"],
        "additionalProperties": False,
    },
}

_WIKI_RELATIVE_PREFIXES = (
    "tests/agent-write/",
    "concepts/",
    "entities/",
    "comparisons/",
    "queries/",
    "raw/",
    "Myah/",
    "MuckMuncher/",
    "Honcho/",
)
_WIKI_RELATIVE_FILES = {"SCHEMA.md", "index.md", "log.md"}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _safe_target_dir(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WikiPathError("target_dir must be a string")
    return value.strip().replace("\\", "/").strip("/")


def _looks_like_wiki_relative_path(raw_path: Any) -> bool:
    if not isinstance(raw_path, str):
        return False
    candidate = raw_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or candidate.startswith("./"):
        return False
    if candidate in _WIKI_RELATIVE_FILES:
        return True
    return any(candidate.startswith(prefix) for prefix in _WIKI_RELATIVE_PREFIXES)


def guard_relative_wiki_write(**kwargs: Any) -> dict[str, str] | None:
    """Block generic file writes that look like Knowledge Base-relative paths.

    ``write_file(path='tests/agent-write/foo.md')`` writes under the agent cwd
    (currently ``/root``), not under ``WIKI_PATH``. Blocking this pattern turns a
    silent false positive into an actionable correction the model can recover
    from by calling ``knowledge_base_write``.
    """

    tool_name = kwargs.get("tool_name")
    if tool_name not in {"write_file", "patch"}:
        return None
    args = kwargs.get("args") or {}
    if not isinstance(args, dict):
        return None
    path = args.get("path")
    if not _looks_like_wiki_relative_path(path):
        return None
    if not os.environ.get("WIKI_PATH"):
        return None
    return {
        "action": "block",
        "message": (
            "This looks like a Knowledge Base wiki-relative path. Generic "
            f"{tool_name} would write it under the agent working directory, not WIKI_PATH. "
            "Use the knowledge_base_write tool with the same wiki-relative path, "
            "or explicitly target the configured WIKI_PATH and verify via the Knowledge Base tree/graph."
        ),
    }


def handle(args: dict[str, Any], **_kwargs: Any) -> str:
    files = args.get("files")
    if not isinstance(files, list) or not files:
        return _json({"error": "files must be a non-empty list"})
    if len(files) > MAX_IMPORT_FILES:
        return _json({"error": "too many files", "limit": MAX_IMPORT_FILES})

    try:
        target_dir = _safe_target_dir(args.get("target_dir", ""))
    except WikiPathError as exc:
        return _json({"error": str(exc)})

    root = wiki_root()
    root.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    prepared: list[tuple[Path, str, str]] = []
    seen: set[str] = set()

    for item in files:
        if not isinstance(item, dict):
            return _json({"error": "each file must be an object"})
        file_path = item.get("path")
        content = item.get("content")
        if not isinstance(file_path, str) or not file_path.strip():
            return _json({"error": "file path is required"})
        if not isinstance(content, str):
            return _json({"error": f"content must be a string: {file_path}"})

        try:
            target, relative = _normalized_import_relative_path(root, target_dir, file_path)
        except WikiPathError as exc:
            return _json({"error": str(exc), "path": file_path})

        if relative in seen:
            return _json({"error": f"duplicate import path: {relative}"})
        seen.add(relative)

        size = len(content.encode("utf-8"))
        if size > MAX_READ_BYTES:
            return _json({"error": f"file too large: {relative}", "limit": MAX_READ_BYTES})
        total_bytes += size
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            return _json({"error": "import too large", "limit": MAX_IMPORT_TOTAL_BYTES})
        if not markdown_only(target):
            return _json({"error": "only markdown files are writable", "path": relative})

        prepared.append((target, relative, content))

    created: list[str] = []
    updated: list[str] = []
    try:
        for target, relative, content in prepared:
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if existed:
                updated.append(relative)
            else:
                created.append(relative)
    except OSError:
        return _json({"error": "failed to write Knowledge Base file"})

    return _json(
        {
            "success": True,
            "created": created,
            "updated": updated,
            "written": created + updated,
            "target_dir": target_dir,
        }
    )
