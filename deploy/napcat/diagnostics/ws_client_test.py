import asyncio
import logging
import sys

sys.path.insert(0, "/opt/qqbot/bot")

from onebot import OneBotConfig, OneBotWSClient  # noqa: E402

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# websockets DEBUG logs handshake headers, including Authorization.
logging.getLogger("websockets").setLevel(logging.WARNING)


async def main():
    token = sys.argv[1]
    client = OneBotWSClient(
        OneBotConfig(url="ws://127.0.0.1:3001", token=token, self_id=0),
        lambda _event: asyncio.sleep(0),
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(2)
    print("ws is", client._ws, flush=True)
    try:
        info = await asyncio.wait_for(client.get_login_info(), 5)
        print("info", info, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("call error", type(exc).__name__, exc, flush=True)
    await asyncio.sleep(1)
    await client.close()
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001
        pass


asyncio.run(main())
