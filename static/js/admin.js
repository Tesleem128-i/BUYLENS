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
      <tr>
        <td>${escapeHtml(r.name || "—")}</td>
        <td>${escapeHtml(r.email || "—")}</td>
        <td>${escapeHtml(r.country || "—")}</td>
        <td>${r.created_at ? new Date(r.created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—"}</td>
      </tr>`).join("");
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

  refresh(7);
})();