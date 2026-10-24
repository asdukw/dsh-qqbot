from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from feeds import (  # noqa: E402
    DEFAULT_RSSHUB_BASE,
    FeedError,
    FeedPoller,
    FeedRecord,
    FeedStore,
    bilibili_feed_url,
    feed_title,
    format_entry,
    parse_feed,
    strip_html,
)

RSS_V1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>UP 主 的 bilibili 动态</title>
<item><title>old post</title><link>https://t.bilibili.com/1</link>
<description>&lt;p&gt;first &amp; best&lt;/p&gt;</description><guid>1</guid></item>
</channel></rss>""".encode("utf-8")

RSS_V2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>UP 主 的 bilibili 动态</title>
<item><title>new post</title><link>https://t.bilibili.com/2</link>
<description>hello &lt;b&gt;world&lt;/b&gt;</description><guid>2</guid></item>
<item><title>old post</title><link>https://t.bilibili.com/1</link>
<description>first</description><guid>1</guid></item>
</channel></rss>""".encode("utf-8")

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom feed</title>
<entry><id>tag:1</id><title>Hello</title>
<link rel="alternate" href="https://example.com/1"/>
<summary>World</summary></entry>
</feed>"""


class ParseTests(unittest.TestCase):
    def test_rss_entries(self) -> None:
        entries = parse_feed(RSS_V2)
        self.assertEqual([entry.id for entry in entries], ["2", "1"])
        self.assertEqual(entries[1].title, "old post")
        self.assertEqual(feed_title(RSS_V2), "UP 主 的 bilibili 动态")

    def test_atom_entries(self) -> None:
        entries = parse_feed(ATOM)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Hello")
        self.assertEqual(entries[0].link, "https://example.com/1")
        self.assertEqual(feed_title(ATOM), "Atom feed")

    def test_strip_html(self) -> None:
        self.assertEqual(strip_html("<p>a &amp; b</p>"), "a & b")
        self.assertTrue(strip_html("x" * 300).endswith("…"))

    def test_invalid_xml(self) -> None:
        with self.assertRaises(FeedError):
            parse_feed(b"not xml")

    def test_bilibili_urls(self) -> None:
        expected = f"{DEFAULT_RSSHUB_BASE}/bilibili/user/dynamic/546195"
        self.assertEqual(bilibili_feed_url("546195"), expected)
        self.assertEqual(
            bilibili_feed_url("https://space.bilibili.com/546195/dynamic"), expected
        )
        self.assertEqual(
            bilibili_feed_url("https://example.com/feed.xml"), "https://example.com/feed.xml"
        )
        other = f"{DEFAULT_RSSHUB_BASE}/bilibili/user/dynamic/456664753"
        self.assertEqual(bilibili_feed_url("UID:456664753"), other)
        self.assertEqual(bilibili_feed_url("uid：456664753"), other)
        with self.assertRaises(FeedError):
            bilibili_feed_url("老番茄")

    def test_format_entry(self) -> None:
        entry = parse_feed(RSS_V2)[0]
        text = format_entry("UP", entry)
        self.assertIn("【UP】", text)
        self.assertIn("new post", text)
        self.assertIn("hello world", text)
        self.assertIn("https://t.bilibili.com/2", text)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FeedStore(str(Path(self.tmp.name) / "feeds.db"))

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def add(self, url="https://example.com/feed", seen=()) -> FeedRecord:
        record, _ = self.store.add(
            chat_type="group", chat_id=42, feed_url=url, title="t", seen=seen
        )
        return record

    def test_add_is_idempotent_per_chat(self) -> None:
        first = self.add(seen=("1",))
        second, created = self.store.add(
            chat_type="group", chat_id=42, feed_url="https://example.com/feed", title="t"
        )
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.seen, ("1",))

    def test_list_delete_and_seen(self) -> None:
        record = self.add()
        self.assertEqual(len(self.store.list_for("group", 42)), 1)
        self.store.update_seen(record.id, ["1", "2"])
        self.assertEqual(self.store.get(record.id).seen, ("1", "2"))
        self.store.mark_check(record.id, "boom")
        self.assertEqual(self.store.get(record.id).last_error, "boom")
        self.assertTrue(self.store.delete(record.id))
        self.assertEqual(self.store.count(), 0)


class PollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FeedStore(str(Path(self.tmp.name) / "feeds.db"))
        self.sent = []

        async def send(record, entry):
            self.sent.append((record.id, entry.id, entry.link))

        self.send = send

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_first_poll_baselines_then_sends_new_entries(self) -> None:
        record, _ = self.store.add(
            chat_type="group", chat_id=42, feed_url="https://example.com/feed", title="UP"
        )

        class Fetcher:
            def __init__(self):
                self.payload = RSS_V1

            async def __call__(self, url):
                return self.payload

        fetcher = Fetcher()
        poller = FeedPoller(self.store, self.send, fetcher=fetcher)
        self.assertEqual(asyncio.run(poller.tick()), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.get(record.id).seen, ("1",))

        fetcher.payload = RSS_V2
        self.assertEqual(asyncio.run(poller.tick()), 1)
        self.assertEqual(self.sent[0][1], "2")
        self.assertEqual(self.store.get(record.id).seen[0], "2")

        # Same feed again: nothing new.
        self.assertEqual(asyncio.run(poller.tick()), 0)

    def test_fetch_errors_are_recorded(self) -> None:
        record, _ = self.store.add(
            chat_type="group", chat_id=42, feed_url="https://example.com/feed", title="UP"
        )

        async def failing(url):
            raise FeedError("down")

        poller = FeedPoller(self.store, self.send, fetcher=failing)
        self.assertEqual(asyncio.run(poller.tick()), 0)
        self.assertIn("down", self.store.get(record.id).last_error)


if __name__ == "__main__":
    unittest.main()
