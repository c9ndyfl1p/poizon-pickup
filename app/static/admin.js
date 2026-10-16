const tbody = document.querySelector('#orders');
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const rub = amount => new Intl.NumberFormat('ru-RU').format(amount) + ' ₽';

async function loadOrders() {
  tbody.innerHTML = '<tr><td colspan="6" class="admin-empty">Загружаем заявки…</td></tr>';
  try {
    const response = await fetch('/api/admin/orders');
    if (response.status === 401) { tbody.innerHTML = '<tr><td colspan="6" class="admin-empty">Обновите страницу и введите пароль администратора.</td></tr>'; return; }
    const rows = await response.json();
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="6" class="admin-empty">Пока нет заявок.</td></tr>'; return; }
    tbody.innerHTML = rows.map(order => {
      const client = order.telegram_id ? 'Telegram-клиент' : 'Локальная заявка';
      const tg = order.telegram_id ? `<br><small>ID ${esc(order.telegram_id)} · заявок: ${order.order_count}</small>` : '';
      return `<tr><td><span class="order-pill">#${order.id}</span><br><small>${esc(order.status)}</small></td><td>${esc(client)}${tg}</td><td><a href="${esc(order.product_url)}" target="_blank" rel="noopener">Открыть товар</a><br>${esc(order.category)} · ${esc(order.item_price_cny)} ¥</td><td>${esc(order.size_cm)} см</td><td><strong>${rub(order.upfront_total_rub)}</strong><br><small>товар ${rub(order.item_rub)} + комиссия ${rub(order.commission_rub)}</small></td><td>${esc(order.created_at)}</td></tr>`;
    }).join('');
  } catch { tbody.innerHTML = '<tr><td colspan="6" class="admin-empty">Не удалось загрузить заявки.</td></tr>'; }
}
document.querySelector('#refresh').addEventListener('click', loadOrders);
loadOrders();
