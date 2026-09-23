/* Runs: the landing list, and the composer behind it.
 *
 * Two routes share this module because they share state -- the channel list,
 * the scope resolver and the live-run registry are the same on both, and
 * splitting them meant the list and the composer could disagree about what is
 * currently running.
 */
(function () {
"use strict";

  const { api, esc, fmt, duration, pill, pager, streamRun, showError, clearError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let CHANNELS = {};        // {channel: {category: [subcategory, ...]}}
  let RUNS = [];            // recent runs, for the landing table
  let PREVIEW = null;       // last resolved scope
  let PLAN = null;          // last confirmation plan
  let QUEUE = [];           // runs launched from this browser, in order
  let STREAMS = [];         // open EventSources, closed on teardown

  /* ------------------------------------------------------------ helpers */

  const enginePill = (e) => e === "himalaya"
    ? '<span class="pill p-cat"><i class="sq"></i>Himalaya</span>'
    : '<span class="pill p-comp"><i class="sq"></i>Competitor</span>';

  /* The engine writes `status`; "stale" is what reconcile_stale() sets on a
     run whose heartbeat stopped. It is not a status the engine ever emits
     itself, which is exactly why it needs its own vocabulary on screen. */
  const outcomePill = (r) => {
    if (r.status === "completed" && r.failed) {
      return '<span class="pill p-rev"><i class="sq"></i>Completed with failures</span>';
    }
    return pill(r.status);
  };

  /* Console.duration() returns "—" below one second, which is right for a
     match run and wrong for a run that had nothing to do and finished in
     0.3s. A dash there reads as missing data rather than as "instant". */
  function runDuration(a, b) {
    if (!a || !b) return "—";
    const ms = new Date(b) - new Date(a);
    if (ms < 1000) return "<1s";
    return duration(Math.round(ms / 1000));
  }

  /* The audit trail stores the full address; the column shows the local part.
     Eight rows of "@covalenseglobal.com" is the same word eight times. */
  const who = (email) => (email || "unknown").split("@")[0];

  /* ------------------------------------------------------------ landing */

  async function loadRuns() {
    RUNS = await api("/runs?limit=200");
    renderTiles();
    renderRecent();
  }

  function renderTiles() {
    const today = new Date().toDateString();
    const active = RUNS.filter((r) => r.status === "running").length;
    const stuck = RUNS.filter((r) => r.status === "stale").length;
    const failedToday = RUNS.filter((r) =>
      r.started_at && new Date(r.started_at).toDateString() === today
      && (r.failed > 0 || r.status === "failed")).length;

    $("tile-active").textContent = fmt(active);
    /* Queued counts only what this browser launched and is still waiting on.
       The server has no queue table -- "Both" launches two runs at once --
       so claiming a system-wide queue depth would be inventing a number. */
    $("tile-queued").textContent = fmt(QUEUE.filter((q) => q.state === "queued").length);
    $("tile-failed").textContent = fmt(failedToday);
    $("tile-stuck").textContent = fmt(stuck);

    if (stuck) {
      const oldest = RUNS.filter((r) => r.status === "stale")
        .map((r) => r.started_at).filter(Boolean).sort()[0];
      $("stuck-note").hidden = false;
      $("stuck-note-text").innerHTML =
        `<b>${fmt(stuck)} run${stuck === 1 ? " is" : "s are"} stuck in "running."</b> `
        + (oldest ? `The oldest has had no heartbeat since ${
              new Date(oldest).toLocaleDateString(undefined, { day: "2-digit", month: "short" })}. ` : "")
        + `They crashed without finalising. `
        + `<a href="#alerts" data-route="alerts">Reconcile them in Alerts</a> `
        + `so history stops counting them as active.`;
    } else {
      $("stuck-note").hidden = true;
    }
  }

  function renderRecent() {
    const ch = $("rr-channel").value;
    const en = $("rr-engine").value;
    const st = $("rr-status").value;

    const rows = RUNS.filter((r) =>
      (!ch || r.channel === ch) && (!en || r.engine === en) && (!st || r.status === st));

    $("rr-rows").innerHTML = rows.length
      ? rows.slice(0, 8).map((r) => {
          const stale = r.status === "stale";
          /* A run whose scope was already fully mapped legitimately processes
             nothing. Shown as a bare 0 beside "Completed" it reads as a
             failure, so the row says what actually happened instead. */
          const noWork = !stale && !r.to_run && !r.processed;
          return `<tr class="${stale ? "stale" : ""}">
            <td class="m">${esc((r.execution_id || "").slice(0, 8))}
              <div class="t-sub">${r.started_at
                ? new Date(r.started_at).toLocaleString(undefined,
                    { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
                : "—"}</div></td>
            <td>${esc(r.channel)} · ${esc(r.category || "all categories")}
              ${r.subcategory ? `<div class="t-sub">${esc(r.subcategory)}</div>` : ""}</td>
            <td>${enginePill(r.engine)}</td>
            <td>${noWork
              ? '<span class="pill p-no"><i class="sq"></i>Nothing to run</span>'
              : outcomePill(r)}</td>
            <td class="num">${stale ? "—" : fmt(r.processed)}${
              noWork ? `<div class="t-sub">${fmt(r.already_mapped)} already mapped</div>` : ""}</td>
            <td class="num" ${r.failed ? 'style="color:var(--crit)"' : ""}>${
              stale ? "—" : fmt(r.failed)}</td>
            <td class="num">${stale ? "—" : runDuration(r.started_at, r.ended_at)}</td>
            <td class="m">${esc(who(r.triggered_by))}</td>
            <td><button class="btn open rd-open" data-run="${esc(r.execution_id)}">View details</button></td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="9" class="empty">No runs match these filters.</td></tr>`;

    $("rr-cap").textContent = rows.length
      ? `Showing ${Math.min(8, rows.length)} of ${fmt(rows.length)} runs`
      : "";

    $("rr-rows").querySelectorAll(".rd-open").forEach((b) =>
      b.addEventListener("click", async () => {
        const run = RUNS.find((r) => r.execution_id === b.dataset.run);
        b.disabled = true;
        try {
          await window.RunDetail.open({ runId: b.dataset.run, run });
        } catch (e) {
          showError($("errors"), e.message);
        } finally {
          b.disabled = false;
        }
      }));
  }

  /* ------------------------------------------------------------ composer */

  function fillChannels() {
    const names = Object.keys(CHANNELS);
    /* Deliberately NOT defaulting to the first channel.
       The engine runs one source table per invocation, so there is no "all
       channels" to offer -- and alphabetical order put amazon first, which
       is the 151k-row table. That parked a ~123-hour run one click behind
       the button before anyone had chosen anything. An unselected channel
       resolves no scope and disables the run button. */
    $("ch").innerHTML = `<option value="">Select a channel…</option>`
      + names.map((c) => `<option>${esc(c)}</option>`).join("");
    const opts = `<option value="">All channels</option>`
      + names.map((c) => `<option>${esc(c)}</option>`).join("");
    $("rr-channel").innerHTML = opts;
  }

  function fillCategories() {
    const cats = CHANNELS[$("ch").value] || {};
    $("cat").innerHTML = `<option value="">All categories</option>`
      + Object.keys(cats).map((c) => `<option>${esc(c)}</option>`).join("");
    /* A category list belongs to a channel, so offering one before a channel
       is chosen would show the previous channel's taxonomy. */
    $("cat").disabled = !$("ch").value;
    fillSubcategories();
  }

  function fillSubcategories() {
    const cats = CHANNELS[$("ch").value] || {};
    const subs = cats[$("cat").value] || [];
    $("sub").innerHTML = `<option value="">All sub-categories</option>`
      + subs.filter(Boolean).map((s) => `<option>${esc(s)}</option>`).join("");
    /* Sub-category is meaningless without a category: leaving it enabled
       invites a filter that silently matches nothing. */
    $("sub").disabled = !$("cat").value;
  }

  function currentEngine() {
    return document.querySelector('input[name="t"]:checked')?.value || "competitor";
  }

  const scopeMode = () =>
    document.querySelector('input[name="sb"]:checked')?.value || "category";

  /* The two modes are exclusive, not additive. Sending a category alongside a
     SKU list would let a stale select silently narrow a hand-picked run to
     nothing -- so whichever mode is off contributes null, and the panel hides
     its fields to match. */
  function scopeBody() {
    const bySkus = scopeMode() === "skus";
    const skus = $("skus").value.split(/[\s,\r\n]+/).filter(Boolean);
    return {
      channel: $("ch").value,
      engine: currentEngine(),
      category: bySkus ? null : ($("cat").value || null),
      subcategory: bySkus ? null : ($("sub").value || null),
      skus: bySkus && skus.length ? skus : null,
    };
  }

  function applyScopeMode() {
    const bySkus = scopeMode() === "skus";
    $("scope-by-category").hidden = bySkus;
    $("scope-by-skus").hidden = !bySkus;
    scheduleResolve();
  }

  /* Resolve is debounced because every control change triggers it and the
     query counts rows across a 151k-row table. */
  let resolveTimer = null;
  function scheduleResolve() {
    clearTimeout(resolveTimer);
    resolveTimer = setTimeout(resolve, 250);
  }

  async function resolve() {
    if (!$("ch").value) {
      PREVIEW = null;
      renderScopeCounts();
      /* In SKU mode the missing channel is not a formality: a SKU is only
         unique within a channel's table, and the same code can exist on
         several. The message says which fact is blocking the resolve. */
      $("preview-b").innerHTML = scopeMode() === "skus"
        ? `<p class="empty">Pick a channel before pasting SKUs.<br>
             <span class="hint">Each channel is a separate source table, so the same
               SKU can exist on more than one — the engine cannot tell which you
               mean without the channel.</span></p>`
        : `<p class="empty">Pick a channel to resolve a scope.<br>
             <span class="hint">A run covers one channel — the engine reads a single
               source table, so there is no all-channels mode.</span></p>`;
      $("preview-at").textContent = "";
      return;
    }
    $("preview-b").innerHTML = `<div class="loading">Resolving scope</div>`;
    try {
      /* Asked for BOTH legs regardless of which is selected, so the count
         beside Himalaya and the one beside Competitor are always live.
         Picking one must not blank the other's figure. */
      const body = scopeBody();
      PREVIEW = await api("/scope", {
        method: "POST",
        body: JSON.stringify({ ...body, engine: null }),
      });
      renderScopeCounts();
      renderPreview();
    } catch (e) {
      $("preview-b").innerHTML = "";
      showError($("errors"), e.message);
    }
  }

  function legOf(engine) {
    return PREVIEW?.legs.find((l) => l.engine === engine) || null;
  }

  function renderScopeCounts() {
    const h = legOf("himalaya"), c = legOf("competitor");
    $("n-cat").textContent = h ? fmt(h.to_run) : "—";
    $("n-comp").textContent = c ? fmt(c.to_run) : "—";
  }

  function legCard(leg, cls, title, module) {
    return `<div class="leg ${cls}">
      <div class="leg-h"><b>${title}</b><span class="tag">${module}</span></div>
      <div class="leg-n">${fmt(leg.to_run)}<small>to run</small></div>
      <div class="bd">
        <div><span class="k">Approved — skipped</span><span class="v skip">${fmt(leg.approved_skipped)}</span></div>
        <div><span class="k">Already mapped</span><span class="v">${fmt(leg.already_mapped)}</span></div>
        <div><span class="k">Failed — resettable</span><span class="v ${
          leg.failed_resettable ? "bad" : ""}">${fmt(leg.failed_resettable)}</span></div>
        <div><span class="k">Scoped total</span><span class="v">${fmt(leg.scoped_total)}</span></div>
      </div>
    </div>`;
  }

  function renderPreview() {
    const engine = currentEngine();
    const leg = legOf(engine);
    if (!leg) return;

    const total = leg.to_run;
    const approved = leg.approved_skipped;
    const resettable = leg.failed_resettable;
    const est = PREVIEW.estimate;

    /* The estimate covers both legs; scaled to the one selected so a
       Himalaya run does not quote the combined duration. */
    const share = PREVIEW.legs.reduce((a, l) => a + l.to_run, 0);
    const scale = share ? total / share : 0;

    const split = `<div class="split" style="grid-template-columns:1fr">
        ${legCard(leg, engine === "himalaya" ? "cat" : "comp",
           engine === "himalaya" ? "Himalaya catalog" : "Competitive substitute",
           engine === "himalaya" ? "oneds_master" : "oneds_competitor")}
      </div>`;

    const bySkus = scopeMode() === "skus";
    const listed = $("skus").value.split(/[\s,\r\n]+/).filter(Boolean).length;

    $("preview-b").innerHTML = split
      + `<div class="meta" style="margin-top:14px">
           <div><span class="k">Total SKUs</span><span class="v">${fmt(total)}</span></div>
           <div><span class="k">LLM calls</span><span class="v">${
             $("llm").checked ? "≈ " + fmt(Math.round(est.llm_calls * scale)) : "none"}</span></div>
           <div><span class="k">Est. duration</span><span class="v">≈ ${
             duration(Math.round(est.seconds * scale))}</span></div>
           <div><span class="k">Scoped by</span><span class="v">${
             bySkus ? fmt(listed) + " listed" : "category"}</span></div>
         </div>`
      + (bySkus && listed && listed !== leg.scoped_total ? `<div class="note">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
           <div><b>${fmt(listed)} SKU${listed === 1 ? "" : "s"} pasted,
             ${fmt(leg.scoped_total)} found</b> on this channel for the selected engine.
             The rest are not in <span class="m">staging.${esc($("ch").value)}_products</span>,
             or belong to the other engine.</div></div>` : "")
      + (approved ? `<div class="note info">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/></svg>
           <div><b>${fmt(approved)} steward-approved SKU${approved === 1 ? "" : "s"} excluded</b>
             and cannot be overridden here.</div></div>` : "")
      + (resettable ? `<div class="note">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
           <div><b>${fmt(resettable)} row${resettable === 1 ? " sits" : "s sit"} at <span class="m">Failed</span></b>
             from a dead worker. A plain re-run skips them — tick reset below to include them.</div></div>` : "")
      + `<div class="actions">
           <button class="btn primary" id="go" ${total ? "" : "disabled"}>
             ${total ? `Run ${fmt(total)} SKU${total === 1 ? "" : "s"}` : "Nothing to run"}</button>
           ${resettable ? `<label class="stale" style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="reset-failed"> Reset &amp; include ${fmt(resettable)} failed</label>` : ""}
         </div>`;

    $("preview-at").textContent = "Resolved " + new Date().toLocaleTimeString();
    $("go").addEventListener("click", openConfirm);
  }

  /* ------------------------------------------------------------ confirm */

  /* The dialog is shared between launching a run and rejecting a match, so
     the confirm button dispatches to whatever set this rather than being
     hard-wired to one of them. */
  let CONFIRM_ACTION = null;

  function runBody() {
    return {
      ...scopeBody(),
      use_llm: $("llm").checked,
      workers: Number($("w").value),
      batch_size: Number($("bs").value) || 500,
      reset_failed: !!$("reset-failed")?.checked,
    };
  }

  async function openConfirm() {
    try {
      PLAN = await api("/plan", { method: "POST", body: JSON.stringify(runBody()) });
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }
    const p = PLAN;
    const blocked = p.blocked_by.length;

    /* Restored, because rejectMatch() borrows this dialog and rewrites both. */
    CONFIRM_ACTION = launch;
    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Confirm this run`;
    $("confirm-lede").textContent =
      "This writes mapping rows to the database and calls the LLM. "
      + "Read the scope below before confirming.";

    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>Environment</h3>
         <dl class="kv">
           <dt>Environment</dt><dd class="${p.environment.label === "PROD" ? "crit" : ""}">${esc(p.environment.label)}</dd>
           <dt>Database</dt><dd>${esc(p.environment.database)}</dd>
           <dt>Server</dt><dd>${esc(p.environment.server)}</dd>
           <dt>Triggered by</dt><dd>${esc(p.triggered_by)}</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Scope</h3>
         <dl class="kv">
           <dt>Channel</dt><dd>${esc(p.scope.channel)}</dd>
           <dt>Category</dt><dd>${esc(p.scope.category)}</dd>
           <dt>Sub-category</dt><dd>${esc(p.scope.subcategory)}</dd>
           <dt>SKUs to run</dt><dd class="big">${fmt(p.totals.to_run)}</dd>
           <dt>Approved, skipped</dt><dd>${fmt(p.totals.approved_skipped)}</dd>
           <dt>Already mapped</dt><dd>${fmt(p.totals.already_mapped)}</dd>
           <dt>Failed, resettable</dt><dd class="${p.totals.failed_resettable ? "warn" : ""}">${
             fmt(p.totals.failed_resettable)}${p.settings.reset_failed_first ? " — will be reset" : ""}</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Engines</h3>
         <div class="m-leg">${p.legs.map((l) => `<div>
           <b>${l.engine === "himalaya" ? "Himalaya" : "Competitor"} · ${esc(l.module)}</b>
           <dl class="kv"><dt>Filter</dt><dd>${esc(l.brand_filter)}</dd>
             <dt>To run</dt><dd class="big">${fmt(l.to_run)}</dd></dl>
         </div>`).join("")}</div>
       </div>
       <div class="m-sec">
         <h3>Cost &amp; settings</h3>
         <dl class="kv">
           <dt>LLM judge</dt><dd>${p.settings.use_llm ? "on" : "off"}</dd>
           <dt>LLM calls</dt><dd>${p.settings.use_llm ? "≈ " + fmt(p.totals.llm_calls) : "0"}</dd>
           <dt>Est. duration</dt><dd>≈ ${duration(p.totals.seconds)}</dd>
           <dt>Workers</dt><dd>${p.settings.workers}</dd>
           <dt>Candidates to judge</dt><dd>${p.settings.top_n_output} (locked)</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>What this run will do</h3>
         <ul class="m-rules">${p.rules.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
       </div>
       <div class="m-sec">
         <h3>Exact command${p.commands.length > 1 ? "s" : ""}</h3>
         ${p.commands.map((c) => `<div class="m-cmd">${esc(c.command)}</div>`).join("")}
       </div>
       ${blocked ? `<div class="m-sec"><div class="m-block">
           <b>Already running.</b> ${p.blocked_by.map((b) =>
             `${esc(b.engine)} · ${esc(b.run_id.slice(0, 8))}`).join(", ")}
           — the same scope cannot run twice at once.</div></div>`
        : `<div class="m-sec">
             <label class="m-ack"><input type="checkbox" id="ack">
               I have read the scope above and want to run
               ${fmt(p.totals.to_run)} SKU${p.totals.to_run === 1 ? "" : "s"}
               against <b>${esc(p.environment.label)}</b>.</label>
           </div>`}`;

    $("confirm-go").disabled = true;
    $("confirm-go").textContent = blocked ? "Blocked" : "Start run";
    $("ack")?.addEventListener("change", (e) => { $("confirm-go").disabled = !e.target.checked; });
    $("confirm").hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeConfirm() {
    $("confirm").hidden = true;
    document.body.style.overflow = "";
  }

  /* Errors and dialog lifecycle belong to the confirm handler that invokes
     this, so a failure here propagates rather than being swallowed. */
  async function launch() {
    const res = await api("/runs", { method: "POST", body: JSON.stringify(runBody()) });
    clearError($("errors"));
    startQueue(res.runs);
  }

  /* ------------------------------------------------------------ queue */

  function startQueue(runs) {
    /* One engine per launch now that "Both" is gone, but this still reads a
       list: /runs returns an array, and a tracker that assumed a single
       element would break silently the day it returns two. */
    QUEUE = runs.map((r) => ({
      run_id: r.run_id,
      engine: r.engine,
      log: r.log,
      processed: 0, failed: 0, to_run: 0,
      state: "running",
    }));
    $("queue-panel").hidden = false;
    $("cancelBtn").disabled = false;
    $("cancelNote").textContent = "Cancellation takes effect after the current SKU.";
    renderQueue();

    teardownStreams();
    QUEUE.forEach((q) => {
      const es = streamRun(q.run_id, {
        onProgress: (d) => {
          Object.assign(q, {
            processed: d.processed || 0,
            failed: d.failed || 0,
            to_run: d.to_run || 0,
            state: d.status || q.state,
          });
          renderQueue();
        },
        onDone: (d) => {
          q.state = d.status || "completed";
          Object.assign(q, { processed: d.processed ?? q.processed, failed: d.failed ?? q.failed });
          renderQueue();
          if (QUEUE.every((x) => x.state !== "running" && x.state !== "starting")) {
            onQueueFinished();
          }
        },
        onError: (msg) => { $("cancelNote").textContent = msg; },
      });
      STREAMS.push(es);
    });
  }

  function teardownStreams() {
    STREAMS.forEach((es) => { try { es.close(); } catch { /* already closed */ } });
    STREAMS = [];
  }

  function renderQueue() {
    $("queue-rows").innerHTML = QUEUE.map((q, i) => {
      const pct = q.to_run ? Math.round((q.processed / q.to_run) * 100) : 0;
      const colour = q.failed ? "var(--warn)" : "var(--ok)";
      return `<div class="q-row">
        <span class="ord">${i + 1}</span>
        ${enginePill(q.engine)}
        <span class="nm">${esc($("ch").value)} · ${esc($("cat").value || "all categories")}
          <span>${esc(q.run_id.slice(0, 8))}</span></span>
        <span class="q-bar"><i style="width:${pct}%;background:${colour}"></i></span>
        <span class="n">${fmt(q.processed)} / ${fmt(q.to_run)}</span>
      </div>`;
    }).join("");

    const processed = QUEUE.reduce((a, q) => a + q.processed, 0);
    const failed = QUEUE.reduce((a, q) => a + q.failed, 0);
    const total = QUEUE.reduce((a, q) => a + q.to_run, 0);
    const live = QUEUE.some((q) => q.state === "running" || q.state === "starting");

    $("progN").innerHTML = `${fmt(processed)}<small> / ${fmt(total)} total</small>`;
    $("progPill").innerHTML = live
      ? '<span class="pill p-rev"><i class="sq"></i>Running</span>'
      : (failed ? '<span class="pill p-rev"><i class="sq"></i>Finished with failures</span>'
                : '<span class="pill p-ok"><i class="sq"></i>Completed</span>');

    const donePct = total ? (processed / total) * 100 : 0;
    const failPct = total ? (failed / total) * 100 : 0;
    $("progBar").innerHTML =
      `<i style="width:${donePct}%;background:var(--ok)"></i>`
      + `<i style="width:${failPct}%;background:var(--crit)"></i>`;
    $("progBar").setAttribute("aria-valuenow", String(processed));
    $("progBar").setAttribute("aria-valuemax", String(total));

    $("progLegend").innerHTML =
      `<span><i class="sq" style="background:var(--ok)"></i>${fmt(processed - failed)} processed</span>
       <span><i class="sq" style="background:var(--crit)"></i>${fmt(failed)} failed</span>
       <span><i class="sq" style="background:var(--sunken);border:1px solid var(--line)"></i>${
         fmt(Math.max(0, total - processed))} queued</span>`;

    $("liveTag").className = live ? "live" : "live stopped";
    $("liveTag").innerHTML = live ? "<i></i>Streaming" : "<i></i>Stopped";

    renderStages(live, processed, total);
  }

  function renderStages(live, processed, total) {
    const anyStarted = QUEUE.some((q) => q.to_run > 0);
    const done = !live;
    const stages = [
      ["Scope resolved", "done", fmt(total) + " SKUs"],
      ["Index built", anyStarted ? "done" : "active", anyStarted ? "master loaded" : "loading"],
      ["Processing", done ? "done" : (anyStarted ? "active" : ""), `${fmt(processed)} / ${fmt(total)}`],
      ["Recorded", done ? "done" : "", done ? "history written" : "pending"],
    ];
    $("stages").innerHTML = stages.map(([label, state, sub], i) =>
      `<div class="stage ${state}">
         <div class="stage-dot">${state === "done"
           ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="m5 12 5 5 9-10" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
           : state === "active" ? "<i></i>" : String(i + 1)}</div>
         ${i < stages.length - 1 ? '<div class="stage-line"></div>' : ""}
         <span class="stage-label">${esc(label)}</span>
         <span class="stage-sub">${esc(sub)}</span>
       </div>`).join("");
  }

  async function onQueueFinished() {
    teardownStreams();
    $("cancelBtn").disabled = true;
    $("cancelNote").textContent = "Run finished. Its detail and log are recorded.";

    /* The finished run's detail is one click away rather than a page hop --
       the operator is already looking at the thing they want to inspect. */
    const first = QUEUE[0];
    $("queue-detail").hidden = false;
    $("queue-detail").onclick = async () => {
      try {
        await window.RunDetail.open({ runId: first.run_id });
      } catch (e) {
        showError($("errors"), e.message);
      }
    };

    await Promise.all([loadRuns().catch(() => {}), loadResults().catch(() => {})]);
  }

  $("cancelBtn").addEventListener("click", async () => {
    $("cancelBtn").disabled = true;
    $("cancelNote").textContent = "Cancelling — finishing the SKUs already in flight…";
    await Promise.all(QUEUE.filter((q) => q.state === "running").map((q) =>
      api(`/runs/${q.run_id}/cancel`, { method: "POST" }).catch(() => {})));
  });

  /* ------------------------------------------------------------ results */

  let RES = { offset: 0, limit: 25 };
  let LAST_RESULTS = null;       // the page currently rendered, for the reject dialog

  async function loadResults() {
    /* /results requires a channel and 400s without one. Before a channel is
       picked there is nothing to show, so the request is not made at all --
       a red console error on an untouched page is noise that trains people
       to ignore real ones. */
    if (!$("ch").value) {
      $("resBody").innerHTML =
        `<tr><td colspan="7" class="empty">Pick a channel to see its results.</td></tr>`;
      $("c-results").textContent = "0";
      $("c-failures").textContent = "0";
      $("resPager").innerHTML = "";
      return;
    }
    const params = new URLSearchParams({
      channel: $("ch").value,
      limit: String(RES.limit),
      offset: String(RES.offset),
    });
    if ($("cat").value) params.set("category", $("cat").value);
    if ($("sub").value) params.set("subcategory", $("sub").value);
    if ($("resEngine").value) params.set("engine", $("resEngine").value);
    if ($("resStatus").value) params.set("status", $("resStatus").value);
    if ($("resSearch").value.trim()) params.set("q", $("resSearch").value.trim());

    const d = await api("/results?" + params);
    renderResults(d);
    renderFailures(d);
  }

  function confCell(r) {
    if (r.score == null) return "—";
    const pct = Math.round(r.score * 100);
    const colour = r.status === "AutoMatch" ? "var(--ok)"
      : r.status === "StewardReview" ? "var(--warn)" : "var(--mute)";
    return `<span class="conf"><span class="meter"><i style="width:${pct}%;background:${colour}"></i></span>${
      r.score.toFixed(2)}</span>`;
  }

  /* Three states, three controls. An approved row is not re-decidable from
     here -- its crosswalk entry has already been written and the engine
     short-circuits on it, so offering Reject there only ever produced a 409
     the user could not act on. */
  function reviewCell(r) {
    const verdict = (r.review_status || "").trim().toLowerCase();
    if (verdict === "approved") {
      return '<span class="pill p-ok" title="Steward-approved — withdraw in the review portal">'
        + '<i class="sq"></i>Approved</span>';
    }
    if (verdict === "rejected") {
      return '<span class="pill p-fail"><i class="sq"></i>Rejected</span>';
    }
    if (!r.product_code) return "";
    return `<button class="btn sm rej-mark" data-sku="${esc(r.sku)}"
              title="Record that this match is wrong">Reject</button>`;
  }

  function renderResults(d) {
    /* Kept so the reject dialog can name the pairing without re-fetching the
       row the user is already looking at. */
    LAST_RESULTS = d;
    $("c-results").textContent = fmt(d.total);
    $("resBody").innerHTML = d.rows.length
      ? d.rows.map((r) => `<tr>
          <td>${enginePill(r.engine)}</td>
          <td class="t-title">${esc(r.title || r.sku)}
            <div class="t-sub m">${esc(r.brand || "")}${r.sku ? " · " + esc(r.sku) : ""}${
              r.pack ? " · " + esc(r.pack) : ""}</div></td>
          <td class="t-title">${esc(r.product_name || "No equivalent in master")}
            <div class="t-sub m">${esc(r.product_code || "")}</div></td>
          <td class="cell-cat">${esc(r.category || "—")}
            <div class="sc">${esc(r.subcategory || "")}</div></td>
          <td>${pill(r.status)}</td>
          <td class="num">${confCell(r)}</td>
          <td>${reviewCell(r)}</td>
        </tr>`).join("")
      : `<tr><td colspan="7" class="empty">No rows match these filters.</td></tr>`;

    $("resBody").querySelectorAll(".rej-mark").forEach((b) =>
      b.addEventListener("click", () => rejectMatch(b.dataset.sku)));

    $("resPager").innerHTML = "";
    $("resPager").appendChild(pager({
      total: d.total, offset: d.offset, limit: d.limit,
      onGo: (offset, limit) => { RES = { offset, limit }; loadResults().catch(() => {}); },
    }));

    /* Category filter options come from what the result set actually holds,
       so it can never offer a category with nothing behind it. */
    const cats = [...new Set(d.rows.map((r) => r.category).filter(Boolean))].sort();
    const cur = $("resCat").value;
    $("resCat").innerHTML = `<option value="">All categories</option>`
      + cats.map((c) => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");
  }

  function renderFailures(d) {
    const fails = d.rows.filter((r) => r.status === "Failed");
    $("c-failures").textContent = fmt(fails.length);
    $("fail-banner").innerHTML = fails.length
      ? `<div class="banner">
           <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
           <div><h4>${fmt(fails.length)} SKU${fails.length === 1 ? "" : "s"} on this page failed</h4>
             <p>These rows are marked <span class="m">Failed</span> — a plain re-run skips them
               until they are reset. Tick "Reset &amp; include failed" in the preview.</p></div>
         </div>`
      : "";
    $("failBody").innerHTML = fails.length
      ? fails.map((r) => `<tr>
          <td class="t-title">${esc(r.title || r.sku)}
            <div class="t-sub m">${esc(r.brand || "")}</div></td>
          <td class="t-title" style="color:var(--crit)">${esc(r.reasoning || "No reason recorded")}
            <div class="t-sub m">sku=${esc(r.sku || "—")}</div></td>
          <td><span class="pill p-fail"><i class="sq"></i>Failed</span></td>
          <td class="num"><span class="pill p-rev"><i class="sq"></i>Retry</span></td>
        </tr>`).join("")
      : `<tr><td colspan="4" class="empty">No failures on this page.</td></tr>`;
  }

  /* ------------------------------------------------------------ rejected */

  let REJ = { offset: 0, limit: 25 };

  async function loadRejected() {
    if (!$("ch").value) {
      $("rejBody").innerHTML =
        `<tr><td colspan="5" class="empty">Pick a channel to see its rejections.</td></tr>`;
      $("c-rejected").textContent = "0";
      $("offenders").innerHTML = "";
      $("rejPager").innerHTML = "";
      return;
    }
    const params = new URLSearchParams({
      channel: $("ch").value,
      limit: String(REJ.limit), offset: String(REJ.offset),
    });
    if ($("cat").value) params.set("category", $("cat").value);
    if ($("rejEngine").value) params.set("engine", $("rejEngine").value);
    if ($("rejSearch").value.trim()) params.set("q", $("rejSearch").value.trim());

    const [d, off] = await Promise.all([
      api("/rejected?" + params),
      api(`/rejected/offenders?channel=${encodeURIComponent($("ch").value)}`).catch(() => []),
    ]);

    $("c-rejected").textContent = fmt(d.total);
    $("rejCap").textContent = d.total
      ? `${fmt(d.total)} rejected on ${$("ch").value}` : "";

    /* A product code collecting several rejections is the decoy-node
       signature: a gate target too small to hold an alternative, so
       everything routed there comes back as the same product. Surfaced
       above the list because it is the finding, not a row in it. */
    const repeat = off.filter((o) => o.rejections > 1);
    $("offenders").innerHTML = repeat.length
      ? `<div class="note" style="margin-top:14px">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
           <div><b>${repeat.length} master product${repeat.length === 1 ? "" : "s"}
             attracting repeat rejections.</b>
             ${repeat.slice(0, 3).map((o) =>
               `<span class="m">${esc(o.product_code)}</span> ${esc(o.product_name || "")}
                — ${fmt(o.rejections)}&times;`).join("; ")}.
             A code that keeps being rejected is usually a gate target too small to
             offer an alternative — check it in
             <a href="#catalog" data-route="catalog">Catalog</a>.</div></div>`
      : "";

    $("rejBody").innerHTML = d.rows.length
      ? d.rows.map((r) => `<tr>
          <td class="t-title">${esc(r.title || r.sku)}
            <div class="t-sub m">${esc(r.brand || "")} · ${esc(r.sku)}</div></td>
          <td class="t-title" style="color:var(--crit)">${esc(r.rejected_name || "—")}
            <div class="t-sub m">${esc(r.rejected_code || "")}</div></td>
          <td class="t-sub" style="margin:0">${esc(who(r.reviewed_by))}
            ${r.reviewed_at ? `<div class="t-sub m">${new Date(r.reviewed_at)
              .toLocaleDateString(undefined, { day: "2-digit", month: "short" })}</div>` : ""}
            ${r.comment ? `<div class="t-sub" style="max-width:280px">${esc(r.comment)}</div>` : ""}</td>
          <td class="num">${r.score == null ? "—" : r.score.toFixed(2)}</td>
          <td><button class="btn sm rej-undo" data-sku="${esc(r.sku)}">Withdraw</button></td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">Nothing has been rejected on this channel.</td></tr>`;

    $("rejPager").innerHTML = "";
    $("rejPager").appendChild(pager({
      total: d.total, offset: d.offset, limit: d.limit,
      onGo: (offset, limit) => { REJ = { offset, limit }; loadRejected().catch(() => {}); },
    }));

    $("rejBody").querySelectorAll(".rej-undo").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(`/rejected?channel=${encodeURIComponent($("ch").value)}`
            + `&sku=${encodeURIComponent(b.dataset.sku)}`, { method: "DELETE" });
          await loadRejected();
        } catch (e) {
          showError($("errors"), e.message);
          b.disabled = false;
        }
      }));
  }

  /* The reason is the point of the record, so it gets a real field rather
     than window.prompt -- which is unstyled, truncates, and is suppressed
     outright in some embedded contexts. */
  function rejectMatch(sku) {
    const row = (LAST_RESULTS?.rows || []).find((r) => r.sku === sku);
    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Reject this match`;
    $("confirm-lede").textContent =
      "Recorded against this listing. It does not delete the mapping row — "
      + "it records that a human judged this pairing wrong.";
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>The pairing being rejected</h3>
         <dl class="kv">
           <dt>Listing</dt><dd>${esc(row?.title || sku)}</dd>
           <dt>SKU</dt><dd>${esc(sku)}</dd>
           <dt>Matched to</dt><dd class="crit">${esc(row?.product_name || "—")}</dd>
           <dt>Product code</dt><dd>${esc(row?.product_code || "—")}</dd>
           <dt>Score</dt><dd>${row?.score == null ? "—" : row.score.toFixed(2)}</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Why is it wrong?</h3>
         <textarea id="rej-comment" rows="3"
           placeholder="e.g. flavour mismatch — cocoa vs cherry"></textarea>
         <p class="hint" style="margin-top:8px">Recorded against the row with your name.
           The SKU stays eligible for re-runs — this is a verdict on the pairing,
           not on the listing.</p>
       </div>`;
    $("confirm-go").textContent = "Reject match";
    $("confirm-go").disabled = false;
    CONFIRM_ACTION = async () => {
      await api("/rejected", {
        method: "POST",
        body: JSON.stringify({
          channel: $("ch").value, sku,
          comment: $("rej-comment").value.trim() || null,
        }),
      });
      await Promise.all([loadResults(), loadRejected()]);
    };
    $("confirm").hidden = false;
    document.body.style.overflow = "hidden";
    $("rej-comment").focus();
  }

  /* ------------------------------------------------------------ gate */

  async function loadGate() {
    const rows = await api("/gate-warnings?limit=50");
    $("c-gate").textContent = fmt(rows.length);
    $("gateBody").innerHTML = rows.length
      ? rows.slice(0, 12).map((g) => `<tr>
          <td class="m">${esc(g.node)}
            <div class="t-sub">${esc(g.reached_from.slice(0, 3).join(", "))}${
              g.reached_from.length > 3 ? ` +${g.reached_from.length - 3}` : ""}</div></td>
          <td class="num" style="color:${g.rows ? "var(--warn)" : "var(--crit)"};font-weight:600">${fmt(g.rows)}</td>
          <td class="num">${fmt(g.reached_from.length)}</td>
          <td>${g.state === "does not exist"
            ? '<span class="pill p-fail"><i class="sq"></i>Does not exist</span>'
            : '<span class="pill p-rev"><i class="sq"></i>Starved</span>'}</td>
        </tr>`).join("")
      : `<tr><td colspan="4" class="empty">No starved gate targets.</td></tr>`;
  }

  /* ------------------------------------------------------------ wiring */

  ["rr-channel", "rr-engine", "rr-status"].forEach((id) =>
    $(id).addEventListener("change", renderRecent));

  /* The results table is scoped by the same channel/category as the run, so
     it follows the pickers rather than staying on whatever was loaded first. */
  const rescope = () => {
    RES.offset = 0; REJ.offset = 0;
    loadResults().catch(() => {});
    /* The rejected count sits on a tab label, so it follows the channel even
       while that tab is closed -- a tab reading 0 until you click it is
       worse than no count. */
    loadRejected().catch(() => {});
  };

  $("ch").addEventListener("change", () => { fillCategories(); scheduleResolve(); rescope(); });
  $("cat").addEventListener("change", () => { fillSubcategories(); scheduleResolve(); rescope(); });
  $("sub").addEventListener("change", () => { scheduleResolve(); rescope(); });
  $("skus").addEventListener("input", scheduleResolve);
  document.querySelectorAll('input[name="sb"]').forEach((r) =>
    r.addEventListener("change", applyScopeMode));
  $("llm").addEventListener("change", () => { if (PREVIEW) renderPreview(); });
  document.querySelectorAll('input[name="t"]').forEach((r) =>
    r.addEventListener("change", () => { if (PREVIEW) renderPreview(); }));

  $("confirm-cancel").addEventListener("click", closeConfirm);
  $("confirm-go").addEventListener("click", async () => {
    const action = CONFIRM_ACTION;
    if (!action) return;
    const label = $("confirm-go").textContent;
    $("confirm-go").disabled = true;
    $("confirm-go").textContent = "Working…";
    try {
      await action();
      closeConfirm();
    } catch (e) {
      showError($("errors"), e.message);
      closeConfirm();
    } finally {
      $("confirm-go").textContent = label;
    }
  });
  $("confirm").addEventListener("click", (e) => { if (e.target === $("confirm")) closeConfirm(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("confirm").hidden) closeConfirm();
  });

  let resTimer = null;
  $("resSearch").addEventListener("input", () => {
    clearTimeout(resTimer);
    resTimer = setTimeout(() => { RES.offset = 0; loadResults().catch(() => {}); }, 300);
  });
  ["resEngine", "resStatus", "resCat"].forEach((id) =>
    $(id).addEventListener("change", () => { RES.offset = 0; loadResults().catch(() => {}); }));

  document.querySelectorAll(".tab[data-tab]").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab[data-tab]").forEach((x) =>
        x.setAttribute("aria-selected", String(x === t)));
      document.querySelectorAll(".tabpanel[data-panel]").forEach((p) =>
        (p.hidden = p.dataset.panel !== t.dataset.tab));
      if (t.dataset.tab === "gate") loadGate().catch(() => {});
      if (t.dataset.tab === "rejected") {
        loadRejected().catch((e) => showError($("errors"), e.message));
      }
    }));

  let rejTimer = null;
  $("rejSearch").addEventListener("input", () => {
    clearTimeout(rejTimer);
    rejTimer = setTimeout(() => { REJ.offset = 0; loadRejected().catch(() => {}); }, 300);
  });
  $("rejEngine").addEventListener("change", () => {
    REJ.offset = 0; loadRejected().catch(() => {});
  });

  $("saved-last").addEventListener("click", () => {
    try {
      const s = JSON.parse(localStorage.getItem("console.scope") || "{}");
      if (s.channel) $("ch").value = s.channel;
      fillCategories();
      if (s.category) $("cat").value = s.category;
      fillSubcategories();
      if (s.subcategory) $("sub").value = s.subcategory;
      scheduleResolve();
    } catch { /* nothing saved yet */ }
  });

  /* ------------------------------------------------------------ boot */

  window.Pages = window.Pages || {};

  window.Pages["runs"] = {
    async boot() {
      CHANNELS = await api("/channels");
      fillChannels();
      fillCategories();
      await loadRuns();
    },
    refresh() { return loadRuns().catch(() => {}); },
  };

  window.Pages["runs-new"] = {
    async boot() {
      /* Shares the Runs boot: the channel list has to exist before the scope
         can resolve, and arriving straight on #runs-new must work. */
      if (!Object.keys(CHANNELS).length) {
        CHANNELS = await api("/channels");
        fillChannels();
        fillCategories();
      }
      await resolve();
      await loadResults().catch(() => {});
    },
  };

  /* Remember the scope so the next session opens where this one left off. */
  window.addEventListener("beforeunload", () => {
    try {
      localStorage.setItem("console.scope", JSON.stringify({
        channel: $("ch").value, category: $("cat").value, subcategory: $("sub").value,
      }));
    } catch { /* private window */ }
    teardownStreams();
  });
})();
