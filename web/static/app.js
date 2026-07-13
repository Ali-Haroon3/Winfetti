/* Winfetti player app. Talks to the same-origin API; the server is the
   truth for every number on this page. The wheel scene follows the
   seat-layer-design skill: one SVG on an isometric plane, depth drawn as
   offset extrusion copies (#D9D3C6 platform sides, #9A8F73 coin edges). */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const ERRORS = {
    email_not_verified: "Verify your email first.",
    account_too_new: "Accounts must be at least 7 days old to redeem.",
    not_enough_verified_ads: "Redemptions unlock after 10 verified rewarded ads.",
    redemption_already_pending: "You already have a redemption in review.",
    redemption_cooldown: "Please wait a minute between redemptions.",
    insufficient_balance: "Not enough coins for that reward.",
    unknown_sku: "That reward is no longer available.",
    email_in_use: "That address is already verified on another account.",
    invalid_or_expired_token: "That code is invalid or expired. Request a new one.",
    unknown_game_event: "The server does not recognize that outcome.",
    rate_limited: "Too many requests. Give it a minute.",
  };
  const explain = (detail, status) => {
    if (status === 429) return ERRORS.rate_limited;
    return ERRORS[detail] || "Something went wrong. Try again.";
  };

  // ------------------------------------------------------------------
  // Auth: anonymous device identity, JWT kept in localStorage.

  let jwt = localStorage.getItem("winfetti_jwt") || "";

  async function deviceAuth() {
    let deviceId = localStorage.getItem("winfetti_device_id");
    if (!deviceId) {
      deviceId = "web-" + crypto.randomUUID();
      localStorage.setItem("winfetti_device_id", deviceId);
    }
    const resp = await fetch("/v1/auth/device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: deviceId }),
    });
    if (!resp.ok) throw new Error("auth failed: " + resp.status);
    const data = await resp.json();
    jwt = data.jwt;
    localStorage.setItem("winfetti_jwt", jwt);
  }

  async function api(path, opts = {}, retried = false) {
    const resp = await fetch(path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + jwt,
        ...(opts.headers || {}),
      },
    });
    if (resp.status === 401 && !retried) {
      await deviceAuth();
      return api(path, opts, true);
    }
    return resp;
  }

  // ------------------------------------------------------------------
  // The wheel scene. Geometry is presentation; amounts come from
  // /v1/config so the segments always match the server's payout table.

  const CX = 320, CY = 270, R = 186;
  // the plane is rotated -33deg, so "screen down" in local svg coords is
  // (-sin33, cos33); extrusion sides offset along that vector
  const EX = { x: -0.545, y: 0.839 };
  const off = (d) => `translate(${(EX.x * d).toFixed(1)},${(EX.y * d).toFixed(1)})`;
  const at = (x, y, d) => [x + EX.x * d, y + EX.y * d];

  const WHEEL_ORDER = [
    "seg_50", "seg_1000", "seg_100", "seg_5000",
    "seg_250", "seg_2500", "seg_500", "jackpot",
  ];
  const SEG_FILLS = ["var(--panel)", "var(--raise)"];
  let segments = []; // {event, coins, angle}
  let rotation = 0;
  let spinning = false;

  function coinStack(x, y, count, delay) {
    // a stack of coins as extruded cylinders, rising along the plane normal
    let g = `<g class="fadein" style="animation-delay:${delay}ms">`;
    for (let i = 0; i < count; i++) {
      const lift = -i * 8;
      const [sx, sy] = at(x, y, lift + 8);
      const [tx, ty] = at(x, y, lift);
      g += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="30" fill="#9A8F73"/>`;
      g += `<circle cx="${tx.toFixed(1)}" cy="${ty.toFixed(1)}" r="30" fill="var(--sand)" stroke="rgba(26,24,20,.25)" stroke-width=".8"/>`;
      g += `<circle cx="${tx.toFixed(1)}" cy="${ty.toFixed(1)}" r="21" fill="none" stroke="#9A8F73" stroke-width="1" opacity=".7"/>`;
    }
    return g + "</g>";
  }

  function buildWheel(wheelPayouts) {
    const events = WHEEL_ORDER.filter((e) => e in wheelPayouts).concat(
      Object.keys(wheelPayouts).filter((e) => !WHEEL_ORDER.includes(e))
    );
    const svg = $("wheel");
    const step = 360 / events.length;
    const pt = (deg, rad) => {
      const t = ((deg - 90) * Math.PI) / 180;
      return [CX + rad * Math.cos(t), CY + rad * Math.sin(t)];
    };

    // platform tile the whole scene sits on
    const tile = `x="80" y="30" width="540" height="480" rx="18"`;
    let inner = `<g class="fadein">`;
    inner += `<rect class="tile-side" ${tile} transform="${off(26)}" fill="#D9D3C6"/>`;
    inner += `<rect ${tile} fill="var(--panel2)" stroke="var(--line2)"/>`;
    inner += `</g>`;

    // the wheel: a disc with a static extruded side; only the top spins
    inner += `<g class="fadein" style="animation-delay:60ms">`;
    inner += `<circle cx="${CX}" cy="${CY}" r="190" transform="${off(16)}" fill="#D9D3C6"/>`;
    inner += `<g id="rotor">`;
    segments = events.map((event, i) => {
      const angle = i * step;
      const coins = wheelPayouts[event];
      const jackpot = event === "jackpot";
      const [x1, y1] = pt(angle - step / 2, R);
      const [x2, y2] = pt(angle + step / 2, R);
      const fill = jackpot ? "var(--accent)" : SEG_FILLS[i % 2];
      const text = jackpot ? "var(--panel)" : "var(--text)";
      inner += `<path d="M${CX},${CY} L${x1.toFixed(2)},${y1.toFixed(2)} ` +
        `A${R},${R} 0 0 1 ${x2.toFixed(2)},${y2.toFixed(2)} Z" ` +
        `fill="${fill}" stroke="var(--line2)" stroke-width="1"/>`;
      inner += `<text x="${CX}" y="${CY - 132}" data-angle="${angle}" ` +
        `dominant-baseline="middle" text-anchor="middle" font-family="Space Mono, monospace" ` +
        `font-size="${jackpot ? 16 : 17}" font-weight="700" fill="${text}">${fmt(coins)}</text>`;
      return { event, coins, angle };
    });
    inner += `</g>`;
    inner += `<circle cx="${CX}" cy="${CY}" r="190" fill="none" stroke="var(--text)" stroke-width="2"/>`;
    inner += `<circle cx="${CX}" cy="${CY}" r="26" transform="${off(5)}" fill="#D9D3C6"/>`;
    inner += `<circle cx="${CX}" cy="${CY}" r="26" fill="var(--panel)" stroke="var(--line2)"/>`;
    // the pointer, a small extruded ink flag at the top of the disc
    inner += `<path d="M305,50 h30 L320,88 Z" transform="${off(6)}" fill="#D9D3C6"/>`;
    inner += `<path d="M305,50 h30 L320,88 Z" fill="var(--text)"/>`;
    inner += `</g>`;

    // today's winnings, sitting on the same table
    inner += coinStack(548, 178, 4, 120);
    inner += coinStack(562, 352, 3, 180);

    svg.innerHTML = inner;
    orientLabels();
    if (!reducedMotion) svg.parentElement.classList.add("can-animate");
  }

  function orientLabels() {
    // Labels that would read upside down on the stopped wheel get flipped
    // 180. "Upside down" is judged in screen space: the plane's rotateZ
    // shifts every local angle by -33deg (except in the flat
    // reduced-motion view). Reruns after every spin.
    const planeShift = reducedMotion ? 0 : 33;
    for (const label of $("wheel").querySelectorAll("text[data-angle]")) {
      const angle = Number(label.dataset.angle);
      const resting = (((angle + rotation) % 360) + 360) % 360;
      const seen = (((resting - planeShift) % 360) + 360) % 360;
      const flip = seen > 90 && seen < 270 ? ` rotate(180 ${CX} ${CY - 132})` : "";
      label.setAttribute("transform", `rotate(${angle} ${CX} ${CY})${flip}`);
    }
  }

  function pickSegment() {
    // Client-side feel only: the server prices whatever lands. Rarity
    // scales with payout so the jackpot stays a jackpot.
    const weights = segments.map((s) => Math.max(1, Math.round(1000 / Math.sqrt(s.coins))));
    const total = weights.reduce((a, b) => a + b, 0);
    let roll = Math.random() * total;
    for (let i = 0; i < segments.length; i++) {
      roll -= weights[i];
      if (roll <= 0) return segments[i];
    }
    return segments[0];
  }

  async function claim(segment) {
    const resp = await api("/v1/game/claim", {
      method: "POST",
      body: JSON.stringify({
        game: "wheel",
        event: segment.event,
        idem_key: "web-" + crypto.randomUUID(),
      }),
    });
    const result = $("result");
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      result.textContent = explain(body.detail, resp.status);
      return;
    }
    const data = await resp.json();
    if (data.awarded === 0 && data.capped) {
      result.textContent = "Daily cap reached. Come back tomorrow.";
    } else if (data.capped) {
      result.innerHTML = `<span class="win">+${fmt(data.awarded)}</span> coins, daily cap reached`;
    } else if (data.awarded > segment.coins) {
      result.innerHTML = `<span class="win">+${fmt(data.awarded)}</span> coins with multipliers`;
    } else {
      result.innerHTML = `<span class="win">+${fmt(data.awarded)}</span> coins`;
    }
    await refreshMe();
  }

  function spin() {
    if (spinning) return;
    spinning = true;
    $("spin-btn").disabled = true;
    $("result").textContent = "";
    const segment = pickSegment();
    const finish = async () => {
      try {
        await claim(segment);
      } finally {
        spinning = false;
        $("spin-btn").disabled = false;
      }
    };

    const rotor = $("rotor");
    const jitter = (Math.random() - 0.5) * 20;
    const delta = ((-segment.angle - rotation) % 360 + 360) % 360;
    rotation += 3 * 360 + delta + jitter;
    if (reducedMotion) {
      rotor.setAttribute("transform", `rotate(${rotation} ${CX} ${CY})`);
      orientLabels();
      finish();
      return;
    }
    rotor.style.transform = `rotate(${rotation}deg)`;
    orientLabels(); // flips happen mid-motion where they can't be seen
    rotor.addEventListener("transitionend", finish, { once: true });
  }

  // ------------------------------------------------------------------
  // Account state

  let me = null;
  let catalog = [];
  let config = null;

  function renderMultipliers() {
    const parts = [];
    if (me.daily.happy_hour_active) parts.push("Happy hour");
    if (me.gold) parts.push("Gold");
    if (me.boost_until) parts.push("Boost");
    $("mult-line").textContent = parts.length ? parts.join(" + ") + " active" : "No multipliers";
  }

  function renderCaps() {
    const d = me.daily;
    $("cap-game").textContent = fmt(d.game_win_credited_today);
    $("cap-game-max").textContent = fmt(d.daily_game_win_cap);
    $("cap-game-bar").style.width =
      Math.min(100, (100 * d.game_win_credited_today) / d.daily_game_win_cap) + "%";
    $("cap-total").textContent = fmt(d.total_credited_today);
    $("cap-total-max").textContent = fmt(d.daily_total_credit_cap);
    $("cap-total-bar").style.width =
      Math.min(100, (100 * d.total_credited_today) / d.daily_total_credit_cap) + "%";
  }

  function renderCheckin() {
    const d = me.daily;
    $("chip-streak").textContent = d.checkin_streak;
    const ladder = $("ladder");
    const rewards = config.checkin_rewards;
    const dayNow = d.checked_in_today ? d.checkin_streak : d.checkin_streak + 1;
    const highlight = Math.min(Math.max(dayNow, 1), rewards.length) - 1;
    ladder.innerHTML = rewards
      .map((r, i) => `<span class="${i === highlight ? "today" : ""}">${fmt(r)}</span>`)
      .join("");
    $("checkin-day").textContent = d.checkin_streak > 0 ? "day " + d.checkin_streak : "";
    $("checkin-btn").disabled = d.checked_in_today;
    if (d.checked_in_today && !$("checkin-msg").textContent) {
      $("checkin-msg").textContent = "Checked in. Come back tomorrow.";
      $("checkin-msg").className = "small dim";
    }
  }

  function renderEmail() {
    const state = $("email-state");
    if (me.email_verified) {
      state.textContent = "verified";
      state.className = "st sent";
      $("email-body").innerHTML =
        `<p class="small">Gift cards go to <span class="mono">${me.email}</span>.</p>`;
    } else {
      state.textContent = "unverified";
      state.className = "st pending";
    }
  }

  function renderCatalog() {
    $("catalog").innerHTML = catalog
      .map(
        (item) => `
        <div class="item">
          <span class="usd">${item.label}</span>
          <span class="cost">${fmt(item.coins)} coins</span>
          <button class="btn sm" data-sku="${item.sku}" type="button"
            ${me.balance < item.coins ? "disabled" : ""}>Redeem</button>
        </div>`
      )
      .join("");
    for (const btn of $("catalog").querySelectorAll("button[data-sku]")) {
      btn.addEventListener("click", () => redeem(btn.dataset.sku));
    }
  }

  async function renderRedemptions() {
    const resp = await api("/v1/redemptions");
    if (!resp.ok) return;
    const rows = await resp.json();
    const bySku = Object.fromEntries(catalog.map((c) => [c.sku, c.label]));
    $("redemptions").innerHTML = rows.length
      ? rows
          .map((r) => {
            const when = new Date(r.created_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
            });
            return `<div class="row" style="display:flex;justify-content:space-between;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)">
              <span class="small">${bySku[r.sku] || r.sku} <span class="faint">${when}</span></span>
              <span class="num small">${fmt(r.coins)} <span class="st ${r.status}">${r.status}</span></span>
            </div>`;
          })
          .join("")
      : `<p class="empty">No redemptions yet.</p>`;
  }

  async function redeem(sku) {
    const msg = $("redeem-msg");
    msg.textContent = "";
    const resp = await api("/v1/redemptions", {
      method: "POST",
      body: JSON.stringify({ sku }),
    });
    if (resp.ok) {
      const r = await resp.json();
      msg.textContent =
        r.status === "sent"
          ? "Approved and sent. Check your inbox."
          : "Queued for review. You will see it move to sent once approved.";
      msg.className = "ok small";
    } else {
      const body = await resp.json().catch(() => ({}));
      msg.textContent = explain(body.detail, resp.status);
      msg.className = "err small";
    }
    await refreshMe();
    await renderRedemptions();
  }

  async function refreshMe() {
    const resp = await api("/v1/me");
    if (!resp.ok) throw new Error("me failed: " + resp.status);
    me = await resp.json();
    $("chip-balance").textContent = fmt(me.balance);
    renderMultipliers();
    renderCaps();
    renderCheckin();
    renderEmail();
    renderCatalog();
  }

  // ------------------------------------------------------------------
  // Events

  $("spin-btn").addEventListener("click", spin);

  $("checkin-btn").addEventListener("click", async () => {
    $("checkin-btn").disabled = true;
    const resp = await api("/v1/checkin", { method: "POST" });
    const msg = $("checkin-msg");
    if (resp.ok) {
      const data = await resp.json();
      msg.textContent = data.already_checked_in
        ? "Already checked in today."
        : `+${fmt(data.awarded)} coins, day ${data.streak}`;
      msg.className = data.already_checked_in ? "small dim" : "ok small";
      await refreshMe();
    } else {
      msg.textContent = explain((await resp.json().catch(() => ({}))).detail, resp.status);
      msg.className = "err small";
      $("checkin-btn").disabled = false;
    }
  });

  document.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (ev.target.id === "email-form") {
      const msg = $("email-msg");
      $("email-send").disabled = true;
      const resp = await api("/v1/me/email", {
        method: "POST",
        body: JSON.stringify({ email: $("email-input").value }),
      });
      $("email-send").disabled = false;
      if (resp.status === 202) {
        msg.textContent = "Sent. Check your inbox for the code.";
        msg.className = "ok small";
      } else {
        msg.textContent = explain((await resp.json().catch(() => ({}))).detail, resp.status);
        msg.className = "err small";
      }
    }
    if (ev.target.id === "token-form") {
      const msg = $("token-msg");
      $("token-send").disabled = true;
      const resp = await api("/v1/me/email/verify", {
        method: "POST",
        body: JSON.stringify({ token: $("token-input").value }),
      });
      $("token-send").disabled = false;
      if (resp.ok) {
        await refreshMe();
        await renderRedemptions();
      } else {
        msg.textContent = explain((await resp.json().catch(() => ({}))).detail, resp.status);
        msg.className = "err small";
      }
    }
  });

  // ------------------------------------------------------------------
  // Boot

  async function boot() {
    const banner = $("banner");
    try {
      if (!jwt) await deviceAuth();
      const cfgResp = await fetch("/v1/config");
      if (!cfgResp.ok) throw new Error("config failed: " + cfgResp.status);
      config = await cfgResp.json();
      buildWheel(config.games.wheel || {});
      const catResp = await api("/v1/redemptions/catalog");
      catalog = catResp.ok ? await catResp.json() : [];
      await refreshMe();
      await renderRedemptions();
      banner.hidden = true;
      $("app").hidden = false;
    } catch (err) {
      banner.hidden = false;
      banner.innerHTML =
        `Can't reach the Winfetti API right now. ` +
        `<button class="btn sm ghost" id="retry" type="button">Retry</button>`;
      $("retry").addEventListener("click", boot, { once: true });
    }
  }

  boot();
})();
