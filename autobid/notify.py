"""
Уведомления в Telegram о важных событиях (ТЗ п.6): запуск/остановка, падение и перезапуск рынка,
неверный токен, аварийный стоп. Отдельный бот-уведомитель (NOTIFY_BOT_TOKEN/NOTIFY_CHAT_ID в config.py).

Уведомления никогда не должны ронять торговлю: любые ошибки отправки проглатываются. Если бот/чат
не настроены — используется «пустой» уведомитель, который ничего не делает.
"""
import aiohttp

from . import settings

API = "https://api.telegram.org"


class NullNotifier:
    enabled = False

    async def send(self, text):
        return False

    async def close(self):
        pass


class Notifier:
    enabled = True

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)
        self.http = None

    async def send(self, text):
        """Отправить сообщение. Ошибки не поднимаем — уведомление не важнее торговли."""
        try:
            if self.http is None:
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            url = f"{API}/bot{self.token}/sendMessage"
            async with self.http.post(url, json={"chat_id": self.chat_id, "text": text,
                                                 "disable_web_page_preview": True}) as r:
                return r.status < 400
        except Exception:
            return False

    async def close(self):
        if self.http:
            await self.http.close()
            self.http = None


def build():
    """Notifier из config.py или NullNotifier, если уведомления не настроены."""
    token = settings.get("NOTIFY_BOT_TOKEN")
    chat = settings.get("NOTIFY_CHAT_ID")
    if token and chat:
        return Notifier(token, chat)
    return NullNotifier()
