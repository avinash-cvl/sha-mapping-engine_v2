/* Shell: routing, theme, and lazy page boot.
 *
 * Loads last so every page module has registered itself on window.Pages by
 * the time the first route resolves.
 *
 * Pages are boot-on-first-visit, not boot-on-load. The console opens on Runs;
 * fetching the catalog, the gate backlog and the log directory at that moment
 * would put six queries in front of the one table the operator came to read.
 */
(function () {
"use strict";

  const { showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  /* ---------- theme ---------- */
  const root = document.documentElement;
  const THEME_KEY = "console.theme";

  function effectiveTheme() {
    const explicit = root.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved) root.setAttribute("data-theme", saved);
  } catch { /* private window: the OS preference still applies */ }

  $("themeToggle").addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* not worth failing over */ }
  });

  /* ---------- routing ---------- */
  const ROUTES = {
    "runs":      { label: "Runs" },
    "runs-new":  { label: "Runs", sub: "New run" },
    "catalog":   { label: "Catalog" },
    "rules":     { label: "Match rules" },
    "history":   { label: "History" },
    "alerts":    { label: "Alerts" },
    "settings":  { label: "Settings" },
  };
  /* runs-new has no rail button of its own -- it is a mode of Runs, and
     highlighting nothing while you sit on it makes the rail look broken. */
  const RAIL_OF = { "runs-new": "runs" };

  const booted = new Set();

  function go(route) {
    if (!ROUTES[route]) route = "runs";

    document.querySelectorAll(".page").forEach((p) => {
      p.hidden = p.dataset.page !== route;
    });
    const rail = RAIL_OF[route] || route;
    document.querySelectorAll(".rail-btn[data-route]").forEach((b) =>
      b.setAttribute("aria-current", b.dataset.route === rail ? "page" : "false"));

    const r = ROUTES[route];
    $("crumb").innerHTML = r.sub
      ? `<span>${r.label}</span><span class="sep">/</span><b>${r.sub}</b>`
      : `<b>${r.label}</b>`;

    if (location.hash.slice(1) !== route) history.replaceState(null, "", "#" + route);
    window.scrollTo({ top: 0 });

    /* One boot per page, and a failure surfaces on the error strip rather
       than as an unhandled rejection nobody sees. */
    const page = window.Pages?.[route];
    if (page && !booted.has(route)) {
      booted.add(route);
      Promise.resolve(page.boot?.()).catch((e) => {
        booted.delete(route);            // let a retry happen on re-entry
        showError($("errors"), e.message);
      });
    } else if (page?.refresh) {
      Promise.resolve(page.refresh()).catch(() => { /* refresh is best effort */ });
    }
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-route]");
    if (!el) return;
    e.preventDefault();
    go(el.dataset.route);
  });
  window.addEventListener("hashchange", () => go(location.hash.slice(1)));

  /* ---------- keyboard ---------- */
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); $("cmdInput").focus();
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
      e.preventDefault(); go("runs-new");
    }
  });

  /* Search routes rather than filtering in place: the thing being searched
     for lives on a page, so the search takes you there with the term applied. */
  $("cmdInput").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim();
    if (!q) return;
    /* A run id is a uuid or its 8-char prefix; anything else is a product
       or category, which Catalog is the page for. */
    if (/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(q) || /^[0-9a-f]{8}$/i.test(q)) {
      go("history");
      const f = $("f-q"); if (f) { f.value = q; f.dispatchEvent(new Event("input")); }
    } else {
      go("catalog");
      const c = $("cat-q"); if (c) { c.value = q; c.dispatchEvent(new Event("input")); }
    }
  });

  /* ---------- identity ---------- */
  (async () => {
    try {
      const user = await window.Console.whoami();
      const name = user.name || user.email;
      $("avatar").textContent = (name.match(/\b\w/g) || ["?"]).slice(0, 2).join("").toUpperCase();
      $("who").innerHTML =
        `${window.Console.esc(name)} <a href="#" id="signout">Sign out</a>`;
      $("signout").addEventListener("click", async (e) => {
        e.preventDefault();
        await fetch("/api/session/logout", { method: "POST" });
        location.href = "/login";
      });
    } catch { /* whoami already redirects to /login */ }
  })();

  window.Shell = { go };
  go(location.hash ? location.hash.slice(1) : "runs");
})();
