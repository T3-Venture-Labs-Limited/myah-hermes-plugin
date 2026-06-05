"""Tests for the Myah Knowledge Base agent tool."""

from __future__ import annotations

import json
from pathlib import Path

from myah_hermes_plugin.myah_tools import knowledge_base_tool


def _call(payload: dict) -> dict:
    return json.loads(knowledge_base_tool.handle(payload))


def test_write_uses_wiki_path_for_relative_kb_paths(monkeypatch, tmp_path):
    wiki_root = tmp_path / 'wiki'
    cwd = tmp_path / 'cwd'
    wiki_root.mkdir()
    cwd.mkdir()
    monkeypatch.setenv('WIKI_PATH', str(wiki_root))
    monkeypatch.chdir(cwd)

    result = _call(
        {
            'action': 'write',
            'path': 'tests/agent-write/stories/story-map.md',
            'content': '# Story Map\n',
        }
    )

    assert result['error'] is None
    assert result['written'] == ['tests/agent-write/stories/story-map.md']
    assert (wiki_root / 'tests' / 'agent-write' / 'stories' / 'story-map.md').read_text(
        encoding='utf-8'
    ) == '# Story Map\n'
    assert not (cwd / 'tests' / 'agent-write' / 'stories' / 'story-map.md').exists()


def test_read_returns_file_from_wiki_path(monkeypatch, tmp_path):
    wiki_root = tmp_path / 'wiki'
    target = wiki_root / 'tests' / 'agent-write' / 'stories' / 'story-map.md'
    target.parent.mkdir(parents=True)
    target.write_text('# Story Map\n', encoding='utf-8')
    monkeypatch.setenv('WIKI_PATH', str(wiki_root))

    result = _call({'action': 'read', 'path': 'tests/agent-write/stories/story-map.md'})

    assert result['error'] is None
    assert result['path'] == 'tests/agent-write/stories/story-map.md'
    assert result['content'] == '# Story Map\n'


def test_list_returns_nested_markdown_tree(monkeypatch, tmp_path):
    wiki_root = tmp_path / 'wiki'
    (wiki_root / 'tests' / 'agent-write' / 'stories' / 'lighthouse').mkdir(parents=True)
    (wiki_root / 'tests' / 'agent-write' / 'stories' / 'story-map.md').write_text(
        '# Story Map\n', encoding='utf-8'
    )
    (wiki_root / 'tests' / 'agent-write' / 'stories' / 'lighthouse' / 'a.md').write_text(
        '# A\n', encoding='utf-8'
    )
    monkeypatch.setenv('WIKI_PATH', str(wiki_root))

    result = _call({'action': 'list', 'path': 'tests/agent-write/stories'})

    assert result['error'] is None
    assert result['root'] == 'tests/agent-write/stories'
    assert result['files'] == [
        'tests/agent-write/stories/lighthouse/a.md',
        'tests/agent-write/stories/story-map.md',
    ]


def test_write_rejects_traversal_without_writing(monkeypatch, tmp_path):
    wiki_root = tmp_path / 'wiki'
    wiki_root.mkdir()
    monkeypatch.setenv('WIKI_PATH', str(wiki_root))

    result = _call({'action': 'write', 'path': '../outside.md', 'content': '# nope\n'})

    assert result['error'] == 'path traversal not allowed'
    assert not (tmp_path / 'outside.md').exists()


def test_write_rejects_non_markdown(monkeypatch, tmp_path):
    wiki_root = tmp_path / 'wiki'
    wiki_root.mkdir()
    monkeypatch.setenv('WIKI_PATH', str(wiki_root))

    result = _call({'action': 'write', 'path': 'tests/story.txt', 'content': 'nope\n'})

    assert result['error'] == 'only markdown files are writable'
    assert not (wiki_root / 'tests' / 'story.txt').exists()
