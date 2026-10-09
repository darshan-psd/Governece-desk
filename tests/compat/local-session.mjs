// Preload used to run the original API suites (tests/phases.mjs, tests/client-portal.mjs) unchanged
// against the FastAPI backend. Those suites were written for the Node prototype, where a request
// without a cookie acted as the Owner. FastAPI requires a session, so this preload signs in the
// local-preview way (opening the app page) and sends that cookie when a request has none.
const base=process.env.TEST_BASE||'http://127.0.0.1:5175';
const original=globalThis.fetch;
const page=await original(base+'/');
const cookie=(page.headers.get('set-cookie')||'').split(';')[0];
if(!cookie)throw new Error('The API did not issue a local-preview session from '+base+'/');
globalThis.fetch=(input,init={})=>{const headers=new Headers(init.headers||{});if(!headers.has('cookie'))headers.set('cookie',cookie);return original(input,{...init,headers})};
