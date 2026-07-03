/* Oliver's web app — all client JS. CSP forbids inline scripts, so every page's behavior lives
   here, dispatched on <body data-page="…">. Shared pieces run everywhere. */
(() => {
  "use strict";
  const CSRF = document.querySelector('meta[name="csrf"]')?.content || "";

  // ── Shared: toast + POST helper ────────────────────────────────────────
  let toastTimer;
  function toast(msg, ok = true) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast show" + (ok ? "" : " err");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = "toast"; }, 2200);
  }
  function post(url, data) {
    const body = new URLSearchParams(Object.assign({ csrf: CSRF }, data || {}));
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch" },
      body,
    });
  }
  function markDirty() { document.getElementById("publishBtn")?.classList.add("dirty"); }

  // ── Shared: keep-alive heartbeat ───────────────────────────────────────
  // The server idles off after 15 request-free minutes — but "idle" must mean "nobody here",
  // not "someone composing a long review". While a tab is visible, ping every 4 minutes.
  setInterval(() => {
    if (!document.hidden) fetch("/healthz").catch(() => {});
  }, 4 * 60 * 1000);

  // ── Shared: publish button ─────────────────────────────────────────────
  const pb = document.getElementById("publishBtn");
  if (pb) pb.addEventListener("click", async () => {
    pb.textContent = "Publishing…"; pb.disabled = true;
    try {
      const r = await post("/webapp/publish");
      const j = await r.json();
      if (r.ok) {
        pb.textContent = j.published ? "Published ✓" : "Up to date";
        pb.classList.remove("dirty");
        toast(j.published ? "Published to the live site ✓" : "Already up to date");
      } else { pb.textContent = "Publish failed"; toast(j.error || "Publish failed", false); }
    } catch (e) { pb.textContent = "Publish failed"; toast("Publish failed — network error", false); }
    setTimeout(() => { pb.textContent = "Publish"; pb.disabled = false; }, 4000);
  });

  // ── Shared: quick filter (rows carry data-q; optional #year select) ────
  function initQuickFilter() {
    const filter = document.querySelector("[data-quickfilter]");
    if (!filter) return;
    const rowsSel = filter.dataset.quickfilter;
    const yearSel = document.getElementById("year");
    const apply = () => {
      const q = filter.value.toLowerCase(), y = yearSel ? yearSel.value : "";
      document.querySelectorAll(rowsSel).forEach((tr) => {
        if (tr.dataset.q === undefined) return;
        const ok = tr.dataset.q.includes(q) && (!y || tr.dataset.year === y);
        tr.style.display = ok ? "" : "none";
      });
    };
    filter.addEventListener("input", apply);
    yearSel?.addEventListener("change", apply);
  }

  // ── Shared: list tables (drag-to-reorder + inline notes) ───────────────
  document.querySelectorAll("table[data-list-table]").forEach((tbl) => {
    const listSlug = tbl.dataset.list, ret = tbl.dataset.ret || "";
    const bodyOf = (tr) => tr.parentElement;
    let dragRow = null;
    tbl.querySelectorAll("td.drag[draggable]").forEach((h) => {
      const tr = h.closest("tr");
      h.addEventListener("dragstart", (e) => {
        dragRow = tr; tr.classList.add("dragging"); e.dataTransfer.effectAllowed = "move";
      });
      h.addEventListener("dragend", () => {
        if (dragRow) dragRow.classList.remove("dragging");
        const moved = dragRow; dragRow = null; if (moved) persist();
      });
    });
    tbl.addEventListener("dragover", (e) => {
      if (!dragRow) return;
      e.preventDefault();
      const rows = [...bodyOf(dragRow).querySelectorAll("tr:not(.dragging)")];
      const after = rows.find((r) => {
        const b = r.getBoundingClientRect(); return e.clientY < b.top + b.height / 2;
      });
      if (after) bodyOf(dragRow).insertBefore(dragRow, after);
      else bodyOf(dragRow).appendChild(dragRow);
    });
    async function persist() {
      const order = [...tbl.querySelectorAll("tr[data-slug]")].map((r) => r.dataset.slug).join(",");
      try {
        const r = await post("/webapp/lists/act", { op: "reorder", list: listSlug, order, return: ret });
        toast(r.ok ? "Order saved ✓" : "Could not save order", r.ok);
        if (r.ok) markDirty();
      } catch (e) { toast("Could not save order", false); }
    }
    tbl.querySelectorAll(".note-input").forEach((inp) => {
      inp.addEventListener("change", async () => {
        try {
          const r = await post("/webapp/lists/act",
            { op: "set-note", list: listSlug, book: inp.dataset.slug, note: inp.value, return: ret });
          toast(r.ok ? "Note saved ✓" : "Could not save note", r.ok);
          if (r.ok) markDirty();
        } catch (e) { toast("Could not save note", false); }
      });
    });
  });

  // ── Shared: star-rating rows (ratings grid + home dashboard) ───────────
  function initStarGrid(container) {
    function paint(slug, rating, dnf) {
      const wrap = container.querySelector('.stars[data-slug="' + CSS.escape(slug) + '"]');
      wrap?.querySelectorAll(".star").forEach((s) => {
        s.className = "star " + (!dnf && rating && +s.dataset.v <= rating ? "on" : "off");
      });
      const d = container.querySelector('.dnf[data-slug="' + CSS.escape(slug) + '"]');
      d?.classList.toggle("active", !!dnf);
    }
    async function save(params, onOk) {
      try {
        const r = await post("/webapp/ratings/set", params);
        if (r.ok) { onOk(); toast("Saved ✓"); markDirty(); }
        else { const j = await r.json().catch(() => ({})); toast(j.error || "Could not save", false); }
      } catch (e) { toast("Could not save — network error", false); }
    }
    container.addEventListener("click", (e) => {
      const star = e.target.closest(".star"), dnfBtn = e.target.closest(".dnf");
      if (star) {
        const slug = star.parentElement.dataset.slug, v = +star.dataset.v;
        save({ book_slug: slug, rating: v }, () => paint(slug, v, false));
      } else if (dnfBtn) {
        const slug = dnfBtn.dataset.slug, makeDnf = !dnfBtn.classList.contains("active");
        save({ book_slug: slug, dnf: makeDnf ? "1" : "" }, () => paint(slug, null, makeDnf));
      }
    });
  }

  const jsonData = (id) => {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  };

  // ── Pages ───────────────────────────────────────────────────────────────
  const pages = {
    ratings() {
      initQuickFilter();
      initStarGrid(document.getElementById("grid"));
    },

    home() {
      const grid = document.getElementById("unrated");
      if (grid) initStarGrid(grid);
    },

    review() {
      const ratingInput = document.getElementById("ratingInput");
      const starWidget = document.getElementById("starWidget");
      const dnfBtn = document.getElementById("dnfBtn");
      function paintStars(rating) {
        starWidget.querySelectorAll(".star").forEach((s) => {
          s.className = "star " + (rating && +s.dataset.v <= rating ? "on" : "off");
        });
      }
      starWidget.addEventListener("click", (e) => {
        const star = e.target.closest(".star"); if (!star) return;
        const v = +star.dataset.v;
        ratingInput.value = v; dnfBtn.classList.remove("active"); paintStars(v);
      });
      dnfBtn.addEventListener("click", () => {
        const makeDnf = !dnfBtn.classList.contains("active");
        dnfBtn.classList.toggle("active", makeDnf);
        ratingInput.value = makeDnf ? "DNF" : "";
        paintStars(0);
      });
      document.getElementById("clearRating").addEventListener("click", () => {
        ratingInput.value = ""; dnfBtn.classList.remove("active"); paintStars(0);
      });

      // Draft autosave: a long compose must survive a server idle-out, browser crash, or
      // accidental tab close. localStorage per book; cleared only by a successful submit.
      const form = document.querySelector("form[data-review-form]");
      const body = form?.querySelector("textarea[name=body]");
      if (form && body) {
        const key = "oliver-draft:" + form.dataset.book;
        const saved = localStorage.getItem(key);
        if (saved && saved !== body.value && !body.value.trim()) {
          body.value = saved;
          toast("Restored an unsaved draft");
        }
        let t;
        body.addEventListener("input", () => {
          clearTimeout(t);
          t = setTimeout(() => localStorage.setItem(key, body.value), 400);
        });
        form.addEventListener("submit", () => localStorage.removeItem(key));
      }

      // Preview toggle: server-side render (same escaping-safe renderer as Oliver's emails).
      const previewBtn = document.getElementById("previewBtn");
      const pane = document.getElementById("previewPane");
      previewBtn?.addEventListener("click", async () => {
        if (!pane.hidden) { pane.hidden = true; previewBtn.textContent = "Preview"; return; }
        try {
          const r = await post("/webapp/preview", { text: body.value });
          const j = await r.json();
          pane.innerHTML = j.html || "<p class='muted'>(nothing to preview)</p>";
          pane.hidden = false;
          previewBtn.textContent = "Hide preview";
        } catch (e) { toast("Preview failed", false); }
      });
    },

    "reviews-index"() { initQuickFilter(); },
    "admin-books"() { initQuickFilter(); },
    "admin-events"() { initQuickFilter(); },
    "admin-bookcloud"() { initQuickFilter(); },
    "admin-memories"() { initQuickFilter(); },
    "member-memories"() { initQuickFilter(); },
    "member-bookcloud"() { initQuickFilter(); },

    "admin-meetings"() {
      const T2S = jsonData("t2s") || {};
      const search = document.getElementById("book-search"), slug = document.getElementById("book-slug");
      const resolve = () => { slug.value = T2S[search.value] || ""; };
      search.addEventListener("change", resolve);
      document.getElementById("addform").addEventListener("submit", resolve);
    },

    "admin-meeting"() {
      const T2S = jsonData("t2s") || {};
      function resolve(input) {
        input.closest(".book-row").querySelector(".book-slug").value = T2S[input.value] || "";
      }
      function wire(input) { input.addEventListener("change", () => resolve(input)); }
      document.querySelectorAll(".book-search").forEach(wire);
      document.getElementById("add-book").addEventListener("click", () => {
        const rows = document.getElementById("book-rows");
        const row = rows.firstElementChild.cloneNode(true);
        row.querySelector(".book-search").value = ""; row.querySelector(".book-slug").value = "";
        rows.appendChild(row); wire(row.querySelector(".book-search"));
      });
      document.getElementById("add-host").addEventListener("click", () => {
        const rows = document.getElementById("host-rows");
        const row = rows.firstElementChild.cloneNode(true);
        row.querySelector("select").value = "";
        rows.appendChild(row);
      });
      document.addEventListener("click", (e) => {
        const btn = e.target.closest(".rm-row"); if (!btn) return;
        const container = btn.closest("#book-rows, #host-rows");
        const row = btn.closest(".book-row, .host-row");
        if (container.children.length > 1) row.remove();
        else row.querySelectorAll("input, select").forEach((el) => { el.value = ""; });
      });
      document.getElementById("mform").addEventListener("submit", () =>
        document.querySelectorAll(".book-search").forEach(resolve));
    },
  };

  const page = document.body.dataset.page;
  if (page && pages[page]) pages[page]();
})();
