/* ============================================================
   PRISM — Dashboard logic
   ============================================================ */
(() => {
  "use strict";

  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  const body = document.body;
  const PRISM_ON = body.dataset.lens === "on";

  /* ---------------- user profile data (injected by Flask) ---------------- */
  function readUserData() {
    try {
      const raw = $("#prism-user-data")?.textContent;
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  }
  const userData = readUserData();

  const CURRENCY_SYMBOLS = { NGN: "₦", USD: "$", GBP: "£", EUR: "€", GHS: "₵", KES: "KSh", ZAR: "R", CAD: "$", INR: "₹" };
  function currencySymbol() {
    return CURRENCY_SYMBOLS[userData.currency] || "₦";
  }

  function avatarInnerHtml(name, picUrl) {
    if (picUrl) return `<img src="${escapeHtmlAttr(picUrl)}" alt="${escapeHtmlAttr(name || "")}">`;
    return escapeHtml((name || "?")[0] || "?").toUpperCase();
  }
  function escapeHtmlAttr(str) { return String(str ?? "").replace(/"/g, "&quot;"); }

  function refreshAvatarsEverywhere() {
    const html = avatarInnerHtml(userData.firstName, userData.profilePictureUrl);
    const barAvatar = document.getElementById("bar-avatar");
    const profileAvatar = document.getElementById("profile-avatar");
    const settingsAvatar = document.getElementById("settings-avatar");
    if (barAvatar) barAvatar.innerHTML = html;
    if (profileAvatar) profileAvatar.innerHTML = html;
    if (settingsAvatar) settingsAvatar.innerHTML = html;
  }
  refreshAvatarsEverywhere();

  /* ---------------- storage helpers ----------------
     `Store` still uses localStorage, but now only for tiny device-local
     UI preferences (theme) and as an instant-paint cache of the last
     server snapshot. All real dashboard data (wishlist, alerts, history,
     stats, achievements) lives in the database and is fetched from /
     pushed to the /api/... endpoints below — Store.cache* is just a
     "show something while we fetch" layer, never the source of truth. */
  const Store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem("prism:" + key);
        return raw ? JSON.parse(raw) : fallback;
      } catch { return fallback; }
    },
    set(key, val) {
      try { localStorage.setItem("prism:" + key, JSON.stringify(val)); } catch {}
    },
  };

  const state = {
    wishlist: [],
    alerts: [],
    history: [],
    stats: { saved: 0, searches: 0, accepted: 0, categories: {} },
    chatHistory: [], // {role, text}
    achievements: [],
    ready: false,
  };

  /* Cache the last known-good snapshot locally so a reload paints instantly
     instead of a blank dashboard while /api/data/bootstrap round-trips. */
  function cacheSnapshot() {
    Store.set("cache:snapshot", {
      wishlist: state.wishlist, alerts: state.alerts, history: state.history,
      stats: state.stats, achievements: state.achievements,
    });
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed.");
    return data;
  }
  async function deleteJSON(url) {
    const res = await fetch(url, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed.");
    return data;
  }

  /** Load everything from the database. Called once on boot; paints from
   *  the cached snapshot immediately, then reconciles with the server. */
  async function loadFromServer() {
    const cached = Store.get("cache:snapshot", null);
    if (cached) Object.assign(state, cached);
    renderAll();
    try {
      const data = await getJSON("/api/data/bootstrap");
      state.wishlist = data.wishlist || [];
      state.alerts = data.alerts || [];
      state.history = data.history || [];
      state.stats = data.stats || { saved: 0, searches: 0, accepted: 0, categories: {} };
      state.achievements = data.achievements || [];
      state.ready = true;
      cacheSnapshot();
      renderAll();
    } catch (err) {
      toast("⚠️ Couldn't sync with the server — showing your last saved data.");
    }
  }

  function renderAll() {
    renderStats();
    renderWishlist();
    renderAlerts();
    renderHistory();
    renderRecent();
    renderProfile();
  }

  /* ---------------- trending panel: refetches every 60s, weighted to this
     account's own category activity server-side ---------------- */
  let __trendTimer = null;
  function renderTrending(payload) {
    const list = $("#trend-list");
    const badge = $("#trend-live-badge");
    if (!list) return;
    const items = (payload && payload.items) || [];
    if (!items.length) return;
    list.classList.add("is-updating");
    list.innerHTML = items.map((it) => {
      const up = it.direction !== "▼";
      const liveTag = it.live
        ? `<span class="trend-src trend-src--live" title="Backed by real Google Trends data">●</span>`
        : `<span class="trend-src" title="Simulated signal — no live data available for this item right now">○</span>`;
      return `<li><span>${liveTag}${escapeHtml(it.name)}</span><b class="${up ? "trend-up" : "trend-down"}">${it.direction} ${it.pct}%</b></li>`;
    }).join("");
    requestAnimationFrame(() => list.classList.remove("is-updating"));
    if (badge) badge.title = payload.personalized ? "Personalized to your activity" : "General trending";
  }

  async function refreshTrending() {
    try {
      const data = await getJSON("/api/trending");
      renderTrending(data);
    } catch {
      /* keep showing whatever was last rendered rather than clearing it */
    }
  }

  function startTrendingLoop() {
    refreshTrending();
    if (__trendTimer) clearInterval(__trendTimer);
    __trendTimer = setInterval(refreshTrending, 60 * 1000);
    // also resync the moment a tab regains focus, in case it was backgrounded
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshTrending();
    });
  }

  function toast(msg) {
    const stack = $("#toast-stack");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  /** Log a history/analytics event. Optimistically updates the UI, then
   *  writes it to the database so it survives reloads and other devices. */
  function logHistory(type, text) {
    const optimistic = { id: "tmp_" + Date.now(), type, text, ts: Date.now() };
    state.history.unshift(optimistic);
    state.history = state.history.slice(0, 60);
    cacheSnapshot();
    renderHistory();
    renderRecent();
    postJSON("/api/history", { type, text })
      .then((saved) => {
        const idx = state.history.indexOf(optimistic);
        if (idx !== -1) state.history[idx] = saved;
        cacheSnapshot();
      })
      .catch(() => {});
  }

  function bumpStat(field, amount = 1) {
    state.stats[field] = (state.stats[field] || 0) + amount;
    cacheSnapshot();
    renderStats();
    postJSON("/api/stats/bump", { field, amount })
      .then((stats) => { state.stats = stats; cacheSnapshot(); renderStats(); })
      .catch(() => {});
  }

  function bumpCategory(name) {
    if (!name) return;
    state.stats.categories[name] = (state.stats.categories[name] || 0) + 1;
    cacheSnapshot();
    postJSON("/api/stats/category", { name })
      .then((stats) => { state.stats = stats; cacheSnapshot(); renderStats(); })
      .catch(() => {});
  }

  function maybeAward(name) {
    if (!state.achievements.includes(name)) {
      state.achievements.push(name);
      cacheSnapshot();
      toast("🏆 Achievement unlocked: " + name);
      renderProfile();
      postJSON("/api/achievements", { name }).catch(() => {});
    }
  }

  /* ---------------- navigation ---------------- */
  const railItems = $$(".rail__item[data-view]");
  const mobileTabItems = $$(".mobile-tabs__item[data-view]");
  const views = $$(".view");

  let previousView = "home";
  let currentView = "home";

  function showView(name) {
    if (name !== currentView) {
      // Never treat "assistant" itself as a place to go "back" to — we want
      // Back to always land on whatever view the shopper was on *before*
      // they opened the assistant, even if they hop into it more than once.
      if (currentView !== "assistant") previousView = currentView;
      currentView = name;
    }
    railItems.forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
    mobileTabItems.forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
    views.forEach((v) => v.classList.toggle("is-active", v.id === "view-" + name));
    if (name === "analytics") requestAnimationFrame(renderCharts);
    if (name === "copilot") renderMemoryCard();
    if (window.innerWidth <= 900) collapseRail(true);
  }
  railItems.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  mobileTabItems.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  $("#assistant-back-btn")?.addEventListener("click", () => showView(previousView || "home"));

  // stagger index for the drawer's opening animation (section labels count too)
  $$(".rail__nav > *").forEach((el, i) => { el.style.setProperty("--ri", i); });

  const osRoot = document.querySelector(".os");
  const railBackdrop = $("#rail-backdrop");

  function collapseRail(collapse) {
    osRoot.classList.toggle("is-rail-collapsed", collapse);
    if (window.innerWidth <= 900) {
      document.body.classList.toggle("rail-open", !collapse);
      $("#mobile-menu-btn")?.classList.toggle("is-active", !collapse);
    }
  }
  $("#burger").addEventListener("click", () => collapseRail(!osRoot.classList.contains("is-rail-collapsed")));
  $("#rail-close")?.addEventListener("click", () => collapseRail(true));
  $("#mobile-menu-btn")?.addEventListener("click", () => {
    collapseRail(osRoot.classList.contains("is-rail-collapsed") ? false : true);
  });
  railBackdrop?.addEventListener("click", () => collapseRail(true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !osRoot.classList.contains("is-rail-collapsed")) collapseRail(true);
  });
  if (window.innerWidth <= 900) collapseRail(true);

  /* ---------------- notifications panel ---------------- */
  const notifPanel = $("#notif-panel");
  $("#bell-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    notifPanel.classList.toggle("is-open");
    renderNotifs();
  });
  document.addEventListener("click", (e) => {
    if (!notifPanel.contains(e.target) && e.target.id !== "bell-btn") notifPanel.classList.remove("is-open");
  });
  function renderNotifs() {
    const list = $("#notif-list");
    const items = [
      ...state.alerts.slice(0, 3).map((a) => `<div class="notif-item">Watching <b>${escapeHtml(a.product)}</b> for ${escapeHtml(a.price)}</div>`),
      `<div class="notif-item"><b>iPhone 17 Pro</b> demand is up 12% this week.</div>`,
      `<div class="notif-item">Prism found a possible price drop pattern on <b>noise-cancelling earbuds</b>.</div>`,
    ];
    list.innerHTML = items.join("") || `<div class="notif-item">You're all caught up.</div>`;
  }

  /* ---------------- user menu (avatar) ---------------- */
  $("#user-menu-btn").addEventListener("click", () => showView("profile"));

  /* ---------------- utils ---------------- */
  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed.");
    return data;
  }

  /* ================================================================
     MARKETPLACE BAR — every Prism suggestion links out to real
     marketplaces where the shopper can verify and buy the item.
     (The product-photo gallery that used to sit here has been removed:
     the underlying image providers weren't reliably returning real
     photos, so rather than keep showing broken/blank images, Prism
     just points shoppers straight to verified marketplace listings.)
     ================================================================ */
  function marketBar(links) {
    if (!links || !links.length) return "";
    return `<div class="pick__market">
      <span class="pick__market-label">Find it on</span>
      <div class="pick__market-row">
        ${links.map((l) => `<a class="market-pill" href="${l.url}" target="_blank" rel="noopener noreferrer">
          <span class="market-pill__icon">${l.icon || "🔗"}</span>${escapeHtml(l.name)}
        </a>`).join("")}
      </div>
    </div>`;
  }

  /* ================================================================
     PACK CARDS — every Prism suggestion is wrapped in a sealed "pack"
     that opens with a shake + spark-burst + reveal, like opening a
     card pack. Rarity is derived from the pick's own rank/tag, so the
     shine actually communicates something (this is the top pick vs.
     a runner-up) instead of being arbitrary decoration.
     ================================================================ */
  function rarityForTag(tag) {
    const t = (tag || "").toLowerCase();
    if (t.includes("overall") || t.includes("rank 1") || t.includes("#1")) return "iconic";
    if (t.includes("value") || t.includes("rank 2") || t.includes("#2")) return "gold";
    if (t.includes("long") || t.includes("rank 3") || t.includes("#3")) return "silver";
    return "standard";
  }
  const RARITY_ICON = { iconic: "✦", gold: "◆", silver: "▲", standard: "●" };
  const RARITY_LABEL = { iconic: "Iconic Pick", gold: "Gold Pick", silver: "Silver Pick", standard: "Prism Pick" };

  /** Wrap any pick's inner HTML in a sealed pack shell. Call this instead of
   *  returning raw pick markup, then call initPacks(root) after inserting. */
  function wrapPack(innerHtml, tag) {
    const rarity = rarityForTag(tag);
    return `
      <div class="pack" data-rarity="${rarity}">
        <button type="button" class="pack__seal" aria-label="Reveal ${escapeHtml(tag || "Prism pick")}">
          <span class="pack__seal-icon">${RARITY_ICON[rarity]}</span>
          <span class="pack__seal-label">${escapeHtml(RARITY_LABEL[rarity])}</span>
          <span class="pack__seal-tag">${escapeHtml(tag || "Prism Pick")}</span>
          <span class="pack__seal-cta">Tap to reveal</span>
        </button>
        <div class="pack__content" aria-hidden="true">${innerHtml}</div>
      </div>`;
  }

  function spawnSparks(pack, count) {
    const n = count || 16;
    for (let i = 0; i < n; i++) {
      const spark = document.createElement("span");
      spark.className = "pack__spark";
      const angle = Math.random() * Math.PI * 2;
      const dist = 60 + Math.random() * 100;
      spark.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
      spark.style.setProperty("--dy", `${Math.sin(angle) * dist}px`);
      pack.appendChild(spark);
      setTimeout(() => spark.remove(), 800);
    }
  }

  /* ----------------------------------------------------------------
     FULL-SCREEN REVEAL SEQUENCE — the "FC Mobile" style pack-opening
     moment: a card zooms in centre-screen, shakes with anticipation,
     bursts into light rays, then flips to show the rarity face before
     handing off to the normal inline reveal. A Skip control is visible
     the instant the sequence starts so nobody's stuck watching it twice.
     ---------------------------------------------------------------- */
  function buildRevealOverlay(rarity, tag, icon, label) {
    const el = document.createElement("div");
    el.className = "reveal-overlay";
    el.dataset.rarity = rarity;
    el.innerHTML = `
      <div class="reveal-overlay__backdrop"></div>
      <button type="button" class="reveal-overlay__skip">Skip <span>›</span></button>
      <div class="reveal-overlay__stage">
        <div class="reveal-overlay__rays"></div>
        <div class="reveal-overlay__glow"></div>
        <div class="reveal-overlay__card">
          <div class="reveal-overlay__face reveal-overlay__face--back">
            <span class="reveal-overlay__mark">✦</span>
          </div>
          <div class="reveal-overlay__face reveal-overlay__face--front">
            <span class="reveal-overlay__icon">${escapeHtml(icon)}</span>
            <span class="reveal-overlay__label">${escapeHtml(label)}</span>
            <span class="reveal-overlay__tag">${escapeHtml(tag)}</span>
          </div>
        </div>
        <div class="reveal-overlay__beams"></div>
      </div>`;
    return el;
  }

  /** Plays the full-screen reveal sequence, then reveals `content` inline.
   *  Skipping (button, tap-anywhere, or Escape) jumps straight to the end. */
  function playRevealSequence(pack, content, rarity, tag, icon, label) {
    const overlay = buildRevealOverlay(rarity, tag, icon, label);
    document.body.appendChild(overlay);
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => overlay.classList.add("is-live"));

    const timers = [];
    let finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      timers.forEach(clearTimeout);
      document.removeEventListener("keydown", onKey);
      overlay.classList.add("is-closing");
      setTimeout(() => {
        overlay.remove();
        document.body.style.overflow = "";
        finishInlineReveal(pack, content);
      }, 240);
    }
    function onKey(e) { if (e.key === "Escape" || e.key === " " || e.key === "Enter") finish(); }

    overlay.querySelector(".reveal-overlay__skip").addEventListener("click", finish);
    overlay.querySelector(".reveal-overlay__backdrop").addEventListener("click", finish);
    document.addEventListener("keydown", onKey);

    // Anticipation shake -> light-ray burst -> card flip -> auto hand-off.
    timers.push(setTimeout(() => overlay.classList.add("stage-shake"), 60));
    timers.push(setTimeout(() => overlay.classList.add("stage-burst"), 60 + 850));
    timers.push(setTimeout(() => overlay.classList.add("stage-flip"), 60 + 850 + 240));
    timers.push(setTimeout(finish, 60 + 850 + 240 + 950));
  }

  function finishInlineReveal(pack, content) {
    pack.classList.add("is-burst");
    spawnSparks(pack);
    setTimeout(() => {
      pack.classList.remove("is-opening", "is-burst");
      pack.classList.add("is-opened");
      if (content) content.removeAttribute("aria-hidden");
      pack.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 160);
  }

  /** Call after inserting HTML built with wrapPack() into the DOM. */
  function initPacks(root) {
    $$(".pack", root).forEach((pack) => {
      if (pack.dataset.packBound) return;
      pack.dataset.packBound = "1";
      const seal = pack.querySelector(".pack__seal");
      const content = pack.querySelector(".pack__content");
      if (!seal) return;
      seal.addEventListener("click", () => {
        if (pack.classList.contains("is-opening") || pack.classList.contains("is-opened")) return;
        const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        pack.classList.add("is-opening");
        const rarity = pack.dataset.rarity || "standard";
        const icon = seal.querySelector(".pack__seal-icon")?.textContent || "●";
        const label = seal.querySelector(".pack__seal-label")?.textContent || "Prism Pick";
        const tag = seal.querySelector(".pack__seal-tag")?.textContent || "";
        if (reduced) {
          finishInlineReveal(pack, content);
          return;
        }
        playRevealSequence(pack, content, rarity, tag, icon, label);
      });
    });
  }

  /* ================================================================
     HOME
     ================================================================ */
  function renderStats() {
    const s = state.stats;
    $("#stat-saved").textContent = currencySymbol() + (s.saved || 0).toLocaleString();
    $("#stat-searches").textContent = s.searches || 0;
    $("#stat-wishlist").textContent = state.wishlist.length;
    $("#stat-alerts").textContent = state.alerts.length;
    $("#an-saved").textContent = currencySymbol() + (s.saved || 0).toLocaleString();
    $("#an-searches").textContent = s.searches || 0;
    $("#an-accepted").textContent = s.accepted || 0;
    const topCat = Object.entries(s.categories || {}).sort((a, b) => b[1] - a[1])[0];
    $("#an-category").textContent = topCat ? topCat[0] : "—";
  }

  function renderRecent() {
    const wrap = $("#recent-list");
    const searches = state.history.filter((h) => h.type === "search" || h.type === "chat").slice(0, 6);
    $("#recent-count").textContent = searches.length;
    if (!searches.length) {
      wrap.innerHTML = `<p class="empty-note">Nothing yet — your recent searches will land here.</p>`;
      return;
    }
    wrap.innerHTML = searches.map((h) => `
      <div class="recent-item">
        <span>${escapeHtml(h.text)}</span>
        <time>${new Date(h.ts).toLocaleDateString()}</time>
      </div>`).join("");
  }

  /* ================================================================
     AI ASSISTANT (streaming chat)
     ================================================================ */
  const chatLog = $("#chat-log");
  const chatForm = $("#chat-form");
  const chatInput = $("#chat-input");
  const chatSendBtn = $("#chat-send-btn");
  const chatImageInput = $("#chat-image-input");
  const chatAttachBtn = $("#chat-attach-btn");
  const chatAttachPreview = $("#chat-attach-preview");
  const chatAttachThumb = $("#chat-attach-thumb");
  const chatAttachRemove = $("#chat-attach-remove");

  let pendingChatImage = null; // { dataUrl, base64, mimeType }

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  chatAttachBtn.addEventListener("click", () => chatImageInput.click());
  chatImageInput.addEventListener("change", async () => {
    const file = chatImageInput.files[0];
    if (!file) return;
    const dataUrl = await fileToDataUrl(file);
    pendingChatImage = { dataUrl, base64: dataUrl.split(",")[1] || "", mimeType: file.type || "image/jpeg" };
    chatAttachThumb.src = dataUrl;
    chatAttachPreview.hidden = false;
    chatImageInput.value = "";
  });
  chatAttachRemove.addEventListener("click", () => {
    pendingChatImage = null;
    chatAttachPreview.hidden = true;
    chatAttachThumb.src = "";
  });

  chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 140) + "px";
  });
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
  });

  function addBubble(role, html) {
    const wrap = document.createElement("div");
    wrap.className = "chat__msg chat__msg--" + role;
    const avatarHtml = role === "user"
      ? `<div class="chat__avatar chat__avatar--user">${avatarInnerHtml(userData.firstName, userData.profilePictureUrl)}</div>`
      : `<div class="chat__avatar chat__avatar--model">L</div>`;
    wrap.innerHTML = role === "user"
      ? `<div class="chat__bubble">${html}</div>${avatarHtml}`
      : `${avatarHtml}<div class="chat__bubble">${html}</div>`;
    chatLog.appendChild(wrap);
    chatLog.scrollTop = chatLog.scrollHeight;
    return wrap.querySelector(".chat__bubble");
  }

  function renderMarkdown(text) {
    try { return window.marked ? window.marked.parse(text) : escapeHtml(text); }
    catch { return escapeHtml(text); }
  }

  async function sendChat(message, imageAttachment) {
    const userBubbleHtml = imageAttachment
      ? `<img class="chat__inline-image" src="${imageAttachment.dataUrl}" alt="Attached photo">${message ? `<p>${escapeHtml(message)}</p>` : ""}`
      : escapeHtml(message);
    addBubble("user", userBubbleHtml);
    state.chatHistory.push({ role: "user", text: message || "[Sent a photo]" });
    logHistory("chat", message || "Sent a photo to Prism");
    bumpStat("searches");

    const bubble = addBubble("model", '<span class="chat__cursor"></span>');
    chatSendBtn.disabled = true;

    if (!PRISM_ON) {
      bubble.innerHTML = renderMarkdown("⚠️ **Prism is offline.** Add a `GROQ_API_KEY` to the server environment to activate live AI answers.");
      chatSendBtn.disabled = false;
      return;
    }

    try {
      const res = await fetch("/api/ai/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          history: state.chatHistory.slice(0, -1),
          image: imageAttachment ? imageAttachment.base64 : undefined,
          image_mime_type: imageAttachment ? imageAttachment.mimeType : undefined,
        }),
      });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let full = "";
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop();
        for (const evt of events) {
          const line = evt.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          try {
            const payload = JSON.parse(line.slice(5).trim());
            if (payload.text) {
              full += payload.text;
              bubble.innerHTML = renderMarkdown(full) + '<span class="chat__cursor"></span>';
              chatLog.scrollTop = chatLog.scrollHeight;
            }
            if (payload.error) full = "⚠️ " + payload.error;
          } catch {}
        }
      }
      bubble.innerHTML = renderMarkdown(full || "I couldn't generate a response — try rephrasing.");
      state.chatHistory.push({ role: "model", text: full });
      if (imageAttachment) maybeAward("First photo sent to Prism");
    } catch (err) {
      bubble.innerHTML = renderMarkdown("⚠️ Something went wrong reaching Prism: " + err.message);
    } finally {
      chatSendBtn.disabled = false;
      maybeAward("First conversation with Prism");
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const msg = chatInput.value.trim();
    const attachment = pendingChatImage;
    if (!msg && !attachment) return;
    chatInput.value = "";
    chatInput.style.height = "auto";
    pendingChatImage = null;
    chatAttachPreview.hidden = true;
    chatAttachThumb.src = "";
    sendChat(msg, attachment);
  });

  $("#chat-clear-btn").addEventListener("click", () => {
    state.chatHistory = [];
    chatLog.innerHTML = `<div class="chat__msg chat__msg--model"><div class="chat__avatar chat__avatar--model">L</div><div class="chat__bubble">New chat started. What are you shopping for?</div></div>`;
  });

  /* ================================================================
     SEARCH
     ================================================================ */
  const searchForm = $("#search-form");
  const searchInput = $("#search-input");
  const searchResults = $("#search-results");

  async function runSearch(query) {
    if (!query) return;
    logHistory("search", query);
    bumpStat("searches");
    searchResults.innerHTML = `<div class="glass result-card"><p class="empty-note">Prism is analyzing "${escapeHtml(query)}"…</p></div>`;
    if (!PRISM_ON) {
      searchResults.innerHTML = `<div class="glass result-card"><p class="empty-note">⚠️ Prism is offline — set GROQ_API_KEY on the server.</p></div>`;
      return;
    }
    try {
      const data = await postJSON("/api/ai/search", { query });
      bumpCategory(guessCategory(query));
      renderSearchResults(data);
      $("#insight-text").textContent = data.verdict || $("#insight-text").textContent;
    } catch (err) {
      searchResults.innerHTML = `<div class="glass result-card"><p class="empty-note">⚠️ ${escapeHtml(err.message)}</p></div>`;
    }
  }

  function guessCategory(q) {
    const text = (q || "").toLowerCase();
    // Keys here must match TRENDING_POOL in app.py exactly — this is what
    // actually drives "Trending Right Now" toward what a shopper searches.
    const rules = [
      { cat: "laptops", words: ["laptop", "notebook", "macbook", "ultrabook", "chromebook"] },
      { cat: "phones", words: ["phone", "iphone", "samsung", "pixel", "smartphone", "android"] },
      { cat: "vehicles", words: ["car", "vehicle", "suv", "ev", "sedan", "corolla", "truck"] },
      { cat: "homes", words: ["apartment", "flat", "house", "home", "mortgage", "rent"] },
      { cat: "travel", words: ["flight", "travel", "hotel", "luggage", "trip", "vacation"] },
      { cat: "insurance", words: ["insurance", "cover", "hmo", "policy"] },
      { cat: "gaming", words: ["game", "gaming", "console", "ps5", "xbox", "playstation"] },
      { cat: "fashion", words: ["sneaker", "shoe", "jacket", "dress", "fashion", "denim", "watch"] },
      { cat: "groceries", words: ["rice", "grocery", "groceries", "food", "pasta", "formula"] },
      { cat: "audio", words: ["earbud", "headphone", "speaker", "airpods"] },
      { cat: "electronics", words: ["camera", "tv", "gadget", "electronic", "gpu", "rtx", "monitor"] },
    ];
    const hit = rules.find((r) => r.words.some((w) => text.includes(w)));
    return hit ? hit.cat : "general";
  }

  function pickCard(p, sourceLabel) {
    const id = "p_" + Math.random().toString(36).slice(2, 9);
    const inner = `
      <div class="pick">
        <span class="pick__tag">${escapeHtml(p.tag || "Pick")}</span>
        <span class="pick__name">${escapeHtml(p.name || "")}</span>
        <span class="pick__price">${escapeHtml(p.price_estimate || p.price || "")}</span>
        <p class="pick__why">${escapeHtml(p.why || "")}</p>
        ${p.pros || p.cons ? `<div class="pick__lists">
          <div class="pos"><b>Pros</b><ul>${(p.pros || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>
          <div class="neg"><b>Cons</b><ul>${(p.cons || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>
        </div>` : ""}
        ${marketBar(p.marketplace_links)}
        <p class="pick__verify">⚠ Estimates from Prism — confirm live price &amp; stock on the marketplace before paying.</p>
        <div class="pick__actions">
          <button data-act="wishlist" data-name="${escapeHtml(p.name || "")}" data-price="${escapeHtml(p.price_estimate || p.price || "")}">+ Wishlist</button>
          <button data-act="reviews" data-name="${escapeHtml(p.name || "")}">Reviews</button>
        </div>
      </div>`;
    return wrapPack(inner, p.tag);
  }

  function renderSearchResults(data) {
    searchResults.innerHTML = `
      <div class="glass result-card">
        <div class="result-card__interp">${escapeHtml(data.interpretation || "")}</div>
        <div class="result-card__verdict">${escapeHtml(data.verdict || "")}</div>
        <div class="pick-grid">${(data.picks || []).map((p) => pickCard(p)).join("")}</div>
      </div>`;
    initPacks(searchResults);
  }

  searchResults.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    if (btn.dataset.act === "wishlist") addToWishlist(btn.dataset.name, btn.dataset.price);
    if (btn.dataset.act === "reviews") { showView("assistant"); sendChat(`Summarize reviews for ${btn.dataset.name} — should I buy it?`); }
  });

  searchForm.addEventListener("submit", (e) => { e.preventDefault(); runSearch(searchInput.value.trim()); });
  $$(".chip", $("#view-search")).forEach((c) => c.addEventListener("click", () => { searchInput.value = c.dataset.val; runSearch(c.dataset.val); }));

  /* ================================================================
     SCANNER (vision + barcode)
     ================================================================ */
  let scanMode = "product";
  $$("#scan-mode-seg .seg__opt").forEach((btn) => btn.addEventListener("click", () => {
    scanMode = btn.dataset.mode;
    $$("#scan-mode-seg .seg__opt").forEach((b) => b.classList.toggle("is-active", b === btn));
    $("#scanner-label").textContent = scanMode === "barcode" ? "Drop a barcode image or click to scan" : "Drop an image or click to scan";
  }));

  const scanFile = $("#scan-file");
  const scanPreview = $("#scan-preview");
  const scannerLabel = $("#scanner-label");
  const scannerLaser = $("#scanner-laser");
  const scanResultPanel = $("#scan-result");

  ["dragover", "dragenter"].forEach((evt) => $("#scan-drop").addEventListener(evt, (e) => { e.preventDefault(); }));
  $("#scan-drop").addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) handleScanFile(file);
  });
  scanFile.addEventListener("change", () => { if (scanFile.files[0]) handleScanFile(scanFile.files[0]); });

  function handleScanFile(file) {
    if (!file.type || !file.type.startsWith("image/")) {
      toast("That doesn't look like an image — pick a photo (JPG, PNG, WEBP) to scan.");
      return;
    }
    if (file.size > 16 * 1024 * 1024) {
      toast("That image is too large to scan (16MB max).");
      return;
    }
    const url = URL.createObjectURL(file);
    scanPreview.onerror = () => { scanPreview.style.display = "none"; toast("Couldn't preview that image."); };
    scanPreview.src = url;
    scanPreview.style.display = "block";
    scannerLabel.textContent = "Scanning…";
    scannerLaser.classList.add("is-active");
    runVision(file);
  }

  /* ---- live camera capture ("snap a photo") ---- */
  const camModal = $("#cam-modal");
  const camVideo = $("#cam-video");
  const camCanvas = $("#cam-canvas");
  let camStream = null;

  async function openCamera() {
    if (!navigator.mediaDevices?.getUserMedia) {
      toast("Your browser doesn't support camera capture — use file upload instead.");
      return;
    }
    try {
      camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      camVideo.srcObject = camStream;
      camModal.hidden = false;
    } catch (err) {
      toast("Couldn't access your camera: " + err.message);
    }
  }

  function closeCamera() {
    camStream?.getTracks().forEach((t) => t.stop());
    camStream = null;
    camModal.hidden = true;
  }

  function captureFromCamera() {
    const w = camVideo.videoWidth, h = camVideo.videoHeight;
    if (!w || !h) return;
    camCanvas.width = w;
    camCanvas.height = h;
    camCanvas.getContext("2d").drawImage(camVideo, 0, 0, w, h);
    camCanvas.toBlob((blob) => {
      if (!blob) return;
      const file = new File([blob], "camera-snap.jpg", { type: "image/jpeg" });
      closeCamera();
      handleScanFile(file);
    }, "image/jpeg", 0.92);
  }

  $("#scan-camera-btn").addEventListener("click", openCamera);
  $("#cam-cancel-btn").addEventListener("click", closeCamera);
  $("#cam-modal-backdrop").addEventListener("click", closeCamera);
  $("#cam-shoot-btn").addEventListener("click", captureFromCamera);

  async function runVision(file) {
    scanResultPanel.innerHTML = `<p class="empty-note">Prism is looking closely…</p>`;
    if (!PRISM_ON) {
      scanResultPanel.innerHTML = `<p class="empty-note">⚠️ Prism is offline — set GROQ_API_KEY on the server.</p>`;
      scannerLaser.classList.remove("is-active");
      return;
    }
    const fd = new FormData();
    fd.append("image", file);
    fd.append("mode", scanMode);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000); // never hang forever
    try {
      const res = await fetch("/api/ai/vision", { method: "POST", body: fd, signal: controller.signal });
      const data = await res.json().catch(() => { throw new Error("Prism sent back something unreadable — try again."); });
      if (!res.ok) throw new Error(data.error || "Scan failed.");
      renderVisionResult(data);
      logHistory("scan", data.product_name || "Scanned product");
      bumpCategory(data.category);
      maybeAward("First AI scan");
    } catch (err) {
      const msg = err.name === "AbortError" ? "Scan timed out — check your connection and try again." : err.message;
      scanResultPanel.innerHTML = `<p class="empty-note">⚠️ ${escapeHtml(msg)}</p>`;
    } finally {
      clearTimeout(timeout);
      scannerLaser.classList.remove("is-active");
      scannerLabel.textContent = "Scan complete";
    }
  }

  function renderVisionResult(d) {
    const confidence = (d.confidence || "").toLowerCase();
    const confidenceNote = confidence === "low"
      ? `<p class="pick__verify pick__verify--low">⚠ Prism isn't confident about this one (low confidence) — the photo may be unclear. Treat this as a rough guess and verify carefully before buying.</p>`
      : "";
    const inner = `
      <div class="vision-card">
        <div class="vision-card__row"><span>Product</span><b>${escapeHtml(d.product_name || "—")}</b></div>
        <div class="vision-card__row"><span>Brand</span><b>${escapeHtml(d.brand || "—")}</b></div>
        <div class="vision-card__row"><span>Category</span><b>${escapeHtml(d.category || "—")}</b></div>
        <div class="vision-card__row"><span>Estimated Price</span><b>${escapeHtml(d.estimated_price || "—")}</b></div>
        ${d.barcode_digits ? `<div class="vision-card__row"><span>Barcode</span><b>${escapeHtml(d.barcode_digits)}</b></div>` : ""}
        <p class="panel__body">${escapeHtml(d.summary || "")}</p>
        <div class="vision-card__specs">${(d.specs || []).map((s) => `<span class="tag-pill">${escapeHtml(s)}</span>`).join("")}</div>
        ${marketBar(d.marketplace_links)}
        ${confidenceNote}
        <p class="pick__verify">⚠ Prism identified this from the photo — confirm exact model, condition &amp; live price on the marketplace before buying.</p>
        <button class="ghost-btn" id="scan-to-wishlist" data-name="${escapeHtml(d.product_name || "")}" data-price="${escapeHtml(d.estimated_price || "")}">+ Add to wishlist</button>
      </div>`;
    scanResultPanel.innerHTML = `
      <div class="panel__head"><h3>Analysis</h3><span class="eyebrow-mini">Prism Vision</span></div>
      ${wrapPack(inner, "Vision Match")}`;
    initPacks(scanResultPanel);
    $("#scan-to-wishlist")?.addEventListener("click", (e) => addToWishlist(e.target.dataset.name, e.target.dataset.price));
  }

  /* ================================================================
     COMPARE
     ================================================================ */
  $("#compare-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const products = [$("#compare-a").value, $("#compare-b").value, $("#compare-c").value].map((v) => v.trim()).filter(Boolean);
    const out = $("#compare-results");
    out.innerHTML = `<div class="glass panel"><p class="empty-note">Prism is weighing the tradeoffs…</p></div>`;
    logHistory("compare", products.join(" vs "));
    if (!PRISM_ON) { out.innerHTML = `<div class="glass panel"><p class="empty-note">⚠️ Prism is offline — set GROQ_API_KEY.</p></div>`; return; }
    try {
      const data = await postJSON("/api/ai/compare", { products });
      const productLinks = data.product_links || {};
      out.innerHTML = `
        <div class="glass compare-table-wrap">
          <table class="compare-table">
            <thead><tr><th>Spec</th>${data.products.map((p) => `<th>${escapeHtml(p)}</th>`).join("")}</tr></thead>
            <tbody>${data.rows.map((r) => `<tr><td>${escapeHtml(r.spec)}</td>${r.values.map((v) => `<td>${escapeHtml(v)}</td>`).join("")}</tr>`).join("")}</tbody>
          </table>
        </div>
        <div class="glass compare-winner">🏆 Winner: <b>${escapeHtml(data.winner || "")}</b><p style="margin-top:8px;color:var(--ink-dim);font-size:13.5px;">${escapeHtml(data.verdict || "")}</p></div>
        <div class="pick-grid">${data.products.map((name) => wrapPack(`
          <div class="pick pick--compare">
            <span class="pick__name">${escapeHtml(name)}</span>
            ${marketBar(productLinks[name])}
          </div>`, name)).join("")}</div>
        <p class="pick__verify">⚠ Specs above are Prism's best estimate — confirm exact configuration and price on the marketplace links before buying.</p>`;
      initPacks(out);
      maybeAward("First comparison");
    } catch (err) {
      out.innerHTML = `<div class="glass panel"><p class="empty-note">⚠️ ${escapeHtml(err.message)}</p></div>`;
    }
  });

  /* ================================================================
     RECOMMENDATIONS
     ================================================================ */
  async function loadFxHint() {
    const hint = $("#fx-hint");
    if (!hint || !PRISM_ON) return;
    try {
      const data = await (await fetch("/api/fx/usd-ngn")).json();
      if (data && data.rate) hint.textContent = `Live rate: $1 ≈ ${data.symbol || currencySymbol()}${Number(data.rate).toLocaleString()} ${data.currency || userData.currency || "NGN"}`;
    } catch { /* silent — non-critical */ }
  }
  loadFxHint();

  $("#recommend-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const need = $("#rec-need").value.trim();
    const budget = $("#rec-budget").value.trim();
    const out = $("#recommend-results");
    out.innerHTML = `<div class="glass panel"><p class="empty-note">Prism is ranking options…</p></div>`;
    logHistory("recommend", need);
    bumpCategory(guessCategory(need));
    if (!PRISM_ON) { out.innerHTML = `<div class="glass panel"><p class="empty-note">⚠️ Prism is offline — set GROQ_API_KEY.</p></div>`; return; }
    try {
      const data = await postJSON("/api/ai/recommend", { need, budget });
      out.innerHTML = `
        <div class="glass result-card">
          <div class="result-card__verdict">${escapeHtml(data.summary || "")}</div>
          <div class="pick-grid">${(data.picks || []).map((p) => wrapPack(`
            <div class="pick">
              <span class="pick__tag">${escapeHtml(p.tag || ("Rank " + p.rank))}</span>
              <span class="pick__name">${escapeHtml(p.name || "")}</span>
              <span class="pick__price">${escapeHtml(p.price || "")}</span>
              <p class="pick__why">${escapeHtml(p.why || "")}</p>
              <div class="pick__lists">
                <div><b>Performance</b><p style="color:var(--ink-dim)">${escapeHtml(p.performance || "")}</p></div>
              </div>
              <div class="pick__lists">
                <div><b>Battery</b><p style="color:var(--ink-dim)">${escapeHtml(p.battery || "")}</p></div>
                <div><b>Long-term</b><p style="color:var(--ink-dim)">${escapeHtml(p.long_term || "")}</p></div>
              </div>
              ${marketBar(p.marketplace_links)}
              <p class="pick__verify">⚠ Estimate from Prism — confirm live price &amp; stock before paying.</p>
              <div class="pick__actions">
                <button data-act="accept" data-name="${escapeHtml(p.name || "")}" data-price="${escapeHtml(p.price || "")}">Accept pick</button>
              </div>
            </div>`, p.tag || ("Rank " + p.rank))).join("")}</div>
        </div>`;
      initPacks(out);
      maybeAward("First AI recommendation");
    } catch (err) {
      out.innerHTML = `<div class="glass panel"><p class="empty-note">⚠️ ${escapeHtml(err.message)}</p></div>`;
    }
  });
  $("#recommend-results").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act='accept']");
    if (!btn) return;
    addToWishlist(btn.dataset.name, btn.dataset.price);
    bumpStat("accepted");
    toast("Added to wishlist and counted as an accepted recommendation.");
  });

  /* ================================================================
     WISHLIST / SAVED PRODUCTS
     ================================================================ */
  function addToWishlist(name, price) {
    if (!name) return;
    if (state.wishlist.some((w) => w.name === name)) { toast(`${name} is already on your wishlist.`); return; }
    const optimistic = { id: "tmp_" + Date.now(), name, price: price || "—", addedAt: Date.now() };
    state.wishlist.push(optimistic);
    cacheSnapshot();
    renderWishlist();
    renderStats();
    toast(`Added ${name} to wishlist.`);
    maybeAward("First item wishlisted");
    postJSON("/api/wishlist", { name, price: price || "—" })
      .then((saved) => {
        const idx = state.wishlist.indexOf(optimistic);
        if (idx !== -1) state.wishlist[idx] = saved;
        cacheSnapshot();
        renderWishlist();
      })
      .catch((err) => {
        // Rejected server-side (e.g. duplicate added from another tab/
        // device) — drop the optimistic entry and let the shopper know.
        state.wishlist = state.wishlist.filter((w) => w !== optimistic);
        cacheSnapshot();
        renderWishlist();
        renderStats();
        toast("⚠️ " + err.message);
      });
  }

  const CATEGORY_ICONS = {
    Laptop: "💻", Phone: "📱", Car: "🚗", Apartment: "🏠", Camera: "📷",
    Headphone: "🎧", Gaming: "🎮", Tv: "📺", Watch: "⌚", General: "🛍️",
  };

  function userInterestList() {
    return (userData.interests || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  }
  function matchesInterest(text) {
    const interests = userInterestList();
    if (!interests.length || !text) return false;
    const t = text.toLowerCase();
    return interests.some((i) => i && t.includes(i));
  }

  function renderWishlist() {
    const grid = $("#wishlist-grid");
    const savedGrid = $("#saved-grid");
    if (!state.wishlist.length) {
      grid.innerHTML = `<p class="empty-note">Your wishlist is empty — add products from Search, Recommendations, or the Scanner.</p>`;
      savedGrid.innerHTML = `<p class="empty-note">Nothing saved yet.</p>`;
      return;
    }
    // Items matching the shopper's stated interests float to the top.
    const sorted = [...state.wishlist].sort((a, b) => {
      const am = matchesInterest(a.name) ? 1 : 0;
      const bm = matchesInterest(b.name) ? 1 : 0;
      return bm - am;
    });
    const cardsHtml = sorted.map((w) => {
      const category = guessCategory(w.name);
      const icon = CATEGORY_ICONS[category] || CATEGORY_ICONS.General;
      const matched = matchesInterest(w.name) || matchesInterest(category);
      return `
      <div class="glass item-card${matched ? " item-card--match" : ""}">
        <div class="item-card__top">
          <span class="item-card__icon">${icon}</span>
          <span class="item-card__name">${escapeHtml(w.name)}</span>
          <button class="item-card__remove" data-remove="${w.id}">✕</button>
        </div>
        ${matched ? `<span class="item-card__badge">✨ Matches your interests</span>` : ""}
        <span class="item-card__price">${escapeHtml(w.price)}</span>
        <span class="item-card__meta">${escapeHtml(category)} · Added ${new Date(w.addedAt).toLocaleDateString()}</span>
        <div class="item-card__bar"><span style="width:${matched ? 88 : (30 + (w.id.length % 50))}%"></span></div>
      </div>`;
    }).join("");
    grid.innerHTML = cardsHtml;
    savedGrid.innerHTML = cardsHtml;
  }
  $("#wishlist-grid").addEventListener("click", removeWishlistHandler);
  $("#saved-grid").addEventListener("click", removeWishlistHandler);
  function removeWishlistHandler(e) {
    const btn = e.target.closest("button[data-remove]");
    if (!btn) return;
    const id = btn.dataset.remove;
    state.wishlist = state.wishlist.filter((w) => w.id !== id);
    cacheSnapshot();
    renderWishlist();
    renderStats();
    if (!id.startsWith("tmp_")) deleteJSON(`/api/wishlist/${encodeURIComponent(id)}`).catch(() => {});
  }
  $("#wishlist-add-btn").addEventListener("click", () => {
    const name = prompt("Product name to add to wishlist:");
    if (name) addToWishlist(name.trim(), prompt("Approx. price (optional):") || "—");
  });

  /* ================================================================
     PRICE ALERTS
     ================================================================ */
  $("#alert-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const product = $("#alert-product").value.trim();
    const price = $("#alert-price").value.trim();
    if (!product || !price) return;
    const optimistic = { id: "tmp_" + Date.now(), product, price, createdAt: Date.now() };
    state.alerts.push(optimistic);
    cacheSnapshot();
    $("#alert-form").reset();
    renderAlerts();
    renderStats();
    toast(`Watching ${product} for ${price}.`);
    maybeAward("First price alert set");
    postJSON("/api/alerts", { product, price })
      .then((saved) => {
        const idx = state.alerts.indexOf(optimistic);
        if (idx !== -1) state.alerts[idx] = saved;
        cacheSnapshot();
        renderAlerts();
      })
      .catch(() => {});
  });
  function renderAlerts() {
    const grid = $("#alerts-grid");
    if (!state.alerts.length) { grid.innerHTML = `<p class="empty-note">No active alerts yet.</p>`; return; }
    grid.innerHTML = state.alerts.map((a) => `
      <div class="glass item-card">
        <div class="item-card__top">
          <span class="item-card__name">${escapeHtml(a.product)}</span>
          <button class="item-card__remove" data-remove-alert="${a.id}">✕</button>
        </div>
        <span class="item-card__price">Target: ${escapeHtml(a.price)}</span>
        <span class="item-card__meta">Since ${new Date(a.createdAt).toLocaleDateString()}</span>
      </div>`).join("");
  }
  $("#alerts-grid").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-remove-alert]");
    if (!btn) return;
    const id = btn.dataset.removeAlert;
    state.alerts = state.alerts.filter((a) => a.id !== id);
    cacheSnapshot();
    renderAlerts();
    renderStats();
    if (!id.startsWith("tmp_")) deleteJSON(`/api/alerts/${encodeURIComponent(id)}`).catch(() => {});
  });

  /* ================================================================
     HISTORY
     ================================================================ */
  function renderHistory() {
    const list = $("#history-list");
    if (!state.history.length) { list.innerHTML = `<p class="empty-note">No activity yet.</p>`; return; }
    list.innerHTML = state.history.map((h) => `
      <div class="history-item">
        <div><span class="history-item__type">${escapeHtml(h.type)}</span>${escapeHtml(h.text)}</div>
        <time>${new Date(h.ts).toLocaleString()}</time>
      </div>`).join("");
  }
  $("#history-clear-btn").addEventListener("click", () => {
    state.history = [];
    cacheSnapshot();
    renderHistory();
    renderRecent();
    deleteJSON("/api/history").catch(() => {});
  });

  /* ================================================================
     PROFILE
     ================================================================ */
  function renderProfile() {
    $("#pf-saved").textContent = currencySymbol() + (state.stats.saved || 0).toLocaleString();
    $("#pf-searched").textContent = state.stats.searches || 0;
    const topCat = Object.entries(state.stats.categories || {}).sort((a, b) => b[1] - a[1])[0];
    $("#pf-category").textContent = topCat ? topCat[0] : "—";
    $("#pf-achievements").textContent = state.achievements.length;
    $("#achv-row").innerHTML = state.achievements.length
      ? state.achievements.map((a) => `<div class="achv">🏆 <b>${escapeHtml(a)}</b></div>`).join("")
      : `<p class="empty-note">Use Prism across the dashboard to unlock achievements.</p>`;
  }

  /* ================================================================
     ANALYTICS CHARTS
     ================================================================ */
  let chartSearches, chartCategories;
  function renderCharts() {
    if (!window.Chart) return;
    const days = [...Array(7)].map((_, i) => {
      const d = new Date(); d.setDate(d.getDate() - (6 - i));
      return d.toLocaleDateString(undefined, { weekday: "short" });
    });
    const counts = days.map(() => 0);
    state.history.forEach((h) => {
      const diffDays = Math.floor((Date.now() - h.ts) / 86400000);
      if (diffDays >= 0 && diffDays < 7) counts[6 - diffDays]++;
    });

    const ctx1 = $("#chart-searches");
    if (ctx1) {
      chartSearches?.destroy();
      chartSearches = new Chart(ctx1, {
        type: "line",
        data: { labels: days, datasets: [{ data: counts, borderColor: "#4CE0FF", backgroundColor: "rgba(76,224,255,.12)", fill: true, tension: 0.4, pointRadius: 3 }] },
        options: { plugins: { legend: { display: false } }, scales: { x: { grid: { color: "rgba(255,255,255,.05)" }, ticks: { color: "#8B93AE" } }, y: { grid: { color: "rgba(255,255,255,.05)" }, ticks: { color: "#8B93AE", precision: 0 } } } },
      });
    }

    const cats = Object.entries(state.stats.categories || {});
    const ctx2 = $("#chart-categories");
    if (ctx2) {
      chartCategories?.destroy();
      chartCategories = new Chart(ctx2, {
        type: "doughnut",
        data: {
          labels: cats.length ? cats.map((c) => c[0]) : ["No data yet"],
          datasets: [{ data: cats.length ? cats.map((c) => c[1]) : [1], backgroundColor: ["#4CE0FF", "#9D6BFF", "#2E6BFF", "#7CF7B0", "#FF8FE0", "#8B93AE"] }],
        },
        options: { plugins: { legend: { position: "bottom", labels: { color: "#8B93AE", boxWidth: 10, font: { size: 11 } } } } },
      });
    }
  }

  /* ================================================================
     SCAM CHECK (surfaced inside AI Assistant flow via bar camera icon reused for URL paste)
     ================================================================ */
  window.prismScamCheck = async function (url) {
    showView("assistant");
    addBubble("user", "Check this listing for scam risk: " + escapeHtml(url));
    const bubble = addBubble("model", '<span class="chat__cursor"></span>');
    try {
      const data = await postJSON("/api/ai/scam-check", { url });
      bubble.innerHTML = renderMarkdown(
        `**Risk score: ${data.risk_score}/100 — ${data.risk_label}**\n\n` +
        `${data.summary}\n\n**Red flags**\n` + (data.red_flags || []).map((f) => `- ${f}`).join("\n") +
        `\n\n**Safety tips**\n` + (data.safety_tips || []).map((f) => `- ${f}`).join("\n")
      );
    } catch (err) {
      bubble.innerHTML = renderMarkdown("⚠️ " + err.message);
    }
  };

  /* ================================================================
     VOICE INPUT (Web Speech API)
     ================================================================ */
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  function attachVoice(button, onResult) {
    if (!SpeechRecognition) { button.title = "Voice input not supported in this browser"; return; }
    let recognizing = false;
    let recognizer;
    button.addEventListener("click", () => {
      if (recognizing) { recognizer?.stop(); return; }
      recognizer = new SpeechRecognition();
      recognizer.lang = "en-US";
      recognizer.interimResults = false;
      recognizer.onstart = () => { recognizing = true; button.classList.add("is-listening"); };
      recognizer.onend = () => { recognizing = false; button.classList.remove("is-listening"); };
      recognizer.onerror = () => { recognizing = false; button.classList.remove("is-listening"); };
      recognizer.onresult = (e) => { onResult(e.results[0][0].transcript); };
      recognizer.start();
    });
  }
  attachVoice($("#chat-mic-btn"), (text) => { chatInput.value = text; chatForm.requestSubmit(); });

  /* ================================================================
     SETTINGS
     ================================================================ */
  const THEME_CLASSES = ["theme-aurora", "theme-ember", "theme-frost"];
  function applyTheme(theme) {
    THEME_CLASSES.forEach((c) => document.body.classList.remove(c));
    if (theme && theme !== "void") document.body.classList.add(`theme-${theme}`);
    $$("#theme-seg .seg__opt").forEach((b) => b.classList.toggle("is-active", b.dataset.theme === theme));
  }
  $$("#theme-seg .seg__opt").forEach((btn) => btn.addEventListener("click", () => {
    applyTheme(btn.dataset.theme);
    Store.set("theme", btn.dataset.theme);
  }));
  (function initTheme() {
    applyTheme(Store.get("theme", "void"));
  })();

  /* ---------------- currency / country (Settings) ---------------- */
  const currencySelect = $("#currency-select");
  const countrySelect = $("#country-select");
  async function saveLocaleSetting(field, value) {
    const fd = new FormData();
    fd.append(field, value);
    try {
      const res = await fetch("/api/profile/update", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Couldn't save that setting.");
      if (data.currency) userData.currency = data.currency;
      if (data.country) userData.country = data.country;
      renderStats();
      renderProfile();
      loadFxHint();
      toast(field === "currency" ? "Currency updated." : "Country updated.");
    } catch (err) {
      toast("⚠️ " + err.message);
    }
  }
  currencySelect?.addEventListener("change", () => saveLocaleSetting("currency", currencySelect.value));
  countrySelect?.addEventListener("change", () => saveLocaleSetting("country", countrySelect.value));

  $("#delete-account-btn").addEventListener("click", async () => {
    if (!confirm("This will permanently clear your saved Prism data (wishlist, history, alerts, achievements, stats). Continue?")) return;
    try {
      await deleteJSON("/api/data/clear");
      localStorage.removeItem("prism:cache:snapshot");
      toast("Your saved data has been cleared.");
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      toast("⚠️ " + err.message);
    }
  });

  /* ---------------- profile edit (name, interests, picture) ---------------- */
  const profileEditForm = $("#profile-edit-form");
  const profileEditPicBtn = $("#profile-edit-pic-btn");
  const profileEditPicInput = $("#profile-edit-pic-input");
  let pendingProfilePicFile = null;

  profileEditPicBtn.addEventListener("click", (e) => {
    // clicking the label already opens the file picker via the <label>/<input>
    // relationship, but stopPropagation avoids double-triggers on some browsers.
  });
  profileEditPicInput.addEventListener("change", () => {
    const file = profileEditPicInput.files[0];
    if (!file) return;
    pendingProfilePicFile = file;
    const reader = new FileReader();
    reader.onload = () => {
      const html = `<img src="${reader.result}" alt="Preview">`;
      $("#settings-avatar").innerHTML = html;
    };
    reader.readAsDataURL(file);
  });

  profileEditForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const saveBtn = $("#profile-edit-save");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";

    const fd = new FormData();
    fd.append("full_name", $("#profile-edit-name").value.trim());
    fd.append("interests", $("#profile-edit-interests").value.trim());
    if (pendingProfilePicFile) fd.append("profile_picture", pendingProfilePicFile);

    try {
      const res = await fetch("/api/profile/update", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Couldn't save your changes.");

      userData.fullName = data.full_name;
      userData.firstName = data.first_name;
      userData.interests = data.interests;
      userData.profilePictureUrl = data.profile_picture_url;
      pendingProfilePicFile = null;

      refreshAvatarsEverywhere();
      const nameEl = $("#profile-name");
      if (nameEl) nameEl.textContent = data.full_name;
      const interestsEl = $("#profile-interests");
      if (interestsEl) interestsEl.textContent = data.interests || "No shopping interests set yet — add them in Settings.";
      renderWishlist();
      toast("Profile updated.");
    } catch (err) {
      toast("⚠️ " + err.message);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save changes";
    }
  });

  /* ================================================================
     PARTICLE BACKGROUND (lightweight canvas)
     ================================================================ */
  (function particles() {
    const canvas = $("#os-canvas");
    const ctx = canvas.getContext("2d");
    let w, h, particlesArr;
    function resize() {
      w = canvas.width = canvas.offsetWidth * devicePixelRatio;
      h = canvas.height = canvas.offsetHeight * devicePixelRatio;
    }
    function init() {
      resize();
      const count = Math.min(70, Math.floor((w * h) / 60000));
      particlesArr = [...Array(count)].map(() => ({
        x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25,
        r: Math.random() * 1.6 + 0.4,
        c: Math.random() > 0.5 ? "76,224,255" : "157,107,255",
      }));
    }
    function tick() {
      ctx.clearRect(0, 0, w, h);
      particlesArr.forEach((p) => {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * devicePixelRatio, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${p.c},0.5)`;
        ctx.fill();
      });
      requestAnimationFrame(tick);
    }
    window.addEventListener("resize", resize);
    init();
    tick();
  })();

  /* ================================================================
     AI COPILOT TOOLS — 14 focused tools sharing one tile/panel/form
     shell and a small set of result-rendering primitives (stat tiles,
     check lists, timelines) so each tool only has to define its inputs
     and how to map its JSON response onto those primitives.
     ================================================================ */
  function statTile(label, value, opts = {}) {
    return `<div class="stat-tile${opts.accent ? " stat-tile--accent" : ""}">
      <span class="stat-tile__label">${escapeHtml(label)}</span>
      <span class="stat-tile__value">${escapeHtml(value == null || value === "" ? "—" : String(value))}</span>
      ${opts.sub ? `<span class="stat-tile__sub">${escapeHtml(opts.sub)}</span>` : ""}
    </div>`;
  }
  function statGrid(tiles) { return `<div class="stat-tile-grid">${tiles.join("")}</div>`; }
  function checkList(items) {
    const ICON = { good: "✓", compatible: "✓", warn: "⚠", bad: "✕", needs_upgrade: "⚠", unknown: "•" };
    return `<div class="check-list">${(items || []).map((it) => {
      const status = (it.status || "unknown").toLowerCase();
      const icon = ICON[status] || "•";
      const prefix = it.component ? `<b>${escapeHtml(it.component)}:</b> ` : "";
      return `<div class="check-item" data-status="${escapeHtmlAttr(status)}"><span class="check-item__mark">${icon}</span><span>${prefix}${escapeHtml(it.note || it.text || "")}</span></div>`;
    }).join("")}</div>`;
  }
  function timelineList(events) {
    return `<div class="timeline-list">${(events || []).map((ev) => `
      <div class="timeline-item">
        <span class="timeline-item__year">${escapeHtml(ev.year || "")}</span>
        <div class="timeline-item__body"><span class="timeline-item__label">${escapeHtml(ev.label || "")}</span></div>
      </div>`).join("")}</div>`;
  }
  function copilotMsg(text) { return text ? `<div class="copilot-message">${escapeHtml(text)}</div>` : ""; }

  const TOOLS = [
    {
      id: "negotiate", icon: "🤝", title: "AI Negotiator",
      blurb: "Turn any listing into a negotiation strategy and a ready-to-send message.",
      endpoint: "/api/ai/negotiate",
      fields: [{ name: "listing", label: "Paste the listing (price + description)", type: "textarea",
        placeholder: "e.g. iPhone 13 Pro Max 256GB, asking ₦950,000, lightly used, box included…" }],
      render(d) {
        return statGrid([
          statTile("Seller wants", d.seller_price),
          statTile("Market value", (d.market_low && d.market_high) ? `${d.market_low} – ${d.market_high}` : "—"),
          statTile("Recommended offer", d.recommended_offer, { accent: true }),
          statTile("Negotiation chance", d.negotiation_chance != null ? `${d.negotiation_chance}%` : "—"),
        ]) + copilotMsg(d.reasoning) + (d.message ? `<div class="copilot-message">💬 “${escapeHtml(d.message)}”</div>` : "");
      },
    },
    {
      id: "fake-listing", icon: "🕵️", title: "Fake Listing Detector",
      blurb: "Score a listing's authenticity and spot red flags before you pay.",
      endpoint: "/api/ai/fake-listing",
      fields: [{ name: "listing", label: "Paste the listing text", type: "textarea",
        placeholder: "Full description, price, seller notes…" }],
      render(d) {
        return statGrid([
          statTile("Authenticity score", d.authenticity_score != null ? `${d.authenticity_score}%` : "—", { accent: true }),
          statTile("Risk", d.risk_label),
        ]) + checkList(d.checks) + copilotMsg(d.summary);
      },
    },
    {
      id: "shopping-agent", icon: "🛰️", title: "AI Shopping Agent",
      blurb: "Send Prism to check every marketplace and bring back the single best deal.",
      endpoint: "/api/ai/shopping-agent",
      fields: [
        { name: "need", label: "What should Prism go find?", type: "text", placeholder: "iPhone 16 Pro under ₦1.6M" },
        { type: "row", fields: [
          { name: "budget", label: "Budget (optional)", type: "text", placeholder: "1,600,000" },
          { name: "currency", label: "Currency", type: "select", options: ["NGN", "USD", "GBP", "EUR"] },
        ]},
      ],
      beforeRender() { return `<p class="empty-note">Checking Jumia, Konga, Amazon, AliExpress, eBay, Temu…</p>`; },
      render(d) {
        return statGrid([
          statTile("Best deal", d.product),
          statTile("Store", d.best_store, { accent: true }),
          statTile("Price", d.best_price),
          statTile("Market price", d.market_price),
          statTile("You save", d.savings),
          statTile("Delivery", d.delivery_estimate),
        ]) + copilotMsg(d.why_this_store) + marketBar(d.marketplace_links);
      },
    },
    {
      id: "budget-planner", icon: "🧮", title: "AI Budget Planner",
      blurb: "Hand Prism a lump budget and a goal — it splits it into a real shopping list.",
      endpoint: "/api/ai/budget-planner",
      fields: [
        { name: "goal", label: "What are you budgeting for?", type: "text", placeholder: "Building a gaming PC setup" },
        { type: "row", fields: [
          { name: "budget", label: "Total budget", type: "text", placeholder: "900,000" },
          { name: "currency", label: "Currency", type: "select", options: ["NGN", "USD", "GBP", "EUR"] },
        ]},
      ],
      render(d) {
        const items = (d.items || []).map((it) => `<li><span>${escapeHtml(it.name)}</span><span>${escapeHtml(it.price)}</span></li>`).join("");
        return `<div class="copilot-plan-cat"><ul>${items}</ul></div>` +
          statGrid([statTile("Remaining", d.remaining, { accent: true })]) + copilotMsg(d.notes);
      },
    },
    {
      id: "buy-or-wait", icon: "⏳", title: "Buy or Wait Predictor",
      blurb: "Should you buy this now, or hold off for a better price?",
      endpoint: "/api/ai/buy-or-wait",
      fields: [{ name: "product", label: "Product", type: "text", placeholder: "RTX 5080" }],
      render(d) {
        const pct = d.predicted_30d_movement_pct;
        const pctLabel = typeof pct === "number" ? `${pct > 0 ? "+" : ""}${pct}%` : "—";
        return statGrid([
          statTile("Current price", d.current_price),
          statTile("Fair market value", d.fair_market_value),
          statTile("Predicted 30-day movement", pctLabel, { accent: typeof pct === "number" && pct < 0 }),
          statTile("Recommendation", d.recommendation || d.verdict, { accent: true }),
          statTile("Confidence", d.confidence != null ? `${d.confidence}%` : "—"),
          statTile("Estimated savings", d.estimated_savings),
        ]) + copilotMsg(d.reason) + marketBar(d.marketplace_links);
      },
    },
    {
      id: "scam-messages", icon: "🚩", title: "AI Scam Detector",
      blurb: "Paste a seller's chat messages and check them for scam patterns.",
      endpoint: "/api/ai/scam-messages",
      fields: [{ name: "messages", label: "Paste the seller's messages", type: "textarea",
        placeholder: "e.g. 'Pay first before I can arrange delivery, I have another buyer waiting...'" }],
      render(d) {
        return statGrid([statTile("Scam probability", d.scam_probability != null ? `${d.scam_probability}%` : "—", { accent: true })])
          + checkList((d.reasons || []).map((r) => ({ status: "bad", note: r })))
          + copilotMsg(d.recommendation);
      },
    },
    {
      id: "lifespan", icon: "🔧", title: "Product Life Expectancy",
      blurb: "How long will this actually last, and what's it worth down the line?",
      endpoint: "/api/ai/lifespan",
      fields: [{ name: "product", label: "Product", type: "text", placeholder: "MacBook Air M4" }],
      render(d) {
        return statGrid([
          statTile("Average lifespan", d.average_lifespan),
          statTile("Battery", d.battery_cycles),
          statTile("Repairability", d.repairability),
          statTile("Software support", d.software_support),
          statTile("Resale after 3y", d.resale_after_3y, { accent: true }),
        ]) + marketBar(d.marketplace_links);
      },
    },
    {
      id: "deal-hunter", icon: "🌅", title: "AI Deal Hunter",
      blurb: "A quick digest of today's best deals, based on your interests.",
      endpoint: "/api/ai/deal-hunter",
      fields: [],
      submitLabel: "Get today's deals",
      render(d) {
        const rows = (d.deals || []).map((deal) => `
          <li>
            <span>${escapeHtml(deal.name)}</span>
            <span>${escapeHtml(deal.detail || "")}${deal.urgency ? ` · <i>${escapeHtml(deal.urgency)}</i>` : ""}</span>
          </li>`).join("");
        return `<div class="copilot-plan-cat"><ul>${rows || "<li>No deals right now — check back later.</li>"}</ul></div>`;
      },
    },
    {
      id: "resale-predictor", icon: "📉", title: "AI Resale Predictor",
      blurb: "Project what a product will be worth 1, 2, and 3 years from now.",
      endpoint: "/api/ai/resale-predictor",
      fields: [
        { name: "product", label: "Product", type: "text", placeholder: "iPhone 16 Pro" },
        { type: "row", fields: [
          { name: "buy_price", label: "Buy price (optional)", type: "text", placeholder: "1,800,000" },
          { name: "currency", label: "Currency", type: "select", options: ["NGN", "USD", "GBP", "EUR"] },
        ]},
      ],
      render(d) {
        return statGrid([
          statTile("Buy price", d.buy_price),
          statTile("After 1 year", d.value_1y),
          statTile("After 2 years", d.value_2y),
          statTile("After 3 years", d.value_3y),
          statTile("Depreciation", d.depreciation_label, { accent: true }),
        ]) + copilotMsg(d.notes);
      },
    },
    {
      id: "compatibility", icon: "🖥️", title: "AI Compatibility Checker",
      blurb: "Check a new part against the PC you already have.",
      endpoint: "/api/ai/compatibility",
      fields: [
        { name: "product", label: "Part you want to add", type: "text", placeholder: "RTX 5080" },
        { name: "system", label: "Your current system", type: "textarea",
          placeholder: "e.g. Ryzen 5 5600, 550W PSU, NZXT H510 case, B450 motherboard" },
      ],
      render(d) {
        return checkList(d.checks) + statGrid([statTile("Estimated upgrade cost", d.estimated_upgrade_cost, { accent: true })]) + copilotMsg(d.summary);
      },
    },
    {
      id: "timeline", icon: "📈", title: "AI Product Timeline",
      blurb: "A visual history of a product's price and reputation over time.",
      endpoint: "/api/ai/timeline",
      fields: [{ name: "product", label: "Product", type: "text", placeholder: "PS5" }],
      render(d) { return timelineList(d.events); },
    },
    {
      id: "community", icon: "👥", title: "AI Buyer Community",
      blurb: "What do other owners of this product tend to say?",
      endpoint: "/api/ai/community",
      fields: [{ name: "product", label: "Product", type: "text", placeholder: "Sony WH-1000XM6" }],
      render(d) {
        return statGrid([
          statTile("Would recommend", d.recommend_pct != null ? `${d.recommend_pct}%` : "—", { accent: true }),
          statTile("Most common complaint", d.most_common_complaint),
          statTile("Most loved feature", d.most_loved_feature),
        ]) + copilotMsg(d.summary);
      },
    },
    {
      id: "score", icon: "⭐", title: "AI Shopping Score",
      blurb: "A multi-axis scorecard: value, performance, repairability, future-proofing.",
      endpoint: "/api/ai/score",
      fields: [{ name: "product", label: "Product", type: "text", placeholder: "Dell XPS 15" }],
      render(d) {
        const stars = (n) => "★".repeat(Math.round(n || 0) / 2) + "☆".repeat(5 - Math.round(n || 0) / 2);
        return statGrid([
          statTile("Value", d.value), statTile("Performance", d.performance),
          statTile("Repairability", d.repairability), statTile("Future-proof", d.future_proof),
          statTile("Overall", d.overall, { accent: true, sub: typeof d.overall === "number" ? stars(d.overall) : "" }),
        ]) + copilotMsg(d.summary);
      },
    },
    {
      id: "copilot", icon: "🧭", title: "AI Shopping Copilot",
      blurb: "Describe a life situation — get a full categorized shopping plan.",
      endpoint: "/api/ai/copilot",
      fields: [
        { name: "situation", label: "Describe the situation", type: "textarea", placeholder: "I'm moving into my first apartment" },
        { type: "row", fields: [
          { name: "budget", label: "Budget (optional)", type: "text", placeholder: "2,850,000" },
          { name: "currency", label: "Currency", type: "select", options: ["NGN", "USD", "GBP", "EUR"] },
        ]},
      ],
      render(d) {
        const cats = (d.categories || []).map((cat) => `
          <div class="copilot-plan-cat">
            <div class="copilot-plan-cat__name">${escapeHtml(cat.name)}</div>
            <ul>${(cat.items || []).map((it) => `<li><span>${escapeHtml(it.name)}</span><span>${"★".repeat(it.priority || 3)}</span></li>`).join("")}</ul>
          </div>`).join("");
        const phases = (d.buy_order || []).map((p) => `
          <div class="copilot-plan-phase"><span class="copilot-plan-phase__name">${escapeHtml(p.phase)}</span><span>${(p.items || []).map(escapeHtml).join(", ")}</span></div>`).join("");
        return statGrid([statTile("Estimated cost", d.estimated_cost, { accent: true })]) + cats +
          (phases ? `<div class="copilot-plan-cat"><div class="copilot-plan-cat__name">Best order to buy</div>${phases}</div>` : "");
      },
    },
  ];

  function toolFieldHtml(f, toolId) {
    if (f.type === "row") return `<div class="tool-form__row">${f.fields.map((sf) => toolFieldHtml(sf, toolId)).join("")}</div>`;
    const id = `tool-${toolId}-${f.name}`;
    if (f.type === "textarea") return `<label for="${id}">${escapeHtml(f.label)}<textarea id="${id}" name="${f.name}" rows="3" placeholder="${escapeHtmlAttr(f.placeholder || "")}"></textarea></label>`;
    if (f.type === "select") return `<label for="${id}">${escapeHtml(f.label)}<select id="${id}" name="${f.name}">${f.options.map((o) => `<option value="${escapeHtmlAttr(o)}">${escapeHtml(o)}</option>`).join("")}</select></label>`;
    return `<label for="${id}">${escapeHtml(f.label)}<input type="text" id="${id}" name="${f.name}" placeholder="${escapeHtmlAttr(f.placeholder || "")}"></label>`;
  }

  function collectToolFields(fields, form) {
    const flat = [];
    fields.forEach((f) => (f.type === "row" ? flat.push(...f.fields) : flat.push(f)));
    const out = {};
    flat.forEach((f) => { out[f.name] = (form.querySelector(`[name="${f.name}"]`)?.value || "").trim(); });
    return out;
  }

  function renderToolGrid() {
    const grid = $("#tool-grid");
    if (!grid || grid.dataset.built) return;
    grid.dataset.built = "1";
    grid.innerHTML = TOOLS.map((tool) => `
      <div class="tool-tile" data-tool="${tool.id}">
        <button type="button" class="tool-tile__head">
          <span class="tool-tile__icon">${tool.icon}</span>
          <span>
            <div class="tool-tile__title">${escapeHtml(tool.title)}</div>
            <div class="tool-tile__blurb">${escapeHtml(tool.blurb)}</div>
          </span>
          <span class="tool-tile__chev">⌄</span>
        </button>
        <div class="tool-panel">
          <div class="tool-panel__inner">
            <form class="tool-form" data-tool-form="${tool.id}">
              ${tool.fields.map((f) => toolFieldHtml(f, tool.id)).join("")}
              <button type="submit">${escapeHtml(tool.submitLabel || "Run " + tool.title)}</button>
            </form>
            <div class="tool-result" id="tool-result-${tool.id}"></div>
          </div>
        </div>
      </div>`).join("");

    $$(".tool-tile__head", grid).forEach((head) => {
      head.addEventListener("click", () => head.closest(".tool-tile").classList.toggle("is-open"));
    });

    $$(".tool-form", grid).forEach((form) => {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const toolId = form.dataset.toolForm;
        const tool = TOOLS.find((t) => t.id === toolId);
        const resultEl = $("#tool-result-" + toolId);
        const btn = form.querySelector("button[type='submit']");
        const payload = collectToolFields(tool.fields, form);
        if (!PRISM_ON) { resultEl.innerHTML = `<p class="empty-note">⚠️ Prism is offline — set GROQ_API_KEY on the server.</p>`; return; }
        btn.disabled = true;
        const originalLabel = btn.textContent;
        btn.textContent = "Thinking…";
        resultEl.innerHTML = tool.beforeRender ? tool.beforeRender() : `<p class="empty-note">Prism is working on it…</p>`;
        try {
          const data = await postJSON(tool.endpoint, payload);
          resultEl.innerHTML = tool.render(data);
          logHistory("tool:" + toolId, tool.title + (payload.product || payload.need || payload.goal || payload.situation ? " — " + (payload.product || payload.need || payload.goal || payload.situation) : ""));
          bumpCategory(tool.title);
          maybeAward("First Copilot tool used");
        } catch (err) {
          resultEl.innerHTML = `<p class="empty-note">⚠️ ${escapeHtml(err.message)}</p>`;
        } finally {
          btn.disabled = false;
          btn.textContent = originalLabel;
        }
      });
    });
  }

  let memoryLoaded = false;
  async function renderMemoryCard() {
    if (memoryLoaded) return;
    memoryLoaded = true;
    const el = $("#copilot-memory-notes");
    if (!el) return;
    try {
      const data = await getJSON("/api/memory/profile");
      el.innerHTML = (data.notes || []).map((n) => `<div class="memory-note">${escapeHtml(n)}</div>`).join("");
    } catch {
      el.innerHTML = `<p class="empty-note">Couldn't load your shopping memory right now.</p>`;
    }
  }

  renderToolGrid();

  /* ================================================================
     init
     ================================================================ */
  loadFromServer();
  startTrendingLoop();
})();