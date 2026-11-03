/* ==========================================================================
   Coutos Conveniência — Core Client Toolkit (core.js)
   Gerencia CSRF Token, Fetch Autenticado, Prevenção XSS, Tema e Notificações
   ========================================================================== */

window.CoutosApp = (() => {
  let csrfToken = null;
  let currentUser = null;

  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  function getCsrfToken() {
    return csrfToken || getCookie("coutos_csrf") || "";
  }

  function setCsrfToken(token) {
    if (token) csrfToken = token;
  }

  function escapeHtml(val) {
    if (val === null || val === undefined) return "";
    return String(val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtTemp(v) {
    return (v === null || v === undefined || isNaN(v))
      ? "--"
      : `${Number(v).toFixed(2).replace(".", ",")} °C`;
  }

  function fmtTempShort(v) {
    return (v === null || v === undefined || isNaN(v))
      ? "--"
      : `${Number(v).toFixed(1).replace(".", ",")} °C`;
  }

  function fmtTime(iso) {
    if (!iso) return "--:--:--";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "--:--:--";
    return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtDateTime(iso) {
    if (!iso) return "--";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "--";
    return d.toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  }

  function showNotice(elId, text, isOk = true) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = text;
    el.className = `notice show ${isOk ? "ok" : "err"}`;
    setTimeout(() => { el.classList.remove("show"); }, 7000);
  }

  function showToast(text, isOk = true) {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = `toast ${isOk ? "ok" : "err"}`;
    toast.textContent = text;
    container.appendChild(toast);
    setTimeout(() => {
      toast.remove();
    }, 4500);
  }

  async function apiFetch(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = Object.assign({}, options.headers || {});

    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const token = getCsrfToken();
      if (token) {
        headers["X-CSRF-Token"] = token;
      }
    }

    const fetchOpts = Object.assign({}, options, {
      method,
      headers,
      credentials: "same-origin"
    });

    const response = await fetch(url, fetchOpts);
    if (response.status === 401 && !url.includes("/api/v1/auth/login") && !url.includes("/api/v1/auth/change-password")) {
      window.location.href = "/login?expired=1";
      throw new Error("Sessão expirada ou não autenticada.");
    }
    return response;
  }

  async function requireSession() {
    try {
      const res = await fetch("/api/v1/auth/check", { credentials: "same-origin" });
      if (!res.ok) {
        window.location.href = "/login";
        return null;
      }
      const data = await res.json();
      if (!data.authenticated) {
        window.location.href = "/login";
        return null;
      }
      if (data.csrf_token) setCsrfToken(data.csrf_token);
      currentUser = data;

      const userBadge = document.getElementById("lbl-current-user");
      if (userBadge) {
        const roleLabel = data.role === "admin" ? "Admin" : "Operador";
        userBadge.textContent = `${data.username} (${roleLabel})`;
      }
      return data;
    } catch (e) {
      window.location.href = "/login";
      return null;
    }
  }

  function initTheme() {
    const saved = localStorage.getItem("coutos_theme") || "light";
    document.documentElement.setAttribute("data-theme", saved);
    const btn = document.getElementById("btn-theme-toggle");
    if (btn) {
      btn.textContent = saved === "dark" ? "☀ Claro" : "☾ Escuro";
      btn.addEventListener("click", () => {
        const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("coutos_theme", next);
        btn.textContent = next === "dark" ? "☀ Claro" : "☾ Escuro";
      });
    }
  }

  function initTabs(containerSelector = ".tabs-bar") {
    const bar = document.querySelector(containerSelector);
    if (!bar) return;
    const buttons = bar.querySelectorAll(".tab-btn");
    buttons.forEach(btn => {
      btn.addEventListener("click", () => {
        const targetId = btn.getAttribute("data-tab");
        buttons.forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        const pane = document.getElementById(targetId);
        if (pane) pane.classList.add("active");
      });
    });
  }

  function initLogoutButton() {
    const btn = document.getElementById("btn-logout");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      try {
        await apiFetch("/api/v1/auth/logout", { method: "POST" });
      } finally {
        window.location.href = "/login";
      }
    });
  }

  return {
    apiFetch,
    escapeHtml,
    fmtTemp,
    fmtTempShort,
    fmtTime,
    fmtDateTime,
    showNotice,
    showToast,
    requireSession,
    setCsrfToken,
    getCsrfToken,
    initTheme,
    initTabs,
    initLogoutButton,
    getCurrentUser: () => currentUser
  };
})();
