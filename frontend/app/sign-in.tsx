'use client';
import {useEffect,useState} from 'react';
import Workspace from './workspace-next';

// Sign-in screens for GOVERNANCE_AUTH_MODE=password. In local-preview mode the workspace opens directly.
// Every write is protected by the CSRF token the server puts in the gov_csrf cookie (see installCsrf).
const csrfCookie=()=>{const m=/(?:^|;\s*)(?:__Host-)?gov_csrf=([^;]+)/.exec(document.cookie);return m?decodeURIComponent(m[1]):''};
export function installCsrf(){
 const original=window.fetch.bind(window);
 window.fetch=async(input:RequestInfo|URL,init:RequestInit={})=>{
  const method=(init.method||(input instanceof Request?input.method:'GET')).toUpperCase();
  const url=new URL(input instanceof Request?input.url:String(input),location.href);
  const sameOrigin=url.origin===location.origin;
  if(sameOrigin&&!['GET','HEAD','OPTIONS'].includes(method)){const headers=new Headers(init.headers||(input instanceof Request?input.headers:undefined));if(!headers.has('X-CSRF-Token'))headers.set('X-CSRF-Token',csrfCookie());init={...init,headers}}
  const response=await original(input,init);
  // A protected business API returned 401: the session ended (expiry, revocation, deactivation).
  // The sign-in screens handle their own 401s directly, so they are excluded here.
  if(response.status===401&&sameOrigin&&url.pathname.startsWith('/api/')&&!url.pathname.startsWith('/api/auth/')){
   window.dispatchEvent(new Event('governance:session-expired'));
  }
  return response;
 };
}
async function post(path:string,body:any){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d:any=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||'Request failed');return d}
async function whoAmI():Promise<any>{const r=await fetch('/api/auth/me');if(!r.ok)return null;return r.json()}
type Screen={kind:'loading'|'workspace'|'signin'|'forgot'|'reset'|'activate'|'change-password'|'error',token?:string,message?:string};
function route():Screen|null{const m=/^#(activate|reset-password)\?token=([A-Za-z0-9_-]+)$/.exec(location.hash);return m?{kind:m[1]==='activate'?'activate':'reset',token:m[2]}:null}

// Reusable show/hide password field.
let fieldSeq=0;
function PasswordField({label,value,onChange,autoComplete,disabled,required=true,minLength}:{
 label:string,value:string,onChange:(v:string)=>void,autoComplete:string,disabled?:boolean,required?:boolean,minLength?:number}){
 const [shown,setShown]=useState(false);
 const [id]=useState(()=>`password-field-${++fieldSeq}`);
 // The toggle button sits outside the <label> (as the input's sibling): only the input itself
 // carries the "Password" accessible name, so screen readers and `getByLabel` queries are unambiguous.
 return <div className="field"><label htmlFor={id}><span>{label}{required&&<em> *</em>}</span></label><div className="password-field">
  <input id={id} type={shown?'text':'password'} autoComplete={autoComplete} required={required} minLength={minLength}
         value={value} onChange={e=>onChange(e.target.value)} disabled={disabled}/>
  <button type="button" className="password-toggle" onClick={()=>setShown(!shown)} aria-pressed={shown} aria-label={shown?`Hide ${label.toLowerCase()}`:`Show ${label.toLowerCase()}`}>{shown?'Hide':'Show'}</button>
 </div></div>;
}

export default function AuthGate(){
 const [screen,setScreen]=useState<Screen>({kind:'loading'});
 // After sign-in (or on reload with an existing session), route to the workspace or, if this
 // account still has a temporary password, to the forced change screen (also enforced server-side).
 async function afterSignedIn(){
  const me=await whoAmI();
  if(!me){setScreen({kind:'signin'});return}
  setScreen(me.mustChangePassword?{kind:'change-password'}:{kind:'workspace'});
 }
 useEffect(()=>{(async()=>{try{
  const config:any=await (await fetch('/api/auth/config')).json();
  if(config.mode!=='password'){setScreen({kind:'workspace'});return}
  const linked=route();if(linked){setScreen(linked);return}
  await afterSignedIn();
 }catch{setScreen({kind:'error',message:'The workspace could not be reached. Check your connection and reload.'})}})()},[]);
 // A protected request anywhere in the app got 401 (session expired, revoked or deactivated):
 // drop back to the sign-in screen without leaving any protected data on screen.
 useEffect(()=>{const onExpired=()=>setScreen({kind:'signin',message:'Your session ended. Sign in again.'});
  window.addEventListener('governance:session-expired',onExpired);return()=>window.removeEventListener('governance:session-expired',onExpired)},[]);
 if(screen.kind==='workspace')return <Workspace/>;
 if(screen.kind==='loading')return <div className="auth-screen"><div className="auth-card"><p className="muted">Opening Governance Desk…</p></div></div>;
 return <div className="auth-screen"><div className="auth-card"><div className="auth-brand">Governance<span>DESK</span></div>
  {screen.kind==='error'&&<p role="alert" className="auth-error">{screen.message}</p>}
  {screen.kind==='signin'&&<SignIn notice={screen.message} onForgot={()=>setScreen({kind:'forgot'})} onDone={afterSignedIn}/>}
  {screen.kind==='forgot'&&<Forgot onBack={()=>setScreen({kind:'signin'})}/>}
  {(screen.kind==='reset'||screen.kind==='activate')&&<ChoosePassword kind={screen.kind} token={screen.token!} onDone={message=>{history.replaceState(null,'',location.pathname);setScreen({kind:'signin',message})}}/>}
  {screen.kind==='change-password'&&<ForcedChange onDone={()=>setScreen({kind:'workspace'})}/>}
 </div></div>;
}

function SignIn({notice,onForgot,onDone}:{notice?:string,onForgot:()=>void,onDone:()=>void}){
 const [email,setEmail]=useState(''),[password,setPassword]=useState(''),[organizations,setOrganizations]=useState<{id:string,name:string}[]>([]),[organization,setOrganization]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function submit(e:React.FormEvent){e.preventDefault();if(busy)return;setBusy(true);setError('');try{
  const result=await post('/api/auth/login',{email,password,...(organization?{organization}:{})});
  if(result.ok){setPassword('');await onDone();return}
  setOrganizations(result.chooseOrganization||[]);setOrganization(result.chooseOrganization?.[0]?.id||'');
 }catch(err:any){setError(err.message)}finally{setBusy(false)}}
 return <form onSubmit={submit} className="auth-form"><h1>Sign in</h1><p className="muted">Use the email address your workspace owner gave you.</p>
  {notice&&<p className="auth-notice" role="status">{notice}</p>}
  <label className="field"><span>Email</span><input type="email" autoComplete="username" required value={email} onChange={e=>setEmail(e.target.value)} disabled={busy}/></label>
  <PasswordField label="Password" value={password} onChange={setPassword} autoComplete="current-password" disabled={busy}/>
  {organizations.length>0&&<label className="field"><span>Workspace</span><select value={organization} onChange={e=>setOrganization(e.target.value)}>{organizations.map(o=><option key={o.id} value={o.id}>{o.name}</option>)}</select></label>}
  {error&&<p role="alert" className="auth-error">{error}</p>}
  <button className="btn primary" disabled={busy}>{busy?'Signing in…':organizations.length?'Open workspace':'Sign in'}</button>
  <button type="button" className="text-btn" onClick={onForgot} disabled={busy}>Forgot your password?</button>
 </form>;
}

function Forgot({onBack}:{onBack:()=>void}){
 const [email,setEmail]=useState(''),[sent,setSent]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function submit(e:React.FormEvent){e.preventDefault();if(busy)return;setBusy(true);setError('');try{setSent((await post('/api/auth/password/forgot',{email})).message)}catch(err:any){setError(err.message)}finally{setBusy(false)}}
 return <form onSubmit={submit} className="auth-form"><h1>Reset your password</h1><p className="muted">We will email a link that is valid for a short time.</p>
  <label className="field"><span>Email</span><input type="email" autoComplete="username" required value={email} onChange={e=>setEmail(e.target.value)} disabled={busy||!!sent}/></label>
  {sent&&<p className="auth-notice" role="status">{sent}</p>}{error&&<p role="alert" className="auth-error">{error}</p>}
  {!sent&&<button className="btn primary" disabled={busy}>Send reset link</button>}
  <button type="button" className="text-btn" onClick={onBack} disabled={busy}>Back to sign in</button>
 </form>;
}

function ChoosePassword({kind,token,onDone}:{kind:'reset'|'activate',token:string,onDone:(message:string)=>void}){
 const [password,setPassword]=useState(''),[repeat,setRepeat]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function submit(e:React.FormEvent){e.preventDefault();if(busy)return;setError('');if(password!==repeat){setError('The passwords do not match.');return}setBusy(true);try{
  await post(kind==='activate'?'/api/auth/activate':'/api/auth/password/reset',{token,password});
  onDone(kind==='activate'?'Your account is active. Sign in with your new password.':'Your password was changed and every session was signed out. Sign in again.');
 }catch(err:any){setError(err.message)}finally{setBusy(false)}}
 return <form onSubmit={submit} className="auth-form"><h1>{kind==='activate'?'Activate your account':'Choose a new password'}</h1><p className="muted">At least 12 characters. A short sentence works well.</p>
  <PasswordField label="New password" value={password} onChange={setPassword} autoComplete="new-password" minLength={12} disabled={busy}/>
  <PasswordField label="Repeat password" value={repeat} onChange={setRepeat} autoComplete="new-password" minLength={12} disabled={busy}/>
  {error&&<p role="alert" className="auth-error">{error}</p>}
  <button className="btn primary" disabled={busy}>{kind==='activate'?'Activate account':'Save password'}</button>
 </form>;
}

// Shown after signing in with a temporary password the Owner set (Team & access). Enforced
// server-side too: every other API call is refused with 403 until this succeeds.
function ForcedChange({onDone}:{onDone:()=>void}){
 const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[repeat,setRepeat]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function submit(e:React.FormEvent){e.preventDefault();if(busy)return;setError('');if(password!==repeat){setError('The new passwords do not match.');return}setBusy(true);try{
  await post('/api/auth/password/change',{current,password});
  onDone();
 }catch(err:any){setError(err.message)}finally{setBusy(false)}}
 return <form onSubmit={submit} className="auth-form"><h1>Choose your password</h1>
  <p className="muted">Your workspace owner gave you a temporary password. Choose one only you know before continuing.</p>
  <PasswordField label="Temporary password" value={current} onChange={setCurrent} autoComplete="current-password" disabled={busy}/>
  <PasswordField label="New password" value={password} onChange={setPassword} autoComplete="new-password" minLength={12} disabled={busy}/>
  <PasswordField label="Repeat new password" value={repeat} onChange={setRepeat} autoComplete="new-password" minLength={12} disabled={busy}/>
  {error&&<p role="alert" className="auth-error">{error}</p>}
  <button className="btn primary" disabled={busy}>{busy?'Saving…':'Save and continue'}</button>
 </form>;
}
