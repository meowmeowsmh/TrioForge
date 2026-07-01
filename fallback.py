#!/usr/bin/env python3
"""
Reverse proxy that forwards to Flask on port 5001 (HTTPS).
Serves static files and 404.html when Flask is down.
Handles client disconnections gracefully.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import os
import sys
import traceback
import socket
import http.client
import ssl

PORT = 5000
FLASK_PORT = 5001
FLASK_URL = f"https://127.0.0.1:{FLASK_PORT}"
STATIC_404 = "404.html"
STATIC_EXTENSIONS = {'.mp3', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg'}

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.handle_request("GET")
    def do_POST(self):
        self.handle_request("POST")
    def do_HEAD(self):
        self.handle_request("HEAD")

    def safe_write(self, data):
        """Write data to the client, ignoring broken pipe errors."""
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, socket.error) as e:
            # Client disconnected – just log and ignore
            print(f"⚠️ Client disconnected while writing: {e}")

    def handle_request(self, method):
        try:
            path = self.path.split('?')[0]

            # Serve static files directly if they exist
            if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
                if os.path.exists(path.lstrip('/')):
                    self.serve_static_file(path.lstrip('/'))
                    return

            # Forward to Flask
            flask_path = self.path
            if flask_path.startswith("/"):
                flask_path = flask_path[1:]
            target = f"{FLASK_URL}/{flask_path}" if flask_path else FLASK_URL

            req = urllib.request.Request(target, method=method)
            for header, value in self.headers.items():
                if header.lower() not in ("host", "connection"):
                    req.add_header(header, value)
            if method == "POST":
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                req.data = post_data

            with urllib.request.urlopen(req, timeout=2, context=SSL_CONTEXT) as response:
                self.send_response(response.status)
                for header, value in response.headers.items():
                    self.send_header(header, value)
                self.end_headers()
                # Read and write in chunks to avoid large memory usage
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    self.safe_write(chunk)

        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError,
                ConnectionResetError, BrokenPipeError, OSError,
                http.client.RemoteDisconnected, socket.error) as e:
            print(f"⚠️ Flask unavailable ({type(e).__name__}: {e}) – serving fallback")
            if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
                if os.path.exists(path.lstrip('/')):
                    self.serve_static_file(path.lstrip('/'))
                    return
            self.serve_404()

        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            traceback.print_exc()
            self.serve_404()

    def serve_static_file(self, filepath):
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            ext = os.path.splitext(filepath)[1].lower()
            content_type = {
                '.mp3': 'audio/mpeg',
                '.css': 'text/css',
                '.js': 'application/javascript',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.ico': 'image/x-icon',
                '.svg': 'image/svg+xml',
            }.get(ext, 'application/octet-stream')
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.safe_write(content)
            print(f"✅ Served static: {filepath}")
        except Exception as e:
            print(f"⚠️ Static file error: {e}")
            self.serve_404()

    def serve_404(self):
        try:
            self.send_response(404)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            try:
                with open(STATIC_404, 'rb') as f:
                    self.safe_write(f.read())
                print("📄 Served 404.html")
            except FileNotFoundError:
                self.safe_write(b"<h1>404 - Page Not Found</h1>")
                print("⚠️ 404.html not found!")
        except Exception as e:
            # If we can't even send headers, just log
            print(f"⚠️ Failed to send 404 response: {e}")

if __name__ == "__main__":
    with socketserver.TCPServer(("127.0.0.1", PORT), ProxyHandler) as httpd:
        print(f"🔄 Proxy running on http://127.0.0.1:{PORT}")
        print(f"   Forwarding to Flask on {FLASK_URL}")
        print("   Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 Proxy stopped.")