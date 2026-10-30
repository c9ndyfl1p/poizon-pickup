const tbody = document.querySelector('#orders');
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const rub = amount => new Intl.NumberFormat('ru-RU').format(amount) + ' ₽';
const PAGE_SIZE = 50;
let offset = 0;
let loadedCount = 0;
const statuses = ['Новая', 'Оплачена', 'Выкуплена', 'Выдана', 'Отменена'];
const settingLabels = {
  pronto_rate: 'Курс Pronto, ₽ за ¥', buffer_rub: 'Надбавка к курсу, ₽',
  shoes_new_percent: 'Обувь · новый клиент, %', shoes_new_min_rub: 'Обувь · минимум новый, ₽',
  shoes_regular_percent: 'Обувь · постоянный клиент, %', shoes_regular_min_rub: 'Обувь · минимум постоянный, ₽',
  clothes_new_percent: 'Одежда · новый клиент, %', clothes_new_min_rub: 'Одежда · минимум новый, ₽',
  clothes_regular_percent: 'Одежда · постоянный клиент, %', clothes_regular_min_rub: 'Одежда · минимум постоянный, ₽',
  regular_after_paid_orders: 'Оплаченных заказов до постоянного тарифа'
};

async function loadOrders(reset = false) {
  if (reset) { offset = 0; loadedCount = 0; tbody.innerHTML = ''; }
  if (!loadedCount) tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">Загружаем заявки…</td></tr>';
  try {
    const response = await fetch(`/api/admin/orders?offset=${offset}&limit=${PAGE_SIZE}`);
    if (!response.ok) throw new Error('Не удалось загрузить заявки');
    const rows = await response.json();
    if (reset && !rows.length) { tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">Пока нет заявок.</td></tr>'; return; }
    if (reset) tbody.innerHTML = '';
    const html = rows.map(order => {
      const client = order.telegram_id ? 'Telegram-клиент' : 'Локальная заявка';
      const tg = order.telegram_id ? `<br><small>ID ${esc(order.telegram_id)} · оплачено: ${order.order_count}</small>` : '';
      const options = statuses.map(status => `<option value="${esc(status)}" ${order.status === status ? 'selected' : ''}>${esc(status)}</option>`).join('');
      return `<tr><td><span class="order-pill">#${Number(order.id)}</span><br><select class="status-select" data-order-id="${Number(order.id)}">${options}</select></td><td>${esc(client)}${tg}</td><td><a href="${esc(order.product_url)}" target="_blank" rel="noopener">Открыть товар</a><br>${esc(order.category)} · ${esc(order.item_price_cny)} ¥</td><td>${esc(order.size_cm)} см</td><td><strong>${rub(order.upfront_total_rub)}</strong><br><small>товар ${rub(order.item_rub)} + комиссия ${rub(order.commission_rub)}</small></td><td>${esc(order.notes) || '—'}</td><td>${esc(order.created_at)}</td></tr>`;
    }).join('');
    tbody.insertAdjacentHTML('beforeend', html);
    loadedCount += rows.length;
    offset += rows.length;
    document.querySelector('#load-more').hidden = rows.length < PAGE_SIZE;
    tbody.querySelectorAll('.status-select').forEach(select => select.addEventListener('change', updateStatus));
  } catch (error) {
    if (!loadedCount) tbody.innerHTML = `<tr><td colspan="7" class="admin-empty">${esc(error.message)}</td></tr>`;
  }
}

async function updateStatus(event) {
  const select = event.currentTarget;
  select.disabled = true;
  try {
    const response = await fetch(`/api/admin/orders/${Number(select.dataset.orderId)}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status: select.value})
    });
    if (!response.ok) throw new Error('Не удалось изменить статус');
    await loadOrders(true);
  } catch (error) {
    alert(error.message);
    select.disabled = false;
  }
}

async function loadSettings() {
  const response = await fetch('/api/admin/settings');
  if (!response.ok) throw new Error('Не удалось загрузить настройки');
  const settings = await response.json();
  document.querySelector('#settings-fields').innerHTML = Object.entries(settingLabels).map(([key, label]) =>
    `<label>${esc(label)}<input name="${key}" type="number" step="any" required value="${esc(settings[key])}"></label>`
  ).join('');
}

document.querySelector('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  const message = document.querySelector('#settings-message');
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  for (const key of Object.keys(data)) data[key] = key === 'regular_after_paid_orders' ? Number.parseInt(data[key], 10) : Number(data[key]);
  try {
    const response = await fetch('/api/admin/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Не удалось сохранить настройки');
    message.textContent = 'Настройки сохранены.';
  } catch (error) { message.textContent = error.message; }
});

document.querySelector('#refresh').addEventListener('click', () => loadOrders(true));
document.querySelector('#load-more').addEventListener('click', () => loadOrders(false));
loadOrders(true);
loadSettings().catch(error => { document.querySelector('#settings-message').textContent = error.message; });
