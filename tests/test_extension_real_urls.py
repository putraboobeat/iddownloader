import os
import sys
import time
import json
import shutil
import urllib.request
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent.resolve()
TEST_ENV_DIR = BASE_DIR / 'test_env'
VPS_DATA_DIR = TEST_ENV_DIR / 'vps_data'
PROFILE_DIR = TEST_ENV_DIR / 'chrome_profile'
EXTENSION_DIR = TEST_ENV_DIR / 'extension_test'

CHROME_BIN = str(Path.home() / 'Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')
PORT_BACKEND = 8089

def run_tests():
    print("=" * 70)
    print("PENGUJIAN UNDUH REAL URLS MENGGUNAKAN EKSTENSI OMNIFETCH COMPANION")
    print("=" * 70)

    # 1. Jalankan Backend Lokal
    print(f"\n[1] Menjalankan OmniFetch Backend pada port {PORT_BACKEND} (VPS_ROOT={VPS_DATA_DIR})...")
    VPS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    backend_env = os.environ.copy()
    backend_env['VPS_ROOT'] = str(VPS_DATA_DIR)

    backend_proc = subprocess.Popen(
        ['.venv/bin/python', 'app.py', '--port', str(PORT_BACKEND), '--vps', '--download-dir', str(VPS_DATA_DIR)],
        cwd=str(BASE_DIR),
        env=backend_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    time.sleep(1.5)
    if backend_proc.poll() is not None:
        stdout, stderr = backend_proc.communicate()
        raise RuntimeError(f"Gagal memulai backend:\n{stdout}\n{stderr}")
    print(f"  -> Backend aktif di http://127.0.0.1:{PORT_BACKEND}/")

    session_token = "omnitestreal01"

    try:
        with sync_playwright() as p:
            print(f"\n[2] Membuka Google Chrome dengan Ekstensi OmniFetch Companion v2.1.2...")
            context = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                executable_path=CHROME_BIN,
                ignore_default_args=['--disable-extensions', '--disable-component-extensions-with-background-pages'],
                args=[
                    f'--disable-extensions-except={str(EXTENSION_DIR)}',
                    f'--load-extension={str(EXTENSION_DIR)}',
                    '--no-sandbox',
                    '--disable-gpu'
                ]
            )
            time.sleep(1.5)
            sw = context.service_workers[0] if context.service_workers else None
            assert sw is not None, "Service worker ekstensi tidak ditemukan!"
            ext_id = sw.url.split('/')[2]
            print(f"  -> Ekstensi termuat dengan ID: {ext_id}")

            # Konfigurasi token sesi ekstensi
            popup_url = f"chrome-extension://{ext_id}/popup.html"
            page_popup = context.new_page()
            page_popup.goto(popup_url)
            page_popup.wait_for_selector("#btn-download-main")
            page_popup.evaluate(f'''async () => {{
                const {{ Config }} = await import('./background/config.js');
                await Config.setToken("{session_token}");
            }}''')

            # =========================================================================
            # UJI 1: YOUTUBE
            # =========================================================================
            yt_url = "https://www.youtube.com/watch?v=t21iunZ5R-4"
            print(f"\n[UJI 1] Menguji Unduhan YouTube melalui Ekstensi:")
            print(f"  Target: {yt_url}")
            
            # Buka tab YouTube di browser
            tab_yt = context.new_page()
            try:
                tab_yt.goto(yt_url, timeout=25000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  Warning loading YouTube page: {e}")
            time.sleep(2)
            yt_title = tab_yt.title()
            print(f"  -> Tab YouTube terbuka: '{yt_title}'")

            # Kirim pekerjaan unduh melalui alur ekstensi (SEND_JOB)
            print("  -> Mengirim pekerjaan unduh ke background ekstensi (SEND_JOB)...")
            yt_res = page_popup.evaluate(f'''async () => {{
                return new Promise(resolve => {{
                    chrome.runtime.sendMessage({{
                        type: 'SEND_JOB',
                        payload: {{
                            urls: "{yt_url}",
                            name: "YouTube_Lapas_Merauke",
                            media_type: "video",
                            resolution: "best",
                            video_format: "mp4"
                        }},
                        title: "YouTube: Lapas Merauke"
                    }}, res => resolve(res));
                }});
            }}''')
            print(f"  -> Respons SEND_JOB ekstensi: {yt_res}")

            job_id_yt = yt_res.get('job_id') or yt_res.get('data', {}).get('job_id')
            if job_id_yt:
                print(f"  -> Job ID diterima: {job_id_yt}. Memantau progres backend...")
                
                # Polling progres pekerjaan
                max_wait = 60
                final_status = None
                files_list = []
                for _ in range(max_wait):
                    time.sleep(2)
                    req = urllib.request.Request(f"http://127.0.0.1:{PORT_BACKEND}/api/status?job_id={job_id_yt}")
                    req.add_header("X-App-Session", session_token)
                    try:
                        with urllib.request.urlopen(req) as resp:
                            s = json.loads(resp.read().decode())
                            final_status = s.get('status')
                            percent = s.get('percent', 0)
                            print(f"     [YouTube] Status={final_status}, Progres={percent}%")
                            if final_status in ('completed', 'failed', 'cancelled'):
                                files_list = s.get('files', [])
                                break
                    except Exception as err:
                        print(f"     [YouTube] Polling error: {err}")

                print(f"  -> Status akhir backend: {final_status}")
                print(f"  -> Berkas hasil: {[f.get('name') for f in files_list]}")

                # Picu penyimpanan file oleh ekstensi
                if final_status == 'completed' and files_list:
                    print("  -> Memicu penyimpanan file via Chrome Downloads API...")
                    save_res = page_popup.evaluate(f'''async () => {{
                        const {{ JobManager }} = await import('./background/jobs.js');
                        await JobManager.saveFilesToDisk("{job_id_yt}");
                        await new Promise(r => setTimeout(r, 2000));
                        return new Promise(resolve => {{
                            chrome.downloads.search({{}}, items => resolve(items));
                        }});
                    }}''')
                    print(f"  -> Unduhan Chrome tercatat: {len(save_res)} berkas")
                    for it in save_res:
                        print(f"     * ID={it.get('id')}, State={it.get('state')}, File={it.get('filename')}")

                # Ambil tangkapan layar popup
                page_popup.reload()
                time.sleep(1.5)
                page_popup.screenshot(path=str(TEST_ENV_DIR / "screenshot_yt_extension.png"))
                print(f"  -> Screenshot tersimpan: test_env/screenshot_yt_extension.png")

            # =========================================================================
            # UJI 2: INSTAGRAM
            # =========================================================================
            ig_url = "https://www.instagram.com/p/DeQq_-Wy9mn/"
            print(f"\n[UJI 2] Menguji Unduhan Instagram melalui Ekstensi:")
            print(f"  Target: {ig_url}")

            tab_ig = context.new_page()
            try:
                tab_ig.goto(ig_url, timeout=25000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  Warning loading Instagram page: {e}")
            time.sleep(2)
            ig_title = tab_ig.title()
            print(f"  -> Tab Instagram terbuka: '{ig_title}'")

            print("  -> Mengirim pekerjaan unduh ke background ekstensi (SEND_JOB)...")
            ig_res = page_popup.evaluate(f'''async () => {{
                return new Promise(resolve => {{
                    chrome.runtime.sendMessage({{
                        type: 'SEND_JOB',
                        payload: {{
                            urls: "{ig_url}",
                            name: "Instagram_Reels_Aceh",
                            media_type: "video",
                            resolution: "best",
                            video_format: "mp4"
                        }},
                        title: "Instagram: Reels Aceh"
                    }}, res => resolve(res));
                }});
            }}''')
            print(f"  -> Respons SEND_JOB ekstensi: {ig_res}")

            job_id_ig = ig_res.get('job_id') or ig_res.get('data', {}).get('job_id')
            if job_id_ig:
                print(f"  -> Job ID diterima: {job_id_ig}. Memantau progres backend...")
                
                max_wait = 60
                final_status = None
                files_list = []
                for _ in range(max_wait):
                    time.sleep(2)
                    req = urllib.request.Request(f"http://127.0.0.1:{PORT_BACKEND}/api/status?job_id={job_id_ig}")
                    req.add_header("X-App-Session", session_token)
                    try:
                        with urllib.request.urlopen(req) as resp:
                            s = json.loads(resp.read().decode())
                            final_status = s.get('status')
                            percent = s.get('percent', 0)
                            print(f"     [Instagram] Status={final_status}, Progres={percent}%")
                            if final_status in ('completed', 'failed', 'cancelled'):
                                files_list = s.get('files', [])
                                break
                    except Exception as err:
                        print(f"     [Instagram] Polling error: {err}")

                print(f"  -> Status akhir backend: {final_status}")
                print(f"  -> Berkas hasil: {[f.get('name') for f in files_list]}")

                if final_status == 'completed' and files_list:
                    print("  -> Memicu penyimpanan file via Chrome Downloads API...")
                    save_res = page_popup.evaluate(f'''async () => {{
                        const {{ JobManager }} = await import('./background/jobs.js');
                        await JobManager.saveFilesToDisk("{job_id_ig}");
                        await new Promise(r => setTimeout(r, 2000));
                        return new Promise(resolve => {{
                            chrome.downloads.search({{}}, items => resolve(items));
                        }});
                    }}''')
                    print(f"  -> Unduhan Chrome tercatat: {len(save_res)} berkas")
                    for it in save_res:
                        print(f"     * ID={it.get('id')}, State={it.get('state')}, File={it.get('filename')}")

                page_popup.reload()
                time.sleep(1.5)
                page_popup.screenshot(path=str(TEST_ENV_DIR / "screenshot_ig_extension.png"))
                print(f"  -> Screenshot tersimpan: test_env/screenshot_ig_extension.png")

            # =========================================================================
            # UJI 3: IDLIX
            # =========================================================================
            idlix_url = "https://z2.idlixku.com/series/take-charge-of-my-heart-2026"
            print(f"\n[UJI 3] Menguji Deteksi IDLIX melalui Ekstensi:")
            print(f"  Target: {idlix_url}")

            tab_idlix = context.new_page()
            try:
                resp = tab_idlix.goto(idlix_url, timeout=20000)
                idlix_status = resp.status if resp else 'Unknown'
            except Exception as e:
                idlix_status = f"Gagal: {e}"
            time.sleep(3)
            idlix_title = tab_idlix.title()
            print(f"  -> Respons tab IDLIX: Status={idlix_status}, Judul='{idlix_title}'")

            # Periksa apa yang ditangkap oleh detector ekstensi
            detected = page_popup.evaluate(f'''async () => {{
                return new Promise(resolve => {{
                    chrome.tabs.query({{}}, tabs => {{
                        const idlixTab = tabs.find(t => t.url && t.url.includes('idlixku.com'));
                        if (!idlixTab) return resolve({{ candidates: [], error: 'Tab tidak ditemukan' }});
                        chrome.runtime.sendMessage({{ type: 'GET_CANDIDATES', tabId: idlixTab.id }}, res => resolve(res));
                    }});
                }});
            }}''')
            print(f"  -> Kandidat media yang terdeteksi oleh sniffer ekstensi di halaman katalog: {detected}")

            # Ambil screenshot tab IDLIX dan popup
            tab_idlix.screenshot(path=str(TEST_ENV_DIR / "screenshot_idlix_tab.png"))
            page_popup.reload()
            time.sleep(1.5)
            page_popup.screenshot(path=str(TEST_ENV_DIR / "screenshot_idlix_extension.png"))
            print(f"  -> Screenshot tersimpan: test_env/screenshot_idlix_extension.png")

            context.close()

    finally:
        if backend_proc.poll() is None:
            backend_proc.terminate()
            backend_proc.wait()
        print("\n[Selesai] Backend lokal telah dimatikan.")

if __name__ == '__main__':
    run_tests()
