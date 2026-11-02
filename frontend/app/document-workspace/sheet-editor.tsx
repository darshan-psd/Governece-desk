'use client';
// Spreadsheet editor for XLSX and CSV, built on ExcelJS (MIT). The workbook object ExcelJS loads is
// the single source of truth: edits change only the cells touched, so formatting, column widths,
// merges, other sheets and everything else ExcelJS understands are written back as they were read.
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { AlertCircle, Download, Loader2 } from 'lucide-react';
import { address, colName, evaluate, FormulaError, parse, parseAddress, references, SUPPORTED_FUNCTIONS, Unsupported, type Scalar } from './formula';
import { workbookLosses } from './detect';
import type { ViewerApi } from './viewer-api';

type XWorkbook = any; type XSheet = any; type XCell = any;
const ROW_H = 24, HEAD_W = 52, DEFAULT_COL = 64, MAX_FORMAT_CELLS = 200_000;
const FORMATS: [string, string][] = [['General', 'General'], ['0', 'Whole number'], ['0.00', 'Number (2 dp)'], ['#,##0', 'Thousands'], ['#,##0.00', 'Thousands (2 dp)'], ['0%', 'Percent'], ['0.00%', 'Percent (2 dp)'], ['"₹"#,##0.00', 'Rupee'], ['dd-mm-yyyy', 'Date']];

// CSV ---------------------------------------------------------------------------------------
export function parseCsv(text: string): string[][] {
    const rows: string[][] = []; let row: string[] = [], field = '', quoted = false;
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        if (quoted) { if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++ } else quoted = false } else field += ch; continue }
        if (ch === '"' && field === '') quoted = true;
        else if (ch === ',') { row.push(field); field = '' }
        else if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; row.push(field); rows.push(row); row = []; field = '' }
        else field += ch;
    }
    if (field !== '' || row.length) { row.push(field); rows.push(row) }
    return rows;
}
const csvField = (v: string) => /[",\r\n]/.test(v) || /^\s|\s$/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
const NUMERIC = /^-?(0|[1-9]\d*)(\.\d+)?$/;

// Values and display ------------------------------------------------------------------------
function scalarOf(v: any): Scalar {
    if (v === null || v === undefined) return null;
    if (typeof v !== 'object' || v instanceof Date) return v;
    if ('formula' in v || 'sharedFormula' in v) return scalarOf(v.result);
    if ('error' in v) return new FormulaError(String(v.error));
    if ('richText' in v) return (v.richText || []).map((t: any) => t.text).join('');
    if ('text' in v) return typeof v.text === 'string' ? v.text : scalarOf(v.text);
    return null;
}
const isFormula = (v: any) => !!v && typeof v === 'object' && ('formula' in v || 'sharedFormula' in v);
const serialToDate = (n: number) => new Date(Math.round((n - 25569) * 86400000));
const isDateFormat = (f: string) => /(^|[^"\\])[dmy]{1,4}/i.test(f.replace(/"[^"]*"/g, '')) && !/0/.test(f.replace(/"[^"]*"/g, ''));

export function formatValue(v: Scalar, numFmt?: string): string {
    if (v === null || v === undefined) return '';
    if (v instanceof FormulaError) return v.code;
    if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
    const fmt = (numFmt || 'General').split(';')[0];
    if (v instanceof Date || (typeof v === 'number' && fmt !== 'General' && isDateFormat(fmt))) {
        const d = v instanceof Date ? v : serialToDate(v);
        if (isNaN(d.getTime())) return String(v);
        const dd = String(d.getUTCDate()).padStart(2, '0'), mm = String(d.getUTCMonth() + 1).padStart(2, '0'), yyyy = d.getUTCFullYear();
        return /mmm/i.test(fmt) ? `${dd}-${d.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })}-${yyyy}` : /^m/i.test(fmt) ? `${mm}/${dd}/${yyyy}` : `${dd}-${mm}-${yyyy}`;
    }
    if (typeof v !== 'number') return String(v);
    if (fmt === 'General' || fmt === '@') return Math.abs(v) >= 1e11 || (Math.abs(v) < 1e-9 && v !== 0) ? v.toExponential(5) : String(Number(v.toPrecision(11)));
    const plain = fmt.replace(/"[^"]*"|\[[^\]]*\]|\\./g, ''), decimals = (/\.(0+)/.exec(plain)?.[1].length) ?? 0;
    const percent = plain.includes('%'), grouped = plain.includes(',');
    const symbol = /₹|\$|€|£/.exec(fmt)?.[0] ?? '';
    const n = percent ? v * 100 : v;
    const body = Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: grouped });
    return (n < 0 ? '-' : '') + symbol + body + (percent ? '%' : '');
}

function inputText(cell: XCell): string {
    const v = cell?.value;
    if (isFormula(v)) return '=' + (cell.formula ?? v.formula ?? '');
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    const s = scalarOf(v);
    return s === null ? '' : s instanceof FormulaError ? s.code : typeof s === 'boolean' ? (s ? 'TRUE' : 'FALSE') : String(s);
}

/** ExcelJS colour {argb:'FFRRGGBB'} → CSS, ignoring theme/indexed colours it cannot resolve. */
const argb = (c: any) => typeof c?.argb === 'string' && c.argb.length === 8 ? '#' + c.argb.slice(2) : undefined;

function cellStyle(cell: XCell): CSSProperties {
    const s: CSSProperties = {}, font = cell?.font, al = cell?.alignment, fill = cell?.fill;
    if (font?.bold) s.fontWeight = 700;
    if (font?.italic) s.fontStyle = 'italic';
    if (font?.underline) s.textDecoration = 'underline';
    const color = argb(font?.color); if (color && color !== '#000000') s.color = color;
    if (fill?.type === 'pattern' && fill.pattern === 'solid') { const bg = argb(fill.fgColor); if (bg) s.background = bg }
    if (al?.horizontal === 'center' || al?.horizontal === 'centerContinuous') s.justifyContent = 'center';
    else if (al?.horizontal === 'right') s.justifyContent = 'flex-end';
    else if (al?.horizontal === 'left') s.justifyContent = 'flex-start';
    return s;
}

/** Shared formulas are rewritten as the equivalent explicit formulas, so editing any cell is safe. */
function explicitFormulas(wb: XWorkbook) {
    for (const ws of wb.worksheets) {
        const pending: [XCell, string, any][] = [];
        ws.eachRow({ includeEmpty: false }, (row: any) => row.eachCell({ includeEmpty: false }, (cell: XCell) => {
            const v = cell.value;
            if (v && typeof v === 'object' && ('sharedFormula' in v || v.shareType === 'shared')) pending.push([cell, cell.formula, v.result]);
        }));
        for (const [cell, formula, result] of pending) if (formula) cell.value = { formula, result };
    }
}

type Sel = { r: number; c: number };
type Props = { bytes: ArrayBuffer; name: string; kind: 'xlsx' | 'csv'; editable: boolean; onDirty: (dirty: boolean) => void; register: (api: ViewerApi | null) => void };

export default function SheetEditor({ bytes, name, kind, editable, onDirty, register }: Props) {
    const [wb, setWb] = useState<XWorkbook | null>(null), [error, setError] = useState(''), [sheet, setSheet] = useState(0), [tick, setTick] = useState(0);
    const [sel, setSel] = useState<Sel>({ r: 1, c: 1 }), [anchor, setAnchor] = useState<Sel>({ r: 1, c: 1 }), [editing, setEditing] = useState<{ text: string; bar: boolean } | null>(null);
    const [view, setView] = useState({ top: 0, left: 0, w: 800, h: 400 }), [resizing, setResizing] = useState<{ c: number; px: number } | null>(null);
    const [status, setStatus] = useState(''), [losses, setLosses] = useState<string[]>([]), [nameBox, setNameBox] = useState('A1');
    const scroller = useRef<HTMLDivElement>(null), colHead = useRef<HTMLDivElement>(null), rowHead = useRef<HTMLDivElement>(null), editor = useRef<HTMLInputElement>(null), stale = useRef(new Set<string>()), parsed = useRef(new Map<string, any>());
    const ws: XSheet | null = wb?.worksheets[sheet] ?? null;

    useEffect(() => {
        let gone = false;
        (async () => {
            try {
                const mod: any = await import('exceljs'), ExcelJS = mod.default ?? mod, book = new ExcelJS.Workbook();
                if (kind === 'xlsx') { await book.xlsx.load(bytes.slice(0)); explicitFormulas(book); setLosses(workbookLosses(new Uint8Array(bytes))) }
                else {
                    const sheetCsv = book.addWorksheet('Sheet1'), rows = parseCsv(new TextDecoder().decode(bytes).replace(/^\uFEFF/, ''));
                    rows.forEach((row, i) => row.forEach((v, j) => { if (v !== '') sheetCsv.getCell(i + 1, j + 1).value = NUMERIC.test(v) && v.length < 16 ? Number(v) : v }));
                }
                if (!book.worksheets.length) book.addWorksheet('Sheet1');
                if (!gone) { setWb(book); setSheet(Math.max(0, book.worksheets.findIndex((w: any) => w.state === 'visible'))) }
            } catch (e: any) { if (!gone) setError('This workbook could not be opened in the browser editor: ' + (e?.message || 'unknown error') + '. Download it to open it in Excel.') }
        })();
        return () => { gone = true };
    }, [bytes, kind]);

    // Grid geometry ----------------------------------------------------------------------------
    const rowCount = ws ? Math.min(1048576, Math.max(ws.rowCount + 30, 100)) : 0, colCount = ws ? Math.min(16384, Math.max(ws.columnCount + 6, 26)) : 0;
    const widths = useMemo(() => {
        const out = [0];
        for (let c = 1; c <= colCount; c++) { const col = ws?.getColumn(c); out.push(col?.hidden ? 0 : col?.width ? Math.round(col.width * 7 + 5) : DEFAULT_COL) }
        if (resizing) out[resizing.c] = resizing.px;
        return out;
    }, [ws, colCount, tick, resizing]);
    const lefts = useMemo(() => { const out = [0, 0]; for (let c = 1; c <= colCount; c++) out[c + 1] = out[c] + widths[c]; return out }, [widths, colCount]);
    const merges = useMemo(() => {
        const spans = new Map<string, { rows: number; cols: number }>(), covered = new Set<string>();
        for (const range of Object.values<any>(ws?._merges || {})) {
            const m = range?.model; if (!m) continue;
            spans.set(m.top + ':' + m.left, { rows: m.bottom - m.top + 1, cols: m.right - m.left + 1 });
            for (let r = m.top; r <= m.bottom; r++) for (let c = m.left; c <= m.right; c++) if (r !== m.top || c !== m.left) covered.add(r + ':' + c);
        }
        return { spans, covered };
    }, [ws, tick]);
    const colAt = (x: number) => { let lo = 1, hi = colCount; while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (lefts[mid] <= x) lo = mid; else hi = mid - 1 } return lo };
    const r1 = Math.max(1, Math.floor(view.top / ROW_H) + 1), r2 = Math.min(rowCount, Math.ceil((view.top + view.h) / ROW_H) + 1);
    const c1 = ws ? colAt(view.left) : 1, c2 = ws ? Math.min(colCount, colAt(view.left + view.w) + 1) : 0;
    const box = { r1: Math.min(sel.r, anchor.r), r2: Math.max(sel.r, anchor.r), c1: Math.min(sel.c, anchor.c), c2: Math.max(sel.c, anchor.c) };

    useEffect(() => {
        const el = scroller.current; if (!el) return;
        const measure = () => setView(v => ({ ...v, w: el.clientWidth, h: el.clientHeight }));
        measure(); const ro = new ResizeObserver(measure); ro.observe(el); return () => ro.disconnect();
    }, [wb]);
    function onScroll() {
        const el = scroller.current!; const { scrollTop: top, scrollLeft: left } = el;
        if (colHead.current) colHead.current.style.transform = `translateX(${-left}px)`;
        if (rowHead.current) rowHead.current.style.transform = `translateY(${-top}px)`;
        setView(v => ({ ...v, top, left }));
    }
    useEffect(() => { // keep the active cell in view
        const el = scroller.current; if (!el || !ws) return;
        const top = (sel.r - 1) * ROW_H, left = lefts[sel.c], w = widths[sel.c];
        if (top < el.scrollTop) el.scrollTop = top; else if (top + ROW_H > el.scrollTop + el.clientHeight) el.scrollTop = top + ROW_H - el.clientHeight;
        if (left < el.scrollLeft) el.scrollLeft = left; else if (left + w > el.scrollLeft + el.clientWidth) el.scrollLeft = left + w - el.clientWidth;
        setNameBox(address(sel.r, sel.c));
    }, [sel, sheet, ws]);
    useEffect(() => { setSel({ r: 1, c: 1 }); setAnchor({ r: 1, c: 1 }); setEditing(null); scroller.current?.scrollTo(0, 0) }, [sheet]);

    // Formulas ---------------------------------------------------------------------------------
    const book = useMemo(() => wb && {
        hasSheet: (s: string) => !!wb.getWorksheet(s),
        rows: (s: string) => wb.getWorksheet(s)?.rowCount ?? 0,
        get: (s: string, r: number, c: number) => { const w = wb.getWorksheet(s); return w ? scalarOf(w.findCell(r, c)?.value) : new FormulaError('#REF!') },
    }, [wb]);
    const tree = (f: string) => { let t = parsed.current.get(f); if (t === undefined) { try { t = parse(f) } catch (e) { t = e } parsed.current.set(f, t) } return t };
    /** Recalculates every formula that (transitively) reads a changed cell. */
    function recalc(changed: { sheet: string; r: number; c: number }[]) {
        if (!wb || !book) return;
        const formulas: { w: XSheet; cell: XCell; node: any; refs: any[] }[] = [];
        for (const w of wb.worksheets) w.eachRow({ includeEmpty: false }, (row: any) => row.eachCell({ includeEmpty: false }, (cell: XCell) => {
            if (!isFormula(cell.value)) return; const node = tree(cell.formula);
            if (!(node instanceof Error)) formulas.push({ w, cell, node, refs: references(node, w.name) });
        }));
        let wave = changed;
        for (let round = 0; round < 64 && wave.length; round++) {
            const next: typeof wave = [];
            for (const f of formulas) {
                if (!wave.some(k => f.refs.some(a => a.sheet === k.sheet && k.r >= a.r1 && k.r <= a.r2 && k.c >= a.c1 && k.c <= a.c2))) continue;
                const key = f.w.name + '!' + f.cell.address;
                try {
                    const result = evaluate(f.node, f.w.name, book), old = scalarOf(f.cell.value);
                    stale.current.delete(key);
                    const same = old instanceof FormulaError && result instanceof FormulaError ? old.code === result.code : old === result;
                    if (!same) { f.cell.value = { formula: f.cell.formula, result: result instanceof FormulaError ? { error: result.code } : result }; next.push({ sheet: f.w.name, r: Number(f.cell.row), c: Number(f.cell.col) }) }
                } catch (e) { if (e instanceof Unsupported) stale.current.add(key); else throw e }
            }
            wave = next;
        }
    }
    function changed() { setTick(t => t + 1); onDirty(true) }

    function commit(r: number, c: number, text: string) {
        if (!ws || !editable) return true;
        const target = ws.getCell(r, c).isMerged ? ws.getCell(r, c).master : ws.getCell(r, c), raw = text.trim();
        let value: any;
        if (raw.startsWith('=') && raw.length > 1) {
            const formula = raw.slice(1), node = tree(formula);
            if (node instanceof SyntaxError) { setStatus('That formula has a syntax error: ' + node.message + '.'); return false }
            let result: any; const key = ws.name + '!' + target.address;
            try { const v = node instanceof Unsupported ? (() => { throw node })() : evaluate(node, ws.name, book!); result = v instanceof FormulaError ? { error: v.code } : v; stale.current.delete(key); setStatus('') }
            catch (e) { if (!(e instanceof Unsupported)) throw e; result = { error: '#NAME?' }; stale.current.add(key); setStatus((e.message || 'This formula') + '. Excel will calculate it when the file is opened. Supported here: ' + SUPPORTED_FUNCTIONS.join(', ') + '.') }
            value = { formula, result };
        } else if (raw === '') value = null;
        else if (/^-?[\d,]*\.?\d+%$/.test(raw)) { value = Number(raw.replace(/[,%]/g, '')) / 100; if (!target.numFmt || target.numFmt === 'General') target.numFmt = '0%' }
        else if (/^-?\d{1,3}(,\d{2,3})+(\.\d+)?$|^-?\d+(\.\d+)?(e[+-]?\d+)?$/i.test(raw) && !/^-?0\d/.test(raw)) value = Number(raw.replace(/,/g, ''));
        else if (/^(true|false)$/i.test(raw)) value = raw.toUpperCase() === 'TRUE';
        else value = text;
        const before = inputText(target);
        if (before === (value === null ? '' : raw.startsWith('=') ? raw : text) && !isFormula(value)) return true;
        target.value = value;
        recalc([{ sheet: ws.name, r: Number(target.row), c: Number(target.col) }]);
        changed();
        return true;
    }
    function eachSelected(fn: (cell: XCell) => void) {
        if (!ws) return false;
        if ((box.r2 - box.r1 + 1) * (box.c2 - box.c1 + 1) > MAX_FORMAT_CELLS) { setStatus('Select fewer than 200,000 cells to format at once.'); return false }
        for (let r = box.r1; r <= box.r2; r++) for (let c = box.c1; c <= box.c2; c++) fn(ws.getCell(r, c));
        return true;
    }
    const active: XCell | null = ws ? ws.findCell(sel.r, sel.c) ?? null : null;
    function toggleFont(key: 'bold' | 'italic') { if (!editable) return; const on = !active?.font?.[key]; if (eachSelected(cell => { cell.font = { ...(cell.font || {}), [key]: on } })) changed() }
    function align(horizontal: string) { if (!editable) return; const off = active?.alignment?.horizontal === horizontal; if (eachSelected(cell => { cell.alignment = { ...(cell.alignment || {}), horizontal: off ? undefined : horizontal } })) changed() }
    function numberFormat(fmt: string) { if (!editable) return; if (eachSelected(cell => { cell.numFmt = fmt === 'General' ? undefined : fmt })) changed() }
    function clearSelection() { if (!editable || !ws) return; const keys: any[] = []; if (eachSelected(cell => { if (cell.value !== null) { cell.value = null; keys.push({ sheet: ws.name, r: Number(cell.row), c: Number(cell.col) }) } }) && keys.length) { recalc(keys); changed() } }

    function move(dr: number, dc: number, extend = false) {
        const next = { r: Math.min(rowCount, Math.max(1, sel.r + dr)), c: Math.min(colCount, Math.max(1, sel.c + dc)) };
        setSel(next); if (!extend) setAnchor(next);
    }
    function startEdit(initial?: string) { if (!editable) { setStatus('This version is read-only.'); return } setEditing({ text: initial ?? inputText(active), bar: false }); requestAnimationFrame(() => editor.current?.focus()) }
    function finishEdit(dr: number, dc: number) { if (!editing) return; if (commit(sel.r, sel.c, editing.text)) { setEditing(null); move(dr, dc); scroller.current?.focus() } }

    function onGridKey(e: React.KeyboardEvent) {
        if (editing && !editing.bar) return; // the cell editor handles its own keys
        const mod = e.metaKey || e.ctrlKey;
        if (mod && (e.key === 'b' || e.key === 'i')) { e.preventDefault(); toggleFont(e.key === 'b' ? 'bold' : 'italic'); return }
        if (mod && e.key === 'Home') { e.preventDefault(); setSel({ r: 1, c: 1 }); setAnchor({ r: 1, c: 1 }); return }
        if (e.altKey && e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { e.preventDefault(); if (editable) { setWidth(sel.c, Math.max(16, widths[sel.c] + (e.key === 'ArrowRight' ? 8 : -8))); setStatus(`Column ${colName(sel.c)} width ${Math.max(16, widths[sel.c] + (e.key === 'ArrowRight' ? 8 : -8))} px`) } return }
        const keys: Record<string, [number, number]> = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], PageDown: [20, 0], PageUp: [-20, 0] };
        if (keys[e.key]) { e.preventDefault(); move(...keys[e.key], e.shiftKey); return }
        if (e.key === 'Tab') { e.preventDefault(); move(0, e.shiftKey ? -1 : 1); return }
        if (e.key === 'Enter') { e.preventDefault(); move(e.shiftKey ? -1 : 1, 0); return }
        if (e.key === 'F2') { e.preventDefault(); startEdit(); return }
        if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); clearSelection(); return }
        if (e.key.length === 1 && !mod && !e.altKey) { e.preventDefault(); startEdit(e.key) }
    }
    function cellFromPoint(e: React.MouseEvent) {
        const rect = scroller.current!.getBoundingClientRect(), x = e.clientX - rect.left + scroller.current!.scrollLeft, y = e.clientY - rect.top + scroller.current!.scrollTop;
        return { r: Math.min(rowCount, Math.max(1, Math.floor(y / ROW_H) + 1)), c: colAt(x) };
    }
    const dragging = useRef(false);

    // Column resizing (pointer drag, or arrow keys on the focused handle) ------------------------
    function setWidth(c: number, px: number) { const col = ws.getColumn(c); col.width = Math.max(1, Math.round(((px - 5) / 7) * 100) / 100); changed() }
    function resizeStart(e: React.PointerEvent, c: number) {
        if (!editable) return; e.preventDefault(); e.stopPropagation();
        const startX = e.clientX, startW = widths[c]; let px = startW;
        const moveFn = (ev: PointerEvent) => { px = Math.max(16, startW + ev.clientX - startX); setResizing({ c, px }) };
        const up = () => { window.removeEventListener('pointermove', moveFn); window.removeEventListener('pointerup', up); setResizing(null); if (px !== startW) setWidth(c, px) };
        window.addEventListener('pointermove', moveFn); window.addEventListener('pointerup', up);
    }

    // Export -------------------------------------------------------------------------------------
    async function xlsxBytes() { wb.calcProperties = { ...(wb.calcProperties || {}), fullCalcOnLoad: true }; return new Uint8Array(await wb.xlsx.writeBuffer()) }
    function csvBytes() {
        const w = wb.worksheets[0], lines: string[] = [];
        for (let r = 1; r <= w.rowCount; r++) { const row = w.getRow(r), cells: string[] = []; for (let c = 1; c <= w.columnCount; c++) { const v = scalarOf(row.getCell(c).value); cells.push(csvField(v === null ? '' : v instanceof FormulaError ? v.code : v instanceof Date ? v.toISOString().slice(0, 10) : typeof v === 'boolean' ? (v ? 'TRUE' : 'FALSE') : String(v))) } lines.push(cells.join(',').replace(/,+$/, '')) }
        return new TextEncoder().encode(lines.join('\r\n') + '\r\n');
    }
    async function downloadXlsx() {
        const url = URL.createObjectURL(new Blob([await xlsxBytes() as BlobPart], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
        const a = document.createElement('a'); a.href = url; a.download = name.replace(/\.(xlsx|csv)$/i, '') + ' (edited).xlsx'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
    }
    useEffect(() => {
        if (!wb) return;
        register({ exportFile: async () => kind === 'xlsx' ? xlsxBytes() : csvBytes() });
        return () => register(null);
    }, [wb, kind]);

    if (error) return <div className="viewer-message" role="alert"><AlertCircle size={22} /><p>{error}</p></div>;
    if (!wb || !ws) return <div className="viewer-message" aria-busy="true"><Loader2 size={22} className="spin" /><p>Opening workbook…</p></div>;

    const numbersInSel: number[] = []; let filled = 0;
    if ((box.r2 - box.r1 + 1) * (box.c2 - box.c1 + 1) <= 20000) for (let r = box.r1; r <= box.r2; r++) for (let c = box.c1; c <= box.c2; c++) { const v = scalarOf(ws.findCell(r, c)?.value); if (v !== null && v !== '') filled++; if (typeof v === 'number') numbersInSel.push(v) }
    const sum = numbersInSel.reduce((a, b) => a + b, 0), single = box.r1 === box.r2 && box.c1 === box.c2;
    const cells = [];
    for (let r = r1; r <= r2; r++) {
        const row = [];
        for (let c = c1; c <= c2; c++) {
            if (!widths[c] || merges.covered.has(r + ':' + c)) continue;
            const cell = ws.findCell(r, c), span = merges.spans.get(r + ':' + c), v = scalarOf(cell?.value);
            const text = formatValue(v, cell?.numFmt), key = ws.name + '!' + address(r, c);
            const inBox = r >= box.r1 && r <= box.r2 && c >= box.c1 && c <= box.c2;
            const style: CSSProperties = { left: lefts[c], top: (r - 1) * ROW_H, width: span ? lefts[c + span.cols] - lefts[c] : widths[c], height: span ? span.rows * ROW_H : ROW_H, ...cellStyle(cell) };
            if (!style.justifyContent && (typeof v === 'number' || v instanceof Date)) style.justifyContent = 'flex-end';
            row.push(<div key={c} id={`cell-${r}-${c}`} role="gridcell" aria-rowindex={r + 1} aria-colindex={c + 1} aria-selected={inBox}
                className={'sheet-cell' + (inBox ? ' in-sel' : '') + (r === sel.r && c === sel.c ? ' active' : '') + (v instanceof FormulaError ? ' err' : '') + (stale.current.has(key) ? ' stale' : '')}
                title={stale.current.has(key) ? 'Calculated by Excel when the file is opened' : undefined} style={style}><span>{text}</span></div>);
        }
        cells.push(<div role="row" key={r} className="sheet-row" aria-rowindex={r + 1}>{row}</div>);
    }
    const barText = editing ? editing.text : inputText(active);
    const fmt = active?.numFmt || 'General';
    return <div className="sheet-editor">
        <div className="sheet-toolbar" role="toolbar" aria-label="Spreadsheet formatting">
            <button type="button" className={'tool' + (active?.font?.bold ? ' on' : '')} aria-pressed={!!active?.font?.bold} disabled={!editable} onClick={() => toggleFont('bold')} title="Bold (Ctrl+B)" aria-label="Bold"><b>B</b></button>
            <button type="button" className={'tool' + (active?.font?.italic ? ' on' : '')} aria-pressed={!!active?.font?.italic} disabled={!editable} onClick={() => toggleFont('italic')} title="Italic (Ctrl+I)" aria-label="Italic"><i>I</i></button>
            <span className="tool-sep" />
            {(['left', 'center', 'right'] as const).map(h => <button type="button" key={h} className={'tool' + (active?.alignment?.horizontal === h ? ' on' : '')} aria-pressed={active?.alignment?.horizontal === h} disabled={!editable} onClick={() => align(h)} aria-label={'Align ' + h} title={'Align ' + h}><span className={'align-icon ' + h}><i /><i /><i /></span></button>)}
            <span className="tool-sep" />
            <label className="tool-select"><span className="sr-only">Number format</span><select aria-label="Number format" disabled={!editable} value={FORMATS.some(([f]) => f === fmt) ? fmt : '__custom'} onChange={e => numberFormat(e.target.value)}>{!FORMATS.some(([f]) => f === fmt) && <option value="__custom">{fmt}</option>}{FORMATS.map(([f, label]) => <option key={f} value={f}>{label}</option>)}</select></label>
            <span className="tool-grow" />
            <button type="button" className="tool text" onClick={downloadXlsx} title="Download the workbook as it is now, including unsaved edits"><Download size={15} />Download XLSX</button>
        </div>
        <div className="formula-bar">
            <input className="name-box" aria-label="Cell address. Type an address and press Enter to go there" value={nameBox} onChange={e => setNameBox(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { const a = parseAddress(nameBox); if (a) { const t = { r: Math.min(a.r, rowCount), c: Math.min(a.c, colCount) }; setSel(t); setAnchor(t); scroller.current?.focus() } else setStatus('Enter a cell address such as B12.') } if (e.key === 'Escape') { e.stopPropagation(); setNameBox(address(sel.r, sel.c)) } }} />
            <span className="fx" aria-hidden="true">fx</span>
            <input className="formula-input" aria-label={'Contents of ' + address(sel.r, sel.c)} readOnly={!editable} value={barText}
                onFocus={() => editable && setEditing({ text: inputText(active), bar: true })}
                onChange={e => setEditing({ text: e.target.value, bar: true })}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); finishEdit(1, 0) } if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setEditing(null); scroller.current?.focus() } }}
                onBlur={() => { if (editing?.bar) { if (commit(sel.r, sel.c, editing.text)) setEditing(null) } }} />
        </div>
        {losses.length > 0 && <div className="viewer-warning" role="note"><AlertCircle size={15} />This workbook contains {losses.join(', ')}, which the browser editor does not carry into a saved copy. The original upload is always kept unchanged.</div>}
        {kind === 'csv' && <div className="viewer-warning" role="note"><AlertCircle size={15} />CSV files store values only. Formatting and formulas are kept in this session; saving writes calculated values. Use Download XLSX to keep formatting.</div>}
        <div className="sheet-grid">
            <div className="sheet-corner" aria-hidden="true" />
            <div className="sheet-colhead" aria-hidden="true"><div ref={colHead} className="sheet-colhead-inner" style={{ width: lefts[colCount + 1] }}>{Array.from({ length: Math.max(0, c2 - c1 + 1) }, (_, i) => c1 + i).filter(c => widths[c]).map(c => <div key={c} className={'sheet-hcell' + (c >= box.c1 && c <= box.c2 ? ' in-sel' : '')} style={{ left: lefts[c], width: widths[c] }} onMouseDown={e => { setAnchor({ r: rowCount, c }); setSel({ r: 1, c }); e.preventDefault() }}>{colName(c)}
                {editable && <span className="col-resize" role="separator" aria-orientation="vertical" aria-label={'Resize column ' + colName(c)} aria-valuenow={widths[c]} tabIndex={-1} onPointerDown={e => resizeStart(e, c)} />}</div>)}</div></div>
            <div className="sheet-rowhead" aria-hidden="true"><div ref={rowHead} className="sheet-rowhead-inner" style={{ height: rowCount * ROW_H }}>{Array.from({ length: Math.max(0, r2 - r1 + 1) }, (_, i) => r1 + i).map(r => <div key={r} className={'sheet-hcell' + (r >= box.r1 && r <= box.r2 ? ' in-sel' : '')} style={{ top: (r - 1) * ROW_H }} onMouseDown={e => { setAnchor({ r, c: colCount }); setSel({ r, c: 1 }); e.preventDefault() }}>{r}</div>)}</div></div>
            <div ref={scroller} className="sheet-body" tabIndex={0} role="grid" aria-label={`Sheet ${ws.name}`} aria-rowcount={rowCount + 1} aria-colcount={colCount + 1} aria-multiselectable="true"
                aria-activedescendant={`cell-${sel.r}-${sel.c}`} onScroll={onScroll} onKeyDown={onGridKey}
                onMouseDown={e => { if ((e.target as HTMLElement).closest('.cell-editor')) return; const p = cellFromPoint(e); if (editing) finishEdit(0, 0); if (e.shiftKey) setSel(p); else { setSel(p); setAnchor(p) } dragging.current = true; scroller.current?.focus(); e.preventDefault() }}
                onMouseMove={e => { if (dragging.current && e.buttons === 1) setSel(cellFromPoint(e)) }} onMouseUp={() => { dragging.current = false }}
                onDoubleClick={() => startEdit()}>
                <div className="sheet-sizer" style={{ width: lefts[colCount + 1], height: rowCount * ROW_H }}>
                    {cells}
                    <div className="sel-box" aria-hidden="true" style={{ left: lefts[box.c1], top: (box.r1 - 1) * ROW_H, width: lefts[box.c2 + 1] - lefts[box.c1], height: (box.r2 - box.r1 + 1) * ROW_H }} />
                    {editing && !editing.bar && <input ref={editor} className="cell-editor" aria-label={'Edit ' + address(sel.r, sel.c)} value={editing.text}
                        style={{ left: lefts[sel.c], top: (sel.r - 1) * ROW_H, minWidth: Math.max(widths[sel.c], 80), height: ROW_H }}
                        onChange={e => setEditing({ text: e.target.value, bar: false })}
                        onKeyDown={e => { e.stopPropagation(); if (e.key === 'Enter') { e.preventDefault(); finishEdit(e.shiftKey ? -1 : 1, 0) } else if (e.key === 'Tab') { e.preventDefault(); finishEdit(0, e.shiftKey ? -1 : 1) } else if (e.key === 'Escape') { e.preventDefault(); setEditing(null); scroller.current?.focus() } }}
                        onBlur={() => { if (editing) { if (commit(sel.r, sel.c, editing.text)) setEditing(null) } }} />}
                </div>
            </div>
        </div>
        <div className="sheet-footer">
            <div className="sheet-tabs" role="tablist" aria-label="Worksheets">{wb.worksheets.map((w: any, i: number) => <button key={w.id ?? i} role="tab" aria-selected={i === sheet} className={i === sheet ? 'on' : ''} onClick={() => { if (editing) finishEdit(0, 0); setSheet(i) }}>{w.name}{w.state !== 'visible' ? ' (hidden)' : ''}</button>)}</div>
            <div className="sheet-status" aria-live="polite">{status ? <span className="sheet-status-msg">{status}</span> : !single && numbersInSel.length ? <span>Sum {formatValue(sum)} · Average {formatValue(sum / numbersInSel.length)} · Count {filled}</span> : <span>{address(sel.r, sel.c)}{active?.formula ? ' · formula' : ''}{!editable ? ' · read-only' : ''}</span>}</div>
        </div>
        <p className="sr-only" aria-live="polite">{address(sel.r, sel.c)} {formatValue(scalarOf(active?.value), active?.numFmt)}</p>
    </div>;
}
