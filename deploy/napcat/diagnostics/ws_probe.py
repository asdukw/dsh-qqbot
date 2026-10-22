import asyncio
import json
import sys

import websockets

TOKEN = sys.argv[1]
ACTION = json.dumps({"action": "get_login_info", "params": {}, "echo": "1"})


async def hold(name, url, headers):
    try:
        ws = await websockets.connect(
            url, additional_headers=headers, open_timeout=5,
            ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024,
        )
        print(name, "connected", flush=True)
        try:
            for _ in range(3):
                try:
                    msg = await asyncio.wait_for(ws.recv(), 1.5)
                    print(name, "event", str(msg)[:160], flush=True)
                except asyncio.TimeoutError:
                    print(name, "idle", flush=True)
            await ws.send(ACTION)
            for _ in range(3):
                try:
                    msg = await asyncio.wait_for(ws.recv(), 1.5)
                    print(name, "after", str(msg)[:160], flush=True)
                except asyncio.TimeoutError:
                    print(name, "after idle", flush=True)
        finally:
            print(name, "close_code", ws.close_code, "reason", ws.close_reason, flush=True)
            await ws.close()
    except Exception as exc:  # noqa: BLE001
        print(name, "error", type(exc).__name__, exc, flush=True)


async def main():
    header = {"Authorization": "Bearer " + TOKEN}
    await hold("header-noslash", "ws://127.0.0.1:3001", header)
    await hold("header-slash", "ws://127.0.0.1:3001/", header)
    await hold("query-noslash", "ws://127.0.0.1:3001?access_token=" + TOKEN, {})
    await hold("both-noslash", "ws://127.0.0.1:3001?access_token=" + TOKEN, header)


asyncio.run(main())
