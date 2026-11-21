/* MY MONEY - share a receipt. Opens a small sheet with: send the PDF itself on WhatsApp (phones that allow sharing
   files), send a link on WhatsApp (works everywhere), copy the link, or download the PDF.
   The link and the PDF are fetched when the sheet opens, so every button works from one tap (browsers only allow
   sharing right after a tap). */
(function () {
  "use strict";
  var sheet = null, opener = null, onKey = null;
  var WA = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>';

  function csrf() { return (document.querySelector('meta[name="csrf-token"]') || {}).content || ""; }
  function toast(m, k) { if (window.MM && MM.toast) MM.toast(m, k || "error"); else alert(m); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function close() {
    if (!sheet) return;
    document.removeEventListener("keydown", onKey);
    sheet.remove(); sheet = null;
    document.documentElement.classList.remove("rc-lock");
    if (opener && opener.focus) { try { opener.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
  }

  function open(btn) {
    close(); opener = btn;
    var number = btn.dataset.rcptNumber || "", pdfUrl = btn.dataset.rcptPdf, shareUrl = btn.dataset.rcptShare;
    var state = { link: null, file: null, abort: false };
    sheet = document.createElement("div");
    sheet.className = "rc-sheet";
    sheet.innerHTML = '<div class="rc-card" role="dialog" aria-modal="true" aria-labelledby="rc-t">' +
      '<button type="button" class="rc-x" aria-label="Close">&times;</button>' +
      '<h3 id="rc-t">Share receipt' + (number ? ' ' + esc(number) : '') + '</h3>' +
      '<p class="rc-sub" data-sub>Preparing the receipt&hellip;</p>' +
      '<div class="rc-actions">' +
        '<button type="button" class="btn btn-wa rc-btn hide" data-act="file" disabled>' + WA + '<span>Send PDF on WhatsApp</span></button>' +
        '<a class="btn btn-wa rc-btn disabled" data-act="link" target="_blank" rel="noopener" aria-disabled="true">' + WA + '<span>Send link on WhatsApp</span></a>' +
        '<button type="button" class="btn rc-btn" data-act="copy" disabled><i data-lucide="copy"></i><span>Copy link</span></button>' +
        '<a class="btn rc-btn" data-act="dl" href="' + esc(pdfUrl) + (pdfUrl.indexOf("?") < 0 ? "?" : "&") + 'download=1"><i data-lucide="file-down"></i><span>Download PDF</span></a>' +
      '</div><p class="rc-note" data-note></p></div>';
    document.body.appendChild(sheet);
    document.documentElement.classList.add("rc-lock");
    if (window.MM && MM.icons) MM.icons();
    var $ = function (s) { return sheet.querySelector(s); };
    var fileBtn = $('[data-act="file"]'), linkBtn = $('[data-act="link"]'), copyBtn = $('[data-act="copy"]'), sub = $("[data-sub]");

    onKey = function (e) { if (e.key === "Escape") { e.preventDefault(); close(); } };
    document.addEventListener("keydown", onKey);
    sheet.addEventListener("click", function (e) { if (e.target === sheet || e.target.closest(".rc-x")) close(); });
    $(".rc-x").focus({ preventScroll: true });

    // 1) the shareable link (needed by "send link" and "copy link")
    fetch(shareUrl, { method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "fetch", Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("share"); return r.json(); })
      .then(function (d) {
        if (!sheet) return;
        state.link = d;
        sub.textContent = d.title;
        linkBtn.href = d.has_phone ? d.whatsapp : d.whatsapp_choose;
        linkBtn.classList.remove("disabled"); linkBtn.removeAttribute("aria-disabled");
        copyBtn.disabled = false;
        $("[data-note]").textContent = "The link opens the PDF and works for " + d.days + " days. " +
          (d.has_phone ? "WhatsApp opens a chat with this person." : "WhatsApp lets you choose who to send it to.");
        // 2) the PDF itself, only where this phone can share files
        if (navigator.canShare && navigator.share) {
          fileBtn.classList.remove("hide");
          fetch(pdfUrl, { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error("pdf"); return r.blob(); })
            .then(function (blob) {
              if (!sheet) return;
              var f = new File([blob], "receipt_" + (d.number || "receipt") + ".pdf", { type: "application/pdf" });
              if (navigator.canShare({ files: [f] })) { state.file = f; fileBtn.disabled = false; }
              else fileBtn.classList.add("hide");
            }).catch(function () { fileBtn.classList.add("hide"); });
        }
      })
      .catch(function () { sub.textContent = "Could not prepare the share link. You can still download the PDF."; });

    fileBtn.addEventListener("click", function () {
      if (!state.file || !state.link) return;
      var caption = state.link.text.split("\n\nView or download")[0];
      navigator.share({ files: [state.file], title: state.link.title, text: caption })
        .then(close)
        .catch(function (e) { if (e && e.name !== "AbortError") toast("Could not open the share sheet. Use 'Send link on WhatsApp' instead.", "info"); });
    });
    copyBtn.addEventListener("click", function () {
      if (!state.link) return;
      var done = function () { toast("Link copied", "success"); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(state.link.url).then(done, function () { window.prompt("Copy this link", state.link.url); });
      else window.prompt("Copy this link", state.link.url);
    });
    linkBtn.addEventListener("click", function (e) { if (linkBtn.classList.contains("disabled")) e.preventDefault(); else setTimeout(close, 300); });
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-rcpt-share]");
    if (!b) return;
    e.preventDefault();
    open(b);
  });
  window.addEventListener("pagehide", close);
})();
