import { Config, BASE_URL } from './background/config.js';

const tokenInput = document.getElementById('token');
const syncInput = document.getElementById('sync');
const status = document.getElementById('status');
const btnGenToken = document.getElementById('btn-generate-token');

function showStatus(type, msg) {
    status.className = type;
    status.innerHTML = msg;
    status.style.display = 'block';
}

(async () => {
    const s = await Config.getAppInfo();
    if (tokenInput) tokenInput.value = s.token || '';
    if (syncInput) syncInput.checked = !!s.cookieSync;
})();

if (btnGenToken) {
    btnGenToken.onclick = () => {
        // Hasilkan token sesi alphanumeric acak yang aman
        const randStr = Array.from(crypto.getRandomValues(new Uint8Array(12)))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        tokenInput.value = 'omni' + randStr;
        showStatus('info', 'Token baru dibuat. Klik <strong>Simpan Pengaturan</strong> untuk mengaktifkannya.');
    };
}

document.getElementById('save').onclick = async () => {
    try {
        const tokenVal = tokenInput ? tokenInput.value.trim() : '';
        const syncVal = syncInput ? syncInput.checked : false;

        if (tokenVal && !/^[a-zA-Z0-9]+$/.test(tokenVal)) {
            throw new Error('Token sesi harus berupa kombinasi huruf dan angka (alphanumeric) tanpa spasi atau simbol.');
        }

        if (syncVal) {
            const granted = await chrome.permissions.request({ permissions: ['cookies'] });
            if (!granted) throw new Error('Izin pembacaan cookie ditolak oleh browser.');
        }

        await Config.setAppInfo(tokenVal, syncVal);
        showStatus('success', '✅ Pengaturan berhasil disimpan untuk <strong>' + BASE_URL + '</strong>.');
    } catch (e) {
        showStatus('error', '❌ ' + e.message);
    }
};

document.getElementById('test').onclick = async () => {
    const tokenVal = tokenInput ? tokenInput.value.trim() : '';
    if (!tokenVal) {
        showStatus('error', '❌ Masukkan atau buat token sesi terlebih dahulu sebelum menguji.');
        return;
    }

    if (!/^[a-zA-Z0-9]+$/.test(tokenVal)) {
        showStatus('error', '❌ Token sesi tidak valid: harus huruf dan angka (alphanumeric).');
        return;
    }

    showStatus('info', 'Menguji koneksi ke ' + BASE_URL + '…');

    chrome.runtime.sendMessage({ type: 'CHECK_SESSION', token: tokenVal }, (res) => {
        if (chrome.runtime.lastError) {
            showStatus('error', '❌ Gagal berkomunikasi dengan background worker: ' + chrome.runtime.lastError.message);
            return;
        }

        if (res && res.ok) {
            showStatus('success', '✅ Koneksi dan sesi valid! Server <strong>' + BASE_URL + '</strong> merespons dengan baik.');
        } else if (res && res.code === '401') {
            showStatus('error', '❌ Sesi belum terhubung atau kedaluwarsa (401 Unauthorized). Silakan periksa kembali token Anda.');
        } else {
            showStatus('error', '❌ ' + (res?.message || 'Gagal terhubung ke server dw.pmlab.id.'));
        }
    });
};
