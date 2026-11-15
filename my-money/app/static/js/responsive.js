/* MY MONEY - responsive helpers: public burger menu, drawer polish, labelled table cards. */
(function () {
  "use strict";

  /* ----- public navigation ----- */
  function pubNav() {
    var nav = document.querySelector(".pub-nav");
    if (!nav) return;
    var btn = nav.querySelector("[data-pubnav]");
    if (!btn) return;
    function set(open) {
      nav.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      document.body.classList.toggle("no-scroll", open);
    }
    btn.addEventListener("click", function () { set(!nav.classList.contains("open")); });
    nav.addEventListener("click", function (e) { if (e.target.closest(".pub-panel a")) set(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") set(false); });
    window.addEventListener("resize", function () { if (window.innerWidth > 991) set(false); });
  }

  /* ----- app drawer: lock page scroll, close on link tap or Escape ----- */
  function drawer() {
    var side = document.querySelector(".sidebar");
    var scrim = document.querySelector(".scrim");
    if (!side) return;
    function sync() { document.body.classList.toggle("no-scroll", side.classList.contains("open")); }
    new MutationObserver(sync).observe(side, { attributes: true, attributeFilter: ["class"] });
    function close() { side.classList.remove("open"); if (scrim) scrim.classList.remove("open"); }
    side.addEventListener("click", function (e) { if (e.target.closest("a[href]")) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && side.classList.contains("open")) close(); });
    window.addEventListener("resize", function () { if (window.innerWidth > 991) close(); });
  }

  /* ----- tables: copy header text into each cell so phones can show label/value cards ----- */
  function labelTables(root) {
    (root || document).querySelectorAll(".table-wrap").forEach(function (wrap) {
      if (wrap.classList.contains("keep-table") || wrap.hasAttribute("data-no-stack")) return;
      var table = wrap.querySelector("table.table");
      if (!table) return;
      var heads = [];
      var headRow = table.querySelector("thead tr");
      if (!headRow) return;
      headRow.querySelectorAll("th").forEach(function (th) {
        var span = th.colSpan || 1, text = (th.textContent || "").replace(/\s+/g, " ").trim();
        for (var i = 0; i < span; i++) heads.push(text);
      });
      table.querySelectorAll("tbody tr, tfoot tr").forEach(function (tr) {
        var col = 0;
        Array.prototype.forEach.call(tr.children, function (td) {
          if (td.tagName !== "TD" && td.tagName !== "TH") return;
          if (!td.children.length && !(td.textContent || "").trim()) td.classList.add("empty-cell");
          if (!td.hasAttribute("data-label")) td.setAttribute("data-label", td.colSpan > 1 ? "" : (heads[col] || ""));
          col += td.colSpan || 1;
        });
      });
      wrap.classList.add("is-stack");
    });
  }

  function init() {
    pubNav(); drawer(); labelTables();
    var t = null;
    new MutationObserver(function () { clearTimeout(t); t = setTimeout(function () { labelTables(); }, 120); })
      .observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
