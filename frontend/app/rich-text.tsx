'use client';
// Clarification notes and messages: a small Markdown subset (**bold**, *italic*, "- " bullets,
// "1. " numbered lists) stored as plain text. Rendering builds React elements only — never HTML —
// so stored text cannot inject markup, whoever wrote it.
import { useRef, useState, type ReactNode } from 'react';

type Block = { kind: 'p'; lines: string[] } | { kind: 'ul' | 'ol'; items: string[]; start: number };
const BULLET = /^\s*[-*•]\s+(.*)$/, NUMBER = /^\s*(\d{1,4})[.)]\s+(.*)$/;

function blocks(source: string): Block[] {
    const out: Block[] = [];
    for (const line of source.replace(/\r\n?/g, '\n').split('\n')) {
        const last = out[out.length - 1], bullet = BULLET.exec(line), number = NUMBER.exec(line);
        if (bullet) { if (last?.kind === 'ul') last.items.push(bullet[1]); else out.push({ kind: 'ul', items: [bullet[1]], start: 1 }) }
        else if (number) { if (last?.kind === 'ol') last.items.push(number[2]); else out.push({ kind: 'ol', items: [number[2]], start: Number(number[1]) || 1 }) }
        else if (!line.trim()) out.push({ kind: 'p', lines: [] });
        else if (last?.kind === 'p' && last.lines.length) last.lines.push(line);
        else out.push({ kind: 'p', lines: [line] });
    }
    return out.filter(b => b.kind !== 'p' || b.lines.length);
}

// Inline emphasis: **bold**, *italic* / _italic_ (bold may contain italic and vice versa).
function inline(text: string, key = 'i'): ReactNode[] {
    const out: ReactNode[] = [], pattern = /\*\*(.+?)\*\*|\*(?!\s)(.+?)\*|(?<![\w])_(?!\s)(.+?)_(?![\w])/g;
    let at = 0, m: RegExpExecArray | null, n = 0;
    while ((m = pattern.exec(text))) {
        if (m.index > at) out.push(text.slice(at, m.index));
        const k = key + '-' + n++;
        out.push(m[1] !== undefined ? <strong key={k}>{inline(m[1], k)}</strong> : <em key={k}>{inline(m[2] ?? m[3], k)}</em>);
        at = m.index + m[0].length;
    }
    if (at < text.length) out.push(text.slice(at));
    return out;
}

export function RichText({ value, className = '' }: { value?: string | null; className?: string }) {
    if (!value?.trim()) return null;
    return <div className={'rich-text ' + className}>{blocks(value).map((b, i) => b.kind === 'p'
        ? <p key={i}>{b.lines.map((l, j) => <span key={j}>{j > 0 && <br />}{inline(l, i + '.' + j)}</span>)}</p>
        : b.kind === 'ul' ? <ul key={i}>{b.items.map((t, j) => <li key={j}>{inline(t, i + '.' + j)}</li>)}</ul>
            : <ol key={i} start={b.start}>{b.items.map((t, j) => <li key={j}>{inline(t, i + '.' + j)}</li>)}</ol>)}</div>;
}

type Props = { label: string; value: string; onChange: (v: string) => void; placeholder?: string; rows?: number; maxLength?: number; disabled?: boolean; hint?: string; id?: string };

/** Textarea with a formatting toolbar and a preview. Keyboard: Ctrl/⌘+B bold, Ctrl/⌘+I italic. */
export function RichTextEditor({ label, value, onChange, placeholder, rows = 5, maxLength = 20000, disabled, hint, id }: Props) {
    const ref = useRef<HTMLTextAreaElement>(null), [preview, setPreview] = useState(false);
    const fieldId = id || 'rte-' + label.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    function apply(next: string, selStart: number, selEnd: number) {
        onChange(next.slice(0, maxLength));
        requestAnimationFrame(() => { const t = ref.current; if (t) { t.focus(); t.setSelectionRange(selStart, selEnd) } });
    }
    function wrap(marker: string, fallback: string) {
        const t = ref.current; if (!t) return;
        const { selectionStart: s, selectionEnd: e } = t, picked = value.slice(s, e) || fallback;
        const already = value.slice(s - marker.length, s) === marker && value.slice(e, e + marker.length) === marker;
        if (already) return apply(value.slice(0, s - marker.length) + picked + value.slice(e + marker.length), s - marker.length, s - marker.length + picked.length);
        apply(value.slice(0, s) + marker + picked + marker + value.slice(e), s + marker.length, s + marker.length + picked.length);
    }
    function list(numbered: boolean) {
        const t = ref.current; if (!t) return;
        const start = value.lastIndexOf('\n', t.selectionStart - 1) + 1, endBreak = value.indexOf('\n', t.selectionEnd), end = endBreak < 0 ? value.length : endBreak;
        const lines = value.slice(start, end).split('\n'), test = numbered ? NUMBER : BULLET;
        const strip = (l: string) => l.replace(BULLET, '$1').replace(NUMBER, '$2');
        const all = lines.every(l => test.test(l));
        const next = lines.map((l, i) => all ? strip(l) : (numbered ? `${i + 1}. ` : '- ') + strip(l)).join('\n');
        apply(value.slice(0, start) + next + value.slice(end), start, start + next.length);
    }
    const tools: [string, string, () => void][] = [['B', 'Bold (Ctrl+B)', () => wrap('**', 'bold text')], ['I', 'Italic (Ctrl+I)', () => wrap('*', 'italic text')], ['•', 'Bulleted list', () => list(false)], ['1.', 'Numbered list', () => list(true)]];
    return <div className={'rte field' + (disabled ? ' disabled' : '')}>
        <div className="rte-head"><label htmlFor={fieldId}>{label}</label><div className="rte-tabs" role="group" aria-label={label + ' view'}><button type="button" className={preview ? '' : 'on'} aria-pressed={!preview} onClick={() => setPreview(false)}>Write</button><button type="button" className={preview ? 'on' : ''} aria-pressed={preview} onClick={() => setPreview(true)}>Preview</button></div></div>
        {!preview && <div className="rte-toolbar" role="toolbar" aria-label={label + ' formatting'}>{tools.map(([text, name, run]) => <button type="button" key={name} title={name} aria-label={name} disabled={disabled} onMouseDown={e => e.preventDefault()} onClick={run} className={'rte-' + (text === 'B' ? 'bold' : text === 'I' ? 'italic' : 'list')}>{text}</button>)}</div>}
        {preview ? <div className="rte-preview" aria-live="polite">{value.trim() ? <RichText value={value} /> : <p className="muted">Nothing to preview yet.</p>}</div>
            : <textarea id={fieldId} ref={ref} rows={rows} value={value} placeholder={placeholder} disabled={disabled} maxLength={maxLength}
                onChange={e => onChange(e.target.value)}
                onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && (e.key === 'b' || e.key === 'i')) { e.preventDefault(); wrap(e.key === 'b' ? '**' : '*', e.key === 'b' ? 'bold text' : 'italic text') } }} />}
        <small className="rte-hint">{hint || 'Use the toolbar for bold, italic, bullet and numbered lists.'} <span>{value.length.toLocaleString()} / {maxLength.toLocaleString()}</span></small>
    </div>;
}
