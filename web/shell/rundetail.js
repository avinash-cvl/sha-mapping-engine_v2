/* Run detail modal.
 *
 * Moved verbatim out of history.html so the Runs page, the History table and
 * the log browser all open the SAME dialog rather than three that drift. The
 * renderers below are unchanged from the version that shipped; what changed is
 * only that they now live behind window.RunDetail instead of in one page's
 * inline script.
 *
 * It takes either a recorded run or a bare log file. The parser returns the
 * same shape either way, which is why the run row is optional context here
 * rather than a second source of truth.
 */
(function () {
"use strict";

  const { api, esc, fmt, duration } = window.Console;
  const $ = (id) => document.getElementById(id);

  let RD = null;            // parsed log for the open run
  let RD_RUN = null;        // audit.engine_run row, when there is one

  function rdOpen() { $("rd").hidden = false; document.body.style.overflow = "hidden"; }
  function rdClose() { $("rd").hidden = true; document.body.style.overflow = ""; RD = null; RD_RUN = null; }

  function rdTab(name) {
    document.querySelectorAll(".tab[data-rd]").forEach((t) =>
      t.setAttribute("aria-selected", String(t.dataset.rd === name)));
    ["overview", "groups", "failures", "console", "config"].forEach((v) =>
      ($("rd-" + v).hidden = v !== name));
  }

  async function open({ runId = null, logName = null, run = null }) {
    RD_RUN = run;
    RD = runId
      ? await api(`/runs/${runId}/log/structured`)
      : await api(`/logs/${encodeURIComponent(logName)}`);
    render();
    rdTab("overview");
    rdOpen();
  }

  function render() {
    const d = RD, t = d.totals, cfg = d.setup.config || {}, r = RD_RUN;

    const scope = r
      ? `${r.channel} · ${r.category || "all categories"}`
      : (d.groups[0]
          ? `${d.groups[0].category} · ${d.groups[0].subcategory}`
          : d.path);
    $("rd-title").textContent = scope;
    $("rd-sub").textContent = r
      ? `${r.engine === "himalaya" ? "Himalaya" : "Competitor"} engine · run execution details`
      : "Log file · run execution details";

    const state = t.failed ? (t.successful ? "partial" : "failed") : "ok";
    $("rd-status").innerHTML = state === "ok"
      ? '<span class="pill p-ok"><i class="sq"></i>Success</span>'
      : state === "partial"
        ? '<span class="pill p-rev"><i class="sq"></i>Partial failure</span>'
        : '<span class="pill p-fail"><i class="sq"></i>Failed</span>';

    /* A log file has no run row, so started/completed fall back to the first
       and last timestamps in the log itself. Showing "—" for a run that plainly
       did happen reads as broken. */
    const startedAt = r?.started_at || d.started_at;
    const endedAt = r?.ended_at || d.ended_at;
    const when = (v) => v
      ? new Date(v).toLocaleString(undefined,
          { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
      : "—";

    $("rd-meta").innerHTML = [
      ["Run ID", r?.execution_id || cfg.run_id || "—", true],
      ["Source", cfg.source_table || (r ? `staging.${r.channel}_products` : "—")],
      ["Master", cfg.master_table || "staging.himalaya_products"],
      ["Scope", r ? `${r.category || "all"} / ${r.subcategory || "all"}`
                  : (d.groups[0] ? `${d.groups[0].category} / ${d.groups[0].subcategory}` : "—")],
      ["Started", when(startedAt)],
      ["Completed", when(endedAt)],
    ].map(([k, v, mono]) =>
      `<div><label>${esc(k)}</label>
         <b class="${mono ? "mono" : ""}" title="${esc(v)}">${esc(v)}</b></div>`).join("");

    /* Submitted is the sum of the per-group batches, which is what actually
       reached the worker pool -- not the scope, which still counts rows the
       crosswalk short-circuited away before any work happened. */
    const submitted = d.groups.reduce((a, g) => a + g.batch_size, 0);
    const shorted = d.groups.reduce((a, g) => a + g.crosswalk_resolved, 0);
    const pct = submitted ? Math.round((t.successful / submitted) * 100) : 0;

    $("rd-metrics").innerHTML = [
      ["Scoped records", fmt(r?.scoped_total ?? submitted), "Initial scope"],
      ["Auto-closed", fmt(shorted), "Crosswalk short-circuit", "var(--ok)"],
      ["Submitted to workers", fmt(submitted), "After short-circuit"],
      ["Successful", fmt(t.successful), `${pct}% of submitted`, "var(--ok)"],
      ["Failed", fmt(t.failed), t.failed ? "Remain PENDING" : "None", t.failed ? "var(--crit)" : null],
    ].map(([k, v, note, colour]) =>
      `<div class="rd-metric"><label>${esc(k)}</label>
         <b${colour ? ` style="color:${colour}"` : ""}>${v}</b>
         <small>${esc(note)}</small></div>`).join("");

    $("rd-c-groups").textContent = fmt(d.groups.length);
    $("rd-c-failures").textContent = fmt(d.failures.length);
    $("rd-c-console").textContent = fmt(d.warnings.length);
    $("rd-file").textContent = `Log file: ${d.path} · ${fmt(d.counts.lines)} lines`
      + (d.counts.unparsed ? ` · ${fmt(d.counts.unparsed)} unrecognised` : "");

    renderOverview(d, t);
    renderGroups(d);
    renderFailures(d);
    renderConsole(d);
    renderConfig(d, r);
  }

  function renderOverview(d, t) {
    const banner = t.failed
      ? `<div class="note" style="margin:0 0 14px;background:var(--crit-soft);
           border:1px solid color-mix(in srgb,var(--crit) 32%,transparent)">
           <div><b>Partial failure:</b> ${fmt(t.successful)} SKUs completed,
             ${fmt(t.failed)} failed across ${fmt(t.groups_with_failures)} group${
               t.groups_with_failures === 1 ? "" : "s"} and remain PENDING.
             ${t.all_failures_retryable
               ? " All are SQL&nbsp;40001 deadlocks — transient, retryable at fewer workers."
               : ""}</div></div>`
      : `<div class="note" style="margin:0 0 14px;background:var(--ok-soft);
           border:1px solid color-mix(in srgb,var(--ok) 30%,transparent)">
           <div><b>Completed:</b> ${fmt(t.successful)} SKUs processed across
             ${fmt(t.groups)} group${t.groups === 1 ? "" : "s"}, none failed.</div></div>`;

    /* Each step logs a key=value fragment. Read verbatim -- "groups=1",
       "master_records=3662" -- it is a log dump, not a narrative. These say
       what the step did, in the words someone would use describing it. */
    const SAY = {
      "1":  (v) => `Scope resolved to ${fmt(v.groups || 0)} category group${
                     (v.groups || 0) === 1 ? "" : "s"}.`,
      "2":  (v) => `Loaded ${fmt(v.master_records || 0)} master records from the Himalaya catalogue.`,
      "3":  (v) => `Built lookups over ${fmt(v.master_lookup || 0)} master products.`,
      "4":  (v) => `Resolved ${fmt(v.mappings || 0)} category mapping${
                     (v.mappings || 0) === 1 ? "" : "s"}.`,
      "5":  (v) => `Prepared ${fmt(v.eligible_groups || 0)} eligible master group${
                     (v.eligible_groups || 0) === 1 ? "" : "s"}.`,
      "16": () => "Source rows marked completed.",
    };

    function phrase(step) {
      const v = {};
      (step.detail || "").split("|").forEach((part) => {
        const [k, val] = part.split("=").map((x) => (x || "").trim());
        if (k && val !== undefined) v[k] = Number(val) || val;
      });
      const say = SAY[step.step];
      if (say) return say(v);
      /* Unknown step: show the detail rather than inventing a sentence for it,
         so a new log line appears instead of disappearing. */
      return step.detail ? esc(step.detail) : `Step ${esc(step.step)} completed.`;
    }

    const rows = [];
    d.setup.steps.forEach((st) =>
      rows.push(["info", "STEP " + st.step, st.at, phrase(st)]));

    d.groups.slice(0, 40).forEach((g) => {
      const short = g.crosswalk_resolved
        ? ` ${fmt(g.crosswalk_resolved)} closed by the approved crosswalk before scoring.` : "";
      rows.push([g.failed ? "warn" : "info", "GROUP", g.at,
        `<mark>${esc(g.category)} / ${esc(g.subcategory)}</mark> — `
        + `${fmt(g.batch_size)} SKUs submitted to ${fmt(g.eligible_master || 0)} master candidates, `
        + `${fmt(g.successful)} matched`
        + (g.failed ? `, <b>${fmt(g.failed)} failed</b>` : "")
        + `.${short}`]);
    });

    d.warnings.filter((w) => w.level === "ERROR").slice(0, 10).forEach((w) =>
      rows.push(["err", "ERROR", w.at, esc(w.message)]));

    $("rd-overview").innerHTML = banner
      + `<div class="sec-t">Execution flow</div><div class="flow">`
      + rows.map(([lvl, tag, at, msg]) =>
          `<div class="flow-row"><div class="lvl ${lvl}">${esc(tag)}</div>
             <div class="at">${esc(at || "")}</div><div class="msg">${msg}</div></div>`).join("")
      + `</div>`
      + (d.groups.length > 40
          ? `<p class="hint" style="margin-top:8px">Showing the first 40 groups — see the
               Groups tab for all ${fmt(d.groups.length)}.</p>` : "");
  }

  function renderGroups(d) {
    const groups = [...d.groups].sort((a, b) => b.failed - a.failed);
    $("rd-groups").innerHTML = `<div class="tw tw-short"><table>
        <thead><tr><th>Category</th><th>Sub-category</th><th class="num">Submitted</th>
          <th class="num">OK</th><th class="num">Failed</th><th class="num">Pool</th>
          <th class="num">Time</th></tr></thead>
        <tbody>${groups.length ? groups.map((g) => `<tr>
          <td class="cell-cat">${esc(g.category || "")}</td>
          <td class="cell-cat">${esc(g.subcategory || "")}</td>
          <td class="num">${fmt(g.batch_size)}</td>
          <td class="num">${fmt(g.successful)}</td>
          <td class="num" ${g.failed ? 'style="color:var(--crit);font-weight:600"' : ""}>${fmt(g.failed)}</td>
          <td class="num">${g.eligible_master == null ? "—" : fmt(g.eligible_master)}</td>
          <td class="num">${g.seconds == null ? "—" : duration(Math.round(g.seconds))}</td>
        </tr>`).join("")
        : `<tr><td colspan="7" class="empty">No groups completed.</td></tr>`}</tbody></table></div>`;
  }

  function renderFailures(d) {
    if (!d.failures.length) {
      $("rd-failures").innerHTML = `<p class="empty">No SKUs failed.</p>`;
      return;
    }
    /* One error text usually explains every failure in the run. Leading with
       the pattern turns 95 rows into one finding plus its evidence. */
    const byError = {};
    d.failures.forEach((f) => {
      const key = (f.error || "No error recorded").replace(/\(Process ID \d+\)/, "(Process ID N)");
      (byError[key] ||= []).push(f);
    });
    const patterns = Object.entries(byError).sort((a, b) => b[1].length - a[1].length);

    $("rd-failures").innerHTML = patterns.map(([err, list]) =>
      `<div class="note" style="margin:0 0 12px;background:var(--crit-soft);
         border:1px solid color-mix(in srgb,var(--crit) 32%,transparent);display:block">
         <b>${fmt(list.length)} SKU${list.length === 1 ? "" : "s"} —
           ${list[0].retryable ? "SQL 40001 deadlock, retryable" : "needs investigating"}</b>
         <div class="m" style="font-size:11px;line-height:1.6;margin-top:6px;
              color:var(--ink-2);overflow-wrap:anywhere">${esc(err)}</div>
       </div>`).join("")
      + `<div class="sec-t">Failed SKUs</div>
         <div class="tw tw-short"><table>
           <thead><tr><th>Source ID</th><th>SKU</th><th>Group</th><th class="num">Disposition</th></tr></thead>
           <tbody>${d.failures.map((f) => `<tr>
             <td class="m">${esc(f.source_id || "—")}</td>
             <td class="m">${esc(f.sku)}</td>
             <td class="cell-cat">${esc(f.category || "—")}
               <div class="sc">${esc(f.subcategory || "")}</div></td>
             <td class="num">${f.retryable
               ? '<span class="pill p-rev"><i class="sq"></i>Retry</span>'
               : '<span class="pill p-fail"><i class="sq"></i>Investigate</span>'}</td>
           </tr>`).join("")}</tbody></table></div>`;
  }

  function renderConsole(d) {
    $("rd-console").innerHTML = d.warnings.length
      ? `<div class="sec-t">Warnings and errors</div><div class="flow">`
        + d.warnings.map((w) => `<div class="flow-row">
            <div class="lvl ${w.level === "ERROR" ? "err" : "warn"}">${esc(w.level)}</div>
            <div class="at">${esc(w.at || "")}</div>
            <div class="msg">${esc(w.message)}</div></div>`).join("")
        + `</div>`
      : `<p class="empty">No warnings — every group resolved cleanly.</p>`;
  }

  function renderConfig(d, r) {
    const cfg = d.setup.config || {};
    const rows = [
      ["Source table", cfg.source_table || (r ? `staging.${r.channel}_products` : "—")],
      ["Master table", cfg.master_table || "staging.himalaya_products"],
      ["Batch size", cfg.batch_size || "—"],
      ["Max workers", cfg.max_workers || "—"],
      ["Category", r?.category || cfg.category || "all"],
      ["Sub-category", r?.subcategory || cfg.subcategory || "all"],
      ["SKU filter", cfg["sku filter"] || (r?.explicit_sku_count ? `${r.explicit_sku_count} SKUs` : "none (all)")],
      ["Engine", r ? (r.engine === "himalaya" ? "Himalaya (oneds_master)" : "Competitor (oneds_competitor)") : "—"],
      ["Triggered by", r?.triggered_by || "—"],
      ["Code version", r?.git_sha || "—"],
      ["Input population", r?.source_row_count ? fmt(r.source_row_count) + " rows" : "—"],
    ];
    $("rd-config").innerHTML = `<div class="tw tw-short"><table><tbody>${
      rows.map(([k, v]) =>
        `<tr><th style="width:190px;text-transform:none;letter-spacing:0;font-size:12px">${esc(k)}</th>
           <td class="m">${esc(String(v))}</td></tr>`).join("")}</tbody></table></div>`;
  }

  /* ---- wiring ---- */
  $("rd-close").addEventListener("click", rdClose);
  $("rd").addEventListener("click", (e) => { if (e.target === $("rd")) rdClose(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("rd").hidden) rdClose();
  });
  $("rd-copy").addEventListener("click", () => {
    const id = RD_RUN?.execution_id || RD?.setup?.config?.run_id || "";
    if (id) navigator.clipboard?.writeText(id);
  });
  $("rd-raw").addEventListener("click", async () => {
    const name = RD?.path;
    if (!name) return;
    const d = await api(`/logs/${encodeURIComponent(name)}?raw=true`);
    $("rd-console").innerHTML =
      `<div class="sec-t">Raw log — last ${fmt((d.raw_tail || []).length)} lines</div>
       <div class="log" style="max-height:none">${esc((d.raw_tail || []).join("\n"))}</div>`;
    rdTab("console");
  });
  document.querySelectorAll(".tab[data-rd]").forEach((t) =>
    t.addEventListener("click", () => rdTab(t.dataset.rd)));

  window.RunDetail = { open, close: rdClose };
})();
