/* Rejected matches: the standing review backlog.
 *
 * Its own page rather than a tab inside New run. A rejection outlives the run
 * that produced it -- it is a queue someone works through, not an artifact of
 * whatever scope you happen to be composing -- and buried behind a channel
 * picker three panels down, nobody found it.
 *
 * The page leads with the offenders callout rather than the list, because the
 * finding is not "here are twelve rejections", it is "one master product
 * collected four of them". A code that keeps being rejected is almost always
 * a gate target too small to offer an alternative, so everything routed there
 * comes back as the same product.
 */
(function () {
"use strict";

  const { api, esc, fmt, pager, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let STATE = { offset: 0, limit: 25 };
  let CHANNELS = [];

  const who = (email) => (email || "unknown").split("@")[0];

  /* Rebuilt after any write, not just on first load: the option labels carry
     per-channel counts, and a reset that empties a channel left it still
     reading "zepto · 6" -- a number contradicted by the 0 in the tile beside
     it. The current selection is preserved across the rebuild. */
  async function fillChannels() {
    const first = !$("rej-channel").dataset.filled;
    if (first) {
      const chans = await api("/channels");
      CHANNELS = Object.keys(chans);
    }
    const selected = $("rej-channel").value;

    const counts = await Promise.all(CHANNELS.map((c) =>
      api(`/rejected?channel=${encodeURIComponent(c)}&limit=1`)
        .then((d) => d.total).catch(() => 0)));

    $("rej-channel").innerHTML = CHANNELS.map((c, i) =>
      `<option value="${esc(c)}">${esc(c)}${counts[i] ? ` · ${counts[i]}` : ""}</option>`
    ).join("");

    if (first) {
      /* Opened on a channel that actually has rejections, so the page does
         not greet you with an empty table on one nobody has reviewed. */
      const best = counts.indexOf(Math.max(...counts));
      $("rej-channel").value = CHANNELS[best >= 0 ? best : 0];
      $("rej-channel").dataset.filled = "1";
    } else if (selected) {
      $("rej-channel").value = selected;
    }
  }

  async function load() {
    const channel = $("rej-channel").value;
    if (!channel) return;

    const params = new URLSearchParams({
      channel, limit: String(STATE.limit), offset: String(STATE.offset),
    });
    if ($("rejEngine").value) params.set("engine", $("rejEngine").value);
    if ($("rejSearch").value.trim()) params.set("q", $("rejSearch").value.trim());

    const [d, off] = await Promise.all([
      api("/rejected?" + params),
      api(`/rejected/offenders?channel=${encodeURIComponent(channel)}`).catch(() => []),
    ]);

    renderTiles(d, off);
    renderOffenders(off);
    renderRows(d, channel);
  }

  function renderTiles(d, off) {
    const repeat = off.filter((o) => o.rejections > 1);
    const worst = off.length ? Math.max(...off.map((o) => o.rejections)) : 0;

    $("rej-t-total").textContent = fmt(d.total);
    $("rej-t-repeat").textContent = fmt(repeat.length);
    $("rej-t-codes").textContent = fmt(off.length);
    $("rej-t-worst").textContent = worst ? `${fmt(worst)}×` : "—";
    $("rejCap").textContent = d.total
      ? `${fmt(d.total)} on ${$("rej-channel").value}` : "";
  }

  function renderOffenders(off) {
    const repeat = off.filter((o) => o.rejections > 1);
    if (!repeat.length) {
      $("offenders").innerHTML = "";
      return;
    }
    $("offenders").innerHTML =
      `<div class="note" style="margin-top:14px">
         <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
         <div><b>${repeat.length} master product${repeat.length === 1 ? "" : "s"}
           attracting repeat rejections.</b> A code that keeps being rejected is usually a
           gate target too small to offer an alternative — every listing routed there comes
           back as the same product.
           <div class="tw tw-short" style="margin-top:10px;background:var(--surface)">
             <table>
               <thead><tr><th>Master product</th><th>Code</th><th class="num">Rejections</th><th></th></tr></thead>
               <tbody>${repeat.map((o) => `<tr>
                 <td class="t-title">${esc(o.product_name || "—")}</td>
                 <td class="m">${esc(o.product_code)}</td>
                 <td class="num" style="color:var(--crit);font-weight:600">${fmt(o.rejections)}</td>
                 <td><a href="#catalog" data-route="catalog"
                   style="color:var(--accent);font-size:12.5px;text-decoration:none;font-weight:600">Inspect &rarr;</a></td>
               </tr>`).join("")}</tbody>
             </table>
           </div>
         </div>
       </div>`;
  }

  function renderRows(d, channel) {
    $("rejBody").innerHTML = d.rows.length
      ? d.rows.map((r) => `<tr>
          <td class="t-title">${esc(r.title || r.sku)}
            <div class="t-sub m">${esc(r.brand || "")} · ${esc(r.sku)}</div></td>
          <td class="t-title" style="color:var(--crit)">${esc(r.rejected_name || "—")}
            <div class="t-sub m">${esc(r.rejected_code || "")}</div></td>
          <td class="t-sub" style="margin:0">${esc(who(r.reviewed_by))}
            ${r.reviewed_at ? `<div class="t-sub m">${new Date(r.reviewed_at)
              .toLocaleDateString(undefined, { day: "2-digit", month: "short" })}</div>` : ""}
            ${r.comment ? `<div class="t-sub" style="max-width:300px">${esc(r.comment)}</div>` : ""}</td>
          <td class="num">${r.score == null ? "—" : r.score.toFixed(2)}</td>
          <td><button class="btn sm rej-undo" data-sku="${esc(r.sku)}">Withdraw</button></td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">Nothing has been rejected on ${esc(channel)}.
           Reject a match from the Results table on New run.</td></tr>`;

    $("rejPager").innerHTML = "";
    $("rejPager").appendChild(pager({
      total: d.total, offset: d.offset, limit: d.limit,
      onGo: (offset, limit) => {
        STATE = { offset, limit };
        load().catch((e) => showError($("errors"), e.message));
      },
    }));

    $("rejBody").querySelectorAll(".rej-undo").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(`/rejected?channel=${encodeURIComponent(channel)}`
            + `&sku=${encodeURIComponent(b.dataset.sku)}`, { method: "DELETE" });
          await fillChannels();
          await load();
        } catch (e) {
          showError($("errors"), e.message);
          b.disabled = false;
        }
      }));
  }

  /* ------------------------------------------------------ reset & re-run */

  /* The SKUs step 1 freed, waiting for step 2.
   *
   * Held here because nothing else can recover it: reset_rejected clears
   * review_status, which is what made those rows identifiable as rejected.
   * After that they are ordinary PENDING rows among thousands. So step 2 is
   * enabled by the existence of this list, not by a flag -- if the list is
   * empty there is genuinely nothing to re-run.
   */
  let RESET_BATCH = null;   // { channel, skus }

  function renderStepState() {
    const ready = !!RESET_BATCH?.skus?.length;
    $("rej-rerun").disabled = !ready;
    $("rej-step-note").textContent = ready
      ? `${fmt(RESET_BATCH.skus.length)} SKU${RESET_BATCH.skus.length === 1 ? "" : "s"} `
        + `reset on ${RESET_BATCH.channel} and waiting for a run.`
      : "Nothing reset yet — step 2 needs a list from step 1.";
  }

  async function openResetConfirm() {
    const channel = $("rej-channel").value;
    let preview;
    try {
      preview = await api("/rejected/reset/preview", {
        method: "POST", body: JSON.stringify({ channel }),
      });
    } catch (e) {
      showError($("errors"), e.message);
      return;
    }
    if (!preview.rows_found) {
      showError($("errors"), `Nothing is rejected on ${channel}.`);
      return;
    }

    $("confirm-title").innerHTML =
      `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8.5v4.5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20.2h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7"/></svg>
       Delete these matches and re-queue`;
    $("confirm-lede").textContent =
      "This deletes mapping rows. It cannot be undone — the rejected matches "
      + "and the rejection records both go.";
    $("confirm-body").innerHTML =
      `<div class="m-sec">
         <h3>What changes</h3>
         <dl class="kv">
           <dt>Channel</dt><dd>${esc(channel)}</dd>
           <dt>Rejected rows</dt><dd class="big">${fmt(preview.rows_found)}</dd>
           <dt>Mapping rows</dt><dd class="crit">deleted</dd>
           <dt>mapping_status</dt><dd>&rarr; PENDING</dd>
           <dt>review_status</dt><dd>&rarr; cleared</dd>
         </dl>
       </div>
       <div class="m-sec">
         <h3>Afterwards</h3>
         <ul class="m-rules">
           <li>These SKUs become ordinary PENDING rows — the engine will match them again.</li>
           <li>The rejection record goes with the match, so the Rejected page empties.</li>
           <li>The list is held on this page for step 2. Leaving the page loses it,
             and the SKUs are then indistinguishable from any other PENDING row.</li>
           <li>Steward-approved rows are never touched.</li>
         </ul>
       </div>
       <div class="m-sec">
         <h3>Sample of what is affected</h3>
         <div class="m-cmd">${preview.skus.slice(0, 8).map(esc).join("\n")}${
           preview.skus.length > 8 ? `\n… and ${fmt(preview.skus.length - 8)} more` : ""}</div>
       </div>
       <div class="m-sec">
         <label class="m-ack"><input type="checkbox" id="rej-ack">
           I understand ${fmt(preview.rows_found)} mapping row${
             preview.rows_found === 1 ? "" : "s"} will be deleted and cannot be restored.</label>
       </div>`;
    $("confirm-go").textContent = "Delete and re-queue";
    $("confirm-go").disabled = true;
    $("rej-ack").addEventListener("change", (e) => {
      $("confirm-go").disabled = !e.target.checked;
    });

    window.ConfirmDialog.open(async () => {
      const result = await api("/rejected/reset", {
        method: "POST", body: JSON.stringify({ channel }),
      });
      RESET_BATCH = { channel, skus: result.skus };
      renderStepState();
      await fillChannels();      // the option labels carry the counts
      await load();
    });
  }

  async function rerun() {
    if (!RESET_BATCH?.skus?.length) return;
    const { channel, skus } = RESET_BATCH;

    $("rej-rerun").disabled = true;
    $("rej-step-note").textContent = "Starting the run…";
    try {
      await window.RunLauncher.launchSkus({
        channel, skus,
        /* Both engines: a reset batch can hold Himalaya and competitor
           listings alike, and the brand filter decides which is which. */
        engine: null,
        onDone: async () => {
          /* Consumed, so the same batch cannot be run twice -- a second run
             over the same SKUs costs LLM spend for no new information. */
          RESET_BATCH = null;
          renderStepState();
          if ($("rej-channel").dataset.filled) await load().catch(() => {});
        },
      });
    } catch (e) {
      showError($("errors"), e.message);
      renderStepState();
    }
  }

  $("rej-reset").addEventListener("click", () => openResetConfirm());
  $("rej-rerun").addEventListener("click", () => rerun());

  /* ------------------------------------------------------------ wiring */

  $("rej-channel").addEventListener("change", () => {
    STATE.offset = 0;
    load().catch((e) => showError($("errors"), e.message));
  });
  $("rejEngine").addEventListener("change", () => {
    STATE.offset = 0;
    load().catch((e) => showError($("errors"), e.message));
  });

  let qTimer = null;
  $("rejSearch").addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      STATE.offset = 0;
      load().catch((e) => showError($("errors"), e.message));
    }, 300);
  });

  window.Pages = window.Pages || {};
  window.Pages["rejected"] = {
    async boot() {
      await fillChannels();
      await load();
    },
    /* Called after a rejection is recorded elsewhere, so the backlog is
       current the next time it is opened. Silent if the page has never been
       opened -- there is nothing rendered to correct. */
    async refresh() {
      if (!$("rej-channel").dataset.filled) return;
      try {
        await fillChannels();
        await load();
      } catch { /* the page reports on its next open */ }
    },
  };
})();
