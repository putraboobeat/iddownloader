import { Config, BASE_URL, LOCAL_SERVER_URL } from './background/config.js';

const tokenInput = document.getElementById('token');
const syncInput = document.getElementById('sync');
const domainBox = document.getElementById('domain-selector-box');
const status = document.getElementById('status');
const btnGenToken = document.getElementById('btn-generate-token');

const modeLocal = document.getElementById('mode-local');
const modeProd = document.getElementById('mode-prod');
const modeCustom = document.getElementById('mode-custom');
const customBox = document.getElementById('custom-server-box');
const customUrlInput = document.getElementById('custom-server-url');

function showStatus(type, msg) {
    status.className = type;
    status.textContent = msg;
    status.style.display = 'block';
}

function updateServerBox() {
    if (customBox) {
        customBox.style.display = modeCustom && modeCustom.checked ? 'block' : 'none';
    }
}

if (modeLocal) modeLocal.onchange = updateServerBox;
if (modeProd) modeProd.onchange = updateServerBox;
if (modeCustom) modeCustom.onchange = updateServerBox;

(async () => {
    const s = await Config.getAppInfo();
    if (tokenInput) tokenInput.value = s.token || '';
    if (syncInput) {
        syncInput.checked = !!s.cookieSync;
        if (domainBox) domainBox.style.display = s.cookieSync ? 'block' : 'none';
    }

    if (s.serverMode === 'local' && modeLocal) {
        modeLocal.checked = true;
    } else if (s.serverMode === 'custom' && modeCustom) {
        modeCustom.checked = true;
        if (customUrlInput) customUrlInput.value = s.serverUrl || '';
    } else if (modeProd) {
        modeProd.checked = true;
    }
    updateServerBox();

    const savedDomains = s.cookieDomains || [];
    document.querySelectorAll('.site-cb').forEach(cb => {
        if (savedDomains.includes(cb.value)) {
            cb.checked = true;
        }
    });
})();

if (syncInput) {
    syncInput.onchange = () => {
        if (domainBox) domainBox.style.display = syncInput.checked ? 'block' : 'none';
    };
}

if (btnGenToken) {
    btnGenToken.onclick = () => {
        const randStr = Array.from(crypto.getRandomValues(new Uint8Array(12)))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        tokenInput.value = 'omni' + randStr;
        showStatus('info', 'Token baru dibuat. Klik "Simpan Pengaturan" untuk mengaktifkannya.');
    };
}

document.getElementById('save').onclick = async () => {
    try {
        const tokenVal = tokenInput ? tokenInput.value.trim() : '';
        const syncVal = syncInput ? syncInput.checked : false;

        if (tokenVal && !/^[a-zA-Z0-9]+$/.test(tokenVal)) {
            throw new Error('Token sesi harus berupa kombinasi huruf dan angka (alphanumeric) tanpa spasi atau simbol.');
        }

        let chosenMode = 'production';
        let customUrl = BASE_URL;

        if (modeLocal && modeLocal.checked) {
            chosenMode = 'local';
            customUrl = LOCAL_SERVER_URL;
        } else if (modeCustom && modeCustom.checked) {
            chosenMode = 'custom';
            customUrl = customUrlInput ? customUrlInput.value.trim() : LOCAL_SERVER_URL;
            if (!customUrl.startsWith('http://') && !customUrl.startsWith('https://')) {
                throw new Error('URL server kustom harus diawali dengan http:// atau https://');
            }
        }

        const selectedDomains = [];
        if (syncVal) {
            const granted = await chrome.permissions.request({ permissions: ['cookies'] });
            if (!granted) throw new Error('Izin pembacaan cookie ditolak oleh browser.');

            document.querySelectorAll('.site-cb:checked').forEach(cb => {
                selectedDomains.push(cb.value);
            });
        }

        await Config.setAppInfo(tokenVal, syncVal, selectedDomains, chosenMode, customUrl);
        const activeUrl = await Config.getBaseUrl();
        showStatus('success', '✅ Pengaturan berhasil disimpan untuk server ' + activeUrl + '.');
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

    const activeUrl = await Config.getBaseUrl();
    showStatus('info', 'Menguji koneksi ke ' + activeUrl + '…');

    chrome.runtime.sendMessage({ type: 'CHECK_SESSION', token: tokenVal }, (res) => {
        if (chrome.runtime.lastError) {
            showStatus('error', '❌ Gagal berkomunikasi dengan background worker: ' + chrome.runtime.lastError.message);
            return;
        }

        if (res && res.ok) {
            showStatus('success', '✅ Koneksi dan sesi valid! Server ' + activeUrl + ' merespons dengan baik.');
        } else if (res && res.code === '401') {
            showStatus('error', '❌ Sesi belum terhubung atau kedaluwarsa (401 Unauthorized). Silakan periksa kembali token Anda.');
        } else {
            showStatus('error', '❌ ' + (res?.message || 'Gagal terhubung ke server ' + activeUrl + '. Pastikan server aktif.'));
        }
    });
};
