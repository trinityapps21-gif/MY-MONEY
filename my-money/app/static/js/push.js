/* MY MONEY - pop-up notifications (Web Push) for signed-in users on Android, iPhone (installed app) and laptops.
   - Once permission is granted, this device is re-registered silently on every visit, so alerts keep working after logins.
   - Logging out removes this device from the account so the next person using it never sees someone else's alerts. */
(function () {
  "use strict";
  var KEY = "mm_push_dismissed", WAIT_DAYS = 14;
  var token = function () { return (document.querySelector('meta[name="csrf-token"]') || {}).content || ""; };
  function store(op, k, v) { try { return op === "get" ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { return null; } }
  var ua = navigator.userAgent || "";
  var isIOS = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window &&
                  (location.protocol === "https:" || location.hostname === "localhost");
  // iPhones only allow pop-ups for an app that was added to the Home Screen.
  var needsInstall = isIOS && !standalone;

  function api(path, body) {
    return fetch("/notifications/push/" + path, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": token(), "X-Requested-With": "fetch", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "failed"); return d; }); });
  }
  function toKey(b64) {
    var p = b64.replace(/-/g, "+").replace(/_/g, "/"), raw = atob(p + "===".slice((p.length + 3) % 4)), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function same(a, b) {
    if (!a || !b || a.byteLength !== b.byteLength) return false;
    var x = new Uint8Array(a), y = new Uint8Array(b);
    for (var i = 0; i < x.length; i++) if (x[i] !== y[i]) return false;
    return true;
  }
  function ready() { return navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function () { return navigator.serviceWorker.ready; }); }

  /* subscribe (or refresh) this device and tell the server */
  function enable() {
    return ready().then(function (reg) {
      return api("config").then(function (cfg) {
        var want = toKey(cfg.key);
        return reg.pushManager.getSubscription().then(function (sub) {
          if (sub && sub.options && sub.options.applicationServerKey && !same(sub.options.applicationServerKey, want.buffer)) {
            return sub.unsubscribe().then(function () { return null; });         // the server key changed: subscribe again
          }
          return sub;
        }).then(function (sub) {
          return sub || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: want });
        });
      });
    }).then(function (sub) { return api("subscribe", sub.toJSON()).then(function () { return sub; }); });
  }
  function disable() {
    return ready().then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (sub) {
      if (!sub) return null;
      var ep = sub.endpoint;
      return api("unsubscribe", { endpoint: ep }).catch(function () {}).then(function () { return sub.unsubscribe(); });
    });
  }
  function current() { return ready().then(function (reg) { return reg.pushManager.getSubscription(); }); }

  /* ----- logout: detach this device from the account ----- */
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!supported || !f || !/\/logout\/?$/.test(f.getAttribute("action") || "")) return;
    if (!navigator.serviceWorker.controller || Notification.permission !== "granted") return;
    e.preventDefault();
    var done = function () { f.submit(); };
    var timer = setTimeout(done, 1500);
    current().then(function (sub) {
      if (!sub) return;
      return fetch("/notifications/push/unsubscribe", { method: "POST", credentials: "same-origin", keepalive: true,
        headers: { "Content-Type": "application/json", "X-CSRFToken": token() }, body: JSON.stringify({ endpoint: sub.endpoint }) });
    }).catch(function () {}).then(function () { clearTimeout(timer); done(); });
  }, true);

  if (!supported || !document.querySelector('meta[name="csrf-token"]')) { wireCard(); return; }
  var loggedIn = !!document.querySelector("[data-push-auth]");
  if (!loggedIn) return;

  /* ----- silent refresh when already allowed ----- */
  if (Notification.permission === "granted") enable().catch(function () {});

  /* ----- the settings card on the Notifications page ----- */
  function wireCard() {
    var card = document.querySelector("[data-push-card]");
    if (!card) return;
    var state = card.querySelector("[data-push-state]"), on = card.querySelector("[data-push-on]"),
        off = card.querySelector("[data-push-off]"), test = card.querySelector("[data-push-test]"),
        help = card.querySelector("[data-push-help]"), pill = card.querySelector("[data-push-pill]");
    function paint(kind, text, helpText) {
      state.textContent = text;
      pill.className = "pill " + (kind === "on" ? "green" : kind === "blocked" ? "red" : "");
      pill.textContent = kind === "on" ? "On" : kind === "blocked" ? "Blocked" : kind === "na" ? "Not available" : "Off";
      on.hidden = kind !== "off"; off.hidden = kind !== "on"; test.hidden = kind !== "on";
      help.textContent = helpText || ""; help.hidden = !helpText;
    }
    function refresh() {
      if (!supported) return paint("na", "This browser cannot show pop-up notifications.",
        "Use a recent Chrome, Edge, Firefox or Safari, and open the app over a secure (https) address.");
      if (needsInstall) return paint("na", "Install the app first to get pop-ups on iPhone.",
        "Tap the Share button, choose Add to Home Screen, open MY MONEY from your home screen, then come back to this page and turn notifications on.");
      if (Notification.permission === "denied") return paint("blocked", "Notifications are blocked for this site.",
        "Open your browser or phone settings for this site (or app), allow Notifications, then reload this page.");
      current().then(function (sub) {
        if (sub && Notification.permission === "granted") paint("on", "Pop-up notifications are on for this device.");
        else paint("off", "Get a pop-up on this device when something needs your attention.");
      }).catch(function () { paint("off", "Get a pop-up on this device when something needs your attention."); });
    }
    on.addEventListener("click", function () {
      on.disabled = true;
      Notification.requestPermission().then(function (p) {
        if (p !== "granted") return;
        return enable().then(function () { window.MM && MM.toast("Pop-up notifications are on.", "success"); return api("test", {}); });
      }).catch(function () { window.MM && MM.toast("Could not turn notifications on. Please try again.", "error"); })
        .then(function () { on.disabled = false; refresh(); });
    });
    off.addEventListener("click", function () {
      disable().then(function () { window.MM && MM.toast("Pop-up notifications are off for this device.", "success"); }).then(refresh);
    });
    test.addEventListener("click", function () {
      test.disabled = true;
      api("test", {}).then(function (d) {
        window.MM && MM.toast(d.sent ? "Test notification sent." : "No device could be reached. Turn notifications off and on again.", d.sent ? "success" : "error");
      }).catch(function () {}).then(function () { test.disabled = false; });
    });
    refresh();
  }
  wireCard();

  /* ----- a gentle one-time invitation ----- */
  if (supported && !needsInstall && Notification.permission === "default" && !document.querySelector("[data-push-card]")) {
    var t = parseInt(store("get", KEY) || "0", 10);
    if (!(t && Date.now() - t < WAIT_DAYS * 86400000)) {
      setTimeout(function () {
        if (document.querySelector(".pwa-banner.show") || Notification.permission !== "default") return;
        var b = document.createElement("div");
        b.className = "pwa-banner show"; b.setAttribute("role", "dialog"); b.setAttribute("aria-label", "Turn on notifications");
        b.innerHTML = '<img src="/static/img/icon-192.png" alt="" width="46" height="46"><div class="pwa-text"><b>Turn on notifications</b>' +
          '<span>Get a pop-up for repayments, approvals and overdue loans.</span></div><div class="pwa-actions">' +
          '<button type="button" class="go" data-go>Turn on</button><button type="button" data-no>Not now</button></div>';
        document.body.appendChild(b);
        b.addEventListener("click", function (e) {
          if (e.target.closest("[data-no]")) { store("set", KEY, String(Date.now())); b.remove(); }
          if (e.target.closest("[data-go]")) {
            b.remove();
            Notification.requestPermission().then(function (p) {
              if (p === "granted") return enable().then(function () { window.MM && MM.toast("Pop-up notifications are on.", "success"); });
              store("set", KEY, String(Date.now()));
            }).catch(function () {});
          }
        });
      }, 9000);
    }
  }
})();
