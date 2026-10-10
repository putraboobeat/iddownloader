import sys
import threading
from http.server import ThreadingHTTPServer

from backend.api.router import APIHandler
from backend.core.config import PORT

def run_server():
    server_address = ('', PORT)
    httpd = ThreadingHTTPServer(server_address, APIHandler)
    print(f"OmniFetch Backend berjalan di port {PORT}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nMematikan server...")
    finally:
        httpd.server_close()

if __name__ == '__main__':
    run_server()
