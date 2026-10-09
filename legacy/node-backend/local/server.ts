import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
import {randomBytes} from 'node:crypto';
import * as portal from '../app/api/portal/route';
import * as workspace from '../app/api/v2/route';
import * as files from '../app/api/v2-files/route';
import * as imports from '../app/api/v2-import/route';
import {allRecords,runAutomation} from '../lib/phase-service';
const routes:any={'/api/portal':portal,'/api/v2':workspace,'/api/v2-files':files,'/api/v2-import':imports};
const port=Number(process.env.PORT||5174);const origin='http://127.0.0.1:'+port;const sessions=new Map<string,string>();
const server=createServer(async(req,res)=>{try{
 if(req.headers.host!==new URL(origin).host){res.writeHead(403);res.end('Host not allowed');return}
 const path=new URL(req.url!,origin).pathname;
 if(req.headers.origin&&req.headers.origin!==origin){res.writeHead(403);res.end('Origin not allowed');return}
 const chunks:Buffer[]=[];let size=0;for await(const c of req){size+=c.length;if(size>12*1024*1024){res.writeHead(413);res.end('Upload limit exceeded');return}chunks.push(c)}const body=Buffer.concat(chunks);
 const cookie=/(?:^|;\s*)gov_preview=([^;]+)/.exec(req.headers.cookie||'')?.[1];const actor=cookie?sessions.get(cookie)||'owner':'owner';
 if(path==='/api/session'&&req.method==='POST'){const requested=JSON.parse(body.toString()).actor;const members=await allRecords('local-preview');if(requested!=='owner'&&!members.some(r=>r.kind==='member'&&r.id===requested&&r.active!==false)){res.writeHead(403);res.end(JSON.stringify({error:'Unknown local profile'}));return}const token=randomBytes(32).toString('hex');sessions.set(token,requested);res.writeHead(200,{'Content-Type':'application/json','Set-Cookie':`gov_preview=${token}; HttpOnly; SameSite=Strict; Path=/`});res.end('{"ok":true}');return}
 if(routes[path]){const headers=new Headers();for(const [k,v] of Object.entries(req.headers))if(v)headers.set(k,Array.isArray(v)?v.join(','):v);headers.set('oai-authenticated-user-id','local-preview');headers.set('oai-authenticated-user-email','Local preview');headers.set('x-governance-actor',actor);const r=new Request(origin+req.url,{method:req.method,headers,...(body.length?{body}:{})});const handler=routes[path][req.method!];const response=handler?await handler(r):new Response('Method not allowed',{status:405});res.writeHead(response.status,Object.fromEntries(response.headers.entries()));res.end(Buffer.from(await response.arrayBuffer()));return}
 const assets:Record<string,[string,string]>={'/':['index.html','text/html'],'/app.js':['app.js','text/javascript'],'/style.css':['style.css','text/css']};const asset=assets[path];if(!asset){res.writeHead(404);res.end();return}res.writeHead(200,{'Content-Type':asset[1],'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(readFileSync('.sites-runtime/preview/'+asset[0]));
 }catch(e){console.error(e);res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Unable to complete the local request.'}))}});
server.listen(port,'127.0.0.1',()=>console.log('Governance Desk local workspace: '+origin));
const ticker=setInterval(()=>runAutomation('local-preview').catch(e=>console.error('Reminder check:',e.message)),60000);ticker.unref();
