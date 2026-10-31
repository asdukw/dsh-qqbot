"""Real pinned extractors against HTTP fixtures, never live websites."""

from __future__ import annotations

import importlib
import importlib.util
import json
import socket
import sys
import tempfile
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
                'bvid': BV, 'aid': 123, 'pages': [{'cid': 10, 'part': 'First'}, {'cid': 11, 'part': 'Second'}]}})
        if path.startswith('/video/'):
            state = {'videoData': {'bvid': BV, 'aid': 123, 'cid': 10, 'title': 'Fixture video',
                                  'desc': 'Fixture description', 'pages': [{'cid': 10, 'part': 'First'}, {'cid': 11, 'part': 'Second'}],
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
        def transport(platform, **options):
            self.assertTrue(options.get('browser'))
            return SafeHTTP(platform)
        with (patch('video_worker.SafeHTTP', side_effect=transport),
              patch('video_http.httpx.Client', side_effect=client),
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

    def test_real_extractor_download_mode_uses_only_pinned_cdn_transport(self):
        def media(request):
            if request.headers['host'].endswith('.bilivideo.com'):
                self.assertNotIn('cookie', request.headers)
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, headers={'content-type': 'video/mp4'}, content=b'media')
            response = self.fixture(request)
            if request.url.path == '/x/player/wbi/playurl':
                body = response.json()
                for fmt in body['data']['dash']['video']:
                    fmt.update(width=640, height=360, bandwidth=1000, baseUrl='https://cn-fixture.bilivideo.com/video?secret')
                for fmt in body['data']['dash']['audio']:
                    fmt['baseUrl'] = 'https://cn-fixture.bilivideo.com/audio?secret'
                    fmt['bandwidth'] = 1000
                return httpx.Response(200, json=body)
            return response
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_fixture({'source': BV, 'part': 2, 'materialize': True,
                'directory': tmp, 'max_bytes': 10000, 'max_duration': 600}, media)
            self.assertEqual(result['artifacts']['files'], ['input0.mp4', 'input1.mp4'])
            self.assertEqual((Path(tmp) / 'input0.mp4').read_bytes(), b'media')
            self.assertNotIn('secret', json.dumps(result))

    def test_embedded_playback_reuse_requires_matching_part(self):
        page = self.fixture
        network = []
        def embedded(request):
            network.append(request.url.path)
            response = page(request)
            if request.url.path.startswith('/video/'):
                play = {'code': 0, 'data': {'cid': 11, 'timelength': 12000, 'dash': {
                    'video': [{'baseUrl': 'https://media.example/video?secret', 'mimeType': 'video/mp4',
                               'codecs': 'avc1', 'id': 80, 'height': 720, 'width': 1280}],
                    'audio': [{'baseUrl': 'https://media.example/audio?secret', 'mimeType': 'audio/mp4',
                               'codecs': 'mp4a', 'id': 30280}]}}}
                return httpx.Response(200, text=response.text + '<script>window.__playinfo__=' + json.dumps(play) + ';</script>')
            return response
        result = self.run_fixture({'source': BV, 'part': 2}, embedded)
        self.assertEqual(result['content_id'], BV)
        self.assertEqual(network, [f'/video/{BV}'])
        self.assertNotIn('secret', json.dumps(result))
        def wrong_part(request):
            response = embedded(request)
            if request.url.path.startswith('/video/'):
                return httpx.Response(200, text=response.text.replace('"cid": 11, "timelength"', '"cid": 10, "timelength"'))
            return response
        network.clear()
        self.run_fixture({'source': BV, 'part': 2}, wrong_part)
        self.assertIn('/x/player/wbi/playurl', network)
        self.assertNotIn('/x/web-interface/view', network)
        self.assertNotIn('/x/player/pagelist', network)
        self.assertNotIn('/x/player/wbi/v2', network)

    def test_anonymous_nav_can_supply_signing_keys_but_playback_auth_is_preserved(self):
        def nav(request):
            response = self.fixture(request)
            if request.url.path == '/x/web-interface/nav':
                body = response.json()
                body['code'] = -101
                return httpx.Response(200, json=body)
            return response
        result = self.run_fixture({'source': BV, 'part': 2}, nav)
        self.assertEqual(result['status'], 'ok')
        def restricted(request):
            if request.url.path == '/x/player/wbi/playurl':
                return httpx.Response(200, json={'code': -101, 'message': 'private'})
            return nav(request)
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': BV, 'part': 2}, restricted)
        self.assertEqual(error.exception.code, 'AUTH_REQUIRED')

    def test_initial_state_validation_precedes_any_playback_request(self):
        for mutation, category in ((lambda d: d.update(bvid='BV1GJ411x7h7'), 'EXTRACTOR_BROKEN'),
                                   (lambda d: d.update(pages=[]), 'EXTRACTOR_BROKEN'),
                                   (lambda d: d.update(pages=[{'cid': 10}]), 'INVALID_SOURCE'),
                                   (lambda d: d.update(rights={'is_stein_gate': True}), 'UNSUPPORTED_SOURCE')):
            called = []
            def malformed(request, called=called, mutation=mutation):
                called.append(request.url.path)
                self.assertTrue(request.url.path.startswith('/video/'))
                response = self.fixture(request)
                state = json.loads(response.text.split('=', 1)[1].split(';</script>', 1)[0])
                mutation(state['videoData'])
                return httpx.Response(200, text='<script>window.__INITIAL_STATE__=' + json.dumps(state) + ';</script>')
            with self.subTest(category=category), self.assertRaises(VideoError) as error:
                self.run_fixture({'source': BV, 'part': 2}, malformed)
            self.assertEqual(error.exception.code, category)
            self.assertEqual(len(called), 1)

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

    def test_shortlink_block_is_reported_at_the_actual_stage(self):
        calls = []
        def metadata_block(request):
            calls.append((request.headers['host'], request.url.path))
            if request.headers['host'] == 'b23.tv':
                return httpx.Response(302, headers={'location': f'https://www.bilibili.com/video/{BV}?p=2'})
            if request.url.path.startswith('/video/'):
                return httpx.Response(412, text='secret upstream details')
            raise AssertionError('Unexpected request before metadata')
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://b23.tv/test'}, metadata_block)
        result = error.exception.result()['error']
        self.assertEqual((result['code'], result['stage'], result['http_status']),
                         ('UPSTREAM_BLOCKED', 'extractor', 412))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('secret', json.dumps(result))
        for status, code in ((401, 'AUTH_REQUIRED'), (404, 'NOT_FOUND'),
                             (412, 'UPSTREAM_BLOCKED'), (429, 'RATE_LIMITED')):
            with self.subTest(status=status), self.assertRaises(VideoError) as error:
                self.run_fixture({'source': 'https://b23.tv/test'},
                                 lambda request, status=status: httpx.Response(status))
            result = error.exception.result()['error']
            self.assertEqual((result['code'], result['stage'], result['http_status']),
                             (code, 'shortlink', status))

    def test_platform_json_block_is_not_mislabeled_as_missing_cookies(self):
        def blocked(request):
            if request.headers['host'] == 'b23.tv':
                return httpx.Response(302, headers={'location': f'https://www.bilibili.com/video/{BV}'})
            if request.url.path == '/x/player/wbi/playurl':
                return httpx.Response(200, json={'code': -412, 'message': 'secret platform text'})
            return self.fixture(request)
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://b23.tv/test'}, blocked)
        result = error.exception.result()['error']
        self.assertEqual(result['code'], 'UPSTREAM_BLOCKED')
        self.assertEqual(result['stage'], 'extractor')
        self.assertNotIn('http_status', result)
        self.assertNotIn('secret', json.dumps(result))

    def test_valid_shortlink_does_not_duplicate_extractor_page_request(self):
        pages = []
        def blocked_page(request):
            if request.headers['host'] == 'b23.tv':
                return httpx.Response(302, headers={'location': f'https://www.bilibili.com/video/{BV}?p=2'})
            if request.url.path.startswith('/video/'):
                pages.append(request.url.path)
                return httpx.Response(412)
            return self.fixture(request)
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://b23.tv/test'}, blocked_page)
        self.assertEqual(error.exception.code, 'UPSTREAM_BLOCKED')
        self.assertEqual(error.exception.stage, 'extractor')
        self.assertEqual(error.exception.http_status, 412)
        self.assertEqual(len(pages), 1)

    def test_douyin_shortlink_resolves_without_visiting_video_page(self):
        calls = []
        def douyin(request):
            calls.append((request.headers['host'], request.url.path))
            if request.headers['host'] == 'v.douyin.com':
                return httpx.Response(302, headers={'location': 'https://www.douyin.com/video/123'})
            self.assertEqual(request.url.path, '/aweme/v1/web/aweme/detail/')
            return httpx.Response(412)
        with self.assertRaises(VideoError) as error:
            self.run_fixture({'source': 'https://v.douyin.com/test/'}, douyin)
        self.assertEqual(error.exception.stage, 'extractor')
        self.assertEqual(error.exception.http_status, 412)
        self.assertEqual(len(calls), 2)

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
