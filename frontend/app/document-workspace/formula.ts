// A small spreadsheet formula engine for the review workspace.
//
// Supports arithmetic (+ - * / ^ %), comparison (= <> < > <= >=), text concatenation (&), cell and
// range references (A1, $B$2, A1:C9, A:A, Sheet2!A1, 'My sheet'!A1:B2) and the functions in FUNCTIONS.
// Anything else raises Unsupported, and the caller keeps the result Excel last calculated, so the
// grid never shows a value this engine merely guessed at. Excel recalculates every formula when
// the saved workbook is opened (fullCalcOnLoad).

export class FormulaError { constructor(public code: string) { } toString() { return this.code } }
export class Unsupported extends Error { }
export type Scalar = number | string | boolean | null | FormulaError | Date;
type Area = { sheet?: string; r1: number; c1: number; r2: number; c2: number };
type Node =
    | { t: 'lit'; v: Scalar }
    | { t: 'area'; a: Area; single: boolean }
    | { t: 'fn'; name: string; args: Node[] }
    | { t: 'bin'; op: string; a: Node; b: Node }
    | { t: 'neg'; a: Node }
    | { t: 'pct'; a: Node };

export interface Workbook {
    /** Evaluated value of a cell (1-based row/column) on the named sheet. */
    get(sheet: string, r: number, c: number): Scalar;
    /** Last used row of a sheet, for whole-column references. */
    rows(sheet: string): number;
    hasSheet(sheet: string): boolean;
}

export const colName = (c: number) => { let s = ''; for (let n = c; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s; return s };
export const colNumber = (s: string) => [...s.toUpperCase()].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
export const address = (r: number, c: number) => colName(c) + r;
export function parseAddress(text: string): { r: number; c: number } | null {
    const m = /^\$?([A-Za-z]{1,3})\$?(\d{1,7})$/.exec(text.trim());
    if (!m) return null;
    const c = colNumber(m[1]), r = Number(m[2]);
    return c >= 1 && c <= 16384 && r >= 1 && r <= 1048576 ? { r, c } : null;
}

// Tokenizer ---------------------------------------------------------------------------------
type Tok = { k: 'num' | 'str' | 'ref' | 'fn' | 'bool' | 'op' | '(' | ')' | ','; v: string };
const REF = /^(?:(?:'((?:[^']|'')+)'|([A-Za-z_][\w.]*))!)?(\$?[A-Za-z]{1,3}\$?\d{1,7}(?::\$?[A-Za-z]{1,3}\$?\d{1,7})?|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3})(?![\w(])/;

function tokenize(src: string): Tok[] {
    const out: Tok[] = [];
    let i = 0;
    while (i < src.length) {
        const ch = src[i], rest = src.slice(i);
        if (/\s/.test(ch)) { i++; continue }
        if (ch === '"') {
            let j = i + 1, s = '';
            for (; j < src.length; j++) { if (src[j] === '"') { if (src[j + 1] === '"') { s += '"'; j++ } else break } else s += src[j] }
            if (j >= src.length) throw new SyntaxError('Unclosed text');
            out.push({ k: 'str', v: s }); i = j + 1; continue;
        }
        const num = /^(\d+\.?\d*|\.\d+)(e[+-]?\d+)?/i.exec(rest);
        if (num && !/^\d+:/.test(rest)) { out.push({ k: 'num', v: num[0] }); i += num[0].length; continue }
        const ref = REF.exec(rest);
        if (ref) { out.push({ k: 'ref', v: ref[0] }); i += ref[0].length; continue }
        const word = /^([A-Za-z_][\w.]*)\s*\(/.exec(rest);
        if (word) { out.push({ k: 'fn', v: word[1].toUpperCase() }); i += word[0].length; continue }
        const bool = /^(TRUE|FALSE)(?![\w(])/i.exec(rest);
        if (bool) { out.push({ k: 'bool', v: bool[1].toUpperCase() }); i += bool[0].length; continue }
        const op = /^(<=|>=|<>|[-+*/^&=<>%])/.exec(rest);
        if (op) { out.push({ k: 'op', v: op[0] }); i += op[0].length; continue }
        if (ch === '(' || ch === ')' || ch === ',') { out.push({ k: ch, v: ch }); i++; continue }
        throw new Unsupported('Unsupported formula syntax near ' + rest.slice(0, 12));
    }
    return out;
}

function area(text: string): { a: Area; single: boolean } {
    const bang = text.lastIndexOf('!');
    let sheet: string | undefined, body = text;
    if (bang >= 0) { sheet = text.slice(0, bang); body = text.slice(bang + 1); if (sheet.startsWith("'")) sheet = sheet.slice(1, -1).replace(/''/g, "'") }
    const [from, to] = body.replace(/\$/g, '').split(':');
    if (/^[A-Za-z]+$/.test(from)) { // whole columns
        const c1 = colNumber(from), c2 = colNumber(to);
        return { a: { sheet, r1: 1, c1: Math.min(c1, c2), r2: Infinity, c2: Math.max(c1, c2) }, single: false };
    }
    const p = parseAddress(from), q = to ? parseAddress(to) : p;
    if (!p || !q) throw new SyntaxError('Bad reference');
    return { a: { sheet, r1: Math.min(p.r, q.r), c1: Math.min(p.c, q.c), r2: Math.max(p.r, q.r), c2: Math.max(p.c, q.c) }, single: !to };
}

// Parser (Excel precedence: comparison < & < + - < * / < ^ < unary minus < %) -------------
export function parse(formula: string): Node {
    const toks = tokenize(formula.replace(/^=/, ''));
    let i = 0;
    const peek = () => toks[i], take = () => toks[i++];
    const isOp = (...ops: string[]) => peek()?.k === 'op' && ops.includes(peek().v);
    function binary(next: () => Node, ops: string[]): () => Node {
        return () => { let a = next(); while (isOp(...ops)) { const op = take().v; a = { t: 'bin', op, a, b: next() } } return a };
    }
    function primary(): Node {
        const t = take();
        if (!t) throw new SyntaxError('Unexpected end of formula');
        if (t.k === 'num') return { t: 'lit', v: Number(t.v) };
        if (t.k === 'str') return { t: 'lit', v: t.v };
        if (t.k === 'bool') return { t: 'lit', v: t.v === 'TRUE' };
        if (t.k === 'ref') return { t: 'area', ...area(t.v) };
        if (t.k === '(') { const e = comparison(); if (take()?.k !== ')') throw new SyntaxError('Missing )'); return e }
        if (t.k === 'fn') {
            const args: Node[] = [];
            if (peek()?.k !== ')') for (; ;) { args.push(comparison()); if (peek()?.k === ',') { take(); continue } break }
            if (take()?.k !== ')') throw new SyntaxError('Missing )');
            return { t: 'fn', name: t.v, args };
        }
        if (t.k === 'op' && (t.v === '-' || t.v === '+')) { const a = unary(); return t.v === '-' ? { t: 'neg', a } : a }
        throw new SyntaxError('Unexpected ' + t.v);
    }
    function percent(): Node { let a = primary(); while (isOp('%')) { take(); a = { t: 'pct', a } } return a }
    function unary(): Node { if (isOp('-', '+')) { const op = take().v; const a = unary(); return op === '-' ? { t: 'neg', a } : a } return percent() }
    const power = binary(unary, ['^']), product = binary(power, ['*', '/']), sum = binary(product, ['+', '-']), concat = binary(sum, ['&']);
    const comparison = binary(concat, ['=', '<>', '<', '>', '<=', '>=']);
    const tree = comparison();
    if (i < toks.length) throw new SyntaxError('Unexpected ' + toks[i].v);
    return tree;
}

/** Every area a formula reads, with its sheet resolved. */
export function references(node: Node, sheet: string, out: Area[] = []): Area[] {
    if (node.t === 'area') out.push({ ...node.a, sheet: node.a.sheet ?? sheet });
    else if (node.t === 'fn') node.args.forEach(a => references(a, sheet, out));
    else if (node.t === 'bin') { references(node.a, sheet, out); references(node.b, sheet, out) }
    else if (node.t === 'neg' || node.t === 'pct') references(node.a, sheet, out);
    return out;
}

// Evaluation --------------------------------------------------------------------------------
const VALUE = () => new FormulaError('#VALUE!'), DIV0 = () => new FormulaError('#DIV/0!'), REFERR = () => new FormulaError('#REF!');
type Arg = Scalar | Scalar[];

function num(v: Scalar): number | FormulaError {
    if (v instanceof FormulaError) return v;
    if (v === null || v === '') return 0;
    if (typeof v === 'number') return v;
    if (typeof v === 'boolean') return v ? 1 : 0;
    if (v instanceof Date) return v.getTime() / 86400000 + 25569;
    const n = Number(String(v).replace(/,/g, ''));
    return Number.isFinite(n) ? n : VALUE();
}
const str = (v: Scalar) => v === null ? '' : typeof v === 'boolean' ? (v ? 'TRUE' : 'FALSE') : v instanceof Date ? v.toISOString().slice(0, 10) : String(v);
const truth = (v: Scalar): boolean | FormulaError => v instanceof FormulaError ? v : typeof v === 'string' ? (v.toUpperCase() === 'TRUE' ? true : v.toUpperCase() === 'FALSE' ? false : VALUE() as any) : !!num(v);
const flat = (args: Arg[]) => args.flatMap(a => Array.isArray(a) ? a.map(v => ({ v, ref: true })) : [{ v: a, ref: false }]);
const firstError = (args: Arg[]) => flat(args).find(x => x.v instanceof FormulaError)?.v as FormulaError | undefined;

/** Numbers to aggregate: numbers anywhere; text/booleans only when typed directly as an argument. */
function numbers(args: Arg[]): number[] | FormulaError {
    const out: number[] = [];
    for (const { v, ref } of flat(args)) {
        if (v instanceof FormulaError) return v;
        if (typeof v === 'number') out.push(v);
        else if (v instanceof Date) out.push(num(v) as number);
        else if (!ref && v !== null) { const n = num(v); if (n instanceof FormulaError) return n; out.push(n) }
    }
    return out;
}

const FUNCTIONS: Record<string, (args: Arg[]) => Scalar> = {
    SUM: a => { const n = numbers(a); return n instanceof FormulaError ? n : n.reduce((s, x) => s + x, 0) },
    AVERAGE: a => { const n = numbers(a); return n instanceof FormulaError ? n : n.length ? n.reduce((s, x) => s + x, 0) / n.length : DIV0() },
    MIN: a => { const n = numbers(a); return n instanceof FormulaError ? n : n.length ? Math.min(...n) : 0 },
    MAX: a => { const n = numbers(a); return n instanceof FormulaError ? n : n.length ? Math.max(...n) : 0 },
    COUNT: a => flat(a).filter(({ v }) => typeof v === 'number' || v instanceof Date).length,
    COUNTA: a => flat(a).filter(({ v }) => v !== null && v !== '').length,
    ROUND: a => { const x = num(a[0] as Scalar), d = num((a[1] ?? 0) as Scalar); if (x instanceof FormulaError) return x; if (d instanceof FormulaError) return d; const f = 10 ** Math.trunc(d); return Math.round((x + Math.sign(x) * Number.EPSILON) * f) / f },
    ABS: a => { const x = num(a[0] as Scalar); return x instanceof FormulaError ? x : Math.abs(x) },
    AND: a => { const e = firstError(a); if (e) return e; const v = flat(a).filter(x => x.v !== null).map(x => truth(x.v)); return v.length ? v.every(Boolean) : VALUE() },
    OR: a => { const e = firstError(a); if (e) return e; const v = flat(a).filter(x => x.v !== null).map(x => truth(x.v)); return v.length ? v.some(Boolean) : VALUE() },
    NOT: a => { const t = truth(a[0] as Scalar); return t instanceof FormulaError ? t : !t },
    CONCAT: a => firstError(a) ?? flat(a).map(x => str(x.v)).join(''),
    CONCATENATE: a => firstError(a) ?? flat(a).map(x => str(x.v)).join(''),
    LEN: a => a[0] instanceof FormulaError ? a[0] : str(a[0] as Scalar).length,
    UPPER: a => a[0] instanceof FormulaError ? a[0] : str(a[0] as Scalar).toUpperCase(),
    LOWER: a => a[0] instanceof FormulaError ? a[0] : str(a[0] as Scalar).toLowerCase(),
    TRIM: a => a[0] instanceof FormulaError ? a[0] : str(a[0] as Scalar).trim().replace(/ +/g, ' '),
};
export const SUPPORTED_FUNCTIONS = ['IF', 'IFERROR', ...Object.keys(FUNCTIONS)].sort();

export function evaluate(node: Node, sheet: string, book: Workbook): Scalar {
    const scalar = (n: Node): Scalar => {
        const v = value(n);
        if (!Array.isArray(v)) return v;
        return v.length === 1 ? v[0] : VALUE();
    };
    const value = (n: Node): Arg => {
        switch (n.t) {
            case 'lit': return n.v;
            case 'area': {
                const s = n.a.sheet ?? sheet;
                if (!book.hasSheet(s)) return REFERR();
                if (n.single) return book.get(s, n.a.r1, n.a.c1);
                const r2 = Math.min(n.a.r2, book.rows(s)), out: Scalar[] = [];
                if ((r2 - n.a.r1 + 1) * (n.a.c2 - n.a.c1 + 1) > 2_000_000) throw new Unsupported('Range too large');
                for (let r = n.a.r1; r <= r2; r++) for (let c = n.a.c1; c <= n.a.c2; c++) out.push(book.get(s, r, c));
                return out;
            }
            case 'neg': { const x = num(scalar(n.a)); return x instanceof FormulaError ? x : -x }
            case 'pct': { const x = num(scalar(n.a)); return x instanceof FormulaError ? x : x / 100 }
            case 'fn': {
                if (n.name === 'IF') {
                    const t = truth(scalar(n.args[0] ?? { t: 'lit', v: null }));
                    if (t instanceof FormulaError) return t;
                    return t ? (n.args[1] ? scalar(n.args[1]) : true) : (n.args[2] ? scalar(n.args[2]) : false);
                }
                if (n.name === 'IFERROR') { const v = scalar(n.args[0]); return v instanceof FormulaError ? scalar(n.args[1] ?? { t: 'lit', v: '' }) : v }
                const fn = FUNCTIONS[n.name];
                if (!fn) throw new Unsupported(n.name + ' is not supported in the browser editor');
                return fn(n.args.map(value));
            }
            case 'bin': {
                const a = scalar(n.a), b = scalar(n.b);
                if (a instanceof FormulaError) return a;
                if (b instanceof FormulaError) return b;
                if (n.op === '&') return str(a) + str(b);
                if (['=', '<>', '<', '>', '<=', '>='].includes(n.op)) {
                    const bothNum = typeof (a ?? 0) !== 'string' && typeof (b ?? 0) !== 'string';
                    const x: any = bothNum ? num(a) : str(a).toLowerCase(), y: any = bothNum ? num(b) : str(b).toLowerCase();
                    return n.op === '=' ? x === y : n.op === '<>' ? x !== y : n.op === '<' ? x < y : n.op === '>' ? x > y : n.op === '<=' ? x <= y : x >= y;
                }
                const x = num(a), y = num(b);
                if (x instanceof FormulaError) return x;
                if (y instanceof FormulaError) return y;
                if (n.op === '+') return x + y;
                if (n.op === '-') return x - y;
                if (n.op === '*') return x * y;
                if (n.op === '/') return y === 0 ? DIV0() : x / y;
                if (n.op === '^') return x ** y;
                throw new Unsupported(n.op);
            }
        }
    };
    return scalar(node);
}
