"""Operational Settings Management for OmniFetch Super Admin.
Safely bounds and persists operational configurations in SQLite.
"""
from typing import Dict, Any, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core import config
from ..core import library_db
from .audit import log_admin_action

SETTING_DEFS = {
    'retention_seconds': {
        'type': int,
        'min': 300,        # 5 minutes
        'max': 864000,     # 10 days
        'default': config.RETENTION_SECONDS,
        'label': 'Retensi File Sementara (detik)',
        'description': 'Masa simpan file unduhan sementara sebelum dibersihkan. Pekerjaan perpustakaan dan histori unduhan tidak ikut terhapus.',
        'immediate': True
    },
    'trash_retention_days': {
        'type': int,
        'min': 1,
        'max': 365,
        'default': config.TRASH_RETENTION_DAYS,
        'label': 'Retensi Karantina / Trash (hari)',
        'description': 'Lama penyimpanan file koleksi yang dihapus ke folder karantina sebelum dihapus permanen.',
        'immediate': True
    },
    'history_retention_days': {
        'type': int,
        'min': 7,
        'max': 3650,
        'default': config.HISTORY_RETENTION_DAYS,
        'label': 'Retensi Catatan Riwayat (hari)',
        'description': 'Batas penyimpanan data statistik dan riwayat unduhan pada database SQLite.',
        'immediate': True
    },
    'max_concurrent_jobs': {
        'type': int,
        'min': 1,
        'max': 16,
        'default': config.MAX_CONCURRENT_JOBS,
        'label': 'Batas Tugas Bersamaan (Concurrency)',
        'description': 'Jumlah maksimum worker unduhan aktif yang diizinkan memproses media secara simultan.',
        'immediate': True
    },
    'disk_warning_percent': {
        'type': int,
        'min': 50,
        'max': 98,
        'default': config.DISK_WARNING_PERCENT,
        'label': 'Ambang Batas Peringatan Disk (%)',
        'description': 'Persentase kapasitas penyimpanan terpakai yang memicu peringatan waspada pada dashboard.',
        'immediate': True
    },
    'analytics_timezone': {
        'type': str,
        'default': config.ANALYTICS_TIMEZONE,
        'label': 'Zona Waktu Statistik',
        'description': 'Zona waktu IANA untuk agregasi riwayat aktivitas harian (contoh: Asia/Jakarta, UTC, Asia/Makassar).',
        'immediate': True
    },
    'maintenance_mode': {
        'type': bool,
        'default': False,
        'label': 'Mode Pemeliharaan (Hentikan Pekerjaan Baru)',
        'description': 'Jika aktif, server menolak penambahan antrean unduhan baru dengan pesan maintenance yang jelas.',
        'immediate': True
    }
}

def get_effective_settings() -> Dict[str, Any]:
    """Retrieves all settings combining schema defaults with persisted DB overrides."""
    persisted = library_db.get_all_settings()
    result = {}
    for key, spec in SETTING_DEFS.items():
        if key in persisted:
            val = persisted[key]['value']
            updated_at = persisted[key]['updated_at']
            updated_by = persisted[key]['updated_by']
        else:
            val = spec['default']
            updated_at = None
            updated_by = 'system_default'
            
        result[key] = {
            'value': val,
            'label': spec['label'],
            'description': spec['description'],
            'type': spec['type'].__name__,
            'min': spec.get('min'),
            'max': spec.get('max'),
            'immediate': spec['immediate'],
            'updated_at': updated_at,
            'updated_by': updated_by
        }
    return result

def update_setting(key: str, raw_value: Any, username: str = "admin", ip_address: str = "") -> Tuple[bool, str, Any]:
    """Validates and persists an operational setting with bounds enforcement and audit logging."""
    if key not in SETTING_DEFS:
        return False, f"Pengaturan '{key}' tidak dikenal atau tidak diizinkan untuk diubah.", None

    spec = SETTING_DEFS[key]
    val_type = spec['type']

    try:
        if val_type == bool:
            if isinstance(raw_value, str):
                parsed_val = raw_value.lower() in ('true', '1', 'yes', 'on')
            else:
                parsed_val = bool(raw_value)
        elif val_type == int:
            parsed_val = int(raw_value)
            if 'min' in spec and parsed_val < spec['min']:
                return False, f"Nilai minimum untuk {spec['label']} adalah {spec['min']}.", None
            if 'max' in spec and parsed_val > spec['max']:
                return False, f"Nilai maksimum untuk {spec['label']} adalah {spec['max']}.", None
        elif val_type == str:
            parsed_val = str(raw_value).strip()
            if key == 'analytics_timezone':
                try:
                    ZoneInfo(parsed_val)
                except (ZoneInfoNotFoundError, Exception):
                    return False, f"Zona waktu '{parsed_val}' tidak valid (Gunakan zona waktu standar seperti Asia/Jakarta).", None
        else:
            parsed_val = raw_value
    except Exception as e:
        return False, f"Format nilai tidak valid: {e}", None

    old_val = library_db.get_setting(key, spec['default'])
    library_db.set_setting(key, parsed_val, updated_by=username)

    log_admin_action(
        username=username,
        action="settings.update",
        resource_type="setting",
        resource_id=key,
        details={'old_value': old_val, 'new_value': parsed_val},
        ip_address=ip_address,
        status="success"
    )

    return True, f"Pengaturan '{spec['label']}' berhasil diperbarui.", parsed_val
