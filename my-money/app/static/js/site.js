/* MY MONEY - public site behaviour: savings/lending theme switch, reveal-on-scroll. Developed and Powered by Emtrixz Technology Company. */
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");

  /* ----- theme switch (landing, about, contact). Auth pages fix their own theme on the server. ----- */
  var switchable = root.hasAttribute("data-switchable");
  function paint(t) {
    root.setAttribute("data-theme", t);
    document.querySelectorAll("[data-set-theme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-set-theme") === t ? "true" : "false");
    });
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute("content", t === "lending" ? "#0A0A0B" : "#060912");
  }
  paint(root.getAttribute("data-theme") || "savings");
  if (switchable) {
    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-set-theme]");
      if (!b) return;
      var t = b.getAttribute("data-set-theme") === "lending" ? "lending" : "savings";
      paint(t);
      try { localStorage.setItem("mm-theme", t); } catch (err) { /* private mode: the choice just is not remembered */ }
    });
  }

  /* ----- reveal on scroll (skipped for reduced motion) ----- */
  var items = document.querySelectorAll(".reveal");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!("IntersectionObserver" in window) || reduced) {
    items.forEach(function (n) { n.classList.add("in-view"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("in-view"); io.unobserve(en.target); } });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach(function (n) { io.observe(n); });
  }
})();
