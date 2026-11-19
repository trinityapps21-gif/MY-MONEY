/* MY MONEY - fingerprint / face sign-in for the installed phone app (WebAuthn). */
(function () {
  "use strict";
  var doc = document;
  function csrf() { return (doc.querySelector('meta[name="csrf-token"]') || {}).content || ""; }
  function installedApp() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  }
  function installedOnly() { return ((doc.querySelector('meta[name="biometric-installed-only"]') || {}).content || "1") === "1"; }
  function toast(m, k) { if (window.MM && MM.toast) MM.toast(m, k || "error"); else alert(m); }

  function b64uToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/"); while (s.length % 4) s += "=";
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }
  function bufToB64u(buf) {
    var b = new Uint8Array(buf), s = "";
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "fetch", Accept: "application/json" },
      body: JSON.stringify(body || {}) }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) { var e = new Error(d.error || "Something went wrong. Please try again."); e.data = d; throw e; }
          return d;
        });
      });
  }

  function supported() {
    if (!window.PublicKeyCredential || !window.isSecureContext) return Promise.resolve(false);
    if (installedOnly() && !installedApp()) return Promise.resolve(false);
    if (!PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable) return Promise.resolve(false);
    return PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().catch(function () { return false; });
  }

  function creationOptions(o) {
    o.challenge = b64uToBuf(o.challenge); o.user.id = b64uToBuf(o.user.id);
    (o.excludeCredentials || []).forEach(function (c) { c.id = b64uToBuf(c.id); });
    return o;
  }
  function requestOptions(o) {
    o.challenge = b64uToBuf(o.challenge);
    (o.allowCredentials || []).forEach(function (c) { c.id = b64uToBuf(c.id); });
    return o;
  }
  function common(c) {
    return { id: c.id, rawId: bufToB64u(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
      authenticatorAttachment: c.authenticatorAttachment || undefined };
  }

  /* ---------- sign in ---------- */
  function setupLogin(btn) {
    var form = btn.closest("form");
    btn.classList.remove("hide");
    btn.addEventListener("click", function () {
      var tok = form.querySelector('[name="cf-turnstile-response"]');
      var hasWidget = !!form.querySelector(".cf-turnstile");
      if (hasWidget && !(tok && tok.value)) { toast("Please complete the security check first."); return; }
      btn.classList.add("loading"); btn.disabled = true;
      post("/login/biometric/options", { "cf-turnstile-response": tok ? tok.value : "" })
        .then(function (opts) { return navigator.credentials.get({ publicKey: requestOptions(opts) }); })
        .then(function (c) {
          var r = c.response, cred = common(c);
          cred.response = { clientDataJSON: bufToB64u(r.clientDataJSON), authenticatorData: bufToB64u(r.authenticatorData),
            signature: bufToB64u(r.signature), userHandle: r.userHandle ? bufToB64u(r.userHandle) : undefined };
          return post("/login/biometric/verify", { credential: cred, next: btn.dataset.next || "" });
        })
        .then(function (d) { window.location.replace(d.redirect || "/"); })
        .catch(function (e) {
          btn.classList.remove("loading"); btn.disabled = false;
          if (window.turnstile) { try { window.turnstile.reset(); } catch (x) { /* ignore */ } }   // each token works once
          if (e && e.name === "NotAllowedError") toast("Biometric sign-in was cancelled. You can use your password instead.", "info");
          else toast((e && e.message) || "Biometric sign-in did not work. Use your password instead.");
        });
    });
  }

  /* ---------- enrol (Profile page) ---------- */
  function setupEnrol(card) {
    card.classList.remove("hide");
    var form = doc.getElementById("bio-enable"), pw = doc.getElementById("bio-pw");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = form.querySelector("button"); btn.disabled = true;
      var ua = navigator.userAgent || "", label = /iPhone|iPad/.test(ua) ? "iPhone / iPad" : /Android/.test(ua) ? "Android phone" : "This device";
      post("/account/biometric/register/options", { current_password: pw.value })
        .then(function (opts) { return navigator.credentials.create({ publicKey: creationOptions(opts) }); })
        .then(function (c) {
          var r = c.response, cred = common(c);
          cred.response = { clientDataJSON: bufToB64u(r.clientDataJSON), attestationObject: bufToB64u(r.attestationObject),
            transports: r.getTransports ? r.getTransports() : undefined };
          return post("/account/biometric/register/verify", { credential: cred, label: label });
        })
        .then(function () { window.location.reload(); })
        .catch(function (e) {
          btn.disabled = false; pw.value = "";
          if (e && e.name === "NotAllowedError") toast("Setup was cancelled.", "info");
          else if (e && e.name === "InvalidStateError") toast("This device is already enabled.", "info");
          else toast((e && e.message) || "Could not turn on biometric sign-in.");
        });
    });
  }

  doc.addEventListener("DOMContentLoaded", function () {
    var login = doc.getElementById("bio-login"), card = doc.getElementById("bio-card");
    if (!login && !card) return;
    supported().then(function (ok) { if (!ok) return; if (login) setupLogin(login); if (card) setupEnrol(card); });
  });
})();
