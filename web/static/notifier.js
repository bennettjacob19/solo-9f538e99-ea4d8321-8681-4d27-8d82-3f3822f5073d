async function refresh() {
  const resp = await fetch("/api/notifications");
  const data = await resp.json();
  const fmt = (ts) => new Date(ts * 1000).toLocaleString();
  const escapeHtml = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  document.querySelector("#received-table tbody").innerHTML =
    data.notifications.map((n) => `
      <tr>
        <td><code>${n.notification_no}</code></td>
        <td>${n.fingerprint}</td>
        <td>${n.round}</td>
        <td>${n.kind}</td>
        <td><span class="badge badge-${n.status}">${n.status}</span></td>
        <td>${escapeHtml(n.summary)}</td>
        <td>${n.attempts_seen}</td>
        <td>${fmt(n.first_seen_at)}</td>
        <td>${fmt(n.last_seen_at)}</td>
      </tr>`).join("") ||
    '<tr><td colspan="9" class="muted">暂无通知</td></tr>';
}

refresh();
setInterval(refresh, 2000);
