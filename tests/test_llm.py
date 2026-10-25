from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from llm import LLMClient, LLMConfig, LLMError  # noqa: E402


class ChatTimeoutTests(unittest.TestCase):
    def make_client(self) -> LLMClient:
        return LLMClient(
            LLMConfig(
                base_url="http://example.invalid/v1",
                model="test-model",
                provider="custom",
            )
        )

    def test_single_attempt_fails_fast_and_passes_timeout(self) -> None:
        client = self.make_client()
        client._client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        try:
            with self.assertRaises(LLMError):
                asyncio.run(
                    client.chat(
                        [{"role": "user", "content": "hi"}],
                        timeout=5,
                        attempts=1,
                    )
                )
            self.assertEqual(client._client.post.await_count, 1)
            self.assertEqual(client._client.post.await_args.kwargs["timeout"], 5)
        finally:
            asyncio.run(client.close())

    def test_attempts_controls_the_retry_count(self) -> None:
        client = self.make_client()
        client._client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        try:
            with patch("llm.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(LLMError):
                    asyncio.run(
                        client.chat(
                            [{"role": "user", "content": "hi"}],
                            timeout=5,
                            attempts=2,
                        )
                    )
            self.assertEqual(client._client.post.await_count, 2)
        finally:
            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
