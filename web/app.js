// DR Live Monitor. Reads go through the edge (/api/*); writes use client-side failover
// (POST /r/<active>/notes, refresh /api/config and retry on failure), same as drsystem.client.
"use strict";

const LABELS = { "ap-south-1": "Mumbai", "ap-southeast-1": "Singapore" };
const COLORS = { "ap-south-1": "var(--mumbai)", "ap-southeast-1": "var(--singapore)" };
const $ = (id) => document.getElementById(id);
let status = null;

async function getJSON(url, opts = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), opts.timeout || 2500);
  try {
    const r = await fetch(url, { ...opts, signal: ctl.signal });
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body, failover: r.headers.get("x-origin-failover") === "1" };
  } catch (e) {
    return { ok: false, status: 0, body: { error: e.name } };
  } finally {
    clearTimeout(t);
  }
}

function regionName(r) { return r ? `${LABELS[r] || r} (${r})` : "unavailable"; }

async function pollServing() {
  const t0 = performance.now();
  const r = await getJSON("/api/whoami");
  const ms = Math.round(performance.now() - t0);
  if (r.ok) {
    $("serving").textContent = LABELS[r.body.region] || r.body.region;
    $("serving").style.color = COLORS[r.body.region] || "inherit";
    $("serving-detail").textContent = `${r.body.region} · ${ms} ms${r.failover ? " · edge failed over" : ""}`;
  } else {
    $("serving").textContent = "Unavailable";
    $("serving").style.color = "var(--bad)";
    $("serving-detail").textContent = `${r.body.error || r.status} · ${ms} ms`;
  }
}

function fmtTime(t) { return new Date(t * 1000).toLocaleTimeString(); }

function renderStatus(s) {
  const ctl = s.control[s.standby] && !s.control[s.standby].error ? s.control[s.standby] : s.control[s.home];
  if (ctl && ctl.active_region) {
    $("active").textContent = LABELS[ctl.active_region] || ctl.active_region;
    $("active").style.color = COLORS[ctl.active_region];
    $("epoch").textContent = `epoch ${ctl.epoch}${ctl.write_freeze ? " · WRITES FROZEN" : ""}${ctl.demoted_region ? " · demoted: " + ctl.demoted_region : ""}`;
  }
  const d = s.last_decision;
  if (d) {
    $("score").textContent = d.score;
    $("score").className = "badge " + (d.score >= 70 ? "ok" : d.score >= 40 ? "warn" : "bad");
    $("score-bar").style.width = `${d.score}%`;
    $("score-bar").style.background = d.score >= 70 ? "var(--ok)" : d.score >= 40 ? "var(--warn)" : "var(--bad)";
    $("decision").textContent = `${d.action}: ${d.reason}`;
  }
  let html = "<h2>Regions</h2><table><tr><th>Region</th><th>Faults</th><th>Replication out</th><th>Control view</th></tr>";
  for (const [name, r] of Object.entries(s.regions)) {
    const faults = Object.entries(r.faults).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(", ") || '<span class="ok">none</span>';
    const rep = r.replication_out;
    const c = s.control[name] || {};
    html += `<tr><td><b>${r.label}</b><br><code>${name}</code></td><td>${faults}</td>` +
      `<td>${rep.pending} pending${rep.pending ? ` (oldest ${rep.oldest_pending_age_s.toFixed(1)} s)` : ""}<br><span class="muted">${rep.replicated_total} replicated</span></td>` +
      `<td>${c.error ? `<span class="bad">${c.error}</span>` : `${c.active_region} · e${c.epoch}`}</td></tr>`;
  }
  $("regions").innerHTML = html + "</table>";
  $("events").innerHTML = s.events.slice().reverse().map((e) => {
    const { t, kind, label, run_id, ...rest } = e;
    return `<tr><td>${fmtTime(t)}</td><td><b>${kind}</b></td><td><code>${JSON.stringify(rest).slice(0, 180)}</code></td></tr>`;
  }).join("");
}

async function pollStatus() {
  const r = await getJSON("/_sim/status");
  if (r.ok) { status = r.body; renderStatus(status); }
}

function faultButtons() {
  const defs = [
    ["Outage", { outage: true }], ["70% errors", { error_rate: 0.7 }],
    ["Storage down", { storage_down: true }], ["Health fail", { health_fail: true }],
  ];
  let html = "";
  for (const region of Object.keys(LABELS)) {
    html += `<div style="margin:4px 0"><b>${LABELS[region]}</b>: `;
    for (const [label, fault] of defs) {
      html += `<button data-region="${region}" data-fault='${JSON.stringify(fault)}'>${label}</button>`;
    }
    html += `<button data-clear="${region}">Clear</button></div>`;
  }
  $("fault-buttons").innerHTML = html;
  $("fault-buttons").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.clear) {
      await getJSON(`/_sim/clear/${b.dataset.clear}`, { method: "POST" });
    } else {
      await getJSON("/_sim/fault", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ region: b.dataset.region, ...JSON.parse(b.dataset.fault) }),
      });
    }
    pollStatus();
  });
}

async function writeNote() {
  $("write-result").textContent = "writing…";
  const key = crypto.randomUUID();
  const deadline = Date.now() + 30000;
  let active = null, attempts = 0;
  while (Date.now() < deadline) {
    if (!active) {
      const cfg = await getJSON("/api/config");
      if (cfg.ok) active = cfg.body.active_region;
      else { await new Promise((r) => setTimeout(r, 300)); continue; }
    }
    attempts++;
    const r = await getJSON(`/r/${active}/notes`, {
      method: "POST", headers: { "content-type": "application/json", "idempotency-key": key },
      body: JSON.stringify({ title: "dashboard note", body: new Date().toISOString() }),
    });
    if (r.ok) {
      $("write-result").innerHTML = `<span class="ok">stored in ${regionName(r.body.written_region)}</span> after ${attempts} attempt(s)`;
      return;
    }
    active = r.body.active_region && r.body.active_region !== active ? r.body.active_region : null;
    await new Promise((res) => setTimeout(res, 300));
  }
  $("write-result").innerHTML = '<span class="bad">write failed (no writable region)</span>';
}

$("failback").addEventListener("click", async () => {
  const who = prompt("Approver name (recorded in the timeline):");
  if (who) await getJSON("/_sim/failback", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ approved_by: who }) });
});
$("write").addEventListener("click", writeNote);
faultButtons();
setInterval(pollServing, 1000);
setInterval(pollStatus, 1000);
pollServing(); pollStatus();
