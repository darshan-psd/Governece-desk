export type Role='Owner'|'Secretary'|'Accountant'|'Director'|'Client'|'Auditor';
export const reviewRoles:Role[]=['Owner','Secretary','Director'];
export const editRoles:Role[]=['Owner','Secretary','Accountant'];
export const eventTemplates:Record<string,string[]>={
 'Director appointment':['Obtain consent and disclosures','Review appointment resolution','Update officer register','Prepare applicable filing','Record acknowledgement'],
 'Director resignation':['Obtain resignation notice','Record board acknowledgement','Update officer register','Review applicable filings'],
 'Registered office change':['Collect address evidence','Approve office change','Update corporate records','Review applicable filings'],
 'Share allotment':['Review authority and terms','Approve allotment resolution','Update ownership register','Review applicable filings'],
 'Registration renewal':['Confirm renewal requirements','Collect supporting documents','Approve renewal submission','Record renewal evidence']
};
export function dateValid(v:string){return /^\d{4}-\d{2}-\d{2}$/.test(v)&&!isNaN(Date.parse(v))&&new Date(v+'T12:00:00Z').toISOString().slice(0,10)===v}
export function requireDate(v:string,label='Due date'){if(!dateValid(v))throw new Error(label+' must be a valid calendar date.');return v}
export function text(v:any,max=500){return String(v??'').trim().slice(0,max)}
export function list(v:any){return Array.isArray(v)?v:[]}
export function cleanChecklist(v:any){return list(v).slice(0,100).map((x:any)=>({id:text(x.id)||crypto.randomUUID(),label:text(x.label),done:!!x.done})).filter(x=>x.label)}
export function nextDate(date:string,frequency:string,step=1){requireDate(date);const d=new Date(date+'T12:00:00Z');const day=d.getUTCDate();const months=frequency==='Monthly'?1:frequency==='Quarterly'?3:frequency==='Annual'?12:0;if(!months)throw new Error('Choose Monthly, Quarterly or Annual.');d.setUTCDate(1);d.setUTCMonth(d.getUTCMonth()+months*step);d.setUTCDate(Math.min(day,new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth()+1,0)).getUTCDate()));return d.toISOString().slice(0,10)}
export function canAccess(member:any,record:any){return member.role==='Owner'||member.companies?.includes('*')||member.companies?.includes(record.kind==='company'?record.id:record.company)}
export function validateRecord(kind:string,input:any){
 const common={title:text(input.title),company:text(input.company),due:text(input.due),assignee:text(input.assignee),priority:['Normal','High','Urgent'].includes(input.priority)?input.priority:'Normal',notes:text(input.notes,12000)};
 if(kind==='company'){if(!text(input.name))throw new Error('Company name is required.');return {name:text(input.name),jurisdiction:text(input.jurisdiction),registration:text(input.registration),yearEnd:text(input.yearEnd),contact:text(input.contact),framework:text(input.framework)}}
 if(!common.title)throw new Error('Title is required.');if(!common.company)throw new Error('Choose a company.');requireDate(common.due);
 if(kind==='task'||kind==='close')return {...common,category:text(input.category)||'Compliance',checklist:cleanChecklist(input.checklist),dependencies:list(input.dependencies).map(String),evidence:list(input.evidence).map(String),filing:!!input.filing,acknowledgement:text(input.acknowledgement),source:text(input.source),trialbalance:text(input.trialbalance),period:text(input.period)};
 if(kind==='meeting'){const quorum=Number(input.quorum||1);if(!Number.isInteger(quorum)||quorum<1||quorum>100)throw new Error('Quorum must be a whole number between 1 and 100.');return {...common,location:text(input.location),quorum,agenda:text(input.agenda,12000),minutes:text(input.minutes,20000),attendees:list(input.attendees).slice(0,100).map((x:any)=>({name:text(x.name),present:!!x.present,conflict:!!x.conflict})).filter(x=>x.name),evidence:list(input.evidence).map(String)}}
 if(kind==='event'){if(!eventTemplates[input.eventType])throw new Error('Select a supported corporate event.');return {...common,eventType:input.eventType,effective:requireDate(input.effective,'Effective date'),evidence:list(input.evidence).map(String)}}
 if(kind==='request')return {...common,requestType:text(input.requestType)||'Information',response:'',evidence:[],status:'Open'};
 if(kind==='rule'){if(!['Monthly','Quarterly','Annual'].includes(input.frequency))throw new Error('Choose a recurrence.');if(!text(input.source))throw new Error('Record the source or internal policy reference.');const offset=Number(input.offset||0);if(!Number.isInteger(offset)||offset<0||offset>365)throw new Error('Deadline offset must be between 0 and 365 days.');return {...common,jurisdiction:text(input.jurisdiction),source:text(input.source),frequency:input.frequency,offset,ruleVersion:1,status:'Draft',scope:'Internal / manually reviewed'}}
 if(kind==='submission')return {...common,task:text(input.task),method:'Manual',reference:'',evidence:[],status:'Prepared'};
 throw new Error('Unsupported record type.');
}
export function reviewProblems(record:any,records:any[],actor:any){
 const problems:string[]=[];const docIds=list(record.evidence);const docs=records.filter(r=>r.kind==='document'&&docIds.includes(r.id)&&r.company===record.company);
 if(docs.length!==new Set(docIds).size)problems.push('All evidence must belong to this company.');
 if(!docs.length)problems.push('Attach at least one supporting document.');
 if(docs.some(d=>records.some(n=>n.kind==='document'&&n.previous===d.id)))problems.push('Replace superseded evidence with the latest document version.');
 if((record.preparer||'owner')===actor.id)problems.push('A different team member must approve the work.');
 if(list(record.dependencies).some(id=>records.find(r=>r.id===id)?.status!=='Completed'))problems.push('Complete all dependent tasks first.');
 if(list(record.checklist).some(x=>!x.done))problems.push('Complete every preparation checklist item.');
 if(record.kind==='meeting'){const eligible=new Set(list(record.attendees).filter(x=>x.present&&!x.conflict).map(x=>text(x.name).toLowerCase())).size;if(eligible<(record.quorum||1))problems.push('The configured quorum is not met after excluding declared conflicts.');if(!text(record.minutes))problems.push('Record the meeting minutes.');if(!text(record.agenda))problems.push('Record the meeting agenda.');}
 if(record.kind==='close'){const tb=records.find(r=>r.id===record.trialbalance&&r.company===record.company);if(!tb||tb.kind!=='trialbalance'||tb.status!=='Reviewed')problems.push('Link a reviewed trial balance for this company.');else if(records.some(r=>r.previousImport===tb.id))problems.push('Link the latest trial balance version.');else if(record.period&&record.period!==tb.period)problems.push('The close period must match the trial balance period.');if(records.some(r=>r.kind==='request'&&r.company===record.company&&r.status!=='Closed'))problems.push('Resolve outstanding company information requests.');}
 return problems;
}
export function escapeHTML(v:any){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!))}
export function csvCell(v:any){let s=String(v??'');if(/^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"'}
