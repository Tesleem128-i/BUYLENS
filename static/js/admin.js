(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const CYAN = "#4ce0ff";
  const VIOLET = "#9d6bff";
  const RED = "#ff8b8b";
  const GRID = "rgba(255,255,255,.06)";
  const TICK = "rgba(139,147,174,.75)";

  let charts = {};
  function destroyCharts() { Object.values(charts).forEach((c) => c?.destroy()); charts = {}; }

  function baseOptions(extra = {}) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: TICK, font: { family: "Inter", size: 11 } } } },
      scales: {
        x: { grid: { color: GRID }, ticks: { color: TICK, font: { size: 10 } } },
        y: { grid: { color: GRID }, ticks: { color: TICK, font: { size: 10 } }, beginAtZero: true },
      },
      ...extra,
    };
  }

  function lineChart(canvasId, labels, datasets) {
    const ctx = $(canvasId).getContext("2d");
    return new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: baseOptions(),
    });
  }

  function barChart(canvasId, labels, data, color) {
    const ctx = $(canvasId).getContext("2d");
    return new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ data, backgroundColor: color, borderRadius: 6, maxBarThickness: 34 }] },
      options: baseOptions({
        plugins: { legend: { display: false } },
        indexAxis: labels.length > 5 ? "y" : "x",
      }),
    });
  }

  function fmtDay(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  async function loadAnalytics(days) {
    const res = await fetch(`/api/admin/analytics?days=${days}`, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error("Couldn't load analytics.");
    return res.json();
  }

  function renderKPIs(d) {
    $("#kpi-total-users").textContent = d.total_users.toLocaleString();
    $("#kpi-verified-users").textContent = d.verified_users.toLocaleString();
    $("#kpi-active-users").textContent = d.active_users_in_range.toLocaleString();
    $("#kpi-logins").textContent = d.total_logins_in_range.toLocaleString();
    $("#kpi-failed-logins").textContent = d.total_failed_logins_in_range.toLocaleString();
    $("#kpi-locked-out").textContent = d.currently_locked_out.toLocaleString();
  }

  function renderCharts(d) {
    destroyCharts();

    const signupDays = Object.keys(d.signups_by_day);
    charts.signups = lineChart("#chart-signups", signupDays.map(fmtDay), [
      { label: "New signups", data: Object.values(d.signups_by_day), borderColor: CYAN, backgroundColor: "rgba(76,224,255,.12)", fill: true, tension: 0.35, pointRadius: 2 },
    ]);

    const loginDays = Object.keys(d.logins_by_day);
    charts.logins = lineChart("#chart-logins", loginDays.map(fmtDay), [
      { label: "Successful", data: Object.values(d.logins_by_day), borderColor: CYAN, backgroundColor: "rgba(76,224,255,.1)", fill: true, tension: 0.35, pointRadius: 2 },
      { label: "Failed", data: Object.values(d.failed_logins_by_day), borderColor: RED, backgroundColor: "rgba(255,139,139,.08)", fill: true, tension: 0.35, pointRadius: 2 },
    ]);

    const feat = d.most_used_features.length ? d.most_used_features : [{ name: "No activity yet", count: 0 }];
    charts.features = barChart("#chart-features", feat.map((f) => f.name), feat.map((f) => f.count), VIOLET);

    const views = d.most_visited_views.length ? d.most_visited_views : [{ name: "No activity yet", count: 0 }];
    charts.views = barChart("#chart-views", views.map((v) => v.name), views.map((v) => v.count), CYAN);

    const leaving = d.leaving_from.length ? d.leaving_from : [{ name: "No logouts recorded yet", count: 0 }];
    charts.leaving = barChart("#chart-leaving", leaving.map((v) => v.name), leaving.map((v) => v.count), RED);
  }

  function renderSignupsTable(rows) {
    const tbody = $("#admin-signups-table tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="admin-empty">No signups yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((r) => `
      <tr data-user-id="${r.id}">
        <td>${escapeHtml(r.name || "—")}</td>
        <td>${escapeHtml(r.email || "—")}</td>
        <td>${escapeHtml(r.country || "—")}</td>
        <td>${r.created_at ? new Date(r.created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—"}</td>
      </tr>`).join("");
    $$("#admin-signups-table tbody tr").forEach((tr) => {
      tr.addEventListener("click", () => openUserModal(tr.dataset.userId));
    });
  }

  function renderAnomalyBanner(days) {
    const banner = $("#admin-anomaly-banner");
    if (!days || !days.length) { banner.hidden = true; return; }
    const formatted = days.map((d) => new Date(d + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }));
    banner.hidden = false;
    banner.textContent = `⚠ Unusually high failed-login activity on ${formatted.join(", ")} — worth a closer look for brute-force attempts.`;
  }

  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function refresh(days) {
    try {
      const data = await loadAnalytics(days);
      renderKPIs(data);
      renderCharts(data);
      renderSignupsTable(data.recent_signups);
      renderAnomalyBanner(data.failed_login_anomaly_days);
    } catch (err) {
      console.error(err);
    }
  }

  $$("#admin-range button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#admin-range button").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      refresh(Number(btn.dataset.days));
    });
  });

  /* ---------------- export ---------------- */
  $$("#admin-export button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const activeDays = $("#admin-range button.is-active")?.dataset.days || 30;
      window.location.href = `/api/admin/export?type=${btn.dataset.type}&days=${activeDays}`;
    });
  });

  /* ---------------- per-user drill-down ---------------- */
  const modal = $("#admin-user-modal");
  const modalBody = $("#admin-modal-body");
  function closeModal() { modal.hidden = true; }
  $("#admin-modal-close").addEventListener("click", closeModal);
  $(".admin-modal__backdrop", modal).addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  function fmtDate(iso) {
    return iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
  }

  async function openUserModal(userId) {
    modal.hidden = false;
    modalBody.innerHTML = `<p class="admin-empty">Loading…</p>`;
    try {
      const res = await fetch(`/api/admin/user/${userId}`, { headers: { Accept: "application/json" } });
      const u = await res.json();
      if (!res.ok) throw new Error(u.error || "Couldn't load that user.");

      const badges = [
        u.is_verified ? `<span class="admin-user__badge admin-user__badge--ok">Verified</span>` : `<span class="admin-user__badge admin-user__badge--warn">Unverified</span>`,
        u.currently_locked_out ? `<span class="admin-user__badge admin-user__badge--warn">Locked out</span>` : "",
        u.country ? `<span class="admin-user__badge">${escapeHtml(u.country)}</span>` : "",
        u.currency ? `<span class="admin-user__badge">${escapeHtml(u.currency)}</span>` : "",
      ].filter(Boolean).join("");

      const topCats = (u.top_categories || []).map(([name, count]) => `<li><span>${escapeHtml(name)}</span><span>${count}</span></li>`).join("") || `<li><span>No activity yet</span></li>`;
      const logins = (u.recent_logins || []).slice(0, 8).map((l) => `<li><span>${l.success ? "✅ Success" : "❌ Failed"} — ${escapeHtml(l.ip || "unknown IP")}</span><span>${fmtDate(l.ts)}</span></li>`).join("") || `<li><span>No login history</span></li>`;
      const features = (u.recent_features || []).slice(0, 8).map((f) => `<li><span>${escapeHtml(f.feature)}</span><span>${fmtDate(f.ts)}</span></li>`).join("") || `<li><span>No feature activity</span></li>`;

      modalBody.innerHTML = `
        <div class="admin-user__name">${escapeHtml(u.full_name || "—")}</div>
        <div class="admin-user__email">${escapeHtml(u.email || "—")} · Joined ${fmtDate(u.created_at)} · Last login ${fmtDate(u.last_login)}</div>
        <div class="admin-user__badges">${badges}</div>
        <div class="admin-user__stats">
          <div class="admin-user__stat"><span>Searches</span><span>${u.searches ?? 0}</span></div>
          <div class="admin-user__stat"><span>Wishlist items</span><span>${u.wishlist_count ?? 0}</span></div>
          <div class="admin-user__stat"><span>History items</span><span>${u.history_count ?? 0}</span></div>
          <div class="admin-user__stat"><span>Accepted picks</span><span>${u.accepted_recommendations ?? 0}</span></div>
          <div class="admin-user__stat"><span>Failed logins</span><span>${u.failed_login_attempts ?? 0}</span></div>
          <div class="admin-user__stat"><span>Saved amount</span><span>${(u.saved_amount ?? 0).toLocaleString()}</span></div>
        </div>
        <div class="admin-user__section-title">Top categories</div>
        <ul class="admin-user__list">${topCats}</ul>
        <div class="admin-user__section-title">Recent logins</div>
        <ul class="admin-user__list">${logins}</ul>
        <div class="admin-user__section-title">Recent activity</div>
        <ul class="admin-user__list">${features}</ul>
      `;
    } catch (err) {
      modalBody.innerHTML = `<p class="admin-empty">⚠️ ${escapeHtml(err.message)}</p>`;
    }
  }

  refresh(7);
})();