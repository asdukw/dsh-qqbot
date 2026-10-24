"""Tiny OpenAI-compatible fake server for exercising DSH tools without an API key.

First request: asks the model to call profile_remember("称呼", "阿杜").
After the tool result: returns a final text response.
Supports both JSON and SSE streaming responses.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = "/tmp/fake_llm.log"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # keep the console quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "body": body}, ensure_ascii=False)[:20000] + "\n")

        messages = body.get("messages") or []
        has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)
        blob = json.dumps(messages, ensure_ascii=False)

        if has_tool_result:
            content = "好的，记住了。" if "记住了" in blob or "已记住" in blob else "好的。"
            tool_calls = None
            finish = "stop"
        elif "写文件" in blob:
            content = None
            tool_calls = [{
                "id": "call_fake_writefile",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": json.dumps(
                        {"file_path": "memory/notes/test.md", "content": "hello file memory"},
                        ensure_ascii=False,
                    ),
                },
            }]
            finish = "tool_calls"
        elif "读文件" in blob:
            content = None
            tool_calls = [{
                "id": "call_fake_readfile",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": json.dumps({"file_path": "memory/notes/test.md"}, ensure_ascii=False),
                },
            }]
            finish = "tool_calls"
        elif "读画像" in blob:
            content = None
            tool_calls = [{
                "id": "call_fake_read",
                "type": "function",
                "function": {"name": "profile_read", "arguments": "{}"},
            }]
            finish = "tool_calls"
        else:
            content = None
            tool_calls = [{
                "id": "call_fake_write",
                "type": "function",
                "function": {
                    "name": "profile_write",
                    "arguments": json.dumps({"key": "称呼", "value": "阿杜"}, ensure_ascii=False),
                },
            }]
            finish = "tool_calls"

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            delta: dict[str, object] = {"role": "assistant"}
            if content is not None:
                delta["content"] = content
            if tool_calls:
                delta["tool_calls"] = [{"index": 0, **tool_calls[0]}]
            self._sse({
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            })
            self._sse({
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            })
            self.wfile.write(b"data: [DONE]\n\n")
            return

        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        response = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("fake LLM listening on 127.0.0.1:8000", flush=True)
    server.serve_forever()
