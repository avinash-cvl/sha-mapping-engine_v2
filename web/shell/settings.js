/* Settings: team defaults, channels, members, and what this console is.
 *
 * Deliberately thin. The engine's tunables live in config.py and are shown
 * (read-only) on Match rules; repeating them here as editable fields would
 * create a second place to change something that only one place can actually
 * change. What belongs here is what is genuinely about the deployment rather
 * than about matching.
 */
(function () {
"use strict";

  const { api, esc, fmt } = window.Console;
  const $ = (id) => document.getElementById(id);

  async function loadDefaults() {
    const d = await api("/config");
    const pick = (k) => d.settings.find((s) => s.key === k);

    const rows = [
      ["SEMANTIC_TOPK", "Vector retrieval depth"],
      ["LEXICAL_TOPK", "BM25 retrieval depth"],
      ["CATEGORY_GATE_MIN_TARGET_POP", "Starved-node backstop threshold"],
      ["TOP_N_OUTPUT", "Candidates handed to the LLM judge"],
    ].map(([key, blurb]) => {
      const s = pick(key);
      return `<div class="setrow">
        <div class="st"><b>${esc(key)}</b><span>${esc(blurb)}</span></div>
        <div class="sv" style="display:flex;align-items:center;gap:8px">${esc(s?.value ?? "—")}
          ${s?.locked ? '<span class="pill p-no"><i class="sq"></i>Locked</span>' : ""}
          ${s?.env_override ? '<span class="pill p-rev"><i class="sq"></i>Env</span>' : ""}</div>
      </div>`;
    }).join("");

    $("set-defaults").innerHTML = rows
      + `<div class="setrow">
           <div class="st"><b>Scoring weights</b>
             <span>5 signals, currently summing to ${d.weights.total.toFixed(2)}</span></div>
           <div class="sv"><a href="#rules" data-route="rules"
             style="color:var(--accent);text-decoration:none;font-weight:600">View &amp; edit &rarr;</a></div>
         </div>`;
  }

  async function loadChannels() {
    const d = await api("/settings/channels");
    $("set-channels").innerHTML = d.length
      ? d.map((c) => `<tr>
          <td class="t-title" style="font-weight:600">${esc(c.channel)}</td>
          <td class="m">${esc(c.source_table)}</td>
          <td class="num">${fmt(c.rows)}</td>
          <td class="num" ${c.pending ? 'style="color:var(--warn);font-weight:600"' : ""}>${fmt(c.pending)}</td>
          <td class="t-sub" style="margin:0">${c.last_run
            ? new Date(c.last_run).toLocaleString(undefined,
                { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
            : "never"}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">No channels configured.</td></tr>`;
  }

  async function loadMembers() {
    const d = await api("/settings/members");
    $("set-members").innerHTML = d.length
      ? d.map((m) => `<tr>
          <td class="t-title">${esc(m.name || m.email)}
            <div class="t-sub m">${esc(m.email)}</div></td>
          <td>${m.role === "ADMIN"
            ? '<span class="pill p-ok"><i class="sq"></i>Admin</span>'
            : `<span class="pill p-no"><i class="sq"></i>${esc(m.role || "—")}</span>`}</td>
          <td>${m.is_active
            ? '<span class="pill p-ok"><i class="sq"></i>Active</span>'
            : '<span class="pill p-no"><i class="sq"></i>Disabled</span>'}</td>
        </tr>`).join("")
      : `<tr><td colspan="3" class="empty">No members found.</td></tr>`;
  }

  async function loadAbout() {
    const d = await api("/settings/about");
    $("set-about").innerHTML = [
      ["Environment", d.environment],
      ["Database", d.database],
      ["Server", d.server],
      ["Code version", d.git_sha || "unknown"],
      ["Branch", d.branch || "unknown"],
      ["Console port", String(d.port)],
      ["Log directory", d.log_dir],
    ].map(([k, v]) => `<div class="setrow">
        <div class="st"><b>${esc(k)}</b></div>
        <div class="sv">${esc(v)}</div></div>`).join("")
      + `<div class="note info">
           <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8v5M12 16.5v.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/></svg>
           <div>The console launches the same command a terminal user types —
             <span class="m">python -m &lt;engine&gt;.batch_flow match …</span> — as a subprocess.
             There is no second code path for a run, so the CLI keeps working unchanged.</div></div>`;
  }

  window.Pages = window.Pages || {};
  window.Pages["settings"] = {
    async boot() {
      /* Each section reports its own failure rather than the page failing as
         a whole: members is the one most likely to 403, and that should not
         hide the environment block someone opened the page to check. */
      const fail = (el, e) => { $(el).innerHTML = `<p class="empty">${esc(e.message)}</p>`; };
      await Promise.all([
        loadDefaults().catch((e) => fail("set-defaults", e)),
        loadChannels().catch((e) => ($("set-channels").innerHTML =
          `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`)),
        loadMembers().catch((e) => ($("set-members").innerHTML =
          `<tr><td colspan="3" class="empty">${esc(e.message)}</td></tr>`)),
        loadAbout().catch((e) => fail("set-about", e)),
      ]);
    },
  };
})();
