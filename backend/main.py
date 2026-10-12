import sys
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

# Add the parent directory to sys.path so 'backend' can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import config
from backend.core import library_db
from backend.admin.cli import create_admin_interactive, create_admin_programmatic

def run_server():
    parser = argparse.ArgumentParser(description="OmniFetch Personal Cinema & Downloader Engine")
    parser.add_argument('--create-admin', action='store_true', help='Jalankan inisialisasi interaktif akun Super Admin')
    parser.add_argument('--admin-user', default='', help='Username admin (non-interaktif)')
    parser.add_argument('--admin-password', default='', help='Kata sandi admin (non-interaktif)')
    parser.add_argument('--admin-name', default='', help='Nama lengkap admin (non-interaktif)')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--vps', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--download-dir', default='')
    parser.add_argument('--library-dir', default='')
    parser.add_argument('--cookies-path', default='')
    args, unknown = parser.parse_known_args()

    # 1. Konfigurasi Port & Jalur SEBELUM worker/singleton dimuat
    if args.port:
        config.PORT = args.port
    elif args.vps:
        config.PORT = 6666

    if args.cookies_path:
        config.COOKIES_PATH = args.cookies_path
    if args.vps and args.download_dir:
        config.VPS_ROOT = Path(args.download_dir).expanduser().resolve()
    if args.library_dir:
        config.LIBRARY_ROOT = Path(args.library_dir).expanduser().resolve()
        config.LIBRARY_MOVIES_DIR = config.LIBRARY_ROOT / 'Movies'
        config.JELLYFIN_MEDIA_ROOT = str(config.LIBRARY_ROOT)

    # Inisialisasi Database SQLite & Migrasi Schema
    library_db.init_db()

    # Penanganan CLI Bootstrap Super Admin
    if args.create_admin:
        create_admin_interactive()
        return

    if args.admin_user and args.admin_password:
        user = create_admin_programmatic(args.admin_user, args.admin_password, args.admin_name)
        print(f"Super Admin '{user['username']}' berhasil dikonfigurasi.")
        if not args.port and not args.vps:
            return

    # Impor APIHandler setelah konfigurasi selesai dimuat
    from backend.api.router import APIHandler

    server_address = ('', config.PORT)
    httpd = ThreadingHTTPServer(server_address, APIHandler)
    
    port_str = config.PORT or 8080
    print("=" * 60)
    print(f"🎬 OmniFetch Cinema & Downloader Studio siap!")
    print(f"   • Perpustakaan Film  : http://localhost:{port_str}/")
    print(f"   • Downloader Studio  : http://localhost:{port_str}/downloader")
    print(f"   • Panel Super Admin  : http://localhost:{port_str}/admin")
    print("=" * 60)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nMematikan server...")
    finally:
        httpd.server_close()

if __name__ == '__main__':
    run_server()
