const $ = (sel) => document.querySelector(sel);

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json();
}

function fmt(ts) {
  if (ts == null) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function badge(status) {
  if (status == null) return '<span class="muted">—</span>';
  return `<span class="badge badge-${status}">${status}</span>`;
}

async function refresh() {
  const [alertsData, notifData, gateData] = await Promise.all([
    getJSON("/api/alerts"),
    getJSON("/api/notifications"),
    getJSON("/api/gate/tickets"),
  ]);

  const s = alertsData.silence;
  $("#silence-state").innerHTML = s.active
    ? `<span class="silence-on">🔕 静默中，剩余 ${Math.ceil(s.remaining_seconds)} 秒</span>（截止 ${fmt(s.until_ts)}，原因：${s.reason || "未填写"}）`
    : '<span class="silence-off">🔔 通知正常</span>';

  $("#alerts-table tbody").innerHTML = alertsData.alerts.map((a) => `
    <tr>
      <td><code>${a.fingerprint}</code></td>
      <td>${a.current_seq}</td>
      <td>${badge(a.status)}</td>
      <td>${a.round}</td>
      <td>${escapeHtml(a.summary)}</td>
      <td>${badge(a.firing_notify_status)}</td>
      <td>${badge(a.recovery_notify_status)}</td>
      <td>${fmt(a.updated_at)}</td>
    </tr>`).join("") || '<tr><td colspan="8" class="muted">暂无告警</td></tr>';

  $("#notifications-table tbody").innerHTML = notifData.notifications.map((n) => `
    <tr>
      <td><code>${n.notification_no}</code></td>
      <td>${n.fingerprint}</td>
      <td>${n.round}</td>
      <td>${n.kind}</td>
      <td>${badge(n.status)}</td>
      <td>${n.attempts}</td>
      <td>${n.last_error ? escapeHtml(n.last_error) : "—"}</td>
      <td>${fmt(n.enqueued_at)}</td>
      <td>${fmt(n.delivered_at)}</td>
    </tr>`).join("") || '<tr><td colspan="9" class="muted">暂无通知</td></tr>';

  renderGatePicker(alertsData.alerts);
  renderGateTickets(gateData.tickets);
}

// --------------------------------------------------------------------------- //
// Release gate
// --------------------------------------------------------------------------- //
function renderGatePicker(alerts) {
  // Preserve the engineer's selections across the 2s auto-refresh.
  const checked = new Set(
    [...document.querySelectorAll(".gate-fp:checked")].map((i) => i.value));
  const list = $("#gate-fp-list");
  if (!alerts.length) {
    list.innerHTML = '<span class="muted">暂无告警指纹</span>';
    return;
  }
  list.innerHTML = alerts.map((a) => `
    <label class="gate-fp-item">
      <input type="checkbox" class="gate-fp" value="${escapeHtml(a.fingerprint)}"
        ${checked.has(a.fingerprint) ? "checked" : ""}>
      <code>${escapeHtml(a.fingerprint)}</code>
      ${badge(a.status)}
      <span class="muted">序号 ${a.current_seq} · 轮次 ${a.round}</span>
    </label>`).join("");
}

function summarizeReasons(reasons) {
  return (reasons || []).map((r) => {
    switch (r.reason) {
      case "seq_changed": return `${r.fingerprint} 序号 ${r.baseline}→${r.current}`;
      case "round_changed": return `${r.fingerprint} 轮次 ${r.baseline}→${r.current}`;
      case "not_recovered": return `${r.fingerprint} 未恢复（${r.status}）`;
      case "silence_changed": return "期间曾设置/解除静默";
      case "silence_active": return "当前处于全局静默";
      case "alert_missing": return `${r.fingerprint} 告警已不存在`;
      case "unknown_fingerprint": return `${r.fingerprint} 指纹不存在`;
      default: return r.reason;
    }
  }).join("；");
}

function describeError(data) {
  const d = (data && data.detail) ? data.detail : (data || {});
  if (d.reasons && d.reasons.length) return summarizeReasons(d.reasons);
  return d.message || d.error || JSON.stringify(d);
}

function renderGateTickets(tickets) {
  $("#gate-tickets-table tbody").innerHTML = tickets.map((t) => {
    const fps = t.fingerprints.map((f) => {
      const b = t.baseline[f] || {};
      return `<code>${escapeHtml(f)}</code> <span class="muted">@序号${b.seq}·轮次${b.round}</span>`;
    }).join("<br>");
    let reason;
    if (t.status === "open") {
      reason = t.valid_now
        ? '<span class="silence-off">当前有效</span>'
        : `<span class="silence-on">${escapeHtml(summarizeReasons(t.current_reasons))}</span>`;
    } else {
      reason = t.reasons && t.reasons.length
        ? escapeHtml(summarizeReasons(t.reasons)) : "—";
    }
    return `<tr>
      <td><code>${t.ticket_no}</code></td>
      <td>${fps}</td>
      <td>${fmt(t.created_at)}</td>
      <td>${badge(t.status)}</td>
      <td>${t.job_id ? escapeHtml(t.job_id) : "—"}</td>
      <td>${fmt(t.decided_at)}</td>
      <td class="gate-reasons">${reason}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">暂无票据</td></tr>';
}

$("#gate-issue-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("#gate-issue-result");
  const fps = [...document.querySelectorAll(".gate-fp:checked")].map((i) => i.value);
  if (!fps.length) {
    box.innerHTML = '<span class="silence-on">请先勾选至少一个指纹</span>';
    return;
  }
  const resp = await fetch("/api/gate/tickets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fingerprints: fps }),
  });
  const data = await resp.json();
  box.innerHTML = resp.ok
    ? `已签发票据 <code>${data.ticket_no}</code>（基线已快照，交付部署作业使用）`
    : `<span class="silence-on">签发失败：${escapeHtml(describeError(data))}</span>`;
  refresh();
});

$("#gate-confirm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("#gate-confirm-result");
  const resp = await fetch("/api/gate/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ticket_no: $("#confirm-ticket").value.trim(),
      job_id: $("#confirm-job").value.trim(),
    }),
  });
  const data = await resp.json();
  if (resp.ok) {
    box.innerHTML = data.decision === "released"
      ? `<span class="silence-off">✅ 放行</span> 票据 <code>${data.ticket_no}</code>` +
        `（作业 ${escapeHtml(data.job_id)}${data.replayed ? "，重试沿用原结论" : "，首次确认"}）`
      : `<span class="silence-on">⛔ 拒绝</span> 票据 <code>${data.ticket_no}</code>：` +
        escapeHtml(summarizeReasons(data.reasons));
  } else {
    box.innerHTML = `<span class="silence-on">确认失败：${escapeHtml(describeError(data))}</span>`;
  }
  refresh();
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("#silence-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const resp = await fetch("/api/silence", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      duration_seconds: Number($("#duration").value),
      reason: $("#reason").value,
    }),
  });
  if (!resp.ok) alert("设置失败: " + (await resp.text()));
  refresh();
});

$("#clear-silence").addEventListener("click", async () => {
  await fetch("/api/silence", { method: "DELETE" });
  refresh();
});

refresh();
setInterval(refresh, 2000);
