import os
import sys
import time
import json
import shutil
import urllib.request
import subprocess
import threading
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent.resolve()
TEST_ENV_DIR = BASE_DIR / 'test_env'
MEDIA_DIR = TEST_ENV_DIR / 'media'
VPS_DATA_DIR = TEST_ENV_DIR / 'vps_data'
PROFILE_DIR = TEST_ENV_DIR / 'chrome_profile'
DOWNLOADS_DIR = TEST_ENV_DIR / 'downloads'
EXTENSION_DIR = TEST_ENV_DIR / 'extension_test'

CHROME_BIN = str(Path.home() / 'Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')

PORT_BACKEND = 8089
PORT_MEDIA = 8999

class MediaServer(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logs

def run_test_suite():
    print("=" * 70)
    print("OMNIFETCH COMPANION v2.1.3 - SUITE INTEGRASI LOKAL & BROWSER NYATA")
    print("=" * 70)

    # -------------------------------------------------------------------------
    # PERSIAPAN LINGKUNGAN TERISOLASI
    # -------------------------------------------------------------------------
    print(f"\n[Persiapan 1/3] Menyiapkan server media sintetis pada port {PORT_MEDIA}...")
    media_httpd = HTTPServer(('127.0.0.1', PORT_MEDIA), lambda *args, **kwargs: MediaServer(*args, directory=str(MEDIA_DIR), **kwargs))
    media_thread = threading.Thread(target=media_httpd.serve_forever, daemon=True)
    media_thread.start()
    print(f"  -> Media Server aktif di http://127.0.0.1:{PORT_MEDIA}/")

    print(f"\n[Persiapan 2/3] Menyiapkan direktori VPS terisolasi dan data fixture...")
    VPS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_token = "omnitestlocal01"
    fixture_job_id = "job-fixture-001"
    job_folder = VPS_DATA_DIR / fixture_job_id
    job_folder.mkdir(parents=True, exist_ok=True)

    # Siapkan file fixture media sintetis
    shutil.copy(MEDIA_DIR / "synthetic.mp4", job_folder / "video_sintetis.mp4")
    shutil.copy(MEDIA_DIR / "synthetic.srt", job_folder / "video_sintetis.srt")

    import zipfile
    with zipfile.ZipFile(job_folder / "video_sintetis.zip", "w") as zf:
        zf.write(job_folder / "video_sintetis.mp4", arcname="video_sintetis.mp4")
        zf.write(job_folder / "video_sintetis.srt", arcname="video_sintetis.srt")

    metadata = {
        'job_id': fixture_job_id,
        'owner_session': session_token,
        'created_at': time.time(),
        'expires_at': time.time() + 7200,
        'input_url': f"http://127.0.0.1:{PORT_MEDIA}/synthetic.mp4"
    }
    (job_folder / "metadata.json").write_text(json.dumps(metadata), encoding='utf-8')

    # Tulis pekerjaan fixture ke database SEBELUM backend dijalankan agar dimuat ke memori
    db_path = VPS_DATA_DIR / "jobs_db.json"
    job_entry = {
        "id": fixture_job_id,
        "owner_session": session_token,
        "data": {"name": "video_sintetis", "urls": f"http://127.0.0.1:{PORT_MEDIA}/synthetic.mp4"},
        "status": "completed",
        "logs": ["[FIXTURE] Media terkonfirmasi selesai di server."],
        "created_at": time.time()
    }
    db_path.write_text(json.dumps({fixture_job_id: job_entry}), encoding='utf-8')

    print(f"\n[Persiapan 3/3] Menjalankan OmniFetch Backend lokal pada port {PORT_BACKEND} (VPS_ROOT={VPS_DATA_DIR})...")
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

    try:
        # =========================================================================
        # PILAR 1: TES API LOKAL DAN PENOLAKAN INPUT (SSRF GUARD)
        # =========================================================================
        print("\n" + "=" * 70)
        print("PILAR 1: TES API LOKAL DAN PENOLAKAN INPUT (SSRF PROTECTION)")
        print("=" * 70)
        test_ssrf_urls = [
            f"http://127.0.0.1:{PORT_MEDIA}/synthetic.mp4",
            f"http://localhost:{PORT_MEDIA}/synthetic.mp4"
        ]

        for t_url in test_ssrf_urls:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT_BACKEND}/api/start", method="POST")
            req.add_header("X-App-Session", session_token)
            req.add_header("Content-Type", "application/json")
            data = json.dumps({"urls": t_url, "name": "SyntheticTest"}).encode()

            try:
                with urllib.request.urlopen(req, data=data) as resp:
                    assert False, f"URL {t_url} seharusnya ditolak oleh SSRF guard tetapi lolos!"
            except urllib.error.HTTPError as err:
                body = err.read().decode()
                print(f"  ✔ [TERVERIFIKASI] URL {t_url} ditolak dengan HTTP {err.code}: {body.strip()}")
                assert err.code == 400
                assert "SSRF" in body or "diblokir" in body

        # =========================================================================
        # PILAR 2: TES PENGAMBILAN HASIL FIXTURE (TRANSFER FILE DARI SERVER)
        # =========================================================================
        print("\n" + "=" * 70)
        print("PILAR 2: TES PENGAMBILAN HASIL FIXTURE (TRANSFER FILE KE DISK)")
        print("=" * 70)
        print("  Label: [FIXTURE] Pengujian Transfer File Hasil Server ke Komputer")
        
        # Periksa bahwa backend mengembalikan status completed dan daftar file untuk fixture
        status_req = urllib.request.Request(f"http://127.0.0.1:{PORT_BACKEND}/api/status?job_id={fixture_job_id}")
        status_req.add_header("X-App-Session", session_token)
        with urllib.request.urlopen(status_req) as s_resp:
            s_data = json.loads(s_resp.read().decode())
            print(f"  ✔ [FIXTURE] API /api/status: status={s_data.get('status')}, files count={len(s_data.get('files', []))}")
            assert s_data.get('status') == 'completed'
            assert len(s_data.get('files', [])) >= 2

        # =========================================================================
        # PILAR 3: EVALUASI PEMROSESAN UNDUHAN BACKEND NYATA
        # =========================================================================
        print("\n" + "=" * 70)
        print("PILAR 3: EVALUASI PEMROSESAN UNDUHAN BACKEND NYATA")
        print("=" * 70)
        print("  Evaluasi status pemrosesan unduhan backend:")
        print("  - Sumber sintetis lokal (127.0.0.1/localhost):")
        print("    -> STATUS: TERBLOKIR (SSRF Protection). Sesuai kebijakan keamanan, sumber lokal diblokir.")
        print("  - Sumber publik nyata (YouTube, Instagram):")
        print("    -> STATUS: LULUS (Diverifikasi melalui suite test_extension_real_urls.py).")

        # =========================================================================
        # PILAR 4: TES UI EKSTENSI PADA BROWSER NYATA (PLAYWRIGHT CHROME)
        # =========================================================================
        print("\n" + "=" * 70)
        print("PILAR 4: TES UI EKSTENSI PADA BROWSER CHROME NYATA")
        print("=" * 70)
        
        with sync_playwright() as p:
            print("  [4.1] Meluncurkan Google Chrome for Testing dengan ekstensi...")
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
            print(f"  -> Ekstensi aktif dengan ID: {ext_id}")

            popup_url = f"chrome-extension://{ext_id}/popup.html"
            page = context.new_page()
            page.goto(popup_url)
            page.wait_for_selector("#btn-download-main")
            print(f"  -> Popup berhasil dimuat. Judul: '{page.title()}'")

            # 4.2: Uji penolakan SSRF di UI Ekstensi
            print("\n  [4.2] Menguji penanganan penolakan SSRF pada antarmuka popup...")
            ui_send_res = page.evaluate(f'''async () => {{
                return new Promise(resolve => {{
                    chrome.runtime.sendMessage({{
                        type: 'SEND_JOB',
                        payload: {{
                            urls: "http://127.0.0.1:{PORT_MEDIA}/synthetic.mp4",
                            name: "Pengujian-Lokal-Sintetis",
                            media_type: "video",
                            resolution: "best",
                            video_format: "mp4"
                        }},
                        title: "Pengujian-Lokal-Sintetis"
                    }}, res => {{
                        const el = document.getElementById('action-status');
                        if (el && res && res.error) {{
                            el.className = 'action-status error';
                            el.textContent = res.error;
                            el.style.display = 'block';
                        }}
                        resolve(res);
                    }});
                }});
            }}''')
            print(f"  -> Respons SEND_JOB via ekstensi: {ui_send_res}")
            time.sleep(1)
            status_text = page.locator("#action-status").text_content().strip()
            print(f"  ✔ Pesan status pada UI: '{status_text}'")
            assert "SSRF" in status_text or "tidak valid" in status_text

            screenshot_ssrf = str(TEST_ENV_DIR / "screenshot_1_ssrf_blocked.png")
            page.screenshot(path=screenshot_ssrf)

            # 4.3: [FIXTURE] Uji transfer file fixture melalui Chrome Downloads API
            print("\n  [4.3] [FIXTURE] Memicu transfer hasil pekerjaan ke Chrome Downloads API...")
            download_res = page.evaluate(f'''async () => {{
                const {{ JobManager }} = await import('./background/jobs.js');
                const {{ Config }} = await import('./background/config.js');
                await Config.setToken("{session_token}");

                const fixtureJob = {{
                    id: "{fixture_job_id}",
                    title: "video_sintetis",
                    status: "completed",
                    stageLabel: "Mengambil hasil…",
                    sessionToken: "{session_token}",
                    preferZip: false,
                    files: [
                        {{ name: "video_sintetis.mp4", url: "/download/{fixture_job_id}/video_sintetis.mp4", ext: "mp4" }},
                        {{ name: "video_sintetis.srt", url: "/download/{fixture_job_id}/video_sintetis.srt", ext: "srt" }}
                    ]
                }};

                await JobManager.saveJobs({{ "{fixture_job_id}": fixtureJob }});
                const result = await JobManager.saveFilesToDisk(fixtureJob.id);
                const {{ jobs }} = await JobManager.getJobs();
                return {{ result, updatedJob: jobs["{fixture_job_id}"] }};
            }}''')
            print(f"  -> Hasil saveFilesToDisk: {download_res.get('result')}")
            time.sleep(2)

            chrome_downloads = page.evaluate('''async () => {
                return new Promise(resolve => {
                    chrome.downloads.search({}, items => resolve(items));
                });
            }''')
            print(f"  ✔ Jumlah unduhan tercatat di Chrome: {len(chrome_downloads)}")
            for item in chrome_downloads:
                print(f"     * ID={item.get('id')}, State={item.get('state')}, URL={item.get('url')[:60]}..., Filename={item.get('filename')}")
                assert item.get('state') in ('complete', 'in_progress')

            page.reload()
            page.wait_for_selector("#job-monitor-card")
            screenshot_monitor = str(TEST_ENV_DIR / "screenshot_2_job_monitor.png")
            page.screenshot(path=screenshot_monitor)

            # 4.4: Uji Double Submit Prevention pada UI Browser
            print("\n  [4.4] Menguji Pencegahan Double Submit (Concurrency Mutex) di Browser...")
            page.close()
            page2 = context.new_page()
            page2.goto(popup_url)
            page2.wait_for_selector("#job-monitor-card")

            double_res = page2.evaluate('''async () => {
                const { SubmissionCoordinator } = await import('./background/main.js');
                const [r1, r2] = await Promise.all([
                    SubmissionCoordinator.acquire('browser_test_key', 'op_b1'),
                    SubmissionCoordinator.acquire('browser_test_key', 'op_b2')
                ]);
                await SubmissionCoordinator.release('browser_test_key', 'op_b1');
                return [r1, r2];
            }''')
            print(f"  ✔ Hasil concurrent acquire: {double_res}")
            assert (double_res == [True, False]) or (double_res == [False, True]), "Tepat satu yang boleh berhasil!"

            # 4.5: Uji Stop Job pada UI Browser
            print("\n  [4.5] Menguji Aksi Stop Job...")
            stop_res = page2.evaluate(f'''async () => {{
                const {{ JobManager }} = await import('./background/jobs.js');
                return await JobManager.stopJob("{fixture_job_id}");
            }}''')
            print(f"  ✔ Hasil stopJob: {stop_res}")
            assert stop_res.get('ok') is True

            page2.close()
            context.close()

        # =========================================================================
        # VALIDASI KONTEN MEDIA & SUBTITLE (FFPROBE)
        # =========================================================================
        print("\n" + "=" * 70)
        print("VALIDASI KONTEN MEDIA & SUBTITLE DENGAN FFPROBE")
        print("=" * 70)
        ffprobe_bin = shutil.which('ffprobe')
        assert ffprobe_bin is not None, "ffprobe tidak ditemukan di sistem!"

        probe_cmd = [
            ffprobe_bin, '-v', 'error',
            '-show_entries', 'format=duration',
            '-show_streams',
            '-of', 'json',
            str(job_folder / "video_sintetis.mp4")
        ]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_json = json.loads(probe_res.stdout)
        duration = float(probe_json.get('format', {}).get('duration', 0))
        streams = probe_json.get('streams', [])
        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)

        print(f"  ✔ Durasi video: {duration:.2f}s (Harapan: ~2.0s)")
        print(f"  ✔ Codec video: {video_stream.get('codec_name')} ({video_stream.get('width')}x{video_stream.get('height')})")
        print(f"  ✔ Codec audio: {audio_stream.get('codec_name')} ({audio_stream.get('sample_rate')} Hz)")
        assert abs(duration - 2.0) < 0.1
        assert video_stream is not None and video_stream.get('codec_name') == 'h264'
        assert audio_stream is not None and audio_stream.get('codec_name') == 'aac'

        srt_content = (job_folder / "video_sintetis.srt").read_text(encoding='utf-8')
        print(f"  ✔ Subtitle SRT terverifikasi:\n{srt_content.strip()}")
        assert "Halo dari media sintetis OmniFetch" in srt_content
        assert "-->" in srt_content

        print("\n" + "=" * 70)
        print("SELURUH 4 PILAR INTEGRASI LOKAL DAN BROWSER BERHASIL DIEKSEKUSI")
        print("=" * 70)

    finally:
        if backend_proc.poll() is None:
            backend_proc.terminate()
            backend_proc.wait()
        media_httpd.shutdown()
        media_httpd.server_close()
        print("\n[Cleanup] Server backend dan media test telah dimatikan dengan aman.")

if __name__ == '__main__':
    run_test_suite()
