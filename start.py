import subprocess, sys, os, webbrowser, time, socket

PROXY_PORT = 5000
FLASK_PORT = 5001
PROXY_SCRIPT = 'fallback.py'
FLASK_SCRIPT = 'app.py'
STARTUP_TIMEOUT = 15  # max seconds to wait for a service to come up

def is_port_open(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0

def wait_for_port(host, port, timeout):
    """Poll until the port opens or we hit the timeout. Returns True if it came up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.5)
    return False

def start_service(script_name, port, label):
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    if not os.path.exists(script_path):
        print(f"⚠️  Can't start {label}: {script_path} not found")
        return

    print(f"🔄 Starting {label} on port {port}...")
    if sys.platform == 'win32':
        subprocess.Popen(['start', 'cmd', '/k', sys.executable, script_path], shell=True)
    else:
        subprocess.Popen([sys.executable, script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if wait_for_port('127.0.0.1', port, STARTUP_TIMEOUT):
        print(f"✅ {label} is up on port {port}")
    else:
        print(f"⚠️  {label} did not open port {port} within {STARTUP_TIMEOUT}s (it may still be starting)")

def launch():
    # Start the fallback proxy if not running
    if not is_port_open('127.0.0.1', PROXY_PORT):
        start_service(PROXY_SCRIPT, PROXY_PORT, 'fallback proxy')

    # Start Flask if not already running
    if not is_port_open('127.0.0.1', FLASK_PORT):
        start_service(FLASK_SCRIPT, FLASK_PORT, 'Flask app')

    # Open the proxy URL
    webbrowser.open(f'http://127.0.0.1:{PROXY_PORT}')

if __name__ == "__main__":
    launch()