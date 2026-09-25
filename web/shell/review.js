/* Review status: validate an engine change against real data, by channel.
 *
 * Two jobs, both scoped to whichever channel and engine are selected at the
 * top of the page -- nothing here ever mixes channels or runs both engines
 * in one action:
 *
 *   1. MAIN ACTION -- "Reset & re-run all Non-Approved <engine> SKUs".
 *      For validating a finished engine change against the whole backlog:
 *      resets every rejected + never-reviewed row for that engine on that
 *      channel (approved rows are never touched) and runs the current
 *      engine code over all of them. No sku list needed -- the server
 *      selects by scope=non_approved + engine, see common/rejection.py.
 *
 *   2. VALIDATION -- paste a handful of SKUs, reset + run just those.
 *      For trying a change on a few known rows before committing to the
 *      full backlog above.
 *
 * A read-only grid below both ("All records") is for looking a SKU up, with
 * a per-row reset+run for one-off spot checks -- not the primary flow.
 *
 * All three still end in the same place: reset_rejected(scope=non_approved)
 * on the backend, then window.RunLauncher.launchSkus, one engine leg at a
 * time -- same contract Rejected matches uses, so a run launched from here
 * behaves identically to one launched from there.
 */
(function () {
"use strict";

  const { api, esc, fmt, pager, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let STATE = { offset: 0, limit: 25 };
  let CHANNELS = [];
  let LAST_ROWS = [];      // the grid page on screen, for per-row actions

  const ENGINE_LABEL = { himalaya: "Himalaya", competitor: "Competitor" };

  /* --------------------------------------------------------------- load */

  async function fillChannels() {
    if (!$("rv-channel").dataset.filled) {
      const chans = await api("/channels");
      CHANNELS = Object.keys(chans);
      $("rv-channel").innerHTML =
        CHANNELS.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
      $("rv-channel").dataset.filled = "1";
    }
  }

  function currentScope() {
    return { channel: $("rv-channel").value, engine: $("rv-engine").value };
  }

  /* Every label on the page that names "what this button will act on" is
     driven from here, so the channel/engine pickers at the top are the only
     place scope is chosen -- nothing below can silently drift from them. */
  function renderScopeLabels() {
    const { channel, engine } = currentScope();
    $("rv-scope-channel").textContent = channel || "—";
    $("rv-manual-channel-label").textContent = channel || "—";
    $("rv-full-channel-label").textContent = channel || "—";

    const engineText = engine ? ENGINE_LABEL[engine] : "Himalaya + Competitor";
    $("rv-full-engine-label").textContent = engineText;
    $("rv-full-engine-label2").textContent = engineText;

    /* The full-backlog action needs exactly one engine -- "both" has no
       single audit entry_key/by_engine split that means "run both at once"
       the way a manual SKU list can. Disabled with an explanatory note
       rather than silently doing something wider than the label says. */
    const canRunFull = engine === "himalaya" || engine === "competitor";
    $("rv-full-reset").disabled = !canRunFull;
    $("rv-full-note").textContent = canRunFull
      ? "" : "Pick Himalaya or Competitor above to enable this — not both at once.";
  }

  async function load() {
    const { channel, engine } = currentScope();
    renderScopeLabels();
    if (!channel) return;

    const params = new URLSearchParams({
      channel, limit: String(STATE.limit), offset: String(STATE.offset),
      status: $("rvStatus").value || "all",
    });
    if (engine) params.set("engine", engine);
    if ($("rvSearch").value.trim()) params.set("q", $("rvSearch").value.trim());

    const countParams = new URLSearchParams({ channel });
    if (engine) countParams.set("engine", engine);

    const [counts, d] = await Promise.all([
      api("/review/counts?" + countParams),
      api("/review?" + params),
    ]);

    renderTiles(counts);
    renderRows(d, channel);
  }

  function renderTiles(c) {
    $("rv-t-approved").textContent = fmt(c.approved);
    $("rv-t-rejected").textContent = fmt(c.rejected);
    $("rv-t-pending").textContent = fmt(c.non_approved);
    $("rv-t-total").textContent = fmt(c.total);
  }

  const STATUS_LABEL = {
    Approved: { text: "Approved", cls: "" },
    Rejected: { text: "Rejected", cls: "crit" },
  };
  function statusPill(reviewStatus) {
    const known = STATUS_LABEL[reviewStatus];
    if (known) return `<span class="${known.cls}">${esc(known.text)}</span>`;
    return `<span class="stale">Pending</span>`;
  }

  function renderRows(d, channel) {
    LAST_ROWS = d.rows;
    $("rvCap").textContent = d.total ? `${fmt(d.total)} on ${channel}` : "";
    $("rvBody").innerHTML = d.rows.length
      ? d.rows.map((r) => `<tr>
          <td class="t-title">${esc(r.title || r.sku)}
            <div class="t-sub m">${esc(r.brand || "")} · ${esc(r.sku)}</div></td>
          <td>${statusPill(r.review_status)}
            ${r.mapping_status ? `<div class="t-sub m">${esc(r.mapping_status)}</div>` : ""}</td>
          <td class="t-title">${esc(r.matched_name || "—")}
            <div class="t-sub m">${esc(r.matched_code || "")}</div></td>
          <td class="num">${r.score == null ? "—" : r.score.toFixed(2)}</td>
          <td style="white-space:nowrap">
            <button class="btn sm danger rv-one" data-sku="${esc(r.sku)}"
              ${r.review_status === "Approved" ? "disabled" : ""}
              title="Delete this SKU's mappings, set it PENDING, and run the engine on it">Reset &amp; run</button>
          </td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">No records match this filter on ${esc(channel)}.</td></tr>`;

    $("rvPager").innerHTML = "";
    $("rvPager").appendChild(pager({
      total: d.total, offset: d.offset, limit: d.limit,
      onGo: (offset, limit) => {
        STATE = { offset, limit };
        load().catch((e) => showError($("errors"), e.message));
      },
    }));

    $("rvBody").querySelectorAll(".rv-one").forEach((b) =>
      b.addEventListener("click", () => resetOne(b.dataset.sku, channel)));
  }

  /* --------------------------------------------- main action: full backlog
   * No sku list is sent -- the server selects scope=non_approved+engine
   * itself, so this scales to however large the backlog is without the
   * client ever holding the row list in memory.
   */
  async function openFullResetConfirm() {
    const { channel, engine } = currentScope();
    if (!channel || !engine) return;

    let preview;
    try {
      preview = await api("/review/reset/preview", {
        method: "POST", body: JSON.stringify({ channel, engine }),
      });
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }
    if (!preview.rows_found) {
      showError($("errors"), `Nothing is Non-Approved for ${ENGINE_LABEL[engine]} on ${channel}.`);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Reset and re-run the full Non-Approved backlog`;
    $("confirm-lede").textContent =
      `This deletes mapping rows for every Non-Approved ${ENGINE_LABEL[engine]} SKU on `
      + `${channel}. It cannot be undone.`;
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>What changes</h3>
         <dl class="kv">
           <dt>Channel</dt><dd>${esc(channel)}</dd>
           <dt>Engine</dt><dd>${esc(ENGINE_LABEL[engine])} only</dd>
           <dt>SKUs affected</dt><dd class="big">${fmt(preview.rows_found)}</dd>
           <dt>Mapping rows</dt><dd class="crit">deleted</dd>
           <dt>mapping_status</dt><dd>&rarr; PENDING</dd>
           <dt>review_status</dt><dd>&rarr; cleared</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Afterwards</h3>
         <ul class="m-rules">
           <li>All ${fmt(preview.rows_found)} SKUs become ordinary PENDING rows, then run
             through the current ${esc(ENGINE_LABEL[engine])} engine code immediately.</li>
           <li>Approved rows on ${esc(channel)} are never touched.</li>
           <li>The ${esc(engine === "himalaya" ? "Competitor" : "Himalaya")} side of this
             channel is not touched — switch the engine dropdown to run it separately.</li>
         </ul>
       </div>
       <div class="m-sec">
         <h3>Sample of what is affected</h3>
         <div class="m-cmd">${preview.skus.slice(0, 8).map(esc).join("\n")}${
           preview.skus.length > 8 ? `\n… and ${fmt(preview.skus.length - 8)} more` : ""}</div>
       </div>
       <div class="m-sec">
         <h3>How it will run</h3>
         <dl class="kv">
           <dt>LLM judge</dt><dd class="${$("llm").checked ? "" : "warn"}">${
             $("llm").checked ? "on" : "OFF — scoring only"}</dd>
           <dt>Workers</dt><dd>${esc($("w").value)}</dd>
         </dl>
         <p class="hint" style="margin-top:8px">Taken from the New run page. Change them
           there if this validation run needs different settings.</p>
       </div>
       <div class="m-sec">
         <label class="m-ack"><input type="checkbox" id="rv-full-ack">
           I understand ${fmt(preview.rows_found)} SKU${
             preview.rows_found === 1 ? "" : "s"} will be reset and cannot be restored.</label>
       </div>`;
    $("confirm-go").textContent = "Reset and re-run all";
    $("confirm-go").disabled = true;
    $("rv-full-ack").addEventListener("change", (e) => {
      $("confirm-go").disabled = !e.target.checked;
    });

    window.ConfirmDialog.open(async () => {
      const result = await api("/review/reset", {
        method: "POST", body: JSON.stringify({ channel, engine }),
      });
      const skus = result.by_engine?.[engine] || result.skus || [];
      if (!skus.length) return;
      $("rv-full-note").textContent = `Running ${fmt(skus.length)} SKUs…`;
      await window.RunLauncher.launchSkus({
        channel, skus, engine,
        onDone: async () => {
          $("rv-full-note").textContent = "";
          await load().catch(() => {});
        },
      });
    });
  }

  /* One row, both steps, one click -- same shortcut Rejected matches gives
     a deliberately-picked single SKU. */
  async function resetOne(sku, channel) {
    const row = (LAST_ROWS || []).find((r) => r.sku === sku);
    if (row?.review_status === "Approved") {
      showError($("errors"), `${sku} is steward-approved and can't be reset here.`);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Reset and run this one SKU`;
    $("confirm-lede").textContent =
      "Deletes this SKU's mapping rows and runs the engine on it immediately. "
      + "The delete cannot be undone.";
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>The SKU</h3>
         <dl class="kv">
           <dt>Listing</dt><dd>${esc(row?.title || sku)}</dd>
           <dt>SKU</dt><dd>${esc(sku)}</dd>
           <dt>Channel</dt><dd>${esc(channel)}</dd>
           <dt>Current status</dt><dd>${esc(row?.review_status || "Pending")}</dd>
           <dt>Current match</dt><dd>${esc(row?.matched_name || "—")}
             ${row?.matched_code ? `(${esc(row.matched_code)})` : ""}</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>What happens</h3>
         <ul class="m-rules">
           <li>Every mapping row for this SKU is deleted.</li>
           <li>mapping_status goes back to PENDING and any review verdict is cleared.</li>
           <li>The engine runs on this SKU alone, and writes a fresh match.</li>
           <li>Nothing else on ${esc(channel)} is touched.</li>
         </ul>
       </div>
       <div class="m-sec">
         <label class="m-ack"><input type="checkbox" id="rv-one-ack">
           I understand this SKU's existing mappings will be deleted.</label>
       </div>`;
    $("confirm-go").textContent = "Reset and run";
    $("confirm-go").disabled = true;
    $("rv-one-ack").addEventListener("change", (e) => {
      $("confirm-go").disabled = !e.target.checked;
    });

    window.ConfirmDialog.open(async () => {
      const res = await api("/review/reset", {
        method: "POST",
        body: JSON.stringify({ channel, skus: [sku] }),
      });
      if (!res.rows_reset) throw new Error(`${sku} was not reset — is it steward-approved?`);
      if (!row?.engine) {
        throw new Error(`Cannot tell which engine owns ${sku} — reload the page.`);
      }
      await window.RunLauncher.launchSkus({
        channel, skus: [sku], engine: row.engine,
        onDone: async () => { await load().catch(() => {}); },
      });
    });
  }

  /* ---------------------------------------- validation: specific SKU list */

  function parseSkus(text) {
    return Array.from(new Set(
      text.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean)
    ));
  }

  $("rv-manual-skus").addEventListener("input", () => {
    const n = parseSkus($("rv-manual-skus").value).length;
    $("rv-manual-note").textContent = n
      ? `${fmt(n)} SKU${n === 1 ? "" : "s"} entered.` : "";
  });

  async function runManual() {
    const { channel } = currentScope();
    if (!channel) {
      showError($("errors"), "Pick a channel first — SKUs are reset per channel.");
      return;
    }
    const skus = parseSkus($("rv-manual-skus").value);
    if (!skus.length) {
      showError($("errors"), "Enter at least one SKU.");
      return;
    }

    let preview;
    try {
      preview = await api("/review/reset/preview", {
        method: "POST", body: JSON.stringify({ channel, skus }),
      });
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }
    if (!preview.rows_found) {
      showError($("errors"), "None of the entered SKUs can be reset — check they exist on "
        + `${channel} and are not steward-approved.`);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Reset and run ${fmt(skus.length)} entered SKU${skus.length === 1 ? "" : "s"}`;
    $("confirm-lede").textContent =
      "Deletes mapping rows for the entered SKUs and runs the engine on them. "
      + "The delete cannot be undone.";
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>What changes</h3>
         <dl class="kv">
           <dt>Channel</dt><dd>${esc(channel)}</dd>
           <dt>Entered</dt><dd class="big">${fmt(skus.length)}</dd>
           <dt>Resettable</dt><dd>${fmt(preview.rows_found)}
             ${preview.rows_found < skus.length
               ? " (approved or unknown SKUs skipped)" : ""}</dd>
           <dt>Mapping rows</dt><dd class="crit">deleted</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>SKUs that will be reset</h3>
         <div class="m-cmd">${preview.skus.slice(0, 15).map(esc).join("\n")}${
           preview.skus.length > 15 ? `\n… and ${fmt(preview.skus.length - 15)} more` : ""}</div>
       </div>
       <div class="m-sec">
         <label class="m-ack"><input type="checkbox" id="rv-manual-ack">
           I understand these SKUs' existing mappings will be deleted and cannot be restored.</label>
       </div>`;
    $("confirm-go").textContent = "Reset and run";
    $("confirm-go").disabled = true;
    $("rv-manual-ack").addEventListener("change", (e) => {
      $("confirm-go").disabled = !e.target.checked;
    });

    window.ConfirmDialog.open(async () => {
      const result = await api("/review/reset", {
        method: "POST", body: JSON.stringify({ channel, skus }),
      });
      /* A manually-typed list can span both engines (nothing stops someone
         pasting a mix), so -- unlike the full-backlog action -- this
         launches one leg per engine that actually came back non-empty. */
      const legs = Object.entries(result.by_engine || {}).filter(([, l]) => l.length);
      for (const [engine, list] of legs) {
        await window.RunLauncher.launchSkus({
          channel, skus: list, engine,
          onDone: async () => { await load().catch(() => {}); },
        });
      }
      $("rv-manual-skus").value = "";
      $("rv-manual-note").textContent = "";
    });
  }

  $("rv-manual-run").addEventListener("click", () => runManual());
  $("rv-full-reset").addEventListener("click", () => openFullResetConfirm());

  /* ------------------------------------------------------------ wiring */

  $("rv-channel").addEventListener("change", () => {
    STATE.offset = 0;
    load().catch((e) => showError($("errors"), e.message));
  });
  $("rv-engine").addEventListener("change", () => {
    STATE.offset = 0;
    load().catch((e) => showError($("errors"), e.message));
  });
  $("rvStatus").addEventListener("change", () => {
    STATE.offset = 0;
    load().catch((e) => showError($("errors"), e.message));
  });

  let qTimer = null;
  $("rvSearch").addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      STATE.offset = 0;
      load().catch((e) => showError($("errors"), e.message));
    }, 300);
  });

  window.Pages = window.Pages || {};
  window.Pages["review"] = {
    async boot() {
      await fillChannels();
      await load();
    },
    async refresh() {
      if (!$("rv-channel").dataset.filled) return;
      try { await load(); } catch { /* reports on next open */ }
    },
  };
})();
