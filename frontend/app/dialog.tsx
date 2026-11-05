'use client';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

/** Modal shell: focus moves in, Tab stays inside, Escape closes, and focus returns afterwards.
 *  ``inline`` renders it in place instead of at the document root, so it keeps the styles of the
 *  page around it (the client portal's own palette). */
export default function Dialog({ overline, title, onClose, busy, wide, inline, panel, children, footer }: {
    overline: string; title: string; onClose: () => void; busy?: boolean; wide?: boolean; inline?: boolean; panel?: string; children: any; footer?: any;
}) {
    const ref = useRef<HTMLDivElement>(null), close = useRef(onClose), blocked = useRef(!!busy);
    useEffect(() => { close.current = onClose; blocked.current = !!busy });
    const [id] = useState(() => 'dlg-' + Math.random().toString(36).slice(2));
    useEffect(() => {
        const previous = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
        ref.current?.querySelector<HTMLElement>('input,select,textarea,button:not([aria-label="Close dialog"])')?.focus();
        const keys = (e: KeyboardEvent) => {
            if (e.key === 'Escape' && !blocked.current) { e.stopPropagation(); close.current() }
            if (e.key === 'Tab' && ref.current) { const all = Array.from(ref.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')).filter(x => x.offsetParent !== null); if (!all.length) return; if (e.shiftKey && document.activeElement === all[0]) { e.preventDefault(); all[all.length - 1].focus() } else if (!e.shiftKey && document.activeElement === all[all.length - 1]) { e.preventDefault(); all[0].focus() } }
        };
        window.addEventListener('keydown', keys, true);
        return () => { window.removeEventListener('keydown', keys, true); document.body.style.overflow = overflow; previous?.focus() };
    }, []);
    const shell = <div className="drawer-overlay" onMouseDown={e => { if (e.target === e.currentTarget && !blocked.current) close.current() }}><div className={panel || (wide ? 'detail-drawer calendar-drawer' : 'upload-dialog')} role="dialog" aria-modal="true" aria-labelledby={id} ref={ref}><header><div><span className="overline">{overline}</span><h2 id={id}>{title}</h2></div><button className="icon-btn" aria-label="Close dialog" disabled={busy} onClick={onClose}><X size={22} /></button></header>{children}{footer && <footer className="drawer-footer">{footer}</footer>}</div></div>;
    return inline ? shell : createPortal(shell, document.body);
}
