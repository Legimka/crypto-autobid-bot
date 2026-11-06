"""
Оркестратор автоперебивки (ТЗ §7: работа 24/7 в фоне).

Читает рынки из config.py (MARKETS — P2P, EXCHANGE — биржа) и на каждый ВКЛЮЧЁННЫЙ рынок поднимает
свою задачу, которая держится 24/7. Каждая задача изолирована: падение одного рынка не роняет
остальные; после ошибки рынок перезапускается с нарастающей паузой (10, 20, 40… с, до 2 минут).
Старты разнесены во времени, чтобы не бить по API синхронно (см. docs/DECISIONS.md §3).

Запуск — через run.py в корне проекта:
    python run.py            все включённые рынки (P2P + биржа)
    python run.py p2p        только P2P
    python run.py exchange   только биржа
Ctrl+C — мягкая остановка: задачам выставляется stop, они выходят из цикла.
"""
import asyncio
import random

from . import logging_setup, settings
from .cryptobot import exchange as exchange_mod
from .cryptobot import p2p as p2p_mod
from .xrocket import trader as xrocket_mod

BACKOFF_START = 10
BACKOFF_MAX = 120


def _enabled(cfg):
    """Рынок работает, если нет "enabled": False. Пустой/закомментированный конфиг — выключен."""
    return bool(cfg) and cfg.get("enabled", True) is not False


def validate_p2p(key, cfg):
    """Список проблем конфига P2P-рынка (пусто — всё хорошо)."""
    problems = []
    if not (cfg.get("observer") or cfg.get("session")):
        problems.append("не задан observer или session")
    if cfg.get("floating"):
        if cfg.get("initial_ratio") and not cfg.get("max_dev_pp"):
            problems.append("для плавающей цены задан initial_ratio, но нет max_dev_pp (коридор)")
    else:
        if cfg.get("initial") and not cfg.get("max_dev_pct"):
            problems.append("задан initial, но нет max_dev_pct (коридор не ограничен)")
    if cfg.get("on_limit") not in (None, "reset", "hold", "pause"):
        problems.append(f"on_limit должен быть 'reset', 'hold' или 'pause', а не {cfg.get('on_limit')!r}")
    _check_bounds(cfg, problems)
    return problems


def validate_exchange(key, cfg):
    """Список проблем конфига биржевого рынка CryptoBot (пусто — всё хорошо)."""
    problems = []
    if not cfg.get("session"):
        problems.append("не задан session")
    if not cfg.get("limit"):
        problems.append("не задан limit (коридор цены)")
    if not cfg.get("step"):
        problems.append("не задан step (шаг перебивки)")
    if str(cfg.get("mode", "Buy")).lower() not in ("buy", "sell"):
        problems.append(f"mode должен быть 'Buy' или 'Sell', а не {cfg.get('mode')!r}")
    _check_bounds(cfg, problems)
    return problems


def validate_xrocket(key, cfg):
    """Список проблем конфига биржевого рынка xRocket (пусто — всё хорошо). Токен проверяется при старте."""
    problems = []
    if not settings.get("XROCKET_TOKEN"):
        problems.append("не задан XROCKET_TOKEN в config.py")
    if not cfg.get("step"):
        problems.append("не задан step (шаг перебивки)")
    if not (cfg.get("size") or cfg.get("amount")):
        problems.append("не задан size (объём ордера)")
    if str(cfg.get("side", "buy")).lower() not in ("buy", "sell"):
        problems.append(f"side должен быть 'buy' или 'sell', а не {cfg.get('side')!r}")
    if cfg.get("on_limit") not in (None, "reset", "hold", "pause"):
        problems.append(f"on_limit должен быть 'reset', 'hold' или 'pause', а не {cfg.get('on_limit')!r}")
    if cfg.get("initial") and not cfg.get("max_dev_pct"):
        problems.append("задан initial, но нет max_dev_pct (коридор не ограничен)")
    _check_bounds(cfg, problems)
    return problems


def _check_bounds(cfg, problems):
    lo, hi = cfg.get("min_price"), cfg.get("max_price")
    try:
        if lo is not None and hi is not None and float(lo) > float(hi):
            problems.append(f"min_price {lo} больше max_price {hi}")
    except (TypeError, ValueError):
        problems.append("min_price/max_price должны быть числами")


def _jobs(which):
    """Собрать включённые рынки: [(label, key, cfg, kind)]."""
    jobs = []
    if which in ("all", "p2p"):
        for key, cfg in (settings.get("MARKETS") or {}).items():
            if _enabled(cfg):
                jobs.append((f"P2P {key}", key, cfg, "p2p"))
    if which in ("all", "exchange"):
        for key, cfg in (settings.get("EXCHANGE") or {}).items():
            if _enabled(cfg):
                jobs.append((f"Биржа {key}", key, cfg, "exchange"))
    if which in ("all", "xrocket"):
        for key, cfg in (settings.get("XROCKET_MARKETS") or {}).items():
            if _enabled(cfg):
                jobs.append((f"xRocket {key}", key, cfg, "xrocket"))
    return jobs


async def _sleep_or_stop(sec, stop):
    """Подождать sec секунд или до остановки — что раньше."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=sec)
    except asyncio.TimeoutError:
        pass


async def _close(trader):
    """Закрыть http-сессии клиентов рынка, если они есть (у P2P — observer/trader)."""
    for attr in ("observer", "trader", "api"):
        c = getattr(trader, attr, None)
        if c is not None and hasattr(c, "close"):
            try:
                await c.close()
            except Exception:
                pass


async def _supervise(label, make, stop, log):
    """Держать один рынок живым: построить → run → при ошибке перезапустить с паузой."""
    await _sleep_or_stop(random.uniform(0, 3), stop)     # разнести старты рынков
    delay = BACKOFF_START
    while not stop.is_set():
        trader = None
        try:
            trader = await make()
        except Exception as e:
            log.error("[%s] не удалось запустить: %s: %s — повтор через %d с",
                      label, type(e).__name__, str(e)[:200], delay)
            await _sleep_or_stop(delay, stop)
            delay = min(delay * 2, BACKOFF_MAX)
            continue
        delay = BACKOFF_START
        try:
            await trader.run(stop)
            log.info("[%s] завершился штатно", label)      # биржа после продажи выходит сама
            return
        except Exception as e:
            log.error("[%s] упал: %s: %s — перезапуск через %d с",
                      label, type(e).__name__, str(e)[:200], delay)
            await _sleep_or_stop(delay, stop)
            delay = min(delay * 2, BACKOFF_MAX)
        finally:
            await _close(trader)


def _make_factory(key, cfg, kind, log):
    """Фабрика построения трейдера рынка (вызывается при каждом старте/перезапуске)."""
    def to_log(msg):
        log.info("%s", msg)

    trade_log = logging_setup.TradeLog()

    async def make():
        if kind == "p2p":
            return await p2p_mod.build(key, cfg, log=to_log)
        if kind == "xrocket":
            return await xrocket_mod.build(key, cfg, log=to_log, trade_log=trade_log)
        return await exchange_mod.build(key, cfg, log=to_log)

    return make


async def run_all(which="all", stop=None, log=None):
    log = log or logging_setup.setup()
    stop = stop or asyncio.Event()

    jobs = _jobs(which)
    if not jobs:
        log.warning("Нет включённых рынков для режима «%s». Проверь MARKETS/EXCHANGE в config.py "
                    "(и что у рынка не стоит \"enabled\": False).", which)
        return

    tasks, started = [], []
    validators = {"p2p": validate_p2p, "exchange": validate_exchange, "xrocket": validate_xrocket}
    for label, key, cfg, kind in jobs:
        problems = validators[kind](key, cfg)
        if problems:
            log.error("[%s] пропущен — проблемы в настройках: %s", label, "; ".join(problems))
            continue
        tasks.append(asyncio.create_task(
            _supervise(label, _make_factory(key, cfg, kind, log), stop, log), name=label))
        started.append(label)

    if not tasks:
        log.error("Все рынки отсеяны проверкой настроек — запускать нечего.")
        return

    log.info("Запущено рынков: %d (%s). Остановка — Ctrl+C.", len(started), ", ".join(started))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def main(which="all"):
    """Точка входа: настроить логи, повесить обработчик остановки, крутить рынки."""
    import signal
    log = logging_setup.setup()
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, stop.set)      # на Windows SIGTERM не поддержан
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
    except Exception:
        pass
    try:
        await run_all(which, stop, log)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка по запросу — выхожу.")
        stop.set()
