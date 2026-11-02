// Chooses the viewer for a file from its content signature first and its extension second, so a
// renamed file never reaches the wrong parser.
export type ViewerKind = 'pdf' | 'xlsx' | 'csv' | 'docx' | 'text' | 'xls' | 'unsupported';

const starts = (b: Uint8Array, sig: number[]) => sig.every((v, i) => b[i] === v);

export function extensionOf(name: string) { const i = name.lastIndexOf('.'); return i < 0 ? '' : name.slice(i + 1).toLowerCase() }

/** Names of the entries in a zip file's central directory (no decompression). */
export function zipEntries(bytes: Uint8Array): string[] {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), names: string[] = [];
    // The end-of-central-directory record is within the last 64 KB + 22 bytes.
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
        if (view.getUint32(i, true) !== 0x06054b50) continue;
        let p = view.getUint32(i + 16, true); const count = view.getUint16(i + 10, true), decoder = new TextDecoder();
        for (let n = 0; n < count && p + 46 <= bytes.length && view.getUint32(p, true) === 0x02014b50; n++) {
            const nameLength = view.getUint16(p + 28, true), extra = view.getUint16(p + 30, true), comment = view.getUint16(p + 32, true);
            names.push(decoder.decode(bytes.subarray(p + 46, p + 46 + nameLength)));
            p += 46 + nameLength + extra + comment;
        }
        break;
    }
    return names;
}

export function detectKind(name: string, bytes: Uint8Array): ViewerKind {
    const ext = extensionOf(name);
    if (starts(bytes, [0x25, 0x50, 0x44, 0x46, 0x2d])) return 'pdf'; // %PDF-
    if (starts(bytes, [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])) return 'xls'; // legacy OLE2 (.xls / .doc)
    if (starts(bytes, [0x50, 0x4b])) {
        const entries = zipEntries(bytes);
        if (entries.some(e => e.startsWith('xl/'))) return 'xlsx';
        if (entries.some(e => e.startsWith('word/'))) return 'docx';
        return 'unsupported';
    }
    if (ext === 'csv') return 'csv';
    if (ext === 'txt') return 'text';
    return 'unsupported';
}

/** Workbook parts the browser spreadsheet editor (ExcelJS) does not carry over into a saved copy. */
export function workbookLosses(bytes: Uint8Array): string[] {
    const entries = zipEntries(bytes), found: string[] = [];
    if (entries.some(e => e.startsWith('xl/charts/'))) found.push('charts');
    if (entries.some(e => e.startsWith('xl/pivotTables/') || e.startsWith('xl/pivotCache/'))) found.push('pivot tables');
    if (entries.some(e => e.includes('vbaProject'))) found.push('macros');
    if (entries.some(e => e.startsWith('xl/slicers/') || e.startsWith('xl/timelines/'))) found.push('slicers');
    if (entries.some(e => e.startsWith('xl/externalLinks/'))) found.push('external links');
    return found;
}
