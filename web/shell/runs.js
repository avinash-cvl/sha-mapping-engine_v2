/* Runs: the landing list, and the composer behind it.
 *
 * Two routes share this module because they share state -- the channel list,
 * the scope resolver and the live-run registry are the same on both, and
 * splitting them meant the list and the composer could disagree about what is
 * currently running.
 */
(function () {
"use strict";

  const { api, esc, fmt, duration, pill, pager, streamRun, showError, clearError,
    llmToggle, bindLlmToggle } = window.Console;
  const $ = (id) => document.getElementById(id);

  let CHANNELS = {};        // {channel: {category: [subcategory, ...]}}
  let RUNS = [];            // recent runs, for the landing table
  let PREVIEW = null;       // last resolved scope
  let PLAN = null;          // last confirmation plan
  let QUEUE = [];           // runs launched from this browser, in order
  let STREAMS = [];         // open EventSources, closed on teardown
  /* Callbacks another page asked to be told about when the queue finishes --
     the Rejected page uses one to consume its reset list. Cleared as they
     fire, so a second run does not re-notify the first caller. */
  const QUEUE_DONE_HOOKS = [];

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
    markRefreshed();
  }

  /* Auto-poll while anything on the landing table is actually running --
     a fixed 2-minute cadence is cheap enough to leave on, but pointless (and
     one more thing to explain) once every run shown is finished. Cleared and
     restarted rather than left running forever, so leaving the tab open for
     days doesn't accumulate more than one interval. */
  let refreshTimer = null;
  const REFRESH_MS = 120000;

  function scheduleAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(() => {
      if (!RUNS.some((r) => r.status === "running")) return;
      loadRuns().catch(() => { /* next tick tries again */ });
    }, REFRESH_MS);
  }

  function markRefreshed() {
    const el = $("recent-live");
    if (el) el.textContent = "Updated " + new Date().toLocaleTimeString(undefined,
      { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
          const running = r.status === "running";
          const pct = r.to_run ? Math.round((r.processed / r.to_run) * 100) : (noWork ? 100 : 0);
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
            <td class="num">${stale ? "—" : fmt(pct) + "%"}</td>
            <td class="num">${stale ? "—" : fmt(r.processed)}${
              noWork ? `<div class="t-sub">${fmt(r.already_mapped)} already mapped</div>` : ""}</td>
            <td class="num" ${r.failed ? 'style="color:var(--crit)"' : ""}>${
              stale ? "—" : fmt(r.failed)}</td>
            <td class="num">${stale ? "—" : runDuration(r.started_at, r.ended_at)}</td>
            <td class="m">${esc(who(r.triggered_by))}</td>
            <td style="white-space:nowrap;display:flex;gap:6px">
              <button class="btn open rd-open" data-run="${esc(r.execution_id)}">View details</button>
              ${running
                ? `<button class="btn sm danger rr-cancel" data-run="${esc(r.execution_id)}">Cancel</button>`
                : ""}
            </td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="10" class="empty">No runs match these filters.</td></tr>`;

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

    $("rr-rows").querySelectorAll(".rr-cancel").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Cancel this run? It stops after the SKU currently in progress.")) return;
        b.disabled = true;
        b.textContent = "Cancelling…";
        try {
          await api(`/runs/${b.dataset.run}/cancel`, { method: "POST" });
          await loadRuns();
        } catch (e) {
          showError($("errors"), e.message);
          b.disabled = false;
          b.textContent = "Cancel";
        }
      }));
  }

  /* ------------------------------------------------------- never matched */

  /* Rows the engine has no answer for at all.
   *
   * Every other figure on this page reports what a run DID. This reports what
   * it did not, which is the harder thing to notice: a run can say "177 of
   * 177 processed" and still leave rows behind, because it selects its batch
   * once at the start and anything re-queued after that moment is simply not
   * in it. Four rows survived the rejected re-run exactly that way.
   */
  async function loadUnmatched() {
    const d = await api("/unmatched?engine=himalaya&limit=200");
    $("tile-unmatched").textContent = fmt(d.total);

    if (!d.total) {
      $("unmatched-note").hidden = true;
      return;
    }
    const byChannel = Object.entries(d.by_channel)
      .filter(([, n]) => n)
      .map(([ch, n]) => `${esc(ch)} ${fmt(n)}`)
      .join(", ");

    $("unmatched-note").hidden = false;
    $("unmatched-body").innerHTML =
      `<b>${fmt(d.total)} Himalaya listing${d.total === 1 ? " has" : "s have"}
         no match at all.</b> PENDING with zero mapping rows — ${byChannel}.
       A run reports the batch it selected, so a row re-queued after that
       moment is neither processed nor reported as skipped.
       <div class="tw tw-short" style="margin-top:10px;background:var(--surface)">
         <table>
           <thead><tr><th>Listing</th><th>Channel</th><th>Category</th><th>SKU</th></tr></thead>
           <tbody>${d.rows.slice(0, 10).map((r) => `<tr>
             <td class="t-title">${esc(r.title || "—")}</td>
             <td><span class="tag">${esc(r.channel)}</span></td>
             <td class="cell-cat">${esc(r.category || "—")}
               <div class="sc">${esc(r.subcategory || "")}</div></td>
             <td class="m">${esc(r.sku)}</td>
           </tr>`).join("")}</tbody>
         </table>
       </div>
       ${d.rows.length > 10
         ? `<p class="hint" style="margin:8px 0 0">Showing 10 of ${fmt(d.total)}.</p>` : ""}
       <div class="actions" style="margin-top:10px">
         <button class="btn sm" id="unmatched-copy">Copy SKU list</button>
         <span class="stale">Paste into New run → Scope by → SKU list to process them.</span>
       </div>`;

    $("unmatched-copy").addEventListener("click", () => {
      navigator.clipboard?.writeText(d.rows.map((r) => r.sku).join("\n"));
      $("unmatched-copy").textContent = `Copied ${fmt(d.rows.length)}`;
    });
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

  /* ------------------------------------------------------ batch launch
   * "Run entire channel": launches every category/sub-category with PENDING
   * rows for the current channel+engine, throttled server-side (see
   * routers/engine.py's _pump_batch) instead of the operator clicking Run
   * once per scope. A channel can have 20+ such scopes -- built after an
   * 18k-record backlog made "click Run, wait, click Run again" the real
   * bottleneck, not any per-run limit.
   *
   * Deliberately its own button and its own confirm dialog, not a third
   * "Scope by" mode alongside Category/SKU list: those two feed the single-
   * scope preview/confirm/launch path this page already had, and wedging a
   * many-scope action through the same $("go") button risked breaking that
   * path for a change nothing asked to touch. A batch's launched runs still
   * show up in the same Queue panel below, through the same attachRuns()/
   * syncQueue() every other run already goes through.
   */
  let ACTIVE_BATCH = null;   // {batch_id, pollTimer} while one is running
  let batchLaunchInFlight = false;
  const ENGINE_LABEL_BATCH = { himalaya: "Himalaya", competitor: "Competitor" };

  function updateBatchButton() {
    const field = $("batch-launch-field");
    const ch = $("ch").value;
    if (!ch || scopeMode() === "skus" || ACTIVE_BATCH) {
      field.hidden = true;
      return;
    }
    field.hidden = false;
    $("batch-launch-btn").disabled = batchLaunchInFlight;
  }

  async function openBatchConfirm() {
    const channel = $("ch").value;
    const engine = currentEngine();
    if (!channel) return;

    let preview;
    try {
      preview = await api(`/runs/batch/preview?channel=${encodeURIComponent(channel)}&engine=${encodeURIComponent(engine)}`);
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }
    if (!preview.scopes.length) {
      showError($("errors"), `Nothing is PENDING for ${ENGINE_LABEL_BATCH[engine]} on ${channel}.`);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Run every scope with PENDING SKUs`;
    $("confirm-lede").textContent =
      `Launches ${fmt(preview.scopes.length)} separate runs for ${esc(channel)}, `
      + `${fmt(3)} at a time, until all of them have been through the engine.`;
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>What will run</h3>
         <dl class="kv">
           <dt>Channel</dt><dd>${esc(channel)}</dd>
           <dt>Engine</dt><dd>${esc(ENGINE_LABEL_BATCH[engine])} only</dd>
           <dt>Scopes</dt><dd class="big">${fmt(preview.scopes.length)}</dd>
           <dt>Total SKUs</dt><dd class="big">${fmt(preview.total)}</dd>
           <dt>Running at once</dt><dd>3 scopes</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>How it will run</h3>
         <dl class="kv">
           <dt>LLM judge</dt><dd style="display:flex;align-items:center;gap:8px">
             <input type="checkbox" id="batch-llm" checked style="width:16px;height:16px;margin:0">
             <label for="batch-llm" style="margin:0;font-weight:400">Use the LLM judge for this batch</label></dd>
           <dt>Workers per scope</dt><dd>${esc($("w").value)}</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Largest scopes first</h3>
         <div class="m-cmd">${preview.scopes.slice(0, 10).map((s) =>
           `${esc(s.category)} / ${esc(s.subcategory)} — ${fmt(s.to_run)} SKUs`).join("\n")}${
           preview.scopes.length > 10 ? `\n… and ${fmt(preview.scopes.length - 10)} more scopes` : ""}</div>
       </div>
       <div class="m-sec">
         <label class="m-ack"><input type="checkbox" id="batch-ack">
           I understand this launches ${fmt(preview.scopes.length)} separate runs, a few at a time,
           against ${fmt(preview.total)} SKUs total.</label>
       </div>`;
    $("confirm-go").textContent = "Start batch";
    $("confirm-go").disabled = true;
    $("batch-ack").addEventListener("change", (e) => { $("confirm-go").disabled = !e.target.checked; });

    window.ConfirmDialog.open(async () => {
      const useLlm = $("batch-llm").checked;
      // Temporary: confirms what the browser actually sends, since the
      // server-side chain (BatchLaunchRequest -> spawn_one -> RunRequest ->
      // _build_command) tested correct in isolation for both true and
      // false -- if every launched scope still comes back use_llm=true
      // despite this logging useLlm===false, the bug is between the click
      // and this line, not below it.
      console.log("[batch launch] use_llm being sent:", useLlm);
      batchLaunchInFlight = true;
      updateBatchButton();
      try {
        const res = await api("/runs/batch", {
          method: "POST",
          body: JSON.stringify({
            channel, engine, concurrency: 3,
            workers: Number($("w").value), use_llm: useLlm,
          }),
        });
        startBatchTracking(res.batch_id, res.scope_count, res.total_to_run);
      } finally {
        batchLaunchInFlight = false;
        updateBatchButton();
      }
    });
  }

  function startBatchTracking(batchId, scopeCount, totalToRun) {
    if (ACTIVE_BATCH?.pollTimer) clearInterval(ACTIVE_BATCH.pollTimer);
    ACTIVE_BATCH = { batch_id: batchId, scopeCount, totalToRun };
    updateBatchButton();
    $("batch-banner").hidden = false;
    renderBatchBanner({ running: 0, queued: scopeCount, done: false });

    const poll = async () => {
      let status;
      try {
        status = await api(`/runs/batch/${batchId}`);
      } catch {
        return;   // transient fetch failure -- try again next tick
      }
      renderBatchBanner(status);
      // New scopes this batch has started since the last poll need their
      // own entries in QUEUE -- attachRuns() merges by run_id, so calling
      // it with the full launched list every tick is safe and idempotent
      // for ones already tracked, and picks up ones the pump just started.
      const known = new Set(QUEUE.map((q) => q.run_id));
      const fresh = status.launched.filter((id) => !known.has(id));
      if (fresh.length) {
        attachRuns(fresh.map((run_id) => ({
          run_id, engine: status.engine, log: null, channel: status.channel,
        })));
      }
      if (status.done && status.queued === 0) {
        clearInterval(ACTIVE_BATCH.pollTimer);
        ACTIVE_BATCH = null;
        updateBatchButton();
      }
    };
    poll();
    ACTIVE_BATCH.pollTimer = setInterval(poll, 3000);
  }

  function renderBatchBanner(status) {
    const el = $("batch-banner-text");
    if (status.done && status.queued === 0 && status.running === 0) {
      el.textContent = "Batch finished — every scope has been launched and completed.";
      $("batch-cancel-btn").hidden = true;
      return;
    }
    el.textContent = `Batch running: ${fmt(status.running ?? 0)} scope${
      (status.running ?? 0) === 1 ? "" : "s"} in progress, `
      + `${fmt(status.queued ?? 0)} still queued.`;
    $("batch-cancel-btn").hidden = (status.queued ?? 0) === 0;
  }

  $("batch-launch-btn").addEventListener("click", () => openBatchConfirm().catch((e) =>
    showError($("errors"), e.message)));
  $("batch-cancel-btn").addEventListener("click", async () => {
    if (!ACTIVE_BATCH) return;
    if (!confirm("Stop launching new scopes? Scopes already running keep going — cancel those individually if needed.")) return;
    try {
      await api(`/runs/batch/${ACTIVE_BATCH.batch_id}/cancel`, { method: "POST" });
    } catch (e) {
      showError($("errors"), e.message);
    }
  });

  /* Resolve is debounced because every control change triggers it and the
     query counts rows across a 151k-row table. */
  let resolveTimer = null;
  function scheduleResolve() {
    clearTimeout(resolveTimer);
    resolveTimer = setTimeout(resolve, 250);
  }

  // Cached for the session: the same figure until something actually
  // changes it (a run completes, a SKU is reset), so re-showing the empty
  // state while flipping channels back and forth doesn't refetch it. Feeds
  // only the Pending tile in the Run overview strip -- the per-channel
  // breakdown used to also render as a block of text under the empty state,
  // which duplicated the same total the tile already shows.
  let PENDING_SUMMARY = null;

  async function fetchPendingSummary() {
    try {
      if (!PENDING_SUMMARY) PENDING_SUMMARY = await api("/pending-summary");
    } catch { /* tile falls back to "—" */ }
  }

  async function resolve() {
    if (!$("ch").value) {
      PREVIEW = null;
      renderScopeCounts();
      // First paint before the pending-summary fetch resolves: the tile
      // shows "—" only for that one instant, same dash the rest of the page
      // already uses for "not known yet".
      if (!QUEUE.length) renderOverview();
      /* In SKU mode the missing channel is not a formality: a SKU is only
         unique within a channel's table, and the same code can exist on
         several. The message says which fact is blocking the resolve. */
      const headline = scopeMode() === "skus"
        ? `<p class="empty">Pick a channel before pasting SKUs.<br>
             <span class="hint">Each channel is a separate source table, so the same
               SKU can exist on more than one — the engine cannot tell which you
               mean without the channel.</span></p>`
        : `<p class="empty">Pick a channel to resolve a scope.<br>
             <span class="hint">A run covers one channel — the engine reads a single
               source table, so there is no all-channels mode.</span></p>`;
      $("preview-b").innerHTML = headline;
      $("preview-at").textContent = "";
      await fetchPendingSummary();
      // The channel picker can be filled in while that fetch was in flight --
      // re-rendering the tile under a scope now being resolved would show a
      // stale total next to real data.
      if (!$("ch").value && !QUEUE.length) renderOverview();
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

  /* Auto-suggested Workers, from the scope size alone.
   *
   * Two tiers, not four: a worker is one thread from a pool submitted the
   * WHOLE batch at once (see batch_flow.py's ThreadPoolExecutor), so above a
   * handful of SKUs there is always enough work to keep every thread busy --
   * "3 workers for 50 SKUs" saved nothing real, it was an unproven middle
   * tier. The only case with a genuine reason to scale down is a handful of
   * SKUs too few to meaningfully parallelise at all. Above that, go straight
   * to 4: deadlocks were observed at 6 workers on this table in a single run
   * (see /runs launch()'s docstring) -- a risk tied to concurrent writers,
   * not to how many SKUs are queued behind them, so a bigger batch is not
   * safer at a lower worker count and 4 is both the floor and the ceiling
   * that make sense once there's real work to spread across them.
   */
  function suggestedWorkers(toRun) {
    return toRun <= 5 ? 1 : 4;
  }

  // True once the operator has picked a Workers value themselves -- after
  // that, the scope resolving again (a new category, a bigger SKU paste)
  // must never silently overwrite a deliberate choice.
  let workersTouched = false;
  $("w").addEventListener("change", () => {
    workersTouched = true;
    $("w-auto").hidden = true;
  });

  function applyWorkerSuggestion(toRun) {
    if (workersTouched) return;
    const suggested = String(suggestedWorkers(toRun));
    if ($("w").value !== suggested) {
      $("w").value = suggested;
      // Setting .value programmatically does not fire "change" -- and must
      // not here, or this very handler would mark itself as user-touched.
    }
    $("w-auto").hidden = suggested === "4";
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

  /* Run overview: the summary strip above the scope details (Pending /
     Running / Completed / Failed tiles, the scope headline, the "Ready to
     run" badge). Reads PREVIEW (scope resolve) and QUEUE (live run progress)
     -- both already fetched for other reasons -- rather than requesting
     anything of its own, so this can never show a number the rest of the
     page disagrees with.
     Two distinct states: no run launched yet (PREVIEW drives Pending, the
     other three tiles sit at 0) and a queue actually running or finished
     (QUEUE drives all four, since a launched run's real Running/Completed/
     Failed figures matter more than the pre-run estimate). */
  /* Three states, not two:
       1. No channel picked, no run active -- the new landing view (Pending
          tile showing the cross-channel total, "Pick a channel" badge).
       2. A channel IS picked but nothing has been launched yet -- reverts
          entirely to the original page: no tiles, no badge, panel retitled
          back to "Run preview", renderPreview()'s leg card is what's on
          screen. Requested explicitly: the new overview strip was only ever
          meant for the empty state, not to replace the scope breakdown
          operators already knew how to read.
       3. A run has actually been launched (QUEUE non-empty) -- tiles and
          badge come back, now showing live progress, regardless of what the
          channel picker currently says (a reattached/synced run may not
          match it at all). */
  function renderOverview() {
    const head = $("run-ov-head");
    const tiles = $("run-ov-tiles");
    const badge = $("ready-badge");
    const title = $("preview-panel-title");
    const engine = currentEngine();
    const leg = legOf(engine);
    const queueActive = QUEUE.length > 0;

    if (!leg && !queueActive) {
      title.textContent = "Run overview";
      tiles.hidden = false;
      badge.hidden = false;
      head.hidden = true;
      // Same figure the empty-state summary below already shows in words --
      // the tile echoes it in numeral form rather than sitting on a dash
      // while real data is one panel down. Only ever a cache hit here:
      // fetchPendingSummary() (called from resolve()'s no-channel branch)
      // already fetched and cached PENDING_SUMMARY by the time a render
      // reaches this branch on any normal path.
      $("ov-pending").textContent = PENDING_SUMMARY ? fmt(PENDING_SUMMARY.total) : "—";
      $("ov-running").textContent = "0";
      $("ov-completed").textContent = "0";
      $("ov-failed").textContent = "0";
      badge.className = "pill-status neutral";
      badge.innerHTML = '<i class="sq"></i>Pick a channel';
      return;
    }

    if (!queueActive) {
      // A channel is picked, nothing launched yet -- old view, full stop.
      title.textContent = "Run preview";
      tiles.hidden = true;
      head.hidden = true;
      badge.hidden = true;
      return;
    }

    title.textContent = "Run overview";
    tiles.hidden = false;
    badge.hidden = false;
    head.hidden = false;
    const chLabel = $("ch").value || "—";
    const catLabel = scopeMode() === "skus"
      ? "SKU list"
      : `${$("cat").value || "All categories"} · ${$("sub").value || "All sub-categories"}`;
    $("run-ov-title").textContent =
      `${engine === "himalaya" ? "Himalaya" : "Competitor"} · ${chLabel === "—" ? "" : chLabel}`.trim();
    $("run-ov-sub").textContent = catLabel;
    $("ov-llm-pill").textContent = `LLM Judge ${$("llm").checked ? "ON" : "OFF"}`;
    $("ov-llm-pill").className = "pill-status" + ($("llm").checked ? "" : " neutral");

    const processed = QUEUE.reduce((a, q) => a + q.processed, 0);
    const failed = QUEUE.reduce((a, q) => a + q.failed, 0);
    const total = QUEUE.reduce((a, q) => a + q.to_run, 0);
    const running = QUEUE.filter((q) => q.state === "running" || q.state === "starting").length;
    const live = running > 0;
    $("ov-pending-pill").textContent = live ? "Running" : "Finished";
    $("ov-pending").textContent = fmt(Math.max(0, total - processed - running));
    $("ov-running").textContent = fmt(running);
    $("ov-completed").textContent = fmt(processed - failed);
    $("ov-failed").textContent = fmt(failed);
    badge.className = "pill-status" + (live ? "" : failed ? " neutral" : "");
    badge.innerHTML = live
      ? '<i class="sq"></i>Running'
      : failed ? '<i class="sq"></i>Finished with failures' : '<i class="sq"></i>Completed';
  }

  function renderPreview() {
    const engine = currentEngine();
    const leg = legOf(engine);
    if (!leg) { renderOverview(); return; }

    applyWorkerSuggestion(leg.to_run);

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
           <button class="btn primary" id="go" ${total ? "" : "disabled"}
             data-label="${esc(total ? `Run ${fmt(total)} SKU${total === 1 ? "" : "s"}` : "Nothing to run")}">
             ${total ? `Run ${fmt(total)} SKU${total === 1 ? "" : "s"}` : "Nothing to run"}</button>
           ${resettable ? `<label class="stale" style="display:flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" id="reset-failed"> Reset &amp; include ${fmt(resettable)} failed</label>` : ""}
         </div>`;

    $("preview-at").textContent = "Resolved " + new Date().toLocaleTimeString();
    $("go").addEventListener("click", openConfirm);
    disableRunIfQueued(engine);
    renderOverview();
  }

  /* Backend already refuses a second launch of the same channel+engine+
     category via the 409 in /runs' launch() -- but that only surfaces after
     Confirm, one click and a round trip after the operator committed to it.
     A run that just finished leaves its scope's PENDING count unchanged
     until the next resolve (nothing here re-fetches it automatically), so
     the "Run N SKUs" button stayed fully clickable, inviting exactly that
     click on a scope already running or just run. Disabling it up front,
     the moment this page already knows a matching run is queued, turns an
     error the operator has to hit into a state they can just see. */
  function disableRunIfQueued(engine) {
    const go = $("go");
    if (!go) return;
    // Only ever overrides the "nothing to run" disable, never clears it --
    // that one is a real fact about the scope (to_run === 0), this one is
    // about a run in progress, and if the button carries neither label it
    // must be because there genuinely is nothing to run.
    if (go.disabled && go.textContent === "Nothing to run") return;
    const ch = $("ch").value;
    const cat = scopeMode() === "skus" ? null : ($("cat").value || null);
    const match = QUEUE.find((q) =>
      q.engine === engine && (q.channel ?? ch) === ch && (q.category ?? cat) === cat
      && (q.state === "running" || q.state === "starting"));
    if (match) {
      go.disabled = true;
      go.textContent = "Already running this scope";
    } else if (go.textContent === "Already running this scope") {
      // The matching run just finished -- restore the button to what
      // renderPreview() last computed for it, so it goes back to whatever
      // SKU count is actually still true rather than staying stuck.
      go.disabled = false;
      go.textContent = go.dataset.label || go.textContent;
    }
  }

  /* ------------------------------------------------------------ confirm */

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

    /* Title and lede are rewritten every time: three callers share this
       dialog and each leaves its own copy behind. */
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
    window.ConfirmDialog.open(launch);
  }

  /* Errors and dialog lifecycle belong to the confirm handler that invokes
     this, so a failure here propagates rather than being swallowed. */
  async function launch() {
    const res = await api("/runs", { method: "POST", body: JSON.stringify(runBody()) });
    clearError($("errors"));
    // Stamped here, from the composer fields at the exact moment of launch --
    // not left to fall back to "whatever the composer currently shows" at
    // render time (renderQueue()'s q.channel ?? $("ch").value), which reads
    // as correct for a single in-flight run but mislabels every earlier
    // queued run the moment the operator changes the channel/category picker
    // to look at something else. The API response carries only run_id/
    // engine/log, so this is the one place that knows the true scope.
    const channel = $("ch").value;
    const category = scopeMode() === "skus" ? null : ($("cat").value || null);
    startQueue(res.runs.map((r) => ({ ...r, channel, category })));
  }

  /* ------------------------------------------------------------ queue */

  function startQueue(runs) {
    // Merges into whatever QUEUE already holds -- this used to reset to
    // QUEUE = [] on every launch, which silently dropped any run already
    // being tracked from earlier in the same page load (launch zepto, watch
    // it, launch swiggy: zepto's entry vanished from QUEUE even though it
    // was still running on the server). That fed disableRunIfQueued() a
    // queue that no longer knew about zepto, so switching the scope picker
    // back to it showed "Run" as clickable again -- looked like the guard
    // needed a page refresh to "pick up" a run that a refresh would only
    // ever be rediscovering through syncQueue(), not something teardown
    // here should have thrown away in the first place. attachRuns() already
    // merges by construction (see syncQueue()); startQueue is now just its
    // entry point for a fresh launch rather than a separate reset path.
    attachRuns(runs);
  }

  /* Adds runs to the queue WITHOUT touching what's already there -- the
     piece startQueue's old all-in-one version couldn't do, because it always
     opened with teardownStreams() and a fresh QUEUE = [...]. Called both for
     a brand new launch (from an empty queue) and by syncQueue() to fold in a
     run that started elsewhere while this page stayed open; either way, a
     stream already open for an existing entry is left running, not closed
     and reopened. */
  function attachRuns(runs) {
    /* One engine per launch now that "Both" is gone, but this still reads a
       list: /runs returns an array, and a tracker that assumed a single
       element would break silently the day it returns two. */
    const added = runs.map((r) => ({
      run_id: r.run_id,
      engine: r.engine,
      log: r.log,
      // A run just launched from this page has no channel/category of its
      // own yet -- it IS whatever the composer says, so falling back to the
      // form fields is correct there. A run folded in by syncQueue is not:
      // the composer may be empty or pointed at something else entirely, and
      // showing its fields would label someone else's run with the wrong
      // scope. Passing channel/category explicitly for that case is what
      // render reads first, below.
      channel: r.channel ?? null,
      category: r.category ?? null,
      processed: 0, failed: 0, to_run: 0,
      state: "running",
    }));
    QUEUE = QUEUE.concat(added);

    $("queue-panel").hidden = false;
    $("cancelBtn").disabled = false;
    $("cancelNote").textContent = "Cancellation takes effect after the current SKU.";
    renderQueue();

    const refreshBtn = $("queue-refresh");
    refreshBtn.onclick = () => {
      refreshBtn.disabled = true;
      Promise.all([refreshQueue().catch(() => {}), syncQueue().catch(() => {})])
        .finally(() => { refreshBtn.disabled = false; });
    };

    added.forEach((q) => {
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

  /* Manual refresh: re-reads each queued run's row directly rather than
     waiting on the SSE stream. The stream already pushes updates as they
     happen, but a dropped connection (proxy timeout, laptop sleep) leaves the
     panel showing its last received tick with nothing on screen to say so --
     this is the operator's way to force a fresh read without reloading the
     whole page and losing the queue tracker. */
  async function refreshQueue() {
    await Promise.all(QUEUE.map(async (q) => {
      try {
        const d = await api(`/runs/${q.run_id}`);
        Object.assign(q, {
          processed: d.run.processed ?? q.processed,
          failed: d.run.failed ?? q.failed,
          to_run: d.run.to_run ?? q.to_run,
          state: d.run.status || q.state,
        });
      } catch { /* leave this row's last-known state on a failed refresh */ }
    }));
    renderQueue();
    if (QUEUE.every((x) => x.state !== "running" && x.state !== "starting")) {
      onQueueFinished();
    }
  }

  function renderQueue() {
    $("queue-rows").innerHTML = QUEUE.map((q, i) => {
      const pct = q.to_run ? Math.round((q.processed / q.to_run) * 100) : 0;
      const colour = q.failed ? "var(--warn)" : "var(--ok)";
      return `<div class="q-row">
        <span class="ord">${i + 1}</span>
        ${enginePill(q.engine)}
        <span class="nm">${esc(q.channel ?? $("ch").value)} · ${esc(
          (q.channel != null ? q.category : $("cat").value) || "all categories")}
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

    /* "In progress" is not a per-SKU count the stream gives us -- each run
       processes one SKU at a time, so the honest figure is how many of the
       QUEUED RUNS are currently live, not a count of SKUs mid-flight within
       them. One run live is 1; two concurrent runs (a second launched or
       attached while this page was open, see syncQueue()) is 2. */
    const inProgress = QUEUE.filter((q) => q.state === "running" || q.state === "starting").length;
    const remaining = Math.max(0, total - processed - inProgress);
    $("progLegend").innerHTML =
      `<span><i class="sq" style="background:var(--ok)"></i>${fmt(processed - failed)} processed</span>
       <span><i class="sq" style="background:var(--accent)"></i>${fmt(inProgress)} in progress</span>
       <span><i class="sq" style="background:var(--crit)"></i>${fmt(failed)} failed</span>
       <span><i class="sq" style="background:var(--sunken);border:1px solid var(--line)"></i>${
         fmt(remaining)} queued</span>`;

    $("liveTag").className = live ? "live" : "live stopped";
    $("liveTag").innerHTML = live ? "<i></i>Streaming" : "<i></i>Stopped";

    renderStages(live, processed, total);
    renderOverview();
    if ($("go")) disableRunIfQueued(currentEngine());
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

    /* Drained rather than iterated in place: a hook that fails must not stop
       the others, and none may fire twice on a later run. */
    while (QUEUE_DONE_HOOKS.length) {
      const hook = QUEUE_DONE_HOOKS.shift();
      try { await hook(QUEUE); } catch { /* the hook owns its own reporting */ }
    }
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
    window.ConfirmDialog.open(async () => {
      await api("/rejected", {
        method: "POST",
        body: JSON.stringify({
          channel: $("ch").value, sku,
          comment: $("rej-comment").value.trim() || null,
        }),
      });
      await loadResults();
      /* The Rejected page owns the backlog; tell it to refresh if it has
         already been opened, rather than reaching into its state from here. */
      window.Pages?.rejected?.refresh?.();
    });
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
    RES.offset = 0;
    loadResults().catch(() => {});
  };

  $("ch").addEventListener("change", () => { fillCategories(); scheduleResolve(); rescope(); updateBatchButton(); });
  $("cat").addEventListener("change", () => { fillSubcategories(); scheduleResolve(); rescope(); });
  $("sub").addEventListener("change", () => { scheduleResolve(); rescope(); });
  $("skus").addEventListener("input", scheduleResolve);
  document.querySelectorAll('input[name="sb"]').forEach((r) =>
    r.addEventListener("change", () => { applyScopeMode(); updateBatchButton(); }));
  $("llm").addEventListener("change", () => {
    if (PREVIEW) renderPreview(); else renderOverview();
  });
  document.querySelectorAll('input[name="t"]').forEach((r) =>
    r.addEventListener("change", () => {
      updateBatchButton();
      if (PREVIEW) renderPreview();
      /* Results has its own engine filter, independent of the composer's --
         left alone, picking Competitor to run still showed Himalaya rows
         mixed into Results because that dropdown silently stayed on "All
         engines". Syncing it here means Results defaults to showing what you
         just chose to run, not a filter nothing told it to change. */
      $("resEngine").value = currentEngine();
      RES.offset = 0;
      loadResults().catch(() => {});
    }));

  /* Backdrop click, Escape and the confirm button are all wired in
     confirm.js -- three pages open that dialog now. */

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
    }));

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
      await loadUnmatched().catch(() => {});
      $("rr-refresh").addEventListener("click", () => {
        $("rr-refresh").disabled = true;
        loadRuns().catch((e) => showError($("errors"), e.message))
          .finally(() => { $("rr-refresh").disabled = false; });
      });
      scheduleAutoRefresh();
    },
    refresh() {
      return Promise.all([
        loadRuns().catch(() => {}),
        loadUnmatched().catch(() => {}),
      ]);
    },
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
      // Same sync as the "t" radio's change handler -- covers arriving on
      // this page with Competitor (or a restored scope) already selected,
      // so the first Results load doesn't show the unfiltered mix.
      $("resEngine").value = currentEngine();
      // use_llm is read once, at launch (runBody()/launchSkus) -- flipping it
      // mid-run cannot reach a process that already started, so the switch is
      // disabled while this page's queue is live rather than left clickable
      // and silently doing nothing.
      bindLlmToggle("llm", { isRunning: () => QUEUE.some(
        (q) => q.state === "running" || q.state === "starting") });
      updateBatchButton();
      await syncQueue().catch(() => {});
      scheduleQueueSync();
      await resolve();
      await loadResults().catch(() => {});
    },
  };

  /* Attaches the Queue panel to every run still actually executing on the
     server -- not just the ones launched from this page load.
     Two distinct gaps this closes:
       1. A page load starting with an empty, in-memory QUEUE (a refresh, a
          fresh tab, navigating back here) had no way back to a run still in
          progress -- nothing before this persisted the queue anywhere but
          this one page's JS state.
       2. Launching ONE run from this page, then starting a SECOND one
          elsewhere (another tab, another operator) while this page stayed
          open: the old reattachQueue() only ever ran once, at boot, and
          bailed immediately if QUEUE already had anything in it -- so a
          run that started later was invisible here even though the server
          was actively running it, right up until a full page reload.
     Merges by run_id rather than replacing QUEUE outright, so a run this
     page already has an open SSE stream for is left alone -- only genuinely
     new rows get a stream opened for them. */
  async function syncQueue() {
    const runs = await api("/runs?limit=20");
    const live = runs.filter((r) => r.status === "running");
    const known = new Set(QUEUE.map((q) => q.run_id));
    const missing = live.filter((r) => !known.has(r.execution_id));
    if (!missing.length) return;
    attachRuns(missing.map((r) => ({
      run_id: r.execution_id, engine: r.engine, log: null,
      channel: r.channel, category: r.category,
    })));
  }

  // Catches a run started elsewhere (another tab, another operator) while
  // this page stays open -- the SSE streams this page already has open
  // cover progress on runs it already knows about, but nothing pushes word
  // of a run it doesn't know exists yet. 30s: cheap enough to leave running
  // for as long as the page is open (one small GET, compared against the
  // in-memory QUEUE), tight enough that a second run doesn't sit invisible
  // for minutes after someone else starts it.
  let queueSyncTimer = null;
  function scheduleQueueSync() {
    if (queueSyncTimer) clearInterval(queueSyncTimer);
    queueSyncTimer = setInterval(() => { syncQueue().catch(() => {}); }, 30000);
  }

  /* Launching a SKU-scoped run from another page.
   *
   * Exposed rather than reimplemented: the Rejected page needs to run exactly
   * the SKUs it just reset, and a second launch path would eventually differ
   * from this one in some detail -- which worker count it sends, whether it
   * opens a stream -- and the difference would only show up in production.
   * The caller gets the same queue tracker and the same confirmation.
   */
  window.RunLauncher = {
    /* use_llm defaults to whatever the New run page's toggle is currently
       showing, but a caller confirming its OWN "how it will run" dialog
       (Review, Rejected) must pass the value that dialog actually displayed
       -- reading $("llm") here unconditionally meant a launch could silently
       use a different LLM setting than the one the operator just confirmed,
       because that checkbox lives on a hidden page and nothing kept the two
       in sync. */
    async launchSkus({ channel, skus, engine = null, onDone = null, use_llm = null }) {
      /* One engine per launch, never both.
         engine:null asks the API for both, which spawns two subprocesses --
         and on a SKU-scoped run one of them finds nothing every time. Three
         such runs put 272 unasked-for competitor SKUs through the LLM. A
         caller with rows from both sides launches twice, naming each. */
      if (!engine) {
        throw new Error(
          "launchSkus needs an engine ('himalaya' or 'competitor'). "
          + "Launch once per engine rather than asking for both."
        );
      }
      const body = {
        channel, engine, category: null, subcategory: null, skus,
        use_llm: use_llm === null ? $("llm").checked : use_llm,
        workers: Number($("w").value),
        batch_size: Number($("bs").value) || 500,
        reset_failed: false,
      };
      const res = await api("/runs", { method: "POST", body: JSON.stringify(body) });

      /* Shown on New run, because that is where the queue tracker lives --
         sending the operator to a page with no progress on it would be
         worse than moving them. */
      $("ch").value = channel;
      fillCategories();
      window.Shell?.go?.("runs-new");
      startQueue(res.runs);
      if (onDone) QUEUE_DONE_HOOKS.push(onDone);
      return res;
    },
    async plan(body) {
      return api("/plan", { method: "POST", body: JSON.stringify(body) });
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
