/* Catalog: the master catalogue, and what maps to each product.
 *
 * The inverse of the Results table. Results answers "what did this listing
 * match to"; Catalog answers "what has been matched to this product" -- which
 * is the question a steward actually asks when they suspect a master row is
 * acting as a decoy. A node holding one product returns that product for
 * everything routed to it, and the only way to SEE that is to stand on the
 * product and count what arrived.
 */
(function () {
"use strict";

  const { api, esc, fmt, pager, showError } = window.Console;
  const $ = (id) => document.getElementById(id);

  let STATE = { offset: 0, limit: 25, q: "", category: "" };
  let SELECTED = null;

  function gatePill(n) {
    if (n === 0) return '<span class="pill p-fail"><i class="sq"></i>Empty node</span>';
    if (n <= 3) return '<span class="pill p-rev"><i class="sq"></i>At threshold</span>';
    return '<span class="pill p-ok"><i class="sq"></i>Healthy</span>';
  }

  async function load() {
    const params = new URLSearchParams({
      limit: String(STATE.limit), offset: String(STATE.offset),
    });
    if (STATE.q) params.set("q", STATE.q);
    if (STATE.category) params.set("category", STATE.category);

    const d = await api("/catalog?" + params);

    $("cat-count").textContent = STATE.category || STATE.q
      ? `${fmt(d.total)} of ${fmt(d.catalog_total)} products`
      : `${fmt(d.total)} products in the master catalogue`;

    $("cat-rows").innerHTML = d.rows.length
      ? d.rows.map((p) => `<tr class="rowlink" data-code="${esc(p.product_code)}">
          <td class="t-title" style="font-weight:${p.mapped ? 600 : 400}">${esc(p.product_name)}
            <div class="t-sub">${esc(p.category || "—")}${
              p.subcategory ? " › " + esc(p.subcategory) : ""}</div></td>
          <td class="m">${esc(p.product_code)}</td>
          <td class="num">${esc(p.pack || "—")}</td>
          <td class="num">${p.mapped ? fmt(p.mapped) : '<span class="t-sub" style="margin:0">0</span>'}</td>
          <td>${gatePill(p.node_rows)}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="empty">No products match this search.</td></tr>`;

    $("cat-pager").innerHTML = "";
    $("cat-pager").appendChild(pager({
      total: d.total, offset: d.offset, limit: d.limit,
      onGo: (offset, limit) => {
        STATE.offset = offset; STATE.limit = limit;
        load().catch((e) => showError($("errors"), e.message));
      },
    }));

    $("cat-rows").querySelectorAll(".rowlink").forEach((tr) =>
      tr.addEventListener("click", () => select(tr.dataset.code)));

    /* Selecting the first row on load means the detail panel is never an
       empty box next to a full table -- the page opens showing what it does. */
    if (!SELECTED && d.rows.length) select(d.rows[0].product_code);
    else if (SELECTED) markSelected();

    if (!$("cat-category").dataset.filled && d.categories) {
      $("cat-category").innerHTML = `<option value="">All categories</option>`
        + d.categories.map((c) => `<option>${esc(c)}</option>`).join("");
      $("cat-category").dataset.filled = "1";
    }
  }

  function markSelected() {
    $("cat-rows").querySelectorAll("tr").forEach((tr) =>
      tr.classList.toggle("rowsel", tr.dataset.code === SELECTED));
  }

  async function select(code) {
    SELECTED = code;
    markSelected();
    $("cat-detail").innerHTML = `<div class="panel-b"><div class="loading">Loading product</div></div>`;
    try {
      const d = await api(`/catalog/${encodeURIComponent(code)}`);
      renderDetail(d);
    } catch (e) {
      $("cat-detail").innerHTML =
        `<div class="panel-b"><p class="empty">${esc(e.message)}</p></div>`;
    }
  }

  function renderDetail(d) {
    const p = d.product;
    const listings = d.listings || [];

    $("cat-detail").innerHTML =
      `<div class="prod-h">
         <div class="pn">${esc(p.product_name)}</div>
         <div class="ps">${esc(p.product_code)} · ${esc(p.category || "—")}${
           p.subcategory ? " › " + esc(p.subcategory) : ""}</div>
       </div>
       <div class="panel-b">
         <div class="meta">
           <div><span class="k">Pack size</span><span class="v">${esc(p.pack || "—")}</span></div>
           <div><span class="k">Gate target</span><span class="v">${fmt(d.node_rows)} rows</span></div>
         </div>
         <div class="grp" style="margin-top:16px">Mapped listings <span>· ${fmt(d.total)}</span></div>
         ${listings.length ? `<div class="tw tw-short"><table>
           <thead><tr><th>Source</th><th>Channel</th><th class="num">Conf.</th></tr></thead>
           <tbody>${listings.map((l) => `<tr>
             <td class="t-title">${esc(l.title || l.sku)}
               <div class="t-sub m">${esc(l.brand || "")}</div></td>
             <td class="t-sub" style="margin:0">${esc(l.channel)}</td>
             <td class="num">${l.score == null ? "—" : `<span class="conf">
               <span class="meter"><i style="width:${Math.round(l.score * 100)}%;background:${
                 l.status === "AutoMatch" ? "var(--ok)" : "var(--warn)"}"></i></span>${
                 l.score.toFixed(2)}</span>`}</td>
           </tr>`).join("")}</tbody></table></div>`
          : `<p class="empty">Nothing has been mapped to this product yet.</p>`}
         ${d.total > listings.length
           ? `<p class="hint" style="margin-top:10px">Showing the ${fmt(listings.length)}
                highest-scoring of ${fmt(d.total)}.</p>` : ""}
         <p class="hint" style="margin-top:10px">${d.node_rows <= 3
           ? `<b style="color:var(--warn)">This node holds only ${fmt(d.node_rows)} row${
               d.node_rows === 1 ? "" : "s"}</b> — anything gated onto it has almost no shortlist to choose from.`
           : `This node holds ${fmt(d.node_rows)} rows, so a gate onto it has a real shortlist.`}</p>
       </div>`;
  }

  /* ------------------------------------------------------------ wiring */

  let qTimer = null;
  $("cat-q").addEventListener("input", (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      STATE.q = e.target.value.trim();
      STATE.offset = 0;
      SELECTED = null;
      load().catch((err) => showError($("errors"), err.message));
    }, 300);
  });
  $("cat-category").addEventListener("change", (e) => {
    STATE.category = e.target.value;
    STATE.offset = 0;
    SELECTED = null;
    load().catch((err) => showError($("errors"), err.message));
  });

  window.Pages = window.Pages || {};
  window.Pages["catalog"] = { boot: load };
})();
