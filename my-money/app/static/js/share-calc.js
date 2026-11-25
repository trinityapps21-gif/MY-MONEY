/* Add shares: live "amount -> shares" preview. The server repeats the same sum, so this is only a convenience. */
(function () {
  var form = document.querySelector("[data-share-form]");
  if (!form) return;
  var groupPrice = parseInt(form.dataset.price, 10) || 0;
  var dec = parseInt(form.dataset.decimals, 10) || 0;
  var cur = form.dataset.currency || "";
  var box = form.querySelector("[data-share-preview]");
  var amount = form.elements.amount, units = form.elements.units, price = form.elements.unit_price;
  var scale = Math.pow(10, dec);

  function minor(v) {
    v = String(v || "").replace(/[^0-9.]/g, "");
    return v ? Math.round(parseFloat(v) * scale) : 0;
  }
  function money(m) {
    return cur + " " + (m / scale).toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function mode() { return (form.querySelector("input[name=mode]:checked") || {}).value || "amount"; }

  function setMode() {
    var m = mode();
    form.querySelectorAll("[data-mode-tab]").forEach(function (t) { t.classList.toggle("on", t.dataset.modeTab === m); });
    form.querySelectorAll("[data-when]").forEach(function (f) { f.hidden = f.dataset.when !== m; });
    amount.required = m === "amount";
    units.required = m === "units";
    update();
  }
  function say(cls, html) { box.className = "alert " + cls + " small"; box.innerHTML = html; }

  function update() {
    var p = minor(price.value) || groupPrice;
    if (p <= 0) { say("warn", "Set a share price in Share settings first."); return; }
    if (mode() === "amount") {
      var paid = minor(amount.value);
      if (!paid) { say("info", "Price per share: <b>" + money(p) + "</b>. Type the amount paid to see the shares."); return; }
      var n = Math.floor(paid / p), left = paid - n * p;
      if (n < 1) { say("warn", "<b>" + money(paid) + "</b> is less than the price of one share (" + money(p) + ")."); return; }
      say(left ? "warn" : "success",
        "<b>" + n.toLocaleString() + " share" + (n === 1 ? "" : "s") + "</b> at " + money(p) + " = <b>" + money(n * p) + "</b>" +
        (left ? ". " + money(left) + " is left over (less than one share) and will not be recorded." : "."));
    } else {
      var u = parseInt(String(units.value).replace(/[^0-9]/g, ""), 10) || 0;
      if (!u) { say("info", "Price per share: <b>" + money(p) + "</b>. Type the number of shares."); return; }
      say("success", "<b>" + u.toLocaleString() + " share" + (u === 1 ? "" : "s") + "</b> x " + money(p) + " = <b>" + money(u * p) + "</b>.");
    }
  }
  form.addEventListener("change", function (e) { if (e.target.name === "mode") setMode(); });
  form.addEventListener("input", update);
  form.querySelectorAll("[data-mode-tab]").forEach(function (t) {
    t.addEventListener("click", function () { t.querySelector("input").checked = true; setMode(); });
  });
  setMode();
})();
