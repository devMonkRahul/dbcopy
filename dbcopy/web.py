import socket
import uvicorn
import threading
import webbrowser
import time

from app import app

def find_free_port(start=8000):
    port = start

    while True:
        try:
            sock = socket.socket()
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            port += 1

def run_dashboard():
    port = find_free_port()

    def start():
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port
        )

    threading.Thread(target=start).start()

    time.sleep(2)

    webbrowser.open(
        f"http://localhost:{port}"
    )

    print(f"Dashboard running on port {port}")