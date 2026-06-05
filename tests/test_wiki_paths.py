"""Tests for the wiki path sandbox helper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from myah_hermes_plugin.myah_admin.dashboard._wiki_paths import (
    WikiPathError,
    iter_wiki_markdown_tree,
    markdown_only,
    resolve_wiki_path,
    wiki_root,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write(root: Path, relative: str, body: str = '# hello\n') -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    return path


def _paths(nodes) -> list[str]:
    found = []
    for node in nodes:
        found.append(node.path)
        if node.children:
            found.extend(_paths(node.children))
    return found


def _flatten(nodes):
    flat = []
    for node in nodes:
        flat.append(node)
        if node.children:
            flat.extend(_flatten(node.children))
    return flat


def _assert_rejects_without_root(root: Path, requested: str, **kwargs) -> None:
    with pytest.raises(WikiPathError) as exc:
        resolve_wiki_path(requested, root=root, **kwargs)

    message = str(exc.value)
    assert str(root) not in message
    assert os.path.realpath(root) not in message


# ── Root and file resolution ─────────────────────────────────────────────────


def test_wiki_root_uses_wiki_path_env_when_set(tmp_path, monkeypatch):
    root = tmp_path / 'custom-wiki'
    monkeypatch.setenv('WIKI_PATH', str(root))

    assert wiki_root() == root


def test_wiki_root_defaults_to_hermes_home_wiki(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    monkeypatch.delenv('WIKI_PATH', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))

    assert wiki_root() == home / 'wiki'


def test_resolve_wiki_path_accepts_safe_relative_markdown(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    safe = _write(root, 'docs/intro.md')

    assert resolve_wiki_path('docs/intro.md', root=root) == Path(os.path.realpath(safe))


def test_resolve_wiki_path_url_decodes_once_for_safe_paths(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    safe = _write(root, 'docs/intro.md')

    assert resolve_wiki_path('docs%2Fintro.md', root=root) == Path(os.path.realpath(safe))


def test_resolve_wiki_path_rejects_absolute_path_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()

    _assert_rejects_without_root(root, '/etc/passwd')


def test_resolve_wiki_path_rejects_parent_traversal_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()

    _assert_rejects_without_root(root, '../../etc/passwd')


def test_resolve_wiki_path_rejects_url_encoded_traversal_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()

    _assert_rejects_without_root(root, '%2e%2e/etc/passwd')


def test_resolve_wiki_path_rejects_nul_byte_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()

    _assert_rejects_without_root(root, 'foo\x00.md')


@pytest.mark.parametrize('requested', ['.hidden.md', '.secrets/key.md', 'docs/.secret.md'])
def test_resolve_wiki_path_rejects_dot_segments_without_leaking_root(tmp_path, requested):
    root = tmp_path / 'wiki'
    root.mkdir()
    _write(root, requested)

    _assert_rejects_without_root(root, requested)


def test_resolve_wiki_path_rejects_directory_when_file_required_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    (root / 'docs').mkdir(parents=True)

    _assert_rejects_without_root(root, 'docs')


def test_resolve_wiki_path_rejects_oversized_file_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    _write(root, 'large.md', 'x' * 11)

    _assert_rejects_without_root(root, 'large.md', max_bytes=10)


def test_resolve_wiki_path_rejects_symlink_escape_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('# outside\n', encoding='utf-8')
    (root / 'escape.md').symlink_to(outside)

    _assert_rejects_without_root(root, 'escape.md')


# ── Markdown and tree walking ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('note.md', True),
        ('note.MARKDOWN', True),
        ('note.txt', False),
        ('note.md.bak', False),
    ],
)
def test_markdown_only_is_case_insensitive(name, expected):
    assert markdown_only(Path(name)) is expected


def test_iter_wiki_markdown_tree_filters_hidden_non_markdown_and_symlinked_dirs(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    _write(root, 'visible/a.md')
    _write(root, 'visible/not-markdown.txt')
    _write(root, '.hidden.md')
    _write(root, '.secrets/key.md')
    outside = tmp_path / 'outside'
    outside.mkdir()
    _write(outside, 'escape.md')
    (root / 'linked').symlink_to(outside, target_is_directory=True)

    nodes = iter_wiki_markdown_tree(root)
    paths = _paths(nodes)
    flat = _flatten(nodes)

    assert 'visible/a.md' in paths
    assert 'visible/not-markdown.txt' not in paths
    assert '.hidden.md' not in paths
    assert '.secrets' not in paths
    assert '.secrets/key.md' not in paths
    assert 'linked' not in paths
    assert 'linked/escape.md' not in paths
    assert all(not Path(node.path).is_absolute() for node in flat)


def test_iter_wiki_markdown_tree_rejects_symlinked_file_escape_without_leaking_root(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('# outside\n', encoding='utf-8')
    (root / 'escape.md').symlink_to(outside)

    with pytest.raises(WikiPathError) as exc:
        iter_wiki_markdown_tree(root)

    message = str(exc.value)
    assert str(root) not in message
    assert os.path.realpath(root) not in message


def test_iter_wiki_markdown_tree_sorts_dirs_first_then_files_alphabetically(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    _write(root, 'z_dir/note.md')
    _write(root, 'a_dir/note.md')
    _write(root, 'z.md')
    _write(root, 'a.md')

    nodes = iter_wiki_markdown_tree(root)

    assert [node.path for node in nodes] == ['a_dir', 'z_dir', 'a.md', 'z.md']
    assert nodes[0].children is not None
    assert [node.path for node in nodes[0].children] == ['a_dir/note.md']


def test_iter_wiki_markdown_tree_respects_max_depth(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    _write(root, 'top.md')
    _write(root, 'level1/deep.md')
    _write(root, 'level1/level2/deeper.md')

    paths = _paths(iter_wiki_markdown_tree(root, max_depth=1))

    assert 'top.md' in paths
    assert 'level1/deep.md' not in paths
    assert 'level1/level2/deeper.md' not in paths
    assert all(len(Path(path).parts) <= 1 for path in paths)


def test_iter_wiki_markdown_tree_respects_max_nodes(tmp_path):
    root = tmp_path / 'wiki'
    root.mkdir()
    for index in range(5):
        _write(root, f'{index}.md')

    nodes = iter_wiki_markdown_tree(root, max_nodes=3)


    assert _paths(nodes) == ['0.md', '1.md', '2.md']
