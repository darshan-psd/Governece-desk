// Preload used to run the original API suites (tests/phases.mjs, tests/client-portal.mjs) unchanged
// against the FastAPI backend. Those suites were written for the Node prototype, where a request
// without a cookie acted as the Owner. FastAPI requires a session, so this preload signs in the
// local-preview way (opening the app page) and sends that cookie when a request has none.
// Like the React app, every request also carries the gov_csrf cookie and writes carry the same
// value in X-CSRF-Token. A session's CSRF token is sha256('governance-csrf:' + session token).
import {createHash} from 'node:crypto';
const base=process.env.TEST_BASE||'http://127.0.0.1:5175';
const original=globalThis.fetch;
const page=await original(base+'/');
const session=page.headers.getSetCookie().map(c=>c.split(';')[0]).find(c=>c.startsWith('gov_preview='));
if(!session)throw new Error('The API did not issue a local-preview session from '+base+'/');
const csrfFor=cookieHeader=>{const m=/(?:^|;\s*)gov_preview=([^;]+)/.exec(cookieHeader);return m?createHash('sha256').update('governance-csrf:'+m[1]).digest('hex'):'anonymous-compat-token'};
globalThis.fetch=(input,init={})=>{
 const headers=new Headers(init.headers||{});
 // Suites that switch profile send their own session cookie; the CSRF token follows that session.
 let cookie=headers.get('cookie')||session;
 const token=csrfFor(cookie);
 if(!/(?:^|;\s*)gov_csrf=/.test(cookie))cookie+='; gov_csrf='+token;
 headers.set('cookie',cookie);
 if(!headers.has('x-csrf-token')&&!['GET','HEAD','OPTIONS'].includes((init.method||'GET').toUpperCase()))headers.set('x-csrf-token',token);
 return original(input,{...init,headers});
};
