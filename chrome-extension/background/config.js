export class Config {
    static async getAppInfo() {
        const data = await chrome.storage.local.get(['app', 'token', 'auto_cookie_sync']);
        return {
            url: data.app || null,
            token: data.token || null,
            cookieSync: !!data.auto_cookie_sync
        };
    }
    
    static async setAppInfo(url, token) {
        await chrome.storage.local.set({ app: url, token: token });
    }
}
