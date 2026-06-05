"""Embedded control + telemetry web UI for the EcoFlow NUT bridge.

An async HTTP server (aiohttp) that runs inside the daemon's event loop, so it
shares the single BLE connection: it reads the daemon's live state and routes
control actions over the existing link. ``aiohttp`` is an optional dependency
(``pip install ecoflow-nut-bridge[web]``) imported lazily by the daemon.

Auth model: control actions (toggling outputs, changing auto-shutdown) require a
shared token; the read-only dashboard is open unless ``require_auth_for_read`` is
set. The token is accepted as an ``X-Auth-Token`` header, an
``Authorization: Bearer`` header, or a ``token`` query parameter.

The single-page dashboard (HTML/CSS/JS, no external CDN) is served from ``/`` and
polls ``/api/state`` for live values, ``/api/history`` for charts.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from .config import WebConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiohttp import web

log = structlog.get_logger(__name__)

# Callback signatures the daemon wires up.
StateProvider = Callable[[], dict[str, Any]]
ControlFn = Callable[[str, bool], Awaitable[str]]
HistoryFn = Callable[[int], Awaitable[list[dict[str, Any]]]]
AutoStatusFn = Callable[[], dict[str, Any]]
AutoSetFn = Callable[[bool], Awaitable[None]]

_VALID_OUTPUTS = ("ac", "usb", "dc")


class WebServer:
    """Owns the aiohttp app lifecycle (runner + site)."""

    def __init__(
        self,
        config: WebConfig,
        *,
        state_provider: StateProvider,
        control: ControlFn,
        history: HistoryFn,
        autoshutdown_status: AutoStatusFn,
        set_autoshutdown: AutoSetFn,
        history_enabled: bool = False,
    ) -> None:
        self._config = config
        self._state_provider = state_provider
        self._control = control
        self._history = history
        self._autoshutdown_status = autoshutdown_status
        self._set_autoshutdown = set_autoshutdown
        self._history_enabled = history_enabled
        self._runner: web.AppRunner | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def build_app(self) -> web.Application:
        """Construct the aiohttp application (also used directly by tests)."""
        from aiohttp import web  # lazy: optional dependency

        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/api/state", self._handle_state),
                web.get("/api/history", self._handle_history),
                web.get("/api/autoshutdown", self._handle_autoshutdown_get),
                web.post("/api/autoshutdown", self._handle_autoshutdown_set),
                web.post("/api/control", self._handle_control),
            ]
        )
        return app

    async def start(self) -> None:
        from aiohttp import web  # lazy: optional dependency

        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        log.info(
            "web.listening",
            host=self._config.host,
            port=self._config.port,
            auth=bool(self._config.auth_token),
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- auth --------------------------------------------------------------- #
    def _token_ok(self, request: web.Request) -> bool:
        """Constant-time comparison of the presented token against the config."""
        expected = self._config.auth_token
        if not expected:
            return False
        presented = request.headers.get("X-Auth-Token", "")
        if not presented:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                presented = auth[len("Bearer ") :]
        if not presented:
            presented = request.query.get("token", "")
        return bool(presented) and hmac.compare_digest(presented, expected)

    def _require_control_auth(self, request: web.Request) -> None:
        from aiohttp import web

        if not self._config.auth_token:
            raise web.HTTPServiceUnavailable(
                reason="control disabled: no auth_token configured"
            )
        if not self._token_ok(request):
            raise web.HTTPUnauthorized(reason="invalid or missing auth token")

    def _require_read_auth(self, request: web.Request) -> None:
        from aiohttp import web

        if self._config.require_auth_for_read and not self._token_ok(request):
            raise web.HTTPUnauthorized(reason="invalid or missing auth token")

    # -- handlers ----------------------------------------------------------- #
    async def _handle_index(self, request: web.Request) -> web.Response:
        from aiohttp import web

        # The page itself is always served; read endpoints enforce auth so the
        # browser can prompt for a token. (When require_auth_for_read is off the
        # dashboard is fully open.)
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _handle_state(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        payload = self._state_provider()
        payload["history_enabled"] = self._history_enabled
        payload["control_enabled"] = bool(self._config.auth_token)
        return web.json_response(payload)

    async def _handle_history(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        if not self._history_enabled:
            return web.json_response({"enabled": False, "points": []})
        try:
            minutes = int(request.query.get("minutes", "60"))
        except ValueError:
            raise web.HTTPBadRequest(reason="minutes must be an integer") from None
        minutes = max(1, min(minutes, 60 * 24 * 30))  # cap at 30 days
        points = await self._history(minutes)
        return web.json_response({"enabled": True, "minutes": minutes, "points": points})

    async def _handle_autoshutdown_get(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        return web.json_response(self._autoshutdown_status())

    async def _handle_autoshutdown_set(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_control_auth(request)
        body = await _json_body(request)
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            raise web.HTTPBadRequest(reason='body must be {"enabled": bool}')
        await self._set_autoshutdown(body["enabled"])
        log.info("web.autoshutdown_set", enabled=body["enabled"])
        return web.json_response(self._autoshutdown_status())

    async def _handle_control(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_control_auth(request)
        body = await _json_body(request)
        output = body.get("output")
        enabled = body.get("enabled")
        if output not in _VALID_OUTPUTS or not isinstance(enabled, bool):
            raise web.HTTPBadRequest(
                reason='body must be {"output": "ac|usb|dc", "enabled": bool}'
            )
        try:
            message = await self._control(output, enabled)
        except Exception as exc:  # noqa: BLE001 - surface as a 4xx/5xx to the client
            raise web.HTTPConflict(reason=str(exc)) from exc
        log.info("web.control", output=output, enabled=enabled)
        return web.json_response({"ok": True, "message": message})


async def _json_body(request: web.Request) -> dict[str, Any]:
    from aiohttp import web

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(reason="invalid JSON body") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="JSON body must be an object")
    return data


# --------------------------------------------------------------------------- #
# Single-page dashboard. Vanilla JS, no external assets, tiny canvas chart.
# --------------------------------------------------------------------------- #
_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EcoFlow DELTA 3 - Bridge</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.4 system-ui, sans-serif; background:#0f1115; color:#e6e6e6; }
  header { display:flex; align-items:center; gap:.75rem; padding:1rem 1.25rem;
           border-bottom:1px solid #222631; background:#151823; }
  header h1 { font-size:1.05rem; margin:0; font-weight:600; }
  .status-pill { margin-left:auto; padding:.2rem .6rem; border-radius:999px;
                 font-size:.8rem; font-weight:600; background:#2a2f3c; }
  .status-OL { background:#16432a; color:#7ee2a8; }
  .status-OB { background:#5a4216; color:#f2c969; }
  .status-LB { background:#5a1d1d; color:#f29494; }
  main { padding:1.25rem; max-width:1000px; margin:0 auto; display:grid; gap:1.25rem; }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
  .card { background:#151823; border:1px solid #222631; border-radius:12px; padding:1rem 1.1rem; }
  .metric .label { font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:#8a93a6; }
  .metric .value { font-size:1.6rem; font-weight:600; margin-top:.2rem; }
  .metric .unit { font-size:.85rem; color:#8a93a6; margin-left:.2rem; font-weight:400; }
  .soc-bar { height:8px; border-radius:999px; background:#222631; margin-top:.5rem; overflow:hidden; }
  .soc-fill { height:100%; background:linear-gradient(90deg,#3a8f5c,#7ee2a8); transition:width .4s; }
  h2 { font-size:.85rem; text-transform:uppercase; letter-spacing:.04em; color:#8a93a6; margin:0 0 .75rem; }
  .controls { display:flex; flex-wrap:wrap; gap:.75rem; }
  .ctl { flex:1; min-width:140px; display:flex; align-items:center; justify-content:space-between;
         gap:.5rem; padding:.6rem .8rem; background:#1b1f2b; border-radius:10px; }
  .ctl .name { font-weight:600; }
  button { font:inherit; border:0; border-radius:8px; padding:.45rem .85rem; cursor:pointer;
           background:#2a2f3c; color:#e6e6e6; font-weight:600; }
  button.on { background:#16432a; color:#7ee2a8; }
  button.off { background:#5a1d1d; color:#f29494; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .range { display:flex; gap:.4rem; margin-bottom:.75rem; flex-wrap:wrap; }
  .range button { background:#1b1f2b; font-size:.8rem; padding:.3rem .7rem; }
  .range button.active { background:#2d3957; color:#a9c2ff; }
  canvas { width:100%; height:220px; display:block; }
  .legend { display:flex; gap:1rem; flex-wrap:wrap; font-size:.78rem; color:#8a93a6; margin-top:.5rem; }
  .legend span::before { content:""; display:inline-block; width:10px; height:10px; border-radius:2px;
                         margin-right:.3rem; vertical-align:middle; background:var(--c); }
  .muted { color:#8a93a6; font-size:.85rem; }
  #toast { position:fixed; bottom:1rem; left:50%; transform:translateX(-50%);
           background:#2a2f3c; padding:.6rem 1rem; border-radius:8px; opacity:0;
           transition:opacity .3s; pointer-events:none; }
  #toast.show { opacity:1; }
  .token-row { display:flex; gap:.5rem; align-items:center; margin-top:.5rem; }
  .token-row input { flex:1; background:#0f1115; border:1px solid #2a2f3c; color:#e6e6e6;
                     border-radius:8px; padding:.45rem .6rem; }
</style>
</head>
<body>
<header>
  <h1>EcoFlow DELTA 3 Bridge</h1>
  <span id="status" class="status-pill">…</span>
</header>
<main>
  <section class="card">
    <div class="grid">
      <div class="metric"><div class="label">State of charge</div>
        <div class="value"><span id="soc">–</span><span class="unit">%</span></div>
        <div class="soc-bar"><div id="socFill" class="soc-fill" style="width:0%"></div></div></div>
      <div class="metric"><div class="label">AC input</div>
        <div class="value"><span id="acIn">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">AC output</div>
        <div class="value"><span id="acOut">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">USB / USB-C</div>
        <div class="value"><span id="usb">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">Runtime est.</div>
        <div class="value"><span id="runtime">–</span></div></div>
      <div class="metric"><div class="label">Charge / discharge</div>
        <div class="value" style="font-size:1.1rem"><span id="remain">–</span></div></div>
    </div>
  </section>

  <section class="card">
    <h2>Port controls</h2>
    <div class="controls">
      <div class="ctl"><span class="name">AC output</span>
        <span><button data-out="ac" data-on="1" class="on">On</button>
        <button data-out="ac" data-on="0" class="off">Off</button></span></div>
      <div class="ctl"><span class="name">USB</span>
        <span><button data-out="usb" data-on="1" class="on">On</button>
        <button data-out="usb" data-on="0" class="off">Off</button></span></div>
      <div class="ctl"><span class="name">12V DC</span>
        <span><button data-out="dc" data-on="1" class="on">On</button>
        <button data-out="dc" data-on="0" class="off">Off</button></span></div>
    </div>
    <div id="controlNote" class="muted" style="margin-top:.6rem"></div>
    <div class="token-row" id="tokenRow" style="display:none">
      <input id="token" type="password" placeholder="control token" autocomplete="off">
      <button id="saveToken">Save</button>
    </div>
  </section>

  <section class="card">
    <h2>Auto-shutdown</h2>
    <div class="ctl">
      <span><span class="name">Policy</span> <span id="asState" class="muted"></span></span>
      <span><button id="asOn" class="on">Enable</button>
      <button id="asOff" class="off">Disable</button></span>
    </div>
    <div id="asDetail" class="muted" style="margin-top:.6rem"></div>
  </section>

  <section class="card" id="historyCard">
    <h2>History</h2>
    <div class="range">
      <button data-min="60">1h</button>
      <button data-min="360">6h</button>
      <button data-min="1440" class="active">24h</button>
      <button data-min="10080">7d</button>
    </div>
    <canvas id="chart" width="940" height="220"></canvas>
    <div class="legend">
      <span style="--c:#7ee2a8">SoC %</span>
      <span style="--c:#f2c969">AC out W</span>
      <span style="--c:#a9c2ff">AC in W</span>
    </div>
    <div id="historyNote" class="muted"></div>
  </section>
</main>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let token = localStorage.getItem("ecoflow_token") || "";
let historyMinutes = 1440;
let controlEnabled = false;
let historyEnabled = false;

function authHeaders() { return token ? { "X-Auth-Token": token } : {}; }

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2500);
}

function fmtMins(m) {
  if (m == null) return "–";
  if (m >= 6000) return "∞";
  const h = Math.floor(m / 60), mm = m % 60;
  return h ? `${h}h ${mm}m` : `${mm}m`;
}
function fmtRuntime(s) {
  if (s == null || s >= 99999) return "idle";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

async function refreshState() {
  try {
    const r = await fetch("api/state", { headers: authHeaders() });
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    controlEnabled = s.control_enabled;
    historyEnabled = s.history_enabled;
    $("#soc").textContent = s.soc_percent ?? "–";
    $("#socFill").style.width = (s.soc_percent ?? 0) + "%";
    $("#acIn").textContent = Math.round(s.ac_input_watts ?? 0);
    $("#acOut").textContent = Math.round(s.ac_output_watts ?? 0);
    const usb = (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0);
    $("#usb").textContent = Math.round(usb);
    $("#runtime").textContent = fmtRuntime(s.runtime_seconds);
    const charging = (s.status || "").startsWith("OL");
    $("#remain").textContent = charging
      ? "chg " + fmtMins(s.remain_charge_minutes)
      : "dsg " + fmtMins(s.remain_discharge_minutes);
    const pill = $("#status");
    pill.textContent = s.status ?? "?";
    pill.className = "status-pill " +
      (s.status?.includes("LB") ? "status-LB" : s.status?.startsWith("OL") ? "status-OL" : "status-OB");
    applyControlState();
    $("#historyCard").style.display = historyEnabled ? "" : "none";
  } catch (e) {
    $("#status").textContent = "offline";
    $("#status").className = "status-pill";
  }
}

function applyControlState() {
  const need = controlEnabled && !token;
  document.querySelectorAll("[data-out]").forEach(b => b.disabled = !controlEnabled || need);
  $("#asOn").disabled = $("#asOff").disabled = !controlEnabled || need;
  $("#tokenRow").style.display = controlEnabled ? "flex" : "none";
  $("#controlNote").textContent = !controlEnabled
    ? "Controls disabled (no auth_token configured on the bridge)."
    : need ? "Enter the control token to enable actions." : "";
}

async function control(output, enabled) {
  try {
    const r = await fetch("api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ output, enabled }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast(d.message || "ok");
    setTimeout(refreshState, 800);
  } catch (e) { toast("error: " + e.message); }
}

async function refreshAuto() {
  try {
    const r = await fetch("api/autoshutdown", { headers: authHeaders() });
    if (!r.ok) return;
    const a = await r.json();
    $("#asState").textContent = a.enabled
      ? (a.triggered ? "ENABLED · cut sent" : a.armed ? "ENABLED · armed" : "ENABLED")
      : "disabled";
    let d = `trigger ≤ ${a.trigger_soc_percent}%, recover ${a.recover_soc_percent}%, ` +
            `grace ${a.grace_period_seconds}s, cuts: ${(a.cut_outputs || []).join(", ") || "none"}`;
    if (a.seconds_until_cut != null) d += ` · cutting in ${Math.round(a.seconds_until_cut)}s`;
    $("#asDetail").textContent = d;
  } catch (e) {}
}

async function setAuto(enabled) {
  try {
    const r = await fetch("api/autoshutdown", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ enabled }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast("auto-shutdown " + (enabled ? "enabled" : "disabled"));
    refreshAuto();
  } catch (e) { toast("error: " + e.message); }
}

function drawChart(points) {
  const c = $("#chart"), ctx = c.getContext("2d");
  const W = c.width, H = c.height, pad = 28;
  ctx.clearRect(0, 0, W, H);
  if (!points.length) {
    ctx.fillStyle = "#8a93a6"; ctx.font = "13px system-ui";
    ctx.fillText("no data yet", pad, H / 2); return;
  }
  const xs = points.map((_, i) => i);
  const series = [
    { key: "soc_percent", color: "#7ee2a8", max: 100 },
    { key: "ac_output_watts", color: "#f2c969", max: null },
    { key: "ac_input_watts", color: "#a9c2ff", max: null },
  ];
  let wMax = 100;
  for (const p of points)
    wMax = Math.max(wMax, p.ac_output_watts || 0, p.ac_input_watts || 0);
  ctx.strokeStyle = "#222631"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, H - pad); ctx.lineTo(W - pad, H - pad); ctx.stroke();
  const xAt = i => pad + (xs.length < 2 ? 0 : (i / (xs.length - 1)) * (W - 2 * pad));
  for (const s of series) {
    const max = s.max || wMax;
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.8; ctx.beginPath();
    let started = false;
    points.forEach((p, i) => {
      const v = p[s.key];
      if (v == null) return;
      const y = H - pad - (v / max) * (H - 2 * pad);
      if (!started) { ctx.moveTo(xAt(i), y); started = true; }
      else ctx.lineTo(xAt(i), y);
    });
    ctx.stroke();
  }
}

async function refreshHistory() {
  if (!historyEnabled) return;
  try {
    const r = await fetch("api/history?minutes=" + historyMinutes, { headers: authHeaders() });
    const d = await r.json();
    drawChart(d.points || []);
    $("#historyNote").textContent = (d.points || []).length
      ? `${d.points.length} buckets over last ${fmtMins(historyMinutes)}`
      : "Collecting data… (Postgres logging on)";
  } catch (e) { $("#historyNote").textContent = "history unavailable"; }
}

document.querySelectorAll("[data-out]").forEach(b =>
  b.addEventListener("click", () => control(b.dataset.out, b.dataset.on === "1")));
$("#asOn").addEventListener("click", () => setAuto(true));
$("#asOff").addEventListener("click", () => setAuto(false));
$("#saveToken").addEventListener("click", () => {
  token = $("#token").value.trim();
  localStorage.setItem("ecoflow_token", token);
  toast("token saved"); applyControlState(); refreshState();
});
document.querySelectorAll(".range button").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".range button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); historyMinutes = +b.dataset.min; refreshHistory();
  }));

$("#token").value = token;
refreshState(); refreshAuto(); refreshHistory();
setInterval(refreshState, 4000);
setInterval(refreshAuto, 8000);
setInterval(refreshHistory, 30000);
</script>
</body>
</html>
"""
