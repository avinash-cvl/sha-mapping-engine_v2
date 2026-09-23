/* Alerts: everything that needs a human, in one place.
 *
 * Derived, not stored. Every alert here is a live query over state the system
 * already holds -- stale runs, failed rows, starved gate targets -- so an
 * alert disappears when the condition does, without anyone marking it
 * resolved. A stored alerts table would need its own reconciliation, and a
 * console whose alert list disagrees with the database is worse than none.
 *
 * "Resolved" therefore means a condition that WAS true and is now fixed and
 * still worth showing (a gate target that was starved and is not any more),
 * not a row someone dismissed.
 */
(function () {
"use strict";

  const { api, esc, fmt, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let ALERTS = [];
  let FILTER = "unresolved";

  const ICON_WARN = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>';
  const ICON_OK = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="m5 12 5 5 9-10" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  async function gather() {
    /* Fetched in parallel, and a source that fails does not take the page
       with it -- a gate-health outage should not hide the stuck runs. */
    const [runs, gate] = await Promise.all([
      api("/runs?limit=500").catch(() => []),
      api("/gate-warnings?limit=200").catch(() => []),
    ]);

    const out = [];

    /* ---- stuck runs ---- */
    const stale = runs.filter((r) => r.status === "stale");
    if (stale.length) {
      const oldest = stale.map((r) => r.started_at).filter(Boolean).sort()[0];
      out.push({
        id: "stale-runs",
        severity: "crit",
        status: "unresolved",
        title: `${fmt(stale.length)} run${stale.length === 1 ? "" : "s"} stuck in "running"`,
        body: `${oldest ? `Oldest since ${new Date(oldest).toLocaleDateString(undefined,
          { day: "2-digit", month: "short", year: "numeric" })}. ` : ""}`
          + `These crashed without finalising. They are not live, but they still count `
          + `in any "currently running" figure until reconciled.`,
        meta: "system · ongoing",
        actions: [
          { label: "Reconcile", primary: true, run: reconcile },
          { label: "View in history", route: "history" },
        ],
      });
    }

    /* ---- failed rows ---- */
    const withFailures = runs.filter((r) => r.failed > 0);
    const totalFailed = withFailures.reduce((a, r) => a + r.failed, 0);
    if (totalFailed) {
      const recent = withFailures.slice(0, 1)[0];
      out.push({
        id: "failed-rows",
        severity: "warn",
        status: "unresolved",
        title: `${fmt(totalFailed)} SKU${totalFailed === 1 ? "" : "s"} left at Failed across ${
          fmt(withFailures.length)} run${withFailures.length === 1 ? "" : "s"}`,
        body: `A row marked <span class="m">Failed</span> is not <span class="m">PENDING</span>, `
          + `so a plain re-run selects straight past it. They stay failed until explicitly reset.`,
        meta: recent ? `most recent: ${(recent.execution_id || "").slice(0, 8)} · ${
          recent.started_at ? new Date(recent.started_at).toLocaleDateString(undefined,
            { day: "2-digit", month: "short" }) : "—"}` : "",
        actions: [
          { label: "Open a run to reset them", primary: true, route: "runs-new" },
        ],
      });
    }

    /* ---- gate targets ---- */
    const empty = gate.filter((g) => g.rows === 0);
    /* Sorted by how many category packs route into the node: a starved node
       nothing reaches is harmless, one four packs point at is a live fault. */
    const reached = [...empty].sort((a, b) => b.reached_from.length - a.reached_from.length);
    reached.slice(0, 3).forEach((g) => {
      out.push({
        id: "gate-empty-" + g.node,
        severity: "crit",
        status: "unresolved",
        title: `Gate target does not exist — ${g.node}`,
        body: `${fmt(g.reached_from.length)} category pack${g.reached_from.length === 1 ? "" : "s"} `
          + `route here and find nothing. Every listing gated onto this node is a silent miss.`,
        meta: `rules · ${g.reached_from.slice(0, 4).join(", ")}`,
        actions: [{ label: "Review gate health", primary: true, route: "rules" }],
      });
    });

    const starved = gate.filter((g) => g.rows > 0)
      .sort((a, b) => b.reached_from.length - a.reached_from.length);
    if (starved.length) {
      const g = starved[0];
      out.push({
        id: "gate-starved",
        severity: "warn",
        status: "unresolved",
        title: `${fmt(starved.length)} gate target${starved.length === 1 ? "" : "s"} below the shortlist floor`,
        body: `The worst is <span class="m">${esc(g.node)}</span> — ${fmt(g.rows)} row${
          g.rows === 1 ? "" : "s"} against a minimum of 3, reached from ${
          fmt(g.reached_from.length)} pack${g.reached_from.length === 1 ? "" : "s"}. `
          + `The backstop widens the pool and logs a warning, but the rule still points at the wrong node.`,
        meta: "rules · standing queue",
        actions: [{ label: "Open the backlog", primary: true, route: "rules" }],
      });
    }

    /* ---- resolved: conditions that were true and are not any more ---- */
    if (!stale.length) {
      out.push({
        id: "no-stale", severity: "ok", status: "resolved",
        title: "No runs are stuck", meta: "checked just now",
        body: "Every recorded run finished or was reconciled.", actions: [],
      });
    }
    if (!empty.length) {
      out.push({
        id: "no-empty-gate", severity: "ok", status: "resolved",
        title: "Every gate target exists", meta: "checked just now",
        body: "No rule routes to a master node that holds nothing.", actions: [],
      });
    }

    ALERTS = out;
    render();
    updateBadges();
  }

  function updateBadges() {
    const n = ALERTS.filter((a) => a.status === "unresolved").length;
    ["rail-badge", "bell-badge"].forEach((id) => ($(id).hidden = !n));
    $("bellBtn").title = n ? `${n} unresolved alert${n === 1 ? "" : "s"}` : "Alerts";
    $("bellBtn").setAttribute("aria-label", $("bellBtn").title);

    $("seg-unresolved").textContent = fmt(n);
    $("seg-resolved").textContent = fmt(ALERTS.length - n);
    $("seg-all").textContent = fmt(ALERTS.length);
  }

  function render() {
    const shown = ALERTS.filter((a) => FILTER === "all" || a.status === FILTER);

    $("alertList").innerHTML = shown.length
      ? shown.map((a, i) => `<div class="alert-card sev-${a.severity}">
          <div class="a-ic">${a.severity === "ok" ? ICON_OK : ICON_WARN}</div>
          <div class="a-body">
            <h4>${esc(a.title)}</h4>
            <p>${a.body}</p>
            ${a.meta ? `<div class="a-meta">${esc(a.meta)}</div>` : ""}
            ${a.actions.length ? `<div class="a-actions">${a.actions.map((act, j) =>
              `<button class="btn sm ${act.primary ? "primary" : ""}"
                 data-alert="${i}" data-act="${j}"
                 ${act.route ? `data-route="${esc(act.route)}"` : ""}>${esc(act.label)}</button>`
            ).join("")}</div>` : ""}
          </div>
        </div>`).join("")
      : `<p class="empty">Nothing ${FILTER === "resolved" ? "resolved" : "needs attention"} right now.</p>`;

    /* Route buttons are handled by the shell's delegated listener; only the
       ones that DO something here need wiring. */
    $("alertList").querySelectorAll("[data-act]").forEach((b) => {
      const act = shown[+b.dataset.alert]?.actions[+b.dataset.act];
      if (!act?.run) return;
      b.addEventListener("click", async () => {
        b.disabled = true;
        const original = b.textContent;
        b.textContent = "Working…";
        try {
          await act.run();
          await gather();
        } catch (e) {
          showError($("errors"), e.message);
          b.disabled = false;
          b.textContent = original;
        }
      });
    });
  }

  async function reconcile() {
    const d = await api("/runs/reconcile", { method: "POST" });
    /* Refreshing the pages that counted those runs, so the tiles on Runs and
       the summary on History stop reporting a number this action just
       invalidated. */
    window.Pages?.runs?.refresh?.();
    return d;
  }

  document.querySelectorAll(".segbtns [data-seg]").forEach((s) =>
    s.addEventListener("click", () => {
      document.querySelectorAll(".segbtns [data-seg]").forEach((x) =>
        x.setAttribute("aria-selected", String(x === s)));
      FILTER = s.dataset.seg;
      render();
    }));

  window.Pages = window.Pages || {};
  window.Pages["alerts"] = { boot: gather, refresh: () => gather().catch(() => {}) };

  /* The badge must be right before anyone opens Alerts -- a bell that only
     lights up once you have already looked is not a notification. */
  gather().catch(() => { /* the page itself will report it on open */ });
})();
