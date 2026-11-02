'use client';
// Read-only DOCX preview. mammoth (BSD-2-Clause) converts the document's content to semantic HTML;
// that HTML is never inserted as markup. It is parsed inertly and rebuilt as React elements from an
// allowlist of tags, with links limited to http(s)/mailto and images to inline data: URIs.
import { createElement, useEffect, useState, type ReactNode } from 'react';
import { AlertCircle, Loader2 } from 'lucide-react';

const TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'b', 'em', 'i', 'u', 's', 'sub', 'sup', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'br', 'a', 'img', 'blockquote', 'pre', 'code', 'span']);

function rebuild(node: Node, key: string): ReactNode {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const el = node as Element, tag = el.tagName.toLowerCase(), children = Array.from(el.childNodes).map((c, i) => rebuild(c, key + '.' + i));
    if (!TAGS.has(tag)) return <span key={key}>{children}</span>;
    const props: Record<string, any> = { key };
    if (tag === 'a') { const href = el.getAttribute('href') || ''; if (/^(https?:|mailto:)/i.test(href)) { props.href = href; props.target = '_blank'; props.rel = 'noopener noreferrer' } }
    if (tag === 'img') { const src = el.getAttribute('src') || ''; if (!/^data:image\/(png|jpe?g|gif|webp|bmp);base64,/i.test(src)) return null; props.src = src; props.alt = el.getAttribute('alt') || ''; return createElement('img', props) }
    if ((tag === 'td' || tag === 'th') && el.getAttribute('colspan')) props.colSpan = Number(el.getAttribute('colspan')) || 1;
    if ((tag === 'td' || tag === 'th') && el.getAttribute('rowspan')) props.rowSpan = Number(el.getAttribute('rowspan')) || 1;
    if (tag === 'br') return <br key={key} />;
    return createElement(tag, props, ...children);
}

export default function DocxPreview({ bytes }: { bytes: ArrayBuffer }) {
    const [content, setContent] = useState<ReactNode>(null), [warnings, setWarnings] = useState(0), [error, setError] = useState('');
    useEffect(() => {
        let gone = false;
        (async () => {
            try {
                const mod: any = await import('mammoth'), mammoth = mod.default ?? mod;
                const result = await mammoth.convertToHtml({ arrayBuffer: bytes.slice(0) });
                const body = new DOMParser().parseFromString(result.value, 'text/html').body; // inert: scripts never run in a parsed document
                if (!gone) { setContent(Array.from(body.childNodes).map((n, i) => rebuild(n, String(i)))); setWarnings(result.messages?.length || 0) }
            } catch (e: any) { if (!gone) setError('This Word document could not be previewed: ' + (e?.message || 'unknown error') + '. Download it to open it in Word.') }
        })();
        return () => { gone = true };
    }, [bytes]);
    if (error) return <div className="viewer-message" role="alert"><AlertCircle size={22} /><p>{error}</p></div>;
    if (!content) return <div className="viewer-message" aria-busy="true"><Loader2 size={22} className="spin" /><p>Opening document…</p></div>;
    return <div className="docx-viewer">
        <div className="viewer-warning" role="note"><AlertCircle size={15} />Preview only: Word documents cannot be edited here. Layout, headers, footers and tracked changes are simplified{warnings ? `; ${warnings} element${warnings === 1 ? '' : 's'} could not be shown` : ''}. Download the file to see it exactly.</div>
        <article className="docx-page">{content}</article>
    </div>;
}
