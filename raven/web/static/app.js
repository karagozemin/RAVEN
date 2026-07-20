/* ── RAVEN Control Room · frontend ────────────────────────────────
 * Consumes the SSE tick stream from /stream and renders the agent's
 * live decision state. Pure vanilla JS, no build step.
 * ---------------------------------------------------------------- */
"use strict";

/* Backend base URL. Empty string = same-origin (local dev / single-service).
 * On Vercel we serve config.js which sets window.RAVEN_API_BASE to the
 * Render backend URL, e.g. "https://raven-xxxx.onrender.com". */
const API_BASE = (window.RAVEN_API_BASE || "").replace(/\/+$/, "");

const $ = (id) => document.getElementById(id);

const el = {
  landing: $("landing"),
  transitionOverlay: $("transitionOverlay"),
  homeBtn: $("homeBtn"),
  logoHomeBtn: $("logoHomeBtn"),
  fixtureId: $("fixtureId"),
  matchTime: $("matchTime"),
  speed: $("speed"),
  startBtn: $("startBtn"),
  stopBtn: $("stopBtn"),
  stateBanner: $("stateBanner"),
  stateValue: $("stateValue"),
  stateReason: $("stateReason"),
  riskFill: $("riskFill"),
  riskValue: $("riskValue"),
  stateTrack: $("stateTrack"),
  scoreHome: $("scoreHome"),
  scoreAway: $("scoreAway"),
  oddHome: $("oddHome"),
  oddDraw: $("oddDraw"),
  oddAway: $("oddAway"),
  eventFlash: $("eventFlash"),
  quotesBody: $("quotesBody"),
  quotesCount: $("quotesCount"),
  quoteMode: $("quoteMode"),
  spreadPnl: $("spreadPnl"),
  fillCount: $("fillCount"),
  exposureBody: $("exposureBody"),
  exposureStatus: $("exposureStatus"),
  hedgeBox: $("hedgeBox"),
  hedgeDetail: $("hedgeDetail"),
  receiptFeed: $("receiptFeed"),
  receiptCount: $("receiptCount"),
  logFeed: $("logFeed"),
  connDot: $("connDot"),
  connText: $("connText"),
  tickNum: $("tickNum"),
  seqNum: $("seqNum"),
  provText: $("provText"),
  receiptDialog: $("receiptDialog"),
  receiptDialogClose: $("receiptDialogClose"),
  receiptDialogTitle: $("receiptDialogTitle"),
  receiptDialogStatus: $("receiptDialogStatus"),
  receiptDialogExplorer: $("receiptDialogExplorer"),
  receiptDialogSequence: $("receiptDialogSequence"),
  receiptDialogPolicy: $("receiptDialogPolicy"),
  receiptDialogRisk: $("receiptDialogRisk"),
  receiptDialogAction: $("receiptDialogAction"),
  receiptDialogReason: $("receiptDialogReason"),
  receiptDialogJson: $("receiptDialogJson"),
  copyReceiptHash: $("copyReceiptHash"),
};

const appViews = Array.from(document.querySelectorAll(".app-view"));
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
let viewTransitioning = false;
let waitingForHistory = false;

function renderAppView(open) {
  document.body.classList.toggle("control-room-open", open);
  el.landing.setAttribute("aria-hidden", String(open));
  appViews.forEach((node) => node.setAttribute("aria-hidden", String(!open)));
  window.scrollTo(0, 0);
  if (!open) stop();
}

async function coverView() {
  if (reducedMotion) return;
  el.transitionOverlay.setAttribute("aria-hidden", "false");
  el.transitionOverlay.className = "transition-overlay";
  void el.transitionOverlay.offsetWidth;
  el.transitionOverlay.classList.add("is-covering");
  await wait(720);
}

async function revealView(open) {
  const entryClass = open ? "view-entering-control" : "view-entering-landing";
  document.body.classList.add(entryClass);
  if (!reducedMotion) {
    el.transitionOverlay.className = "transition-overlay is-revealing";
    await wait(440);
  }
  el.transitionOverlay.className = "transition-overlay";
  el.transitionOverlay.setAttribute("aria-hidden", "true");
  await wait(reducedMotion ? 0 : 180);
  document.body.classList.remove(entryClass);
}

async function transitionToView(open) {
  if (viewTransitioning) return;
  viewTransitioning = true;
  await coverView();
  renderAppView(open);
  await revealView(open);
  viewTransitioning = false;
}

async function openControlRoom() {
  if (viewTransitioning) return;
  viewTransitioning = true;
  await coverView();
  renderAppView(true);
  if (window.location.hash !== "#control-room") {
    history.pushState({ ravenView: "control-room" }, "", "#control-room");
  }
  await revealView(true);
  viewTransitioning = false;
}

async function returnToLanding() {
  if (viewTransitioning) return;
  if (window.location.hash !== "#control-room") {
    await transitionToView(false);
    return;
  }
  viewTransitioning = true;
  await coverView();
  waitingForHistory = true;
  history.back();
}

document.querySelectorAll("[data-enter-app]").forEach((button) => {
  button.addEventListener("click", openControlRoom);
});

el.homeBtn.addEventListener("click", returnToLanding);
el.logoHomeBtn.addEventListener("click", returnToLanding);

window.addEventListener("popstate", async () => {
  const open = window.location.hash === "#control-room";
  if (waitingForHistory) {
    waitingForHistory = false;
    renderAppView(open);
    await revealView(open);
    viewTransitioning = false;
    return;
  }
  await transitionToView(open);
});

document.querySelectorAll('a[href^="#"]').forEach((link) => {
  link.addEventListener("click", (event) => {
    const target = document.querySelector(link.getAttribute("href"));
    if (!target || document.body.classList.contains("control-room-open")) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
});

const revealTargets = document.querySelectorAll(
  ".system-band, .posture-band, .landing-cta"
);
if (reducedMotion || !("IntersectionObserver" in window)) {
  revealTargets.forEach((node) => node.classList.add("is-visible"));
} else {
  revealTargets.forEach((node) => node.classList.add("motion-reveal"));
  const revealObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      });
    },
    { threshold: 0.12 }
  );
  revealTargets.forEach((node) => revealObserver.observe(node));
}

let source = null;
let running = false;
let cumulativePnl = 0;
let receiptTotal = 0;
let fillTotal = 0;
let lastScore = { home: 0, away: 0 };
let flashTimer = null;
let selectedReceiptHash = "";

const STATE_CLASS = {
  NORMAL: "state-normal",
  CAUTION: "state-caution",
  WITHDRAW: "state-withdraw",
  HEDGE: "state-hedge",
  RECALIBRATE: "state-recalibrate",
  REENTER: "state-reenter",
  IDLE: "state-idle",
};

function fmt(n, d = 3) {
  if (n === null || n === undefined) return "—";
  return Number(n).toFixed(d);
}

function setConnection(state) {
  el.connDot.className = "conn-dot conn-" + state;
  el.connText.textContent = state === "on" ? "STREAMING" : state === "ready" ? "READY" : "OFFLINE";
}

async function checkBackend() {
  try {
    const response = await fetch(`${API_BASE}/healthz`, { cache: "no-store" });
    if (response.ok && !running) setConnection("ready");
  } catch (_) {
    if (!running) setConnection("off");
  }
}

async function loadEvidence() {
  try {
    const response = await fetch(`${API_BASE}/counterfactual`);
    if (!response.ok) return;
    const result = await response.json();
    $("evidenceFrames").textContent = Number(result.raven.frames).toLocaleString("en-US");
    $("evidenceReduction").textContent = `${Number(result.peak_risk_reduction_pct).toFixed(2)}%`;
    $("evidenceProofs").textContent = result.onchain_proofs;
  } catch (_) {
    // Static build-time values remain visible while a free backend wakes up.
  }
}

/* ── Renderers ────────────────────────────────────────────────── */
function renderState(t) {
  const state = t.state || "IDLE";
  el.stateBanner.className =
    "state-banner app-view " + (STATE_CLASS[state] || "state-idle");
  el.stateValue.textContent = state;
  el.stateReason.textContent = t.reason || "";

  el.stateTrack.querySelectorAll("[data-state]").forEach((step) => {
    step.classList.toggle("is-active", step.dataset.state === state);
  });

  const risk = t.risk_score ?? 0;
  el.riskFill.style.width = Math.min(100, Math.max(0, risk)) + "%";
  el.riskValue.textContent = fmt(risk, 1);
}

function renderMatch(t) {
  el.fixtureId.textContent = t.fixture_id ?? "—";
  el.matchTime.textContent = t.match_time || "--:--";

  const s = t.score || { home: 0, away: 0 };
  if (s.home !== lastScore.home) bumpScore(el.scoreHome, s.home);
  if (s.away !== lastScore.away) bumpScore(el.scoreAway, s.away);
  lastScore = { home: s.home, away: s.away };

  if (t.odds) {
    el.oddHome.textContent = fmt(t.odds.home, 2);
    el.oddDraw.textContent = fmt(t.odds.draw, 2);
    el.oddAway.textContent = fmt(t.odds.away, 2);
  }
}

function bumpScore(node, value) {
  node.textContent = value;
  node.classList.remove("bump");
  void node.offsetWidth; // reflow to restart animation
  node.classList.add("bump");
}

function flashEvent(t) {
  let text = "";
  let cls = "";
  if (t.event_type && t.event_type !== "OTHER") {
    text = "EVENT / " + t.event_type.replace(/_/g, " ");
    cls = t.is_shock ? "shock" : "goal";
  }
  if (!text) return;
  el.eventFlash.textContent = text;
  el.eventFlash.className = "event-flash show " + cls;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    el.eventFlash.className = "event-flash";
  }, 2500);
}

function renderQuotes(t) {
  el.quotesCount.textContent = t.quotes_count ?? 0;
  el.quoteMode.textContent = t.is_quoting ? "MARKET OPEN" : "QUOTES WITHDRAWN";
  el.quoteMode.className = "market-open " + (t.is_quoting ? "is-open" : "is-closed");
  const rows = t.quotes || [];
  if (!rows.length) {
    el.quotesBody.innerHTML =
      '<tr class="empty"><td colspan="6">No active quotes — agent not quoting</td></tr>';
  } else {
    el.quotesBody.innerHTML = rows
      .map(
        (q) => `<tr class="row-new">
          <td>${q.outcome}</td>
          <td class="bid">${fmt(q.bid, 3)}</td>
          <td class="fair">${fmt(q.fair, 3)}</td>
          <td class="ask">${fmt(q.ask, 3)}</td>
          <td>${fmt(q.spread_pct, 1)}%</td>
          <td>${fmt(q.bid_size, 0)}/${fmt(q.ask_size, 0)}</td>
        </tr>`
      )
      .join("");
  }
  cumulativePnl += t.spread_pnl || 0;
  fillTotal += (t.fills || []).length;
  el.spreadPnl.textContent =
    (cumulativePnl >= 0 ? "+" : "") + fmt(cumulativePnl, 6);
  el.fillCount.textContent = fillTotal;
}

function renderExposure(t) {
  const rows = t.exposure || [];
  el.exposureStatus.textContent = rows.length ? `${rows.length} OPEN` : "FLAT";
  el.exposureStatus.className = "panel-status " + (rows.length ? "" : "status-safe");
  if (!rows.length) {
    el.exposureBody.innerHTML =
      '<tr class="empty"><td colspan="5">Flat book — no open positions</td></tr>';
  } else {
    el.exposureBody.innerHTML = rows
      .map(
        (p) => `<tr>
          <td>${p.outcome}</td>
          <td class="${p.side}">${p.side.toUpperCase()}</td>
          <td>${fmt(p.quantity, 2)}</td>
          <td>${fmt(p.avg_price, 3)}</td>
          <td>${fmt(p.notional, 2)}</td>
        </tr>`
      )
      .join("");
  }

  if (t.hedge) {
    const h = t.hedge;
    el.hedgeBox.classList.remove("hidden");
    el.hedgeDetail.textContent =
      `${h.trades.length} trade(s) · worst shock ${h.worst_shock} · ` +
      `Δ ${fmt(h.worst_before, 2)} → ${fmt(h.worst_after, 2)} ` +
      `(−${fmt(h.reduction, 2)})`;
  } else {
    el.hedgeBox.classList.add("hidden");
  }
}

function renderReceipt(t) {
  if (!t.receipt) return;
  const r = t.receipt;
  receiptTotal += 1;
  el.receiptCount.textContent = receiptTotal;

  const empty = el.receiptFeed.querySelector(".empty-feed");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "receipt act-" + (r.action || "").toLowerCase();
  div.tabIndex = 0;
  div.setAttribute("role", "button");
  div.setAttribute("aria-label", `Inspect ${r.action} receipt at sequence ${r.sequence}`);
  const anchored = r.anchored
    ? `<a class="anchored" href="https://explorer.solana.com/tx/${r.signature}?cluster=devnet" target="_blank" rel="noopener noreferrer">view devnet proof ↗</a>`
    : `<span class="unanchored">local · ${r.backend}</span>`;
  div.innerHTML = `
    <div class="receipt-head">
      <span class="receipt-action">${r.action}</span>
      <span class="receipt-seq">seq #${r.sequence}</span>
    </div>
    <div class="receipt-hash">${(r.hash || "").slice(0, 32)}…</div>
    <div class="receipt-meta">
      <span>${r.previous_state} → ${r.new_state}</span>
      <span>risk ${fmt(r.risk_score, 4)}</span>
      <span>cancelled ${r.quotes_cancelled}</span>
      <span>hedges ${r.hedge_trades}</span>
      ${anchored}
    </div>`;
  el.receiptFeed.prepend(div);
  div.addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    openReceiptDialog(r);
  });
  div.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openReceiptDialog(r);
  });

  while (el.receiptFeed.children.length > 40) {
    el.receiptFeed.lastChild.remove();
  }
}

function openReceiptDialog(receipt) {
  const detail = receipt.detail || {};
  selectedReceiptHash = receipt.hash || detail.receiptHash || "";
  el.receiptDialogTitle.textContent = receipt.action || "RECEIPT";
  el.receiptDialogSequence.textContent = `#${receipt.sequence ?? "—"}`;
  el.receiptDialogPolicy.textContent = detail.policyHash || "—";
  el.receiptDialogRisk.textContent = fmt(receipt.risk_score, 4);
  el.receiptDialogAction.textContent = `${receipt.previous_state} → ${receipt.new_state}`;
  el.receiptDialogReason.textContent = receipt.reason || "—";
  el.receiptDialogJson.textContent = JSON.stringify(detail, null, 2);
  el.copyReceiptHash.textContent = "Copy receipt hash";

  if (receipt.anchored && receipt.signature) {
    el.receiptDialogStatus.textContent = `ANCHORED · ${receipt.backend}`;
    el.receiptDialogStatus.className = "is-anchored";
    el.receiptDialogExplorer.href = `https://explorer.solana.com/tx/${receipt.signature}?cluster=devnet`;
    el.receiptDialogExplorer.hidden = false;
  } else {
    el.receiptDialogStatus.textContent = `LOCAL · ${receipt.backend}`;
    el.receiptDialogStatus.className = "is-local";
    el.receiptDialogExplorer.hidden = true;
  }
  el.receiptDialog.showModal();
}

el.receiptDialogClose.addEventListener("click", () => el.receiptDialog.close());
el.receiptDialog.addEventListener("click", (event) => {
  if (event.target === el.receiptDialog) el.receiptDialog.close();
});
el.copyReceiptHash.addEventListener("click", async () => {
  if (!selectedReceiptHash) return;
  try {
    await navigator.clipboard.writeText(selectedReceiptHash);
    el.copyReceiptHash.textContent = "Copied";
  } catch (_) {
    el.copyReceiptHash.textContent = selectedReceiptHash;
  }
});

function renderLog(t) {
  const empty = el.logFeed.querySelector(".empty-feed");
  if (empty) empty.remove();
  const line = document.createElement("div");
  line.className =
    "log-line st-" +
    (t.state || "").toLowerCase() +
    (t.transitioned ? " transitioned" : "");
  line.innerHTML = `
    <span class="log-seq">#${t.sequence}</span>
    <span class="log-state">${t.state}</span>
    <span class="log-msg">${t.reason || ""}</span>`;
  el.logFeed.prepend(line);
  while (el.logFeed.children.length > 200) {
    el.logFeed.lastChild.remove();
  }
}

function renderFooter(t) {
  el.tickNum.textContent = t.tick ?? 0;
  el.seqNum.textContent = t.sequence ?? "—";
  el.provText.textContent = t.provenance || "—";
}

function handleTick(t) {
  renderState(t);
  renderMatch(t);
  flashEvent(t);
  renderQuotes(t);
  renderExposure(t);
  renderReceipt(t);
  renderLog(t);
  renderFooter(t);
}

/* ── Stream control ───────────────────────────────────────────── */
function start() {
  if (running) return;
  running = true;
  cumulativePnl = 0;
  receiptTotal = 0;
  fillTotal = 0;
  lastScore = { home: 0, away: 0 };
  el.receiptFeed.innerHTML =
    '<div class="empty-feed">Material decisions will be recorded here</div>';
  el.logFeed.innerHTML = '<div class="empty-feed">Waiting for the first decision</div>';
  el.receiptCount.textContent = "0";
  el.fillCount.textContent = "0";
  el.quoteMode.textContent = "CONNECTING";
  el.quoteMode.className = "market-open";

  const speed = el.speed.value || "12";
  source = new EventSource(
    `${API_BASE}/stream?speed=${encodeURIComponent(speed)}`
  );


  source.onopen = () => setConnection("on");

  source.onmessage = (e) => {
    try {
      handleTick(JSON.parse(e.data));
    } catch (err) {
      console.error("tick parse error", err, e.data);
    }
  };

  source.addEventListener("done", () => {
    stop();
    el.stateReason.textContent = "Replay complete — full match processed.";
  });

  source.addEventListener("error", (e) => {
    // EventSource fires 'error' both on stream end and on real errors.
    if (source && source.readyState === EventSource.CLOSED) {
      stop();
    }
  });

  el.startBtn.disabled = true;
  el.stopBtn.disabled = false;
}

function stop() {
  running = false;
  if (source) {
    source.close();
    source = null;
  }
  setConnection("off");
  el.startBtn.disabled = false;
  el.stopBtn.disabled = true;
  checkBackend();
}

el.startBtn.addEventListener("click", start);
el.stopBtn.addEventListener("click", stop);

setConnection("off");
checkBackend();
loadEvidence();

if (window.location.hash === "#control-room") {
  const controlRoomUrl = window.location.href;
  history.replaceState(
    { ravenView: "landing" },
    "",
    window.location.pathname + window.location.search
  );
  history.pushState({ ravenView: "control-room" }, "", controlRoomUrl);
  renderAppView(true);
} else {
  history.replaceState({ ravenView: "landing" }, "", window.location.href);
  renderAppView(false);
}
