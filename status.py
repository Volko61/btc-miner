#!/usr/bin/env python3
"""
Passerelle HTTP devant l'API de ccminer.

ccminer expose ses stats sur un port TCP en protocole maison
(`summary` -> "NAME=ccminer;VER=...;KHS=2847.32;ACC=12;..."), que le proxy
HTTP de SaladCloud ne sait pas relayer. Ce script traduit ca en JSON sur
/status, ce qui permet de lire le hashrate reel d'un replica a distance,
sans passer par l'export CSV du Log Explorer.

Sert aussi de sonde de sante : tant que /status repond avec un hashrate > 0,
le binaire tourne ET le GPU calcule vraiment.
"""

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CCMINER_API = ("127.0.0.1", int(os.environ.get("CCMINER_API_PORT", "4068")))
HTTP_PORT = int(os.environ.get("STATUS_PORT", "8080"))

STATE = {"boot": time.time(), "last_error": None, "gpu": None}


def ask_ccminer(command: str) -> str:
    """Une requete = une connexion TCP, ccminer ferme apres avoir repondu."""
    with socket.create_connection(CCMINER_API, timeout=4) as s:
        s.sendall(command.encode() + b"\n")
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks).decode("utf-8", "replace")


def parse_kv(raw: str) -> dict:
    """"A=1;B=2;|" -> {"A": "1", "B": "2"}"""
    out = {}
    for part in raw.replace("|", ";").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def detect_gpu():
    # Pas d'annotation "str | None" ici : l'image runtime est sur Ubuntu 20.04,
    # donc Python 3.8, ou cette syntaxe est une erreur de syntaxe.
    """Nom du GPU vu par le driver. Confirme que la carte est bien attribuee."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        name = out.stdout.strip().splitlines()
        return name[0].strip() if name else None
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {
            "uptime_s": round(time.time() - STATE["boot"], 1),
            "gpu": STATE["gpu"],
            "worker": os.environ.get("WORKER"),
            "algo": os.environ.get("ALGO"),
            "hashrate_hs": 0.0,
            "accepted": None,
            "rejected": None,
            "miner_up": False,
            "error": None,
        }
        try:
            summary = parse_kv(ask_ccminer("summary"))
            # KHS = kilohashes/s dans l'API ccminer.
            payload["hashrate_hs"] = float(summary.get("KHS", 0) or 0) * 1000
            payload["accepted"] = summary.get("ACC")
            payload["rejected"] = summary.get("REJ")
            payload["algo"] = summary.get("ALGO") or payload["algo"]
            payload["miner_up"] = True
        except Exception as e:
            payload["error"] = f"{type(e).__name__}: {e}"

        body = json.dumps(payload, indent=1).encode()
        # 200 des que le miner repond, 503 sinon : utilisable tel quel comme
        # readiness probe cote Salad.
        self.send_response(200 if payload["miner_up"] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    STATE["gpu"] = detect_gpu()
    print(f"[status] GPU detecte : {STATE['gpu']}", flush=True)
    print(f"[status] ecoute sur 0.0.0.0:{HTTP_PORT}", flush=True)
    HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
