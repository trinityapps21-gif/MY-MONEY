/* Community Social Feed behaviour. All state is saved server-side (Turso); the page only reflects it. */
(function () {
  "use strict";
  const MM = window.MM;
  const root = document.getElementById("community");
  if (!root) return;
  const base = root.dataset.base; // /g/<id>/community
  const MAX_FILES = 6, MAX_BYTES = 5 * 1024 * 1024;

  const html = (s) => { const t = document.createElement("template"); t.innerHTML = s.trim(); return t.content.firstElementChild; };
  const refresh = (el) => { MM.icons(); return el; };
  const err = (e) => MM.toast(e.message || "Something went wrong.", "error");

  /* ---------- composer ---------- */
  const form = document.getElementById("composer");
  let files = [];
  if (form) {
    const input = form.querySelector('input[type=file]'), previews = form.querySelector(".previews"), text = form.querySelector("textarea");
    const render = () => {
      previews.innerHTML = "";
      files.forEach((f, i) => {
        const p = document.createElement("div"); p.className = "prev";
        if (f.type.startsWith("image/")) { const im = document.createElement("img"); im.src = URL.createObjectURL(f); im.alt = ""; p.appendChild(im); } else { p.textContent = f.name; }
        const x = document.createElement("button"); x.type = "button"; x.textContent = "×"; x.setAttribute("aria-label", "Remove"); x.onclick = () => { files.splice(i, 1); render(); };
        p.appendChild(x); previews.appendChild(p);
      });
    };
    input.addEventListener("change", () => {
      for (const f of input.files) {
        if (files.length >= MAX_FILES) { MM.toast("You can attach up to " + MAX_FILES + " files.", "error"); break; }
        if (f.size > MAX_BYTES) { MM.toast(f.name + " is larger than 5 MB.", "error"); continue; }
        files.push(f);
      }
      input.value = ""; render();
    });
    form.querySelector("[data-preview-post]").addEventListener("click", async () => {
      const body = text.value.trim();
      if (!body && !files.length) { MM.toast("Write something to preview.", "info"); return; }
      const d = document.createElement("dialog");
      d.innerHTML = '<div class="dlg"><h3>Post preview</h3><div class="post-head" style="margin:10px 0"><span class="avatar"></span><div><div class="who"></div><div class="when">Just now</div></div></div><div class="post-body"></div><div class="previews"></div><div class="actions"><button class="btn" value="close" type="button">Close</button></div></div>';
      d.querySelector(".avatar").textContent = form.dataset.initials; d.querySelector(".who").textContent = form.dataset.name; d.querySelector(".post-body").textContent = body;
      files.forEach((f) => { const p = document.createElement("div"); p.className = "prev"; if (f.type.startsWith("image/")) { const im = document.createElement("img"); im.src = URL.createObjectURL(f); p.appendChild(im); } else p.textContent = f.name; d.querySelector(".previews").appendChild(p); });
      d.querySelector("button").onclick = () => d.close(); d.addEventListener("close", () => d.remove()); document.body.appendChild(d); d.showModal();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("[type=submit]");
      if (!text.value.trim() && !files.length) { MM.toast("Write something or attach a photo first.", "error"); return; }
      const fd = new FormData(); fd.append("content", text.value); fd.append("category", form.querySelector("select").value);
      files.forEach((f) => fd.append("files", f, f.name));
      btn.classList.add("loading");
      try {
        const data = await MM.post(base + "/posts", fd);
        const el = refresh(html(data.html)); const list = document.getElementById("posts");
        const empty = document.getElementById("posts-empty"); if (empty) empty.remove();
        const firstUnpinned = [...list.children].find((c) => !c.classList.contains("pinned"));
        list.insertBefore(el, firstUnpinned || null);
        text.value = ""; files = []; render(); MM.toast("Post published.", "success");
      } catch (er) { err(er); } finally { btn.classList.remove("loading"); }
    });
  }

  /* ---------- load more posts ---------- */
  const more = document.getElementById("load-more");
  if (more) more.addEventListener("click", async () => {
    more.classList.add("loading");
    try {
      const res = await MM.get(base + "/posts?page=" + more.dataset.next + (more.dataset.query || ""));
      const frag = html("<div>" + (await res.text()) + "</div>");
      document.getElementById("posts").append(...frag.children);
      refresh(document.getElementById("posts"));
      if (res.headers.get("X-Has-Next") === "1") more.dataset.next = res.headers.get("X-Next-Page"); else more.remove();
    } catch (er) { err(er); } finally { more.classList.remove("loading"); }
  });

  /* ---------- delegated actions ---------- */
  const postOf = (el) => el.closest(".post");
  const setCounts = (p, c) => { const e = p.querySelector("[data-comment-count]"); if (e) e.textContent = c; };

  root.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-act]"); if (!t) return;
    const act = t.dataset.act, post = postOf(t), pid = post && post.dataset.id;
    try {
      if (act === "like") {
        const liked = t.classList.contains("liked");
        t.classList.toggle("liked", !liked); // optimistic
        try {
          const d = await MM.post(base + "/posts/" + pid + (liked ? "/unlike" : "/like"));
          t.classList.toggle("liked", d.liked); post.querySelector("[data-like-count]").textContent = d.count;
          post.querySelector("[data-like-wrap]").classList.toggle("hide", d.count === 0);
        } catch (er) { t.classList.toggle("liked", liked); throw er; }
      } else if (act === "likers") {
        const d = await (await MM.get(base + "/posts/" + pid + "/likes")).json();
        const dlg = document.createElement("dialog"); dlg.innerHTML = '<div class="dlg"><h3>Liked by</h3><div class="reaction-list"></div><div class="actions"><button class="btn" type="button">Close</button></div></div>';
        const list = dlg.querySelector(".reaction-list"); d.users.forEach((u) => { const r = document.createElement("div"); r.textContent = u.name; list.appendChild(r); });
        if (!d.users.length) list.textContent = "No likes yet.";
        dlg.querySelector("button").onclick = () => dlg.close(); dlg.addEventListener("close", () => dlg.remove()); document.body.appendChild(dlg); dlg.showModal();
      } else if (act === "toggle-comments") {
        const box = post.querySelector(".comments"); box.classList.toggle("hide");
        if (!box.classList.contains("hide") && !box.dataset.loaded) { await loadComments(post, 1); box.dataset.loaded = "1"; const i = box.querySelector(".comment-form input"); if (i) i.focus(); }
      } else if (act === "more-comments") {
        await loadComments(post, t.dataset.next, t);
      } else if (act === "share") {
        const r = await MM.confirm({ title: "Share with your group", message: "This shares the post to this group's community feed, crediting the original author.", confirm: "Share", reason: true, reasonHint: "Add a note (optional)" });
        if (!r.ok) return;
        const fd = new FormData(); fd.append("content", r.reason);
        const d = await MM.post(base + "/posts/" + pid + "/share", fd);
        document.getElementById("posts").prepend(refresh(html(d.html))); MM.toast("Post shared with your group.", "success");
      } else if (act === "delete-post") {
        const mod = t.dataset.mod === "1";
        const r = await MM.confirm({ title: mod ? "Remove this post?" : "Delete your post?", message: mod ? "The post and its photos will be removed. The author is notified and the action is logged." : "This cannot be undone.", confirm: mod ? "Remove post" : "Delete", danger: true, reason: mod, reasonHint: "Reason (shown to the author)" });
        if (!r.ok) return;
        await MM.post(base + "/posts/" + pid + "/delete", { reason: r.reason });
        if (root.dataset.page === "detail") { location.href = base + "/"; return; }
        post.remove(); MM.toast("Post removed.", "success");
      } else if (act === "pin") {
        const pin = t.dataset.pin === "1"; await MM.post(base + "/posts/" + pid + "/pin", { pin: pin ? "1" : "0" });
        MM.toast(pin ? "Post pinned." : "Post unpinned.", "success"); setTimeout(() => location.reload(), 500);
      } else if (act === "edit-post") {
        const box = post.querySelector(".post-body"); const cur = box.textContent;
        const ed = html('<div class="edit-box"><textarea></textarea><div class="flex gap1 mt1"><button class="btn btn-primary btn-sm" data-save>Save</button><button class="btn btn-sm" data-cancel>Cancel</button></div></div>');
        ed.querySelector("textarea").value = cur; box.classList.add("hide"); box.after(ed);
        ed.querySelector("[data-cancel]").onclick = () => { ed.remove(); box.classList.remove("hide"); };
        ed.querySelector("[data-save]").onclick = async () => {
          const fd = new FormData(); fd.append("content", ed.querySelector("textarea").value);
          fd.append("category", post.dataset.category);
          try { const d = await MM.post(base + "/posts/" + pid + "/edit", fd); const n = refresh(html(d.html)); post.replaceWith(n); MM.toast("Post updated.", "success"); } catch (er) { err(er); }
        };
      } else if (act === "report") {
        const kind = t.dataset.kind, id = t.dataset.id || pid;
        const dlg = document.createElement("dialog");
        dlg.innerHTML = '<form class="dlg" method="dialog"><h3>Report ' + kind + '</h3><p class="muted small">Group officials will review this. Reports are not shown to the author.</p><div class="field"><label>Reason</label><select name="reason" required><option value="">Select a reason</option><option>Spam</option><option>Inappropriate content</option><option>Harassment</option><option>Misleading information</option><option>Other</option></select></div><div class="field"><label>Additional details (optional)</label><textarea name="description" maxlength="500" rows="3"></textarea></div><div class="actions"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">Send report</button></div></form>';
        document.body.appendChild(dlg); dlg.showModal();
        dlg.addEventListener("close", async () => {
          if (dlg.returnValue === "ok") {
            try { const d = await MM.post(base + "/" + (kind === "post" ? "posts" : kind === "comment" ? "comments" : "replies") + "/" + id + "/report", { reason: dlg.querySelector("select").value, description: dlg.querySelector("textarea").value }); MM.toast(d.message, "success"); } catch (er) { err(er); }
          } dlg.remove();
        });
      } else if (act === "reply") {
        const c = t.closest(".comment"); const wrap = c.closest("[data-comment]") || c; let f = wrap.querySelector(":scope > .reply-form-wrap");
        if (f) { f.remove(); return; }
        f = html('<div class="reply-form-wrap" style="margin:6px 0 8px 38px"><form class="comment-form"><input type="text" placeholder="Write a reply..." maxlength="2000" required><button class="btn btn-primary btn-sm">Reply</button></form></div>');
        wrap.appendChild(f); const inp = f.querySelector("input"); if (t.dataset.who) inp.value = "@" + t.dataset.who + " "; inp.focus();
        f.querySelector("form").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          try { const d = await MM.post(base + "/comments/" + wrap.dataset.comment + "/replies", { text: inp.value, reply_to_user_id: t.dataset.user || "" });
            let list = wrap.querySelector(".replies"); if (!list) { list = document.createElement("div"); list.className = "replies"; wrap.appendChild(list); }
            list.appendChild(refresh(html(d.html))); setCounts(post, d.count); f.remove(); } catch (er) { err(er); }
        });
      } else if (act === "edit-c") {
        const bubble = t.closest("[data-item]").querySelector(".txt"); const cur = bubble.textContent;
        const kind = t.dataset.kind, id = t.dataset.id;
        const inp = document.createElement("textarea"); inp.value = cur; inp.rows = 2; bubble.replaceWith(inp); inp.focus();
        const done = async () => { try { await MM.post(base + "/" + (kind === "comment" ? "comments" : "replies") + "/" + id + "/edit", { text: inp.value }); const s = document.createElement("div"); s.className = "txt"; s.textContent = inp.value; inp.replaceWith(s); } catch (er) { err(er); const s = document.createElement("div"); s.className = "txt"; s.textContent = cur; inp.replaceWith(s); } };
        inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); done(); } if (ev.key === "Escape") { const s = document.createElement("div"); s.className = "txt"; s.textContent = cur; inp.replaceWith(s); } });
        inp.addEventListener("blur", done);
      } else if (act === "delete-c") {
        const kind = t.dataset.kind, id = t.dataset.id, mod = t.dataset.mod === "1";
        const r = await MM.confirm({ title: "Delete this " + kind + "?", message: mod ? "The author is notified and the removal is logged." : "This cannot be undone.", confirm: "Delete", danger: true, reason: mod, reasonHint: "Reason (shown to the author)" });
        if (!r.ok) return;
        const d = await MM.post(base + "/" + (kind === "comment" ? "comments" : "replies") + "/" + id + "/delete", { reason: r.reason });
        t.closest("[data-item]").remove(); setCounts(post, d.count); MM.toast("Removed.", "success");
      }
    } catch (er) { err(er); }
  });

  async function loadComments(post, page, btn) {
    const box = post.querySelector(".comment-list"); const spin = html('<div class="spinner"></div>');
    if (!btn) box.appendChild(spin);
    try {
      const res = await MM.get(base + "/posts/" + post.dataset.id + "/comments?page=" + page);
      const frag = html("<div>" + (await res.text()) + "</div>");
      spin.remove(); if (btn) btn.remove();
      box.append(...frag.children); refresh(box);
    } catch (er) { spin.remove(); throw er; }
  }

  /* comment forms (top-level) */
  root.addEventListener("submit", async (e) => {
    const f = e.target.closest("form.comment-top"); if (!f) return;
    e.preventDefault(); const post = postOf(f), inp = f.querySelector("input");
    if (!inp.value.trim()) return;
    try { const d = await MM.post(base + "/posts/" + post.dataset.id + "/comments", { text: inp.value });
      const list = post.querySelector(".comment-list"); const empty = list.querySelector(".no-comments"); if (empty) empty.remove();
      list.appendChild(refresh(html(d.html))); inp.value = ""; setCounts(post, d.count); } catch (er) { err(er); }
  });

  /* search box: Enter submits as normal GET (server-side search) */
  document.querySelectorAll("[data-autoopen]").forEach((a) => { const p = document.getElementById(a.dataset.autoopen); if (p) { const b = p.querySelector('[data-act="toggle-comments"]'); if (b) b.click(); } });
  if (location.hash.startsWith("#comment-")) { const post = document.querySelector(".post"); const b = post && post.querySelector('[data-act="toggle-comments"]'); if (b) setTimeout(() => b.click(), 50); }
})();
