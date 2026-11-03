/* ==========================================================================
   Coutos Conveniência — Login Controller (login.js)
   ========================================================================== */

document.addEventListener("DOMContentLoaded", () => {
  CoutosApp.initTheme();

  const params = new URLSearchParams(window.location.search);
  if (params.get("expired") === "1") {
    CoutosApp.showNotice("notice-login", "Sua sessão expirou ou foi encerrada. Faça login novamente.", false);
  }

  const passInput = document.getElementById("inp-login-pass");
  const toggleBtn = document.getElementById("btn-toggle-pass");
  if (toggleBtn && passInput) {
    toggleBtn.addEventListener("click", () => {
      const isPass = passInput.type === "password";
      passInput.type = isPass ? "text" : "password";
      toggleBtn.textContent = isPass ? "Ocultar" : "Mostrar";
    });
  }

  const form = document.getElementById("form-login");
  const submitBtn = document.getElementById("btn-login-submit");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("inp-login-user").value.trim();
    const password = passInput.value;

    if (!username || !password) {
      CoutosApp.showNotice("notice-login", "Preencha o usuário e a senha.", false);
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "Autenticando...";

    try {
      const res = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username, password })
      });

      const data = await res.json();
      if (res.ok && data.ok) {
        if (data.csrf_token) CoutosApp.setCsrfToken(data.csrf_token);
        window.location.href = "/";
      } else {
        CoutosApp.showNotice("notice-login", data.detail || "Usuário ou senha incorretos.", false);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-login", "Falha de comunicação com o servidor.", false);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Entrar no Sistema";
    }
  });
});
