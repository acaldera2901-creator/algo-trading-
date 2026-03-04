"""
MT5 Bridge Server — run this on your Windows machine with MT5 open.

This script starts an rpyc server that allows the Linux bot to
communicate with MetaTrader5 running on Windows.

Requirements (Windows only):
    pip install MetaTrader5 mt5linux

Steps:
    1. Open MetaTrader5 terminal and log in (login: 5047463257, server: MetaQuotes-Demo)
    2. Run this script on Windows: python mt5_bridge_server.py
    3. In .env on Linux, set: MT5_HOST=<your_windows_ip>
    4. The bot on Linux will then connect automatically

Security note:
    The server listens on port 18812. Restrict access via firewall to
    only the Linux machine's IP address.
"""

import sys

def main():
    try:
        from mt5linux import MetaTrader5
    except ImportError:
        print("ERROR: Run this script on Windows with mt5linux installed.")
        print("Install: pip install MetaTrader5 mt5linux")
        sys.exit(1)

    host = "0.0.0.0"  # Listen on all interfaces
    port = 18812

    print(f"Starting MT5 bridge server on {host}:{port}")
    print("MT5 terminal must be open and logged in.")
    print("Press Ctrl+C to stop.")

    try:
        mt5 = MetaTrader5(host=host, port=port)
        mt5.run_server()
    except KeyboardInterrupt:
        print("\nBridge server stopped.")


if __name__ == "__main__":
    main()
