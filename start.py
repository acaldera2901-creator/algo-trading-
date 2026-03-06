#!/usr/bin/env python3
"""
Avvio unificato: tunnel pubblico + trading agent.

Uso:  python start.py

Avvia automaticamente:
  1. Tunnel pubblico (localhost.run via SSH oppure bore.pub)
  2. HTTP bridge server sulla porta 8765
  3. Trading agent (loop continuo)

L'URL del tunnel viene stampato chiaramente — basta incollarlo
nei parametri ServerURL dell'EA su MT5.
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── Logging base (prima di importare config) ──────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("start")

BRIDGE_PORT = 8765
TUNNEL_URL_FILE = Path("tunnel_url.txt")

_tunnel_proc: subprocess.Popen | None = None


# ── Tunnel ────────────────────────────────────────────────────────────────────

def _start_localhost_run() -> str | None:
    """SSH tunnel via localhost.run — nessun account richiesto."""
    global _tunnel_proc
    try:
        proc = subprocess.Popen(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-o", "ConnectTimeout=15",
                "-R", f"80:localhost:{BRIDGE_PORT}",
                "nokey@localhost.run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _tunnel_proc = proc
        for _ in range(40):
            line = proc.stdout.readline()
            if not line:
                break
            if "https://" in line:
                parts = [p for p in line.strip().split() if "https://" in p]
                if parts:
                    return parts[0].rstrip(".")
            time.sleep(0.5)
    except Exception as e:
        logger.debug(f"localhost.run: {e}")
    return None


def _start_bore() -> str | None:
    """Tunnel via bore.pub — nessun account richiesto."""
    global _tunnel_proc
    bore_candidates = ["bore", "/tmp/bore", str(Path.home() / ".cargo/bin/bore")]
    bore_bin = next((b for b in bore_candidates if Path(b).exists() or
                     subprocess.run(["which", b], capture_output=True).returncode == 0), None)

    if not bore_bin:
        logger.info("bore non trovato — scarico il binario...")
        try:
            dl = subprocess.run(
                ["curl", "-sL", "-o", "/tmp/bore.tar.gz",
                 "https://github.com/ekzhang/bore/releases/latest/download/"
                 "bore-v0.5.0-x86_64-unknown-linux-musl.tar.gz"],
                capture_output=True, timeout=30,
            )
            if dl.returncode != 0:
                return None
            subprocess.run(["tar", "-xzf", "/tmp/bore.tar.gz", "-C", "/tmp/"], capture_output=True)
            subprocess.run(["chmod", "+x", "/tmp/bore"], capture_output=True)
            bore_bin = "/tmp/bore"
        except Exception as e:
            logger.debug(f"download bore: {e}")
            return None

    try:
        proc = subprocess.Popen(
            [bore_bin, "local", str(BRIDGE_PORT), "--to", "bore.pub"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _tunnel_proc = proc
        for _ in range(30):
            line = proc.stdout.readline()
            if "bore.pub:" in line:
                port_str = line.strip().split("bore.pub:")[-1].strip().split()[0]
                return f"http://bore.pub:{port_str}"
            time.sleep(0.5)
    except Exception as e:
        logger.debug(f"bore: {e}")
    return None


def start_tunnel() -> str | None:
    """Prova localhost.run poi bore.pub. Restituisce l'URL pubblico."""
    logger.info("Avvio tunnel (localhost.run)...")
    url = _start_localhost_run()
    if url:
        return url

    logger.info("localhost.run non disponibile — provo bore.pub...")
    url = _start_bore()
    return url


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _cleanup(sig=None, frame=None):
    global _tunnel_proc
    if _tunnel_proc:
        try:
            _tunnel_proc.terminate()
        except Exception:
            pass
    logger.info("Agent fermato.")
    sys.exit(0)


# ── Stampa URL in modo prominente ─────────────────────────────────────────────

def _print_url_box(url: str):
    border = "=" * 64
    print(f"\n{border}")
    print("  TUNNEL ATTIVO — CONFIGURA L'EA MT5 CON QUESTO URL:")
    print(border)
    print(f"\n    ServerURL  =  {url}\n")
    print("  Passi da fare su MT5 (una sola volta per account):")
    print(f"  1. Strumenti → Opzioni → Consulenti Esperti")
    print(f"     Abilita WebRequest e aggiungi l'URL qui sopra")
    print(f"  2. Apri grafico XAUUSD H1 → trascina AlgoTradingBridge")
    print(f"  3. Parametri EA:  ServerURL = {url}")
    print(f"  4. Abilita AutoTrading (pulsante verde in alto a sinistra)")
    print(f"\n  Attendi ~10s — nel log apparirà: [Bridge] EA heartbeat")
    print(f"{border}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    print("\n" + "=" * 64)
    print("  ALGO TRADING AGENT — Avvio")
    print("=" * 64 + "\n")

    # 1. Tunnel
    url = start_tunnel()

    if url:
        TUNNEL_URL_FILE.write_text(url)
        _print_url_box(url)
    else:
        print("\n[AVVISO] Tunnel non disponibile.")
        print("Se MT5 è sulla stessa rete locale usa:")
        local_ip = _get_local_ip()
        print(f"  ServerURL = http://{local_ip}:{BRIDGE_PORT}\n")

    # 2. Agent
    import config

    logger.info(f"Broker: {config.BROKER.upper()} | DRY_RUN: {config.DRY_RUN}")
    logger.info(f"Symbols: {config.SYMBOLS} | Timeframe: {config.TIMEFRAME}")
    if not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY non impostata — analisi AI disabilitata, uso solo regole SMC")

    from agent.core import TradingAgent
    agent = TradingAgent()
    agent.run_loop()


def _get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    main()
