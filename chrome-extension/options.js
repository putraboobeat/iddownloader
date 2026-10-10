import { Config } from './background/config.js';

const app = document.getElementById('app');
const tokenInput = document.getElementById('token');
const syncInput = document.getElementById('sync');
const status = document.getElementById('status');

(async () => {
    const s = await Config.getAppInfo();
    app.value = s.url || '';
    if (tokenInput) tokenInput.value = s.token || '';
    if (syncInput) syncInput.checked = !!s.cookieSync;
})();

document.getElementById('save').onclick = async () => {
    try {
        const val = app.value.trim();
        if (!val) throw Error('Alamat aplikasi tidak boleh kosong.');
        const u = new URL(val.startsWith('http://') || val.startsWith('https://') ? val : 'http://' + val);
        
        if (!['http:', 'https:'].includes(u.protocol) || !u.hostname) {
            throw Error('Gunakan alamat valid.');
        }
        
        const syncVal = syncInput ? syncInput.checked : false;
        
        if (u.protocol === 'http:' && !['127.0.0.1', 'localhost'].includes(u.hostname) && syncVal) {
            throw Error('Sinkronisasi cookie hanya diizinkan untuk HTTPS atau localhost.');
        }
        
        const tokenVal = tokenInput ? tokenInput.value.trim() : '';
        
        if (syncVal) {
            const granted = await chrome.permissions.request({ permissions: ['cookies'] });
            if (!granted) throw Error('Izin cookie ditolak pengguna.');
        }
        
        await chrome.storage.local.set({ app: u.origin, token: tokenVal, auto_cookie_sync: syncVal });
        app.value = u.origin;
        status.textContent = '✅ Pengaturan tersimpan.';
    } catch (e) {
        status.textContent = '❌ ' + e.message;
    }
};
