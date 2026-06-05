"""Tests for the myah-admin wiki import route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from myah_hermes_plugin.myah_admin.dashboard import _wiki


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.delenv('HERMES_WEB_SESSION_TOKEN', raising=False)
    monkeypatch.setenv('WIKI_PATH', str(tmp_path / 'wiki'))
    app = FastAPI()
    app.include_router(_wiki.router)
    return TestClient(app)


def test_import_wiki_files_creates_nested_markdown_files(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        '/wiki/import',
        json={
            'target_dir': 'imports',
            'files': [
                {'path': 'notes/a.md', 'content': '# A\n'},
                {'path': 'root.md', 'content': '# Root\n'},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        'created': ['imports/notes/a.md', 'imports/root.md'],
        'updated': [],
        'imported': ['imports/notes/a.md', 'imports/root.md'],
        'skipped': [],
        'target_dir': 'imports',
    }
    root = tmp_path / 'wiki'
    assert (root / 'imports' / 'notes' / 'a.md').read_text(encoding='utf-8') == '# A\n'
    assert (root / 'imports' / 'root.md').read_text(encoding='utf-8') == '# Root\n'


def test_import_wiki_files_reports_updates(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    target = tmp_path / 'wiki' / 'imports' / 'note.md'
    target.parent.mkdir(parents=True)
    target.write_text('# old\n', encoding='utf-8')

    response = client.post(
        '/wiki/import',
        json={'target_dir': 'imports', 'files': [{'path': 'note.md', 'content': '# new\n'}]},
    )

    assert response.status_code == 200
    assert response.json()['created'] == []
    assert response.json()['updated'] == ['imports/note.md']
    assert target.read_text(encoding='utf-8') == '# new\n'


def test_import_wiki_files_rejects_traversal_without_leaking_root(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        '/wiki/import',
        json={'target_dir': 'imports', 'files': [{'path': '../secret.md', 'content': '# nope\n'}]},
    )

    assert response.status_code == 400
    assert response.json()['detail'] == 'path traversal not allowed'
    assert str(tmp_path) not in response.text


def test_import_wiki_files_rejects_non_markdown(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        '/wiki/import',
        json={'target_dir': 'imports', 'files': [{'path': 'note.txt', 'content': 'plain\n'}]},
    )

    assert response.status_code == 400
    assert response.json()['detail'] == 'only markdown files are importable'


def test_status_advertises_import_limits(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get('/wiki/status')

    assert response.status_code == 200
    body = response.json()
    assert body['readonly'] is False
    assert body['limits']['max_import_files'] == _wiki.MAX_IMPORT_FILES
    assert body['limits']['max_import_total_bytes'] == _wiki.MAX_IMPORT_TOTAL_BYTES
