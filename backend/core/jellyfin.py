"""Jellyfin API Client & Catalog Integration for OmniFetch.
Uses official Jellyfin REST API standards. Strictly protects API key from exposure.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional, Dict, Any, List
from pathlib import Path

from . import config

class JellyfinClient:
    def __init__(
        self,
        internal_url: Optional[str] = None,
        public_url: Optional[str] = None,
        api_key: Optional[str] = None,
        library_id: Optional[str] = None,
        media_root: Optional[str] = None
    ):
        self.internal_url = (internal_url or config.JELLYFIN_INTERNAL_URL).rstrip('/')
        self.public_url = (public_url or config.JELLYFIN_PUBLIC_URL).rstrip('/')
        self.api_key = (api_key or config.JELLYFIN_API_KEY).strip()
        self.library_id = (library_id or config.JELLYFIN_LIBRARY_ID).strip()
        self.media_root = media_root or config.JELLYFIN_MEDIA_ROOT

    def _headers(self, with_auth: bool = True) -> Dict[str, str]:
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'OmniFetch-MediaServer/2.0'
        }
        if with_auth and self.api_key:
            headers['X-Emby-Token'] = self.api_key
        return headers

    def check_health(self) -> Dict[str, Any]:
        """Performs a comprehensive health check:
        1. Checks network connectivity to Jellyfin via /System/Info/Public.
        2. Validates API key authentication via /System/Info.
        3. Verifies library existence if library_id is configured.
        """
        result = {
            'ok': False,
            'configured': bool(self.internal_url and self.api_key),
            'healthy': False,
            'status': 'offline',
            'server_name': None,
            'version': None,
            'authenticated': False,
            'library_configured': bool(self.library_id),
            'message': '',
            'error': None
        }

        # Step 1: Public endpoint check (no auth needed)
        try:
            req = urllib.request.Request(
                f"{self.internal_url}/System/Info/Public",
                headers=self._headers(with_auth=False),
                method='GET'
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                result['server_name'] = data.get('ServerName') or data.get('Name') or 'Jellyfin'
                result['version'] = data.get('Version')
                result['status'] = 'reachable'
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
            result['status'] = 'offline'
            result['message'] = f"Jellyfin tidak dapat dihubungi di {self.internal_url}: {e}"
            result['error'] = result['message']
            return result
        except Exception as e:
            result['status'] = 'error'
            result['message'] = f"Gagal membaca status Jellyfin: {e}"
            result['error'] = result['message']
            return result

        # Step 2: Auth check if API key is provided
        if not self.api_key:
            result['status'] = 'unconfigured_auth'
            result['message'] = "Jellyfin aktif, namun JELLYFIN_API_KEY belum dikonfigurasi."
            result['error'] = result['message']
            return result

        try:
            req = urllib.request.Request(
                f"{self.internal_url}/System/Info",
                headers=self._headers(with_auth=True),
                method='GET'
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    result['authenticated'] = True
                    result['ok'] = True
                    result['status'] = 'online'
                    result['healthy'] = True
                    result['message'] = "Jellyfin online dan terautentikasi."
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                result['status'] = 'unauthorized'
                result['message'] = "API Key Jellyfin ditolak atau tidak valid."
                result['error'] = f"401 Unauthorized: {result['message']}"
            else:
                result['status'] = 'http_error'
                result['message'] = f"HTTP Error dari Jellyfin: {e.code}"
                result['error'] = result['message']
            return result
        except Exception as e:
            result['status'] = 'error'
            result['message'] = f"Gagal memverifikasi API Key Jellyfin: {e}"
            result['error'] = result['message']
            return result

        # Step 3: Library verification if library_id is set
        if self.library_id:
            try:
                lib_req = urllib.request.Request(
                    f"{self.internal_url}/Items?Ids={self.library_id}",
                    headers=self._headers(with_auth=True),
                    method='GET'
                )
                with urllib.request.urlopen(lib_req, timeout=5) as lib_resp:
                    lib_data = json.loads(lib_resp.read().decode('utf-8'))
                    items = lib_data.get('Items', [])
                    if not items:
                        result['ok'] = False
                        result['status'] = 'library_not_found'
                        result['message'] = f"Library ID '{self.library_id}' tidak ditemukan di Jellyfin."
                        result['error'] = result['message']
            except Exception as e:
                # Non-fatal if general access works
                pass

        result['healthy'] = (result['status'] == 'online')
        if not result['ok'] and not result['error']:
            result['error'] = result['message']
        return result

    def trigger_scan(self, library_id: Optional[str] = None) -> bool:
        """Triggers library scanning via official Jellyfin API.
        Uses /Items/{Id}/Refresh for targeted scan if library_id provided,
        or /Library/Refresh for full library scan.
        """
        if not self.api_key:
            return False

        target_lib = library_id or self.library_id
        if target_lib:
            url = f"{self.internal_url}/Items/{target_lib}/Refresh"
        else:
            url = f"{self.internal_url}/Library/Refresh"

        try:
            req = urllib.request.Request(
                url,
                headers=self._headers(with_auth=True),
                data=b'',
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception:
            # Fallback to general library refresh if targeted failed
            if target_lib:
                try:
                    fallback_req = urllib.request.Request(
                        f"{self.internal_url}/Library/Refresh",
                        headers=self._headers(with_auth=True),
                        data=b'',
                        method='POST'
                    )
                    with urllib.request.urlopen(fallback_req, timeout=10) as resp:
                        return resp.status in (200, 204)
                except Exception:
                    pass
            return False

    def find_item_by_title_and_year(
        self,
        title: str,
        year: Optional[int] = None,
        expected_folder: Optional[str] = None,
        timeout: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Queries Jellyfin for a movie matching title and optional year or folder path."""
        if not self.api_key:
            return None

        clean_search = title.strip()
        params = {
            'Recursive': 'true',
            'IncludeItemTypes': 'Movie',
            'SearchTerm': clean_search,
            'Fields': 'Path,ProductionYear,MediaSources,Overview'
        }
        if self.library_id:
            params['ParentId'] = self.library_id

        query_url = f"{self.internal_url}/Items?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(
                query_url,
                headers=self._headers(with_auth=True),
                method='GET'
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                items = data.get('Items', [])
                if not items:
                    return None

                target_lower = title.strip().lower()

                # Strategy 1: Match by Path if expected_folder is known
                if expected_folder:
                    folder_name = Path(expected_folder).name.lower()
                    for item in items:
                        item_path = (item.get('Path') or '').lower()
                        if folder_name in item_path:
                            return item

                # Strategy 2: Match by exact title & year
                for item in items:
                    item_name = (item.get('Name') or '').strip().lower()
                    item_year = item.get('ProductionYear')
                    if item_name == target_lower:
                        if year is None or item_year == year:
                            return item

                # Strategy 3: Match by case-insensitive title prefix
                for item in items:
                    item_name = (item.get('Name') or '').strip().lower()
                    if target_lower in item_name:
                        return item

                return None
        except Exception:
            return None

    def poll_item_indexing(
        self,
        title: str,
        year: Optional[int] = None,
        expected_folder: Optional[str] = None,
        max_retries: int = 5,
        initial_delay: float = 2.0,
        backoff_factor: float = 1.5
    ) -> Optional[Dict[str, Any]]:
        """Polls Jellyfin until the newly added item is indexed and returned, with backoff."""
        delay = initial_delay
        for attempt in range(max_retries):
            time.sleep(delay)
            item = self.find_item_by_title_and_year(title, year=year, expected_folder=expected_folder, timeout=6)
            if item:
                return item
            delay = min(delay * backoff_factor, 10.0)
        return None

    def get_playback_web_url(self, item_id: Optional[str] = None) -> str:
        """Returns the user-facing playback/detail URL in Jellyfin web UI.
        Does NOT expose any admin API key or sensitive token.
        """
        base = self.public_url
        if item_id:
            return f"{base}/web/index.html#!/details?id={urllib.parse.quote(str(item_id))}"
        return f"{base}/web/index.html"

# Default shared client instance
jellyfin_client = JellyfinClient()
