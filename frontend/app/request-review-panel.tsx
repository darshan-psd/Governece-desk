'use client';
// Right-hand panel of the document review workspace for staff (internal "Client requests" view).
// It keeps the request drawer's existing actions — respond, accept and close, request correction —
// and adds the reviewer's clarification notes, which are saved on their own ("Save notes") without
// changing the request's status. Saving a document version happens in the left panel.
import { useEffect, useState } from 'react';
import { Download, FileText } from 'lucide-react';
import { RichText, RichTextEditor } from './rich-text';

type R = { id: string; kind: string; revision: number; [key: string]: any };
type Props = {
    request: R; docs: R[]; role: string; canEdit: boolean; busy: boolean; error: string; company: string; member: (id: string) => string;
    call: (body: any, keep?: boolean) => Promise<any>;
};

export default function RequestReviewPanel({ request: r, docs, role, canEdit, busy, error, company, member, call }: Props) {
    const [notes, setNotes] = useState(r.clarification || ''), [response, setResponse] = useState(''), [reviewNote, setReviewNote] = useState(''), [savedNotes, setSavedNotes] = useState(r.clarification || '');
    useEffect(() => { setNotes(r.clarification || ''); setSavedNotes(r.clarification || '') }, [r.id, r.clarification]);
    const evidence = (r.evidence || []).map((id: string) => docs.find(d => d.id === id)).filter(Boolean) as R[];
    const notesChanged = notes.trim() !== (savedNotes || '').trim(), closed = r.status === 'Closed';
    const field = (label: string, value: any) => <div><dt>{label}</dt><dd>{value || '—'}</dd></div>;
    return <div className="review-panel">
        <div className="drawer-content">
            {error && <div className="error-banner" role="alert">{error}</div>}
            <div className="review-panel-status"><span className={'pill ' + (closed ? 'success' : r.status === 'Returned' ? 'danger' : r.status === 'Responded' ? 'violet' : 'neutral')}><i />{r.status}</span><span>Revision {r.revision}</span></div>
            <dl className="record-facts">{field('Company', company)}{field('Client recipient', member(r.assignee))}{field('Due date', r.due)}{field('Priority', r.priority)}</dl>
            <h3 className="portal-form-heading">Requested information</h3>
            <p className="portal-request-text">{r.notes || 'No further detail was added to this request.'}</p>
            {r.reviewNote && <div className="context-note"><span><strong>Correction requested</strong><br /><RichText value={r.reviewNote} /></span></div>}
            <h3 className="portal-form-heading">Client response</h3>
            {r.response ? <blockquote><RichText value={r.response} /></blockquote> : <p className="muted">No written response yet.</p>}
            {r.respondedAt && <p className="muted">Last submitted {new Date(r.respondedAt).toLocaleString()}</p>}
            <h3 className="portal-form-heading">Documents for this request</h3>
            {evidence.length ? <div className="review-doc-list">{evidence.map(d => <a key={d.id} className="source-link" href={'/api/v2-files?id=' + d.id}><FileText size={16} />{d.name}<small>v{d.version || 1} · {d.category || 'Other'}{d.period ? ' · ' + d.period : ''} · {d.shared ? 'Client visible' : 'Internal'}{d.supersededBy ? ' · edited since' : ''}</small><Download size={15} /></a>)}</div> : <p className="muted">The client has not uploaded a document yet.</p>}
            <p className="muted">Choose a document and version on the left to preview, annotate or edit it.</p>

            <section className="form-section">
                <h3>Clarification notes</h3>
                <p>Instructions and observations for the client. They are shown to the client with the request.</p>
                {canEdit && !closed ? <>
                    <RichTextEditor label="Clarification notes" value={notes} onChange={setNotes} rows={6} disabled={busy} placeholder={'Observations\n- The closing balance does not match the ledger\n1. Re-export the September statement'} />
                    <div className="action-wrap"><button type="button" className="btn secondary" disabled={busy || !notesChanged} onClick={async () => { if (await call({ action: 'requestNotes', id: r.id, revision: r.revision, clarification: notes }, true)) setSavedNotes(notes) }}>{busy ? 'Saving…' : 'Save notes'}</button>{notesChanged && <span className="muted">Unsaved notes</span>}{!notesChanged && r.clarificationAt && <span className="muted">Saved {new Date(r.clarificationAt).toLocaleString()}</span>}</div>
                </> : r.clarification ? <RichText value={r.clarification} /> : <p className="muted">No clarification notes.</p>}
            </section>

            {['Open', 'Returned'].includes(r.status) && role !== 'Auditor' && <section className="form-section">
                <h3>Response</h3>
                <RichTextEditor label="Your response" value={response} onChange={setResponse} rows={4} maxLength={10000} disabled={busy} />
                <button type="button" className="btn primary" disabled={busy || (!response.trim() && !evidence.length)} onClick={async () => { if (await call({ action: 'respond', id: r.id, revision: r.revision, response }, true)) setResponse('') }}>Submit for review</button>
            </section>}
            {r.status === 'Responded' && canEdit && <section className="form-section">
                <h3>Review decision</h3>
                <p>Save any document versions and clarification notes first; a decision is final for this round.</p>
                <RichTextEditor label="Review note / correction requested" value={reviewNote} onChange={setReviewNote} rows={4} maxLength={500} disabled={busy} hint="Required when you request a correction. Shown to the client." />
                <div className="action-wrap"><button type="button" className="btn primary" disabled={busy} onClick={() => call({ action: 'requestDecision', id: r.id, revision: r.revision, status: 'Closed' }, true)}>Accept and close</button><button type="button" className="btn secondary" disabled={busy || !reviewNote.trim()} onClick={() => call({ action: 'requestDecision', id: r.id, revision: r.revision, status: 'Returned', reason: reviewNote }, true)}>Request correction</button></div>
            </section>}
            {closed && <div className="context-note"><span>This request is closed. Its documents are read-only.</span></div>}
        </div>
    </div>;
}
