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
  const [alertsData, notifData] = await Promise.all([
    getJSON("/api/alerts"),
    getJSON("/api/notifications"),
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
}

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
