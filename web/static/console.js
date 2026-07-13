/* Winfetti ops console. Everything goes through the /admin API with the
   X-Admin-Key header; the key lives in sessionStorage only. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const short = (uuid) => String(uuid).slice(0, 8);
  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  let key = sessionStorage.getItem("winfetti_admin_key") || "";

  function age(iso) {
    const mins = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
    if (mins < 60) return mins + "m";
    if (mins < 60 * 48) return Math.round(mins / 60) + "h";
    return Math.round(mins / 1440) + "d";
  }

  function showGate(message) {
    key = "";
    sessionStorage.removeItem("winfetti_admin_key");
    $("console").hidden = true;
    $("signout").hidden = true;
    $("gate").hidden = false;
    $("gate-msg").textContent = message || "";
  }

  async function admin(path, opts = {}) {
    const resp = await fetch(path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Key": key,
        ...(opts.headers || {}),
      },
    });
    if (resp.status === 403) showGate("Key rejected.");
    if (resp.status === 503) showGate("The admin API is disabled until ADMIN_API_KEY is set on the server.");
    return resp;
  }

  // ------------------------------------------------------------------
  // Tabs

  const TABS = ["queue", "fraud", "users", "jobs"];
  function switchTab(name) {
    for (const t of TABS) {
      $("tab-" + t).setAttribute("aria-selected", String(t === name));
      $("sec-" + t).hidden = t !== name;
    }
  }
  for (const t of TABS) $("tab-" + t).addEventListener("click", () => switchTab(t));

  function openUser(id) {
    switchTab("users");
    $("u-id").value = id;
    loadUser();
  }

  // ------------------------------------------------------------------
  // Queue

  async function loadQueue() {
    const status = $("q-status").value;
    const resp = await admin(`/admin/redemptions?status=${status}`);
    if (!resp.ok) return;
    const rows = await resp.json();
    $("q-empty").hidden = rows.length > 0;
    $("q-rows").innerHTML = rows
      .map((r) => {
        const actions =
          r.status === "pending"
            ? `<button class="btn sm" data-approve="${r.id}" type="button">Approve</button>
               <button class="btn danger sm" data-deny="${r.id}" type="button">Deny</button>`
            : r.status === "approved"
              ? `<button class="btn sm" data-approve="${r.id}" type="button">Retry now</button>`
              : "";
        return `<tr data-row="${r.id}">
          <td class="mono">${age(r.created_at)}</td>
          <td class="mono" title="${r.id}">${short(r.id)}</td>
          <td><button class="idlink" data-user="${r.user_id}" title="${r.user_id}" type="button">${short(r.user_id)}</button></td>
          <td>${esc(r.sku)}</td>
          <td class="num mono">${r.usd}</td>
          <td class="num mono">${fmt(r.coins)}</td>
          <td class="mono">${r.tremendous_order_id ? esc(short(r.tremendous_order_id)) : ""}</td>
          <td style="white-space:nowrap">${actions} <span class="rowmsg" data-msg="${r.id}" aria-live="polite"></span></td>
        </tr>`;
      })
      .join("");

    for (const btn of $("q-rows").querySelectorAll("[data-user]")) {
      btn.addEventListener("click", () => openUser(btn.dataset.user));
    }
    for (const btn of $("q-rows").querySelectorAll("[data-approve]")) {
      btn.addEventListener("click", () => approve(btn.dataset.approve, btn));
    }
    for (const btn of $("q-rows").querySelectorAll("[data-deny]")) {
      btn.addEventListener("click", () => deny(btn.dataset.deny, btn));
    }
  }

  async function approve(id, btn) {
    btn.disabled = true;
    const msg = document.querySelector(`[data-msg="${id}"]`);
    const resp = await admin(`/admin/redemptions/${id}/approve`, { method: "POST" });
    if (resp.ok) {
      await loadQueue();
    } else if (resp.status === 502) {
      msg.textContent = "Fulfillment failed. Left approved for the retry job.";
      msg.className = "rowmsg err";
      btn.disabled = false;
    } else {
      const body = await resp.json().catch(() => ({}));
      msg.textContent = body.detail || "Failed.";
      msg.className = "rowmsg err";
      btn.disabled = false;
    }
  }

  async function deny(id, btn) {
    // two-step confirm: deny refunds the hold, so make the click deliberate
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "Refund hold?";
      setTimeout(() => {
        btn.dataset.armed = "0";
        btn.textContent = "Deny";
      }, 3000);
      return;
    }
    btn.disabled = true;
    const resp = await admin(`/admin/redemptions/${id}/deny`, { method: "POST" });
    if (resp.ok) {
      await loadQueue();
    } else {
      const msg = document.querySelector(`[data-msg="${id}"]`);
      const body = await resp.json().catch(() => ({}));
      msg.textContent = body.detail || "Failed.";
      msg.className = "rowmsg err";
      btn.disabled = false;
    }
  }

  $("q-refresh").addEventListener("click", loadQueue);
  $("q-status").addEventListener("change", loadQueue);

  // ------------------------------------------------------------------
  // Fraud

  async function loadFraudSummary() {
    const hours = $("f-hours").value;
    const resp = await admin(`/admin/fraud/summary?hours=${hours}`);
    if (!resp.ok) return;
    const s = await resp.json();

    const kinds = Object.entries(s.events_by_kind || {});
    $("f-kinds-empty").hidden = kinds.length > 0;
    $("f-kinds").innerHTML = kinds
      .map(([k, n]) => `<tr><td>${esc(k)}</td><td class="num mono">${fmt(n)}</td></tr>`)
      .join("");

    const ips = s.top_denied_ips || [];
    $("f-ips-empty").hidden = ips.length > 0;
    $("f-ips").innerHTML = ips
      .map((r) => `<tr><td class="mono">${esc(r.ip)}</td><td class="num mono">${fmt(r.denials)}</td></tr>`)
      .join("");

    const risky = s.top_risk_users || [];
    $("f-risky-empty").hidden = risky.length > 0;
    $("f-risky").innerHTML = risky
      .map(
        (u) => `<tr>
          <td><button class="idlink" data-user="${u.user_id}" title="${u.user_id}" type="button">${short(u.user_id)}</button></td>
          <td class="num mono" title="risk score">${u.risk_score}</td>
          <td class="num mono" title="balance">${fmt(u.balance)}</td>
          <td><span class="st ${esc(u.status)}">${esc(u.status)}</span></td>
        </tr>`
      )
      .join("");
    for (const btn of $("f-risky").querySelectorAll("[data-user]")) {
      btn.addEventListener("click", () => openUser(btn.dataset.user));
    }
  }

  async function loadFraudEvents() {
    const kind = $("f-kind").value.trim();
    const url = kind
      ? `/admin/fraud/events?kind=${encodeURIComponent(kind)}&limit=100`
      : "/admin/fraud/events?limit=100";
    const resp = await admin(url);
    if (!resp.ok) return;
    const rows = await resp.json();
    $("f-events-empty").hidden = rows.length > 0;
    $("f-events").innerHTML = rows
      .map((e) => {
        const detail = e.detail ? JSON.stringify(e.detail) : "";
        const user = e.user_id
          ? `<button class="idlink" data-user="${e.user_id}" title="${e.user_id}" type="button">${short(e.user_id)}</button>`
          : "";
        return `<tr>
          <td class="mono">${age(e.created_at)}</td>
          <td>${esc(e.kind)}</td>
          <td>${user}</td>
          <td class="mono">${e.ip ? esc(e.ip) : ""}</td>
          <td class="mono" title="${esc(detail)}">${esc(detail.length > 80 ? detail.slice(0, 77) + "..." : detail)}</td>
        </tr>`;
      })
      .join("");
    for (const btn of $("f-events").querySelectorAll("[data-user]")) {
      btn.addEventListener("click", () => openUser(btn.dataset.user));
    }
  }

  $("f-refresh").addEventListener("click", () => { loadFraudSummary(); loadFraudEvents(); });
  $("f-hours").addEventListener("change", loadFraudSummary);
  $("f-events-refresh").addEventListener("click", loadFraudEvents);

  // ------------------------------------------------------------------
  // Users

  async function loadUser() {
    const id = $("u-id").value.trim();
    $("u-msg").textContent = "";
    if (!id) return;
    const resp = await admin(`/admin/users/${id}`);
    if (resp.status === 404) {
      $("u-msg").textContent = "No user with that id.";
      $("u-panel").hidden = true;
      return;
    }
    if (resp.status === 422) {
      $("u-msg").textContent = "That is not a valid uuid.";
      $("u-panel").hidden = true;
      return;
    }
    if (!resp.ok) return;
    const u = await resp.json();
    $("u-panel").hidden = false;
    $("u-title").textContent = "User " + short(u.user_id);
    $("u-ban").hidden = u.status === "banned";
    $("u-unban").hidden = u.status !== "banned";
    const redemptions = Object.entries(u.redemptions_by_status || {})
      .map(([k, n]) => `${k} ${n}`)
      .join(", ");
    const rows = [
      ["Id", u.user_id],
      ["Status", `<span class="st ${esc(u.status)}">${esc(u.status)}</span>`],
      ["Risk score", u.risk_score],
      ["Balance", fmt(u.balance)],
      ["Email", u.email ? `${esc(u.email)} ${u.email_verified ? '<span class="st sent">verified</span>' : '<span class="st pending">unverified</span>'}` : "none"],
      ["Created", new Date(u.created_at).toISOString().slice(0, 10)],
      ["Check-in streak", u.checkin_streak],
      ["Credited, all time", fmt(u.total_credited)],
      ["Debited, all time", fmt(u.total_debited)],
      ["Verified ad receipts", u.verified_ad_receipts],
      ["Redemptions", redemptions || "none"],
    ];
    $("u-kv").innerHTML = rows
      .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
      .join("");
  }

  async function setStatus(status) {
    const id = $("u-id").value.trim();
    const resp = await admin(`/admin/users/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    if (resp.ok) await loadUser();
  }

  $("u-load").addEventListener("click", loadUser);
  $("u-id").addEventListener("keydown", (ev) => { if (ev.key === "Enter") loadUser(); });
  $("u-ban").addEventListener("click", (ev) => {
    const btn = ev.currentTarget;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "Confirm ban";
      setTimeout(() => { btn.dataset.armed = "0"; btn.textContent = "Ban"; }, 3000);
      return;
    }
    btn.dataset.armed = "0";
    btn.textContent = "Ban";
    setStatus("banned");
  });
  $("u-unban").addEventListener("click", () => setStatus("active"));

  // ------------------------------------------------------------------
  // Jobs

  for (const btn of document.querySelectorAll("[data-job]")) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const job = btn.dataset.job;
      const out = $("j-" + job);
      out.textContent = "running";
      const resp = await admin(`/admin/jobs/${job}`, { method: "POST" });
      if (resp.ok) {
        const body = await resp.json();
        const count = Object.values(body)[0];
        out.textContent = `${fmt(count)} rows at ${new Date().toTimeString().slice(0, 8)}`;
        out.className = "rowmsg mono ok";
      } else {
        out.textContent = "failed";
        out.className = "rowmsg mono err";
      }
      btn.disabled = false;
    });
  }

  // ------------------------------------------------------------------
  // Gate and boot

  async function openConsole() {
    // any cheap authenticated call validates the key
    const resp = await admin("/admin/redemptions?status=pending&limit=1");
    if (!resp.ok) return; // admin() already routed to the gate
    $("gate").hidden = true;
    $("console").hidden = false;
    $("signout").hidden = false;
    loadQueue();
    loadFraudSummary();
    loadFraudEvents();
  }

  $("gate-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    key = $("key-input").value.trim();
    sessionStorage.setItem("winfetti_admin_key", key);
    openConsole();
  });

  $("signout").addEventListener("click", () => showGate());

  if (key) {
    $("gate").hidden = true;
    openConsole();
  }
})();
