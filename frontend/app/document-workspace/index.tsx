'use client';
// Document review workspace: the document on the left, the request on the right.
//
// The viewer is chosen from the file's content (detect.ts). Editable formats (PDF annotations,
// XLSX/CSV cells) are saved with "Save as new version": the edited copy is uploaded to
// POST /api/v2-files/revision and becomes the next version; the original upload is never changed.
// Saving a version and submitting the request (right panel) are separate actions.
// While editing, the work in progress is auto-saved every few seconds as a draft in this browser
// (drafts.ts) and restored when the same person reopens the same version.
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, Download, FileText, Loader2, X } from 'lucide-react';
import { detectKind, type ViewerKind } from './detect';
import type { ViewerApi } from './viewer-api';
import { deleteDraft, draftKey, getDraft, pruneDrafts, putDraft, type Draft } from './drafts';

const PdfViewer = lazy(() => import('./pdf-viewer'));
const SheetEditor = lazy(() => import('./sheet-editor'));
const DocxPreview = lazy(() => import('./docx-preview'));

export type WorkspaceDocument = { id: string; name: string };
type Version = { id: string; name: string; version: number; size: number; hash: string; shared: boolean; current: boolean; original: boolean; uploadedAt?: string; uploadedBy: string; editKind?: string; editNote?: string; category?: string; period?: string; revision: number };
type History = { document: string; latest: string; versions: Version[]; request: { id: string; title: string; status: string } | null; editable: boolean; editBlocked: string | null };
type Props = {
    title: string; subtitle?: string; documents: WorkspaceDocument[]; initial?: string; actorId: string; actorName: string; staff: boolean;
    onClose: () => void; onSaved?: (id: string) => void; children: ReactNode; emptyTitle?: string; emptyBody?: string;
};

const SPLIT_KEY = 'gov-review-split', MIN_SIDE = 320, AUTOSAVE_MS = 4000;
const clock = (v: string) => new Date(v).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const readSplit = () => { try { return Number(localStorage.getItem(SPLIT_KEY)) || 440 } catch { return 440 } };
const when = (v?: string) => v ? new Date(v).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'date not recorded';
const LABEL: Record<ViewerKind, string> = { pdf: 'PDF', xlsx: 'Excel workbook', csv: 'CSV spreadsheet', docx: 'Word document', text: 'Text file', xls: 'Legacy Excel (.xls)', unsupported: 'Unsupported format' };

export default function DocumentWorkspace({ title, subtitle, documents, initial, actorId, actorName, staff, onClose, onSaved, children, emptyTitle, emptyBody }: Props) {
    const [docId, setDocId] = useState(initial && documents.some(d => d.id === initial) ? initial : documents[0]?.id || ''), [history, setHistory] = useState<History | null>(null), [viewId, setViewId] = useState('');
    const [bytes, setBytes] = useState<ArrayBuffer | null>(null), [kind, setKind] = useState<ViewerKind | null>(null), [loading, setLoading] = useState(false), [error, setError] = useState('');
    const [dirty, setDirty] = useState(false), [saving, setSaving] = useState(false), [saveForm, setSaveForm] = useState<{ note: string; shared: boolean } | null>(null), [saved, setSaved] = useState(''), [saveError, setSaveError] = useState('');
    const [side, setSide] = useState(readSplit), [reload, setReload] = useState(0);
    const root = useRef<HTMLDivElement>(null), body = useRef<HTMLDivElement>(null), api = useRef<ViewerApi | null>(null);
    // Drafts: `seq` counts edits, `savedSeq` the edit count last written to the draft store.
    const [restored, setRestored] = useState<Draft | null>(null), [draftReady, setDraftReady] = useState(false), [draftAt, setDraftAt] = useState(''), [viewerKey, setViewerKey] = useState(0);
    const seq = useRef(0), savedSeq = useRef(0), live = useRef<{ key: string; hash: string; kind: string; editable: boolean }>({ key: '', hash: '', kind: '', editable: false });
    useEffect(() => { pruneDrafts() }, []);

    // A newly uploaded document opens straight away (unless edits to the open one are unsaved).
    const known = useRef(new Set(documents.map(d => d.id)));
    useEffect(() => {
        const added = documents.filter(d => !known.current.has(d.id));
        documents.forEach(d => known.current.add(d.id));
        if (added.length && !dirty) setDocId(added[added.length - 1].id);
        else if (!documents.some(d => d.id === docId)) setDocId(documents[0]?.id || '');
    }, [documents.map(d => d.id).join()]);
    useEffect(() => { // version history of the chosen document
        if (!docId) { setHistory(null); setViewId(''); return }
        let gone = false; setError(''); setLoading(true);
        fetch('/api/v2-files/versions?id=' + encodeURIComponent(docId)).then(async r => {
            const d: any = await r.json().catch(() => ({}));
            // A server without the version-history endpoint answers with the framework's generic 404
            // ("Not Found"; a missing document is "Document not found."). The file can still be
            // previewed, read-only, from the existing download endpoint.
            if (r.status === 404 && d.error === 'Not Found') {
                const name = documents.find(x => x.id === docId)?.name || 'document';
                return { document: docId, latest: docId, request: null, editable: false, editBlocked: 'Preview only: the server does not support saving versions yet. Deploy the updated backend to enable editing and version history.',
                    versions: [{ id: docId, name, version: 1, size: 0, hash: '', shared: true, current: true, original: true, uploadedBy: '', revision: 0 }] } as History;
            }
            if (!r.ok) throw new Error(d.error || 'Unable to load the document.');
            return d as History;
        })
            .then(h => { if (!gone) { setHistory(h); setViewId(h.latest) } }).catch(e => { if (!gone) { setError(e.message); setLoading(false) } });
        return () => { gone = true };
    }, [docId, reload]);
    useEffect(() => { // bytes of the version being viewed
        if (!viewId) return;
        let gone = false; setLoading(true); setBytes(null); setKind(null); setError(''); setDirty(false); setRestored(null); setDraftReady(false); setDraftAt(''); seq.current = savedSeq.current = 0;
        fetch('/api/v2-files?id=' + encodeURIComponent(viewId)).then(async r => { if (!r.ok) { const d: any = await r.json().catch(() => ({})); throw new Error(d.error || 'Unable to download the document.') } return r.arrayBuffer() })
            .then(b => { if (!gone) { const name = history?.versions.find(v => v.id === viewId)?.name || ''; setKind(detectKind(name, new Uint8Array(b))); setBytes(b) } })
            .catch(e => { if (!gone) setError(e.message) }).finally(() => { if (!gone) setLoading(false) });
        return () => { gone = true };
    }, [viewId]);
    useEffect(() => { // browser-level warning, only for edits not yet in the auto-saved draft
        if (!dirty) return; const warn = (e: BeforeUnloadEvent) => { if (seq.current !== savedSeq.current) { e.preventDefault(); e.returnValue = '' } };
        window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
    }, [dirty]);
    useEffect(() => { const p = document.activeElement as HTMLElement | null; requestAnimationFrame(() => root.current?.querySelector<HTMLElement>('.review-close')?.focus()); return () => p?.focus?.() }, []);
    useEffect(() => { if (!saved) return; const t = setTimeout(() => setSaved(''), 6000); return () => clearTimeout(t) }, [saved]);

    const version = history?.versions.find(v => v.id === viewId), latest = history?.latest === viewId;
    const editableKind = kind === 'pdf' || kind === 'xlsx' || kind === 'csv';
    const editable = !!history?.editable && latest && editableKind;
    live.current = { key: viewId ? draftKey(actorId, viewId) : '', hash: version?.hash || '', kind: kind || '', editable: editable && !!version?.hash };

    useEffect(() => { // restore an auto-saved draft of this exact version, if there is one
        if (!bytes || !kind) return;
        const { key, hash, editable: canEdit } = live.current;
        if (!canEdit) { setDraftReady(true); return }
        let gone = false;
        getDraft(key).then(d => {
            if (gone) return;
            if (d && d.base === hash && d.kind === kind) { setRestored(d); setDraftAt(d.savedAt); setDirty(true); seq.current = savedSeq.current = 1 }
            setDraftReady(true);
        });
        return () => { gone = true };
    }, [bytes, kind]);
    /** Writes the current edits to the draft store if they changed since the last write. */
    const flushDraft = useCallback(async () => {
        const { key, hash, kind: k, editable: canEdit } = live.current;
        if (!canEdit || !api.current || seq.current === savedSeq.current) return;
        const at = seq.current;
        try {
            const data = await api.current.draft(), savedAt = new Date().toISOString();
            await putDraft(key, { base: hash, kind: k, savedAt, data });
            savedSeq.current = at; setDraftAt(savedAt);
        } catch { /* drafts are best effort */ }
    }, []);
    useEffect(() => {
        if (!editable) return;
        const timer = setInterval(flushDraft, AUTOSAVE_MS), hidden = () => { if (document.visibilityState === 'hidden') flushDraft() };
        document.addEventListener('visibilitychange', hidden);
        return () => { clearInterval(timer); document.removeEventListener('visibilitychange', hidden) };
    }, [editable, flushDraft]);
    const markDirty = useCallback((d: boolean) => {
        setDirty(d);
        if (d) seq.current++;
        else if (seq.current) { seq.current = savedSeq.current = 0; setDraftAt(''); if (live.current.key) deleteDraft(live.current.key) }
    }, []);
    function discardDraft() {
        if (!window.confirm('Discard the auto-saved changes and go back to the saved version?')) return;
        deleteDraft(live.current.key); setRestored(null); setDirty(false); setDraftAt(''); seq.current = savedSeq.current = 0; setViewerKey(n => n + 1);
    }
    // Leaving never loses edits: they are written to the draft first and restored on return.
    const close = async () => { if (saving) return; await flushDraft(); onClose() };
    const readOnlyReason = !history || !kind ? '' : !editableKind ? '' : !latest ? 'You are viewing an earlier version. Open the latest version to make changes.' : history.editBlocked || '';

    async function save() {
        if (!api.current || !version || !saveForm) return;
        setSaving(true); setSaveError('');
        try {
            const content = await api.current.exportFile(), form = new FormData();
            form.set('id', version.id); form.set('revision', String(version.revision)); form.set('note', saveForm.note); form.set('shared', saveForm.shared ? 'true' : 'false');
            form.set('file', new File([content as BlobPart], version.name, { type: 'application/octet-stream' }));
            const r = await fetch('/api/v2-files/revision', { method: 'POST', body: form }), d: any = await r.json();
            if (!r.ok) throw new Error(d.error || 'The version could not be saved.');
            deleteDraft(live.current.key); seq.current = savedSeq.current = 0; setRestored(null); setDraftAt('');
            setDirty(false); setSaveForm(null); setSaved(`Saved as version ${d.version}. The original upload is unchanged.`); setReload(n => n + 1); onSaved?.(d.id);
        } catch (e: any) { setSaveError(e.message) } finally { setSaving(false) }
    }
    async function pickDoc(id: string) { if (id !== docId) { await flushDraft(); setDocId(id); setSaveForm(null) } }
    async function pickVersion(id: string) { if (id !== viewId) { await flushDraft(); setViewId(id); setSaveForm(null) } }
    function onKey(e: React.KeyboardEvent) {
        // The workspace sits over the page: its keys never reach the page's own shortcuts.
        e.stopPropagation();
        if (e.key === 'Escape') { e.preventDefault(); if (saveForm) setSaveForm(null); else close(); return }
        if (e.key === 'Tab' && root.current) {
            const els = Array.from(root.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]')).filter(el => el.offsetParent !== null);
            const first = els[0], last = els[els.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
        }
    }
    function resizeTo(px: number) { const max = Math.max(MIN_SIDE, (root.current?.clientWidth || 1200) * 0.6), next = Math.round(Math.min(max, Math.max(MIN_SIDE, px))); setSide(next); try { localStorage.setItem(SPLIT_KEY, String(next)) } catch { } }
    function drag(e: React.PointerEvent) {
        e.preventDefault(); const startX = e.clientX, start = side;
        const move = (ev: PointerEvent) => resizeTo(start - (ev.clientX - startX)), up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); document.body.classList.remove('col-resizing') };
        document.body.classList.add('col-resizing'); window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
    }

    const vkey = viewId + ':' + viewerKey, fromDraft = restored?.data;
    const viewer = !bytes || !kind || !version || !draftReady ? null
        : kind === 'pdf' ? <PdfViewer key={vkey} bytes={bytes} editable={editable} author={actorName} initialAnnotations={Array.isArray(fromDraft) ? fromDraft : undefined} onDirty={markDirty} register={a => { api.current = a }} />
            : kind === 'xlsx' || kind === 'csv' ? <SheetEditor key={vkey} bytes={bytes} draftBytes={fromDraft instanceof ArrayBuffer ? fromDraft : undefined} name={version.name} kind={kind} editable={editable} onDirty={markDirty} register={a => { api.current = a }} />
                : kind === 'docx' ? <DocxPreview key={viewId} bytes={bytes} />
                    : kind === 'text' ? <pre className="text-viewer" tabIndex={0} aria-label="Document text">{new TextDecoder().decode(bytes)}</pre>
                        : <div className="viewer-message"><AlertCircle size={22} /><h3>{kind === 'xls' ? 'Legacy Excel files cannot be opened here' : 'This file type cannot be previewed'}</h3><p>{kind === 'xls' ? 'The browser spreadsheet editor reads .xlsx and .csv files. Download this file to open it in Excel, or ask for an .xlsx copy.' : 'Download the file to open it in its own application.'}</p><a className="btn secondary" href={'/api/v2-files?id=' + viewId}><Download size={15} />Download file</a></div>;

    return <div className="review-overlay" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) close() }}>
        <div className="review-workspace" ref={root} role="dialog" aria-modal="true" aria-labelledby="review-title" onKeyDown={onKey} style={{ ['--review-side' as any]: side + 'px' }}>
            <header className="review-head">
                <div className="review-title"><span className="overline">DOCUMENT REVIEW</span><h2 id="review-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
                <div className="review-actions">
                    {dirty && <span className="pill warning" role="status" title="Auto-saved as a draft in this browser. Save as new version to keep it on the server."><i />Not saved as a version{draftAt ? ' · draft auto-saved ' + clock(draftAt) : ''}</span>}
                    {saved && <span className="pill success" role="status"><CheckCircle2 size={13} />{saved}</span>}
                    {editable && <button type="button" className="btn primary" disabled={!dirty || saving} onClick={() => { setSaveForm({ note: '', shared: false }); setSaveError('') }}>Save as new version</button>}
                    {viewId && <a className="btn secondary" href={'/api/v2-files?id=' + viewId}><Download size={15} />Download</a>}
                    <button type="button" className="icon-btn review-close" aria-label="Close review workspace" disabled={saving} onClick={close}><X size={22} /></button>
                </div>
            </header>
            <div className="review-split">
                <section className="review-doc" aria-label="Document" ref={body}>
                    {documents.length > 0 && <div className="review-docbar">
                        {documents.length > 1 ? <label className="review-pick"><span>Document</span><select value={docId} onChange={e => pickDoc(e.target.value)}>{documents.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
                            : <span className="review-docname"><FileText size={16} />{documents[0].name}</span>}
                        {history && history.versions.length > 0 && <label className="review-pick"><span>Version</span><select value={viewId} onChange={e => pickVersion(e.target.value)}>{[...history.versions].reverse().map(v => <option key={v.id} value={v.id}>v{v.version} · {v.original ? 'Original upload' : v.editKind || 'New version'} {v.uploadedBy ? ' · ' + v.uploadedBy : ''}{staff && !v.shared ? ' · internal' : ''}{v.id === history.latest ? ' · latest' : ''}</option>)}</select></label>}
                        {version && <span className="review-meta">{kind ? LABEL[kind] : ''}{version.size ? ' · ' + (version.size / 1024).toFixed(1) + ' KB' : ''}{version.category ? ' · ' + version.category : ''}{version.period ? ' · ' + version.period : ''}{version.uploadedAt ? ' · ' + when(version.uploadedAt) : ''}</span>}
                    </div>}
                    {version?.editNote && <div className="review-note"><strong>Version note:</strong> {version.editNote}</div>}
                    {readOnlyReason && <div className="viewer-warning" role="note"><AlertCircle size={15} />{readOnlyReason}</div>}
                    {restored && <div className="review-draft" role="status"><span><strong>Your unsaved changes were restored</strong> (auto-saved on this device {new Date(restored.savedAt).toLocaleString()}). Use <em>Save as new version</em> to keep them on the server.</span><button type="button" className="text-btn" onClick={discardDraft}>Discard draft</button></div>}
                    {saveForm && <div className="review-save" role="group" aria-labelledby="save-version-title">
                        <h3 id="save-version-title">Save as version {(history?.versions.at(-1)?.version || 1) + 1}</h3>
                        <p className="muted">Your changes become a new version of “{version?.name}”. Earlier versions, including the original upload, stay exactly as they are. Saving does not submit the request.</p>
                        <label className="field"><span>What changed? (optional)</span><textarea rows={2} maxLength={2000} autoFocus value={saveForm.note} onChange={e => setSaveForm({ ...saveForm, note: e.target.value })} placeholder="e.g. Highlighted the closing balance and noted the missing page" /></label>
                        {staff && <label className="check-row"><input type="checkbox" checked={saveForm.shared} onChange={e => setSaveForm({ ...saveForm, shared: e.target.checked })} />Share this version with the client</label>}
                        {saveError && <div className="error-banner" role="alert">{saveError}</div>}
                        <div className="action-wrap"><button type="button" className="btn secondary" disabled={saving} onClick={() => setSaveForm(null)}>Cancel</button><button type="button" className="btn primary" disabled={saving} aria-busy={saving} onClick={save}>{saving ? <><Loader2 size={15} className="spin" />Saving…</> : 'Save version'}</button></div>
                    </div>}
                    <div className="review-viewer">
                        {!documents.length ? <div className="viewer-message"><FileText size={26} /><h3>{emptyTitle || 'No document uploaded yet'}</h3><p>{emptyBody || 'Documents uploaded for this request appear here for preview and review.'}</p></div>
                            : error ? <div className="viewer-message" role="alert"><AlertCircle size={22} /><p>{error}</p><button type="button" className="btn secondary" onClick={() => setReload(n => n + 1)}>Try again</button></div>
                                : loading || !viewer ? <div className="viewer-message" aria-busy="true"><Loader2 size={22} className="spin" /><p>Loading document…</p></div>
                                    : <Suspense fallback={<div className="viewer-message" aria-busy="true"><Loader2 size={22} className="spin" /><p>Loading viewer…</p></div>}>{viewer}</Suspense>}
                    </div>
                </section>
                <div className="review-divider" role="separator" aria-orientation="vertical" aria-label="Resize panels" aria-valuenow={side} aria-valuemin={MIN_SIDE} tabIndex={0}
                    onPointerDown={drag} onKeyDown={e => { if (e.key === 'ArrowLeft') { e.preventDefault(); resizeTo(side + 32) } if (e.key === 'ArrowRight') { e.preventDefault(); resizeTo(side - 32) } }} />
                <aside className="review-side" aria-label="Request details">{children}</aside>
            </div>
        </div>
    </div>;
}
