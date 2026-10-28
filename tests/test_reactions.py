from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from group_context import GroupContextBuffer  # noqa: E402
from main import AppConfig, Bot, load_config  # noqa: E402
from onebot import MessageEvent, OneBotError, OneBotRejected, OneBotWSClient  # noqa: E402
from video_platforms import contains_video_link  # noqa: E402
from video_resolver import VideoSettings  # noqa: E402

URL = 'https://b23.tv/zeYynMt'
DOUYIN = 'https://www.douyin.com/video/6961737553342991651'
BV = 'BV1xx411c7mD'


def event(**overrides):
    values = dict(message_type='private', user_id=7, message_id=123, text=URL,
                  at_me=False, sender_name='someone')
    values.update(overrides)
    return MessageEvent(**values)


def make_bot():
    bot = object.__new__(Bot)
    bot.config = AppConfig()
    bot.config.onebot.self_id = 999
    bot.onebot = SimpleNamespace(set_msg_emoji_like=AsyncMock(return_value={}))
    bot._names = {}
    bot._locks = {}
    bot._global = asyncio.Semaphore(3)
    bot._own_messages = {}
    bot._log_chat_event = Mock()
    bot.relations = None
    bot.story = None
    bot.commands = SimpleNamespace(is_command=Mock(return_value=False), handle=AsyncMock())
    bot._reply = AsyncMock(return_value=None)
    bot._maybe_reload_model = AsyncMock()
    bot.ensure_card = AsyncMock(return_value=True)
    bot._addresses_bot = Mock(return_value=False)
    bot._maybe_reply_proactively = AsyncMock()
    bot.group_context = GroupContextBuffer(10)
    return bot


class DetectionAndConfigTests(unittest.TestCase):
    def test_supported_links_share_text_multiple_candidates_and_false_positives(self):
        for text in (URL, DOUYIN, 'https://v.douyin.com/abc123/',
                     f'https://www.bilibili.com/video/{BV}?p=2',
                     '【分享文案】 ' + URL + '。',
                     'https://example.com/info ' + URL, URL + ' ' + DOUYIN):
            with self.subTest(text=text):
                self.assertTrue(contains_video_link(text))
        for text in ('', BV, 'av123', 'bilibili.com',
                     f'https://www.bilibili.com.evil.test/video/{BV}',
                     f'https://www.bilibili.com@evil.test/video/{BV}',
                     f'https://user:password@www.bilibili.com/video/{BV}',
                     f'http://www.bilibili.com/video/{BV}',
                     'https://127.0.0.1/a', 'https://www.bilibili.com/',
                     'https://www.douyin.com/user/123', 'https://live.douyin.com/123',
                     'https://www.douyin.com/note/123', 'https://b23.tv/',
                     '[视频]', 'x' * 4096 + URL):
            with self.subTest(text=text):
                self.assertFalse(contains_video_link(text))

    def test_config_default_custom_disable_and_invalid_emoji(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text('', encoding='utf-8')
            self.assertTrue(load_config(path).video.reaction_enabled)
            self.assertEqual(load_config(path).video.reaction_emoji_id, '76')
            path.write_text('[video]\nreaction_enabled=false\nreaction_emoji_id="124"\n', encoding='utf-8')
            self.assertFalse(load_config(path).video.reaction_enabled)
            self.assertEqual(load_config(path).video.reaction_emoji_id, '124')
            for value in ('true', '76', '""', '"0"', '"-1"', '"１２４"', '"abc"',
                          '"12345678901"', '[]'):
                path.write_text(f'[video]\nreaction_emoji_id={value}\n', encoding='utf-8')
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reaction_emoji_id'):
                    load_config(path)
        for value in (None, True, 76, '', '0', '-1', '76.0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                VideoSettings(reaction_emoji_id=value)


class OneBotReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_add_remove_timeout_and_error_propagation(self):
        client = object.__new__(OneBotWSClient)
        client.call = AsyncMock(return_value={'result': True})
        self.assertEqual(await client.set_msg_emoji_like(123, '76'), {'result': True})
        client.call.assert_awaited_with('set_msg_emoji_like', {
            'message_id': 123, 'emoji_id': '76', 'set': True,
        }, timeout=2)
        await client.set_msg_emoji_like(123, '124', set=False, timeout=1)
        client.call.assert_awaited_with('set_msg_emoji_like', {
            'message_id': 123, 'emoji_id': '124', 'set': False,
        }, timeout=1)
        for error in (OneBotRejected('unsupported'), TimeoutError('timeout')):
            client.call.side_effect = error
            with self.assertRaises(type(error)):
                await client.set_msg_emoji_like(123, '76')

    async def test_invalid_parameters_never_call_api(self):
        client = object.__new__(OneBotWSClient)
        client.call = AsyncMock()
        for message, emoji, toggle in ((0, '76', True), (True, '76', True),
                                       (123, '', True), (123, 76, True),
                                       (123, '76', 'false')):
            with self.subTest(message=message, emoji=emoji, toggle=toggle), self.assertRaises(ValueError):
                await client.set_msg_emoji_like(message, emoji, set=toggle)
        client.call.assert_not_awaited()


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_precedes_reply_and_command_without_changing_admission(self):
        bot = make_bot()
        order = []
        async def reaction(*args, **kwargs):
            order.append('reaction')
        async def reply(*args):
            order.append('reply')
        bot.onebot.set_msg_emoji_like.side_effect = reaction
        bot._reply.side_effect = reply
        await bot.handle(event())
        self.assertEqual(order, ['reaction', 'reply'])
        bot.onebot.set_msg_emoji_like.assert_awaited_once_with(123, '76', set=True, timeout=2)
        bot.commands.is_command.return_value = True
        bot.commands.handle.side_effect = lambda *args: order.append('command')
        await bot.handle(event(message_id=124, text='/command ' + URL))
        self.assertEqual(order[-2:], ['reaction', 'command'])

    async def test_unaddressed_group_reacts_even_if_parsing_and_douyin_disabled(self):
        bot = make_bot()
        bot.config.video.enabled = False
        bot.config.video.douyin_enabled = False
        await bot.handle(event(message_type='group', group_id=42, text=DOUYIN))
        bot.onebot.set_msg_emoji_like.assert_awaited_once_with(123, '76', set=True, timeout=2)
        bot._reply.assert_not_awaited()
        bot._maybe_reload_model.assert_not_awaited()
        self.assertEqual(len(bot.group_context.peek(42)), 1)

    async def test_skip_own_synthetic_invalid_group_and_disabled(self):
        bot = make_bot()
        for message in (event(user_id=999), event(message_id=0), event(message_id=-1),
                        event(user_id=0), event(message_type='notice'),
                        event(message_type='group', group_id=None),
                        event(message_type='group', group_id=0), event(text='hello'),
                        event(text=BV)):
            await bot._maybe_react_video_link(message, message.text)
        bot.config.video.reaction_enabled = False
        await bot._maybe_react_video_link(event(), URL)
        bot.onebot.set_msg_emoji_like.assert_not_awaited()

    async def test_multiple_links_dedupe_and_same_id_different_conversations(self):
        bot = make_bot()
        await bot._maybe_react_video_link(event(), URL + ' ' + DOUYIN)
        await bot._maybe_react_video_link(event(), URL)
        for group in (42, 43):
            message = event(message_type='group', group_id=group)
            await bot._maybe_react_video_link(message, URL)
        self.assertEqual(bot.onebot.set_msg_emoji_like.await_count, 3)
        self.assertEqual(len(bot._reaction_seen), 3)

    async def test_rejected_and_timeout_are_sanitized_nonblocking_and_not_retried(self):
        for failure in (OneBotRejected(URL + ' message-secret'), OneBotError(URL), TimeoutError(URL)):
            bot = make_bot()
            bot.onebot.set_msg_emoji_like.side_effect = failure
            with self.assertLogs('qqbot', level='WARNING') as logs:
                await bot.handle(event())
            self.assertNotIn(URL, '\n'.join(logs.output))
            self.assertNotIn('message-secret', '\n'.join(logs.output))
            bot._reply.assert_awaited_once()
            await bot._maybe_react_video_link(event(), URL)
            bot.onebot.set_msg_emoji_like.assert_awaited_once()
            self.assertEqual(bot._reaction_inflight, 0)

    async def test_concurrent_duplicates_inflight_limit_and_cancellation(self):
        bot = make_bot()
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        bot.onebot.set_msg_emoji_like.side_effect = blocked
        task = asyncio.create_task(bot._maybe_react_video_link(event(), URL))
        await entered.wait()
        await bot._maybe_react_video_link(event(), URL)
        tasks = [task]
        for message_id in (124, 125, 126):
            tasks.append(asyncio.create_task(bot._maybe_react_video_link(event(message_id=message_id), URL)))
        await asyncio.sleep(0)
        await bot._maybe_react_video_link(event(message_id=127), URL)
        self.assertEqual(bot.onebot.set_msg_emoji_like.await_count, 4)
        self.assertEqual(bot._reaction_inflight, 4)
        for pending in tasks:
            pending.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertEqual(bot._reaction_inflight, 0)
        # Busy/skipped message is claimed, not automatically retried later.
        await bot._maybe_react_video_link(event(message_id=127), URL)
        self.assertEqual(bot.onebot.set_msg_emoji_like.await_count, 4)

    async def test_actual_deadline_and_bounded_dedupe(self):
        bot = make_bot()
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        bot.onebot.set_msg_emoji_like.side_effect = blocked
        with self.assertLogs('qqbot', level='WARNING'):
            await asyncio.wait_for(bot._maybe_react_video_link(event(), URL), 3)
        self.assertEqual(bot._reaction_inflight, 0)
        bot.onebot.set_msg_emoji_like.side_effect = None
        bot.config.video.reaction_emoji_id = '124'
        for message_id in range(1000, 1300):
            await bot._maybe_react_video_link(event(message_id=message_id), URL)
        self.assertEqual(len(bot._reaction_seen), 256)
        self.assertNotIn(('private', 7, 123), bot._reaction_seen)
        bot.onebot.set_msg_emoji_like.assert_awaited_with(1299, '124', set=True, timeout=2)


if __name__ == '__main__':
    unittest.main()
