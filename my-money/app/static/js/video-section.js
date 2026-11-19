/* MY MONEY - landing page video section. Plays muted while visible; visitors can play/pause, turn sound on, seek, or go full screen. */
(function () {
  "use strict";
  var frame = document.getElementById("vsFrame"), v = document.getElementById("vsVideo");
  if (!frame || !v) return;
  var playBtn = document.getElementById("vsPlay"), soundBtn = document.getElementById("vsSound"), fullBtn = document.getElementById("vsFull");
  var track = document.getElementById("vsTrack"), fill = document.getElementById("vsFill"), label = soundBtn.querySelector("span");
  var saveData = navigator.connection && navigator.connection.saveData;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var userPaused = false, visible = false;

  v.muted = true; v.defaultMuted = true;
  frame.classList.add("is-paused");

  function tryPlay() { var p = v.play(); if (p && p.catch) p.catch(function () { frame.classList.add("is-paused"); }); }
  function sync() { frame.classList.toggle("is-paused", v.paused); playBtn.setAttribute("aria-label", v.paused ? "Play video" : "Pause video"); }
  v.addEventListener("play", sync); v.addEventListener("pause", sync); v.addEventListener("playing", sync);

  /* autoplay (muted) only while the section is on screen */
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      visible = entries[0].isIntersecting;
      if (visible && !userPaused && !saveData && !reduced) tryPlay();
      if (!visible && !document.fullscreenElement) v.pause();
    }, { threshold: 0.45 }).observe(frame);
  }
  document.addEventListener("visibilitychange", function () { if (document.hidden) v.pause(); else if (visible && !userPaused && !saveData && !reduced) tryPlay(); });

  function toggle() { if (v.paused) { userPaused = false; tryPlay(); } else { userPaused = true; v.pause(); } }
  playBtn.addEventListener("click", toggle);
  v.addEventListener("click", toggle);

  soundBtn.addEventListener("click", function () {
    v.muted = !v.muted;
    soundBtn.setAttribute("aria-pressed", v.muted ? "false" : "true");
    soundBtn.setAttribute("aria-label", v.muted ? "Turn sound on" : "Turn sound off");
    if (label) label.textContent = v.muted ? "Tap for sound" : "Sound on";
    if (!v.muted) { userPaused = false; if (v.paused) tryPlay(); }
  });

  fullBtn.addEventListener("click", function () {
    var el = frame;
    if (document.fullscreenElement || document.webkitFullscreenElement) { (document.exitFullscreen || document.webkitExitFullscreen).call(document); return; }
    if (el.requestFullscreen) el.requestFullscreen().catch(function () {});
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();   // iPhone Safari
  });

  v.addEventListener("timeupdate", function () {
    var pct = v.duration ? (v.currentTime / v.duration) * 100 : 0;
    fill.style.width = pct + "%"; track.setAttribute("aria-valuenow", String(Math.round(pct)));
  });
  track.addEventListener("click", function (e) {
    var r = track.getBoundingClientRect();
    if (v.duration) v.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * v.duration;
  });
})();
