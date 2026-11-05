'use client';
// PDF viewer and annotator: PDF.js (Apache-2.0) renders pages, the selectable text layer and search;
// pdf-lib (MIT) writes the reviewer's annotations into a new copy of the file.
//
// This is annotation, not text editing: highlights, typed text boxes and comments are added on top
// of the pages. Highlights and text boxes are drawn into the saved page; comments are saved as
// standard PDF note annotations that Acrobat, Chrome, Edge and this viewer list as comments.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ChevronRight, Loader2, Search, X } from 'lucide-react';
import type { ViewerApi } from './viewer-api';

type PdfJs = typeof import('pdfjs-dist');
type Doc = import('pdfjs-dist').PDFDocumentProxy;
type Page = import('pdfjs-dist').PDFPageProxy;
type Rect = [number, number, number, number]; // PDF user space: x1, y1, x2, y2
type Tool = 'select' | 'highlight' | 'area' | 'comment' | 'text';
export type Annotation = { id: string; page: number; type: 'highlight' | 'comment' | 'text'; rects: Rect[]; text: string; at: string };
type Hit = { page: number; rects: Rect[] };
type FileNote = { page: number; type: string; author: string; text: string; rect: Rect };

const CSS_PER_PT = 96 / 72, ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const TOOLS: [Tool, string, string][] = [['select', 'Select', 'Select and copy text'], ['highlight', 'Highlight text', 'Select text to highlight it'], ['area', 'Highlight area', 'Drag a box over any part of a page (useful for scans)'], ['comment', 'Comment', 'Click on a page to pin a comment'], ['text', 'Text box', 'Click on a page to type a note on it']];
let pdfjsPromise: Promise<PdfJs> | null = null;
function loadPdfJs() {
    return pdfjsPromise ??= import('pdfjs-dist').then(m => { m.GlobalWorkerOptions.workerSrc = '/pdf.worker.js'; return m });
}
const uid = () => (crypto as any).randomUUID?.() ?? String(Date.now() + Math.random());
const norm = (r: number[]): Rect => [Math.min(r[0], r[2]), Math.min(r[1], r[3]), Math.max(r[0], r[2]), Math.max(r[1], r[3])];

/** Viewport rectangle (CSS px, top-left origin) of a PDF-space rectangle. */
function toView(viewport: any, r: Rect) {
    const [x1, y1] = viewport.convertToViewportPoint(r[0], r[1]), [x2, y2] = viewport.convertToViewportPoint(r[2], r[3]);
    return { left: Math.min(x1, x2), top: Math.min(y1, y2), width: Math.abs(x2 - x1), height: Math.abs(y2 - y1) };
}
function toPdf(viewport: any, x1: number, y1: number, x2: number, y2: number): Rect {
    return norm([...viewport.convertToPdfPoint(x1, y1), ...viewport.convertToPdfPoint(x2, y2)]);
}

// Page -------------------------------------------------------------------------------------------
type PageProps = {
    pdfjs: PdfJs; page: Page; index: number; scale: number; render: boolean; tool: Tool; annotations: Annotation[]; hits: Rect[]; activeHit: Rect | null;
    fileNotes: FileNote[]; selected: string | null; onSelect: (id: string | null) => void; onAdd: (a: Annotation) => void; onChange: (id: string, text: string) => void; onRemove: (id: string) => void;
};
const PdfPage = memo(function PdfPage({ pdfjs, page, index, scale, render, tool, annotations, hits, activeHit, fileNotes, selected, onSelect, onAdd, onChange, onRemove }: PageProps) {
    const canvas = useRef<HTMLCanvasElement>(null), text = useRef<HTMLDivElement>(null), [draft, setDraft] = useState<{ x: number; y: number; x2: number; y2: number } | null>(null);
    const viewport = useMemo(() => page.getViewport({ scale }), [page, scale]);
    useEffect(() => {
        if (!render || !canvas.current || !text.current) return;
        const el = canvas.current, dpr = Math.min(window.devicePixelRatio || 1, 2);
        el.width = Math.floor(viewport.width * dpr); el.height = Math.floor(viewport.height * dpr);
        el.style.width = Math.floor(viewport.width) + 'px'; el.style.height = Math.floor(viewport.height) + 'px';
        const task = page.render({ canvas: el, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined } as any);
        const layer = new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: text.current, viewport });
        text.current.replaceChildren();
        task.promise.catch(() => { }); layer.render().catch(() => { });
        return () => { task.cancel(); layer.cancel() };
    }, [render, viewport, page, pdfjs]);
    useEffect(() => { if (!render && canvas.current) { canvas.current.width = 0; canvas.current.height = 0 } }, [render]);

    const local = (e: React.PointerEvent | React.MouseEvent) => { const b = (e.currentTarget as HTMLElement).getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top } };
    const capture = tool === 'area' || tool === 'comment' || tool === 'text';
    function down(e: React.PointerEvent) {
        if (!capture || e.button !== 0 || (e.target as HTMLElement).closest('.pdf-annot')) return;
        const p = local(e);
        if (tool === 'area') { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); setDraft({ x: p.x, y: p.y, x2: p.x, y2: p.y }); return }
        const w = tool === 'text' ? 200 * scale / CSS_PER_PT : 22, h = tool === 'text' ? 48 * scale / CSS_PER_PT : 22;
        onAdd({ id: uid(), page: index, type: tool === 'text' ? 'text' : 'comment', rects: [toPdf(viewport, p.x, p.y, p.x + w, p.y + h)], text: '', at: new Date().toISOString() });
    }
    function moveDraft(e: React.PointerEvent) { if (draft) { const p = local(e); setDraft({ ...draft, x2: p.x, y2: p.y }) } }
    function up() {
        if (!draft) return;
        if (Math.abs(draft.x2 - draft.x) > 4 && Math.abs(draft.y2 - draft.y) > 4) onAdd({ id: uid(), page: index, type: 'highlight', rects: [toPdf(viewport, draft.x, draft.y, draft.x2, draft.y2)], text: '', at: new Date().toISOString() });
        setDraft(null);
    }
    return <div className={'pdf-page tool-' + tool} data-page={index} aria-label={`Page ${index + 1}`} role="region"
        style={{ width: Math.floor(viewport.width), height: Math.floor(viewport.height), ['--total-scale-factor' as any]: scale, ['--scale-round-x' as any]: '1px', ['--scale-round-y' as any]: '1px' }}>
        <canvas ref={canvas} aria-hidden="true" />
        <div ref={text} className="textLayer" />
        <div className={'pdf-overlay' + (capture ? ' capture' : '')} onPointerDown={down} onPointerMove={moveDraft} onPointerUp={up}>
            {hits.map((r, i) => <i key={'h' + i} className={'pdf-hit' + (activeHit === r ? ' current' : '')} style={toView(viewport, r)} />)}
            {fileNotes.map((n, i) => { const v = toView(viewport, n.rect); return <span key={'f' + i} className="pdf-file-note" style={{ left: v.left, top: v.top }} title={`${n.author ? n.author + ': ' : ''}${n.text}`} aria-label={`Existing ${n.type} comment: ${n.text}`} /> })}
            {annotations.map(a => {
                const on = selected === a.id;
                if (a.type === 'highlight') return a.rects.map((r, i) => <button type="button" key={a.id + i} className={'pdf-annot pdf-mark' + (on ? ' on' : '')} style={toView(viewport, r)} aria-label="Highlight" onClick={() => onSelect(on ? null : a.id)} />);
                const v = toView(viewport, a.rects[0]);
                if (a.type === 'comment') return <div key={a.id} className={'pdf-annot pdf-pin' + (on ? ' on' : '')} style={{ left: v.left, top: v.top }}>
                    <button type="button" className="pin" aria-label={'Comment: ' + (a.text || 'empty')} aria-expanded={on} onClick={() => onSelect(on ? null : a.id)}>!</button>
                    {on && <div className="pdf-popover" role="dialog" aria-label="Comment"><textarea autoFocus aria-label="Comment text" value={a.text} placeholder="Write a comment…" rows={3} onChange={e => onChange(a.id, e.target.value)} onKeyDown={e => { if (e.key === 'Escape' || ((e.metaKey || e.ctrlKey) && e.key === 'Enter')) { e.stopPropagation(); onSelect(null) } }} /><div><button type="button" className="text-btn" onClick={() => onRemove(a.id)}>Delete</button><button type="button" className="btn primary small" onClick={() => onSelect(null)}>Done</button></div></div>}
                </div>;
                return <div key={a.id} className={'pdf-annot pdf-textbox' + (on ? ' on' : '')} style={{ left: v.left, top: v.top, width: v.width, fontSize: 10 * scale }}>
                    {on ? <textarea autoFocus aria-label="Text box" value={a.text} placeholder="Type your note…" onChange={e => onChange(a.id, e.target.value)} onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); onSelect(null) } }} />
                        : <button type="button" onClick={() => onSelect(a.id)} aria-label={'Text box: ' + (a.text || 'empty')}>{a.text || 'Empty text box'}</button>}
                    {on && <button type="button" className="pdf-textbox-delete" aria-label="Delete text box" onClick={() => onRemove(a.id)}><X size={12} /></button>}
                </div>;
            })}
            {draft && <i className="pdf-draft" style={{ left: Math.min(draft.x, draft.x2), top: Math.min(draft.y, draft.y2), width: Math.abs(draft.x2 - draft.x), height: Math.abs(draft.y2 - draft.y) }} />}
        </div>
    </div>;
});

const Thumb = memo(function Thumb({ page, index, current, onPick }: { page: Page; index: number; current: boolean; onPick: (i: number) => void }) {
    const canvas = useRef<HTMLCanvasElement>(null), host = useRef<HTMLButtonElement>(null), [seen, setSeen] = useState(false);
    useEffect(() => { const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { setSeen(true); io.disconnect() } }, { rootMargin: '200px' }); if (host.current) io.observe(host.current); return () => io.disconnect() }, []);
    useEffect(() => {
        if (!seen || !canvas.current) return;
        const base = page.getViewport({ scale: 1 }), viewport = page.getViewport({ scale: 96 / base.width }), el = canvas.current;
        el.width = Math.floor(viewport.width); el.height = Math.floor(viewport.height);
        const task = page.render({ canvas: el, viewport } as any); task.promise.catch(() => { }); return () => task.cancel();
    }, [seen, page]);
    const base = page.getViewport({ scale: 1 });
    useEffect(() => { if (current) host.current?.scrollIntoView({ block: 'nearest' }) }, [current]);
    return <button type="button" ref={host} className={'pdf-thumb' + (current ? ' on' : '')} aria-label={`Go to page ${index + 1}`} aria-current={current ? 'page' : undefined} onClick={() => onPick(index)}>
        <canvas ref={canvas} style={{ aspectRatio: `${base.width} / ${base.height}` }} /><span>{index + 1}</span></button>;
});

// Viewer -----------------------------------------------------------------------------------------
type Props = { bytes: ArrayBuffer; editable: boolean; author: string; initialAnnotations?: Annotation[]; onDirty: (dirty: boolean) => void; register: (api: ViewerApi | null) => void };

export default function PdfViewer({ bytes, editable, author, initialAnnotations, onDirty, register }: Props) {
    const [pdfjs, setPdfjs] = useState<PdfJs | null>(null), [doc, setDoc] = useState<Doc | null>(null), [pages, setPages] = useState<Page[]>([]), [error, setError] = useState('');
    const [zoom, setZoom] = useState<number | 'fit'>('fit'), [fitScale, setFitScale] = useState(1), [current, setCurrent] = useState(0), [near, setNear] = useState<Set<number>>(new Set([0, 1]));
    const [thumbs, setThumbs] = useState(true), [tool, setTool] = useState<Tool>('select'), [annotations, setAnnotations] = useState<Annotation[]>(() => initialAnnotations ?? []), [selected, setSelected] = useState<string | null>(null);
    const [panel, setPanel] = useState(false), [query, setQuery] = useState(''), [hits, setHits] = useState<Hit[]>([]), [hitIndex, setHitIndex] = useState(0), [searching, setSearching] = useState(false), [pageInput, setPageInput] = useState('1');
    const [fileNotes, setFileNotes] = useState<FileNote[]>([]), [notice, setNotice] = useState('');
    const container = useRef<HTMLDivElement>(null), searchBox = useRef<HTMLInputElement>(null), textCache = useRef(new Map<number, { str: string; items: { start: number; end: number; item: any }[] }>());
    const scale = (zoom === 'fit' ? fitScale : zoom) * CSS_PER_PT;

    useEffect(() => {
        let gone = false, loading: any = null;
        (async () => {
            try {
                const lib = await loadPdfJs();
                // PDF.js takes ownership of the buffer it is given, so it gets a copy; the original bytes stay for saving.
                const task = loading = lib.getDocument({ data: new Uint8Array(bytes.slice(0)), enableXfa: false, isOffscreenCanvasSupported: false } as any);
                task.onPassword = () => { task.destroy(); if (!gone) setError('This PDF is password-protected and cannot be previewed here. Download it to open it with its password.') };
                const loaded: Doc = await task.promise; if (gone) return;
                const all = await Promise.all(Array.from({ length: loaded.numPages }, (_, i) => loaded.getPage(i + 1)));
                setPdfjs(lib); setDoc(loaded); setPages(all);
                const notes: FileNote[] = [];
                for (const [i, p] of all.entries()) for (const a of await p.getAnnotations()) {
                    const body = a.contentsObj?.str || a.contents || '';
                    if (body && a.subtype !== 'Popup' && a.subtype !== 'Link' && a.subtype !== 'Widget') notes.push({ page: i, type: a.subtype, author: a.titleObj?.str || a.title || '', text: body, rect: norm(a.rect) });
                }
                if (!gone) setFileNotes(notes);
            } catch (e: any) { if (!gone && !error) setError(e?.name === 'PasswordException' ? 'This PDF is password-protected and cannot be previewed here.' : 'This PDF could not be displayed: ' + (e?.message || 'unknown error') + '. Download it to open it elsewhere.') }
        })();
        return () => { gone = true; loading?.destroy() };
    }, [bytes]);

    // Fit to width, re-measured when the panel is resized.
    useEffect(() => {
        const el = container.current; if (!el || !pages[0]) return;
        const measure = () => { const w = pages[0].getViewport({ scale: 1 }).width * CSS_PER_PT; setFitScale(Math.max(0.25, Math.min(4, (el.clientWidth - 40) / w))) };
        measure(); const ro = new ResizeObserver(measure); ro.observe(el); return () => ro.disconnect();
    }, [pages]);
    // Which pages are near the viewport (only those keep a rendered canvas).
    useEffect(() => {
        const el = container.current; if (!el || !pages.length) return;
        const io = new IntersectionObserver(entries => setNear(old => { const next = new Set(old); for (const e of entries) { const i = Number((e.target as HTMLElement).dataset.page); e.isIntersecting ? next.add(i) : next.delete(i) } return next }), { root: el, rootMargin: '150% 0px' });
        el.querySelectorAll('.pdf-page').forEach(p => io.observe(p)); return () => io.disconnect();
    }, [pages, scale]);
    function onScroll() {
        const el = container.current!; const mid = el.scrollTop + el.clientHeight / 3;
        let i = 0; el.querySelectorAll<HTMLElement>('.pdf-page').forEach((p, n) => { if (p.offsetTop <= mid) i = n });
        if (i !== current) { setCurrent(i); setPageInput(String(i + 1)) }
    }
    const goTo = useCallback((i: number, offset = 0) => {
        const el = container.current, target = el?.querySelectorAll<HTMLElement>('.pdf-page')[i]; if (!el || !target) return;
        el.scrollTo({ top: target.offsetTop - 12 + offset }); setCurrent(i); setPageInput(String(i + 1));
    }, []);
    function setZoomKeepingPage(z: number | 'fit') { const page = current; setZoom(z); requestAnimationFrame(() => goTo(page)) }
    const zoomBy = (dir: 1 | -1) => { const now = zoom === 'fit' ? fitScale : zoom; const list = dir > 0 ? ZOOMS.filter(z => z > now + 0.01) : ZOOMS.filter(z => z < now - 0.01).reverse(); if (list[0]) setZoomKeepingPage(list[0]) };

    // Annotations --------------------------------------------------------------------------------
    useEffect(() => { onDirty(annotations.some(a => a.type === 'highlight' || a.text.trim())) }, [annotations]);
    const add = useCallback((a: Annotation) => { setAnnotations(list => [...list, a]); setSelected(a.type === 'highlight' ? null : a.id); if (a.type !== 'highlight') setTool('select') }, []);
    const change = useCallback((id: string, text: string) => setAnnotations(list => list.map(a => a.id === id ? { ...a, text } : a)), []);
    const remove = useCallback((id: string) => { setAnnotations(list => list.filter(a => a.id !== id)); setSelected(null) }, []);
    const select = useCallback((id: string | null) => { setSelected(id); setAnnotations(list => list.filter(a => a.type === 'highlight' || a.text.trim() || a.id === id)) }, []);
    function highlightSelection() {
        const sel = window.getSelection(), el = container.current; if (!sel || sel.isCollapsed || !el) return false;
        const pageEls = Array.from(el.querySelectorAll<HTMLElement>('.pdf-page')), byPage = new Map<number, Rect[]>();
        for (let r = 0; r < sel.rangeCount; r++) {
            const rects = Array.from(sel.getRangeAt(r).getClientRects()).filter(b => b.width > 1 && b.height > 1);
            const heights = rects.map(b => b.height).sort((a, b) => a - b), median = heights[heights.length >> 1] || 0;
            for (const b of rects) {
                if (b.height > median * 2.5) continue; // container boxes, not text runs
                const index = pageEls.findIndex(p => { const pb = p.getBoundingClientRect(); return b.top + b.height / 2 >= pb.top && b.top + b.height / 2 <= pb.bottom });
                if (index < 0) continue;
                const pb = pageEls[index].getBoundingClientRect(), viewport = pages[index].getViewport({ scale });
                const list = byPage.get(index) ?? []; list.push(toPdf(viewport, b.left - pb.left, b.top - pb.top, b.right - pb.left, b.bottom - pb.top)); byPage.set(index, list);
            }
        }
        sel.removeAllRanges();
        byPage.forEach((rects, page) => add({ id: uid(), page, type: 'highlight', rects, text: '', at: new Date().toISOString() }));
        return byPage.size > 0;
    }

    // Search -------------------------------------------------------------------------------------
    async function pageText(i: number) {
        let t = textCache.current.get(i);
        if (!t) {
            const content = await pages[i].getTextContent(); let str = ''; const items: { start: number; end: number; item: any }[] = [];
            for (const item of content.items as any[]) { if (typeof item.str !== 'string') continue; items.push({ start: str.length, end: str.length + item.str.length, item }); str += item.str + (item.hasEOL ? ' ' : '') }
            t = { str: str.toLowerCase(), items }; textCache.current.set(i, t);
        }
        return t;
    }
    async function runSearch(q: string) {
        const needle = q.trim().toLowerCase(); setHits([]); setHitIndex(0);
        if (!needle || !pages.length) return;
        setSearching(true);
        const found: Hit[] = [];
        for (let i = 0; i < pages.length; i++) {
            const { str, items } = await pageText(i);
            for (let at = str.indexOf(needle); at >= 0; at = str.indexOf(needle, at + needle.length)) {
                const end = at + needle.length, rects: Rect[] = [];
                for (const { start, end: stop, item } of items) {
                    if (stop <= at || start >= end || !item.str.length) continue;
                    const [, , c, d, e, f] = item.transform, size = Math.hypot(c, d) || item.height || 10, len = item.str.length;
                    const from = Math.max(0, at - start) / len, to = Math.min(len, end - start) / len;
                    rects.push(norm([e + item.width * from, f - size * 0.22, e + item.width * to, f + size * 0.9]));
                }
                if (rects.length) found.push({ page: i, rects });
            }
        }
        setHits(found); setSearching(false);
        if (found.length) jumpToHit(found, 0);
    }
    function jumpToHit(list: Hit[], i: number) {
        const hit = list[i]; if (!hit) return; setHitIndex(i);
        const top = toView(pages[hit.page].getViewport({ scale }), hit.rects[0]).top;
        goTo(hit.page, Math.max(0, top - 80));
    }

    // Saving: the annotations are written into a copy of the original bytes ---------------------
    const exportFile = useCallback(async () => {
        const { PDFDocument, StandardFonts, rgb, PDFName, PDFHexString, PDFString } = await import('pdf-lib');
        let out;
        try { out = await PDFDocument.load(new Uint8Array(bytes), { updateMetadata: false }) }
        catch (e: any) { throw new Error(/encrypt/i.test(e?.message || '') ? 'This PDF is encrypted, so annotations cannot be saved into it. Download it, or ask the client for an unprotected copy.' : 'This PDF could not be prepared for saving: ' + (e?.message || 'unknown error')) }
        const font = await out.embedFont(StandardFonts.Helvetica), outPages = out.getPages();
        const latin = (s: string) => [...s].map(ch => { try { font.encodeText(ch); return ch } catch { return '?' } }).join('');
        const noteIcon = out.context.register(out.context.stream('1 0.82 0.25 rg 0.55 0.4 0 RG 1 w 0.5 0.5 17 17 re B 0.4 0.3 0 RG 1.2 w 4 12.5 m 14 12.5 l 4 9 m 14 9 l 4 5.5 m 11 5.5 l S', { Type: 'XObject', Subtype: 'Form', BBox: [0, 0, 18, 18] }));
        for (const a of annotations) {
            const page = outPages[a.page]; if (!page) continue;
            if (a.type === 'highlight') for (const [x1, y1, x2, y2] of a.rects) page.drawRectangle({ x: x1, y: y1, width: x2 - x1, height: y2 - y1, color: rgb(1, 0.86, 0.2), opacity: 0.38 });
            if (a.type === 'text' && a.text.trim()) {
                const [x1, , x2, y2] = a.rects[0], size = 10, pad = 4, width = x2 - x1, lines: string[] = [];
                for (const para of latin(a.text).split('\n')) {
                    let line = '';
                    for (const word of para.split(' ')) { const next = line ? line + ' ' + word : word; if (line && font.widthOfTextAtSize(next, size) > width - pad * 2) { lines.push(line); line = word } else line = next }
                    lines.push(line);
                }
                const height = lines.length * size * 1.25 + pad * 2;
                page.drawRectangle({ x: x1, y: y2 - height, width, height, color: rgb(1, 0.98, 0.84), opacity: 0.95, borderColor: rgb(0.85, 0.6, 0.1), borderWidth: 0.8 });
                lines.forEach((l, i) => page.drawText(l, { x: x1 + pad, y: y2 - pad - size * (i + 1) * 1.25 + size * 0.25, size, font, color: rgb(0.08, 0.2, 0.45) }));
            }
            if (a.type === 'comment' && a.text.trim()) {
                const [x1, , , y2] = a.rects[0];
                const annot = out.context.obj({
                    Type: 'Annot', Subtype: 'Text', Rect: [x1, y2 - 18, x1 + 18, y2], Contents: PDFHexString.fromText(a.text.trim()), T: PDFHexString.fromText(author),
                    Name: 'Comment', C: [1, 0.82, 0.25], F: 4, M: PDFString.fromDate(new Date(a.at)), Open: false, AP: { N: noteIcon },
                });
                page.node.addAnnot(out.context.register(annot));
            }
        }
        void PDFName;
        return new Uint8Array(await out.save());
    }, [annotations, bytes, author]);
    useEffect(() => { register({ exportFile, draft: async () => annotations.filter(a => a.type === 'highlight' || a.text.trim()) }); return () => register(null) }, [exportFile]);

    if (error) return <div className="viewer-message" role="alert"><AlertCircle size={22} /><p>{error}</p></div>;
    if (!doc || !pdfjs) return <div className="viewer-message" aria-busy="true"><Loader2 size={22} className="spin" /><p>Opening PDF…</p></div>;
    const zoomLabel = Math.round((zoom === 'fit' ? fitScale : zoom) * 100) + '%', active = hits[hitIndex];
    const listed = annotations.filter(a => a.type === 'highlight' || a.text.trim());
    return <div className="pdf-viewer" onKeyDown={e => { if (editable && (e.metaKey || e.ctrlKey) && e.key === 'z' && !(e.target as HTMLElement).closest('textarea,input')) { e.preventDefault(); setAnnotations(list => list.slice(0, -1)); setSelected(null) } }}>
        <div className="pdf-toolbar" role="toolbar" aria-label="PDF controls">
            <button type="button" className={'tool' + (thumbs ? ' on' : '')} aria-pressed={thumbs} onClick={() => setThumbs(!thumbs)} title="Page thumbnails" aria-label="Show page thumbnails"><span className="thumbs-icon"><i /><i /></span></button>
            <span className="tool-sep" />
            <button type="button" className="tool" aria-label="Previous page" disabled={current === 0} onClick={() => goTo(current - 1)}><ChevronRight size={16} className="flip" /></button>
            <label className="page-box"><span className="sr-only">Page number</span><input aria-label="Page number" inputMode="numeric" value={pageInput} onChange={e => setPageInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { const n = Number(pageInput); if (n >= 1 && n <= pages.length) goTo(n - 1); else setPageInput(String(current + 1)) } }} onBlur={() => setPageInput(String(current + 1))} /><span>/ {pages.length}</span></label>
            <button type="button" className="tool" aria-label="Next page" disabled={current >= pages.length - 1} onClick={() => goTo(current + 1)}><ChevronRight size={16} /></button>
            <span className="tool-sep" />
            <button type="button" className="tool" aria-label="Zoom out" onClick={() => zoomBy(-1)}>−</button>
            <select className="zoom-select" aria-label="Zoom" value={zoom === 'fit' ? 'fit' : String(zoom)} onChange={e => setZoomKeepingPage(e.target.value === 'fit' ? 'fit' : Number(e.target.value))}><option value="fit">Fit width ({Math.round(fitScale * 100)}%)</option>{ZOOMS.map(z => <option key={z} value={z}>{Math.round(z * 100)}%</option>)}</select>
            <button type="button" className="tool" aria-label="Zoom in" onClick={() => zoomBy(1)}>+</button>
            <span className="sr-only" aria-live="polite">Zoom {zoomLabel}</span>
            <span className="tool-sep" />
            <form className="pdf-search" role="search" onSubmit={e => { e.preventDefault(); if (hits.length && query.trim().toLowerCase() === (searchBox.current?.dataset.last || '')) jumpToHit(hits, (hitIndex + 1) % hits.length); else { if (searchBox.current) searchBox.current.dataset.last = query.trim().toLowerCase(); runSearch(query) } }}>
                <Search size={14} aria-hidden="true" /><input ref={searchBox} aria-label="Search document text" placeholder="Search text" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Escape' && query) { e.stopPropagation(); setQuery(''); setHits([]) } }} />
                <span className="pdf-search-count" aria-live="polite">{searching ? 'Searching…' : query && searchBox.current?.dataset.last === query.trim().toLowerCase() ? (hits.length ? `${hitIndex + 1} of ${hits.length}` : 'No matches') : ''}</span>
                <button type="button" className="tool" aria-label="Previous match" disabled={!hits.length} onClick={() => jumpToHit(hits, (hitIndex - 1 + hits.length) % hits.length)}><ChevronRight size={14} className="flip" /></button>
                <button type="button" className="tool" aria-label="Next match" disabled={!hits.length} onClick={() => jumpToHit(hits, (hitIndex + 1) % hits.length)}><ChevronRight size={14} /></button>
            </form>
        </div>
        {editable && <div className="pdf-toolbar annotate" role="toolbar" aria-label="Annotation tools">
            <span className="toolbar-label">Annotate</span>
            {TOOLS.map(([t, label, hint]) => <button type="button" key={t} className={'tool text' + (tool === t ? ' on' : '')} aria-pressed={tool === t} title={hint} onClick={() => { if (t === 'highlight' && highlightSelection()) { setTool('select'); return } setTool(t); setNotice(t === 'highlight' ? 'Select text on a page; it is highlighted when you release the mouse.' : '') }}>{label}</button>)}
            <span className="tool-grow" />
            <button type="button" className={'tool text' + (panel ? ' on' : '')} aria-pressed={panel} onClick={() => setPanel(!panel)}>Annotations ({listed.length + fileNotes.length})</button>
        </div>}
        {!editable && fileNotes.length > 0 && <div className="pdf-toolbar annotate"><span className="tool-grow" /><button type="button" className={'tool text' + (panel ? ' on' : '')} aria-pressed={panel} onClick={() => setPanel(!panel)}>Comments in this file ({fileNotes.length})</button></div>}
        {notice && tool === 'highlight' && <div className="viewer-warning" role="status">{notice}</div>}
        <div className="pdf-body">
            {thumbs && <nav className="pdf-thumbs" aria-label="Page thumbnails">{pages.map((p, i) => <Thumb key={i} page={p} index={i} current={i === current} onPick={goTo} />)}</nav>}
            <div ref={container} className="pdf-pages" tabIndex={0} aria-label="Document pages" onScroll={onScroll}
                onMouseUp={() => { if (tool === 'highlight') highlightSelection() }}
                onKeyDown={e => {
                    if ((e.metaKey || e.ctrlKey) && e.key === 'f') { e.preventDefault(); searchBox.current?.focus() }
                    if ((e.metaKey || e.ctrlKey) && (e.key === '=' || e.key === '+')) { e.preventDefault(); zoomBy(1) }
                    if ((e.metaKey || e.ctrlKey) && e.key === '-') { e.preventDefault(); zoomBy(-1) }
                    if (e.key === 'Escape' && (selected || tool !== 'select')) { e.stopPropagation(); select(null); setTool('select') }
                    if ((e.key === 'Delete' || e.key === 'Backspace') && selected && !(e.target as HTMLElement).closest('textarea')) { e.preventDefault(); remove(selected) }
                }}>
                {pages.map((p, i) => <PdfPage key={i} pdfjs={pdfjs} page={p} index={i} scale={scale} render={near.has(i)} tool={editable ? tool : 'select'}
                    annotations={annotations.filter(a => a.page === i)} hits={hits.filter(h => h.page === i).flatMap(h => h.rects)} activeHit={active?.page === i ? active.rects[0] : null}
                    fileNotes={fileNotes.filter(n => n.page === i)} selected={selected} onSelect={select} onAdd={add} onChange={change} onRemove={remove} />)}
            </div>
            {panel && <aside className="pdf-annots" aria-label="Annotations">
                <header><h3>Annotations</h3><button type="button" className="icon-btn" aria-label="Close annotations" onClick={() => setPanel(false)}><X size={16} /></button></header>
                {editable && <p className="muted">New in this session · saved when you save a version.</p>}
                {listed.map(a => <div key={a.id} className={'annot-row' + (selected === a.id ? ' on' : '')}><button type="button" onClick={() => { const top = toView(pages[a.page].getViewport({ scale }), a.rects[0]).top; goTo(a.page, Math.max(0, top - 80)); if (a.type !== 'highlight') setSelected(a.id) }}><strong>{a.type === 'highlight' ? 'Highlight' : a.type === 'comment' ? 'Comment' : 'Text box'} · page {a.page + 1}</strong>{a.text && <span>{a.text}</span>}</button><button type="button" className="icon-btn" aria-label="Delete annotation" onClick={() => remove(a.id)}><X size={14} /></button></div>)}
                {editable && !listed.length && <p className="muted">No new annotations yet. Choose a tool above.</p>}
                {fileNotes.length > 0 && <><h4>Already in this file</h4>{fileNotes.map((n, i) => <div key={i} className="annot-row file"><button type="button" onClick={() => goTo(n.page)}><strong>{n.type} · page {n.page + 1}{n.author ? ' · ' + n.author : ''}</strong><span>{n.text}</span></button></div>)}</>}
            </aside>}
        </div>
        {editable && <p className="pdf-footnote">Annotations are added on top of the pages and saved as a new PDF version. The PDF's own text cannot be edited here.</p>}
    </div>;
}
