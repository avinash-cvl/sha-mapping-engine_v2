/* Shared client for the engine console.
 *
 * Thin by intent: every number rendered here comes from the API, which gets
 * it from the same resolver the engine selects with. Nothing is computed
 * client-side that the server could compute, because a count derived twice
 * eventually disagrees with itself.
 */

/* Wrapped in an IIFE so nothing here lands on the global scope.
 *
 * These were top-level `const api`, `const fmt`, `function esc` ... in a
 * classic <script>, which shares one global scope with every other classic
 * script on the page. Each page then opens with
 * `const { api, fmt, ... } = window.Console;` -- a redeclaration of the same
 * names in that same scope, which is a SyntaxError. A SyntaxError is not
 * caught at runtime: the whole page script is discarded before its first
 * line executes, so every screen rendered its empty shell and fetched
 * nothing. window.Console is the only thing that escapes now.
 */
(function () {
"use strict";

  const API = "/api/engine";

  async function api(path, opts = {}) {
    const res = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    /* An expired session must not surface as a generic error on every panel.
       Send the user to sign in, carrying where they were so the deep link
       survives. */
    if (res.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/login?expired=1&next=${next}`;
      throw new Error("Session expired.");
    }
    if (res.status === 403) {
      throw new Error("Your account does not have access to the engine console.");
    }
    if (!res.ok) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
      throw new Error(detail || `Request failed (${res.status})`);
    }
    return res.json();
  }

  const fmt = (n) => (n ?? 0).toLocaleString("en-IN");

  function duration(seconds) {
    if (!seconds) return "—";
    if (seconds < 60) return `${seconds}s`;
    const m = Math.round(seconds / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  /* Status vocabulary is defined once. A new engine status changes one map,
   * not every table that renders it. */
  const STATUS = {
    AutoMatch:            { cls: "p-ok",   label: "AutoMatch" },
    StewardReview:        { cls: "p-rev",  label: "StewardReview" },
    LowConfidence:        { cls: "p-low",  label: "LowConfidence" },
    NoHimalayaEquivalent: { cls: "p-no",   label: "NoHimalayaEquivalent" },
    Failed:               { cls: "p-fail", label: "Failed" },
    PENDING:              { cls: "p-no",   label: "Pending" },
    completed:            { cls: "p-ok",   label: "Completed" },
    completed_with_failures: { cls: "p-rev", label: "With failures" },
    running:              { cls: "p-rev",  label: "Running" },
    cancelled:            { cls: "p-no",   label: "Cancelled" },
    failed:               { cls: "p-fail", label: "Failed" },
    stale:                { cls: "p-fail", label: "Stale — no heartbeat" },
  };

  function pill(status) {
    const s = STATUS[status] || { cls: "p-no", label: status || "—" };
    return `<span class="pill ${s.cls}"><i class="sq"></i>${s.label}</span>`;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /* Error surfacing: a failed run is a persistent banner, never a toast that
   * scrolls away. The caller decides which container it lands in. */
  function showError(el, message) {
    if (!el) return;
    el.innerHTML = `<div class="note" role="alert" style="background:var(--crit-soft);
        border:1px solid color-mix(in srgb,var(--crit) 32%,transparent);margin:0 0 14px">
        <div><b>Something went wrong.</b> ${esc(message)}</div></div>`;
  }

  function clearError(el) { if (el) el.innerHTML = ""; }

  /* Progress stream. Reconnects are visible rather than silent -- a console
   * that quietly stops updating during a 4-hour run is worse than one that
   * says it lost the connection. */
  function streamRun(runId, { onProgress, onDone, onError }) {
    const es = new EventSource(`${API}/runs/${runId}/stream`);
    es.addEventListener("progress", (e) => onProgress(JSON.parse(e.data)));
    es.addEventListener("done", (e) => { onDone(JSON.parse(e.data)); es.close(); });
    es.addEventListener("error", () => onError?.("Connection lost — retrying…"));
    return es;
  }

  async function whoami() {
    const res = await fetch("/api/session/me");
    if (!res.ok) { location.href = "/login"; throw new Error("not signed in"); }
    return (await res.json()).user;
  }

  async function mountUser(el) {
    if (!el) return null;
    const user = await whoami();
    el.innerHTML = `${esc(user.name || user.email)} · <b>${esc(user.role)}</b>
      <a href="#" id="signout" style="color:var(--accent);margin-left:8px">Sign out</a>`;
    el.querySelector("#signout").addEventListener("click", async (e) => {
      e.preventDefault();
      await fetch("/api/session/logout", { method: "POST" });
      location.href = "/login";
    });
    return user;
  }

  window.Console = { whoami, mountUser, api, fmt, duration, pill, esc, showError, clearError, streamRun, STATUS };

})();
