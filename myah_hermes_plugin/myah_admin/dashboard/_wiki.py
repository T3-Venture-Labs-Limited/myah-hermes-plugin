"""Read-only wiki endpoints for the myah-admin plugin (T3-1034).

Routes:

    GET /wiki/status   — provisioning state of the backbone (SCHEMA.md, index.md, log.md)
    GET /wiki/tree     — markdown-only directory tree under ``WIKI_PATH``
    GET /wiki/file     — frontmatter + body + mtime + etag for a single .md file
    GET /wiki/search   — case-insensitive substring search with snippets

All endpoints are sandboxed: no response body ever contains the absolute
filesystem path of the wiki root. Path-resolution helpers live in
``_wiki_paths``; path-safety violations surface as :class:`WikiPathError`
and are mapped to HTTP 400 with the exception's safe message (never a
stack trace, never an absolute path).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ._common import require_session_token
from ._wiki_paths import (
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_IMPORT_FILES,
    MAX_IMPORT_TOTAL_BYTES,
    MAX_READ_BYTES,
    WikiPathError,
    WikiTreeNode,
    iter_wiki_markdown_tree,
    markdown_only,
    normalized_import_relative_path,
    resolve_wiki_path,
    wiki_root,
)

logger = logging.getLogger(__name__)

router = APIRouter()

BACKBONE_FILES = ('SCHEMA.md', 'index.md', 'log.md')

_SNIPPET_RADIUS = 40
_FRONTMATTER_RE = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n', re.DOTALL)


@router.get('/wiki/status')
async def get_wiki_status(_auth: None = Depends(require_session_token)) -> dict:
    root = wiki_root()
    root_exists = root.exists() and root.is_dir()

    if root_exists:
        missing = [name for name in BACKBONE_FILES if not (root / name).is_file()]
    else:
        missing = list(BACKBONE_FILES)

    provisioned = root_exists and not missing
    bootstrap_available = (not root_exists) or bool(missing)

    return {
        'available': True,
        'root_exists': root_exists,
        'provisioned': provisioned,
        'bootstrap_available': bootstrap_available,
        'missing_backbone': missing,
        'readonly': False,
        'path_label': 'Hermes wiki',
        'limits': {
            'max_read_bytes': MAX_READ_BYTES,
            'max_graph_nodes': MAX_GRAPH_NODES,
            'max_graph_edges': MAX_GRAPH_EDGES,
            'max_import_files': MAX_IMPORT_FILES,
            'max_import_total_bytes': MAX_IMPORT_TOTAL_BYTES,
        },
    }


@router.get('/wiki/tree')
async def get_wiki_tree(
    includeHidden: bool = Query(default=False),
    maxDepth: int = Query(default=8, ge=0, le=32),
    _auth: None = Depends(require_session_token),
) -> dict:
    root = wiki_root()
    nodes = iter_wiki_markdown_tree(
        root,
        max_depth=maxDepth,
        include_hidden=includeHidden,
    )
    return {
        'root': {
            'type': 'directory',
            'path': '',
            'name': 'wiki',
            'children': [_node_to_dict(n) for n in nodes],
        }
    }


def _node_to_dict(node: WikiTreeNode) -> dict:
    payload: dict = {
        'type': node.type,
        'path': node.path,
        'name': node.name,
    }
    if node.type == 'directory':
        payload['children'] = [_node_to_dict(c) for c in (node.children or [])]
    else:
        if node.title is not None:
            payload['title'] = node.title
        if node.size is not None:
            payload['size'] = node.size
        if node.mtime is not None:
            payload['mtime'] = node.mtime
        payload['markdown'] = node.markdown
    return payload


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(loaded, dict):
        return {}, text
    return loaded, text[match.end() :]


def _derive_title(frontmatter: dict[str, Any], body: str, fallback: str) -> str:
    fm_title = frontmatter.get('title')
    if isinstance(fm_title, str) and fm_title.strip():
        return fm_title.strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('# '):
            return stripped[2:].strip()
    return fallback


def _iso_utc(stat_mtime: float) -> str:
    return _dt.datetime.fromtimestamp(stat_mtime, tz=_dt.timezone.utc).isoformat()


@router.get('/wiki/file')
async def read_wiki_file(
    path: str = Query(..., min_length=1),
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    try:
        resolved = resolve_wiki_path(
            path,
            root=wiki_root(),
            must_be_file=True,
            max_bytes=MAX_READ_BYTES,
        )
    except WikiPathError as exc:
        message = str(exc)
        if 'too large' in message:
            raise HTTPException(status_code=413, detail=message)
        raise HTTPException(status_code=400, detail=message)

    if not markdown_only(resolved):
        raise HTTPException(status_code=400, detail='only markdown files are readable')

    if not resolved.exists():
        raise HTTPException(status_code=404, detail='file not found')

    try:
        stat = resolved.stat()
        text = resolved.read_text(encoding='utf-8')
    except OSError:
        raise HTTPException(status_code=404, detail='file not found')

    frontmatter, body = _parse_frontmatter(text)
    relative = resolved.relative_to(Path(os.path.realpath(wiki_root())))

    return {
        'path': str(relative),
        'name': resolved.name,
        'title': _derive_title(frontmatter, body, resolved.stem),
        'content': text,
        'frontmatter': frontmatter,
        'mtime': _iso_utc(stat.st_mtime),
        'etag': f'W/"{int(stat.st_mtime_ns)}-{stat.st_size}"',
        'readonly': True,
    }


def _snippet_for(content: str, needle: str) -> tuple[str, int]:
    lower = content.lower()
    idx = lower.find(needle)
    if idx < 0:
        return '', 0
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(content), idx + len(needle) + _SNIPPET_RADIUS)
    snippet = content[start:end].replace('\n', ' ').strip()
    line_no = content.count('\n', 0, idx) + 1
    return snippet, line_no


def _entry_title(content: str, fallback: str) -> str:
    frontmatter, body = _parse_frontmatter(content)
    return _derive_title(frontmatter, body, fallback)


def _flatten_files(items: list[WikiTreeNode]) -> list[WikiTreeNode]:
    out: list[WikiTreeNode] = []
    for node in items:
        if node.type == 'file':
            out.append(node)
        elif node.type == 'directory' and node.children:
            out.extend(_flatten_files(node.children))
    return out


@router.get('/wiki/search')
async def search_wiki(
    q: str = Query(''),
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    query = q.strip().lower()
    if not query:
        return {'query': q, 'results': []}

    root = wiki_root()
    nodes = iter_wiki_markdown_tree(root)
    results: list[dict[str, Any]] = []

    for file_node in _flatten_files(nodes):
        rel_path = file_node.path
        try:
            resolved = resolve_wiki_path(rel_path, root=root, must_be_file=True)
            content = resolved.read_text(encoding='utf-8')
        except (WikiPathError, OSError) as e:
            logger.warning(f'skipping file during search: {rel_path}: {e}')
            continue

        name_lower = Path(rel_path).name.lower()
        content_lower = content.lower()

        if query in name_lower:
            score = 1.0
        elif query in content_lower:
            score = 0.5
        else:
            continue

        snippet, line = _snippet_for(content, query)
        results.append(
            {
                'path': rel_path,
                'title': _entry_title(content, Path(rel_path).stem),
                'score': score,
                'snippet': snippet,
                'line': line,
            }
        )

    results.sort(key=lambda r: (-r['score'], r['path']))
    return {'query': q, 'results': results}


# ── GET /wiki/graph ─────────────────────────────────────────────────────────

# Wikilink: [[target]] or [[target|alias]] — capture group is target portion.
_WIKILINK_RE = re.compile(r'\[\[([^\]\n]+?)\]\]')
# Relative markdown link: [label](href.md) — must end in .md/.markdown.
# Reject schemes (http://, mailto:, etc.) and anchors-only by matching only a path-like href.
_MD_LINK_RE = re.compile(
    r'\[(?P<label>[^\]\n]+)\]\((?P<href>(?!\w+://|/|#)[^)\s#]+\.(?:md|markdown))(?:#[^)\s]*)?\)',
    re.IGNORECASE,
)


def _normalize_wikilink_target(raw: str) -> str:
    """Normalize a wikilink target to a relative .md path.

    Strips alias (`|alias`) and anchor (`#section`); appends `.md` if absent.
    """
    target = raw.strip()
    if '|' in target:
        target = target.split('|', 1)[0].strip()
    if '#' in target:
        target = target.split('#', 1)[0].strip()
    if not target:
        return ''
    lower = target.lower()
    if not (lower.endswith('.md') or lower.endswith('.markdown')):
        target = f'{target}.md'
    return target


def _normalize_md_href(href: str, source_rel: str) -> str:
    """Resolve a relative markdown href against the source file's directory.

    Returns a normalized relative path under the wiki root, or empty string if
    the link escapes the root via ``..``.
    """
    cleaned = href.strip()
    if not cleaned:
        return ''
    source_dir = os.path.dirname(source_rel)
    joined = os.path.normpath(os.path.join(source_dir, cleaned))
    if joined.startswith('..') or os.path.isabs(joined):
        return ''
    return joined.replace(os.sep, '/')


def _extract_links(content: str, source_rel: str) -> list[tuple[str, str, str]]:
    """Extract (target, kind, label) tuples from a markdown file's body.

    ``kind`` is ``'wikilink'`` or ``'markdown'``. External http(s) links are
    not extracted by the regexes.
    """
    out: list[tuple[str, str, str]] = []
    for match in _WIKILINK_RE.finditer(content):
        raw = match.group(1)
        label = raw.split('|', 1)[1].strip() if '|' in raw else raw.split('#', 1)[0].strip()
        target = _normalize_wikilink_target(raw)
        if target:
            out.append((target, 'wikilink', label))
    for match in _MD_LINK_RE.finditer(content):
        href = match.group('href')
        target = _normalize_md_href(href, source_rel)
        if target:
            out.append((target, 'markdown', match.group('label')))
    return out


@router.get('/wiki/graph')
async def get_wiki_graph(
    maxNodes: int = Query(default=MAX_GRAPH_NODES, ge=1, le=MAX_GRAPH_NODES),
    maxEdges: int = Query(default=MAX_GRAPH_EDGES, ge=1, le=MAX_GRAPH_EDGES),
    includeUnresolved: bool = Query(default=True),
    scope: str = Query(default='global'),
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    root = wiki_root()
    tree = iter_wiki_markdown_tree(root)
    files = _flatten_files(tree)

    # Build per-file metadata for existing markdown files.
    file_meta: dict[str, dict[str, Any]] = {}
    for node in files:
        rel = node.path.replace(os.sep, '/')
        directory = os.path.dirname(rel)
        file_meta[rel] = {
            'id': rel,
            'path': rel,
            'title': Path(rel).stem,
            'directory': directory,
            'tags': [],
            'exists': True,
            'orphan': False,
            'mtime': node.mtime,
        }

    # Walk files once more to extract links + derive titles via frontmatter.
    raw_edges: list[tuple[str, str, str, str, bool]] = []  # (source, target, kind, label, unresolved)
    unresolved_targets: dict[str, dict[str, Any]] = {}

    for rel in list(file_meta.keys()):
        try:
            resolved = resolve_wiki_path(rel, root=root, must_be_file=True)
            content = resolved.read_text(encoding='utf-8')
        except (WikiPathError, OSError) as e:
            logger.warning(f'skipping file during graph build: {rel}: {e}')
            continue

        frontmatter, body = _parse_frontmatter(content)
        file_meta[rel]['title'] = _derive_title(frontmatter, body, Path(rel).stem)
        fm_tags = frontmatter.get('tags')
        tags_list = fm_tags if isinstance(fm_tags, list) else []
        file_meta[rel]['tags'] = [str(t) for t in tags_list]

        for target, kind, label in _extract_links(content, rel):
            target_norm = target.replace(os.sep, '/')
            is_unresolved = target_norm not in file_meta
            if is_unresolved:
                if not includeUnresolved:
                    continue
                if target_norm not in unresolved_targets:
                    unresolved_targets[target_norm] = {
                        'id': target_norm,
                        'path': target_norm,
                        'title': Path(target_norm).stem,
                        'directory': os.path.dirname(target_norm),
                        'tags': [],
                        'exists': False,
                        'orphan': False,
                        'mtime': None,
                    }
            raw_edges.append((rel, target_norm, kind, label, is_unresolved))

    # Compose full node + edge sets prior to capping.
    all_nodes = {**file_meta, **unresolved_targets}

    def _mtime_key(node_id: str) -> tuple[float, str]:
        m = all_nodes[node_id]['mtime']
        try:
            mt = float(m) if m is not None else 0.0
        except (TypeError, ValueError):
            mt = 0.0
        # Sort by mtime DESC then path ASC → negate mtime for desc with stable secondary asc.
        return (-mt, node_id)

    sorted_node_ids = sorted(all_nodes.keys(), key=_mtime_key)
    truncated = False
    if len(sorted_node_ids) > maxNodes:
        truncated = True
    kept_ids = set(sorted_node_ids[:maxNodes])

    # Filter edges whose both endpoints survived; sort deterministically.
    surviving_edges = [e for e in raw_edges if e[0] in kept_ids and e[1] in kept_ids]
    surviving_edges.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    if len(surviving_edges) > maxEdges:
        truncated = True
        surviving_edges = surviving_edges[:maxEdges]

    # Mark orphans: kept nodes with no surviving edge endpoint.
    endpoint_ids: set[str] = set()
    for src, tgt, *_ in surviving_edges:
        endpoint_ids.add(src)
        endpoint_ids.add(tgt)

    nodes_out: list[dict[str, Any]] = []
    for node_id in sorted_node_ids[:maxNodes]:
        meta = dict(all_nodes[node_id])
        meta['orphan'] = node_id not in endpoint_ids
        nodes_out.append(meta)

    edges_out: list[dict[str, Any]] = []
    unresolved_count = 0
    for source, target, kind, label, is_unresolved in surviving_edges:
        if is_unresolved:
            unresolved_count += 1
        edges_out.append(
            {
                'id': f'{source}->{target}',
                'source': source,
                'target': target,
                'kind': kind,
                'label': label,
                'unresolved': is_unresolved,
            }
        )

    return {
        'nodes': nodes_out,
        'edges': edges_out,
        'truncated': truncated,
        'stats': {
            'nodes': len(nodes_out),
            'edges': len(edges_out),
            'unresolved': unresolved_count,
        },
    }


# ── POST /wiki/import ──────────────────────────────────────────────────────


class WikiImportFile(BaseModel):
    path: str
    content: str


class WikiImportRequest(BaseModel):
    files: list[WikiImportFile]
    target_dir: str = 'imports'


_normalized_import_relative_path = normalized_import_relative_path


@router.post('/wiki/import')
async def import_wiki_files(
    body: WikiImportRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    if not body.files:
        raise HTTPException(status_code=400, detail='no files provided')
    if len(body.files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413, detail='too many files')

    root = wiki_root()
    root.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    prepared: list[tuple[Path, str, str]] = []
    seen: set[str] = set()

    for item in body.files:
        try:
            target, relative = _normalized_import_relative_path(root, body.target_dir, item.path)
        except WikiPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if relative in seen:
            raise HTTPException(status_code=400, detail=f'duplicate import path: {relative}')
        seen.add(relative)

        size = len(item.content.encode('utf-8'))
        if size > MAX_READ_BYTES:
            raise HTTPException(status_code=413, detail=f'file too large: {relative}')
        total_bytes += size
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail='import too large')

        prepared.append((target, relative, item.content))

    created: list[str] = []
    updated: list[str] = []

    try:
        for target, relative, content in prepared:
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            if existed:
                updated.append(relative)
            else:
                created.append(relative)
    except OSError:
        raise HTTPException(status_code=500, detail='failed to write import')

    return {
        'created': created,
        'updated': updated,
        'imported': created + updated,
        'skipped': [],
        'target_dir': body.target_dir,
    }


# ── POST /wiki/bootstrap ──────────────────────────────────────────────────


def _sanitize_domain(raw: str) -> str:
    stripped = raw.strip()[:200]
    return _html.escape(stripped)


class BootstrapRequest(BaseModel):
    domain: str = ''


_SCHEMA_TEMPLATE = """\
# Wiki Schema

This wiki stores durable project and company knowledge.

## Structure
- `index.md` — master index of all wiki pages
- `log.md` — recent changes and decisions
- `concepts/` — architecture and design concepts
- `decisions/` — architectural decision records
- `runbooks/` — operational procedures
"""

_INDEX_TEMPLATE = """\
# Wiki Index

{domain}

## Pages
<!-- Add links to wiki pages here -->
"""

_LOG_TEMPLATE = """\
# Change Log

## {today}
- Wiki initialized
"""


@router.post('/wiki/bootstrap')
async def bootstrap_wiki(
    body: BootstrapRequest,
    _auth: None = Depends(require_session_token),
) -> dict[str, Any]:
    domain = _sanitize_domain(body.domain)
    root = wiki_root()
    root.mkdir(parents=True, exist_ok=True)

    today = _dt.date.today().isoformat()
    templates = {
        'SCHEMA.md': _SCHEMA_TEMPLATE,
        'index.md': _INDEX_TEMPLATE.format(domain=domain),
        'log.md': _LOG_TEMPLATE.format(today=today),
    }

    created: list[str] = []
    skipped: list[str] = []

    for name in BACKBONE_FILES:
        target = root / name
        if target.exists():
            skipped.append(name)
        else:
            target.write_text(templates[name], encoding='utf-8')
            created.append(name)

    return {
        'created': created,
        'skipped': skipped,
        'provisioned': all((root / n).is_file() for n in BACKBONE_FILES),
    }
