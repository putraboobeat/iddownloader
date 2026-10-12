/**
 * Pure JavaScript standard uncompressed (STORE) ZIP archive builder.
 * Produces valid .ZIP archives compatible with macOS Archive Utility, Windows Explorer, 7-Zip, and Linux.
 */

export function crc32(u8) {
    const data = u8 instanceof Uint8Array ? u8 : new Uint8Array(u8);
    let table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
        let c = i;
        for (let k = 0; k < 8; k++) {
            c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
        }
        table[i] = c;
    }
    let crc = 0 ^ (-1);
    for (let i = 0; i < data.length; i++) {
        crc = (crc >>> 8) ^ table[(crc ^ data[i]) & 0xFF];
    }
    return (crc ^ (-1)) >>> 0;
}

export function createZipBlob(files) {
    const enc = new TextEncoder();
    const chunks = [];
    const cdChunks = [];
    let offset = 0;

    for (const f of files) {
        const nameBytes = enc.encode(f.name);
        const dataBytes = f.data instanceof Uint8Array ? f.data : new Uint8Array(f.data);
        const crc = crc32(dataBytes);
        const size = dataBytes.length;

        // Local file header (30 bytes + filename)
        const lh = new Uint8Array(30 + nameBytes.length);
        const dv = new DataView(lh.buffer);
        dv.setUint32(0, 0x04034b50, true);
        dv.setUint16(4, 10, true);
        dv.setUint16(6, 0, true);
        dv.setUint16(8, 0, true); // Compression: 0 (STORED)
        dv.setUint16(10, 0, true);
        dv.setUint16(12, 0, true);
        dv.setUint32(14, crc, true);
        dv.setUint32(18, size, true);
        dv.setUint32(22, size, true);
        dv.setUint16(26, nameBytes.length, true);
        dv.setUint16(28, 0, true);
        lh.set(nameBytes, 30);

        chunks.push(lh, dataBytes);

        // Central directory header (46 bytes + filename)
        const cd = new Uint8Array(46 + nameBytes.length);
        const cdv = new DataView(cd.buffer);
        cdv.setUint32(0, 0x02014b50, true);
        cdv.setUint16(4, 20, true);
        cdv.setUint16(6, 10, true);
        cdv.setUint16(8, 0, true);
        cdv.setUint16(10, 0, true);
        cdv.setUint16(12, 0, true);
        cdv.setUint16(14, 0, true);
        cdv.setUint32(16, crc, true);
        cdv.setUint32(20, size, true);
        cdv.setUint32(24, size, true);
        cdv.setUint16(28, nameBytes.length, true);
        cdv.setUint16(30, 0, true);
        cdv.setUint16(32, 0, true);
        cdv.setUint16(34, 0, true);
        cdv.setUint16(36, 0, true);
        cdv.setUint32(38, 0, true);
        cdv.setUint32(42, offset, true);
        cd.set(nameBytes, 46);

        cdChunks.push(cd);
        offset += lh.length + dataBytes.length;
    }

    const cdOffset = offset;
    let cdSize = 0;
    for (const c of cdChunks) cdSize += c.length;

    // End of central directory record (22 bytes)
    const eocd = new Uint8Array(22);
    const edv = new DataView(eocd.buffer);
    edv.setUint32(0, 0x06054b50, true);
    edv.setUint16(4, 0, true);
    edv.setUint16(6, 0, true);
    edv.setUint16(8, files.length, true);
    edv.setUint16(10, files.length, true);
    edv.setUint32(12, cdSize, true);
    edv.setUint32(16, cdOffset, true);
    edv.setUint16(20, 0, true);

    return new Blob([...chunks, ...cdChunks, eocd], { type: 'application/zip' });
}
