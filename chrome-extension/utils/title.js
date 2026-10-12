/**
 * Utilitas pembersih judul film dan penamaan media cerdas
 * Mengonversi judul tab mentah dari platform streaming (Idlix, LK21, YouTube, dsb.)
 * menjadi nama file film yang rapi, manusiawi, dan bebas dari nama file teknis (config-*.json).
 */

export function cleanMovieTitle(raw) {
    if (!raw || typeof raw !== 'string') return '';
    let title = raw.trim();

    // 1. Bersihkan akhiran branding situs / domain streaming
    title = title.replace(/\s*[-–—|•]\s*(?:IDLIX(?:\s+Official)?|LK21|Layarkaca21|Rebahin|Dutafilm|Indoxx1|YouTube|Viu|Netflix|Disney\+?|Bstation|Vimeo|Otakudesu|Samehadaku|Anoboy).*$/i, '');

    // 2. Bersihkan penanda subtitle (Sub Indo, Subtitle Indonesia, Eng Sub, Hardsub)
    title = title.replace(/\s*[-–—|•]?\s*(?:Sub(?:title)?\s*Indo(?:nesia)?|Eng\s*Sub|Hardsub).*$/i, '');

    // 3. Bersihkan awalan streaming umum (Nonton Film, Nonton Series, Streaming Movie, dsb.)
    title = title.replace(/^(?:Nonton\s+(?:Film|Series|Serial|Movie|Anime|Drama|Streaming)?\s*|Streaming\s+(?:Film|Movie)?\s*)/i, '');

    // 4. Bersihkan sisa simbol pembuka/penutup
    title = title.replace(/^[-–—|:•\s]+|[-–—|:•\s]+$/g, '').trim();

    return title || raw.trim();
}

export function getCleanDisplayTitle(item, tabTitle) {
    const cleanedTabTitle = cleanMovieTitle(tabTitle);
    const genericTabTitles = ['new tab', 'chrome', 'extension', 'about:blank', 'media video', 'unduhan media'];
    const isTabTitleValid = cleanedTabTitle && !genericTabTitles.includes(cleanedTabTitle.toLowerCase()) && cleanedTabTitle.length > 2;

    const raw = (item?.displayTitle || '').trim();

    // Deteksi apakah displayTitle merupakan nama file teknis URL internal
    const isTechnicalName = !raw || 
        /^config[-_0-9a-z]+/i.test(raw) ||
        /\.(?:json|m3u8|mpd|ts|mp4|m4s)(\?.*)?$/i.test(raw) ||
        /^[0-9a-f]{16,}$/i.test(raw) ||
        ['get', 'stream', 'play', 'video', 'media', 'index', 'playlist', 'master', 'api', 'content', 'manifest'].includes(raw.toLowerCase()) ||
        raw.length < 3;

    // Prioritaskan judul film bersih dari tab browser
    if (isTabTitleValid) {
        return cleanedTabTitle;
    }

    if (isTechnicalName) {
        return cleanedTabTitle || 'Media Video';
    }

    return raw;
}
