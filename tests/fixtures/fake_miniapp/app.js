// Тестовое мини-приложение: ведёт себя как биржа в Telegram — вход по initData, запросы, MainButton,
// всплывающее окно, BackButton, CloudStorage и WebSocket со стаканом. Секреты здесь нарочно —
// тест проверяет, что ни один из них не попал в запись.
(async function () {
  const tg = window.Telegram.WebApp;
  const state = window.__appState = {
    platform: tg.platform, initData: tg.initData, version: tg.version, events: [],
  };
  tg.ready();
  tg.expand();
  tg.onEvent('viewportChanged', () => { state.viewport = tg.viewportHeight; });

  const auth = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer sk_live_TOKEN_SECRET_123456' },
    body: JSON.stringify({ initData: tg.initData }),
  });
  state.auth = auth.status;

  document.getElementById('markets').addEventListener('click', async () => {
    const r = await fetch('/api/markets?pair=TON-USDT', { headers: { 'X-Api-Key': 'APIKEYSECRET9876543210' } });
    document.getElementById('out').textContent = JSON.stringify(await r.json());
  });

  tg.MainButton.setText('Разместить ордер');
  tg.MainButton.show();
  tg.MainButton.onClick(() => {
    tg.showPopup({
      title: 'Подтверждение', message: 'Разместить ордер?',
      buttons: [{ id: 'yes', type: 'default', text: 'Да' }, { id: 'no', type: 'cancel' }],
    }, async (id) => {
      state.popup = id;
      if (id !== 'yes') return;
      const r = await fetch('/api/orders', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ side: 'sell', price: document.getElementById('price').value, amount: '10',
          session_token: 'SESSIONSECRETVALUE1234567890' }),
      });
      state.order = await r.json();
    });
  });

  tg.BackButton.show();
  tg.BackButton.onClick(() => { state.back = true; });

  tg.CloudStorage.setItem('auth_token', 'CLOUDSECRETTOKENVALUE123456789', (err, ok) => {
    state.cloudSet = err ? 'error: ' + err : ok;
    tg.CloudStorage.getItem('auth_token', (e2, v) => { state.cloudGet = e2 ? 'error: ' + e2 : v; });
  });

  const ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => ws.send(JSON.stringify({ op: 'subscribe', channel: 'orderbook', token: 'WSSECRETTOKEN1234567890' }));
  state.wsFrames = 0;
  ws.onmessage = () => { state.wsFrames += 1; };

  window.__appReady = true;
})();
