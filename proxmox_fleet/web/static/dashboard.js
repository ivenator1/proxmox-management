/* Fleet Dashboard enhancements — vanilla JS, no dependencies, progressive
   enhancement only (every page works with JS disabled).

   Features: theme toggle, command palette (Ctrl+K or /), keyboard shortcuts
   (g+key navigation, ? cheat sheet), live relative timestamps, auto-refresh
   while a run is in flight, the overview trend-chart hover layer, and the
   Konami-code CRT easter egg. The live
   console (ANSI rendering, autoscroll, confetti) lives in console.html since
   it is tied to that page's EventSource. */

(() => {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------- theme toggle */

  const themeBtn = $("#theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", toggleTheme);
  }
  function toggleTheme() {
    const root = document.documentElement;
    const next = root.dataset.theme === "light" ? "dark" : "light";
    root.dataset.theme = next;
    try { localStorage.setItem("fleet-theme", next); } catch (e) { /* private mode */ }
  }

  /* ------------------------------------------------- relative timestamps */

  function tickRelativeTimes() {
    document.querySelectorAll("[data-ts]").forEach((el) => {
      const t = Date.parse(el.dataset.ts);
      if (Number.isNaN(t)) return;
      const s = Math.max(0, (Date.now() - t) / 1000);
      let rel;
      if (s < 60) rel = "just now";
      else if (s < 3600) rel = `${Math.floor(s / 60)}m ago`;
      else if (s < 86400) rel = `${Math.floor(s / 3600)}h ago`;
      else rel = `${Math.floor(s / 86400)}d ago`;
      el.textContent = rel;
    });
  }
  tickRelativeTimes();
  setInterval(tickRelativeTimes, 30000);

  /* ------------------------------------- auto-refresh while run in flight */

  if ($("[data-autorefresh]")) {
    setInterval(() => {
      if (!document.hidden) window.location.reload();
    }, 30000);
  }

  /* -------------------------------------------------------- command palette */

  const paletteData = (() => {
    const blob = $("#palette-data");
    if (!blob) return [];
    try { return JSON.parse(blob.textContent); } catch (e) { return []; }
  })();

  let overlay = null;
  let selIndex = 0;

  function openPalette() {
    closeModal();
    overlay = document.createElement("div");
    overlay.className = "palette-overlay";
    overlay.innerHTML =
      '<div class="palette" role="dialog" aria-label="Command palette">' +
      '<input type="text" placeholder="Jump to page, host, or run…" aria-label="Search">' +
      '<ul></ul>' +
      '<div class="palette-foot"><span>↑↓ navigate</span><span>↵ open</span><span>esc close</span></div>' +
      "</div>";
    document.body.appendChild(overlay);
    const input = $("input", overlay);
    input.addEventListener("input", () => renderList(input.value));
    input.addEventListener("keydown", (e) => {
      const items = overlay.querySelectorAll("li");
      if (e.key === "ArrowDown") { e.preventDefault(); selIndex = Math.min(selIndex + 1, items.length - 1); paint(items); }
      else if (e.key === "ArrowUp") { e.preventDefault(); selIndex = Math.max(selIndex - 1, 0); paint(items); }
      else if (e.key === "Enter") {
        const link = items[selIndex] && $("a", items[selIndex]);
        if (link) window.location.href = link.getAttribute("href");
      }
    });
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    renderList("");
    input.focus();
  }

  function renderList(query) {
    const ul = $("ul", overlay);
    const q = query.trim().toLowerCase();
    // simple subsequence fuzzy match, ranked by match tightness
    const scored = [];
    for (const item of paletteData) {
      const hay = item.label.toLowerCase();
      if (!q) { scored.push([0, item]); continue; }
      let qi = 0, spread = 0, last = -1;
      for (let i = 0; i < hay.length && qi < q.length; i++) {
        if (hay[i] === q[qi]) { if (last >= 0) spread += i - last - 1; last = i; qi++; }
      }
      if (qi === q.length) scored.push([spread + (hay.startsWith(q) ? -10 : 0), item]);
    }
    scored.sort((a, b) => a[0] - b[0]);
    const top = scored.slice(0, 12);
    selIndex = 0;
    if (!top.length) {
      ul.innerHTML = '<li class="nothing">No matches</li>';
      return;
    }
    ul.innerHTML = top
      .map(([, item]) =>
        `<li><a href="${item.url}"><span class="kind">${item.kind}</span>${escapeHtml(item.label)}</a></li>`)
      .join("");
    paint(ul.querySelectorAll("li"));
  }

  function paint(items) {
    items.forEach((li, i) => li.classList.toggle("sel", i === selIndex));
    if (items[selIndex]) items[selIndex].scrollIntoView({ block: "nearest" });
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function openShortcuts() {
    closeModal();
    overlay = document.createElement("div");
    overlay.className = "palette-overlay";
    overlay.innerHTML =
      '<div class="palette" role="dialog" aria-label="Keyboard shortcuts">' +
      '<table class="shortcuts-table">' +
      '<tr><td class="keys">ctrl+k or /</td><td>command palette</td></tr>' +
      '<tr><td class="keys">g then o / p / h / t</td><td>overview · pending · history · trigger</td></tr>' +
      '<tr><td class="keys">t</td><td>toggle dark / light theme</td></tr>' +
      '<tr><td class="keys">?</td><td>this cheat sheet</td></tr>' +
      '<tr><td class="keys">↑↑↓↓←→←→BA</td><td class="muted">…try it</td></tr>' +
      "</table></div>";
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
  }

  function closeModal() {
    if (overlay) { overlay.remove(); overlay = null; }
  }

  /* ----------------------------------------------------- keyboard handling */

  let pendingG = false;
  const GOTO = { o: "/", p: "/pending", h: "/history", t: "/trigger" };

  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || e.target.isContentEditable;

    if (e.key === "Escape") { closeModal(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openPalette();
      return;
    }
    if (typing) return;

    if (e.key === "/") { e.preventDefault(); openPalette(); return; }
    if (e.key === "?") { e.preventDefault(); openShortcuts(); return; }

    if (pendingG && GOTO[e.key]) {
      pendingG = false;
      window.location.href = GOTO[e.key];
      return;
    }
    pendingG = false;
    if (e.key === "g") { pendingG = true; setTimeout(() => { pendingG = false; }, 800); return; }
    if (e.key === "t") toggleTheme();
  });

  /* ------------------------------------- combined trend chart hover layer */

  (function initTrendChart() {
    const el = $("#trend-chart");
    const blob = $("#trend-data");
    if (!el || !blob) return;
    let data;
    try { data = JSON.parse(blob.textContent); } catch (e) { return; }
    const runs = (data && data.runs) || [];
    const max = (data && data.max) || 1;
    if (!runs.length) return;

    // must mirror the server-rendered SVG geometry (viewBox 0 0 1000 100, pad 2)
    const W = 1000, H = 100, PAD = 2;
    const series = [
      { key: "os", label: "OS updates", cls: "s-os" },
      { key: "app", label: "app updates", cls: "s-app" },
      { key: "err", label: "errors", cls: "s-err" },
    ];

    const layer = document.createElement("div");
    layer.className = "chart-hover";
    const hair = document.createElement("div");
    hair.className = "hair";
    layer.appendChild(hair);
    const dots = series.map((s) => {
      const d = document.createElement("div");
      d.className = "dot " + s.cls;
      layer.appendChild(d);
      return d;
    });
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    layer.appendChild(tip);
    el.appendChild(layer);

    let idx = -1;

    function render() {
      if (idx < 0) { layer.style.opacity = "0"; return; }
      const w = el.clientWidth, h = el.clientHeight;
      const run = runs[idx];
      const step = runs.length > 1 ? (W - 2 * PAD) / (runs.length - 1) : 0;
      const px = runs.length === 1 ? w / 2 : (PAD + idx * step) / W * w;
      layer.style.opacity = "1";
      hair.style.left = px + "px";
      series.forEach((s, k) => {
        const v = run[s.key] || 0;
        dots[k].style.left = px + "px";
        dots[k].style.top = (PAD + (max - v) / max * (H - 2 * PAD)) / H * h + "px";
      });
      tip.textContent = "";
      const head = document.createElement("div");
      head.className = "tip-head";
      head.textContent = run.label || run.ts;
      tip.appendChild(head);
      for (const s of series) {
        const row = document.createElement("div");
        row.className = "tip-row";
        const key = document.createElement("span");
        key.className = "tip-key " + s.cls;
        const val = document.createElement("strong");
        val.textContent = String(run[s.key] || 0);
        const name = document.createElement("span");
        name.className = "tip-name";
        name.textContent = s.label;
        row.append(key, val, name);
        tip.appendChild(row);
      }
      // keep the tooltip on the roomy side of the crosshair
      tip.style.left = "auto";
      tip.style.right = "auto";
      if (px > w / 2) tip.style.right = (w - px + 10) + "px";
      else tip.style.left = (px + 10) + "px";
    }

    el.addEventListener("pointermove", (e) => {
      if (runs.length === 1) { idx = 0; render(); return; }
      const r = el.getBoundingClientRect();
      const padPx = PAD / W * r.width;
      const frac = (e.clientX - r.left - padPx) / (r.width - 2 * padPx);
      idx = Math.max(0, Math.min(runs.length - 1, Math.round(frac * (runs.length - 1))));
      render();
    });
    el.addEventListener("pointerleave", () => { idx = -1; render(); });
    el.addEventListener("focus", () => { if (idx < 0) idx = runs.length - 1; render(); });
    el.addEventListener("blur", () => { idx = -1; render(); });
    el.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        idx = Math.max(0, (idx < 0 ? runs.length - 1 : idx) - 1);
        render();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        idx = Math.min(runs.length - 1, (idx < 0 ? runs.length - 1 : idx) + 1);
        render();
      } else if (e.key === "Enter" && idx >= 0 && runs[idx].ts) {
        window.location.href = "/history/" + encodeURIComponent(runs[idx].ts);
      }
    });
    el.addEventListener("click", () => {
      if (idx >= 0 && runs[idx].ts) {
        window.location.href = "/history/" + encodeURIComponent(runs[idx].ts);
      }
    });
  })();

  /* ------------------------------------------------- Konami code CRT mode */

  const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown",
    "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];
  let konamiPos = 0;
  try {
    if (localStorage.getItem("fleet-crt") === "on") document.body.classList.add("crt");
  } catch (e) { /* private mode */ }

  document.addEventListener("keydown", (e) => {
    konamiPos = e.key === KONAMI[konamiPos] ? konamiPos + 1 : (e.key === KONAMI[0] ? 1 : 0);
    if (konamiPos === KONAMI.length) {
      konamiPos = 0;
      const on = document.body.classList.toggle("crt");
      try { localStorage.setItem("fleet-crt", on ? "on" : "off"); } catch (err) { /* ok */ }
    }
  });

  /* ----------------------------------------------------------- confetti
     Exposed as window.fleetConfetti() — fired by console.html on rc 0. */

  window.fleetConfetti = function fleetConfetti() {
    if (reducedMotion) return;
    const canvas = document.createElement("canvas");
    canvas.id = "confetti-canvas";
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    document.body.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const colors = ["#3fb96f", "#6c9ef8", "#e0a73c", "#c792ea", "#56c8d8"];
    const bits = Array.from({ length: 140 }, () => ({
      x: Math.random() * canvas.width,
      y: -20 - Math.random() * canvas.height * 0.4,
      w: 5 + Math.random() * 6,
      h: 8 + Math.random() * 8,
      vy: 2.2 + Math.random() * 3.2,
      vx: -1.5 + Math.random() * 3,
      rot: Math.random() * Math.PI,
      vr: -0.12 + Math.random() * 0.24,
      color: colors[(Math.random() * colors.length) | 0],
    }));
    const t0 = performance.now();
    (function frame(now) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const b of bits) {
        b.x += b.vx; b.y += b.vy; b.rot += b.vr;
        ctx.save();
        ctx.translate(b.x, b.y);
        ctx.rotate(b.rot);
        ctx.fillStyle = b.color;
        ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        ctx.restore();
      }
      if (now - t0 < 4000) requestAnimationFrame(frame);
      else canvas.remove();
    })(t0);
  };
})();
