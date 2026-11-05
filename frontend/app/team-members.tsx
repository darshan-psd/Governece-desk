'use client';
// Team Members page of the client portal. This page only presents choices: who may do what is
// decided on the server for every request (backend/app/services/team_service.py).
import { useEffect, useId, useState } from 'react';
import { AlertCircle, Eye, EyeOff, KeyRound, Pencil, Plus, Power, Search, Trash2, Users, X } from 'lucide-react';
import Dialog from './dialog';

type Member = {
    member: string; name: string; username: string | null; email: string | null; role: string; modules: string[]; designation: string;
    company: { id: string; name: string }; status: string; lastActive: string | null; self: boolean; accountManaged: boolean;
    lastAdmin: boolean; otherCompanies: number;
};
type Team = { members: Member[]; summary: { total: number; active: number; deactivated: number } };
type Open = { kind: 'add' } | { kind: 'edit' | 'password' | 'status' | 'remove'; member: Member };

const ROLES: [string, string][] = [
    ['Company Admin', 'Manages team members and has every section of this company.'],
    ['Director', 'Company information, documents, the compliance calendar and their own requests.'],
    ['Accountant', 'Financial documents, invoices and payments, and their own requests.'],
    ['Employee', 'Their own requests, plus only the sections you choose below.'],
];
const MODULES: [string, string][] = [
    ['tracker', 'Work Tracker'], ['profile', 'Company profile (view only)'], ['onboarding', 'Onboarding'], ['documents', 'Documents'],
    ['inbox', 'Inbox'], ['calendar', 'Compliance calendar'], ['invoices', 'Invoices & payments'],
];
const SHARED = 'This person also belongs to another company, so only your company secretary can change their account.';
const initials = (v: string) => (v || '—').split(' ').map(x => x[0]).slice(0, 2).join('').toUpperCase();
const seen = (at: string | null) => at ? new Date(at).toLocaleString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'Never signed in';

function Secret({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
    const [shown, setShown] = useState(false), id = useId();
    return <div className="field team-secret"><span><label htmlFor={id}>{label} <em>*</em></label></span><div className="password-field"><input id={id} type={shown ? 'text' : 'password'} autoComplete="new-password" required minLength={12} value={value} onChange={e => onChange(e.target.value)} /><button type="button" className="password-toggle" aria-pressed={shown} aria-label={(shown ? 'Hide ' : 'Show ') + label.toLowerCase()} onClick={() => setShown(!shown)}>{shown ? <EyeOff size={18} /> : <Eye size={18} />}</button></div></div>;
}

function RoleFields({ role, modules, disabled, hint, onRole, onModules }: { role: string; modules: string[]; disabled?: boolean; hint?: string; onRole: (r: string) => void; onModules: (m: string[]) => void }) {
    return <>
        <label className="field"><span>Role <em>*</em></span><select value={role} disabled={disabled} onChange={e => onRole(e.target.value)}>{ROLES.map(([r]) => <option key={r}>{r}</option>)}</select><small>{hint || ROLES.find(([r]) => r === role)?.[1]}</small></label>
        {role === 'Employee' && <fieldset className="team-modules" disabled={disabled}><legend>Sections this employee can open</legend>{MODULES.map(([id, label]) => <label className="check-row" key={id}><input type="checkbox" checked={modules.includes(id)} onChange={e => onModules(e.target.checked ? [...modules, id] : modules.filter(m => m !== id))} />{label}</label>)}</fieldset>}
    </>;
}

export default function TeamMembers({ company, notify }: { company: { id: string; name: string }; notify: (message: string) => void }) {
    const [team, setTeam] = useState<Team | null>(null), [loadError, setLoadError] = useState('');
    const [search, setSearch] = useState(''), [roleFilter, setRoleFilter] = useState('all'), [statusFilter, setStatusFilter] = useState('all');
    const [open, setOpen] = useState<Open | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const [form, setForm] = useState<any>({});

    async function load() {
        try {
            const response = await fetch('/api/portal/team?company=' + encodeURIComponent(company.id));
            const body: any = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(body.error || 'Your team could not be loaded.');
            setTeam(body); setLoadError('');
        } catch (e: any) { setLoadError(e.message || 'Your team could not be loaded.') }
    }
    useEffect(() => { load() }, [company.id]); // eslint-disable-line react-hooks/exhaustive-deps, react-hooks/set-state-in-effect

    function show(next: Open) {
        setError('');
        setForm(next.kind === 'add' ? { name: '', username: '', password: '', confirmPassword: '', role: 'Employee', modules: [], designation: '' }
            : next.kind === 'edit' ? { name: next.member.name, username: next.member.username || '', designation: next.member.designation, role: next.member.role, modules: next.member.modules }
                : { password: '', confirmPassword: '' });
        setOpen(next);
    }
    const close = () => { if (!busy) setOpen(null) };
    async function send(action: string, body: any, message: string) {
        setBusy(true); setError('');
        try {
            const response = await fetch('/api/portal/team/' + action, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ company: company.id, ...body }) });
            const result: any = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(result.error || 'That did not work. Try again.');
            await load(); setOpen(null); notify(result.warning || message);
        } catch (e: any) { setError(e.message) } finally { setBusy(false) }
    }
    const passwordsMatch = () => { if (form.password === form.confirmPassword) return true; setError('The passwords do not match.'); return false };
    const query = search.trim().toLowerCase();
    const shown = (team?.members || []).filter(m => (roleFilter === 'all' || m.role === roleFilter) && (statusFilter === 'all' || m.status === statusFilter)
        && (!query || [m.name, m.username, m.email, m.designation].join(' ').toLowerCase().includes(query)));
    const filtering = !!query || roleFilter !== 'all' || statusFilter !== 'all';
    const dialogError = error && <div className="error-banner" role="alert"><AlertCircle size={17} />{error}</div>;
    const panel = 'detail-drawer portal-drawer', small = 'upload-dialog portal-drawer';

    if (!team) return <section className="surface" aria-busy={!loadError}>{loadError ? <div className="empty-state"><span><AlertCircle size={26} /></span><h3>Your team could not be loaded</h3><p>{loadError}</p><button className="btn secondary" onClick={() => { setLoadError(''); load() }}>Try again</button></div> : <div className="empty-state"><span><Users size={26} /></span><h3>Loading your team…</h3></div>}</section>;
    return <>
        <div className="metric-grid portal-metrics team-metrics">
            <div className="metric"><span>Total team members<Users size={18} /></span><strong>{team.summary.total}</strong><small>People with access to {company.name}</small></div>
            <div className="metric"><span>Active members<Users size={18} /></span><strong>{team.summary.active}</strong><small>Can sign in</small></div>
            <div className="metric"><span>Deactivated members<Power size={18} /></span><strong>{team.summary.deactivated}</strong><small>Cannot sign in until reactivated</small></div>
        </div>
        <div className="portal-toolbar team-toolbar">
            <label className="search-field"><Search size={17} /><input aria-label="Search team members" placeholder="Search by name or username…" value={search} onChange={e => setSearch(e.target.value)} /></label>
            <div className="page-actions">
                <select aria-label="Filter by role" value={roleFilter} onChange={e => setRoleFilter(e.target.value)}><option value="all">All roles</option>{ROLES.map(([r]) => <option key={r}>{r}</option>)}</select>
                <select aria-label="Filter by account status" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">All statuses</option><option>Active</option><option>Deactivated</option></select>
                <button className="btn primary" onClick={() => show({ kind: 'add' })}><Plus size={16} />Add Team Member</button>
            </div>
        </div>
        <section className="surface">
            <div className="data-scroll"><table className="records-table team-table"><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Company</th><th>Account status</th><th>Last active</th><th>Actions</th></tr></thead>
                <tbody>{shown.map(m => <tr key={m.member} className={m.status === 'Active' ? '' : 'team-off'}>
                    <td><span className="person-cell"><i>{initials(m.name)}</i>{m.name}{m.self ? ' (you)' : ''}</span>{m.designation && <span className="record-meta">{m.designation}</span>}</td>
                    <td>{m.username || m.email || <span className="muted">No sign-in yet</span>}</td>
                    <td>{m.role}{m.role === 'Employee' && <span className="record-meta">{m.modules.length ? m.modules.length + (m.modules.length === 1 ? ' section' : ' sections') : 'Own requests only'}</span>}</td>
                    <td>{m.company.name}</td>
                    <td><span className={'pill ' + (m.status === 'Active' ? 'success' : 'danger')}><i />{m.status}</span></td>
                    <td>{seen(m.lastActive)}</td>
                    <td><div className="team-actions">
                        <button className="icon-btn" aria-label={'Edit ' + m.name} title="Edit details and role" onClick={() => show({ kind: 'edit', member: m })}><Pencil size={16} /></button>
                        {!m.self && <>
                            <button className="icon-btn" aria-label={'Reset password for ' + m.name} title={m.accountManaged ? 'Reset password' : SHARED} disabled={!m.accountManaged} onClick={() => show({ kind: 'password', member: m })}><KeyRound size={16} /></button>
                            <button className="icon-btn" aria-label={(m.status === 'Active' ? 'Deactivate ' : 'Reactivate ') + m.name} title={!m.accountManaged ? SHARED : m.lastAdmin ? 'The only active Company Admin cannot be deactivated' : m.status === 'Active' ? 'Deactivate' : 'Reactivate'} disabled={!m.accountManaged || m.lastAdmin} onClick={() => show({ kind: 'status', member: m })}><Power size={16} /></button>
                            <button className="icon-btn" aria-label={'Remove ' + m.name} title={m.lastAdmin ? 'The only active Company Admin cannot be removed' : 'Remove from this company'} disabled={m.lastAdmin} onClick={() => show({ kind: 'remove', member: m })}><Trash2 size={16} /></button>
                        </>}
                    </div></td>
                </tr>)}</tbody></table>
                {!shown.length && <div className="empty-state"><span><Users size={26} /></span><h3>{filtering ? 'No team members match' : 'No team members yet'}</h3><p>{filtering ? 'Change the search or filters.' : 'Add the people who work with you on ' + company.name + '.'}</p>{filtering && <button className="btn secondary" onClick={() => { setSearch(''); setRoleFilter('all'); setStatusFilter('all') }}><X size={15} />Clear filters</button>}</div>}
            </div>
        </section>

        {open?.kind === 'add' && <Dialog inline panel={panel} overline={company.name} title="Add team member" busy={busy} onClose={close} footer={<><button className="btn secondary" type="button" disabled={busy} onClick={close}>Cancel</button><div /><button className="btn primary" type="submit" form="team-add" disabled={busy}>{busy ? 'Creating account…' : 'Create account'}</button></>}>
            <form id="team-add" onSubmit={e => { e.preventDefault(); if (passwordsMatch()) send('create', form, `Account created for ${form.name}. Give them their username and password.`) }}><div className="drawer-content">
                {dialogError}
                <div className="context-note"><AlertCircle size={18} /><span>The account works straight away. No email is sent: give the person their username and password yourself, privately.</span></div>
                <label className="field"><span>Full name <em>*</em></span><input required maxLength={200} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
                <label className="field"><span>Username <em>*</em></span><input required minLength={3} maxLength={32} autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="e.g. ravi.kumar" value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} /><small>3 to 32 letters, numbers, dots, hyphens or underscores. They sign in with this.</small></label>
                <div className="form-grid"><Secret label="Password" value={form.password} onChange={v => setForm({ ...form, password: v })} /><Secret label="Confirm password" value={form.confirmPassword} onChange={v => setForm({ ...form, confirmPassword: v })} /></div>
                <p className="field-hint">At least 12 characters. A short sentence works well.</p>
                <RoleFields role={form.role} modules={form.modules} onRole={role => setForm({ ...form, role })} onModules={modules => setForm({ ...form, modules })} />
                <label className="field"><span>Designation (optional)</span><input maxLength={120} placeholder="e.g. Finance manager" value={form.designation} onChange={e => setForm({ ...form, designation: e.target.value })} /></label>
            </div></form>
        </Dialog>}

        {open?.kind === 'edit' && <Dialog inline panel={panel} overline={company.name} title={'Edit ' + open.member.name} busy={busy} onClose={close} footer={<><button className="btn secondary" type="button" disabled={busy} onClick={close}>Cancel</button><div /><button className="btn primary" type="submit" form="team-edit" disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button></>}>
            <form id="team-edit" onSubmit={e => { e.preventDefault(); const { member } = open; const details = member.self || member.accountManaged; send('update', { member: member.member, ...(details ? { name: form.name, designation: form.designation, ...(form.username ? { username: form.username } : {}) } : {}), ...(member.self ? {} : { role: form.role, modules: form.modules }) }, 'Team member updated') }}><div className="drawer-content">
                {dialogError}
                {!open.member.self && !open.member.accountManaged && <div className="context-note"><AlertCircle size={18} /><span>{SHARED} You can still change their role in {company.name}.</span></div>}
                <fieldset disabled={!open.member.self && !open.member.accountManaged} className="team-plain">
                    <label className="field"><span>Full name <em>*</em></span><input required maxLength={200} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
                    <label className="field"><span>Username{open.member.email && !open.member.username ? ' (optional: they sign in with ' + open.member.email + ')' : ''}</span><input minLength={3} maxLength={32} autoComplete="off" autoCapitalize="none" spellCheck={false} value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} /></label>
                    <label className="field"><span>Designation (optional)</span><input maxLength={120} value={form.designation} onChange={e => setForm({ ...form, designation: e.target.value })} /></label>
                </fieldset>
                <RoleFields role={form.role} modules={form.modules} disabled={open.member.self || (open.member.lastAdmin && form.role === 'Company Admin')} hint={open.member.self ? 'You cannot change your own role.' : open.member.lastAdmin ? 'The only active Company Admin must stay one. Make someone else an admin first.' : undefined} onRole={role => setForm({ ...form, role })} onModules={modules => setForm({ ...form, modules })} />
            </div></form>
        </Dialog>}

        {open?.kind === 'password' && <Dialog inline panel={small} overline="RESET PASSWORD" title={'New password for ' + open.member.name} busy={busy} onClose={close} footer={<><button className="btn secondary" type="button" disabled={busy} onClick={close}>Cancel</button><div /><button className="btn primary" type="submit" form="team-password" disabled={busy}>{busy ? 'Saving…' : 'Reset password'}</button></>}>
            <form id="team-password" onSubmit={e => { e.preventDefault(); if (passwordsMatch()) send('password', { member: open.member.member, password: form.password, confirmPassword: form.confirmPassword }, `Password reset for ${open.member.name}. Give them the new password.`) }}><div className="drawer-content">
                {dialogError}
                <div className="context-note"><AlertCircle size={18} /><span>Their old password stops working and they are signed out everywhere. No email is sent: give them the new password yourself, privately.</span></div>
                <Secret label="New password" value={form.password} onChange={v => setForm({ ...form, password: v })} />
                <Secret label="Confirm password" value={form.confirmPassword} onChange={v => setForm({ ...form, confirmPassword: v })} />
            </div></form>
        </Dialog>}

        {open?.kind === 'status' && (() => { const off = open.member.status === 'Active'; return <Dialog inline panel={small} overline={off ? 'DEACTIVATE MEMBER' : 'REACTIVATE MEMBER'} title={(off ? 'Deactivate ' : 'Reactivate ') + open.member.name + '?'} busy={busy} onClose={close} footer={<><button className="btn secondary" type="button" disabled={busy} onClick={close}>Keep as is</button><div /><button className="btn primary" disabled={busy} onClick={() => send('status', { member: open.member.member, status: off ? 'inactive' : 'active' }, off ? 'Team member deactivated' : 'Team member reactivated')}>{busy ? 'Saving…' : off ? 'Deactivate account' : 'Reactivate account'}</button></>}>
            <div className="drawer-content">{dialogError}<p className="muted">{off ? 'They are signed out at once and cannot sign in until you reactivate them. Their work and history stay in place.' : 'They can sign in again with their existing username and password.'}</p></div>
        </Dialog> })()}

        {open?.kind === 'remove' && <Dialog inline panel={small} overline="REMOVE MEMBER" title={'Remove ' + open.member.name + '?'} busy={busy} onClose={close} footer={<><button className="btn secondary" type="button" disabled={busy} onClick={close}>Keep member</button><div /><button className="btn primary team-danger" disabled={busy} onClick={() => send('remove', { member: open.member.member }, 'Team member removed')}>{busy ? 'Removing…' : 'Remove from ' + company.name}</button></>}>
            <div className="drawer-content">{dialogError}<p className="muted">{open.member.otherCompanies ? `They lose access to ${company.name} and keep their other company.` : 'Their account is closed for good: they can never sign in again and their username becomes free. Past work keeps showing their name. If they may come back, deactivate them instead.'}</p></div>
        </Dialog>}
    </>;
}
