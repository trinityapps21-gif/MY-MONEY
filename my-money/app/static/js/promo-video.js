/* MY MONEY - landing page video advert. Plays muted and looping; visitors can turn sound on, watch full screen, or close it. */
(function () {
  "use strict";
  var KEY = "mm_promo_closed";
  var box = document.getElementById("promoVideo");
  if (!box) return;
  var mini = document.getElementById("pvMini"), dlg = document.getElementById("pvDialog"), full = document.getElementById("pvFull");
  var soundBtn = box.querySelector('[data-pv="sound"]'), label = soundBtn && soundBtn.querySelector("span");

  function store(op, v) { try { return op === "get" ? sessionStorage.getItem(KEY) : sessionStorage.setItem(KEY, v); } catch (e) { return null; } }
  if (store("get") === "1") { box.remove(); if (dlg) dlg.remove(); return; }

  var saveData = navigator.connection && navigator.connection.saveData;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function playMini() { var p = mini.play(); if (p && p.catch) p.catch(function () { /* autoplay blocked: poster stays, tapping opens the video */ }); }
  function setSound(on) {
    mini.muted = !on;
    soundBtn.setAttribute("aria-pressed", on ? "true" : "false");
    soundBtn.setAttribute("aria-label", on ? "Turn sound off" : "Turn sound on");
    if (label) label.textContent = on ? "Sound on" : "Tap for sound";
    if (on) playMini();
  }

  function reveal() {
    box.hidden = false;
    mini.muted = true; mini.defaultMuted = true;
    if (!saveData && !reduced) playMini();
  }
  /* Short desktop windows: the hero buttons sit where the player would be, so wait until the visitor scrolls past the hero. */
  function start() {
    var shortDesktop = window.innerWidth > 640 && window.innerHeight < 780;
    if (shortDesktop && window.scrollY < 320) {
      var onScroll = function () { if (window.scrollY > 320) { window.removeEventListener("scroll", onScroll); reveal(); } };
      window.addEventListener("scroll", onScroll, { passive: true });
    } else reveal();
  }
  if (document.readyState === "complete") setTimeout(start, 800);
  else window.addEventListener("load", function () { setTimeout(start, 800); });

  box.addEventListener("click", function (e) {
    var b = e.target.closest("[data-pv]"); if (!b) return;
    var act = b.getAttribute("data-pv");
    if (act === "sound") setSound(mini.muted);
    else if (act === "close") { mini.pause(); box.hidden = true; store("set", "1"); }
    else if (act === "expand") openFull();
  });

  function openFull() {
    if (!dlg || typeof dlg.showModal !== "function") { window.open(mini.currentSrc || mini.querySelector("source").src, "_blank", "noopener"); return; }
    var t = mini.currentTime; mini.pause();
    dlg.showModal();
    try { full.currentTime = t; } catch (e) {}
    full.muted = false;
    var p = full.play(); if (p && p.catch) p.catch(function () { full.muted = true; full.play(); });
  }
  function closeFull() { full.pause(); if (dlg.open) dlg.close(); }
  if (dlg) {
    dlg.addEventListener("close", function () { full.pause(); if (!box.hidden && !saveData && !reduced) { mini.muted = true; setSound(false); playMini(); } });
    dlg.addEventListener("click", function (e) { if (e.target === dlg || e.target.closest("[data-pv-close]")) closeFull(); });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { mini.pause(); }
    else if (!box.hidden && !(dlg && dlg.open) && !saveData && !reduced) playMini();
  });
})();
