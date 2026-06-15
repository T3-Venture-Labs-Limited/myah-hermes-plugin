"""Path sandbox helpers for the myah-admin wiki surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os
import urllib.parse


MAX_READ_BYTES = 1_048_576
MAX_GRAPH_NODES = 1_000
MAX_GRAPH_EDGES = 5_000
MAX_IMPORT_FILES = 100
MAX_IMPORT_TOTAL_BYTES = 5 * 1_048_576


class WikiPathError(Exception):
    """Raised for any path safety violation. Message must NEVER contain absolute paths."""

    pass


def wiki_root() -> Path:
    """Returns WIKI_PATH env var as Path, or hermes_home_path() / 'wiki' as fallback."""
    configured = os.environ.get('WIKI_PATH')
    if configured:
        return Path(configured)

    from ._common import hermes_home_path

    return hermes_home_path() / 'wiki'


def resolve_wiki_path(
    requested: str,
    *,
    root: Optional[Path] = None,
    must_be_file: bool = True,
    max_bytes: Optional[int] = None,
) -> Path:
    """Resolve a user-provided relative path safely within the wiki root.

    Raises WikiPathError (with safe message, no absolute paths) on any violation.
    """
    base_root = root or wiki_root()
    decoded = urllib.parse.unquote(requested)

    if '\x00' in decoded:
        raise WikiPathError('invalid path')

    if decoded.startswith('/') or decoded.startswith('\\'):
        raise WikiPathError('absolute paths not allowed')

    parts = Path(decoded).parts
    if any(part == '..' for part in parts):
        raise WikiPathError('path traversal not allowed')

    if any(part.startswith('.') for part in parts):
        raise WikiPathError('hidden paths not allowed')

    resolved = Path(os.path.realpath(base_root / decoded))
    real_root = Path(os.path.realpath(base_root))

    if not _contained_by_root(resolved, real_root):
        raise WikiPathError('path outside wiki root')

    if must_be_file and resolved.is_dir():
        raise WikiPathError('path is a directory')

    if max_bytes and resolved.exists() and resolved.stat().st_size > max_bytes:
        raise WikiPathError('file too large')

    return resolved


def markdown_only(path: Path) -> bool:
    """True if path has .md or .markdown suffix (case-insensitive)."""
    return path.suffix.lower() in {'.md', '.markdown'}


def normalized_import_relative_path(
    root: Path,
    target_dir: str,
    file_path: str,
    *,
    markdown_error_label: str = 'importable',
) -> tuple[Path, str]:
    """Resolve a wiki import target and return ``(absolute_target, wiki_relative)``.

    Kept in this low-level module so agent-facing tools can reuse import
    validation without importing the dashboard router and triggering plugin
    registration cycles.
    """
    target = target_dir.strip().replace('\\', '/').strip('/')
    requested = file_path.strip().replace('\\', '/').lstrip('/')
    if not requested:
        raise WikiPathError('import path is required')
    combined = '/'.join(part for part in (target, requested) if part)
    resolved = resolve_wiki_path(combined, root=root, must_be_file=False)
    if resolved.exists() and resolved.is_dir():
        raise WikiPathError('import path is a directory')
    if not markdown_only(resolved):
        raise WikiPathError(f'only markdown files are {markdown_error_label}')
    relative = str(resolved.relative_to(Path(os.path.realpath(root)))).replace(os.sep, '/')
    return resolved, relative


@dataclass
class WikiTreeNode:
    path: str
    name: str
    type: str
    children: Optional[list['WikiTreeNode']] = field(default=None)
    title: Optional[str] = field(default=None)
    size: Optional[int] = field(default=None)
    mtime: Optional[str] = field(default=None)
    markdown: bool = False


def iter_wiki_markdown_tree(
    root: Path,
    *,
    max_depth: int = 8,
    max_nodes: int = 1000,
    include_hidden: bool = False,
) -> list[WikiTreeNode]:
    """Recursively walk wiki root, returning only markdown files/dirs.

    Safety: per-entry realpath containment check; no symlink-dir traversal;
    reject dot segments; deterministic sort; relative paths only.
    """
    root = Path(root)
    real_root = Path(os.path.realpath(root))
    count = 0

    if max_depth < 0 or max_nodes <= 0 or not root.exists() or not root.is_dir():
        return []

    def walk(directory: Path, depth: int) -> list[WikiTreeNode]:
        nonlocal count
        nodes: list[WikiTreeNode] = []

        for entry in sorted(_safe_iterdir(directory), key=_tree_sort_key):
            if count >= max_nodes:
                break

            if _has_hidden_segment(entry, root, include_hidden=include_hidden):
                continue

            real_entry = Path(os.path.realpath(entry))
            if entry.is_symlink() and real_entry.is_dir():
                continue

            if not _contained_by_root(real_entry, real_root):
                raise WikiPathError('path outside wiki root')

            if real_entry.is_dir():
                count += 1
                children = walk(entry, depth + 1) if depth + 1 < max_depth and count < max_nodes else []
                nodes.append(
                    WikiTreeNode(
                        path=str(entry.relative_to(root)),
                        name=entry.name,
                        type='directory',
                        children=children,
                    )
                )
                continue

            if not markdown_only(entry):
                continue

            try:
                stat = entry.stat()
            except OSError:
                continue

            count += 1
            nodes.append(
                WikiTreeNode(
                    path=str(entry.relative_to(root)),
                    name=entry.name,
                    type='file',
                    size=stat.st_size,
                    mtime=str(stat.st_mtime),
                    markdown=True,
                )
            )

        return nodes

    return walk(root, 0)


def _contained_by_root(path: Path, root: Path) -> bool:
    return path == root or str(path).startswith(str(root) + os.sep)


def _safe_iterdir(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _tree_sort_key(entry: Path) -> tuple[int, str]:
    try:
        is_directory = entry.is_dir()
    except OSError:
        is_directory = False
    return (0 if is_directory else 1, entry.name.lower())


def _has_hidden_segment(entry: Path, root: Path, *, include_hidden: bool) -> bool:
    try:
        parts = entry.relative_to(root).parts
    except ValueError:
        return True

    if any(part.startswith('.') for part in parts):
        return True


    return not include_hidden and entry.name.startswith('.')
