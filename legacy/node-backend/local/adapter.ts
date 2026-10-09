import {DatabaseSync} from 'node:sqlite';
import {readFileSync,writeFileSync,existsSync,unlinkSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
const root=resolve(process.env.GOVERNANCE_DATA_DIR||'.sites-runtime/preview');
mkdirSync(resolve(root,'files'),{recursive:true});
const db=new DatabaseSync(resolve(root,'preview.sqlite'));
const has=db.prepare("SELECT name FROM sqlite_master WHERE name='records'").get();
if(!has)db.exec(readFileSync('drizzle/0000_redundant_luke_cage.sql','utf8'));
class Statement {constructor(public sql:string,public args:any[]=[] ){}bind(...args:any[]){return new Statement(this.sql,args)}async first(){return db.prepare(this.sql).get(...this.args)||null}async all(){return {results:db.prepare(this.sql).all(...this.args)}}run(){const r=db.prepare(this.sql).run(...this.args);return {meta:{changes:Number(r.changes)}}}
}
const path=(key:string)=>resolve(root,'files',encodeURIComponent(key));
export const env={LOCAL_PREVIEW:true,OPENAI_API_KEY:process.env.OPENAI_API_KEY,OPENAI_MODEL:process.env.OPENAI_MODEL,RESEND_API_KEY:process.env.RESEND_API_KEY,MAIL_FROM:process.env.MAIL_FROM,DB:{prepare:(sql:string)=>new Statement(sql),async batch(statements:Statement[]){db.exec('BEGIN');try{const result=statements.map(s=>s.run());db.exec('COMMIT');return result}catch(e){db.exec('ROLLBACK');throw e}}},BUCKET:{async put(key:string,bytes:ArrayBuffer){writeFileSync(path(key),Buffer.from(bytes))},async get(key:string){return existsSync(path(key))?{body:new Uint8Array(readFileSync(path(key)))}:null},async delete(key:string){if(existsSync(path(key)))unlinkSync(path(key))}}};
