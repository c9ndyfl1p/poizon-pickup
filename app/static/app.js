const form = document.querySelector('#orderForm');
const errorBox = document.querySelector('#formError');
const categoryButtons = [...document.querySelectorAll('[data-category]')];
let category = 'shoes';
let clientType = 'new';
let config = null;
let quoteTimer;

function rub(value) {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(value) + ' ₽';
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

async function refreshQuote() {
  const price = Number(document.querySelector('#priceCny').value);
  if (!Number.isFinite(price) || price <= 0 || !config) return;
  try {
    const response = await fetch('/api/quote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, item_price_cny: price, client_type: clientType })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось рассчитать стоимость.');
    document.querySelector('#itemRub').textContent = rub(data.item_rub);
    document.querySelector('#commissionRub').textContent = rub(data.commission_rub);
    document.querySelector('#upfrontTotal').textContent = rub(data.upfront_total_rub);
    document.querySelector('#commissionLabel').textContent = `Комиссия и проверка (${data.commission_percent}%, минимум ${rub(data.commission_minimum_rub)})`;
    document.querySelector('#deliveryEstimate').textContent = `Ориентир для категории «${data.category_label.toLowerCase()}»: ${data.delivery_estimate}.`;
  } catch (error) { showError(error.message); }
}

categoryButtons.forEach(button => button.addEventListener('click', () => {
  category = button.dataset.category;
  categoryButtons.forEach(item => {
    const selected = item === button;
    item.classList.toggle('selected', selected);
    item.setAttribute('aria-pressed', String(selected));
  });
  refreshQuote();
}));

document.querySelector('#priceCny').addEventListener('input', () => {
  clearTimeout(quoteTimer); quoteTimer = setTimeout(refreshQuote, 120);
});

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    if (!response.ok) throw new Error('Не удалось загрузить настройки расчёта.');
    config = await response.json();
    document.querySelector('#rateDisplay').textContent = `${Number(config.purchase_rate).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽`;
    if (config.rate_is_demo) {
      document.querySelector('#rateCaption').textContent = 'Пример курса';
      document.querySelector('#rateNote').textContent = 'замените в .env';
    }
    await refreshQuote();
    document.querySelector('#connectionStatus').textContent = config.telegram_configured
      ? 'Токен бота подключён · для Telegram нужна HTTPS-ссылка'
      : 'Локальный просмотр · Telegram пока не подключён';
    if (window.Telegram?.WebApp?.initData && config.telegram_configured) {
      const sessionResponse = await fetch('/api/session', {
        method: 'POST', headers: { 'X-Telegram-Init-Data': window.Telegram.WebApp.initData }
      });
      const session = await sessionResponse.json();
      if (!sessionResponse.ok) throw new Error(session.detail || 'Не удалось проверить Telegram-профиль.');
      clientType = session.client_type;
      if (session.client_type === 'regular') {
        document.querySelector('#clientStatus').textContent = `Постоянный клиент · комиссия 15% · заказов: ${session.previous_orders}`;
      }
      await refreshQuote();
      document.querySelector('#connectionStatus').textContent = 'Telegram-профиль привязан автоматически';
    }
  } catch (error) { showError(error.message); }
}

form.addEventListener('submit', async event => {
  event.preventDefault(); showError('');
  if (!form.reportValidity()) return;
  const button = document.querySelector('#submitButton');
  button.disabled = true;
  button.querySelector('span').textContent = 'Отправляем…';
  const body = {
    category,
    item_price_cny: document.querySelector('#priceCny').value,
    product_url: document.querySelector('#productUrl').value.trim(),
    size_cm: document.querySelector('#sizeCm').value,
    notes: document.querySelector('#notes').value.trim()
  };
  const initData = window.Telegram?.WebApp?.initData || '';
  try {
    const response = await fetch('/api/orders', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось отправить заявку. Попробуйте ещё раз.');
    document.querySelector('#successOrderId').textContent = `#${data.order_id}`;
    document.querySelector('#successMessage').textContent = data.telegram_linked
      ? 'Заявка сохранена и привязана к вашему Telegram-аккаунту.'
      : 'Заявка сохранена. После подключения бота она будет автоматически связана с Telegram-аккаунтом.';
    form.hidden = true;
    document.querySelector('#successCard').hidden = false;
    window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.('success');
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; button.querySelector('span').textContent = 'Отправить заявку'; }
});

document.querySelector('#newOrderButton').addEventListener('click', () => {
  document.querySelector('#successCard').hidden = true;
  form.hidden = false;
  document.querySelector('#productUrl').value = '';
  document.querySelector('#sizeCm').value = '';
  document.querySelector('#notes').value = '';
  showError('');
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

if (window.Telegram?.WebApp) {
  window.Telegram.WebApp.ready();
  window.Telegram.WebApp.expand();
  if (window.Telegram.WebApp.themeParams?.bg_color) {
    document.documentElement.style.setProperty('--telegram-bg', window.Telegram.WebApp.themeParams.bg_color);
  }
}
loadConfig();
