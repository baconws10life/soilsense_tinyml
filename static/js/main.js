// SoilSense — shared client behaviour (no build step, no external deps)
(function () {
  "use strict";

  // ---- mobile sidebar toggle -------------------------------------------
  const navToggle = document.getElementById("navToggle");
  const sidebar = document.getElementById("sidebar");
  if (navToggle && sidebar) {
    navToggle.addEventListener("click", () => sidebar.classList.toggle("open"));
    document.addEventListener("click", (e) => {
      if (sidebar.classList.contains("open") && !sidebar.contains(e.target) && e.target !== navToggle) {
        sidebar.classList.remove("open");
      }
    });
  }

  // ---- SVG ring gauges: <div class="X-ring" data-percent="72" data-size="148" data-stroke="9"> ---
  function paintRing(el) {
    const percent = Math.max(0, Math.min(100, parseFloat(el.dataset.percent || "0")));
    const size = parseFloat(el.dataset.size || "60");
    const stroke = parseFloat(el.dataset.stroke || "6");
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const offset = c - (percent / 100) * c;
    el.innerHTML = `
      <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle class="ring-bg" cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="${stroke}"/>
        <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${el.dataset.color || 'var(--accent)'}"
          stroke-width="${stroke}" stroke-linecap="round"
          stroke-dasharray="${c}" stroke-dashoffset="${c}"
          style="transition: stroke-dashoffset .8s cubic-bezier(.4,0,.2,1); filter: drop-shadow(0 0 5px rgba(124,250,107,.35));"/>
      </svg>`;
    // animate in on next frame
    requestAnimationFrame(() => {
      const fg = el.querySelector("circle:last-child");
      requestAnimationFrame(() => { fg.style.strokeDashoffset = offset; });
    });
  }
  document.querySelectorAll("[data-ring]").forEach(paintRing);

  // ---- Learning Guide accordion ------------------------------------------
  document.querySelectorAll(".accordion-head").forEach((head) => {
    head.addEventListener("click", () => {
      const acc = head.closest(".accordion");
      const wasOpen = acc.classList.contains("open");
      if (acc.dataset.group) {
        document.querySelectorAll(`.accordion[data-group="${acc.dataset.group}"]`).forEach((a) => a.classList.remove("open"));
      }
      acc.classList.toggle("open", !wasOpen);
    });
  });
  // open the first accordion by default
  const firstAcc = document.querySelector(".accordion");
  if (firstAcc) firstAcc.classList.add("open");

  // ---- Crop Picks: category filter / sort / suitable-only toggle --------
  const cropFilterForm = document.getElementById("cropFilters");
  if (cropFilterForm) {
    document.querySelectorAll("[data-category]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const url = new URL(window.location.href);
        url.searchParams.set("category", btn.dataset.category);
        window.location.href = url.toString();
      });
    });
    document.querySelectorAll("[data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const url = new URL(window.location.href);
        url.searchParams.set("sort", btn.dataset.sort);
        window.location.href = url.toString();
      });
    });
    const toggle = document.getElementById("suitableToggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        const url = new URL(window.location.href);
        const isOn = toggle.dataset.on === "1";
        url.searchParams.set("suitable", isOn ? "0" : "1");
        window.location.href = url.toString();
      });
    }
  }

  // ---- AI Assistant chat --------------------------------------------------
  const chatLog = document.getElementById("chatLog");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");

  function scrollChatToEnd() {
    if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
  }

  function addMessage(role, html) {
    if (!chatLog) return;
    const wrap = document.createElement("div");
    wrap.className = "msg " + (role === "ai" ? "msg-ai" : "msg-user");
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    wrap.innerHTML = `
      <div class="msg-avatar">${role === "ai" ? "AI" : "Y"}</div>
      <div>
        <div class="msg-bubble">${html}</div>
        <div class="msg-meta">${time}</div>
      </div>`;
    chatLog.appendChild(wrap);
    scrollChatToEnd();
  }

  async function sendToAssistant(message) {
    addMessage("user", escapeHtml(message));
    const typingId = "typing-" + Date.now();
    if (chatLog) {
      const typing = document.createElement("div");
      typing.className = "msg msg-ai";
      typing.id = typingId;
      typing.innerHTML = `<div class="msg-avatar">AI</div><div class="msg-bubble">Thinking…</div>`;
      chatLog.appendChild(typing);
      scrollChatToEnd();
    }
    try {
      const res = await fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      document.getElementById(typingId)?.remove();
      addMessage("ai", escapeHtml(data.reply || "Sorry, I couldn't find an answer for that."));
    } catch (err) {
      document.getElementById(typingId)?.remove();
      addMessage("ai", "I couldn't reach the assistant just now — please try again.");
    }
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  if (chatForm) {
    chatForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const val = chatInput.value.trim();
      if (!val) return;
      chatInput.value = "";
      sendToAssistant(val);
    });
  }
  document.querySelectorAll(".suggest-chip").forEach((chip) => {
    chip.addEventListener("click", () => sendToAssistant(chip.textContent.trim()));
  });

  // "Ask AI about ..." links (Sensors page) open this page with ?ask=<question>
  const preset = new URLSearchParams(window.location.search).get("ask");
  if (preset && chatLog) {
    history.replaceState(null, "", window.location.pathname);
    sendToAssistant(preset);
  }

  scrollChatToEnd();
})();
