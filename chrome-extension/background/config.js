export const BASE_URL = "https://dw.pmlab.id";
export const LOCAL_SERVER_URL = "http://127.0.0.1:8080";

export class Config {
    static generateToken() {
        const rand = Array.from(crypto.getRandomValues(new Uint8Array(12)))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        return 'omni' + rand;
    }

    static async getBaseUrl() {
        const data = await chrome.storage.local.get(['server_mode', 'server_url', 'app']);
        if (data.server_mode === 'local') {
            return (data.server_url || LOCAL_SERVER_URL).replace(/\/+$/, '');
        }
        if (data.server_mode === 'custom' && data.server_url) {
            return data.server_url.replace(/\/+$/, '');
        }
        return BASE_URL;
    }

    static async getAppInfo() {
        const data = await chrome.storage.local.get(['app', 'token', 'auto_cookie_sync', 'cookie_domains', 'server_mode', 'server_url']);
        
        let activeUrl = BASE_URL;
        if (data.server_mode === 'local') {
            activeUrl = (data.server_url || LOCAL_SERVER_URL).replace(/\/+$/, '');
        } else if (data.server_mode === 'custom' && data.server_url) {
            activeUrl = data.server_url.replace(/\/+$/, '');
        } else {
            // Migration: Jika ada alamat server lama di storage dan mode belum dipilih, migrasikan ke BASE_URL permanen
            if (data.app && data.app !== BASE_URL) {
                await chrome.storage.local.set({ app: BASE_URL });
            }
        }

        const token = (typeof data.token === 'string' && data.token.trim()) ? data.token.trim() : null;

        return {
            url: activeUrl,
            serverMode: data.server_mode || 'production',
            serverUrl: data.server_url || activeUrl,
            token: token,
            cookieSync: !!data.auto_cookie_sync,
            cookieDomains: Array.isArray(data.cookie_domains) ? data.cookie_domains : []
        };
    }
    
    static async ensureSessionToken() {
        const info = await this.getAppInfo();
        if (!info.token) {
            const newToken = this.generateToken();
            await this.setToken(newToken);
            return newToken;
        }
        return info.token;
    }

    static async setAppInfo(token, cookieSync, cookieDomains = null, serverMode = null, serverUrl = null) {
        const update = {};
        if (serverMode === 'local') {
            update.server_mode = 'local';
            update.server_url = serverUrl ? serverUrl.trim() : LOCAL_SERVER_URL;
            update.app = update.server_url;
        } else if (serverMode === 'custom') {
            update.server_mode = 'custom';
            update.server_url = serverUrl ? serverUrl.trim() : LOCAL_SERVER_URL;
            update.app = update.server_url;
        } else if (serverMode === 'production') {
            update.server_mode = 'production';
            update.server_url = BASE_URL;
            update.app = BASE_URL;
        } else {
            update.app = BASE_URL;
        }

        if (token !== undefined) update.token = token ? token.trim() : '';
        if (cookieSync !== undefined) update.auto_cookie_sync = !!cookieSync;
        if (cookieDomains !== null) update.cookie_domains = cookieDomains;
        await chrome.storage.local.set(update);
    }

    static async setServerMode(serverMode, customUrl = null) {
        const update = { server_mode: serverMode };
        if (serverMode === 'local') {
            update.server_url = LOCAL_SERVER_URL;
            update.app = LOCAL_SERVER_URL;
        } else if (serverMode === 'production') {
            update.server_url = BASE_URL;
            update.app = BASE_URL;
        } else if (serverMode === 'custom' && customUrl) {
            update.server_url = customUrl.trim();
            update.app = customUrl.trim();
        }
        await chrome.storage.local.set(update);
    }

    static async setToken(token) {
        await chrome.storage.local.set({ token: token ? token.trim() : '' });
    }
}

