"""Agent-facing Knowledge Base tool for Myah.

The dashboard wiki API exposes Knowledge Base routes for platform callers, but
LLM agents do not reliably discover or authenticate against that HTTP surface.
Without a native tool they tend to call the generic ``write_file`` tool with
wiki-relative paths such as ``tests/agent-write/foo.md``. In hosted containers
those relative paths resolve under the agent working directory, not
``WIKI_PATH``, creating false-positive readbacks: the agent can read its own
file while the Knowledge Base tree remains unchanged.

This module provides a small, path-sandboxed agent tool that reads, lists, and
writes markdown files under the configured Knowledge Base root. It also exposes
a pre-tool-call guard that blocks the known false-positive generic write
pattern and tells the model which tool to use instead.
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
    resolve_wiki_path,
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
    "name": "knowledge_base",
    "description": (
        "Read, list, or write markdown files in the Myah Knowledge Base/wiki. "
        "Use this instead of generic write_file/read_file when the user asks to "
        "create, update, verify, or test Knowledge Base notes. Paths are "
        "wiki-relative and sandboxed under WIKI_PATH."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["write", "read", "list"],
                "description": "Operation to perform against the Knowledge Base.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Wiki-relative markdown file path for read/single write, or "
                    "wiki-relative directory for list. Example: "
                    "tests/agent-write/stories/story-map.md."
                ),
            },
            "content": {
                "type": "string",
                "description": "Complete markdown content for single-file writes.",
            },
            "files": {
                "type": "array",
                "description": (
                    "Optional batch of markdown files to write. Use this for "
                    "multi-file Knowledge Base tests."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Wiki-relative markdown file path.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Complete markdown content to write.",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "maxItems": MAX_IMPORT_FILES,
            },
            "target_dir": {
                "type": "string",
                "description": (
                    "Optional wiki-relative directory prepended to write paths. "
                    "Leave empty when paths already include their full "
                    "wiki-relative location."
                ),
                "default": "",
            },
        },
        "required": ["action"],
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
    """Block generic file writes that look like KB-relative paths.

    ``write_file(path='tests/agent-write/foo.md')`` writes under the agent cwd,
    not under ``WIKI_PATH``. Blocking this turns a silent false positive into an
    actionable correction the model can recover from by calling
    ``knowledge_base``.
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
            "Use the knowledge_base tool with action='write' and the same "
            "wiki-relative path, then verify with knowledge_base action='read' "
            "or action='list'."
        ),
    }


def _write(args: dict[str, Any]) -> str:
    raw_files = args.get("files")
    if raw_files is None:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path.strip():
            return _json({"error": "path is required for write"})
        if not isinstance(content, str):
            return _json({"error": "content is required for write"})
        raw_files = [{"path": path, "content": content}]

    if not isinstance(raw_files, list) or not raw_files:
        return _json({"error": "files must be a non-empty list"})
    if len(raw_files) > MAX_IMPORT_FILES:
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

    for item in raw_files:
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
            "error": None,
            "success": True,
            "created": created,
            "updated": updated,
            "written": created + updated,
            "target_dir": target_dir,
        }
    )


def _read(args: dict[str, Any]) -> str:
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _json({"error": "path is required for read"})
    root = wiki_root()
    try:
        target = resolve_wiki_path(raw_path, root=root, must_be_file=True, max_bytes=MAX_READ_BYTES)
    except WikiPathError as exc:
        return _json({"error": str(exc), "path": raw_path})
    if not markdown_only(target):
        return _json({"error": "only markdown files are readable", "path": raw_path})
    if not target.exists():
        return _json({"error": "file not found", "path": raw_path})
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return _json({"error": "file not found", "path": raw_path})
    relative = str(target.relative_to(Path(os.path.realpath(root)))).replace(os.sep, "/")
    return _json({"error": None, "success": True, "path": relative, "content": content})


def _list(args: dict[str, Any]) -> str:
    raw_path = args.get("path") or ""
    if not isinstance(raw_path, str):
        return _json({"error": "path must be a string"})
    root = wiki_root()
    try:
        target = resolve_wiki_path(raw_path or ".", root=root, must_be_file=False)
    except WikiPathError as exc:
        return _json({"error": str(exc), "path": raw_path})
    if not target.exists():
        return _json({"error": "directory not found", "path": raw_path})
    if not target.is_dir():
        return _json({"error": "path is not a directory", "path": raw_path})

    real_root = Path(os.path.realpath(root))
    files: list[str] = []
    for child in sorted(target.rglob("*")):
        try:
            real_child = Path(os.path.realpath(child))
        except OSError:
            continue
        if not (real_child == real_root or str(real_child).startswith(str(real_root) + os.sep)):
            continue
        if child.is_file() and markdown_only(child):
            files.append(str(child.relative_to(root)).replace(os.sep, "/"))
    rel_root = "" if target == root else str(target.relative_to(root)).replace(os.sep, "/")
    return _json({"error": None, "success": True, "root": rel_root, "files": files})


def handle(args: dict[str, Any], **_kwargs: Any) -> str:
    action = args.get("action")
    if action == "write":
        return _write(args)
    if action == "read":
        return _read(args)
    if action == "list":
        return _list(args)
    return _json({"error": "action must be one of: write, read, list"})
