import subprocess, sys, os, webbrowser, time, socket

FLASK_PORT = 5001
FLASK_SCRIPT = 'app.py'
STARTUP_TIMEOUT = 15

def is_port_open(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex((host, port))
    sock.close()
    return result == 0

def wait_for_port(host, port, timeout):
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
    # Start Flask only (no proxy)
    if not is_port_open('127.0.0.1', FLASK_PORT):
        start_service(FLASK_SCRIPT, FLASK_PORT, 'Flask app')
    webbrowser.open(f'https://127.0.0.1:{FLASK_PORT}')

if __name__ == "__main__":
    launch()