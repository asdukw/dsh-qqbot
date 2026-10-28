"""Real pinned extractors against HTTP fixtures, never live websites."""

from __future__ import annotations

import importlib
import importlib.util
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from video_http import SafeHTTP, handler_class  # noqa: E402
from video_platforms import VideoError  # noqa: E402
from video_worker import Quiet, extract  # noqa: E402

BV = 'BV1xx411c7mD'


@unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'requires pinned yt-dlp dependency')
class ExtractorTests(unittest.TestCase):
    def fixture(self, request):
        host, path = request.headers['host'], request.url.path
        self.requests.append((host, path, str(request.url.query)))
        self.assertEqual(request.url.host, '8.8.8.8')
        self.assertEqual(request.extensions['sni_hostname'], host)
        if path == '/x/web-interface/view':
            return httpx.Response(200, json={'code': 0, 'data': {
                'bvid': BV, 'aid': 123, 'pages': [{'cid': 10}, {'cid': 11}]}})
        if path.startswith('/video/'):
            state = {'videoData': {'bvid': BV, 'aid': 123, 'cid': 10, 'title': 'Fixture video',
                                  'desc': 'Fixture description', 'pages': [{'cid': 10}, {'cid': 11}],
                                  'stat': {'view': 1, 'like': 2}, 'rights': {}},
                     'upData': {'name': 'Fixture author', 'mid': 7}, 'tags': []}
            return httpx.Response(200, text='<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script>')
        if path == '/x/player/pagelist':
            return httpx.Response(200, json={'data': [{'page': 1, 'part': 'First'}, {'page': 2, 'part': 'Second'}]})
        if path == '/x/web-interface/nav':
            return httpx.Response(200, json={'data': {'wbi_img': {
                'img_url': 'https://i.hdslb.com/' + 'a' * 32 + '.png',
                'sub_url': 'https://i.hdslb.com/' + 'b' * 32 + '.png'}}})
        if path == '/x/player/wbi/playurl':
            self.assertIn(b'cid=11', request.url.query)
            return httpx.Response(200, json={'code': 0, 'data': {'timelength': 12000,
                'dash': {'video': [{'baseUrl': 'https://media.example/video?secret', 'id': 80,
                                   'mimeType': 'video/mp4', 'width': 1920, 'height': 1080,
                                   'codecs': 'avc1'}],
                         'audio': [{'baseUrl': 'https://media.example/audio?secret', 'id': 30280,
                                    'mimeType': 'audio/mp4', 'codecs': 'mp4a'}]}}})
        if path == '/x/player/wbi/v2':
            return httpx.Response(200, json={'data': {}})
        raise AssertionError(f'unexpected extractor request {host} {path}')

    def run_fixture(self, payload, handler=None):
        self.requests = []
        ydl_class = importlib.import_module('yt_dlp').YoutubeDL
        real_client = httpx.Client
        def client(**settings):
            self.assertFalse(settings['trust_env'])
            def streaming(request):
                response = (handler or self.fixture)(request)
                if response.is_stream_consumed:
                    response.stream = httpx.ByteStream(response.content)
                    response.is_stream_consumed = False
                    response.is_closed = False
                return response
            return real_client(transport=httpx.MockTransport(streaming), **settings)
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        with (patch('video_http.httpx.Client', side_effect=client),
              patch('socket.getaddrinfo', return_value=dns),
              patch.object(ydl_class, 'download', side_effect=AssertionError('download forbidden')),
              patch.object(ydl_class, 'add_default_info_extractors', side_effect=AssertionError('default extractors forbidden')),
              patch.object(ydl_class, 'add_post_processor', side_effect=AssertionError('postprocessors forbidden')),
              patch('yt_dlp.extractor.bilibili.BiliBiliIE._get_subtitles', side_effect=AssertionError('caption bodies forbidden')),
              patch('yt_dlp.extractor.bilibili.BiliBiliIE._get_comments', side_effect=AssertionError('comments forbidden')),
              patch('yt_dlp.YoutubeDL.load_all_plugins', side_effect=AssertionError('plugins forbidden'))):
            return extract(payload)

    def test_real_bilibili_extractor_selected_part_and_safe_projection(self):
        result = self.run_fixture({'source': BV, 'part': 2})
        self.assertEqual(result['title'], 'Fixture video p02 Second')
        self.assertEqual(result['selected_part'], 2)
        self.assertEqual(result['duration_seconds'], 12)
        self.assertEqual(result['author']['name'], 'Fixture author')
        self.assertEqual(len(result['resources']), 2)
        self.assertEqual(result['subtitles']['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(any(path == '/x/player/wbi/playurl' for _, path, _ in self.requests))
        self.assertTrue(all(host in ('www.bilibili.com', 'api.bilibili.com') for host, _, _ in self.requests))
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': BV, 'part': 3})
        self.assertEqual(error.exception.code, 'INVALID_SOURCE')

    def test_short_links_av_identity_and_platform_errors(self):
        def short(request):
            if request.headers['host'] == 'b23.tv':
                return httpx.Response(302, headers={'location': f'https://www.bilibili.com/video/{BV}?p=2'})
            return self.fixture(request)
        self.assertEqual(self.run_fixture({'source': 'https://b23.tv/test'}, short)['selected_part'], 2)
        self.assertEqual(self.run_fixture({'source': 'av123', 'part': 2})['content_id'], BV)
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'av456', 'part': 2})
        self.assertEqual(error.exception.code, 'EXTRACTOR_BROKEN')
        with self.assertRaises(VideoError):
            self.run_fixture({'source': 'https://b23.tv/test', 'part': 1}, short)
        for status, code in ((404, 'NOT_FOUND'), (429, 'RATE_LIMITED'), (412, 'UPSTREAM_BLOCKED')):
            with self.subTest(status=status), self.assertRaises(VideoError) as error:
                self.run_fixture({'source': BV}, lambda request, status=status: httpx.Response(status))
            self.assertEqual(error.exception.code, code)

    def test_douyin_galleries_are_not_misreported_as_video(self):
        def gallery(request):
            return httpx.Response(200, json={'aweme_detail': {
                'aweme_id': '123', 'aweme_type': 68, 'images': [{'url': 'private'}],
                'video': {'duration': 1}}})
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://www.douyin.com/video/123'}, gallery)
        self.assertEqual(error.exception.code, 'UNSUPPORTED_SOURCE')

    def test_backend_refuses_redirect_and_does_not_fallback(self):
        def hostile(request):
            if request.url.path.startswith('/video/'):
                return httpx.Response(302, headers={'location': 'https://127.0.0.1/secret'})
            return self.fixture(request)
        with self.assertRaises(VideoError):
            self.run_fixture({'source': BV, 'part': 2}, hostile)
        from yt_dlp.networking.common import Request, RequestDirector
        from yt_dlp.networking.exceptions import NoSupportingHandlers
        director = RequestDirector(logger=Quiet())
        director.add_handler(handler_class()(transport=SafeHTTP('bilibili'), logger=Quiet()))
        self.assertEqual(len(director.handlers), 1)
        with self.assertRaises(NoSupportingHandlers):
            director.send(Request('http://www.bilibili.com/'))
        director.close()

    def test_real_douyin_extractor_missing_cookie_and_metadata(self):
        def empty(request):
            return httpx.Response(200, json={})
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://www.douyin.com/video/123'}, empty)
        self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRED')
        def video(request):
            self.assertEqual(request.headers['host'], 'www.douyin.com')
            self.assertEqual(request.url.path, '/aweme/v1/web/aweme/detail/')
            return httpx.Response(200, json={'aweme_detail': {
                'aweme_id': '123', 'desc': 'Douyin fixture', 'duration': 1000,
                'author': {'nickname': 'Author', 'uid': '7', 'unique_id': 'author'},
                'statistics': {}, 'video': {'duration': 1000, 'width': 720, 'height': 1280,
                    'play_addr': {'url_list': ['https://media.example/video?secret'],
                                  'url_key': 'v1_h264_720p_1000'}, 'cover': {'url_list': []}}}})
        result = self.run_fixture({'source': 'https://www.douyin.com/video/123'}, video)
        self.assertEqual(result['content_id'], '123')
        self.assertEqual(result['platform'], 'douyin')
        self.assertNotIn('secret', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
