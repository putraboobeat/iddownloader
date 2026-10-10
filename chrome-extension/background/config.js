export const BASE_URL = "https://dw.pmlab.id";

export class Config {
    static async getAppInfo() {
        const data = await chrome.storage.local.get(['app', 'token', 'auto_cookie_sync']);
        
        // Migration: Jika ada alamat server lama di storage, migrasikan ke BASE_URL permanen
        if (data.app && data.app !== BASE_URL) {
            await chrome.storage.local.set({ app: BASE_URL });
        }

        const token = (typeof data.token === 'string' && data.token.trim()) ? data.token.trim() : null;

        return {
            url: BASE_URL,
            token: token,
            cookieSync: !!data.auto_cookie_sync
        };
    }
    
    static async setAppInfo(token, cookieSync) {
        const update = { app: BASE_URL };
        if (token !== undefined) update.token = token ? token.trim() : '';
        if (cookieSync !== undefined) update.auto_cookie_sync = !!cookieSync;
        await chrome.storage.local.set(update);
    }

    static async setToken(token) {
        await chrome.storage.local.set({ token: token ? token.trim() : '' });
    }
}
