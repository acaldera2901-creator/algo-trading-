"""
Tunnel Manager — espone la porta 8765 del bridge HTTP su URL pubblico.

Metodi provati in ordine:
1. bore.pub  (nessun account richiesto)
2. localhost.run via SSH (nessun account richiesto)
3. pyngrok   (richiede token gratuito su ngrok.com)

Uso:
    python tunnel.py

L'URL pubblico viene salvato in tunnel_url.txt e mostrato a schermo.
L'EA MT5 deve usare quell'URL come ServerURL nei parametri.
"""

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TUNNEL] %(message)s",
)
logger = logging.getLogger("tunnel")

TUNNEL_URL_FILE = Path("tunnel_url.txt")
BRIDGE_PORT = 8765


def try_bore() -> str | None:
    """
    Tenta connessione a bore.pub — nessun account richiesto.
    Richiede 'bore' installato (cargo install bore-cli oppure download binario).
    """
    try:
        result = subprocess.run(["which", "bore"], capture_output=True, text=True)
        if result.returncode != 0:
            # Prova a scaricare il binario bore
            logger.info("bore non trovato, provo a scaricarlo...")
            dl = subprocess.run(
                ["curl", "-sL", "-o", "/tmp/bore",
                 "https://github.com/ekzhang/bore/releases/latest/download/bore-v0.5.0-x86_64-unknown-linux-musl.tar.gz"],
                capture_output=True, timeout=20
            )
            if dl.returncode != 0:
                return None
            subprocess.run(["tar", "-xzf", "/tmp/bore", "-C", "/tmp/"], capture_output=True)
            subprocess.run(["chmod", "+x", "/tmp/bore"], capture_output=True)
            bore_bin = "/tmp/bore"
        else:
            bore_bin = "bore"

        proc = subprocess.Popen(
            [bore_bin, "local", str(BRIDGE_PORT), "--to", "bore.pub"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Aspetta URL
        for _ in range(20):
            line = proc.stdout.readline()
            if "bore.pub:" in line:
                port_str = line.strip().split("bore.pub:")[-1].strip().split()[0]
                url = f"http://bore.pub:{port_str}"
                logger.info(f"bore.pub tunnel: {url}")
                return url
            time.sleep(0.5)
    except Exception as e:
        logger.debug(f"bore fallito: {e}")
    return None


def try_localhost_run() -> str | None:
    """
    localhost.run via SSH — nessun account richiesto.
    Richiede ssh installato (sempre disponibile su Linux).
    """
    try:
        proc = subprocess.Popen(
            [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ServerAliveInterval=30",
                "-R", f"80:localhost:{BRIDGE_PORT}",
                "nokey@localhost.run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(30):
            line = proc.stdout.readline()
            if "https://" in line:
                parts = [p for p in line.strip().split() if "https://" in p]
                if parts:
                    url = parts[0].rstrip(".")
                    logger.info(f"localhost.run tunnel: {url}")
                    return url
            time.sleep(0.5)
    except Exception as e:
        logger.debug(f"localhost.run fallito: {e}")
    return None


def try_pyngrok(port: int = BRIDGE_PORT) -> str | None:
    """
    pyngrok — richiede account gratuito su ngrok.com e token.
    Se NGROK_AUTH_TOKEN è impostato in .env lo usa.
    """
    try:
        from pyngrok import ngrok, conf
        import os
        token = os.getenv("NGROK_AUTH_TOKEN", "")
        if token:
            conf.get_default().auth_token = token
        tunnel = ngrok.connect(port, "http")
        url = tunnel.public_url
        logger.info(f"ngrok tunnel: {url}")
        return url
    except Exception as e:
        logger.debug(f"pyngrok fallito: {e}")
    return None


def save_url(url: str):
    """Salva l'URL del tunnel in un file."""
    TUNNEL_URL_FILE.write_text(url)
    logger.info(f"URL salvato in {TUNNEL_URL_FILE}")


def main():
    logger.info(f"Avvio tunnel per porta {BRIDGE_PORT}...")
    logger.info("Provo metodi in ordine: bore → localhost.run → pyngrok")

    url = None

    # Metodo 1: pyngrok (più stabile se ha il token)
    import os
    if os.getenv("NGROK_AUTH_TOKEN"):
        url = try_pyngrok()

    # Metodo 2: localhost.run
    if not url:
        url = try_localhost_run()

    # Metodo 3: bore.pub
    if not url:
        url = try_bore()

    if not url:
        logger.error("Tutti i metodi di tunnel falliti.")
        logger.error("Opzioni:")
        logger.error("  1. Registrati su https://ngrok.com (gratis), prendi il token,")
        logger.error("     aggiungi NGROK_AUTH_TOKEN=<token> in .env e riprova")
        logger.error("  2. Usa il tuo provider cloud per un port forward")
        logger.error("  3. Metti il Mac e questo server sulla stessa LAN/VPN")
        sys.exit(1)

    save_url(url)

    print("\n" + "=" * 60)
    print("  TUNNEL ATTIVO")
    print("=" * 60)
    print(f"\n  URL pubblico: {url}")
    print(f"\n  Configura l'EA MT5:")
    print(f"  1. Apri Strumenti → Opzioni → Consulenti Esperti")
    print(f"  2. Abilita WebRequest e aggiungi: {url}")
    print(f"  3. Riapri i parametri dell'EA")
    print(f"  4. Imposta ServerURL = {url}")
    print(f"\n  File URL: {TUNNEL_URL_FILE.absolute()}")
    print("=" * 60 + "\n")

    # Mantieni il tunnel vivo
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Tunnel chiuso.")


if __name__ == "__main__":
    main()
