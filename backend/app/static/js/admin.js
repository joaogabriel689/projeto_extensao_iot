/* ==========================================================================
   Coutos Conveniência — Admin, Users & Security Audit Controller (admin.js)
   ========================================================================== */

document.addEventListener("DOMContentLoaded", async () => {
  CoutosApp.initTheme();
  CoutosApp.initTabs();
  CoutosApp.initLogoutButton();

  const session = await CoutosApp.requireSession();
  if (!session) return;

  const isAdmin = session.role === "admin";
  let usersCache = [];

  if (!isAdmin) {
    // Operadores comuns veem apenas a aba de alterar a própria senha
    document.getElementById("tab-btn-users").style.display = "none";
    document.getElementById("tab-btn-audit").style.display = "none";
    document.getElementById("pane-users").classList.remove("active");
    document.getElementById("pane-profile").classList.add("active");
    document.querySelector('[data-tab="pane-profile"]').classList.add("active");
  }

  async function fetchUsers() {
    if (!isAdmin) return;
    try {
      const res = await CoutosApp.apiFetch("/api/v1/users");
      if (!res.ok) return;
      const users = await res.json();
      usersCache = users;

      const now = new Date();
      const activeAdmins = users.filter(u => u.role === "admin" && u.is_active).length;
      const lockedUsers = users.filter(u => u.locked_until && new Date(u.locked_until) > now).length;

      document.getElementById("kpi-total-users").textContent = users.length;
      document.getElementById("kpi-active-admins").textContent = activeAdmins;
      document.getElementById("kpi-locked-users").textContent = lockedUsers;

      const lockBadge = document.getElementById("badge-lockout-state");
      if (lockedUsers > 0) {
        lockBadge.className = "badge badge-danger";
        lockBadge.textContent = `${lockedUsers} Bloqueada(s)`;
      } else {
        lockBadge.className = "badge badge-ok";
        lockBadge.textContent = "Nenhum bloqueio";
      }

      const tbody = document.getElementById("tbody-users");
      if (users.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty-row">Nenhum usuário encontrado.</td></tr>`;
        return;
      }

      tbody.innerHTML = users.map(u => {
        const isLocked = u.locked_until && new Date(u.locked_until) > now;
        const roleBadge = u.role === "admin"
          ? `<span class="badge badge-info">Administrador</span>`
          : `<span class="badge badge-neutral">Operador</span>`;

        let statusBadge = `<span class="badge badge-ok">Ativo</span>`;
        if (!u.is_active) {
          statusBadge = `<span class="badge badge-danger">Desativado</span>`;
        } else if (isLocked) {
          statusBadge = `<span class="badge badge-warn" title="Tentativas falhas: ${u.failed_login_attempts}">Bloqueado (Lockout)</span>`;
        } else if (u.failed_login_attempts > 0) {
          statusBadge = `<span class="badge badge-ok">Ativo (${u.failed_login_attempts} falha)</span>`;
        }

        const lastLoginStr = u.last_login_at
          ? `${CoutosApp.escapeHtml(CoutosApp.fmtDateTime(u.last_login_at))} (${CoutosApp.escapeHtml(u.last_login_ip || "-")})`
          : `<span style="color:var(--text-muted);">Nunca acessou</span>`;

        const isSelf = u.username === session.username;

        return `
          <tr>
            <td class="tabular">#${u.id}</td>
            <td><strong>${CoutosApp.escapeHtml(u.username)}</strong> ${isSelf ? '<span class="badge badge-ok">Você</span>' : ''}</td>
            <td>${CoutosApp.escapeHtml(u.full_name || "—")}</td>
            <td>${roleBadge}</td>
            <td>${statusBadge}</td>
            <td class="tabular" style="font-size:12px;">${lastLoginStr}</td>
            <td class="tabular">v${u.session_version || 1}</td>
            <td>
              <div style="display:flex; gap:5px; flex-wrap:wrap;">
                <button type="button" class="btn btn-xs" data-action="edit" data-id="${u.id}">Editar / Senha</button>
                ${(isLocked || u.failed_login_attempts > 0) ? `<button type="button" class="btn btn-xs" data-action="unlock" data-id="${u.id}">Desbloquear</button>` : ""}
                <button type="button" class="btn btn-xs" data-action="revoke" data-id="${u.id}" title="Derrubar todas as sessões abertas deste usuário">Revogar Sessão</button>
                ${!isSelf ? `<button type="button" class="btn btn-xs btn-danger" data-action="delete" data-id="${u.id}">Excluir</button>` : ""}
              </div>
            </td>
          </tr>
        `;
      }).join("");
    } catch (e) {
      console.error("Erro ao listar usuários:", e);
    }
  }

  async function fetchAuditLogs() {
    if (!isAdmin) return;
    try {
      const actionFilter = document.getElementById("sel-audit-action").value;
      const url = actionFilter
        ? `/api/v1/audit-logs?limit=120&action=${encodeURIComponent(actionFilter)}`
        : `/api/v1/audit-logs?limit=120`;
      const res = await CoutosApp.apiFetch(url);
      if (!res.ok) return;
      const logs = await res.json();

      document.getElementById("kpi-audit-count").textContent = `${logs.length} eventos`;

      const tbody = document.getElementById("tbody-audit");
      if (logs.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-row">Nenhum registro de auditoria encontrado.</td></tr>`;
        return;
      }

      tbody.innerHTML = logs.map(item => {
        let badgeCls = "badge-neutral";
        if (item.action.includes("FAIL") || item.action.includes("LOCKOUT") || item.action.includes("RATE_LIMIT") || item.action.includes("DELETE")) {
          badgeCls = "badge-danger";
        } else if (item.action.includes("OK") || item.action.includes("CREATE")) {
          badgeCls = "badge-ok";
        } else if (item.action.includes("UPDATE") || item.action.includes("OTA") || item.action.includes("WIFI") || item.action.includes("PASSWORD")) {
          badgeCls = "badge-info";
        }

        return `
          <tr>
            <td class="tabular">${CoutosApp.escapeHtml(CoutosApp.fmtDateTime(item.created_at))}</td>
            <td><strong>${CoutosApp.escapeHtml(item.username)}</strong></td>
            <td><span class="badge ${badgeCls}">${CoutosApp.escapeHtml(item.action)}</span></td>
            <td class="tabular">${CoutosApp.escapeHtml(item.ip_address || "—")}</td>
            <td style="color:var(--text-secondary);">${CoutosApp.escapeHtml(item.details || "—")}</td>
          </tr>
        `;
      }).join("");
    } catch (e) {
      console.error("Erro ao carregar trilha de auditoria:", e);
    }
  }

  // Modal User Create / Edit
  const modal = document.getElementById("modal-user");
  const formModal = document.getElementById("form-modal-user");

  function openCreateUserModal() {
    document.getElementById("modal-user-title").textContent = "Cadastrar Novo Usuário";
    document.getElementById("inp-modal-user-id").value = "";
    const userInp = document.getElementById("inp-modal-username");
    userInp.value = "";
    userInp.disabled = false;
    document.getElementById("inp-modal-fullname").value = "";
    document.getElementById("sel-modal-role").value = "operator";
    document.getElementById("chk-modal-active").checked = true;
    const passInp = document.getElementById("inp-modal-password");
    passInp.value = "";
    passInp.required = true;
    document.getElementById("lbl-modal-password").textContent = "Senha Inicial (Obrigatória)";
    document.getElementById("help-modal-password").textContent = "Mínimo de 8 caracteres com letras e números/símbolos.";
    document.getElementById("notice-modal-user").classList.remove("show");
    modal.showModal();
  }

  function openEditUserModal(user) {
    document.getElementById("modal-user-title").textContent = `Editar Usuário: ${user.username}`;
    document.getElementById("inp-modal-user-id").value = String(user.id);
    const userInp = document.getElementById("inp-modal-username");
    userInp.value = user.username;
    userInp.disabled = true;
    document.getElementById("inp-modal-fullname").value = user.full_name || "";
    document.getElementById("sel-modal-role").value = user.role;
    document.getElementById("chk-modal-active").checked = !!user.is_active;
    const passInp = document.getElementById("inp-modal-password");
    passInp.value = "";
    passInp.required = false;
    document.getElementById("lbl-modal-password").textContent = "Nova Senha (Opcional — deixe em branco para manter)";
    document.getElementById("help-modal-password").textContent = "Se preenchida, redefine a senha e encerra sessões ativas deste usuário.";
    document.getElementById("notice-modal-user").classList.remove("show");
    modal.showModal();
  }

  document.getElementById("btn-open-create-user").addEventListener("click", openCreateUserModal);
  document.getElementById("btn-close-modal").addEventListener("click", () => modal.close());
  document.getElementById("btn-cancel-modal").addEventListener("click", () => modal.close());

  formModal.addEventListener("submit", async (e) => {
    e.preventDefault();
    const userId = document.getElementById("inp-modal-user-id").value;
    const btn = document.getElementById("btn-submit-modal");
    btn.disabled = true;

    try {
      const fullName = document.getElementById("inp-modal-fullname").value.trim();
      const role = document.getElementById("sel-modal-role").value;
      const isActive = document.getElementById("chk-modal-active").checked;
      const password = document.getElementById("inp-modal-password").value;

      if (!userId) {
        // Criar novo usuário
        const username = document.getElementById("inp-modal-username").value.trim();
        const res = await CoutosApp.apiFetch("/api/v1/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username,
            full_name: fullName || null,
            role,
            is_active: isActive,
            password
          })
        });
        const data = await res.json();
        if (!res.ok) {
          CoutosApp.showNotice("notice-modal-user", data.detail || "Erro ao criar usuário.", false);
          return;
        }
        modal.close();
        CoutosApp.showToast(`Usuário '${data.username}' criado com sucesso!`, true);
      } else {
        // Editar usuário existente
        const payload = {
          full_name: fullName,
          role,
          is_active: isActive
        };
        if (password) payload.new_password = password;

        const res = await CoutosApp.apiFetch(`/api/v1/users/${userId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) {
          CoutosApp.showNotice("notice-modal-user", data.detail || "Erro ao atualizar usuário.", false);
          return;
        }
        modal.close();
        CoutosApp.showToast(`Usuário '${data.username}' atualizado!`, true);
      }

      await fetchUsers();
      await fetchAuditLogs();
    } catch (err) {
      CoutosApp.showNotice("notice-modal-user", "Erro de comunicação com o servidor.", false);
    } finally {
      btn.disabled = false;
    }
  });

  // Delegação de eventos na tabela de usuários
  document.getElementById("tbody-users").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-action");
    const userId = Number(btn.getAttribute("data-id"));
    const user = usersCache.find(u => u.id === userId);
    if (!user) return;

    if (action === "edit") {
      openEditUserModal(user);
    } else if (action === "unlock") {
      const res = await CoutosApp.apiFetch(`/api/v1/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ unlock_account: true })
      });
      if (res.ok) {
        CoutosApp.showToast(`Conta '${user.username}' desbloqueada!`, true);
        await fetchUsers();
        await fetchAuditLogs();
      }
    } else if (action === "revoke") {
      if (!confirm(`Deseja invalidar imediatamente todas as sessões ativas de '${user.username}'?`)) return;
      const res = await CoutosApp.apiFetch(`/api/v1/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revoke_sessions: true })
      });
      if (res.ok) {
        CoutosApp.showToast(`Sessões de '${user.username}' revogadas!`, true);
        await fetchUsers();
        await fetchAuditLogs();
      }
    } else if (action === "delete") {
      if (!confirm(`Tem certeza que deseja excluir permanentemente o usuário '${user.username}'?`)) return;
      const res = await CoutosApp.apiFetch(`/api/v1/users/${userId}`, { method: "DELETE" });
      const data = await res.json();
      if (res.ok) {
        CoutosApp.showToast(`Usuário '${user.username}' excluído.`, true);
        await fetchUsers();
        await fetchAuditLogs();
      } else {
        CoutosApp.showToast(data.detail || "Não foi possível excluir.", false);
      }
    }
  });

  // Alterar própria senha
  document.getElementById("form-change-own-pass").addEventListener("submit", async (e) => {
    e.preventDefault();
    const currPass = document.getElementById("inp-curr-pass").value;
    const newPass = document.getElementById("inp-new-pass").value;
    const confPass = document.getElementById("inp-conf-pass").value;

    if (newPass !== confPass) {
      CoutosApp.showNotice("notice-own-pass", "A confirmação da nova senha não confere.", false);
      return;
    }

    const btn = document.getElementById("btn-save-own-pass");
    btn.disabled = true;
    try {
      const res = await CoutosApp.apiFetch("/api/v1/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: currPass, new_password: newPass })
      });
      const data = await res.json();
      if (!res.ok) {
        CoutosApp.showNotice("notice-own-pass", data.detail || "Falha ao alterar senha.", false);
      } else {
        if (data.csrf_token) CoutosApp.setCsrfToken(data.csrf_token);
        document.getElementById("inp-curr-pass").value = "";
        document.getElementById("inp-new-pass").value = "";
        document.getElementById("inp-conf-pass").value = "";
        CoutosApp.showNotice("notice-own-pass", data.message || "Senha alterada com sucesso!", true);
        CoutosApp.showToast("Senha atualizada com sucesso!", true);
        await fetchUsers();
        await fetchAuditLogs();
      }
    } catch (err) {
      CoutosApp.showNotice("notice-own-pass", "Erro de comunicação com o servidor.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("sel-audit-action").addEventListener("change", fetchAuditLogs);
  document.getElementById("btn-refresh-admin").addEventListener("click", async () => {
    await fetchUsers();
    await fetchAuditLogs();
  });

  await fetchUsers();
  await fetchAuditLogs();
});
