import sys
import argparse
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

# Add the parent directory to sys.path so 'backend' can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.api.router import APIHandler
from backend.core import config

def run_server():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--vps', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--download-dir', default='')
    parser.add_argument('--cookies-path', default='')
    args, unknown = parser.parse_known_args()
    
    if args.port:
        config.PORT = args.port
    elif args.vps:
        config.PORT = 6666
        
    if args.cookies_path:
        config.COOKIES_PATH = args.cookies_path
    if args.vps and args.download_dir:
        config.VPS_ROOT = Path(args.download_dir).expanduser().resolve()
        
    server_address = ('', config.PORT)
    httpd = ThreadingHTTPServer(server_address, APIHandler)
    print(f"OmniFetch Backend berjalan di port {config.PORT}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nMematikan server...")
    finally:
        httpd.server_close()

if __name__ == '__main__':
    run_server()
