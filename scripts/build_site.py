"""Builds site/index.html: a static mailbox of the sample results, for hosts like Vercel.

Vercel cannot run Streamlit, so this replays the samples offline and saves the results. The page
has no server, keys or live lookups, and it only contains masked, pseudonymized sample data.

Usage: python scripts/build_site.py [output_dir]
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clearance.config import FIXTURES_DIR, ROOT, load_policy
from clearance.crustdata import CrustdataClient
from clearance.extract import extract_regex
from clearance.mailbox import sample_inbox
from clearance.pipeline import run_case
from clearance.report import mask, person_names


def build_data() -> list[dict]:
    policy = load_policy()
    mails = []
    outbox = Path(tempfile.mkdtemp())
    for item in sample_inbox(ROOT / "packets"):
        client = CrustdataClient(None, FIXTURES_DIR, offline=True)
        r = run_case(item.text, client, policy, policy["offline_as_of"], case_id=item.id, outbox=outbox)
        claim = extract_regex(item.text)
        names = [*person_names(r), *(n for n in (claim.sender_name, claim.signature_name) if n)]
        names += [re.sub(r"<[^>]*>", " ", item.sender).strip(" \"'")] if item.sender != "Unknown sender" else []
        show = lambda s: mask(str(s), sorted(set(names), key=len, reverse=True))
        ledger = {e.id: e for e in r.ledger}
        case = r.finance_case
        mails.append({
            "id": item.id,
            "subject": show(item.subject),
            "sender": show(item.sender),
            "text": show(item.text),
            "outcome": r.decision.outcome,
            "reasons": [show(x) for x in r.decision.reasons],
            "change": show(r.decision.what_would_change_this),
            "chain": r.chain_ok,
            "records": len(r.ledger),
            "findings": [
                {"rule": f.rule_id, "status": f.status, "severity": f.severity, "message": show(f.message), "evidence": [
                    {"id": i, "source": ledger[i].source, "endpoint": ledger[i].endpoint or "", "field": ledger[i].field_path or "",
                     "value": show(ledger[i].value)[:120], "hash": ledger[i].hash[:12]}
                    for i in f.evidence_ids if i in ledger
                ]}
                for f in r.findings
            ],
            "callback": [show(s.text) for s in r.callback_pack],
            "documents": r.document_requests,
            "candidates": [
                {"name": c.name, "domain": c.primary_domain, "size": c.employee_count_range}
                for c in (r.resolution.candidates if r.resolution and r.resolution.status == "AMBIGUOUS" else [])
            ],
            "drafts": [{"subject": show(d.subject), "body": show(d.body), "checks": d.checks} for d in r.drafts if d.status == "DRAFT_ONLY"],
            "case": {
                "route": case.route_to, "hours": case.respond_within_hours, "release": case.release_requires,
                "needed": case.approvals_needed, "account": case.account, "bank": case.bank_change,
                "flags": [{"rule": f.rule_id, "severity": f.severity, "message": show(f.message)} for f in case.flags],
                "unverified": case.unverified,
            } if case else None,
        })
    return mails


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clearance Demo</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#dfe3e8;--text:#17202a;--mute:#5d6875;--accent:#c7372f;--code:#eef0f3;
--ok-bg:#e4f5ea;--ok:#17663a;--warn-bg:#fdf3d7;--warn:#7a5600;--bad-bg:#fbe5e3;--bad:#a12a22}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#181c22;--line:#2c323a;--text:#e8ebef;--mute:#9aa4af;--accent:#ef5a50;--code:#22272e;
--ok-bg:#12301f;--ok:#7bdca0;--warn-bg:#352b0c;--warn:#f0cf70;--bad-bg:#3a1715;--bad:#ff9a92}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0;font-size:20px}header p{margin:2px 0 0;color:var(--mute)}
.notice{padding:9px 20px;background:var(--code);color:var(--mute);font-size:13px;border-bottom:1px solid var(--line)}
.stats{display:flex;flex-wrap:wrap;gap:8px;padding:12px 20px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 14px;font-size:13px;color:var(--mute)}.stat b{color:var(--text);font-size:17px;margin-right:5px}
main{display:grid;grid-template-columns:minmax(260px,340px) minmax(0,1fr);gap:14px;padding:14px 20px 40px;align-items:start}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
button{font:inherit;cursor:pointer;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:6px 10px}
button:disabled{opacity:.5;cursor:not-allowed}button.on,.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.mail{display:block;width:100%;text-align:left;margin-bottom:7px;padding:10px 12px}.mail small{display:block;color:var(--mute);overflow-wrap:anywhere}.mail.on small{color:#fff}
.mail strong{display:block;font-weight:600;overflow-wrap:anywhere}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}.APPROVE{background:var(--ok)}.HOLD{background:#d39e00}.ESCALATE{background:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;min-width:0}
h2{margin:0 0 2px;font-size:19px;overflow-wrap:anywhere}h3{margin:18px 0 6px;font-size:15px}.sub{color:var(--mute);font-size:13px;overflow-wrap:anywhere}
.verdict{border-radius:8px;padding:12px 14px;margin:12px 0}.verdict p{margin:4px 0;overflow-wrap:anywhere}.verdict b{font-size:16px}
.v-APPROVE{background:var(--ok-bg);color:var(--ok)}.v-HOLD{background:var(--warn-bg);color:var(--warn)}.v-ESCALATE{background:var(--bad-bg);color:var(--bad)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 4px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:8px 0}.kv div small{display:block;color:var(--mute)}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0;overflow-wrap:anywhere}
pre{background:var(--code);border-radius:7px;padding:11px;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,Menlo,monospace;margin:6px 0}
details{border:1px solid var(--line);border-radius:7px;padding:8px 11px;margin:6px 0}summary{cursor:pointer;overflow-wrap:anywhere}
.ev{font-size:12.5px;color:var(--mute);margin:5px 0;overflow-wrap:anywhere}.ev code{background:var(--code);padding:1px 4px;border-radius:4px}
input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 9px;width:100%;max-width:260px;display:block;margin:4px 0 8px}
.err{color:var(--bad);margin:6px 0}.info{background:var(--code);border-radius:7px;padding:8px 11px;margin:8px 0}
@media (max-width:760px){main{grid-template-columns:minmax(0,1fr);padding:12px 16px 32px}header,.notice{padding-left:16px;padding-right:16px}.stats{padding:12px 16px 0}}
</style>
</head>
<body>
<header><h1>Clearance</h1><p>Is the person asking for this payment real, and who should you call instead?</p></header>
<div class="notice">Static demo: saved results for synthetic sample emails. People's names are pseudonymized and masked, and nothing here checks new mail or sends anything. Bank accounts are not verified. These are risk findings, not accusations.</div>
<div class="stats" id="stats"></div>
<main><section><div class="filters" id="filters"></div><div id="list"></div></section><section class="panel" id="pane"></section></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const mails=JSON.parse(document.getElementById('data').textContent);
const VIEW={APPROVE:'Approved',HOLD:'Needs review',ESCALATE:'Escalated'};
const S={sel:(mails.find(m=>m.subject.includes('INV-7731'))||mails[0]).id,view:'All',tab:'handoff',cases:{},error:''};
function el(tag,props,...kids){const n=document.createElement(tag);for(const[k,v]of Object.entries(props||{})){if(k==='on')n.onclick=v;else if(k==='cls')n.className=v;else n[k]=v}
for(const c of kids.flat()){if(c!=null)n.append(c)}return n}
const same=(a,b)=>a.trim().replace(/\s+/g,' ').toLowerCase()===b.trim().replace(/\s+/g,' ').toLowerCase();
function caseState(m){return S.cases[m.id]||(S.cases[m.id]={status:'BLOCKED (simulated)',approvals:[],closed:false})}
function act(m,action,name){const c=caseState(m);S.error='';name=(name||'').trim();
if(!name){S.error='Enter your name before recording an action.';return}
const ok=c.approvals.filter(a=>a.action==='Confirmed legitimate').map(a=>a.name);
if(action==='Confirmed legitimate'&&ok.some(n=>same(n,name))){S.error='You already approved this. A different person must give the second approval.';return}
c.approvals.push({name,action});
if(action==='Confirmed fraud'){c.status='BLOCKED, reported to security (simulated)';c.closed=true}
else if(action==="Can't reach vendor"){c.status='BLOCKED, callback still needed (simulated)'}
else if(ok.length+1>=m.case.needed){c.status=`RELEASED by ${m.case.needed>1?'two approvers':'AP'} after callback (simulated)`;c.closed=true}
else{c.status='BLOCKED, second approver needed (simulated)'}}
function handoff(m){if(!m.case)return[el('p',{textContent:'No finance handoff is needed: the payment continues, with this evidence attached (simulated).'})];
const c=caseState(m);let input;const out=[el('p',{cls:'sub',textContent:'Simulated: finance was not contacted and no payment system was touched.'}),
el('div',{cls:'kv'},el('div',null,el('small',{textContent:'Route to'}),m.case.route),el('div',null,el('small',{textContent:'Respond within'}),m.case.hours+' hours'),el('div',null,el('small',{textContent:'Payment'}),c.status)),
el('p',{cls:'sub',textContent:'Release requires: '+m.case.release})];
if(m.case.bank)out.push(el('p',{textContent:'Bank change requested'+(m.case.account?', account '+m.case.account:'')+'.'}));
out.push(el('ul',null,m.case.flags.map(f=>el('li',null,el('b',{textContent:f.rule}),` (${f.severity}): ${f.message}`))));
if(m.case.unverified.length)out.push(el('p',{cls:'sub',textContent:'Could not verify: '+m.case.unverified.join(', ')}));
c.approvals.forEach(a=>out.push(el('p',{cls:'sub',textContent:`${a.action}, by ${a.name} (typed name)`})));
if(c.closed){out.push(el('div',{cls:'info',textContent:'Closed. Payment: '+c.status}))}else{
input=el('input',{placeholder:'Your name','aria-label':'Your name',value:S.typed||'',oninput:e=>{S.typed=e.target.value}});
out.push(el('h3',{textContent:'Record the outcome of the callback'}),input,el('div',{cls:'tabs'},['Confirmed legitimate','Confirmed fraud',"Can't reach vendor"].map(a=>el('button',{textContent:a,on:()=>{act(m,a,input.value);render()}}))));
if(S.error)out.push(el('div',{cls:'err',textContent:S.error}));}
return out}
function evidence(m){const icon={PASS:'Pass',FLAG:'Flag',UNVERIFIABLE:'Unverifiable'};
return[...m.findings.map(f=>el('details',{open:f.status==='FLAG'},el('summary',{textContent:`${f.rule}: ${icon[f.status]}${f.status==='FLAG'?' ('+f.severity+')':''}`}),el('p',{textContent:f.message}),
f.evidence.map(e=>el('div',{cls:'ev'},el('code',{textContent:e.id}),` ${e.source} ${e.endpoint} ${e.field}: ${e.value} · hash ${e.hash}`)))),
el('h3',{textContent:'What would change this decision?'}),el('p',{textContent:m.change}),el('p',{cls:'sub',textContent:`${m.records} evidence records. Hash chain ${m.chain?'verified':'BROKEN'}. Sent by the agent: 0.`})]}
function callback(m){const out=[];if(m.callback.length)out.push(el('h3',{textContent:'Who to call instead'}),el('ul',null,m.callback.map(t=>el('li',{textContent:t}))));
if(m.candidates.length)out.push(el('h3',{textContent:'Candidates (none picked)'}),el('ul',null,m.candidates.map(c=>el('li',{textContent:`${c.name}, ${c.domain||'no domain on file'}, ${c.size||'size unknown'}`}))));
if(m.documents.length)out.push(el('h3',{textContent:'Documents to request'}),el('ul',null,m.documents.map(d=>el('li',{textContent:d}))));
m.drafts.forEach(d=>out.push(el('h3',{textContent:'Draft (never sent)'}),el('pre',{textContent:`Subject: ${d.subject}\n\n${d.body}`}),el('ul',null,d.checks.map(c=>el('li',{textContent:`${c.passed?'Pass':'FAIL'}: ${c.check}`})))));
if(!out.length)out.push(el('p',{textContent:'Nothing to do: this request was approved.'}));return out}
function render(){const count=o=>mails.filter(m=>m.outcome===o).length;
document.getElementById('stats').replaceChildren(...[['Needs review','HOLD'],['Escalated','ESCALATE'],['Approved','APPROVE']].map(([l,o])=>el('div',{cls:'stat'},el('b',{textContent:count(o)}),l)));
document.getElementById('filters').replaceChildren(...['All','Needs review','Escalated','Approved'].map(v=>el('button',{textContent:v,cls:S.view===v?'on':'',on:()=>{S.view=v;render()}})));
document.getElementById('list').replaceChildren(...mails.filter(m=>S.view==='All'||VIEW[m.outcome]===S.view).map(m=>el('button',{cls:'mail'+(m.id===S.sel?' on':''),'aria-label':`${VIEW[m.outcome]}: ${m.subject}`,on:()=>{S.sel=m.id;S.error='';S.typed='';render()}},
el('small',null,el('span',{cls:'dot '+m.outcome}),m.sender),el('strong',{textContent:m.subject}))));
const m=mails.find(x=>x.id===S.sel);const tabs={handoff:'Finance handoff',evidence:'Findings and evidence',callback:'Callback and draft'};
document.getElementById('pane').replaceChildren(el('h2',{textContent:m.subject}),el('div',{cls:'sub',textContent:m.sender}),
el('div',{cls:'verdict v-'+m.outcome},el('b',{textContent:m.outcome}),m.reasons.map(r=>el('p',{textContent:r}))),
el('div',{cls:'tabs'},Object.entries(tabs).map(([k,l])=>el('button',{textContent:l,cls:S.tab===k?'on':'',on:()=>{S.tab=k;render()}}))),
...({handoff,evidence,callback}[S.tab])(m),
el('details',null,el('summary',{textContent:'Original email (plain text, links not clickable)'}),el('pre',{textContent:m.text})));}
render();
</script>
</body>
</html>
"""


def render_page(mails: list[dict]) -> str:
    # "<" is escaped so mail text can never close the script tag it is embedded in.
    data = json.dumps(mails, ensure_ascii=False).replace("<", "\\u003c")
    return PAGE.replace("__DATA__", data)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_page(build_data()))
    print(f"wrote {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
