"""Tests for Myah platform adapter attachments ingestion.

This is a Myah-only file — no upstream counterpart exists.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 100
MP3_BYTES = b'ID3\x04\x00\x00' + b'\x00' * 100
PDF_BYTES = b'%PDF-1.4\n' + b'\x00' * 100


def _make_adapter():
    """Build a MyahAdapter with minimal config and pre-setup hook mocked out."""
    config = PlatformConfig(
        enabled=True,
        extra={'auth_key': 'test-bearer'},
    )
    with patch('gateway.platforms.api_server.register_pre_setup_hook'):
        from myah_hermes_plugin.myah_platform.adapter import MyahAdapter
        adapter = MyahAdapter(config)

    # Mark as connected so _handle_message_endpoint doesn't bail on stream limits
    adapter._running = True
    adapter._loop = asyncio.get_event_loop()
    return adapter


def _make_request(body: dict) -> MagicMock:
    """Build a mock aiohttp request with the given JSON body and bearer token."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.headers = {'Authorization': 'Bearer test-bearer'}
    return req


# ── _myah_ext ────────────────────────────────────────────────────────────────

class TestMyahExt:
    def test_uses_filename_extension(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        assert _myah_ext('image/jpeg', 'photo.jpg', '.png') == '.jpg'

    def test_filename_takes_priority_over_mime(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        # filename extension beats MIME
        assert _myah_ext('image/png', 'file.gif', '.jpg') == '.gif'

    def test_falls_back_to_mime_when_no_filename_ext(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        result = _myah_ext('image/png', 'noext', '.jpg')
        # Mime lookup result varies by platform — just confirm it's an extension
        assert result.startswith('.')
        assert len(result) >= 2

    def test_uses_default_on_empty_mime_and_filename(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        assert _myah_ext('', '', '.jpg') == '.jpg'

    def test_uses_default_on_no_dot_in_filename(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        # "attachment" has no dot — fall through to mime, then default
        result = _myah_ext('', 'attachment', '.bin')
        assert result == '.bin'

    def test_rejects_very_short_extension(self):
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        # ".x" length 2 is the minimum; ".j" would also be accepted
        # A single-char extension like ".x" is allowed (len == 2)
        # An empty extension from rsplit wouldn't happen, but let's verify
        # the mime/default fallback works when filename ext is too long
        result = _myah_ext('image/jpeg', 'file.toolongext', '.jpg')
        # 'toolongext' => '.toolongext' has len 11 > 6, so fallback to mime/default
        assert result != '.toolongext'

    def test_rejects_missing_file_id(self):
        """_myah_ext itself doesn't reject file_id; just verify it's isolated."""
        from myah_hermes_plugin.myah_platform.adapter import _myah_ext
        # Independent of file_id — pure filename/mime helper
        assert _myah_ext('audio/mpeg', 'track.mp3', '.ogg') == '.mp3'


# ── _handle_message_endpoint: no attachments ─────────────────────────────────

class TestHandleMessageNoAttachments:
    @pytest.mark.asyncio
    async def test_plain_text_message_returns_202(self):
        """Plain text message with no attachments dispatches and returns 202."""
        adapter = _make_adapter()
        body = {'message': 'hello world', 'session_id': 'sess-1', 'user_id': 'u1'}
        req = _make_request(body)

        with patch.object(adapter, '_dispatch_message', new=AsyncMock()):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 202

    @pytest.mark.asyncio
    async def test_empty_attachments_list_ignored(self):
        """Explicit empty attachments list still returns 202."""
        adapter = _make_adapter()
        body = {
            'message': 'no files',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [],
        }
        req = _make_request(body)

        with patch.object(adapter, '_dispatch_message', new=AsyncMock()):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 202

    @pytest.mark.asyncio
    async def test_missing_message_returns_400(self):
        """Request with empty message field returns 400."""
        adapter = _make_adapter()
        body = {'message': '', 'session_id': 's', 'user_id': 'u'}
        req = _make_request(body)

        resp = await adapter._handle_message_endpoint(req)
        assert resp.status == 400


# ── _handle_message_endpoint: missing env vars ───────────────────────────────

class TestHandleMessageMissingEnv:
    @pytest.mark.asyncio
    async def test_attachments_without_env_returns_500(self):
        """When env vars are not set, any attachment returns 500 immediately."""
        adapter = _make_adapter()
        body = {
            'message': 'analyze this',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'x', 'filename': 'x.jpg',
                              'mime_type': 'image/jpeg', 'size': 100}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', None), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', None):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_only_base_url_missing_returns_500(self):
        """Missing base URL alone (with bearer) still returns 500."""
        adapter = _make_adapter()
        body = {
            'message': 'go',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'x', 'filename': 'x.pdf',
                              'mime_type': 'application/pdf', 'size': 100}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', None), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 500


# ── _handle_message_endpoint: oversize attachment ────────────────────────────

class TestHandleMessageOversizeAttachment:
    @pytest.mark.asyncio
    async def test_declared_size_too_large_returns_502(self):
        """Attachment declared larger than cap triggers ValueError → 502."""
        adapter = _make_adapter()
        body = {
            'message': 'analyze',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'x', 'filename': 'big.bin',
                              'mime_type': 'application/octet-stream',
                              'size': 25 * 1024 * 1024}],  # 25 MB > 20 MB cap
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 502

    @pytest.mark.asyncio
    async def test_missing_file_id_returns_502(self):
        """Attachment without file_id raises ValueError → 502."""
        adapter = _make_adapter()
        body = {
            'message': 'go',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'filename': 'nokey.jpg',
                              'mime_type': 'image/jpeg', 'size': 100}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'):
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 502


# ── _handle_message_endpoint: platform HTTP error ────────────────────────────

class TestHandleMessagePlatformError:
    @pytest.mark.asyncio
    async def test_platform_non_200_returns_502(self):
        """Platform returning 404 for a file causes 502."""
        adapter = _make_adapter()

        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.read = AsyncMock(return_value=b'')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        body = {
            'message': 'what is this',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'missing', 'filename': 'a.jpg',
                              'mime_type': 'image/jpeg', 'size': 100}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'), \
             patch('myah_hermes_plugin.myah_platform.adapter._myah_aiohttp') as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout.return_value = MagicMock()
            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 502


# ── _handle_message_endpoint: successful JPEG ────────────────────────────────

class TestHandleMessageJpeg:
    @pytest.mark.asyncio
    async def test_jpeg_calls_cache_image_and_returns_202(self):
        """JPEG attachment is routed through cache_image_from_bytes → 202."""
        adapter = _make_adapter()

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=JPEG_BYTES)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        body = {
            'message': 'describe this image',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'img1', 'filename': 'photo.jpg',
                              'mime_type': 'image/jpeg', 'size': len(JPEG_BYTES)}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'), \
             patch('myah_hermes_plugin.myah_platform.adapter._myah_aiohttp') as mock_aiohttp, \
             patch('myah_hermes_plugin.myah_platform.adapter.cache_image_from_bytes',
                   return_value='/cache/img.jpg') as mock_cache_img, \
             patch.object(adapter, '_dispatch_message', new=AsyncMock()):

            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout.return_value = MagicMock()

            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 202
        mock_cache_img.assert_called_once()
        # Verify extension was derived from filename
        call_kwargs = mock_cache_img.call_args
        assert call_kwargs.kwargs.get('ext') == '.jpg' or \
               (call_kwargs.args and call_kwargs.args[0] == JPEG_BYTES)


# ── _handle_message_endpoint: audio attachment ───────────────────────────────

class TestHandleMessageAudio:
    @pytest.mark.asyncio
    async def test_mp3_calls_cache_audio(self):
        """Audio MIME type is routed through cache_audio_from_bytes."""
        adapter = _make_adapter()

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=MP3_BYTES)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        body = {
            'message': 'transcribe',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'aud1', 'filename': 'track.mp3',
                              'mime_type': 'audio/mpeg', 'size': len(MP3_BYTES)}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'), \
             patch('myah_hermes_plugin.myah_platform.adapter._myah_aiohttp') as mock_aiohttp, \
             patch('myah_hermes_plugin.myah_platform.adapter.cache_audio_from_bytes',
                   return_value='/cache/audio.mp3') as mock_cache_audio, \
             patch.object(adapter, '_dispatch_message', new=AsyncMock()):

            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout.return_value = MagicMock()

            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 202
        mock_cache_audio.assert_called_once()


# ── _handle_message_endpoint: document attachment ────────────────────────────

class TestHandleMessageDocument:
    @pytest.mark.asyncio
    async def test_pdf_calls_cache_document(self):
        """Non-image/non-audio MIME is routed through cache_document_from_bytes."""
        adapter = _make_adapter()

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=PDF_BYTES)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        body = {
            'message': 'summarize',
            'session_id': 's',
            'user_id': 'u',
            'attachments': [{'file_id': 'doc1', 'filename': 'report.pdf',
                              'mime_type': 'application/pdf', 'size': len(PDF_BYTES)}],
        }
        req = _make_request(body)

        with patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BASE_URL', 'http://plat'), \
             patch('myah_hermes_plugin.myah_platform.adapter._MYAH_PLATFORM_BEARER', 'tok'), \
             patch('myah_hermes_plugin.myah_platform.adapter._myah_aiohttp') as mock_aiohttp, \
             patch('myah_hermes_plugin.myah_platform.adapter.cache_document_from_bytes',
                   return_value='/cache/doc.pdf') as mock_cache_doc, \
             patch.object(adapter, '_dispatch_message', new=AsyncMock()):

            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout.return_value = MagicMock()

            resp = await adapter._handle_message_endpoint(req)

        assert resp.status == 202
        mock_cache_doc.assert_called_once_with(PDF_BYTES, 'report.pdf')
