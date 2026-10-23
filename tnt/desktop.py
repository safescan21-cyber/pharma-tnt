"""Desktop application launcher.

Two modes, chosen at first start (and changeable later):
  connect     - the app is a window onto the company's central server (normal multi-site setup)
  standalone  - the full system runs on this computer with a local database (single site / offline / demo)

Usage:
  PharmaTnT                       start with saved settings (asks on first run)
  PharmaTnT --mode connect --server https://tnt.customer.com
  PharmaTnT --mode standalone
  PharmaTnT --reset               forget saved settings
  PharmaTnT --no-window           don't open a window (standalone: serve and open the browser)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Pharma Track & Trace"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / "PharmaTnT"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "PharmaTnT"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "pharma-tnt"
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG = data_dir() / "settings.json"


def load_config() -> dict | None:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2))


# ------------------------------------------------------------- first-run dialog
def ask_mode(previous: dict | None = None) -> dict | None:
    """Small native dialog (tkinter, part of the standard library)."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("No GUI toolkit available; starting in standalone mode "
              "(use --mode connect --server URL to connect to a server).")
        return {"mode": "standalone"}

    result: dict = {}
    root = tk.Tk()
    root.title(f"{APP_NAME} - Setup")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=20)
    frm.grid()
    ttk.Label(frm, text=APP_NAME, font=("", 14, "bold")).grid(column=0, row=0, sticky="w")
    ttk.Label(frm, text="How should this computer work?").grid(column=0, row=1, sticky="w", pady=(4, 12))
    mode = tk.StringVar(value=(previous or {}).get("mode", "connect"))
    ttk.Radiobutton(frm, text="Connect to my company's server (recommended)", variable=mode,
                    value="connect").grid(column=0, row=2, sticky="w")
    ttk.Label(frm, text="Server address:").grid(column=0, row=3, sticky="w", padx=(22, 0))
    server = tk.StringVar(value=(previous or {}).get("server", "https://"))
    ttk.Entry(frm, textvariable=server, width=48).grid(column=0, row=4, sticky="w", padx=(22, 0), pady=(0, 10))
    ttk.Radiobutton(frm, text="Standalone - run everything on this computer", variable=mode,
                    value="standalone").grid(column=0, row=5, sticky="w")
    ttk.Label(frm, text=f"Data is stored in {data_dir()}", foreground="gray").grid(
        column=0, row=6, sticky="w", padx=(22, 0), pady=(0, 14))

    def ok():
        if mode.get() == "connect":
            url = server.get().strip().rstrip("/")
            if not url.startswith(("http://", "https://")) or len(url) < 10:
                messagebox.showerror(APP_NAME, "Enter the server address, e.g. https://tnt.yourcompany.com")
                return
            if not server_alive(url):
                if not messagebox.askyesno(APP_NAME, f"Cannot reach {url} right now.\nSave anyway?"):
                    return
            result.update(mode="connect", server=url)
        else:
            result.update(mode="standalone")
        root.destroy()

    bar = ttk.Frame(frm)
    bar.grid(column=0, row=7, sticky="e")
    ttk.Button(bar, text="Cancel", command=root.destroy).grid(column=0, row=0, padx=4)
    ttk.Button(bar, text="Continue", command=ok).grid(column=1, row=0)
    root.mainloop()
    return result or None


def show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, message)
        r.destroy()
    except Exception:
        print("ERROR:", message, file=sys.stderr)


# ------------------------------------------------------------ local server
def server_alive(url: str, timeout: float = 4) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_local_server(port: int | None = None) -> str:
    """Run the full API + web app on localhost in a background thread."""
    os.environ.setdefault("TNT_DB", str(data_dir() / "tnt.db"))
    os.environ.setdefault("TNT_IDLE_MINUTES", "30")
    import uvicorn

    from . import api  # imported after TNT_DB is set

    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning",
                                           workers=1, lifespan="on"))
    threading.Thread(target=server.run, daemon=True, name="tnt-server").start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server_alive(url, 1):
            return url
        time.sleep(0.1)
    raise RuntimeError("local server did not start")


# ------------------------------------------------------------------ window
def open_window(url: str) -> bool:
    """Native window via pywebview. Returns True if the user asked to change settings."""
    try:
        import webview
    except ImportError:
        return open_browser(url)

    want_settings = {"flag": False}

    def change_settings():
        want_settings["flag"] = True
        for w in list(webview.windows):
            w.destroy()

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window(APP_NAME, url, width=1320, height=880, min_size=(380, 600),
                                   confirm_close=False, text_select=True)
    kwargs = {}
    try:
        from webview.menu import Menu, MenuAction
        kwargs["menu"] = [Menu("Settings", [MenuAction("Change connection...", change_settings)])]
    except ImportError:
        pass
    webview.start(private_mode=False, storage_path=str(data_dir() / "webview"), **kwargs)
    return want_settings["flag"]


def open_browser(url: str) -> bool:
    print(f"{APP_NAME} is running at {url}  (press Ctrl+C to quit)")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return False


# -------------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="PharmaTnT", description=APP_NAME)
    p.add_argument("--mode", choices=["connect", "standalone"])
    p.add_argument("--server", help="central server URL (connect mode)")
    p.add_argument("--port", type=int, help="fixed local port (standalone mode)")
    p.add_argument("--reset", action="store_true", help="forget saved settings")
    p.add_argument("--no-window", action="store_true", help="do not open a native window")
    a = p.parse_args(argv)

    if a.reset and CONFIG.exists():
        CONFIG.unlink()
    cfg = load_config()
    if a.mode:
        cfg = {"mode": a.mode, **({"server": a.server.rstrip("/")} if a.server else {})}
        save_config(cfg)

    while True:
        if not cfg:
            cfg = ask_mode()
            if not cfg:
                return 0
            save_config(cfg)
        if cfg["mode"] == "connect":
            url = cfg.get("server", "")
            if not server_alive(url):
                show_error(f"Cannot reach the server {url}.\nCheck the network or change the connection.")
                cfg = ask_mode(cfg)
                if not cfg:
                    return 1
                save_config(cfg)
                continue
        else:
            try:
                url = start_local_server(a.port)
            except Exception as e:  # port in use, unreadable database, ...
                show_error(f"Could not start the local database: {e}")
                return 1
        if a.no_window:
            open_browser(url)
            return 0
        if open_window(url):
            if cfg["mode"] == "standalone":
                # the local server thread can't be restarted in-process; apply on next launch
                cfg = ask_mode(cfg)
                if cfg:
                    save_config(cfg)
                    show_error("Settings saved. Please restart the application.")
                return 0
            cfg = ask_mode(cfg)
            if not cfg:
                return 0
            save_config(cfg)
            continue
        return 0


if __name__ == "__main__":
    sys.exit(main())
