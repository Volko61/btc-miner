#!/usr/bin/env python3
"""
Dashboard de minage Bitcoin - hashrate total et gains en temps reel.

Concu pour etre filme : gros chiffres, fond sombre, compteurs animes.
Agrege tous les workers d'un compte Braiins Pool (ESP32 + PC + GPU Salad).

    python3 dashboard.py --token TON_TOKEN_BRAIINS
    python3 dashboard.py --demo        # donnees simulees, pour tester l'affichage

Aucune dependance : stdlib uniquement.
"""

import argparse
import http.server
import json
import math
import random
import socketserver
import threading
import time
import urllib.request

POOL_PROFILE = "https://pool.braiins.com/accounts/profile/json/btc/"
POOL_WORKERS = "https://pool.braiins.com/accounts/workers/json/btc/"
PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=eur"

# Braiins tolere ~1 requete / 5s. On reste large.
POLL_INTERVAL = 8
PRICE_INTERVAL = 120

STATE = {
    "hashrate": 0.0,          # H/s agrege
    "workers": [],            # [{name, hashrate, state}]
    "reward_btc": 0.0,        # solde courant non paye
    "reward_24h_btc": 0.0,
    "price_eur": 0.0,
    "updated": 0,
    "error": None,
    "cost_per_hour": 0.0,
    "started": time.time(),
}


def fetch_json(url, token=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "btc-miner-dashboard/1.0")
    if token:
        req.add_header("Pool-Auth-Token", token)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def parse_hashrate(value, unit):
    """Braiins renvoie un nombre + une unite ('Gh/s', 'Th/s'...). On normalise en H/s."""
    mult = {
        "h/s": 1, "kh/s": 1e3, "mh/s": 1e6, "gh/s": 1e9,
        "th/s": 1e12, "ph/s": 1e15, "eh/s": 1e18,
    }
    try:
        return float(value) * mult.get(str(unit).lower().strip(), 1)
    except (TypeError, ValueError):
        return 0.0


def poll_pool(token):
    profile = fetch_json(POOL_PROFILE, token)
    workers = fetch_json(POOL_WORKERS, token)

    btc = profile.get("btc", profile)
    STATE["reward_btc"] = float(btc.get("current_balance", 0) or 0)
    STATE["reward_24h_btc"] = float(btc.get("today_reward", 0) or 0)

    wl = workers.get("btc", workers)
    out, total = [], 0.0
    for name, w in (wl.items() if isinstance(wl, dict) else []):
        hr = parse_hashrate(w.get("hash_rate_unit_5m", w.get("hash_rate_5m", 0)),
                            w.get("hash_rate_unit", "Gh/s"))
        if hr == 0.0:
            hr = parse_hashrate(w.get("hash_rate_5m", 0), w.get("hash_rate_unit", "Gh/s"))
        total += hr
        out.append({"name": name, "hashrate": hr, "state": w.get("state", "?")})

    out.sort(key=lambda x: -x["hashrate"])
    STATE["workers"] = out
    STATE["hashrate"] = total
    STATE["updated"] = time.time()
    STATE["error"] = None


def poll_demo():
    """Donnees simulees : un ESP32, dix PC, dix RTX 5090."""
    t = time.time() - STATE["started"]
    out = [{"name": "esp32", "hashrate": 50e3 * (0.9 + 0.2 * random.random()), "state": "OK"}]
    for i in range(1, 11):
        out.append({"name": f"pc{i}", "hashrate": 90e6 * (0.9 + 0.2 * random.random()), "state": "OK"})
    for i in range(1, 11):
        ramp = min(1.0, t / 45.0)  # montee en charge progressive, comme le JIT PTX
        out.append({"name": f"salad{i}", "hashrate": 2.5e9 * ramp * (0.9 + 0.2 * random.random()),
                    "state": "OK" if ramp > 0.3 else "BOOT"})
    out.sort(key=lambda x: -x["hashrate"])
    STATE["workers"] = out
    STATE["hashrate"] = sum(w["hashrate"] for w in out)
    STATE["reward_btc"] = 0.00000112 + t * 4e-11
    STATE["reward_24h_btc"] = 0.00000098
    STATE["price_eur"] = 91500.0
    STATE["updated"] = time.time()
    STATE["error"] = None


def poller(token, demo):
    last_price = 0
    while True:
        try:
            if demo:
                poll_demo()
            else:
                poll_pool(token)
                if time.time() - last_price > PRICE_INTERVAL:
                    try:
                        STATE["price_eur"] = float(
                            fetch_json(PRICE_URL)["bitcoin"]["eur"])
                        last_price = time.time()
                    except Exception:
                        pass  # le prix n'est pas critique, on garde l'ancien
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
        time.sleep(2 if demo else POLL_INTERVAL)


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>Minage Bitcoin - temps reel</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background:#07090c; color:#e8eef7; font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
         min-height:100vh; padding:4vh 4vw; display:flex; flex-direction:column; gap:3vh; }
  .row { display:flex; gap:3vw; flex-wrap:wrap; }
  .card { flex:1 1 340px; background:linear-gradient(160deg,#111823,#0a0e14);
          border:1px solid #1e2836; border-radius:20px; padding:3vh 2.5vw; }
  .label { font-size:clamp(11px,1.1vw,15px); letter-spacing:.18em; text-transform:uppercase;
           color:#6b7d94; margin-bottom:1.4vh; }
  .big { font-size:clamp(38px,6.5vw,104px); font-weight:800; line-height:1;
         font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .unit { font-size:.4em; font-weight:600; color:#7f95b0; margin-left:.25em; }
  .orange { color:#f7931a; }
  .green  { color:#3ddc97; }
  .sub { margin-top:1.2vh; font-size:clamp(12px,1.2vw,17px); color:#7f95b0;
         font-variant-numeric:tabular-nums; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th { text-align:left; font-size:clamp(10px,.85vw,13px); letter-spacing:.16em;
       text-transform:uppercase; color:#5d6f86; padding-bottom:1.2vh; font-weight:600; }
  td { padding:.85vh 0; font-size:clamp(13px,1.25vw,19px); border-top:1px solid #161f2b; }
  td.n { text-align:right; font-weight:700; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
         background:#3ddc97; margin-right:.7em; vertical-align:middle; }
  .dot.boot { background:#f7931a; }
  .dot.off  { background:#4a5768; }
  canvas { width:100%; height:14vh; display:block; }
  .err { background:#3a1414; border:1px solid #7d2b2b; color:#ffb4b4;
         padding:1.4vh 1.6vw; border-radius:12px; font-size:14px; }
  .foot { color:#4a5768; font-size:12px; letter-spacing:.05em; }
</style>

<div id="err"></div>

<div class="row">
  <div class="card">
    <div class="label">Puissance de calcul</div>
    <div class="big orange"><span id="hr">0</span><span class="unit" id="hru">H/s</span></div>
    <div class="sub" id="hrsub">&nbsp;</div>
  </div>
  <div class="card">
    <div class="label">Gains accumules</div>
    <div class="big green"><span id="eur">0.000</span><span class="unit">EUR</span></div>
    <div class="sub" id="btc">&nbsp;</div>
  </div>
</div>

<div class="card">
  <div class="label">Hashrate (5 dernieres minutes)</div>
  <canvas id="spark"></canvas>
</div>

<div class="card">
  <div class="label">Machines</div>
  <table>
    <thead><tr><th>Worker</th><th style="text-align:right">Hashrate</th><th style="text-align:right">Part</th></tr></thead>
    <tbody id="tb"></tbody>
  </table>
</div>

<div class="foot" id="foot"></div>

<script>
const hist = [];
const fmtHash = h => {
  const u = ["H/s","kH/s","MH/s","GH/s","TH/s","PH/s","EH/s"];
  let i = 0; while (h >= 1000 && i < u.length-1) { h /= 1000; i++; }
  return [h.toFixed(h < 10 ? 2 : h < 100 ? 1 : 0), u[i]];
};

// Interpolation vers la valeur cible : le compteur glisse au lieu de sauter.
let shown = 0, target = 0;
function animate() {
  shown += (target - shown) * 0.08;
  const [v, u] = fmtHash(shown);
  document.getElementById("hr").textContent = v;
  document.getElementById("hru").textContent = u;
  requestAnimationFrame(animate);
}
animate();

function draw() {
  const c = document.getElementById("spark"), x = c.getContext("2d");
  const w = c.width = c.offsetWidth * 2, h = c.height = c.offsetHeight * 2;
  x.clearRect(0,0,w,h);
  if (hist.length < 2) return;
  const max = Math.max(...hist) * 1.15 || 1;
  x.beginPath();
  hist.forEach((v,i) => {
    const px = i / (hist.length-1) * w, py = h - (v/max) * h * 0.92 - h*0.04;
    i ? x.lineTo(px,py) : x.moveTo(px,py);
  });
  x.strokeStyle = "#f7931a"; x.lineWidth = 5; x.lineJoin = "round"; x.stroke();
  x.lineTo(w,h); x.lineTo(0,h); x.closePath();
  const g = x.createLinearGradient(0,0,0,h);
  g.addColorStop(0,"rgba(247,147,26,.28)"); g.addColorStop(1,"rgba(247,147,26,0)");
  x.fillStyle = g; x.fill();
}

async function tick() {
  try {
    const d = await (await fetch("/api")).json();
    document.getElementById("err").innerHTML =
      d.error ? '<div class="err">Pool injoignable — ' + d.error + '</div>' : '';

    target = d.hashrate;
    hist.push(d.hashrate); if (hist.length > 150) hist.shift();
    draw();

    const eur = d.reward_btc * d.price_eur;
    document.getElementById("eur").textContent = eur.toFixed(4);
    document.getElementById("btc").textContent =
      d.reward_btc.toFixed(8) + " BTC" +
      (d.price_eur ? "   ·   1 BTC = " + d.price_eur.toLocaleString("fr-FR") + " EUR" : "");

    const live = d.workers.filter(w => w.hashrate > 0).length;
    document.getElementById("hrsub").textContent =
      live + " / " + d.workers.length + " machines actives";

    document.getElementById("tb").innerHTML = d.workers.map(w => {
      const [v,u] = fmtHash(w.hashrate);
      const pct = d.hashrate ? (w.hashrate/d.hashrate*100) : 0;
      const cls = w.hashrate === 0 ? "off" : (w.state === "BOOT" ? "boot" : "");
      return `<tr><td><span class="dot ${cls}"></span>${w.name}</td>
              <td class="n">${v} ${u}</td>
              <td class="n" style="color:#6b7d94">${pct.toFixed(1)}%</td></tr>`;
    }).join("");

    document.getElementById("foot").textContent =
      "Mis a jour " + new Date(d.updated*1000).toLocaleTimeString("fr-FR");
  } catch (e) {
    document.getElementById("err").innerHTML =
      '<div class="err">Dashboard deconnecte du script local</div>';
  }
}
tick(); setInterval(tick, 2000);
window.addEventListener("resize", draw);
</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(STATE).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # silence : la console reste lisible pendant le tournage


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", help="token API Braiins (Settings > Access Profiles)")
    p.add_argument("--demo", action="store_true", help="donnees simulees")
    p.add_argument("--port", type=int, default=842)
    a = p.parse_args()

    if not a.demo and not a.token:
        p.error("il faut --token TON_TOKEN, ou --demo pour tester l'affichage")

    threading.Thread(target=poller, args=(a.token, a.demo), daemon=True).start()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", a.port), Handler) as srv:
        print(f"Dashboard : http://127.0.0.1:{a.port}"
              + ("   [MODE DEMO]" if a.demo else ""))
        srv.serve_forever()


if __name__ == "__main__":
    main()
