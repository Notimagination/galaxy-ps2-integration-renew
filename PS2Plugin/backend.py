import html
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import config


CSS_FILE = os.path.join(os.path.dirname(__file__), "website", "css", "main.css")


class AuthenticationHandler(BaseHTTPRequestHandler):
    def _css_text(self):
        try:
            with open(CSS_FILE, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def log_message(self, format, *args):
        return

    def _set_headers(self, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/css/main.css":
            self._set_headers("text/css; charset=utf-8")
            try:
                with open(CSS_FILE, "rb") as fh:
                    self.wfile.write(fh.read())
            except OSError:
                self.wfile.write(b"")
            return
        if path == "/setconfig":
            self._save_config(parse_qs(urlparse(self.path).query))
            self._set_headers("text/html; charset=utf-8")
            self.wfile.write(b'<script>window.location="/end";</script>')
            return
        if path == "/end":
            self._set_headers("text/html; charset=utf-8")
            body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{self._css_text()}</style><title>Configuration saved</title></head><body><div class="backdrop"></div><main class="shell"><section class="success-card"><div class="success-icon">&#10003;</div><div class="eyebrow">PS2 INTEGRATION</div><h1>Configuration saved</h1><p>You can close this window and return to GOG Galaxy.</p></section></main></body></html>"""
            self.wfile.write(body.encode("utf-8"))
            return
        self._set_headers("text/html; charset=utf-8")
        self.wfile.write(self._render_index().encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/setconfig":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(max(0, min(length, 64 * 1024)))
            params = parse_qs(raw.decode("utf-8", "replace"))
            self._save_config(params)
            self._set_headers("text/html; charset=utf-8")
            self.wfile.write(b'<script>window.location="/end";</script>')
            return
        self.send_error(404)

    def _load_config(self):
        parser = config.Config().cfg
        parser.read(os.path.expandvars(config.CONFIG_LOC))
        return parser

    def _render_index(self):
        parser = self._load_config()
        roms = html.escape(parser.get("Paths", "roms_path", fallback=""))
        emu = html.escape(parser.get("Paths", "emu_path", fallback=""))
        cfg = html.escape(parser.get("Paths", "config_path", fallback=""))
        full = "checked" if parser.getboolean("EmuSettings", "emu_fullscreen", fallback=False) else ""
        nogui = "checked" if parser.getboolean("EmuSettings", "emu_no_gui", fallback=False) else ""
        pergame = "checked" if parser.getboolean("EmuSettings", "emu_config", fallback=False) else ""
        return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PlayStation 2 Integration · GOG Galaxy</title><style>{self._css_text()}</style></head>
<body><div class="backdrop"></div><main class="shell">
<header class="topbar"><div class="brand"><div class="brand-mark">PS2</div><div><div class="brand-title">PlayStation 2 Integration</div><div class="brand-subtitle">GOG Galaxy · PCSX2</div></div></div><div class="pill"><span class="status-dot"></span>Local library</div></header>
<section class="hero-card"><div class="hero-art"><div class="disc disc-back"></div><div class="disc disc-front"></div><div class="console-word">PLAYSTATION 2</div></div><div class="hero-copy"><div class="eyebrow">GALAXY PLUGIN</div><h1>Your PS2 library, one click away.</h1><p>Connect your game folder and PCSX2 executable. The plugin identifies discs by their real PS2 serial, imports them into GOG Galaxy and launches them directly from your library.</p></div></section>
<form class="config-form" method="POST" action="/setconfig">
<section class="card"><div class="section-heading"><div><div class="eyebrow">01 · PATHS</div><h2>Library & emulator</h2></div><span class="section-badge">Required</span></div><div class="field-grid"><label class="field full"><span>Games location</span><input name="romspath" value="{roms}" placeholder="Path to the game ROMs" required></label><label class="field full"><span>PCSX2 location</span><input name="emupath" value="{emu}" placeholder="Path to the emulator executable" required></label><label class="field full"><span>Per-game config folder <em>Optional</em></span><input name="configpath" value="{cfg}" placeholder="Path to PCSX2 config folder"></label></div><div class="hint"><span class="hint-icon">i</span><span>Supported images: .ISO, .BIN and .GZ. Subfolders are scanned automatically.</span></div></section>
<section class="card"><div class="section-heading"><div><div class="eyebrow">02 · DETECTION</div><h2>Automatic identification</h2></div><span class="section-badge accent">Automatic</span></div><div class="feature-row"><div class="feature-icon">ID</div><div><strong>Disc serial first</strong><small>The plugin reads the real PS2 serial from SYSTEM.CNF / disc data whenever possible, so the regional release in the image wins over the filename.</small></div></div><div class="feature-row"><div class="feature-icon">DB</div><div><strong>Metadata database</strong><small>Official titles and extra game information are enriched from a cached PS2 database. Covers use serial-based PCSX2 cover URLs.</small></div></div><div class="feature-row"><div class="feature-icon">◎</div><div><strong>Mixed regions supported</strong><small>Different NTSC-U, PAL and NTSC-J releases can coexist in the same library. No global region selector is needed.</small></div></div></section>
<section class="card"><div class="section-heading"><div><div class="eyebrow">03 · PCSX2</div><h2>Launch options</h2></div><span class="section-badge">Optional</span></div><div class="toggle-grid"><label class="toggle"><input type="checkbox" name="fullscreen" value="1" {full}><span class="toggle-track"></span><span><strong>Fullscreen</strong><small>Launch PCSX2 in fullscreen.</small></span></label><label class="toggle"><input type="checkbox" name="nogui" value="1" {nogui}><span class="toggle-track"></span><span><strong>No GUI</strong><small>Launch the game without the main PCSX2 window.</small></span></label><label class="toggle"><input type="checkbox" name="config" value="1" {pergame}><span class="toggle-track"></span><span><strong>Per-game config</strong><small>Keep the legacy per-game configuration behavior enabled.</small></span></label></div></section>
<div class="actions"><div class="footer-note"><span class="status-dot"></span>Configuration is stored locally for this plugin. &middot; PS2Plugin by Notimagination</div><button type="submit"><span>Save configuration</span><span class="button-arrow">→</span></button></div>
</form></main></body></html>'''

    def _save_config(self, params=None):
        params = params or parse_qs(urlparse(self.path).query)
        parser = self._load_config()
        parser["Paths"]["roms_path"] = params.get("romspath", [""])[0]
        parser["Paths"]["emu_path"] = params.get("emupath", [""])[0]
        parser["Paths"]["config_path"] = params.get("configpath", [""])[0]
        parser["EmuSettings"]["emu_fullscreen"] = "True" if "fullscreen" in params else "False"
        parser["EmuSettings"]["emu_no_gui"] = "True" if "nogui" in params else "False"
        parser["EmuSettings"]["emu_config"] = "True" if "config" in params else "False"
        target = os.path.expandvars(config.CONFIG_LOC)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        buf = io.StringIO()
        parser.write(buf)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())


class AuthenticationServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.httpd = HTTPServer(("localhost", 0), AuthenticationHandler)
        self.port = self.httpd.server_port

    def run(self):
        self.httpd.serve_forever()
