"""Оркестратор и обвязка: проверка конфига, флаг вкл/выкл, состояние — без сети и Telegram."""
from autobid import runner, state


# ------------------------------------------------------------------ флаг включения рынка
def test_enabled_flag():
    assert runner._enabled({"session": "x"})                 # по умолчанию включён
    assert runner._enabled({"session": "x", "enabled": True})
    assert not runner._enabled({"session": "x", "enabled": False})
    assert not runner._enabled({})                           # пустой/закомментированный — выключен


# ------------------------------------------------------------------ проверка настроек P2P
def test_validate_p2p_ok_and_problems():
    assert runner.validate_p2p("USDT/RUB", {"session": "t", "initial": "95", "max_dev_pct": 2}) == []
    p = runner.validate_p2p("USDT/RUB", {})                  # нет аккаунта
    assert any("observer" in x for x in p)
    p2 = runner.validate_p2p("USDT/RUB", {"session": "t", "initial": "95"})
    assert any("max_dev_pct" in x for x in p2)               # коридор без предела
    p3 = runner.validate_p2p("USDT/RUB", {"session": "t", "on_limit": "wat"})
    assert any("on_limit" in x for x in p3)


def test_validate_p2p_bounds():
    p = runner.validate_p2p("USDT/RUB", {"session": "t", "min_price": "110", "max_price": "100"})
    assert any("min_price" in x for x in p)


# ------------------------------------------------------------------ проверка настроек биржи
def test_validate_exchange():
    ok = {"session": "t", "limit": "3.5", "step": "0.001", "mode": "Buy"}
    assert runner.validate_exchange("TON/USDT", ok) == []
    p = runner.validate_exchange("TON/USDT", {"session": "t"})
    assert any("limit" in x for x in p) and any("step" in x for x in p)
    p2 = runner.validate_exchange("TON/USDT", dict(ok, mode="Hodl"))
    assert any("mode" in x for x in p2)


# ------------------------------------------------------------------ состояние между запусками
def test_state_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_dir", lambda: str(tmp_path))
    s = state.Store("t")
    s.set("a", 1)
    s.update("obj", x=2)
    s.update("obj", y=3)
    s2 = state.Store("t")                                    # перечитали с диска
    assert s2.get("a") == 1 and s2.get("obj") == {"x": 2, "y": 3}
