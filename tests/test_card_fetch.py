from __future__ import annotations

import asyncio
import json
import socket
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from card_fetch import (  # noqa: E402
    CardFetchError,
    _decode_data_uri,
    _resolve_public,
    fetch_public_card_content,
    public_url,
)
from test_cards import b64, chunk, make_png  # noqa: E402

from cards import MAX_CARD_METADATA, CardError, CharacterCard  # noqa: E402


class PublicFetchTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, url='https://cards.example/card.json', **kwargs):
        real_client = httpx.AsyncClient
        options = []

        def client(**settings):
            options.append(settings)
            def streaming(request):
                response = handler(request)
                if response.is_stream_consumed:
                    response.stream = httpx.ByteStream(response.content)
                    response.is_stream_consumed = False
                    response.is_closed = False
                return response
            return real_client(transport=httpx.MockTransport(streaming), **settings)

        with (patch('httpx.AsyncClient', side_effect=client),
              patch('card_fetch._resolve_public', new_callable=AsyncMock, return_value='8.8.8.8') as dns):
            result = await fetch_public_card_content(url, **kwargs)
        self.assertFalse(options[0]['trust_env'])
        self.assertFalse(options[0]['follow_redirects'])
        return result, dns

    async def test_connection_pins_checked_ip_and_preserves_host_sni(self):
        def handler(request):
            self.assertEqual(request.url.host, '8.8.8.8')
            self.assertEqual(request.headers['host'], 'cards.example')
            self.assertEqual(request.extensions['sni_hostname'], 'cards.example')
            self.assertEqual(request.headers['accept-encoding'], 'identity')
            return httpx.Response(200, content=b'card')
        result, dns = await self.request(handler)
        self.assertEqual(result.data, b'card')
        self.assertEqual(result.url, 'https://cards.example/card.json')
        dns.assert_awaited_once_with('cards.example')

    async def test_redirect_is_validated_before_next_connection(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={'location': 'https://127.0.0.1/metadata'})
        with self.assertRaises(CardFetchError):
            await self.request(handler)
        self.assertEqual(len(requests), 1)

    async def test_public_cdn_redirect_rechecks_dns_and_attributes_final_url(self):
        def handler(request):
            if request.headers['host'] == 'cards.example':
                return httpx.Response(302, headers={'location': 'https://cdn.example/card.png'})
            return httpx.Response(200, content=b'png')
        result, dns = await self.request(handler)
        self.assertEqual(result.url, 'https://cdn.example/card.png')
        self.assertEqual(dns.await_count, 2)

    async def test_loop_size_compression_and_status_fail_closed(self):
        cases = [httpx.Response(302, headers={'location': '/card.json'}),
                 httpx.Response(200, headers={'content-length': '99'}, content=b'a'),
                 httpx.Response(200, content=b'12345'),
                 httpx.Response(200, headers={'content-encoding': 'gzip'}, stream=httpx.ByteStream(b'compressed')),
                 httpx.Response(403)]
        for response in cases:
            with self.subTest(status=response.status_code, headers=response.headers):
                with self.assertRaises(CardFetchError):
                    await self.request(lambda request, response=response: response, max_bytes=4)

    async def test_total_deadline_includes_dns(self):
        async def slow(host):
            await asyncio.sleep(0.05)
            return '8.8.8.8'
        with patch('card_fetch._resolve_public', side_effect=slow):
            with self.assertRaises(CardFetchError):
                await fetch_public_card_content('https://cards.example/card', timeout=0.01)

    async def test_mixed_public_private_dns_answer_is_rejected(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new_callable=AsyncMock,
                          return_value=addresses):
            with self.assertRaises(CardFetchError):
                await _resolve_public('rebind.example')

    def test_non_public_and_unsafe_urls_are_rejected(self):
        for url in ('http://example.com/card', 'file:///private/card.json', 'data:abc',
                    'https://user:pass@example.com/a', 'https://example.com:8443/a',
                    'https://localhost/a', 'https://127.0.0.1/a', 'https://10.0.0.1/a',
                    'https://169.254.169.254/a', 'https://[::1]/a', 'https://[fc00::1]/a',
                    'https://[::ffff:8.8.8.8]/a', 'https://example.com/\nprivate'):
            with self.subTest(url=url), self.assertRaises(CardFetchError):
                public_url(url)

    def test_data_uri_is_bounded_before_decoding(self):
        with patch('card_fetch.MAX_CARD_BYTES', 3), patch('card_fetch.base64.b64decode') as decode:
            with self.assertRaises(CardFetchError):
                _decode_data_uri('data:image/png;base64,' + 'A' * 8)
            decode.assert_not_called()


class RemoteCardValidationTests(unittest.TestCase):
    def test_valid_remote_versions_and_png(self):
        for raw in ({'char_name': 'Old', 'char_persona': 'A librarian'},
                    {'name': 'One', 'description': 'A librarian'},
                    *({'spec': spec, 'data': {'name': 'Card', 'description': 'A librarian'}}
                      for spec in ('chara_card_v2', 'chara_card_v3'))):
            data = json.dumps(raw).encode()
            self.assertTrue(CharacterCard.from_bytes(data, strict=True).name)
            self.assertTrue(CharacterCard.from_bytes(make_png([(b'chara', b64(raw), b'tEXt')]), strict=True).name)

    def test_empty_api_html_deep_and_invalid_fields_are_rejected(self):
        for data in (b'{}', b'{"error":"not a card"}', b'<html>page</html>',
                     b'{"name":"avatar"}', b'{"name":"X","description":123}',
                     b'[' * 100 + b'0' + b']' * 100):
            with self.subTest(data=data[:40]), self.assertRaises(CardError):
                CharacterCard.from_bytes(data, strict=True)
        # Trusted local imports retain the existing compatibility behavior.
        self.assertEqual(CharacterCard.from_bytes(b'{}').name, 'Assistant')

    def test_truncated_and_bad_crc_png_rejected(self):
        png = make_png([(b'chara', b64({'name': 'X', 'description': 'real card'}), b'tEXt')])
        for data in (png[:-3], png[:20] + b'xxxx' + png[24:]):
            with self.assertRaises(CardError):
                CharacterCard.from_bytes(data, strict=True)

    def test_compressed_metadata_bomb_is_bounded(self):
        compressed = zlib.compress(b'A' * (MAX_CARD_METADATA + 1))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'iTXt', b'chara\x00\x01\x00\x00\x00' + compressed) + chunk(b'IEND', b'')
        with self.assertRaises(CardError):
            CharacterCard.from_bytes(png, strict=True)


if __name__ == '__main__':
    unittest.main()
