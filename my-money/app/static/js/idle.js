/* MY MONEY - automatic sign-out after inactivity (website and installed app).
   The server enforces the limit too (a request after 30 idle minutes is refused), so this script's job is to
   (1) warn the person and sign out on time, even on a screen that makes no requests,
   (2) keep the server session alive while the person is genuinely typing or tapping,
   (3) re-check the moment a phone app returns from the background, when timers were frozen. */
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="idle-timeout"]');
  if (!meta) return;
  var LIMIT = (parseInt(meta.content, 10) || 1800) * 1000;
  var WARN = (parseInt((document.querySelector('meta[name="idle-warning"]') || {}).content, 10) || 60) * 1000;
  var KEY = "mm_last_activity", PING_EVERY = 4 * 60 * 1000;
  var last = Date.now(), lastWrite = 0, lastPing = Date.now(), warned = false, ending = false, box = null, tick = null;

  function lsGet() { try { return parseInt(localStorage.getItem(KEY) || "0", 10) || 0; } catch (e) { return 0; } }
  function lsSet(v) { try { localStorage.setItem(KEY, String(v)); } catch (e) { /* private mode: fine */ } }
  function csrf() { return (document.querySelector('meta[name="csrf-token"]') || {}).content || ""; }

  function ping() {
    lastPing = Date.now();
    return fetch("/session/keepalive", { method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch", Accept: "application/json" } })
      .then(function (r) { if (r.status === 401 || r.redirected) signOut(); return r; }).catch(function () { /* offline: server will enforce */ });
  }

  function activity() {
    if (ending) return;
    var now = Date.now();
    last = now;
    if (warned) return;                       // once the warning shows, only the button keeps the session
    if (now - lastWrite > 5000) { lastWrite = now; lsSet(now); }   // shared with other tabs
    if (now - lastPing > PING_EVERY) ping();  // typing a long form must not let the server session lapse
  }

  function signOut() {
    if (ending) return;
    ending = true;
    clearInterval(tick);
    var done = function () { window.location.replace("/login?idle=1"); };
    try {
      fetch("/logout", { method: "POST", credentials: "same-origin", redirect: "manual",
        headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch" } }).then(done, done);
      setTimeout(done, 3000);
    } catch (e) { done(); }
  }

  function showWarning(secs) {
    if (!box) {
      box = document.createElement("div");
      box.className = "idle-warn";
      box.setAttribute("role", "alertdialog");
      box.setAttribute("aria-live", "assertive");
      box.innerHTML = '<div class="idle-card"><b>Still there?</b><p>For your security you will be signed out in <span data-idle-secs></span> seconds because there has been no activity.</p>' +
        '<div class="idle-actions"><button type="button" class="btn btn-primary" data-idle-stay>Stay signed in</button><button type="button" class="btn" data-idle-out>Sign out now</button></div></div>';
      document.body.appendChild(box);
      box.querySelector("[data-idle-stay]").addEventListener("click", stay);
      box.querySelector("[data-idle-out]").addEventListener("click", signOut);
    }
    box.querySelector("[data-idle-secs]").textContent = String(Math.max(0, secs));
    box.classList.add("show");
  }
  function stay() {
    warned = false; last = Date.now(); lastWrite = last; lsSet(last);
    if (box) box.classList.remove("show");
    ping();
  }

  function check() {
    if (ending) return;
    var newest = Math.max(last, lsGet());      // another tab may have seen activity
    var idle = Date.now() - newest;
    if (idle >= LIMIT) { signOut(); return; }
    if (idle >= LIMIT - WARN) { warned = true; showWarning(Math.ceil((LIMIT - idle) / 1000)); }
    else if (warned) { warned = false; if (box) box.classList.remove("show"); }   // activity in another tab
  }

  ["pointerdown", "keydown", "touchstart", "scroll", "wheel", "input"].forEach(function (ev) {
    window.addEventListener(ev, activity, { passive: true, capture: true });
  });
  window.addEventListener("mousemove", function () { if (Date.now() - last > 3000) activity(); }, { passive: true });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") check(); });
  window.addEventListener("pageshow", check);
  window.addEventListener("focus", check);
  window.addEventListener("storage", function (e) { if (e.key === KEY && !warned) last = Math.max(last, parseInt(e.newValue || "0", 10) || 0); });

  lsSet(last);
  tick = setInterval(check, 1000);
})();
