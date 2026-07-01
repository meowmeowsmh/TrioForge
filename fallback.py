#!/usr/bin/env python3
"""
Reverse proxy with retry logic – forwards to Flask on port 5001 (HTTPS).
Serves static files and 404.html when Flask is down.
"""

import http.server
import socketserver
import requests
import os
import socket
import ssl
import time

PORT = 5000
FLASK_PORT = 5001
FLASK_URL = f"https://127.0.0.1:{FLASK_PORT}"
STATIC_404 = "404.html"
STATIC_EXTENSIONS = {'.mp3', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg'}

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
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, socket.error) as e:
            print(f"⚠️ Client disconnected while writing: {e}")

    def handle_request(self, method):
        try:
            path = self.path.split('?')[0]

            # Serve static files directly if they exist
            if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
                if os.path.exists(path.lstrip('/')):
                    self.serve_static_file(path.lstrip('/'))
                    return

            # Build target for Flask
            flask_path = self.path
            if flask_path.startswith("/"):
                flask_path = flask_path[1:]
            target = f"{FLASK_URL}/{flask_path}" if flask_path else FLASK_URL

            headers = {}
            for header, value in self.headers.items():
                if header.lower() not in ("host", "connection"):
                    headers[header] = value

            data = None
            if method == "POST":
                content_length = int(self.headers.get('Content-Length', 0))
                data = self.rfile.read(content_length)

            # Retry up to 3 times with 30‑second timeout
            for attempt in range(3):
                try:
                    response = requests.request(
                        method=method,
                        url=target,
                        headers=headers,
                        data=data,
                        timeout=30,
                        verify=False
                    )
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.safe_write(response.content)
                    return  # success
                except (requests.ConnectionError, requests.Timeout) as e:
                    print(f"⚠️ Attempt {attempt+1} failed: {e}")
                    if attempt < 2:
                        time.sleep(1)
                    else:
                        raise  # all retries failed

        except Exception as e:
            print(f"⚠️ Flask unavailable – serving fallback: {e}")
            # For API endpoints, return JSON error
            if self.path.startswith('/providers/') or self.path.startswith('/chat') or self.path.startswith('/conversations'):
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.safe_write(b'{"error": "Backend temporarily unavailable"}')
                return
            if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
                if os.path.exists(path.lstrip('/')):
                    self.serve_static_file(path.lstrip('/'))
                    return
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