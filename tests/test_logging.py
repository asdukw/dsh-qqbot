from __future__ import annotations

import ast
import asyncio
import io
import logging
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import httpx
from websockets.client import ClientProtocol
from websockets.uri import parse_uri

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from dsh_client import DSHClient, DSHConfig, DSHError  # noqa: E402
from group_context import GroupContextBuffer  # noqa: E402
from llm import LLMClient, LLMConfig, LLMError  # noqa: E402
from log_context import get_logger, request_context  # noqa: E402
from main import AppConfig, Bot  # noqa: E402
from onebot import MessageEvent, OneBotConfig, OneBotError, OneBotWSClient  # noqa: E402
from stickers import StickerError, download_image  # noqa: E402
from voice import VoiceError, synthesize_fish, synthesize_stepfun  # noqa: E402

SECRET = "private-credential-sentinel"
URL = f"https://user:{SECRET}@example.invalid/image?token={SECRET}#fragment"


def request_ids(lines: list[str]) -> set[str]:
    return set(re.findall(r"rid=([0-9a-f]{12})(?=\s)", "\n".join(lines)))


class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_nested_turns_and_worker_threads(self) -> None:
        log = get_logger("test.context")

        @request_context
        async def nested(label: str) -> None:
            log.info("nested %s", label)
            await asyncio.to_thread(log.info, "worker %s", label)

        @request_context
        async def turn(label: str) -> None:
            log.info("start %s", label)
            await asyncio.sleep(0)
            await nested(label)
            log.info("end %s", label)

        with self.assertLogs("test.context", level="INFO") as logs:
            await asyncio.gather(turn("first"), turn("second"))
            log.info("outside")
        first = [line for line in logs.output if "first" in line]
        second = [line for line in logs.output if "second" in line]
        self.assertEqual(len(first), 4)
        self.assertEqual(len(request_ids(first)), 1)
        self.assertEqual(len(request_ids(second)), 1)
        self.assertTrue(request_ids(first).isdisjoint(request_ids(second)))
        self.assertIn("rid=- outside", logs.output[-1])

    async def test_reset_after_exception_and_cancellation(self) -> None:
        log = get_logger("test.context")
        entered = asyncio.Event()

        @request_context
        async def failed() -> None:
            log.info("failed turn")
            raise ValueError("expected")

        @request_context
        async def cancelled() -> None:
            log.info("cancelled turn")
            entered.set()
            await asyncio.Event().wait()

        with self.assertLogs("test.context", level="INFO") as logs:
            with self.assertRaises(ValueError):
                await failed()
            log.info("after failure")
            task = asyncio.create_task(cancelled())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            log.info("after cancellation")
        self.assertIn("rid=- after failure", logs.output[1])
        self.assertIn("rid=- after cancellation", logs.output[-1])
        self.assertEqual(len(request_ids(logs.output)), 2)

    async def test_onebot_outer_error_keeps_the_turn_id(self) -> None:
        async def handler(event: MessageEvent) -> None:
            get_logger("qqbot").info("handler start")
            raise RuntimeError("expected")

        client = OneBotWSClient(OneBotConfig(), handler)
        event = MessageEvent("private", 7, 0, "hi", False, "test")
        with self.assertLogs(level="INFO") as logs:
            await client._run_handler(event)
            get_logger("onebot").info("outside")
        correlated = [line for line in logs.output if "outside" not in line]
        self.assertEqual(len(request_ids(correlated)), 1)
        self.assertTrue(all("rid=-" not in line for line in correlated))
        self.assertIn("message handler failed", "\n".join(correlated))
        self.assertIn("rid=- outside", logs.output[-1])


class ReplyCorrelationTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> Bot:
        bot = object.__new__(Bot)
        bot.config = AppConfig()
        bot.config.bot.greeting_on_first_chat = False
        bot._names = {}
        bot._locks = {}
        bot._global = asyncio.Semaphore(3)
        bot._own_messages = {}
        bot._log_chat_event = Mock()
        bot.relations = None
        bot.conversations = None
        bot.commands = SimpleNamespace(is_command=Mock(return_value=False))
        bot.ensure_card = AsyncMock(return_value=True)
        bot._maybe_reload_model = AsyncMock()
        bot._session_for = Mock(return_value=("private-7", False))
        bot._turn_images = Mock(return_value=[])
        bot._turn_prompt = Mock(return_value=("prompt-sentinel", 0))
        bot._recall_block = AsyncMock(return_value="")
        bot._record_dsh_usage = Mock()
        bot._touch_session = Mock()
        bot._sticker_hint = Mock(return_value="")
        bot._context_header = Mock(return_value="header")
        bot.memory = SimpleNamespace(add=AsyncMock(), history=AsyncMock(return_value=[]))
        bot._card = SimpleNamespace(data={}, name="test", lorebook=SimpleNamespace(select=Mock(return_value=[])))
        bot.group_context = GroupContextBuffer(10)
        bot.proactive = SimpleNamespace(record=Mock(), note_speaking=Mock())
        bot.onebot = SimpleNamespace(
            send_private=AsyncMock(side_effect=OneBotError("test transport unavailable")),
            send_group=AsyncMock(return_value={"message_id": 1}),
        )

        async def send_reply(event, conversation_id, reply, **kwargs):
            await bot._send(event, reply)

        bot._send_reply = send_reply
        return bot

    async def test_dsh_worker_and_send_failure_share_id_through_handle(self) -> None:
        bot = self.make_bot()

        def run(session, prompt, images):
            self.assertNotIn("rid=", prompt)
            get_logger("dsh_client").info("worker generation")
            return "reply-sentinel", []

        bot.dsh = SimpleNamespace(run_with_events=run)
        event = MessageEvent("private", 7, 0, "message-sentinel", False, "name-sentinel")
        with self.assertLogs(level="INFO") as logs:
            await bot.handle(event)
        self.assertEqual(len(request_ids(logs.output)), 1)
        output = "\n".join(logs.output)
        for secret in ("message-sentinel", "reply-sentinel", "name-sentinel"):
            self.assertNotIn(secret, output)
        self.assertNotIn("rid=-", output)
        self.assertIn("worker generation", output)
        self.assertIn("failed to send message", output)
        self.assertIn("reply processing finished", output)
        self.assertNotIn("reply sent:", output)
        bot.onebot.send_private.assert_awaited_once_with(7, "reply-sentinel")

    async def test_poke_reply_outer_error_keeps_id_and_resets(self) -> None:
        bot = self.make_bot()
        bot.config.onebot.self_id = 999
        bot._poke_last = {}
        bot._poke_cooldown = 0

        def run(session, prompt, images):
            get_logger("dsh_client").info("poke generation")
            return "reply-sentinel", []

        bot.dsh = SimpleNamespace(run_with_events=run)
        client = OneBotWSClient(bot.config.onebot, bot.handle, on_notice=bot.handle_notice)
        bot.onebot = client
        payload = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "user_id": 7, "target_id": 999,
        }
        seen: set[str] = set()
        for _ in range(2):
            bot.memory.add = AsyncMock(side_effect=[None, sqlite3.OperationalError("test history failure")])
            with self.assertLogs(level="INFO") as logs:
                await client._run_notice(payload)
                get_logger("onebot").info("outside poke")
            correlated = logs.output[:-1]
            ids = request_ids(correlated)
            self.assertEqual(len(ids), 1)
            self.assertTrue(ids.isdisjoint(seen))
            seen.update(ids)
            self.assertTrue(all("rid=-" not in line for line in correlated))
            self.assertTrue(any("poke generation" in line for line in correlated))
            self.assertTrue(any("notice handler failed" in line for line in correlated))
            self.assertTrue(any("OperationalError" in line for line in correlated))
            self.assertIn("rid=- outside poke", logs.output[-1])
            self.assertEqual(bot.memory.add.await_count, 2)

    async def test_llm_retry_and_error_share_id(self) -> None:
        bot = self.make_bot()
        bot.dsh = None
        bot.config.bot.notify_errors = False

        async def chat(*args, **kwargs):
            get_logger("llm").warning("LLM timeout (attempt 1/1)")
            raise LLMError("LLM request timed out")

        bot.client = SimpleNamespace(chat=chat)
        event = MessageEvent("private", 7, 0, "message-sentinel", False, "test")
        with patch("main.build_messages", return_value=[]), self.assertLogs(level="INFO") as logs:
            await bot.handle(event)
        self.assertEqual(len(request_ids(logs.output)), 1)
        output = "\n".join(logs.output)
        self.assertIn("LLM timeout", output)
        self.assertIn("LLM failed", output)
        self.assertNotIn("rid=-", output)
        self.assertNotIn("reply processing finished", output)

    async def test_llm_success_completion_has_same_id(self) -> None:
        bot = self.make_bot()
        bot.dsh = None
        bot.client = SimpleNamespace(chat=AsyncMock(return_value="reply-sentinel"))
        event = MessageEvent("private", 7, 0, "message-sentinel", False, "test")
        with patch("main.build_messages", return_value=[]), self.assertLogs(level="INFO") as logs:
            await bot._reply_llm("private:7", event, event.text)
        self.assertEqual(len(request_ids(logs.output)), 1)
        self.assertIn("reply processing finished", "\n".join(logs.output))
        self.assertIn("failed to send message", "\n".join(logs.output))
        self.assertNotIn("reply sent:", "\n".join(logs.output))

    async def test_proactive_and_forced_turns_are_correlated(self) -> None:
        bot = self.make_bot()

        def run(session, prompt, images):
            get_logger("dsh_client").info("worker generation")
            return "reply-sentinel", []

        bot.dsh = SimpleNamespace(run_with_events=run)
        event = MessageEvent("group", 7, 0, "trigger", False, "test", group_id=42)
        with self.assertLogs(level="INFO") as logs:
            await bot._reply_proactive("group:42", event)
        self.assertEqual(len(request_ids(logs.output)), 1)
        self.assertNotIn("rid=-", "\n".join(logs.output))
        self.assertIn("proactive reply processing finished", "\n".join(logs.output))
        with self.assertLogs(level="INFO") as forced:
            await bot.force_proactive(event)
        self.assertEqual(len(request_ids(forced.output)), 1)
        self.assertTrue(request_ids(logs.output).isdisjoint(request_ids(forced.output)))


class DiagnosticLoggingTests(unittest.TestCase):
    def test_scripts_keep_application_debug_but_hide_handshake_secrets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for filename in ("check_debug.py", "ws_client_test.py"):
            with self.subTest(script=filename):
                source = root / "deploy" / "napcat" / "diagnostics" / filename
                tree = ast.parse(source.read_text(encoding="utf-8"))
                # Only execute logging setup, never config loading or the script.
                setup = [node for node in tree.body if isinstance(node, ast.Expr)
                         and isinstance(node.value, ast.Call)
                         and ast.unparse(node.value.func).startswith("logging.")]
                capture = io.StringIO()
                handler = logging.StreamHandler(capture)
                parent = logging.getLogger()
                names = ("websockets", "websockets.client", "httpx", "test.diagnostic")
                saved = {name: (logging.getLogger(name).level, logging.getLogger(name).handlers[:],
                                logging.getLogger(name).propagate) for name in names}
                old_handlers, old_level = parent.handlers[:], parent.level
                try:
                    parent.handlers = [handler]
                    parent.setLevel(logging.DEBUG)
                    for name in names:
                        logger = logging.getLogger(name)
                        logger.setLevel(logging.NOTSET)
                        logger.handlers = []
                        logger.propagate = True
                    exec(compile(ast.Module(body=setup, type_ignores=[]), str(source), "exec"), {"logging": logging})
                    token = "fake/token+value"
                    url = f"ws://localhost:3001?access_token={quote(token, safe='')}"
                    protocol = ClientProtocol(parse_uri(url))
                    request = protocol.connect()
                    request.headers["Authorization"] = f"Bearer {token}"
                    protocol.send_request(request)
                    logging.getLogger("test.diagnostic").debug("application debug")
                    logging.getLogger("websockets.client").warning("transport warning")
                    output = capture.getvalue()
                    self.assertNotIn(token, output)
                    self.assertNotIn(quote(token, safe=""), output)
                    self.assertNotIn("Authorization", output)
                    self.assertIn("application debug", output)
                    self.assertIn("transport warning", output)
                finally:
                    parent.handlers, parent.level = old_handlers, old_level
                    for name, (level, handlers, propagate) in saved.items():
                        logger = logging.getLogger(name)
                        logger.setLevel(level)
                        logger.handlers, logger.propagate = handlers, propagate


class ErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_images_do_not_log_url_or_transport_error_text(self) -> None:
        bot = object.__new__(Bot)
        bot.config = AppConfig()
        for method in (bot._fetch_image, bot._image_urls):
            for failure in (httpx.Response(403), httpx.ConnectError(f"failed {URL}")):
                with self.subTest(method=method.__name__, failure=type(failure).__name__):
                    get = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
                    with patch("main.httpx.AsyncClient") as client, self.assertLogs("qqbot", level="WARNING") as logs:
                        client.return_value.__aenter__.return_value.get = get
                        await method([URL] if method.__name__ == "_image_urls" else URL)
                    self.assertNotIn(SECRET, "\n".join(logs.output))
                    self.assertNotIn("example.invalid", "\n".join(logs.output))
                    get.assert_awaited_once_with(URL)

    async def test_media_logs_keep_id_but_not_meme_or_sticker_text(self) -> None:
        bot = object.__new__(Bot)
        bot.config = AppConfig()
        bot._meme_last = {}
        bot._sticker_sends = {}
        bot._sticker_last = {}
        bot.config.stickers.send = True
        bot.onebot = SimpleNamespace(send_private_image=AsyncMock(return_value={"message_id": 1}))
        event = MessageEvent("private", 7, 0, "hi", False, "test")
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "image.png"
            image.write_bytes(b"fake image")
            record = SimpleNamespace(id=3, path=str(image))
            bot.stickers = SimpleNamespace(pick=Mock(return_value=record), mark_used=Mock())

            @request_context
            async def media_turn() -> None:
                await bot._send_sticker(event, SECRET)
                bot.stickers = None
                with patch("main.render_meme", return_value=image):
                    self.assertTrue(await bot._send_meme(event, SECRET))

            with self.assertLogs("qqbot", level="INFO") as logs:
                await media_turn()
        self.assertEqual(len(request_ids(logs.output)), 1)
        output = "\n".join(logs.output)
        self.assertIn("sent sticker #3", output)
        self.assertIn("meme sent", output)
        self.assertNotIn(SECRET, output)
        self.assertEqual(bot.onebot.send_private_image.await_count, 2)

    async def test_sticker_download_errors_do_not_echo_url(self) -> None:
        for failure in (httpx.Response(403), httpx.ConnectError(f"failed {URL}")):
            with patch("stickers.httpx.AsyncClient") as client:
                client.return_value.__aenter__.return_value.get = (
                    AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
                )
                with self.assertRaises(StickerError) as error:
                    await download_image(URL)
            self.assertNotIn(SECRET, str(error.exception))
            self.assertRegex(str(error.exception), r"403|ConnectError")

    async def test_llm_errors_keep_status_or_type_without_body(self) -> None:
        client = LLMClient(LLMConfig(base_url="https://example.invalid/v1", model="fake", provider="custom"))
        try:
            for response in (httpx.Response(401, text=SECRET), httpx.Response(503, text=SECRET), httpx.Response(200, text=SECRET)):
                client._client.post = AsyncMock(return_value=response)
                with self.assertRaises(LLMError) as error:
                    await client.chat([], attempts=1)
                self.assertNotIn(SECRET, str(error.exception))
            client._client.post = AsyncMock(side_effect=httpx.ConnectError(SECRET))
            with self.assertLogs("llm", level="WARNING") as logs, self.assertRaises(LLMError) as error:
                await client.chat([], attempts=1)
            self.assertIn("ConnectError", str(error.exception))
            self.assertNotIn(SECRET, str(error.exception) + "\n".join(logs.output))
        finally:
            await client.close()

    async def test_tts_errors_do_not_echo_body_or_url(self) -> None:
        with patch.dict(os.environ, {"TEST_TTS_KEY": "fake-key"}):
            for synth, kwargs in ((synthesize_fish, {"reference_id": "fake"}), (synthesize_stepfun, {"voice": "fake"})):
                for failure in (httpx.Response(401, text=SECRET), httpx.ConnectError(URL)):
                    with patch("voice.httpx.AsyncClient") as client:
                        client.return_value.__aenter__.return_value.post = (
                            AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
                        )
                        with self.assertRaises(VoiceError) as error:
                            await synth("hello", api_key_env="TEST_TTS_KEY", **kwargs)
                    self.assertNotIn(SECRET, str(error.exception))
                    self.assertRegex(str(error.exception), r"401|ConnectError")

    async def test_onebot_retcode_cannot_echo_response_data(self) -> None:
        client = OneBotWSClient(OneBotConfig(), AsyncMock())
        for retcode in (123, SECRET):
            async def send(data, code=retcode):
                client._resolve({"echo": str(client._echo), "retcode": code, "data": SECRET})

            client._ws = SimpleNamespace(send=send)
            with self.assertRaises(OneBotError) as error:
                await client.call("send_private_msg", {})
            self.assertNotIn(SECRET, str(error.exception))
            self.assertIn("123" if isinstance(retcode, int) else "unknown", str(error.exception))
            self.assertEqual(client._pending, {})

    async def test_dsh_runtime_error_log_does_not_echo_exception(self) -> None:
        client = DSHClient(DSHConfig())
        client._harness = SimpleNamespace(run=Mock(side_effect=RuntimeError(SECRET)), close=Mock())
        with self.assertLogs("dsh_client", level="ERROR") as logs, self.assertRaises(DSHError) as error:
            await asyncio.to_thread(client.run, "test", "hello")
        self.assertIn("RuntimeError", str(error.exception))
        self.assertNotIn(SECRET, str(error.exception) + "\n".join(logs.output))
        self.assertIsNone(client._harness)


if __name__ == "__main__":
    unittest.main()
