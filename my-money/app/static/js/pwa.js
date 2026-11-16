/* MY MONEY - PWA: registers the service worker and offers "install" on Android, Samsung and iPhone. */
(function () {
  "use strict";
  var KEY = "mm_pwa_dismissed", WAIT_DAYS = 14;

  function store(op, k, v) { try { return op === "get" ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { return null; } }
  function standalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  }
  function recentlyDismissed() {
    var t = parseInt(store("get", KEY) || "0", 10);
    return t && (Date.now() - t) < WAIT_DAYS * 86400000;
  }
  var ua = navigator.userAgent || "";
  var isIOS = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  /* ----- service worker ----- */
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () { /* app still works without it */ });
    });
  }

  if (standalone()) return;               // already installed: nothing to offer

  /* ----- install banner ----- */
  var deferred = null, banner = null;
  var shareSvg = '<svg class="share-glyph" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3M8 7l4-4 4 4"/><path d="M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></svg>';

  function build(kind) {
    if (banner) return banner;
    banner = document.createElement("div");
    banner.className = "pwa-banner" + (kind === "ios" ? " ios" : "");
    banner.setAttribute("role", "dialog");
    banner.setAttribute("aria-label", "Install the app");
    var text = kind === "ios"
      ? "Tap " + shareSvg + " <b style='display:inline;font-size:12.5px'>Share</b>, then choose <b style='display:inline;font-size:12.5px'>Add to Home Screen</b>."
      : "Add it to your home screen for one-tap access.";
    banner.innerHTML = '<img src="/static/img/icon-192.png" alt="" width="46" height="46">' +
      '<div class="pwa-text"><b>Install MY MONEY</b><span>' + text + '</span></div>' +
      '<div class="pwa-actions">' + (kind === "ios" ? "" : '<button type="button" class="go" data-pwa-go>Install</button>') +
      '<button type="button" data-pwa-no>' + (kind === "ios" ? "Got it" : "Not now") + '</button></div>';
    document.body.appendChild(banner);
    banner.addEventListener("click", function (e) {
      if (e.target.closest("[data-pwa-no]")) { store("set", KEY, String(Date.now())); hide(); }
      if (e.target.closest("[data-pwa-go]")) install();
    });
    return banner;
  }
  function show(kind) { build(kind).classList.add("show"); }
  function hide() { if (banner) banner.classList.remove("show"); }

  function install() {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.then(function (c) {
      if (c && c.outcome !== "accepted") store("set", KEY, String(Date.now()));
      deferred = null; hide(); toggleLinks(false);
    });
  }

  /* any element with data-install (e.g. a footer link) is shown only when installing is possible */
  function toggleLinks(on) {
    document.querySelectorAll("[data-install]").forEach(function (el) { el.hidden = !on; });
  }
  document.addEventListener("click", function (e) {
    var l = e.target.closest("[data-install]");
    if (!l) return;
    e.preventDefault();
    if (deferred) install(); else if (isIOS) show("ios");
  });

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault(); deferred = e; toggleLinks(true);
    if (!recentlyDismissed()) setTimeout(function () { if (deferred) show("android"); }, 3500);
  });
  window.addEventListener("appinstalled", function () { deferred = null; hide(); toggleLinks(false); store("set", KEY, String(Date.now())); });

  if (isIOS) {
    var go = function () {
      toggleLinks(true);
      if (!recentlyDismissed()) setTimeout(function () { show("ios"); }, 3500);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go); else go();
  }
})();
