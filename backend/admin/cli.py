"""CLI Bootstrap Utility for OmniFetch Super Admin User Management.
Provides interactive and non-interactive administrative account creation without default passwords.
"""
import sys
import getpass
from typing import Optional

from ..core import library_db
from .auth import hash_password
from .audit import log_admin_action

def create_admin_interactive():
    """Runs interactive terminal prompts to create the initial or additional Super Admin user."""
    print("=" * 60)
    print("   OMNIFETCH — INISIALISASI AKUN SUPER ADMIN")
    print("=" * 60)
    print("Catatan: Tidak ada kata sandi bawaan. Harap buat akun administratif Anda.")
    print("")

    username = input("Username Super Admin: ").strip().lower()
    if not username or len(username) < 3:
        print("Kesalahan: Username minimal 3 karakter.")
        sys.exit(1)

    existing = library_db.get_admin_user_by_username(username)
    if existing:
        print(f"Peringatan: Pengguna '{username}' sudah terdaftar.")
        reset_choice = input("Apakah Anda ingin mengganti kata sandinya? (y/N): ").strip().lower()
        if reset_choice != 'y':
            print("Operasi dibatalkan.")
            sys.exit(0)
        password = getpass.getpass("Kata Sandi Baru (minimal 8 karakter): ")
        password_confirm = getpass.getpass("Ulangi Kata Sandi Baru: ")
        if password != password_confirm:
            print("Kesalahan: Konfirmasi kata sandi tidak cocok.")
            sys.exit(1)
        if len(password) < 8:
            print("Kesalahan: Kata sandi minimal 8 karakter.")
            sys.exit(1)

        pw_hash, salt = hash_password(password)
        library_db.update_admin_password(existing['id'], pw_hash, salt)
        log_admin_action(username=username, action="auth.password_reset_cli", resource_type="user", resource_id=str(existing['id']))
        print(f"\nBerhasil: Kata sandi untuk Super Admin '{username}' telah diperbarui!")
        return

    full_name = input("Nama Lengkap (opsional): ").strip()
    password = getpass.getpass("Kata Sandi (minimal 8 karakter): ")
    password_confirm = getpass.getpass("Ulangi Kata Sandi: ")

    if password != password_confirm:
        print("Kesalahan: Konfirmasi kata sandi tidak cocok.")
        sys.exit(1)

    if len(password) < 8:
        print("Kesalahan: Kata sandi minimal 8 karakter.")
        sys.exit(1)

    pw_hash, salt = hash_password(password)
    user = library_db.create_admin_user(
        username=username,
        password_hash=pw_hash,
        salt=salt,
        full_name=full_name,
        is_superadmin=1
    )
    log_admin_action(username=username, action="auth.create_admin_cli", resource_type="user", resource_id=str(user['id']))
    print(f"\nSelamat! Super Admin '{username}' berhasil dibuat.")
    print("Silakan masuk ke panel admin di peramban pada route: http://localhost:8080/admin\n")

def create_admin_programmatic(username: str, password: str, full_name: str = "", is_superadmin: bool = True) -> dict:
    """Non-interactive admin bootstrap for automated scripts and testing."""
    clean_u = username.strip().lower()
    if not clean_u or len(clean_u) < 3:
        raise ValueError("Username minimal 3 karakter.")
    if not password or len(password) < 8:
        raise ValueError("Kata sandi minimal 8 karakter.")

    existing = library_db.get_admin_user_by_username(clean_u)
    pw_hash, salt = hash_password(password)

    if existing:
        library_db.update_admin_password(existing['id'], pw_hash, salt)
        return existing

    return library_db.create_admin_user(
        username=clean_u,
        password_hash=pw_hash,
        salt=salt,
        full_name=full_name,
        is_superadmin=1 if is_superadmin else 0
    )
