/**
 * Utilitas Konversi Subtitle VTT ke SRT murni di sisi browser.
 * Menghasilkan format SubRip (.srt) UTF-8 yang valid.
 */
export function convertVttToSrt(vttText) {
    if (!vttText || typeof vttText !== 'string') return '';

    // Bersihkan BOM UTF-8 dan spasi awal
    let text = vttText.replace(/^\uFEFF/, '').trim();

    // Hapus header WEBVTT dan metadata header (misal NOTE, STYLE, REGION)
    text = text.replace(/^WEBVTT[^\n\r]*[\n\r]*/i, '');
    
    // Hapus blok NOTE atau STYLE
    text = text.replace(/(?:NOTE|STYLE|REGION)(?:[ \t\r\n][\s\S]*?)?(?=(?:\r?\n\r?\n|\Z))/gi, '');

    // Pisahkan berdasarkan baris kosong ganda (cue separator)
    const blocks = text.split(/\r?\n\r?\n+/).map(b => b.trim()).filter(Boolean);
    const srtCues = [];
    let cueIndex = 1;

    for (const block of blocks) {
        const lines = block.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
        if (lines.length === 0) continue;

        // Cari baris timestamp (e.g. 00:00:01.000 --> 00:00:04.000)
        let timeLineIndex = lines.findIndex(l => l.includes('-->'));
        if (timeLineIndex === -1) continue;

        const timeLine = lines[timeLineIndex];
        const match = timeLine.match(/((?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})\s*-->\s*((?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})/);
        if (!match) continue;

        const formatTime = (t) => {
            let [hms, ms] = t.split(/[\.,]/);
            const parts = hms.split(':');
            let hours = '00';
            let minutes = '00';
            let seconds = '00';

            if (parts.length === 3) {
                hours = parts[0].padStart(2, '0');
                minutes = parts[1].padStart(2, '0');
                seconds = parts[2].padStart(2, '0');
            } else if (parts.length === 2) {
                minutes = parts[0].padStart(2, '0');
                seconds = parts[1].padStart(2, '0');
            }
            ms = (ms || '000').padEnd(3, '0').slice(0, 3);
            return `${hours}:${minutes}:${seconds},${ms}`;
        };

        const startTime = formatTime(match[1]);
        const endTime = formatTime(match[2]);

        // Ambil isi teks subtitle setelah baris timestamp
        const textLines = lines.slice(timeLineIndex + 1);
        if (textLines.length === 0) continue;

        // Bersihkan tag WebVTT (misal <c.color>, <v Voice>, <ruby>, <b>, <i> dll)
        const cleanText = textLines.map(line => {
            return line
                .replace(/<[^>]+>/g, '') // Hapus tag HTML / VTT
                .replace(/&amp;/g, '&')
                .replace(/&lt;/g, '<')
                .replace(/&gt;/g, '>')
                .replace(/&lrm;/g, '')
                .replace(/&rlm;/g, '')
                .trim();
        }).filter(Boolean).join('\n');

        if (!cleanText) continue;

        srtCues.push(`${cueIndex}\n${startTime} --> ${endTime}\n${cleanText}`);
        cueIndex++;
    }

    return srtCues.join('\n\n') + '\n';
}
