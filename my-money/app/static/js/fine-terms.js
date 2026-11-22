/* Group rules: highlight, as the officials type, that changing the late-fine terms only affects loans created afterwards.
   The amber notice appears the moment any fine field differs from what is saved, and goes away again if they change it back. */
(function () {
  "use strict";
  var form = document.querySelector("[data-fine-form]");
  var notice = document.getElementById("fine-change-alert");
  if (!form || !notice) return;
  var fields = ["fine_grace_days", "fine_period_days", "fine_type", "fine_rate", "fine_fixed", "fine_capped"];

  function read(name) {
    var el = form.elements[name];
    if (!el) return "";
    return el.type === "checkbox" ? (el.checked ? "1" : "0") : String(el.value).trim();
  }

  var saved = {};
  fields.forEach(function (n) { saved[n] = read(n); });

  function refresh() {
    var changed = fields.some(function (n) { return read(n) !== saved[n]; });
    notice.hidden = !changed;
  }

  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  refresh();
})();
