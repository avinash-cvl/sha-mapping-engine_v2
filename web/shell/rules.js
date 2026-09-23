/* Match rules: guardrails, weights, gate health, data health, change log.
 *
 * Read-only, and the page says so rather than offering inputs that would not
 * take effect. config.py is read at import time, so a value edited here would
 * not reach a subprocess launched afterwards -- a form that silently does
 * nothing is worse than a table that is honest about being a table.
 */
(function () {
"use strict";

  const { api, esc, fmt, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let GATE = [];

  /* ------------------------------------------------------------ config */

  async function loadConfig() {
    const d = await api("/config");

    /* Grouped by what a change to it risks, which is the grouping the API
       already applies -- the client does not get to invent a second one. */
    const groups = {};
    d.settings.forEach((s) => (groups[s.group] ||= []).push(s));

    const weightTotal = d.weights.total;
    const weightsOk = Math.abs(weightTotal - 1) < 0.0001;

    $("cfg-body").innerHTML = Object.entries(groups).map(([group, items]) =>
      `<div class="grp">${esc(group)}${
        group === "Scoring weights"
          ? ` <span>— sum ${weightTotal.toFixed(2)}${weightsOk ? "" : " (does not sum to 1.00)"}</span>`
          : ""}</div>
       <div class="tw tw-short"><table>
         <thead><tr><th>Setting</th><th>Effect</th><th class="num">Value</th><th></th></tr></thead>
         <tbody>${items.map((s) => `<tr class="${s.locked ? "lock" : ""}">
           <td class="m">${esc(s.key)}</td>
           <td class="t-title">${esc(s.description)}</td>
           <td class="num">${esc(s.value)}</td>
           <td>${s.locked
             ? '<span class="pill p-no"><i class="sq"></i>Locked</span>'
             : s.env_override
               ? '<span class="pill p-rev"><i class="sq"></i>Env override</span>'
               : '<span class="pill p-ok"><i class="sq"></i>From config.py</span>'}</td>
         </tr>`).join("")}</tbody></table></div>`).join("")
      + `<div class="note" style="margin-top:16px">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
           <div><b>Weight changes are not comparable across runs.</b> ${esc(d.weights.note)}
             Compare two runs in <a href="#history" data-route="history">History</a> before
             trusting a change here.</div></div>`
      + `<div class="note info">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/></svg>
           <div>${esc(d.note)}</div></div>`
      + `<div class="grp" style="margin-top:16px">Models</div>
         <div class="tw tw-short"><table><tbody>
           <tr><th style="width:190px;text-transform:none;letter-spacing:0;font-size:12px">LLM deployment</th>
             <td class="m">${esc(d.llm.deployment || "not configured")}</td></tr>
           <tr><th style="text-transform:none;letter-spacing:0;font-size:12px">Embedding deployment</th>
             <td class="m">${esc(d.llm.embedding || "not configured")}</td></tr>
         </tbody></table></div>`;
  }

  /* ------------------------------------------------------------ gate */

  async function loadGate() {
    GATE = await api("/gate-warnings?limit=200");
    $("c-gate-total").textContent = fmt(GATE.length);
    renderGate();
  }

  function renderGate() {
    /* Sorted by how many rules reach the node rather than by how empty it is.
       A starved node nothing routes to is harmless; one that four category
       packs point at is a live mis-route waiting for traffic. */
    const rows = [...GATE].sort((a, b) =>
      $("gate-sort").value === "rows"
        ? a.rows - b.rows || b.reached_from.length - a.reached_from.length
        : b.reached_from.length - a.reached_from.length || a.rows - b.rows);

    $("gate-cap").textContent =
      `${fmt(GATE.filter((g) => g.rows === 0).length)} hold nothing at all`;

    $("gate-rows").innerHTML = rows.length
      ? rows.map((g) => `<tr>
          <td class="m">${esc(g.node)}</td>
          <td class="t-sub" style="margin:0">${esc(g.reached_from.slice(0, 4).join(", "))}${
            g.reached_from.length > 4 ? ` +${g.reached_from.length - 4} more` : ""}</td>
          <td class="num" style="color:${g.rows ? "var(--warn)" : "var(--crit)"};font-weight:600">${fmt(g.rows)}</td>
          <td class="num">${fmt(g.reached_from.length)}</td>
          <td>${g.state === "does not exist"
            ? '<span class="pill p-fail"><i class="sq"></i>Does not exist</span>'
            : '<span class="pill p-rev"><i class="sq"></i>Backstop active</span>'}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">Every gate target holds enough rows to be a shortlist.</td></tr>`;
  }

  /* ------------------------------------------------------------ health */

  async function loadHealth() {
    const d = await api("/catalog/health");
    $("h-dupe").textContent = fmt(d.duplicate_skus);
    $("h-pack").textContent = fmt(d.missing_pack);
    $("h-empty").textContent = fmt(d.empty_nodes);
    $("h-cold").textContent = fmt(d.never_mapped);

    $("health-dupes").innerHTML = d.duplicates.length
      ? d.duplicates.map((r) => `<tr>
          <td class="m">${esc(r.product_code)}</td>
          <td class="num">${fmt(r.rows)}</td>
          <td class="t-title">${r.names.map(esc).join(" · ")}</td>
        </tr>`).join("")
      : `<tr><td colspan="3" class="empty">No SKU code appears twice in the master.</td></tr>`;
  }

  /* ------------------------------------------------------------ changes */

  async function loadChangeLog() {
    const runs = await api("/config/history?limit=40");

    /* The change log is derived, not stored: each run records the config it
       ran with, so a difference between consecutive runs IS the change. A
       separate audit table would be a second place for the same fact to live
       and to be wrong. */
    const rows = [];
    for (let i = 0; i < runs.length - 1; i++) {
      const now = runs[i], prev = runs[i + 1];
      Object.keys(now.config).forEach((k) => {
        const a = prev.config[k]?.value, b = now.config[k].value;
        if (a !== undefined && a !== b) {
          rows.push({
            at: now.started_at, key: k, before: a, after: b,
            run: now.execution_id, sha: now.git_sha,
            override: now.config[k].is_override,
          });
        }
      });
    }

    $("cfg-log").innerHTML = rows.length
      ? rows.map((r) => `<tr>
          <td class="m">${r.at ? new Date(r.at).toLocaleDateString(undefined,
            { day: "2-digit", month: "short" }) : "—"}</td>
          <td class="m">${esc(r.key)}</td>
          <td class="num">${esc(r.before)}</td>
          <td class="num" style="color:var(--warn);font-weight:600">${esc(r.after)}</td>
          <td class="m">${esc((r.run || "").slice(0, 8))}</td>
          <td class="t-sub" style="margin:0">${r.override
            ? "Per-run override" : "Changed in config.py"}${
            r.sha ? ` · ${esc(r.sha.slice(0, 7))}` : ""}</td>
        </tr>`).join("")
      : `<tr><td colspan="6" class="empty">No setting has changed between the recorded runs.</td></tr>`;
  }

  /* ------------------------------------------------------------ wiring */

  $("gate-sort").addEventListener("change", renderGate);

  const LOADED = new Set();
  document.querySelectorAll(".tab[data-tab2]").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab[data-tab2]").forEach((x) =>
        x.setAttribute("aria-selected", String(x === t)));
      document.querySelectorAll(".tabpanel[data-panel2]").forEach((p) =>
        (p.hidden = p.dataset.panel2 !== t.dataset.tab2));

      const tab = t.dataset.tab2;
      if (LOADED.has(tab)) return;
      LOADED.add(tab);
      const fn = { gate: loadGate, health: loadHealth, changelog: loadChangeLog }[tab];
      fn?.().catch((e) => { LOADED.delete(tab); showError($("errors"), e.message); });
    }));

  window.Pages = window.Pages || {};
  window.Pages["rules"] = {
    async boot() {
      /* Gate health loads with the page even though its tab is not open: its
         count sits on the tab label, and a tab that says "0" until you click
         it is worse than no count. */
      await Promise.all([loadConfig(), loadGate().then(() => LOADED.add("gate"))]);
    },
  };
})();
