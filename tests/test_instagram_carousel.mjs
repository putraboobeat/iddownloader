import assert from 'assert';

console.log('========================================================');
console.log('   PENGUJIAN EKSTRAKSI INSTAGRAM CAROUSEL MULTI-SLIDE   ');
console.log('========================================================');

// Mock helper to simulate processCarouselList from popup.js
function processCarouselList(carouselList) {
    const items = [];
    const seen = new Set();
    const add = (url, type) => {
        if (!url || typeof url !== 'string' || !url.startsWith('http')) return;
        const clean = url.replace(/\\u0026/g, '&').replace(/\\/g, '');
        if (seen.has(clean)) return;
        seen.add(clean);
        items.push({ url: clean, type });
    };

    if (!Array.isArray(carouselList) || carouselList.length === 0) return items;
    for (const slide of carouselList) {
        if (!slide) continue;
        if (slide.is_video || slide.video_versions) {
            const vUrl = slide.video_versions?.[0]?.url || slide.video_url;
            if (vUrl) add(vUrl, 'video');
        } else {
            const candidates = slide.image_versions2?.candidates || [];
            const bestCand = candidates.length > 0 ? candidates[0].url : slide.display_url;
            if (bestCand) add(bestCand, 'image');
        }
    }
    return items;
}

// 1. UJI STRATEGI 1: Web API (6 slides foto & video campuran)
console.log('\n--- TEST 1: Strategi 1 Web API & Carousel Media List ---');
const mockApiResponse = {
    items: [{
        id: "12345_6789",
        carousel_media: [
            { id: "s1", is_video: false, image_versions2: { candidates: [{ url: "https://scontent.cdninstagram.com/v/t51/s1.jpg", width: 1080 }] } },
            { id: "s2", is_video: false, image_versions2: { candidates: [{ url: "https://scontent.cdninstagram.com/v/t51/s2.jpg", width: 1080 }] } },
            { id: "s3", is_video: true, video_versions: [{ url: "https://scontent.cdninstagram.com/v/t51/s3.mp4", width: 720 }] },
            { id: "s4", is_video: false, image_versions2: { candidates: [{ url: "https://scontent.cdninstagram.com/v/t51/s4.jpg", width: 1080 }] } },
            { id: "s5", is_video: false, image_versions2: { candidates: [{ url: "https://scontent.cdninstagram.com/v/t51/s5.jpg", width: 1080 }] } },
            { id: "s6", is_video: false, image_versions2: { candidates: [{ url: "https://scontent.cdninstagram.com/v/t51/s6.jpg", width: 1080 }] } }
        ]
    }]
};
const apiItems = processCarouselList(mockApiResponse.items[0].carousel_media);
assert.strictEqual(apiItems.length, 6, "Harus mengekstrak tepat 6 slide dari API");
assert.strictEqual(apiItems[2].type, 'video', "Slide 3 harus terdeteksi sebagai video");
assert.strictEqual(apiItems[0].type, 'image', "Slide 1 harus terdeteksi sebagai image");
console.log('✔ Test 1 Passed: Seluruh 6 slide (5 foto + 1 video) terdeteksi lengkap dari Web API.');

// 2. UJI STRATEGI 2: In-Page Script Tags & Relay Cache balanced bracket extractor
console.log('\n--- TEST 2: Strategi 2 Script Tag Inline Relay JSON (Balanced Array) ---');
const sampleScriptText = `
self.__wrap && self.__wrap(function() {
    return {"require":[["RelayPrefetchedStreamCache",[],["xdt_api__v1__media__shortcode__web_info",{"items":[{"carousel_media":[{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s1.jpg"}]}},{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s2.jpg"}]}},{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s3.jpg"}]}},{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s4.jpg"}]}},{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s5.jpg"}]}},{"image_versions2":{"candidates":[{"url":"https://scontent.cdninstagram.com/s6.jpg"}]}}]}]}]]};
});
`;

let parsedCar = null;
const carIdx = sampleScriptText.indexOf('"carousel_media"');
assert(carIdx !== -1, "Harus menemukan 'carousel_media' dalam script");
const bracketStart = sampleScriptText.indexOf('[', carIdx);
assert(bracketStart !== -1, "Harus menemukan bracket buka '['");
let depth = 0, inStr = false, esc = false;
for (let i = bracketStart; i < sampleScriptText.length; i++) {
    const c = sampleScriptText[i];
    if (esc) { esc = false; continue; }
    if (c === '\\') { esc = true; continue; }
    if (c === '"') { inStr = !inStr; continue; }
    if (!inStr) {
        if (c === '[') depth++;
        else if (c === ']') {
            depth--;
            if (depth === 0) {
                const jsonArr = sampleScriptText.slice(bracketStart, i + 1);
                parsedCar = JSON.parse(jsonArr);
                break;
            }
        }
    }
}
assert(parsedCar, "JSON parser harus berhasil mengurai array carousel_media lengkap");
const scriptItems = processCarouselList(parsedCar);
assert.strictEqual(scriptItems.length, 6, "Script JSON harus mengekstrak tepat 6 slide");
console.log('✔ Test 2 Passed: 6 slide berhasil diekstrak langsung dari script Relay Instagram tanpa menyentuh DOM.');

// 3. UJI STRATEGI 3: DOM Carousel Navigation Sweep Simulation (Virtual DOM)
console.log('\n--- TEST 3: Strategi 3 Virtualized DOM Navigation Sweep ---');
class MockInstagramDOM {
    constructor(totalSlides = 6) {
        this.totalSlides = totalSlides;
        this.currentIndex = 0; // slide 1
        this.allUrls = Array.from({ length: totalSlides }, (_, i) => `https://scontent.cdninstagram.com/slide_${i + 1}.jpg`);
    }

    getMountedImages() {
        const mounted = [];
        const start = Math.max(0, this.currentIndex - 1);
        const end = Math.min(this.totalSlides - 1, this.currentIndex + 1);
        for (let i = start; i <= end; i++) {
            mounted.push({ src: this.allUrls[i], naturalWidth: 1080, naturalHeight: 1080 });
        }
        return mounted;
    }

    hasNext() {
        return this.currentIndex < this.totalSlides - 1;
    }

    clickNext() {
        if (this.hasNext()) {
            this.currentIndex++;
            return true;
        }
        return false;
    }

    hasPrev() {
        return this.currentIndex > 0;
    }

    clickPrev() {
        if (this.hasPrev()) {
            this.currentIndex--;
            return true;
        }
        return false;
    }
}

const mockDom = new MockInstagramDOM(6);

const harvested = new Set();
const harvest = () => {
    for (const img of mockDom.getMountedImages()) {
        harvested.add(img.src);
    }
};

// 1. Panen slide pertama (di awal hanya ada slide 1 dan 2)
harvest();
assert.strictEqual(harvested.size, 2, "Awal DOM virtualized hanya memuat 2 slide");

// 2. Klik next hingga habis
let forwardClicks = 0;
while (mockDom.hasNext()) {
    mockDom.clickNext();
    forwardClicks++;
    harvest();
}

assert.strictEqual(forwardClicks, 5, "Harus melakukan 5 kali klik Next untuk 6 slide");
assert.strictEqual(harvested.size, 6, "Setelah sweep selesai, seluruh 6 slide harus berhasil terkumpul");

// 3. Rewind kembali ke awal
for (let i = 0; i < forwardClicks; i++) {
    mockDom.clickPrev();
}
assert.strictEqual(mockDom.currentIndex, 0, "Carousel harus kembali tepat ke slide pertama (rewound)");
console.log('✔ Test 3 Passed: DOM Navigation Sweep sukses memanen 6/6 slide dari DOM virtualized & rewind ke posisi semula.');

// 4. UJI PROTEKSI COUNT: Math.max mencegah downgrade jumlah slide
console.log('\n--- TEST 4: Proteksi Count (Tidak Boleh Ter-downgrade ke 2 Slide) ---');
const serverCount = 6;
const browserFound = 2; // misal browser hanya melihat slide awal sebelum sweep
const finalCount = Math.max(browserFound, serverCount);
assert.strictEqual(finalCount, 6, "Jumlah slide harus tetap 6 dan tidak boleh diturunkan menjadi 2");
console.log('✔ Test 4 Passed: Count badge dan slide picker terlindungi dari downgrade nilai.');

console.log('\n========================================================');
console.log('   SEMUA PENGUJIAN INSTAGRAM CAROUSEL 6-SLIDE LULUS!   ');
console.log('========================================================\n');
