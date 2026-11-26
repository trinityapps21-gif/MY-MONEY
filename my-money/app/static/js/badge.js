/* MY MONEY - the red number on the app icon (Badging API).
   Signed-in pages set it to the unread count; signed-out pages and logging out clear it. Browsers that cannot show an icon badge are ignored.
   Pop-ups that arrive while the app is closed set the number from the service worker (see sw.js). */
(function () {
  "use strict";
  var MM = (window.MM = window.MM || {});
  var ok = "setAppBadge" in navigator && "clearAppBadge" in navigator;
  MM.badge = function (n) {
    if (!ok) return;
    n = parseInt(n, 10) || 0;
    try { (n > 0 ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(function () {}); } catch (e) { /* not allowed here */ }
  };
  var m = document.querySelector('meta[name="mm-unread"]');
  MM.badge(m ? m.content : 0);
  // coming back to the app after reading elsewhere: show the true number again
  document.addEventListener("visibilitychange", function () {
    var el = document.getElementById("bell-count");
    if (document.visibilityState === "visible" && el) MM.badge(el.classList.contains("hide") ? 0 : parseInt(el.textContent, 10) || 0);
  });
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f && /\/logout\/?$/.test(f.getAttribute && f.getAttribute("action") || "")) MM.badge(0);
  }, true);
})();
