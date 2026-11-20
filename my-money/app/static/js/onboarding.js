/* MY MONEY - one-time welcome sequence.
   Phase 1: 5-second branded splash (progress bar, Skip).  Phase 2 (founders only): "Founding Billionaire" modal.
   Joined members see Phase 1 only. The server is told once, so the sequence never repeats:
   founders when the modal first opens, members when their splash first appears.
   Every timer, animation frame and listener is released in cleanup(), which also runs on pagehide. */
(function () {
  "use strict";
  var root = document.getElementById("ob-root");
  if (!root) return;

  var KIND = root.dataset.kind, MS = (parseInt(root.dataset.seconds, 10) || 5) * 1000;
  var splash = root.querySelector(".ob-splash"), modal = root.querySelector("[data-ob-modal]");
  var fill = root.querySelector("[data-ob-fill]"), bar = root.querySelector(".ob-bar"), count = root.querySelector("[data-ob-count]");
  var skipBtn = root.querySelector("[data-ob-skip]"), portrait = root.querySelector("[data-ob-portrait]");
  var phase = 0, raf = 0, endTimer = 0, startedAt = 0, sent = false, opener = document.activeElement;
  var timers = [], cleaned = false;

  function later(fn, ms) { var id = setTimeout(fn, ms); timers.push(id); return id; }
  function csrf() { return (document.querySelector('meta[name="csrf-token"]') || {}).content || ""; }
  function lock(on) { document.documentElement.classList.toggle("ob-lock", !!on); }

  function markDone() {                       // tell the server once; keepalive lets it finish even if the page closes
    if (sent) return; sent = true;
    try {
      fetch(root.dataset.completeUrl, { method: "POST", credentials: "same-origin", keepalive: true,
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch", "X-Mm-Passive": "1" } }).catch(function () {});
    } catch (e) { /* offline: it will simply show once more */ }
  }

  /* ---------- Phase 1: splash ---------- */
  function tick(now) {
    var p = Math.min(1, (now - startedAt) / MS);
    fill.style.transform = "scaleX(" + p + ")";
    bar.setAttribute("aria-valuenow", String(Math.round(p * 100)));
    count.textContent = String(Math.max(0, Math.ceil((MS - (now - startedAt)) / 1000)));
    if (p < 1 && phase === 1) raf = requestAnimationFrame(tick);
  }
  function startSplash() {
    if (phase || cleaned) return;
    phase = 1; startedAt = performance.now();
    lock(true);
    raf = requestAnimationFrame(tick);
    endTimer = setTimeout(endSplash, MS);          // fires even if animation frames are paused in a background tab
    splash.focus({ preventScroll: true });          // announce the dialog without putting a focus ring on Skip
    if (KIND !== "FOUNDER") markDone();            // members: the splash is their whole sequence
  }
  function endSplash() {
    if (phase !== 1) return;
    phase = 2;
    clearTimeout(endTimer); cancelAnimationFrame(raf);
    splash.classList.add("ob-out");
    if (KIND === "FOUNDER" && modal) later(openModal, 220);
    else later(finish, 260);
  }

  /* ---------- Phase 2: modal (founders) ---------- */
  function focusables() {
    return Array.prototype.slice.call(modal.querySelectorAll('a[href], button:not([disabled])')).filter(function (el) { return el.offsetParent !== null; });
  }
  function openModal() {
    if (cleaned) return;
    splash.classList.add("hide");
    modal.classList.remove("hide");
    requestAnimationFrame(function () { modal.classList.add("ob-in"); });
    phase = 3;
    markDone();                                    // founders: counted as done the moment the modal first opens
    var go = modal.querySelector("[data-ob-done]"); if (go) go.focus({ preventScroll: true });
  }
  function onDone() {                              // "Let's Build Wealth": close and go to the main group dashboard
    var target = new URL(root.dataset.dashboardUrl, location.href);
    var here = target.pathname === location.pathname;
    finish();
    if (!here) location.assign(target.href);
  }

  /* ---------- shared ---------- */
  function onKey(e) {
    if (phase === 1 && e.key === "Escape") { e.preventDefault(); endSplash(); return; }
    if (phase !== 3) return;
    if (e.key === "Escape") { e.preventDefault(); finish(); return; }
    if (e.key === "Tab") {                         // keep keyboard focus inside the modal
      var f = focusables(); if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  function onVisible() { if (document.visibilityState === "visible" && phase === 1 && performance.now() - startedAt >= MS) endSplash(); }
  function onBackdrop(e) { if (e.target === modal) finish(); }

  function finish() {
    cleanup();
    root.remove();
    if (opener && opener.focus) { try { opener.focus({ preventScroll: true }); } catch (e) { /* element gone */ } }
  }
  function cleanup() {
    if (cleaned) return; cleaned = true;
    clearTimeout(endTimer); cancelAnimationFrame(raf);
    timers.forEach(clearTimeout); timers = [];
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("visibilitychange", onVisible);
    window.removeEventListener("pagehide", cleanup);
    skipBtn.removeEventListener("click", endSplash);
    if (modal) {
      modal.removeEventListener("click", onBackdrop);
      var c = modal.querySelector("[data-ob-close]"), d = modal.querySelector("[data-ob-done]");
      if (c) c.removeEventListener("click", finish);
      if (d) d.removeEventListener("click", onDone);
    }
    lock(false);
  }

  document.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("pagehide", cleanup);
  skipBtn.addEventListener("click", endSplash);
  if (modal) {
    modal.addEventListener("click", onBackdrop);
    var closeBtn = modal.querySelector("[data-ob-close]"), doneBtn = modal.querySelector("[data-ob-done]");
    if (closeBtn) closeBtn.addEventListener("click", finish);
    if (doneBtn) doneBtn.addEventListener("click", onDone);
  }

  /* The countdown starts once the CEO portrait is decoded (waiting at most 1.2s), so nothing pops in or shifts mid-timer. */
  if (portrait) {
    var showFallback = function () {
      portrait.classList.add("hide");
      var fb = root.querySelector(".ob-portrait-fallback"); if (fb) fb.classList.remove("hide");
    };
    portrait.addEventListener("error", showFallback);
    if (portrait.complete && portrait.naturalWidth === 0) showFallback();     // failed before this script ran
    var ready = portrait.decode ? portrait.decode().catch(function () {}) : Promise.resolve();
    Promise.race([ready, new Promise(function (r) { later(r, 1200); })]).then(startSplash);
  } else {
    startSplash();
  }
})();
