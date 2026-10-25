import asyncio
import logging
import sys

sys.path.insert(0, "/opt/qqbot/bot")

import main  # noqa: E402

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# websockets DEBUG logs handshake headers, including Authorization.
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

config = main.load_config("/etc/qqbot/config.toml")
print("url:", config.onebot.url, "token-len:", len(config.onebot.token), "self:", config.onebot.self_id, flush=True)
print("exit:", asyncio.run(main.check_onebot(config)), flush=True)
