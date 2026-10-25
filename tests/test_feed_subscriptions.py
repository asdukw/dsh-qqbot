from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from feeds import (  # noqa: E402
    FeedError,
    FeedPoller,
    FeedStore,
    feed_log_text,
    feed_log_url,
    fetch_feed,
)
from main import AppConfig, Bot  # noqa: E402
from onebot import MessageEvent  # noqa: E402

PRIVATE_URL = "https://feed-user:feed-password@example.com/feed?token=private-token#private-fragment"
SECRETS = ("feed-user", "feed-password", "private-token", "private-fragment")


class DiagnosticTests(unittest.TestCase):
    def assert_safe(self, text: str) -> None:
        self.assertIn("example.com/feed", text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_log_url_keeps_route_but_removes_secrets(self) -> None:
        self.assertEqual(feed_log_url(PRIVATE_URL), "https://example.com/feed?[redacted]")
        self.assertEqual(
            feed_log_url("https://user:password@[::1]:8080/feed?token=secret#fragment"),
            "https://[::1]:8080/feed?[redacted]",
        )
        self.assertEqual(feed_log_url("https://example.com/feed"), "https://example.com/feed")
        self.assertEqual(feed_log_url("https://[invalid"), "[invalid feed URL]")

    def test_text_redacts_encoded_and_decoded_secrets(self) -> None:
        url = "https://reader:p%40ssword@example.com/feed?token=private%2Btoken"
        text = f"failed {url}: p@ssword, private+token, private%2Btoken"
        safe = feed_log_text(text, url)
        for secret in ("reader", "p%40ssword", "p@ssword", "private+token", "private%2Btoken"):
            self.assertNotIn(secret, safe)
        self.assertIn("example.com/feed", safe)

    def fetch_error(self, body: str) -> FeedError:
        response = httpx.Response(503, text=body, headers={"Content-Type": "text/html"})
        with patch("feeds.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
            with self.assertRaises(FeedError) as raised:
                asyncio.run(fetch_feed("https://example.com/feed"))
        return raised.exception

    def test_tagged_error_skips_long_styles_and_header(self) -> None:
        body = (
            "<html><head><style>" + "body { color: red; }" * 40 + "</style></head>"
            "<body><header>" + "RSSHub header " * 40 + "</header>"
            "<strong>Error Message:</strong><pre><code>request was banned (-412) &amp; retry</code></pre>"
            "<strong>Route:</strong><pre>/bilibili/user/dynamic/:uid</pre></body></html>"
        )
        error = self.fetch_error(body)
        self.assertEqual(error.status, 503)
        self.assertEqual(error.detail, "request was banned (-412) & retry")

    def test_message_separated_by_tag_and_newline(self) -> None:
        error = self.fetch_error("<pre>Error Message:\n<code>upstream banned -412</code></pre>")
        self.assertEqual(error.detail, "upstream banned -412")

    def test_fallback_is_readable_and_bounded(self) -> None:
        error = self.fetch_error("<style>hidden</style><p>busy &amp; retry " + "x" * 400 + "</p>")
        self.assertTrue(error.detail.startswith("busy & retry"))
        self.assertEqual(len(error.detail), 300)
        self.assertNotIn("hidden", error.detail)

    def test_empty_label_does_not_produce_empty_detail(self) -> None:
        error = self.fetch_error("<b>Error Message:</b>\n<em> </em><b>Route:</b>/feed")
        self.assertTrue(error.detail)

    def test_retry_logs_redact_url_and_echoed_credentials(self) -> None:
        with patch("feeds.httpx.AsyncClient") as client, patch(
            "feeds.asyncio.sleep", new=AsyncMock()
        ), self.assertLogs("feeds", level="WARNING") as logs:
            client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=[
                httpx.ConnectError(f"failed {PRIVATE_URL} token private-token"),
                httpx.Response(200, content=b"<rss/>"),
            ])
            asyncio.run(fetch_feed(PRIVATE_URL, retries=1))
        text = "\n".join(logs.output)
        self.assertIn("ConnectError", text)
        self.assert_safe(text)

    def test_polling_failure_logs_redact_url_and_echoed_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = FeedStore(str(Path(folder) / "feeds.db"))
            try:
                store.add(chat_type="group", chat_id=42, feed_url=PRIVATE_URL)
                fetcher = AsyncMock(side_effect=FeedError(f"failed {PRIVATE_URL}"))
                poller = FeedPoller(store, AsyncMock(), fetcher=fetcher)
                with self.assertLogs("feeds", level="WARNING") as logs:
                    asyncio.run(poller.tick())
                self.assert_safe("\n".join(logs.output))
            finally:
                store.close()


class SubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FeedStore(str(Path(self.tmp.name) / "feeds.db"))
        self.bot = object.__new__(Bot)
        self.bot.config = AppConfig()
        self.bot.feeds = self.store
        self.event = MessageEvent(
            message_type="group", user_id=1, message_id=1, text="", at_me=True,
            sender_name="admin", group_id=42,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def assert_safe_logs(self, output: list[str]) -> None:
        text = "\n".join(output)
        self.assertIn("example.com/feed", text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_add_and_duplicate_log_safely_and_keep_stored_url(self) -> None:
        # A missing title falls back to the original URL in the store, not logs.
        xml = b"<rss><channel><item><guid>1</guid></item></channel></rss>"
        with patch("main.fetch_feed", new=AsyncMock(return_value=xml)) as fetch, self.assertLogs("qqbot", level="INFO") as logs:
            first, created = asyncio.run(self.bot.add_feed(self.event, PRIVATE_URL))
            second, repeated = asyncio.run(self.bot.add_feed(self.event, PRIVATE_URL))
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.feed_url, PRIVATE_URL)
        self.assertEqual(first.title, PRIVATE_URL)
        self.assertEqual(first.seen, ("1",))
        self.assertEqual((first.chat_type, first.chat_id), ("group", 42))
        fetch.assert_awaited_with(PRIVATE_URL, timeout=20.0, retries=2)
        self.assertIn("subscription added", logs.output[0])
        self.assertIn("subscription exists", logs.output[1])
        self.assert_safe_logs(logs.output)

    def test_fetch_failure_is_logged_safely_and_not_stored(self) -> None:
        error = FeedError(f"failed {PRIVATE_URL}", status=503, detail=f"upstream {PRIVATE_URL} private-token")
        with patch("main.fetch_feed", new=AsyncMock(side_effect=error)), self.assertLogs("qqbot", level="WARNING") as logs:
            with self.assertRaises(FeedError):
                asyncio.run(self.bot.add_feed(self.event, PRIVATE_URL))
        self.assertEqual(self.store.count(), 0)
        self.assertIn("status=503", logs.output[0])
        self.assertIn("upstream", logs.output[0])
        self.assert_safe_logs(logs.output)

    def test_long_credentials_are_redacted_before_detail_truncation(self) -> None:
        token = "synthetic-long-token-" + "0123456789abcdef" * 40
        url = "https://example.com/feed?token=" + token
        bodies = (
            "<b>Error Message:</b><pre>" + token + "</pre>",
            "<p>" + token + "</p>",
        )
        for body in bodies:
            with self.subTest(body=body[:20]), patch("feeds.httpx.AsyncClient") as client, patch(
                "feeds.asyncio.sleep", new=AsyncMock()
            ), self.assertLogs("qqbot", level="WARNING") as logs:
                client.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=httpx.Response(503, text=body)
                )
                with self.assertRaises(FeedError) as raised:
                    asyncio.run(self.bot.add_feed(self.event, url))
            self.assertLessEqual(len(raised.exception.detail), 300)
            self.assertIn("[redacted]", raised.exception.detail)
            self.assertIn("[redacted]", logs.output[0])
            self.assertNotIn(token[:100], raised.exception.detail)
            self.assertNotIn(token[:100], logs.output[0])
            self.assertEqual(self.store.count(), 0)

    def test_parse_failure_is_logged_and_not_stored(self) -> None:
        with patch("main.fetch_feed", new=AsyncMock(return_value=b"not XML")), self.assertLogs("qqbot", level="WARNING") as logs:
            with self.assertRaises(FeedError):
                asyncio.run(self.bot.add_feed(self.event, PRIVATE_URL))
        self.assertEqual(self.store.count(), 0)
        self.assertIn("subscription failed", logs.output[0])
        self.assertIn("不是合法 XML", logs.output[0])
        self.assert_safe_logs(logs.output)


if __name__ == "__main__":
    unittest.main()
