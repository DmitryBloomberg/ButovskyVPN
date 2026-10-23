// ButovskyVPN — клиентские улучшения
(function () {
  // тема
  const root = document.documentElement;
  const saved = localStorage.getItem('bv-theme');
  if (saved) root.setAttribute('data-theme', saved);
  const toggle = document.getElementById('themeToggle');
  function setIcon() {
    if (toggle) toggle.textContent = root.getAttribute('data-theme') === 'light' ? '☀️' : '🌙';
  }
  setIcon();
  if (toggle) toggle.addEventListener('click', () => {
    const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    localStorage.setItem('bv-theme', next);
    setIcon();
  });

  // копирование ссылок подписки
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy || '');
        const old = btn.textContent;
        btn.textContent = '✓ Скопировано';
        setTimeout(() => (btn.textContent = old), 1500);
      } catch (e) {
        const t = document.createElement('textarea');
        t.value = btn.dataset.copy || '';
        document.body.appendChild(t); t.select();
        document.execCommand('copy'); t.remove();
        btn.textContent = '✓ Скопировано';
      }
    });
  });
})();
