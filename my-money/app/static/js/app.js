/* MY MONEY - shared UI behaviour. Developed and Powered by Emtrixz Technology Company. */
(function () {
  "use strict";
  const MM = (window.MM = window.MM || {});
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  MM.csrf = csrf;
  MM.icons = () => { if (window.lucide) window.lucide.createIcons(); };

  /* ---------- toasts ---------- */
  MM.toast = function (message, kind) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast " + (kind || "info");
    t.setAttribute("role", "status");
    const span = document.createElement("span");
    span.textContent = message;
    const x = document.createElement("button");
    x.className = "x"; x.type = "button"; x.setAttribute("aria-label", "Dismiss"); x.textContent = "×";
    x.onclick = () => t.remove();
    t.append(span, x); box.appendChild(t);
    setTimeout(() => t.remove(), kind === "error" ? 8000 : 5000);
  };

  /* ---------- fetch helper (CSRF + JSON errors) ---------- */
  MM.post = async function (url, body) {
    const opts = { method: "POST", headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch", Accept: "application/json" } };
    if (body instanceof FormData) opts.body = body;
    else if (body) { opts.body = new URLSearchParams(body); }
    let res;
    try { res = await fetch(url, opts); } catch (e) { throw new Error("Network problem. Check your connection and try again."); }
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("json")) data = await res.json();
    if (!res.ok) throw new Error((data && data.error) || (res.status === 413 ? "The upload is too large." : res.status === 403 ? "You do not have permission to do that." : "Something went wrong. Please try again."));
    return data;
  };
  MM.get = async function (url) {
    const res = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
    if (!res.ok) throw new Error("Could not load. Please try again.");
    return res;
  };

  /* ---------- confirmation / reason dialogs ---------- */
  function dialog(opts) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.innerHTML = '<form method="dialog" class="dlg"><h3></h3><p class="muted small msg"></p><div class="reason-wrap hide"><label>Reason</label><textarea name="reason" rows="3" maxlength="300"></textarea></div><div class="actions"><button value="cancel" class="btn" type="submit" formnovalidate>Cancel</button><button value="ok" class="btn btn-primary ok" type="submit"></button></div></form>';
      d.querySelector("h3").textContent = opts.title || "Please confirm";
      d.querySelector(".msg").textContent = opts.message || "";
      const ok = d.querySelector(".ok"); ok.textContent = opts.confirm || "Confirm";
      if (opts.danger) ok.className = "btn btn-danger ok";
      const ta = d.querySelector("textarea");
      if (opts.reason) { d.querySelector(".reason-wrap").classList.remove("hide"); if (opts.reasonRequired) ta.required = true; ta.placeholder = opts.reasonHint || ""; }
      document.body.appendChild(d);
      d.addEventListener("close", () => { const v = d.returnValue === "ok" ? { ok: true, reason: ta.value.trim() } : { ok: false }; d.remove(); resolve(v); });
      d.showModal();
      if (opts.reason) ta.focus(); else ok.focus();
    });
  }
  MM.confirm = dialog;

  document.addEventListener("submit", async function (e) {
    const f = e.target;
    if (!(f instanceof HTMLFormElement) || f.dataset.confirmed === "1") return;
    const msg = f.dataset.confirm;
    if (msg === undefined && !f.dataset.reason) return;
    e.preventDefault();
    const res = await dialog({ title: f.dataset.title, message: msg, confirm: f.dataset.confirmLabel, danger: f.dataset.danger === "1",
      reason: !!f.dataset.reason, reasonRequired: f.dataset.reason === "required", reasonHint: f.dataset.reasonHint });
    if (!res.ok) return;
    if (f.dataset.reason) {
      let h = f.querySelector('input[name="reason"]');
      if (!h) { h = document.createElement("input"); h.type = "hidden"; h.name = "reason"; f.appendChild(h); }
      h.value = res.reason;
    }
    f.dataset.confirmed = "1";
    if (e.submitter) { f.requestSubmit(e.submitter); } else { f.submit(); }
  }, true);

  /* ---------- loading state + double-submit guard ---------- */
  document.addEventListener("submit", function (e) {
    const f = e.target;
    if (e.defaultPrevented || !(f instanceof HTMLFormElement) || f.hasAttribute("data-ajax")) return;
    if (f.dataset.submitting === "1") { e.preventDefault(); return; }
    f.dataset.submitting = "1";
    f.querySelectorAll('button[type="submit"],button:not([type])').forEach((b) => b.classList.add("loading"));
    setTimeout(() => { f.dataset.submitting = "0"; f.querySelectorAll(".loading").forEach((b) => b.classList.remove("loading")); }, 15000);
  });
  window.addEventListener("pageshow", () => document.querySelectorAll("form").forEach((f) => { f.dataset.submitting = "0"; f.querySelectorAll(".loading").forEach((b) => b.classList.remove("loading")); }));

  /* ---------- menus, drawer ---------- */
  document.addEventListener("click", function (e) {
    const trig = e.target.closest("[data-menu]");
    const open = document.querySelectorAll(".menu.open");
    if (trig) {
      const m = document.getElementById(trig.dataset.menu);
      const was = m && m.classList.contains("open");
      open.forEach((o) => o.classList.remove("open"));
      if (m && !was) { m.classList.add("open"); if (m.dataset.onopen && MM[m.dataset.onopen]) MM[m.dataset.onopen](m); }
      e.preventDefault(); return;
    }
    if (!e.target.closest(".menu")) open.forEach((o) => o.classList.remove("open"));
    const d = e.target.closest("[data-drawer]");
    if (d) { document.querySelector(".sidebar").classList.toggle("open"); document.querySelector(".scrim").classList.toggle("open"); }
    if (e.target.classList.contains("scrim")) { document.querySelector(".sidebar").classList.remove("open"); e.target.classList.remove("open"); }
    const cp = e.target.closest("[data-copy]");
    if (cp) {
      const text = cp.dataset.copy;
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => MM.toast("Link copied to clipboard.", "success"),
        () => { const i = document.createElement("input"); i.value = text; document.body.appendChild(i); i.select(); try { document.execCommand("copy"); MM.toast("Link copied to clipboard.", "success"); } catch (_) { MM.toast("Copy failed. Select and copy the link manually.", "error"); } i.remove(); });
    }
    const pr = e.target.closest("[data-print]"); if (pr) window.print();
  });

  /* ---------- notification bell ---------- */
  MM.loadBell = async function (menu) {
    const list = menu.querySelector(".nlist");
    list.innerHTML = '<div class="spinner"></div>';
    try {
      const data = await (await MM.get(menu.dataset.url)).json();
      list.innerHTML = "";
      if (!data.items.length) { list.innerHTML = '<div class="empty" style="padding:22px"><p class="muted small mb0">You are all caught up.</p></div>'; }
      data.items.forEach((n) => { const a = document.createElement("a"); a.className = "nitem" + (n.read ? "" : " unread"); a.href = n.url;
        const b = document.createElement("b"); b.textContent = n.title; const s = document.createElement("span"); s.textContent = n.body || ""; a.append(b, s); list.appendChild(a); });
      setBell(data.unread);
    } catch (e) { list.innerHTML = '<div class="empty small">Could not load notifications.</div>'; }
  };
  function setBell(n) { const b = document.getElementById("bell-count"); if (!b) return; b.textContent = n > 99 ? "99+" : n; b.classList.toggle("hide", !n); }
  document.addEventListener("click", async function (e) {
    const b = e.target.closest("[data-read-all]"); if (!b) return;
    try { await MM.post(b.dataset.readAll); setBell(0); document.querySelectorAll(".nitem.unread").forEach((x) => x.classList.remove("unread")); } catch (err) { MM.toast(err.message, "error"); }
  });

  /* ---------- charts ---------- */
  const PALETTE = ["#1F4FB8", "#101C36", "#16834A", "#D99519", "#6FA0E0", "#D83A45"];
  MM.PALETTE = PALETTE;
  MM.moneyFmt = (cur, dec) => (v) => { const n = Number(v) / Math.pow(10, dec || 0); return (cur ? cur + " " : "") + n.toLocaleString(undefined, { maximumFractionDigits: dec || 0, minimumFractionDigits: dec || 0 }); };
  const compact = (v, dec) => { const n = Number(v) / Math.pow(10, dec || 0), a = Math.abs(n); return a >= 1e9 ? +(n / 1e9).toFixed(1) + "B" : a >= 1e6 ? +(n / 1e6).toFixed(1) + "M" : a >= 1e3 ? +(n / 1e3).toFixed(1) + "K" : n.toLocaleString(); };
  MM.chart = function (canvas, cfg) {
    if (!window.Chart || !canvas) return null;
    const money = cfg.money !== false, cur = cfg.currency || "", dec = cfg.decimals || 0;
    const datasets = cfg.datasets.map((d, i) => {
      const color = d.color || PALETTE[i % PALETTE.length];
      const base = { label: d.label, data: d.data, borderColor: color, backgroundColor: cfg.type === "line" ? color + "22" : (cfg.type === "doughnut" ? d.colors || PALETTE : color), borderWidth: cfg.type === "bar" ? 0 : 2 };
      if (cfg.type === "line") Object.assign(base, { fill: d.fill !== false, tension: .35, pointRadius: 3, pointBackgroundColor: "#fff", pointBorderWidth: 2 });
      if (cfg.type === "bar") Object.assign(base, { borderRadius: 4, maxBarThickness: 34 });
      return base;
    });
    return new Chart(canvas, { type: cfg.type, data: { labels: cfg.labels, datasets },
      options: { responsive: true, maintainAspectRatio: false, cutout: cfg.type === "doughnut" ? "68%" : undefined, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: cfg.legend !== undefined ? cfg.legend : datasets.length > 1 || cfg.type === "doughnut", position: "bottom", labels: { boxWidth: 10, usePointStyle: true, font: { size: 11 } } },
          tooltip: { callbacks: { label: (c) => (c.dataset.label ? c.dataset.label + ": " : "") + (cfg.type === "doughnut" ? c.formattedValue + (cfg.suffix || "") : (money ? MM.moneyFmt(cur, dec)(c.parsed.y !== undefined ? c.parsed.y : c.parsed) : c.formattedValue)) } } },
        scales: cfg.type === "doughnut" ? {} : { x: { grid: { display: false }, ticks: { font: { size: 11 } } }, y: { beginAtZero: true, grid: { color: "#EEF1F5" }, border: { display: false }, ticks: { font: { size: 11 }, callback: (v) => (money ? compact(v, dec) : v) } } } } });
  };
  function initCharts() {
    document.querySelectorAll("canvas[data-chart]").forEach((c) => {
      const src = document.getElementById(c.dataset.chart); if (!src || c.dataset.ready) return;
      try { MM.chart(c, JSON.parse(src.textContent)); c.dataset.ready = "1"; } catch (e) { /* ignore bad config */ }
    });
  }

  /* ---------- file-input previews (logo / avatar) ---------- */
  document.addEventListener("change", function (e) {
    const i = e.target; if (!(i instanceof HTMLInputElement) || !i.dataset.preview) return;
    const img = document.getElementById(i.dataset.preview); const f = i.files && i.files[0]; if (!img || !f) return;
    if (!/^image\/(png|jpe?g|webp)$/.test(f.type)) { MM.toast("Choose a PNG, JPG or WebP image.", "error"); i.value = ""; return; }
    if (f.size > (+i.dataset.max || 2097152)) { MM.toast("That image is too large.", "error"); i.value = ""; return; }
    img.src = URL.createObjectURL(f); img.classList.remove("hide"); const ph = document.getElementById(i.dataset.preview + "-ph"); if (ph) ph.classList.add("hide");
  });


  /* ---------- page-change progress bar: instant feedback while the next page loads ---------- */
  (function () {
    let bar = null, t = null;
    function start() {
      if (!bar) { bar = document.createElement("div"); bar.className = "nav-progress"; document.body.appendChild(bar); }
      clearTimeout(t); bar.style.transition = ""; bar.style.width = "0"; void bar.offsetWidth; bar.classList.add("run");
      t = setTimeout(stop, 10000);
    }
    function stop() { if (!bar) return; bar.classList.remove("run"); bar.style.width = "0"; }
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = e.target.closest("a[href]");
      if (!a || a.target === "_blank" || a.hasAttribute("download") || a.hasAttribute("data-menu") || a.hasAttribute("data-no-progress")) return;
      const u = new URL(a.href, location.href);
      if (u.origin !== location.origin || (u.pathname === location.pathname && u.search === location.search) || /^(\/static\/|\/files\/)/.test(u.pathname)) return;
      start();
    });
    document.addEventListener("submit", function (e) { if (!e.defaultPrevented && !e.target.hasAttribute("data-ajax")) start(); });
    window.addEventListener("pageshow", stop);
  })();

  document.addEventListener("DOMContentLoaded", function () {
    MM.icons();
    initCharts();
    document.querySelectorAll("[data-flash]").forEach((n) => MM.toast(n.dataset.flash, n.dataset.kind));
    document.querySelectorAll("input[type=number][data-nowheel]").forEach((n) => n.addEventListener("wheel", (e) => e.preventDefault()));
  });
  window.addEventListener("load", initCharts);
})();
