'use client';
// Page controls for long lists: "Showing 21–40 of 106", page size, previous/next and page numbers.
import { ChevronLeft, ChevronRight } from 'lucide-react';

export const PAGE_SIZES = [10, 20, 50, 100];

/** The page numbers to show: always the first and last, the current one and its neighbours. */
function pages(page: number, count: number): (number | '…')[] {
    const keep = new Set([1, count, page - 1, page, page + 1].filter(n => n >= 1 && n <= count)), out: (number | '…')[] = [];
    [...keep].sort((a, b) => a - b).forEach((n, i, all) => { if (i && n - all[i - 1] > 1) out.push('…'); out.push(n) });
    return out;
}

type Props = { page: number; size: number; total: number; label: string; onPage: (page: number) => void; onSize: (size: number) => void };

export default function Pager({ page, size, total, label, onPage, onSize }: Props) {
    const count = Math.max(1, Math.ceil(total / size)), from = total ? (page - 1) * size + 1 : 0, to = Math.min(total, page * size);
    return <nav className="pager" aria-label={label + ' pages'}>
        <span className="pager-count" aria-live="polite">Showing {from}–{to} of {total}</span>
        <label className="pager-size">Per page<select value={size} onChange={e => onSize(Number(e.target.value))} aria-label="Items per page">{PAGE_SIZES.map(n => <option key={n} value={n}>{n}</option>)}</select></label>
        <div className="pager-pages">
            <button type="button" className="icon-btn" aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft size={16} /></button>
            {pages(page, count).map((n, i) => n === '…' ? <span key={'gap' + i} className="pager-gap" aria-hidden="true">…</span>
                : <button type="button" key={n} className={'pager-page' + (n === page ? ' on' : '')} aria-label={'Page ' + n} aria-current={n === page ? 'page' : undefined} onClick={() => onPage(n)}>{n}</button>)}
            <button type="button" className="icon-btn" aria-label="Next page" disabled={page >= count} onClick={() => onPage(page + 1)}><ChevronRight size={16} /></button>
        </div>
    </nav>;
}
