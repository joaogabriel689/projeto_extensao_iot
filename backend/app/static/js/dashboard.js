/* ==========================================================================
   Coutos Conveniência — Operational Dashboard Controller (dashboard.js)
   ========================================================================== */

document.addEventListener("DOMContentLoaded", async () => {
  CoutosApp.initTheme();
  CoutosApp.initTabs();
  CoutosApp.initLogoutButton();

  const session = await CoutosApp.requireSession();
  if (!session) return;

  const isAdmin = session.role === "admin";
  const DEVICE_ID = "camara01";
  let currentDevice = null;
  let thresholdsFormDirty = false;

  // Se for operador (não admin), desabilita controles exclusivos de administrador
  if (!isAdmin) {
    ["btn-save-esp-wifi", "btn-start-esp-ap", "btn-deploy-ota", "btn-save-evo", "btn-connect-qr", "btn-logout-inst"].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.disabled = true;
        el.title = "Operação restrita a usuários Administradores";
      }
    });
  }

  async function fetchSystemStatus() {
    try {
      const res = await CoutosApp.apiFetch("/api/v1/system/status");
      if (!res.ok) return;
      const data = await res.json();

      const dotMqtt = document.getElementById("dot-mqtt");
      const lblMqtt = document.getElementById("lbl-mqtt");
      dotMqtt.className = `dot ${data.mqtt_connected ? "ok" : "err"}`;
      lblMqtt.textContent = data.mqtt_connected ? "Conectado" : "Offline";

      const dotPg = document.getElementById("dot-pg");
      const lblPg = document.getElementById("lbl-pg");
      dotPg.className = `dot ${data.postgres_connected ? "ok" : "err"}`;
      lblPg.textContent = data.postgres_connected ? "Ativo" : "Offline";

      document.getElementById("lbl-buffer").textContent = data.local_buffer_pending_records ?? 0;
    } catch (e) {
      console.error("Erro ao buscar status:", e);
    }
  }

  async function fetchDevice() {
    try {
      const res = await CoutosApp.apiFetch(`/api/v1/devices/${DEVICE_ID}`);
      if (!res.ok) return;
      const dev = await res.json();
      currentDevice = dev;

      document.getElementById("kpi-device-name").textContent = `${dev.name} (${dev.device_id})`;
      document.getElementById("kpi-temp").textContent = CoutosApp.fmtTemp(dev.last_temp_c);
      document.getElementById("kpi-last-time").textContent = CoutosApp.fmtTime(dev.last_seen_at);
      document.getElementById("kpi-range").textContent = `${CoutosApp.fmtTempShort(dev.min_temp_c)} a ${CoutosApp.fmtTempShort(dev.max_temp_c)}`;
      document.getElementById("kpi-cooldown").textContent = `Porta: ${dev.alert_delay_seconds ?? 180}s | Cooldown: ${dev.alert_cooldown_seconds}s | Leitura: ${dev.telemetry_interval_seconds ?? 10}s`;
      document.getElementById("kpi-esp-id").textContent = dev.device_id;

      const badgeQuiet = document.getElementById("badge-quiet-hours");
      if (badgeQuiet) {
        if (dev.in_quiet_hours) {
          badgeQuiet.className = "badge badge-info tabular";
          badgeQuiet.textContent = `Modo Sem Alertas (${dev.quiet_hours_start}–${dev.quiet_hours_end})`;
        } else if (dev.quiet_hours_enabled) {
          badgeQuiet.className = "badge badge-neutral tabular";
          badgeQuiet.textContent = `Noite: ${dev.quiet_hours_start}–${dev.quiet_hours_end}`;
        } else {
          badgeQuiet.className = "badge badge-neutral";
          badgeQuiet.textContent = "Alertas 24h";
        }
      }

      const badgeTemp = document.getElementById("badge-temp-state");
      if (dev.last_temp_c === null || dev.last_temp_c === undefined) {
        badgeTemp.className = "badge badge-neutral";
        badgeTemp.textContent = "Sem leitura";
      } else if (dev.last_temp_c < dev.min_temp_c || dev.last_temp_c > dev.max_temp_c) {
        if (dev.in_quiet_hours) {
          badgeTemp.className = "badge badge-info";
          badgeTemp.textContent = "Sem Alertas (Noite)";
        } else if (dev.in_door_open_delay) {
          badgeTemp.className = "badge badge-warn tabular";
          badgeTemp.textContent = `Porta Aberta (${dev.out_of_range_elapsed_s ?? 0}s / ${dev.alert_delay_seconds ?? 180}s)`;
        } else {
          badgeTemp.className = "badge badge-danger";
          badgeTemp.textContent = "Fora da Faixa";
        }
      } else {
        badgeTemp.className = "badge badge-ok";
        badgeTemp.textContent = "Dentro da Faixa";
      }

      const badgeEsp = document.getElementById("badge-esp-status");
      if (dev.online_status === "online") {
        badgeEsp.className = "badge badge-ok";
        badgeEsp.textContent = "Online";
      } else if (dev.online_status === "offline") {
        badgeEsp.className = "badge badge-danger";
        badgeEsp.textContent = "Offline (LWT)";
      } else {
        badgeEsp.className = "badge badge-neutral";
        badgeEsp.textContent = dev.online_status || "Desconhecido";
      }

      if (!thresholdsFormDirty) {
        document.getElementById("inp-dev-name").value = dev.name;
        document.getElementById("inp-min-temp").value = dev.min_temp_c;
        document.getElementById("inp-max-temp").value = dev.max_temp_c;
        document.getElementById("inp-alert-delay").value = dev.alert_delay_seconds ?? 180;
        document.getElementById("inp-cooldown").value = dev.alert_cooldown_seconds;
        document.getElementById("inp-telemetry-interval").value = dev.telemetry_interval_seconds ?? 10;
        document.getElementById("inp-replay-interval").value = dev.replay_interval_seconds ?? 10;
        document.getElementById("inp-quiet-enabled").checked = !!dev.quiet_hours_enabled;
        document.getElementById("inp-quiet-start").value = dev.quiet_hours_start || "23:00";
        document.getElementById("inp-quiet-end").value = dev.quiet_hours_end || "07:00";
      }

      const wifiEl = document.getElementById("kpi-esp-wifi");
      const ipEl = document.getElementById("kpi-esp-ip");
      const btnEspWeb = document.getElementById("btn-open-esp-web");
      if (dev.wifi_ssid) {
        wifiEl.textContent = `Wi-Fi: ${dev.wifi_ssid} (${dev.wifi_rssi ?? "--"} dBm)`;
      }
      if (dev.wifi_ip && dev.wifi_ip !== "0.0.0.0") {
        ipEl.textContent = `IP: ${dev.wifi_ip}`;
        btnEspWeb.href = `http://${encodeURIComponent(dev.wifi_ip)}/`;
        btnEspWeb.textContent = `Painel Local ESP32 (${dev.wifi_ip}) ↗`;
        btnEspWeb.style.display = "inline-flex";
      }

      const fwBadge = document.getElementById("badge-fw-version");
      if (dev.fw_version) {
        fwBadge.textContent = `Firmware: v${dev.fw_version}`;
        fwBadge.className = "badge badge-ok tabular";
        document.getElementById("kpi-esp-id").textContent = `${dev.device_id} (v${dev.fw_version})`;
      }

      if (dev.ota_status) {
        const st = dev.ota_status;
        const otaBadge = document.getElementById("badge-ota-state");
        const otaMsg = document.getElementById("lbl-ota-msg");
        const otaPct = document.getElementById("lbl-ota-pct");
        const otaBar = document.getElementById("bar-ota-progress");
        const pct = Math.max(0, Math.min(100, Number(st.progress_pct || 0)));

        otaMsg.textContent = st.message || "Pronto";
        otaPct.textContent = `${pct}%`;
        otaBar.style.width = `${pct}%`;

        if (st.state === "downloading" || st.state === "starting" || st.state === "triggered") {
          otaBadge.className = "badge badge-warn";
          otaBadge.textContent = `Atualizando (${st.state})`;
          otaBar.style.background = "var(--blue)";
        } else if (st.state === "rebooting" || st.state === "success") {
          otaBadge.className = "badge badge-ok";
          otaBadge.textContent = "Concluído";
          otaBar.style.background = "var(--green-dot)";
        } else if (st.state === "failed") {
          otaBadge.className = "badge badge-danger";
          otaBadge.textContent = "Falha / Bloqueado";
          otaBar.style.background = "var(--red-dot)";
        } else {
          otaBadge.className = "badge badge-neutral";
          otaBadge.textContent = "OTA: Pronto";
        }
      }
    } catch (e) {
      console.error("Erro ao buscar dispositivo:", e);
    }
  }

  function renderChart(records) {
    const svg = document.getElementById("temp-chart");
    document.getElementById("chart-count").textContent = `${records.length} leituras`;
    if (!records || records.length === 0) {
      svg.innerHTML = `<text x="320" y="110" text-anchor="middle" fill="#64748b" font-size="13">Nenhuma leitura disponível</text>`;
      return;
    }

    const ordered = [...records].reverse();
    const minLimit = currentDevice ? currentDevice.min_temp_c : ordered[0].min_temp_c;
    const maxLimit = currentDevice ? currentDevice.max_temp_c : ordered[0].max_temp_c;

    const temps = ordered.map(r => Number(r.temperature_c));
    const rawMin = Math.min(...temps, minLimit);
    const rawMax = Math.max(...temps, maxLimit);
    const pad = Math.max(2, (rawMax - rawMin) * 0.15);
    const yMin = rawMin - pad;
    const yMax = rawMax + pad;

    const W = 640, H = 220;
    const padL = 48, padR = 16, padT = 14, padB = 26;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const toY = (val) => padT + plotH - ((val - yMin) / (yMax - yMin || 1)) * plotH;
    const toX = (idx) => padL + (ordered.length === 1 ? plotW / 2 : (idx / (ordered.length - 1)) * plotW);

    const yMaxLim = toY(maxLimit);
    const yMinLim = toY(minLimit);

    let html = "";
    html += `<rect x="${padL}" y="${Math.min(yMaxLim, yMinLim)}" width="${plotW}" height="${Math.abs(yMinLim - yMaxLim)}" fill="rgba(22, 163, 74, 0.12)" />`;

    html += `<line x1="${padL}" y1="${yMaxLim}" x2="${W - padR}" y2="${yMaxLim}" stroke="#dc2626" stroke-dasharray="4 4" stroke-width="1" />`;
    html += `<text x="${padL - 6}" y="${yMaxLim + 4}" text-anchor="end" fill="#dc2626" font-size="10">Máx ${maxLimit.toFixed(0)}°</text>`;

    html += `<line x1="${padL}" y1="${yMinLim}" x2="${W - padR}" y2="${yMinLim}" stroke="#2563eb" stroke-dasharray="4 4" stroke-width="1" />`;
    html += `<text x="${padL - 6}" y="${yMinLim + 4}" text-anchor="end" fill="#2563eb" font-size="10">Mín ${minLimit.toFixed(0)}°</text>`;

    const pts = ordered.map((r, i) => `${toX(i).toFixed(1)},${toY(Number(r.temperature_c)).toFixed(1)}`).join(" ");
    html += `<polyline fill="none" stroke="var(--text-primary)" stroke-width="2" points="${pts}" />`;

    ordered.forEach((r, i) => {
      const cx = toX(i).toFixed(1);
      const cy = toY(Number(r.temperature_c)).toFixed(1);
      const color = r.is_out_of_range ? "#dc2626" : "#16a34a";
      const label = CoutosApp.escapeHtml(`${CoutosApp.fmtTime(r.recorded_at)}: ${CoutosApp.fmtTemp(r.temperature_c)}`);
      html += `<circle cx="${cx}" cy="${cy}" r="3.2" fill="${color}"><title>${label}</title></circle>`;
    });

    const firstT = CoutosApp.escapeHtml(CoutosApp.fmtTime(ordered[0].recorded_at));
    const lastT = CoutosApp.escapeHtml(CoutosApp.fmtTime(ordered[ordered.length - 1].recorded_at));
    html += `<text x="${padL}" y="${H - 6}" fill="#64748b" font-size="10">${firstT}</text>`;
    html += `<text x="${W - padR}" y="${H - 6}" text-anchor="end" fill="#64748b" font-size="10">${lastT}</text>`;

    svg.innerHTML = html;
  }

  async function fetchTelemetry() {
    try {
      const onlyOut = document.getElementById("chk-only-out").checked;
      const res = await CoutosApp.apiFetch(`/api/v1/telemetry?device_id=${DEVICE_ID}&limit=35&out_of_range_only=${onlyOut}`);
      if (!res.ok) return;
      const rows = await res.json();

      renderChart(rows);

      const tbody = document.getElementById("tbody-telemetry");
      if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty-row">Nenhuma leitura encontrada.</td></tr>`;
        return;
      }

      tbody.innerHTML = rows.map(r => {
        const badge = r.is_out_of_range
          ? `<span class="badge badge-danger">Fora da faixa</span>`
          : `<span class="badge badge-ok">Normal</span>`;
        return `
          <tr>
            <td class="tabular">${CoutosApp.escapeHtml(CoutosApp.fmtDateTime(r.recorded_at))}</td>
            <td class="tabular"><strong>${CoutosApp.escapeHtml(CoutosApp.fmtTemp(r.temperature_c))}</strong></td>
            <td class="tabular" style="color:var(--text-muted);">${CoutosApp.escapeHtml(CoutosApp.fmtTempShort(r.min_temp_c))} a ${CoutosApp.escapeHtml(CoutosApp.fmtTempShort(r.max_temp_c))}</td>
            <td>${badge}</td>
          </tr>
        `;
      }).join("");
    } catch (e) {
      console.error("Erro ao carregar telemetria:", e);
    }
  }

  async function fetchAlerts() {
    try {
      const res = await CoutosApp.apiFetch(`/api/v1/alerts?device_id=${DEVICE_ID}&limit=30`);
      if (!res.ok) return;
      const rows = await res.json();

      const tbody = document.getElementById("tbody-alerts");
      if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty-row">Nenhum alerta registrado.</td></tr>`;
        return;
      }

      const labelMap = {
        "TEMP_ABOVE_MAX": { text: "Acima do Máximo", cls: "badge-danger" },
        "TEMP_BELOW_MIN": { text: "Abaixo do Mínimo", cls: "badge-danger" },
        "DEVICE_OFFLINE_LWT": { text: "Queda LWT (Offline)", cls: "badge-warn" },
        "TEMP_NORMALIZED": { text: "Normalizada", cls: "badge-ok" }
      };

      tbody.innerHTML = rows.map(a => {
        const meta = labelMap[a.alert_type] || { text: CoutosApp.escapeHtml(a.alert_type), cls: "badge-neutral" };
        const errTitle = CoutosApp.escapeHtml(a.whatsapp_error || "");
        const waBadge = a.whatsapp_sent
          ? `<span class="badge badge-ok">Enviado</span>`
          : `<span class="badge badge-neutral" title="${errTitle}">Não enviado</span>`;
        return `
          <tr>
            <td class="tabular">${CoutosApp.escapeHtml(CoutosApp.fmtDateTime(a.created_at))}</td>
            <td><span class="badge ${meta.cls}">${meta.text}</span></td>
            <td class="tabular">${a.temperature_c !== null ? CoutosApp.escapeHtml(CoutosApp.fmtTemp(a.temperature_c)) : "--"}</td>
            <td>${waBadge}</td>
          </tr>
        `;
      }).join("");
    } catch (e) {
      console.error("Erro ao carregar alertas:", e);
    }
  }

  function updateEvolutionStatusBadges(cfg) {
    document.getElementById("kpi-wa-instance").textContent = cfg.evolution_instance_name || "--";
    document.getElementById("kpi-wa-phone").textContent = cfg.merchant_whatsapp_number || "Não definido";

    const kpiBadge = document.getElementById("badge-wa-status");
    const detailBadge = document.getElementById("badge-inst-detail");

    if (cfg.instance_state === "open") {
      kpiBadge.className = "badge badge-ok";
      kpiBadge.textContent = "Conectado";
      detailBadge.className = "badge badge-ok";
      detailBadge.textContent = "Conectado (open)";
      document.getElementById("qr-image-container").style.display = "none";
      document.getElementById("qr-instructions").textContent = "WhatsApp conectado e pronto para enviar alertas térmicos e de queda de energia.";
    } else if (cfg.instance_state === "not_created") {
      kpiBadge.className = "badge badge-warn";
      kpiBadge.textContent = "Instância não criada";
      detailBadge.className = "badge badge-warn";
      detailBadge.textContent = "Não criada na Evolution";
    } else if (cfg.instance_state === "connecting" || cfg.instance_state === "close") {
      kpiBadge.className = "badge badge-warn";
      kpiBadge.textContent = "Aguardando QR Code";
      detailBadge.className = "badge badge-warn";
      detailBadge.textContent = `Aguardando leitura (${cfg.instance_state})`;
    } else {
      kpiBadge.className = "badge badge-neutral";
      kpiBadge.textContent = cfg.instance_state || "Desconhecido";
      detailBadge.className = "badge badge-neutral";
      detailBadge.textContent = cfg.instance_state || "Desconhecido";
    }
  }

  async function fetchEvolutionSettings(populateInputs = false) {
    try {
      const res = await CoutosApp.apiFetch("/api/v1/settings/evolution");
      if (!res.ok) return;
      const cfg = await res.json();

      if (populateInputs) {
        document.getElementById("inp-evo-url").value = cfg.evolution_api_url || "";
        document.getElementById("inp-evo-key").value = cfg.evolution_api_key || "";
        document.getElementById("inp-evo-instance").value = cfg.evolution_instance_name || "";
        document.getElementById("inp-evo-phone").value = cfg.merchant_whatsapp_number || "";
        document.getElementById("inp-evo-enabled").checked = !!cfg.whatsapp_notifications_enabled;
      }

      updateEvolutionStatusBadges(cfg);
    } catch (e) {
      console.error("Erro ao carregar configurações da Evolution API:", e);
    }
  }

  ["inp-dev-name", "inp-min-temp", "inp-max-temp", "inp-alert-delay", "inp-cooldown", "inp-telemetry-interval", "inp-replay-interval", "inp-quiet-enabled", "inp-quiet-start", "inp-quiet-end"].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("input", () => { thresholdsFormDirty = true; });
      el.addEventListener("change", () => { thresholdsFormDirty = true; });
    }
  });

  document.getElementById("form-thresholds").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-save-thresholds");
    btn.disabled = true;
    try {
      const payload = {
        name: document.getElementById("inp-dev-name").value.trim(),
        min_temp_c: parseFloat(document.getElementById("inp-min-temp").value),
        max_temp_c: parseFloat(document.getElementById("inp-max-temp").value),
        alert_delay_seconds: parseInt(document.getElementById("inp-alert-delay").value, 10),
        alert_cooldown_seconds: parseInt(document.getElementById("inp-cooldown").value, 10),
        telemetry_interval_seconds: parseInt(document.getElementById("inp-telemetry-interval").value, 10),
        replay_interval_seconds: parseInt(document.getElementById("inp-replay-interval").value, 10),
        quiet_hours_enabled: document.getElementById("inp-quiet-enabled").checked,
        quiet_hours_start: document.getElementById("inp-quiet-start").value.trim() || "23:00",
        quiet_hours_end: document.getElementById("inp-quiet-end").value.trim() || "07:00"
      };
      const res = await CoutosApp.apiFetch(`/api/v1/devices/${DEVICE_ID}/thresholds`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) {
        CoutosApp.showNotice("notice-thresholds", data.detail || "Erro ao salvar parâmetros.", false);
      } else {
        thresholdsFormDirty = false;
        CoutosApp.showNotice("notice-thresholds", "Parâmetros salvos no PostgreSQL e publicados ao ESP32 via MQTT!", true);
        CoutosApp.showToast("Faixa ideal sincronizada com sucesso!", true);
        await fetchDevice();
        await fetchTelemetry();
      }
    } catch (err) {
      CoutosApp.showNotice("notice-thresholds", "Falha de comunicação com o servidor.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-manual-replay").addEventListener("click", async () => {
    const btn = document.getElementById("btn-manual-replay");
    btn.disabled = true;
    try {
      const res = await CoutosApp.apiFetch("/api/v1/system/replay", { method: "POST" });
      const data = await res.json();
      if (res.ok) {
        CoutosApp.showToast(`Replay concluído: ${data.replayed_records} registros sincronizados.`, true);
        await refreshAll();
      }
    } catch (e) {
      CoutosApp.showToast("Erro ao acionar replay manual.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("form-evolution").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-save-evo");
    btn.disabled = true;
    try {
      const payload = {
        evolution_api_url: document.getElementById("inp-evo-url").value.trim(),
        evolution_api_key: document.getElementById("inp-evo-key").value.trim(),
        evolution_instance_name: document.getElementById("inp-evo-instance").value.trim(),
        merchant_whatsapp_number: document.getElementById("inp-evo-phone").value.trim(),
        whatsapp_notifications_enabled: document.getElementById("inp-evo-enabled").checked
      };
      const res = await CoutosApp.apiFetch("/api/v1/settings/evolution", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) {
        CoutosApp.showNotice("notice-evo", data.detail || "Erro ao salvar configurações da Evolution API.", false);
      } else {
        updateEvolutionStatusBadges(data);
        document.getElementById("inp-evo-key").value = data.evolution_api_key || "";
        CoutosApp.showNotice("notice-evo", "Configurações da Evolution API salvas com segurança!", true);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-evo", "Erro ao comunicar com o backend.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-connect-qr").addEventListener("click", async () => {
    const btn = document.getElementById("btn-connect-qr");
    btn.disabled = true;
    btn.textContent = "Gerando QR Code...";
    try {
      const res = await CoutosApp.apiFetch("/api/v1/settings/evolution/connect", { method: "POST" });
      const data = await res.json();
      if (data.qrcode_base64) {
        const img = document.getElementById("qr-image");
        img.src = data.qrcode_base64.startsWith("data:") ? data.qrcode_base64 : `data:image/png;base64,${data.qrcode_base64}`;
        document.getElementById("qr-image-container").style.display = "block";
        document.getElementById("qr-instructions").textContent = "Abra o WhatsApp no celular > Dispositivos conectados > Conectar dispositivo e escaneie o QR Code abaixo:";
      } else if (data.state === "open") {
        document.getElementById("qr-image-container").style.display = "none";
        document.getElementById("qr-instructions").textContent = "A instância já está conectada ao WhatsApp!";
      } else {
        CoutosApp.showNotice("notice-evo", data.error || data.detail || "Não foi possível obter o QR Code.", false);
      }
      await fetchEvolutionSettings(false);
    } catch (err) {
      CoutosApp.showNotice("notice-evo", "Erro ao solicitar QR Code da Evolution API.", false);
    } finally {
      btn.disabled = false;
      btn.textContent = "Conectar / Gerar QR Code";
    }
  });

  document.getElementById("btn-check-inst").addEventListener("click", () => fetchEvolutionSettings(false));

  document.getElementById("btn-logout-inst").addEventListener("click", async () => {
    if (!confirm("Deseja desconectar a sessão atual do WhatsApp nesta instância?")) return;
    await CoutosApp.apiFetch("/api/v1/settings/evolution/logout", { method: "DELETE" });
    document.getElementById("qr-image-container").style.display = "none";
    await fetchEvolutionSettings(false);
  });

  document.getElementById("btn-test-wa").addEventListener("click", async () => {
    const btn = document.getElementById("btn-test-wa");
    btn.disabled = true;
    try {
      const phone = document.getElementById("inp-evo-phone").value.trim();
      const res = await CoutosApp.apiFetch("/api/v1/notifications/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone: phone || null,
          message: "✅ [Coutos Conveniência] Teste de notificação enviado pelo Painel Web Seguro!"
        })
      });
      const data = await res.json();
      if (data.whatsapp_sent) {
        CoutosApp.showNotice("notice-evo", `Mensagem de teste enviada para ${data.target_phone}!`, true);
      } else {
        CoutosApp.showNotice("notice-evo", `Falha no envio: ${data.error || "Verifique se o QR Code foi conectado e o número está correto."}`, false);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-evo", "Erro ao disparar mensagem de teste.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("form-esp-wifi").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-save-esp-wifi");
    btn.disabled = true;
    try {
      const ssid = document.getElementById("inp-esp-ssid").value.trim();
      const pass = document.getElementById("inp-esp-pass").value.trim();
      const host = document.getElementById("inp-esp-mqtt-host").value.trim();
      const payload = { wifi_ssid: ssid, wifi_pass: pass };
      if (host) payload.mqtt_host = host;

      const res = await CoutosApp.apiFetch(`/api/v1/devices/${DEVICE_ID}/wifi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        CoutosApp.showNotice("notice-esp-wifi", `Comando enviado ao ESP32 via MQTT! Ele salvará a rede '${ssid}' na NVS e reiniciará.`, true);
      } else {
        CoutosApp.showNotice("notice-esp-wifi", data.detail || "Falha ao publicar comando MQTT para o ESP32.", false);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-esp-wifi", "Erro de comunicação com o servidor.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-start-esp-ap").addEventListener("click", async () => {
    const btn = document.getElementById("btn-start-esp-ap");
    btn.disabled = true;
    try {
      const res = await CoutosApp.apiFetch(`/api/v1/devices/${DEVICE_ID}/wifi`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_ap: true })
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        CoutosApp.showNotice("notice-esp-wifi", "Ponto de Acesso WPA2 ativado no ESP32 por 5 minutos (http://192.168.4.1).", true);
      } else {
        CoutosApp.showNotice("notice-esp-wifi", data.detail || "Não foi possível enviar o comando para o ESP32.", false);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-esp-wifi", "Erro ao comunicar com o servidor.", false);
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("form-esp-ota").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-deploy-ota");
    const fileInput = document.getElementById("inp-ota-file");
    const version = document.getElementById("inp-ota-version").value.trim() || "1.2.1";
    const secret = document.getElementById("inp-ota-secret").value.trim();

    if (!secret) {
      CoutosApp.showNotice("notice-esp-ota", "Informe a chave OTA_SHARED_SECRET para assinar a atualização.", false);
      return;
    }

    const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
    btn.disabled = true;
    btn.textContent = "Validando e Assinando...";

    try {
      const fname = file ? encodeURIComponent(file.name) : "firmware_stored.bin";
      const url = `/api/v1/devices/${DEVICE_ID}/ota/deploy?version=${encodeURIComponent(version)}&filename=${fname}`;
      const bodyData = file ? await file.arrayBuffer() : null;

      const res = await CoutosApp.apiFetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-OTA-Secret": secret
        },
        body: bodyData
      });
      const data = await res.json();
      if (res.ok && data.ok) {
        document.getElementById("lbl-ota-sha256").textContent = `SHA-256: ${data.sha256.slice(0, 16)}... (${(data.size_bytes / 1024).toFixed(1)} KB)`;
        CoutosApp.showNotice("notice-esp-ota", `${data.message} (SHA-256: ${data.sha256.slice(0, 12)}...)`, true);
        await fetchDevice();
      } else {
        CoutosApp.showNotice("notice-esp-ota", data.detail || "Falha ao validar ou disparar OTA.", false);
      }
    } catch (err) {
      CoutosApp.showNotice("notice-esp-ota", "Erro de comunicação ao enviar binário OTA.", false);
    } finally {
      btn.disabled = false;
      btn.textContent = "Assinar e Atualizar ESP32";
    }
  });

  document.getElementById("chk-only-out").addEventListener("change", fetchTelemetry);
  document.getElementById("btn-refresh").addEventListener("click", refreshAll);

  async function refreshAll() {
    await Promise.all([
      fetchSystemStatus(),
      fetchDevice(),
      fetchTelemetry(),
      fetchAlerts()
    ]);
  }

  await fetchEvolutionSettings(true);
  await refreshAll();

  setInterval(refreshAll, 5000);
  setInterval(() => fetchEvolutionSettings(false), 15000);
});
