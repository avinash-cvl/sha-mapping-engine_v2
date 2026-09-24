/* History: every recorded run, and every log file on disk.
 *
 * Log files live here rather than behind their own rail item, because a log
 * IS a historical run -- 1,092 of them predate audit.engine_run and are the
 * only record those runs left. Two rail entries answering "what happened
 * before" would make the reader choose between them before knowing which one
 * holds their run. Both views open the same detail dialog.
 */
(function () {
"use strict";

  const { api, esc, fmt, duration, pill, pager, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let ALL = [];               // every run fetched
  let SHOWN = [];             // after filters
  let PAGE = { offset: 0, limit: 25 };
  let PICKED = new Set();     // run ids ticked for comparison
  let LOGS = [];

  /* ------------------------------------------------------------ runs */

  async function load() {
    ALL = await api("/runs?limit=500");
    fillChannels();
    renderSummary();
    renderStaleBanner();
    applyFilters(0, PAGE.limit);
  }

  function fillChannels() {
    if ($("f-channel").dataset.filled) return;
    const names = [...new Set(ALL.map((r) => r.channel).filter(Boolean))].sort();
    const opts = `<option value="">All channels</option>`
      + names.map((c) => `<option>${esc(c)}</option>`).join("");
    $("f-channel").innerHTML = opts;
    $("x-channel").innerHTML = opts;
    $("f-channel").dataset.filled = "1";
  }

  /* Counted over what is loaded rather than queried separately: one source of
     numbers means the tiles and the table can never disagree. */
  function renderSummary() {
    const ok = ALL.filter((r) => r.status === "completed" && !r.failed).length;
    const partial = ALL.filter((r) => r.failed > 0).length;
    const failed = ALL.filter((r) => r.status === "failed" || r.status === "stale").length;
    const skus = ALL.reduce((a, r) => a + (r.processed || 0), 0);
    const channels = new Set(ALL.map((r) => r.channel)).size;

    $("hsum").innerHTML = [
      ["Runs recorded", fmt(ALL.length), `Across ${channels} channel${channels === 1 ? "" : "s"}`, ""],
      ["Successful", fmt(ok), "Completed without failures", "ok"],
      ["Partial failures", fmt(partial), "Require retry or review", "warn"],
      ["Failed or stale", fmt(failed), "Execution stopped", "crit"],
      ["SKUs processed", fmt(skus), "Across all recorded runs", ""],
    ].map(([k, v, note, cls]) =>
      `<div><label>${esc(k)}</label><b class="${cls}">${v}</b><small>${esc(note)}</small></div>`
    ).join("");
  }

  function renderStaleBanner() {
    const stale = ALL.filter((r) => r.status === "stale");
    $("stale-banner").hidden = !stale.length;
    if (!stale.length) return;
    $("stale-banner-text").innerHTML =
      `<b>${fmt(stale.length)} run${stale.length === 1 ? "" : "s"} stopped without finalising.</b>
       They are excluded from the success figures above.
       <a href="#alerts" data-route="alerts">Reconcile them in Alerts</a>.`;
  }

  function applyFilters(offset, limit) {
    PAGE = { offset, limit };
    const ch = $("f-channel").value, en = $("f-engine").value;
    const oc = $("f-outcome").value, q = $("f-q").value.trim().toLowerCase();

    SHOWN = ALL.filter((r) => {
      if (ch && r.channel !== ch) return false;
      if (en && r.engine !== en) return false;
      if (oc === "completed_with_failures") { if (!(r.status === "completed" && r.failed)) return false; }
      else if (oc === "completed") { if (!(r.status === "completed" && !r.failed)) return false; }
      else if (oc && r.status !== oc) return false;
      if (q) {
        const hay = `${r.execution_id} ${r.category || ""} ${r.subcategory || ""} ${r.channel}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const page = SHOWN.slice(offset, offset + limit);
    $("rows").innerHTML = page.length
      ? page.map(renderRow).join("")
      : `<tr><td colspan="9" class="empty">No runs match these filters.</td></tr>`;

    $("h-pager").innerHTML = "";
    $("h-pager").appendChild(pager({
      total: SHOWN.length, offset, limit,
      onGo: (o, l) => applyFilters(o, l),
    }));

    wireRows();
  }

  function renderRow(r) {
    const stale = r.status === "stale";
    const started = r.started_at ? new Date(r.started_at) : null;
    /* "<1s" rather than duration()'s "—" for a sub-second run: a run that had
       nothing to do finishes in ~0.3s, and a dash there reads as missing
       data rather than as "instant". */
    const ms = (r.started_at && r.ended_at)
      ? new Date(r.ended_at) - new Date(r.started_at) : null;
    const dur = ms == null ? "—" : (ms < 1000 ? "<1s" : duration(Math.round(ms / 1000)));

    /* Percentage against what was actually attempted. to_run is the figure the
       operator confirmed, so a run that processed 471 of 485 reads as 97% --
       not as a fraction of a scope that included rows the crosswalk closed. */
    const target = r.to_run || r.processed || 0;
    const pct = target ? Math.round((r.processed / target) * 100) : 0;
    const barClass = r.failed ? (r.processed ? "warn" : "crit") : "ok";

    return `<tr class="${stale ? "stale" : ""}" data-id="${esc(r.execution_id)}">
      <td><input type="checkbox" class="pick" value="${esc(r.execution_id)}" ${stale ? "disabled" : ""}
          ${PICKED.has(r.execution_id) ? "checked" : ""}
          aria-label="Select run ${esc((r.execution_id || "").slice(0, 8))}"></td>
      <td><div class="run-main">
        <div class="run-icon">${stale ? "!" : "&#9654;"}</div>
        <div><div class="run-name">${esc(r.channel)} &middot; ${esc(r.category || "all categories")}</div>
          <div class="run-id">${esc(r.execution_id)}</div></div>
      </div></td>
      <td><span class="tag">${esc(r.channel)}</span></td>
      <td><b style="font-size:12.5px">${esc(r.category || "all")}</b>
        <div class="t-sub">${esc(r.subcategory || "all sub-categories")}</div></td>
      <td class="t-sub" style="margin:0">${started ? started.toLocaleString() : "—"}</td>
      <td class="num">${dur}</td>
      <td>${pill(r.status)}</td>
      <td>${stale ? '<span class="t-sub">unknown</span>'
        : (!target ? '<span class="t-sub">nothing to run</span>'
        : `<div class="prog">
          <div class="prog-top"><span>${fmt(r.processed)}/${fmt(target)}</span><b>${pct}%</b></div>
          <div class="prog-bar"><i class="${barClass}" style="width:${pct}%"></i></div>
        </div>`)}</td>
      <td style="white-space:nowrap">
        <button class="btn open rd-open" data-run="${esc(r.execution_id)}">View details</button>
        <button class="btn sm run-export" data-run="${esc(r.execution_id)}"
          title="Export this run's mapping results as CSV">Export</button>
      </td>
    </tr>`;
  }

  function wireRows() {
    $("rows").querySelectorAll(".rd-open").forEach((b) =>
      b.addEventListener("click", async () => {
        const run = ALL.find((r) => r.execution_id === b.dataset.run);
        b.disabled = true;
        try {
          await window.RunDetail.open({ runId: b.dataset.run, run });
        } catch (e) {
          showError($("errors"), e.message);
        } finally {
          b.disabled = false;
        }
      }));

    /* A run export covers what that run's SCOPE holds, not a stored per-SKU
       list -- audit.engine_run records the scope it was launched with, and
       the confirmation says so rather than implying a record that does not
       exist. */
    $("rows").querySelectorAll(".run-export").forEach((b) =>
      b.addEventListener("click", () => exportRun(b.dataset.run)));

    $("rows").querySelectorAll(".pick").forEach((cb) =>
      cb.addEventListener("change", () => {
        if (cb.checked) PICKED.add(cb.value); else PICKED.delete(cb.value);
        /* Exactly two, because a comparison is between two things. More
           than two would need a policy on which pair to use, and any policy
           there is a guess about intent. */
        $("cmp-open").disabled = PICKED.size !== 2;
        $("cmp-open").textContent = PICKED.size
          ? `Compare selected (${PICKED.size})` : "Compare selected";
      }));
  }

  /* ------------------------------------------------------------ export */

  /* A download the browser starts itself, not a fetch.
   *
   * The file is tens of thousands of rows; pulling it into JS memory to make
   * a blob would hold it twice for no benefit, and the server already sends
   * Content-Disposition. A plain navigation lets the browser stream it
   * straight to disk.
   */
  function download(params) {
    const url = "/api/engine/export?" + new URLSearchParams(params);
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function exportRun(runId) {
    const run = ALL.find((r) => r.execution_id === runId);
    let preview;
    try {
      preview = await api("/export/preview?scope=unreviewed&run_id="
        + encodeURIComponent(runId));
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 4v10M8 10l4 4 4-4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 16.5v2A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5v-2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
       Export this run`;
    $("confirm-lede").textContent =
      "A CSV with the same columns as the SQL the team shares — ready to send on.";
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>The run</h3>
         <dl class="kv">
           <dt>Run</dt><dd>${esc(runId.slice(0, 8))}</dd>
           <dt>Scope</dt><dd>${esc(preview.channel || "all channels")}${
             preview.category ? " · " + esc(preview.category) : ""}</dd>
           <dt>Engine</dt><dd>${esc(run?.engine || preview.run?.engine || "—")}</dd>
           <dt>Processed</dt><dd>${fmt(run?.processed ?? preview.run?.processed ?? 0)} SKUs</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>What the file holds</h3>
         <dl class="kv">
           <dt>Rows</dt><dd class="big">${fmt(preview.rows)}</dd>
           <dt>Columns</dt><dd>${preview.columns.length}</dd>
           <dt>Filter</dt><dd>awaiting review · Himalaya brand</dd>
         </dl>
         <p class="hint" style="margin-top:8px">Every candidate rank is included, so a
           SKU appears up to three times — the reviewer needs to see what was passed
           over, not only what won.</p>
       </div>
       <div class="m-sec">
         <h3>One thing to know</h3>
         <ul class="m-rules">
           <li>A run records the <b>scope</b> it covered, not the individual SKUs it
             touched. This exports everything currently awaiting review in that scope,
             which is usually more than this run alone processed.</li>
         </ul>
       </div>`;
    $("confirm-go").textContent = `Download ${fmt(preview.rows)} rows`;
    $("confirm-go").disabled = !preview.rows;

    window.ConfirmDialog.open(async () => {
      download({ scope: "unreviewed", run_id: runId });
    });
  }

  async function refreshExportCount() {
    const params = { scope: $("x-scope").value };
    if ($("x-channel").value) params.channel = $("x-channel").value;
    if ($("x-brand").value) params.brand = $("x-brand").value;
    $("x-count").textContent = "counting…";
    try {
      const d = await api("/export/preview?" + new URLSearchParams(params));
      $("x-count").textContent = `${fmt(d.rows)} rows · ${d.columns.length} columns`;
      $("x-go").disabled = !d.rows;
    } catch (e) {
      $("x-count").textContent = e.message;
      $("x-go").disabled = true;
    }
  }

  /* ------------------------------------------------------------ compare */

  async function compare() {
    const [a, b] = [...PICKED];
    /* Ordered by start time so "before" and "after" mean what they say,
       whichever order they were ticked in. */
    const ra = ALL.find((r) => r.execution_id === a);
    const rb = ALL.find((r) => r.execution_id === b);
    const [before, after] = new Date(ra.started_at) <= new Date(rb.started_at) ? [a, b] : [b, a];

    $("cmp").hidden = false;
    $("cmp-body").innerHTML = `<div class="loading">Comparing</div>`;
    try {
      const d = await api(`/compare?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`);
      renderCompare(d);
    } catch (e) {
      $("cmp-body").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    }
  }

  function renderCompare(d) {
    const label = (r) => `${(r.execution_id || "").slice(0, 8)} · ${
      r.started_at ? new Date(r.started_at).toLocaleDateString(undefined,
        { day: "2-digit", month: "short" }) : "—"} · ${
      r.engine === "himalaya" ? "Himalaya" : "Competitor"} · ${r.channel}`;

    $("cmp-body").innerHTML =
      `<div class="grp">${esc(label(d.before))} → ${esc(label(d.after))}</div>
       <div class="tw tw-short"><table>
         <thead><tr><th>Outcome</th><th class="num">Before</th><th class="num">After</th>
           <th class="num">Δ</th></tr></thead>
         <tbody>${d.rows.map((r) => `<tr>
           <td>${pill(r.status)}</td>
           <td class="num">${fmt(r.before)}</td>
           <td class="num">${fmt(r.after)}</td>
           <td class="num" style="color:${r.delta > 0 ? "var(--ok)" : r.delta < 0 ? "var(--crit)" : "var(--ink-3)"};font-weight:600">${
             r.delta > 0 ? "+" : ""}${fmt(r.delta)}</td>
         </tr>`).join("")}</tbody></table></div>
       <div class="note ${d.flags.input_population_changed || d.flags.code_changed ? "" : "info"}">
         <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/></svg>
         <div>${esc(d.note)}
           ${d.flags.input_population_changed
             ? "<br><b>The input population changed between these runs</b> — the delta may be new rows rather than engine behaviour." : ""}
           ${d.flags.code_changed
             ? "<br><b>The code changed between these runs</b> — compare the config in each run's detail before attributing the delta." : ""}
         </div></div>`;
  }

  /* ------------------------------------------------------------ logs */

  async function loadLogs() {
    const q = $("log-q").value.trim();
    const d = await api("/logs?limit=200" + (q ? `&q=${encodeURIComponent(q)}` : ""));
    LOGS = d.files;
    $("c-logs").textContent = fmt(d.total);

    $("log-rows").innerHTML = LOGS.length
      ? LOGS.map((f) => `<tr>
          <td class="m">${esc(f.label)}
            <div class="t-sub">${esc(f.name)}</div></td>
          <td>${f.name.includes("competitor")
            ? '<span class="pill p-comp"><i class="sq"></i>Competitor</span>'
            : '<span class="pill p-cat"><i class="sq"></i>Himalaya</span>'}</td>
          <td class="t-sub" style="margin:0">${new Date(f.modified).toLocaleString()}</td>
          <td class="num">${(f.size_bytes / 1048576).toFixed(1)} MB</td>
          <td class="num">${f.run_id ? esc(f.run_id.slice(0, 8)) : "—"}</td>
          <td><button class="btn open log-open" data-log="${esc(f.name)}">View details</button></td>
        </tr>`).join("")
      : `<tr><td colspan="6" class="empty">No log files match.</td></tr>`;

    $("log-rows").querySelectorAll(".log-open").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        b.textContent = "Parsing…";
        try {
          await window.RunDetail.open({ logName: b.dataset.log });
        } catch (e) {
          showError($("errors"), e.message);
        } finally {
          b.disabled = false;
          b.textContent = "View details";
        }
      }));
  }

  /* ------------------------------------------------------------ wiring */

  ["f-channel", "f-engine", "f-outcome"].forEach((id) =>
    $(id).addEventListener("change", () => applyFilters(0, PAGE.limit)));

  let qTimer = null;
  $("f-q").addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => applyFilters(0, PAGE.limit), 250);
  });

  $("f-clear").addEventListener("click", () => {
    ["f-channel", "f-engine", "f-outcome", "f-q"].forEach((id) => ($(id).value = ""));
    applyFilters(0, PAGE.limit);
  });

  ["x-scope", "x-channel", "x-brand"].forEach((id) =>
    $(id).addEventListener("change", () => refreshExportCount().catch(() => {})));

  $("x-go").addEventListener("click", () => {
    const params = { scope: $("x-scope").value };
    if ($("x-channel").value) params.channel = $("x-channel").value;
    if ($("x-brand").value) params.brand = $("x-brand").value;
    download(params);
  });

  $("cmp-open").addEventListener("click", () => compare().catch(() => {}));
  $("cmp-close").addEventListener("click", () => { $("cmp").hidden = true; });

  let logTimer = null;
  $("log-q").addEventListener("input", () => {
    clearTimeout(logTimer);
    logTimer = setTimeout(() => loadLogs().catch(() => {}), 300);
  });

  let logsLoaded = false;
  document.querySelectorAll(".tab[data-view]").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab[data-view]").forEach((x) =>
        x.setAttribute("aria-selected", String(x === t)));
      $("view-runs").hidden = t.dataset.view !== "runs";
      $("view-logs").hidden = t.dataset.view !== "logs";
      /* The comparison belongs to the runs view; leaving it on screen under
         the log browser would attach it to the wrong thing. */
      if (t.dataset.view !== "runs") $("cmp").hidden = true;
      if (t.dataset.view === "logs" && !logsLoaded) {
        logsLoaded = true;
        loadLogs().catch((e) => { logsLoaded = false; showError($("errors"), e.message); });
      }
    }));

  window.Pages = window.Pages || {};
  window.Pages["history"] = {
    async boot() {
      await load();
      /* The log count sits on a tab label, so it is fetched with the page
         even though the browser itself is not rendered until you open it. */
      api("/logs?limit=1").then((d) => ($("c-logs").textContent = fmt(d.total))).catch(() => {});
      refreshExportCount().catch(() => {});
    },
  };
})();
