// Runs the original Node-era API suites against a FastAPI server started on a throwaway data folder.
import {spawn,execFileSync} from 'node:child_process';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
const port=process.env.COMPAT_PORT||'5175',base='http://127.0.0.1:'+port,data=mkdtempSync(join(tmpdir(),'governance-compat-'));
const api=spawn(resolve('backend/.venv/bin/python'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',port],{cwd:'backend',stdio:['ignore','ignore','inherit'],env:{...process.env,GOVERNANCE_ENVIRONMENT:'test',GOVERNANCE_DATA_DIR:data,PORT:port,GOVERNANCE_REMINDERS_ENABLED:'false'}});
try{
 for(let i=0;;i++){try{await fetch(base+'/api/v2');break}catch{if(i>100)throw new Error('FastAPI did not start');await new Promise(r=>setTimeout(r,200))}}
 for(const suite of ['tests/phases.mjs','tests/client-portal.mjs']){
  const output=execFileSync(process.execPath,['--import','./tests/compat/local-session.mjs',suite],{env:{...process.env,TEST_BASE:base},encoding:'utf8'});
  const {checks,passed}=JSON.parse(output);console.log(`${suite}: ${checks} checks passed (${passed.length} scenarios)`);
 }
}finally{api.kill();rmSync(data,{recursive:true,force:true})}
