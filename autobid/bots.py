"""Короткие имена ботов для командной строки: `cryptobot` вместо `send` и т.п."""
import re

ALIASES = {
    "cryptobot": "send", "crypto": "send", "cb": "send", "send": "send",
    "cryptobot_testnet": "CryptoTestnetBot", "cryptotestnetbot": "CryptoTestnetBot",
    "xrocket": "xrocket", "rocket": "xrocket", "tonrocketbot": "xrocket",
    "xrocket_testnet": "xrocket_testnet_bot", "rocket_testnet": "xrocket_testnet_bot",
    "xrocket_testnet_bot": "xrocket_testnet_bot",
}

TITLES = {"send": "CryptoBot", "xrocket": "xRocket", "xrocket_testnet_bot": "xRocket testnet",
          "CryptoTestnetBot": "CryptoBot testnet"}


def resolve(name):
    """'@CryptoBot', 't.me/send', 'cryptobot' → username для Telethon."""
    n = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", name.strip(), flags=re.I).lstrip("@").split("?")[0].strip("/")
    if n.lower() == "cryptobot":
        return "send"
    return ALIASES.get(n.lower(), n)


def title(username):
    return TITLES.get(username, "@" + username)


def slug(username):
    """Имя для папки записи."""
    return {"send": "cryptobot"}.get(username, re.sub(r"[^\w-]+", "_", username).lower())
