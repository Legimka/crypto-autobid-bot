"""
Проверка связи с биржей xRocket.

Публичная часть (список пар и стакан) работает БЕЗ токена. Балансы — если в config.py задан
XROCKET_TOKEN. По умолчанию ходит в testnet (XROCKET_TESTNET = True).

    python check_xrocket.py                # пары + стакан TON-USDT (+ балансы, если есть токен)
    python check_xrocket.py BTC-USDT       # другой символ
"""
import asyncio
import json
import sys

from autobid import settings
from autobid.xrocket.api import AuthError, RateLimited, XRocketError, XRocketExchange


def _short(obj, n=600):
    try:
        return json.dumps(obj, ensure_ascii=False)[:n]
    except (TypeError, ValueError):
        return str(obj)[:n]


def _as_list(data, *keys):
    """Достать список из ответа, форма которого в доке не зафиксирована."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


async def main(sym):
    token = settings.get("XROCKET_TOKEN")
    testnet = bool(settings.get("XROCKET_TESTNET"))
    print(f"Сеть: {'TESTNET (тестовая)' if testnet else 'MAINNET — БОЕВОЙ СЧЁТ!'}")
    cli = XRocketExchange(token or None, testnet=testnet)
    try:
        # 1) пары
        syms = await cli.symbols()
        items = _as_list(syms, "symbols", "data", "result")
        names = [s.get("symbol") if isinstance(s, dict) else s for s in items]
        print(f"\nПары: получено {len(items)}." if items else f"\nПары (сырой ответ): {_short(syms)}")
        if names:
            print("  примеры:", ", ".join(str(n) for n in names[:12]))
        # если запрошенной пары нет на этой сети — берём первую существующую (на testnet TON = GRAM)
        if names and sym not in names:
            alt = names[0]
            print(f"  пары {sym} здесь нет — показываю стакан по {alt}")
            sym = alt

        # 2) стакан (своя обработка ошибок, чтобы дойти до балансов)
        try:
            ob = await cli.orderbook(sym, depth=5)
            print(f"\nСтакан {sym}: bids {len(ob['bids'])}, asks {len(ob['asks'])}, seq {ob['sequence']}")
            if ob["asks"]:
                print("  лучшие продажи (ask):", ", ".join(f"{p}×{s}" for p, s in ob["asks"][:3]))
            if ob["bids"]:
                print("  лучшие покупки (bid):", ", ".join(f"{p}×{s}" for p, s in ob["bids"][:3]))
            if not ob["bids"] and not ob["asks"]:
                print("  стакан пуст или форма ответа иная.")
        except XRocketError as e:
            print(f"\nСтакан {sym}: ошибка — {e}")

        # 3) балансы (нужен токен)
        if not token:
            print("\nТокен не задан (XROCKET_TOKEN в config.py) — балансы пропущены. "
                  "Публичная связь с биржей работает. ✅")
            return
        bal = await cli.balances()
        rows = _as_list(bal, "balances", "data", "result")
        print(f"\nБалансы Trading: {len(rows)} позиций." if rows else f"\nБалансы (сырой ответ): {_short(bal)}")
        for b in rows[:20]:
            if isinstance(b, dict):
                print(f"  {b.get('asset')}: баланс {b.get('balance')}, доступно {b.get('available')}, "
                      f"в ордерах {b.get('holds')}")
        print("\nСвязь с аккаунтом есть — токен принят. ✅")
    except AuthError as e:
        print(f"\nТокен не принят ({e.status}): {e.detail}. Проверь XROCKET_TOKEN и XROCKET_TESTNET в config.py "
              "(токен должен быть из @xrocket_testnet_bot).")
    except RateLimited as e:
        print(f"\nЛимит частоты (429), подождать {e.retry_after} с.")
    except XRocketError as e:
        print(f"\nОшибка API: {e}")
    except Exception as e:
        print(f"\nНе удалось соединиться: {type(e).__name__}: {str(e)[:300]}")
    finally:
        await cli.close()


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TON-USDT"
    asyncio.run(main(symbol))
