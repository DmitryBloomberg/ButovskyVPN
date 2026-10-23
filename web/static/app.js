(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const state = {
    csrf: "",
    account: null,
    mode: "login",
    tariffs: [],
    activeOrder: "",
    orderTimer: null,
    toastTimer: null,
  };

  async function api(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, {
      ...options,
      method,
      headers,
      credentials: "same-origin",
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.error || "Не удалось выполнить запрос.");
    return data;
  }

  function toast(message) {
    const element = $("#toast");
    element.textContent = message;
    element.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => element.classList.remove("visible"), 3300);
  }

  function showAuthMessage(message, isError = false) {
    const element = $("#authMessage");
    element.textContent = message;
    element.classList.remove("hidden", "error");
    if (isError) element.classList.add("error");
  }

  function clearAuthMessage() {
    $("#authMessage").classList.add("hidden");
  }

  function setTheme(theme) {
    const valid = ["dark", "black", "light"].includes(theme) ? theme : "dark";
    document.body.dataset.theme = valid;
    localStorage.setItem("butovsky-theme", valid);
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeChoice === valid);
    });
    document.querySelector('meta[name="theme-color"]').content =
      valid === "light" ? "#f2f3ee" : valid === "black" ? "#050505" : "#101114";
  }

  function setAuthMode(mode) {
    state.mode = mode === "register" ? "register" : "login";
    const isRegister = state.mode === "register";
    document.querySelectorAll("[data-auth-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.authMode === state.mode);
    });
    $("#authTitle").textContent = isRegister ? "Создайте аккаунт" : "С возвращением";
    $("#authSubtitle").textContent = isRegister
      ? "Зарегистрируйтесь по почте и паролю, чтобы покупать и хранить ключи."
      : "Войдите, чтобы купить тариф или открыть свои ключи.";
    $("#authSubmit").innerHTML = isRegister
      ? 'Создать аккаунт <span>↗</span>'
      : 'Войти в аккаунт <span>↗</span>';
    $("#passwordHint").classList.toggle("hidden", !isRegister);
    $("#passwordInput").autocomplete = isRegister ? "new-password" : "current-password";
    clearAuthMessage();
  }

  function formatMoney(price, currency) {
    const code = String(currency || "RUB").toUpperCase();
    try {
      return new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency: code,
        maximumFractionDigits: 2,
      }).format(Number(price || 0));
    } catch (_) {
      return `${Number(price || 0).toLocaleString("ru-RU")} ${code}`;
    }
  }

  function durationLabel(days) {
    const count = Number(days || 0);
    if (!count) return "Без срока";
    if (count % 365 === 0) return `${count / 365} г.`;
    if (count % 30 === 0) return `${count / 30} мес.`;
    return `${count} дн.`;
  }

  function deviceLabel(count) {
    const value = Math.abs(Number(count) || 1);
    const lastTwo = value % 100;
    const last = value % 10;
    const word = lastTwo >= 11 && lastTwo <= 14
      ? "устройств"
      : last === 1 ? "устройство"
        : last >= 2 && last <= 4 ? "устройства" : "устройств";
    return `${value} ${word}`;
  }

  function tariffCard(tariff, index) {
    const card = document.createElement("article");
    card.className = `tariff-card${index === 1 ? " featured" : ""}`;
    const overline = document.createElement("div");
    overline.className = "tariff-overline";
    overline.textContent = `ТАРИФ ${String(index + 1).padStart(2, "0")}`;
    const title = document.createElement("h3");
    title.textContent = tariff.name;
    const description = document.createElement("p");
    description.className = "tariff-description";
    description.textContent = `${durationLabel(tariff.duration_days)} доступа`;
    const price = document.createElement("div");
    price.className = "tariff-price";
    price.textContent = formatMoney(tariff.price, tariff.currency);
    const features = document.createElement("div");
    features.className = "tariff-features";
    const details = [
      tariff.traffic_limit_gb > 0 ? `${tariff.traffic_limit_gb} ГБ трафика` : "Без лимита трафика",
      deviceLabel(tariff.max_ips),
    ];
    details.forEach((label) => {
      const item = document.createElement("span");
      item.textContent = label;
      features.append(item);
    });
    const button = document.createElement("button");
    button.className = "button button-primary button-wide";
    button.type = "button";
    button.textContent = state.account ? "Выбрать тариф ↗" : "Войти и купить ↗";
    button.addEventListener("click", () => openCheckout(tariff));
    card.append(overline, title, description, price, features, button);
    return card;
  }

  async function loadCatalog() {
    const container = $("#catalog");
    try {
      const data = await api("/api/catalog");
      state.tariffs = data.tariffs || [];
      container.replaceChildren();
      if (!state.tariffs.length) {
        $("#catalogEmpty").classList.remove("hidden");
        return;
      }
      $("#catalogEmpty").classList.add("hidden");
      state.tariffs.forEach((tariff, index) => container.append(tariffCard(tariff, index)));
    } catch (error) {
      container.replaceChildren();
      const message = document.createElement("div");
      message.className = "empty-state";
      message.textContent = error.message;
      container.append(message);
    }
  }

  function setAuthenticated(account, csrf) {
    state.account = account || null;
    state.csrf = csrf || "";
    const loggedIn = Boolean(state.account);
    $("#authForm").classList.toggle("hidden", loggedIn);
    $("#authTabs").classList.toggle("hidden", loggedIn);
    $("#accountSummary").classList.toggle("hidden", !loggedIn);
    $("#logoutButton").classList.toggle("hidden", !loggedIn);
    $("#accountArea").classList.toggle("hidden", !loggedIn);
    if (loggedIn) {
      $("#accountEmail").textContent = account.email || "";
      const isLinked = Boolean(account.telegram_linked);
      $("#telegramStatus").textContent = isLinked
        ? "Telegram привязан"
        : "Telegram не привязан";
      $("#telegramLinkButton").classList.toggle("hidden", isLinked);
      if (!isLinked) $("#telegramLink").classList.add("hidden");
    } else {
      $("#telegramLink").classList.add("hidden");
    }
    loadCatalog();
    if (loggedIn) loadDashboard();
  }

  async function restoreSession() {
    try {
      const data = await api("/api/session");
      if (data.authenticated) setAuthenticated(data.account, data.csrf);
      else setAuthenticated(null, "");
    } catch (_) {
      setAuthenticated(null, "");
    }
  }

  function dateLabel(value) {
    if (!value) return "Без срока";
    let normalized = String(value).replace(" ", "T");
    if (!/[zZ]$|[+-]\d{2}:\d{2}$/.test(normalized)) normalized += "Z";
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" }).format(date);
  }

  function emptyList(message) {
    const element = document.createElement("div");
    element.className = "muted";
    element.textContent = message;
    return element;
  }

  function addMiniButton(parent, text, onClick, accent = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `mini-button${accent ? " accent" : ""}`;
    button.textContent = text;
    button.addEventListener("click", onClick);
    parent.append(button);
    return button;
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      toast("Ссылка скопирована.");
    } catch (_) {
      toast("Не удалось скопировать ссылку. Откройте её в новой вкладке.");
    }
  }

  async function loadDashboard() {
    if (!state.account) return;
    await Promise.all([loadKeys(), loadOrders()]);
  }

  async function loadKeys() {
    const container = $("#keysList");
    try {
      const data = await api("/api/keys");
      container.replaceChildren();
      if (!(data.keys || []).length) {
        container.append(emptyList("Пока нет активных VPN-ключей. Выберите тариф выше."));
        return;
      }
      data.keys.forEach((key) => {
        const item = document.createElement("div");
        item.className = "data-item";
        const title = document.createElement("div");
        title.className = "data-item-title";
        const name = document.createElement("span");
        name.textContent = key.name || key.tariff;
        const expiry = document.createElement("span");
        expiry.className = "status-pill";
        expiry.textContent = `до ${dateLabel(key.expires_at)}`;
        title.append(name, expiry);
        const meta = document.createElement("div");
        meta.className = "data-item-meta";
        meta.textContent = [key.tariff, key.server].filter(Boolean).join(" · ");
        item.append(title, meta);
        if (key.subscription_url) {
          const actions = document.createElement("div");
          actions.className = "data-item-actions";
          const link = document.createElement("a");
          link.className = "mini-button accent";
          link.href = key.subscription_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "Открыть ключ ↗";
          actions.append(link);
          addMiniButton(actions, "Скопировать", () => copyText(key.subscription_url));
          item.append(actions);
        }
        container.append(item);
      });
    } catch (error) {
      container.replaceChildren(emptyList(error.message));
    }
  }

  function orderStatus(status, fulfillment) {
    if (status === "paid" && ["complete", "completed", "fulfilled", "ready"].includes(fulfillment)) {
      return ["Оплачен и выдан", true];
    }
    if (status === "paid") return ["Оплачен · ключ готовится", true];
    if (status === "canceled") return ["Отменён", false];
    return ["Ожидает оплаты", false];
  }

  async function loadOrders() {
    const container = $("#ordersList");
    try {
      const data = await api("/api/orders");
      container.replaceChildren();
      if (!(data.orders || []).length) {
        container.append(emptyList("Заказов пока нет."));
        return;
      }
      data.orders.forEach((order) => {
        const item = document.createElement("div");
        item.className = "data-item";
        const title = document.createElement("div");
        title.className = "data-item-title";
        const label = document.createElement("span");
        label.textContent = order.tariff || "VPN";
        const [statusText, ok] = orderStatus(order.status, order.fulfillment_status);
        const badge = document.createElement("span");
        badge.className = `status-pill${ok ? " ok" : ""}`;
        badge.textContent = statusText;
        title.append(label, badge);
        const meta = document.createElement("div");
        meta.className = "data-item-meta";
        meta.textContent = `${formatMoney(order.amount, order.currency)} · ${dateLabel(order.created_at)}`;
        item.append(title, meta);
        if (order.status === "pending") {
          const actions = document.createElement("div");
          actions.className = "data-item-actions";
          addMiniButton(actions, "Проверить оплату", () => checkOrder(order.order_id), true);
          item.append(actions);
        }
        container.append(item);
      });
    } catch (error) {
      container.replaceChildren(emptyList(error.message));
    }
  }

  async function openCheckout(tariff) {
    if (!state.account) {
      setAuthMode("login");
      showAuthMessage("Войдите или создайте аккаунт, чтобы оформить этот тариф.");
      $("#emailInput").focus();
      return;
    }
    $("#dialogTitle").textContent = tariff.name;
    $("#dialogDescription").textContent =
      `${durationLabel(tariff.duration_days)} · ${formatMoney(tariff.price, tariff.currency)}. Выберите способ оплаты.`;
    $("#providerChoices").replaceChildren();
    $("#paymentResult").classList.add("hidden");
    $("#checkoutDialog").showModal();
    try {
      const data = await api(`/api/payment-methods?tariff_id=${encodeURIComponent(tariff.id)}`);
      const methods = data.methods || [];
      if (!methods.length) {
        const note = document.createElement("div");
        note.className = "muted";
        note.textContent = "Сейчас нет настроенных способов оплаты. Проверьте провайдеры в админке бота.";
        $("#providerChoices").append(note);
        return;
      }
      methods.forEach((method) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "provider-choice";
        const name = document.createElement("span");
        name.textContent = method.label || method.title;
        const arrow = document.createElement("span");
        arrow.textContent = "Выбрать ↗";
        button.append(name, arrow);
        button.addEventListener("click", () => createOrder(tariff, method, button));
        $("#providerChoices").append(button);
      });
    } catch (error) {
      showPaymentResult(error.message, false);
    }
  }

  function showPaymentResult(message, success, paymentUrl) {
    const result = $("#paymentResult");
    result.replaceChildren();
    result.classList.remove("hidden", "success");
    if (success) result.classList.add("success");
    const text = document.createElement("div");
    text.textContent = message;
    result.append(text);
    if (paymentUrl) {
      const link = document.createElement("a");
      link.href = paymentUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Перейти к оплате ↗";
      result.append(link);
    }
  }

  async function createOrder(tariff, method, button) {
    document.querySelectorAll(".provider-choice").forEach((item) => { item.disabled = true; });
    button.querySelector("span:last-child").textContent = "Создаём счёт…";
    try {
      const data = await api("/api/orders", {
        method: "POST",
        body: JSON.stringify({ tariff_id: tariff.id, provider_id: method.id }),
      });
      state.activeOrder = data.order_id;
      if (data.telegram_invoice_sent) {
        showPaymentResult("Счёт Telegram Stars отправлен в привязанный Telegram-аккаунт. После оплаты ключ появится здесь.", true);
      } else {
        showPaymentResult(
          `Счёт создан: ${formatMoney(Number(data.amount), data.currency)}. После оплаты вернитесь на сайт — статус обновится автоматически.`,
          true,
          data.payment_url,
        );
      }
      startOrderPolling(data.order_id);
      loadOrders();
    } catch (error) {
      showPaymentResult(error.message, false);
      document.querySelectorAll(".provider-choice").forEach((item) => { item.disabled = false; });
      button.querySelector("span:last-child").textContent = "Выбрать ↗";
    }
  }

  function startOrderPolling(orderId) {
    clearInterval(state.orderTimer);
    let attempts = 0;
    state.orderTimer = setInterval(async () => {
      attempts += 1;
      try {
        const data = await api("/api/orders/check", {
          method: "POST",
          body: JSON.stringify({ order_id: orderId }),
        });
        if (data.status === "paid") {
          clearInterval(state.orderTimer);
          state.orderTimer = null;
          showPaymentResult(
            "Оплата подтверждена. Проверьте раздел «Мои ключи» — доступ появится после подготовки.",
            true,
          );
          await loadDashboard();
        } else if (data.status === "canceled" || attempts >= 100) {
          clearInterval(state.orderTimer);
          state.orderTimer = null;
          if (data.status === "canceled") showPaymentResult("Этот счёт отменён. Можно создать новый.", false);
        } else if (attempts % 3 === 0) {
          await loadOrders();
        }
      } catch (_) {
        if (attempts >= 100) clearInterval(state.orderTimer);
      }
    }, 8000);
  }

  async function checkOrder(orderId) {
    try {
      const data = await api("/api/orders/check", {
        method: "POST",
        body: JSON.stringify({ order_id: orderId }),
      });
      await loadDashboard();
      toast(data.status === "paid" ? "Оплата подтверждена." : "Платёж пока не найден.");
    } catch (error) {
      toast(error.message);
    }
  }

  $("#authForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearAuthMessage();
    const button = $("#authSubmit");
    button.disabled = true;
    try {
      const payload = {
        email: $("#emailInput").value.trim(),
        password: $("#passwordInput").value,
      };
      const endpoint = state.mode === "register" ? "/api/auth/register" : "/api/auth/login";
      const data = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
      $("#passwordInput").value = "";
      setAuthenticated(data.account, data.csrf);
      showAuthMessage(state.mode === "register" ? "Аккаунт создан." : "Вы вошли в аккаунт.");
      toast(state.mode === "register" ? "Аккаунт создан." : "Добро пожаловать.");
    } catch (error) {
      showAuthMessage(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    button.addEventListener("click", () => setAuthMode(button.dataset.authMode));
  });
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.addEventListener("click", () => setTheme(button.dataset.themeChoice));
  });
  $("#logoutButton").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } catch (_) {}
    setAuthenticated(null, "");
    toast("Вы вышли из аккаунта.");
  });
  $("#telegramLinkButton").addEventListener("click", async () => {
    try {
      const data = await api("/api/telegram/link", { method: "POST", body: "{}" });
      const link = $("#telegramLink");
      link.href = data.url;
      link.classList.remove("hidden");
      toast("Ссылка готова. Откройте Telegram и нажмите Start.");
    } catch (error) {
      toast(error.message);
    }
  });
  $("#refreshButton").addEventListener("click", async () => {
    await loadDashboard();
    toast("Данные обновлены.");
  });
  $("#closeDialog").addEventListener("click", () => $("#checkoutDialog").close());
  $("#checkoutDialog").addEventListener("click", (event) => {
    if (event.target === $("#checkoutDialog")) $("#checkoutDialog").close();
  });

  setTheme(localStorage.getItem("butovsky-theme") || "dark");
  loadCatalog();
  restoreSession();
})();
