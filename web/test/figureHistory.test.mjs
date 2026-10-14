import test from "node:test";
import assert from "node:assert/strict";
import { historyPath, checkHistoryPage, createHistorySession as actualSession, checkHistoryCapabilities, publicHistoryUrl } from "../src/figureHistory.js";
import { QueryError } from "../src/querySession.js";
const capabilities={api_contract_major:1,figure_history_supported:true,figure_history_contract_revision:1,figure_history_complete:false,figure_history_operations:["catalog","selected","timeline","activations","dependencies","source-events"]};
const createHistorySession=(change,read)=>actualSession(change,(path,signal)=>path==="capabilities"?Promise.resolve(capabilities):read(path,signal));
const context = { security_id: 7, release_id: 11 };
const selector = { metric_path: "名".repeat(40000), view: "STRICT", period: { kind: "UNKNOWN", start: null, end: null } };
const selection = { ...selector, state: "NOT_ASSESSED", value_decimal_text: null };
const endpoint = { release_id: 11, security_id: 7, selection, supporting_records: { inputs: "short-handle" } };
const catalog = () => ({ history_contract_revision: 1, operation: "catalog", security_id: 7, release: { release_id: 11 }, view: "STRICT", rows: [{ selector, selector_handle: "qualified-selector", endpoint, coverage: {} }], next_cursor: null, display_association: "NOT_ASSESSED", financial_readiness: "NOT_ASSESSED", earlier_history_complete: false });
const timeline = () => ({ history_contract_revision: 1, operation: "timeline", security_id: 7, release: { release_id: 11 }, selector, endpoint, rows: [], next_cursor: "next-empty-window", scanned_releases: 300, financial_readiness: "NOT_ASSESSED", earlier_history_complete: false });
const defer = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { resolve, reject, promise }; };

test("long path stays in JSON; first and continued requests use short selector handles", () => {
  for (const operation of ["timeline", "activations"]) {
    const path = historyPath(context, operation, { handle: "qualified-selector", cursor: "continuation" });
    assert.ok(path.length < 300); assert.equal(path.includes("名"), false);
    assert.equal(new URLSearchParams(path.split("?")[1]).get("handle"), "qualified-selector");
  }
  assert.equal(checkHistoryPage(catalog(), context, "catalog", { view: "STRICT" }).rows[0].selector.metric_path, selector.metric_path);
});
test("catalog and other operations have distinct bounded request shapes", () => {
  assert.match(historyPath(context, "catalog", { view: "CONSERVATIVE" }), /figures\?limit=50&view=CONSERVATIVE&release=11/);
  assert.match(historyPath(context, "source-events", { source_item_id: 9 }), /source-events/);
  assert.throws(() => historyPath(context, "timeline", { handle: "x".repeat(8193) }));
  assert.throws(() => historyPath(context, "catalog", { view: "guess" }));
  assert.throws(() => historyPath({ security_id: 0, release_id: 11 }, "catalog", { view: "STRICT" }));
});
test("sparse empty pages and bootstrap unavailable previous endpoints are valid", () => {
  const body=timeline(); assert.equal(checkHistoryPage(body, context, "timeline", { selector }),body);
  body.rows=[{release_id: 11, parent_release_id:null, previous:{release_id:null,security_id:7,selection:null}, replacement:endpoint, delta:{status:"NOT_COMPUTED",value_decimal_text:null}}];
  assert.equal(checkHistoryPage(body,context,"timeline",{selector}),body);
});
for (const value of ["-0.0000", "9007199254740993.123456789012345678901", "1E-20000", "1E+200000"]) test(`exact spelling ${value}`, () => {
  const body=catalog(); body.rows[0].endpoint={...endpoint,selection:{...selection,value_decimal_text:value,state:"REPORTED"}};
  assert.equal(checkHistoryPage(body,context,"catalog",{view:"STRICT"}).rows[0].endpoint.selection.value_decimal_text,value);
});
for (const mutate of [b=>b.security_id=8,b=>b.release.release_id=12,b=>b.view="ASSUMED",b=>b.display_association="MATCHED",b=>b.rows[0].selector_handle=null,b=>b.rows[0].endpoint.selection.value_decimal_text=123,b=>b.rows=Array(51).fill(b.rows[0])]) test("reject malformed or unbound catalog",()=>{
  const b=structuredClone(catalog());mutate(b);assert.throws(()=>checkHistoryPage(b,context,"catalog",{view:"STRICT"}));
});
test("old success and old409 cannot overwrite a later request",async()=>{
  const first=defer(),second=defer();let state;let count=0;
  const session=createHistorySession(s=>state=s,()=>++count===1?first.promise:second.promise);
  const old=session.start(context,"catalog",{view:"STRICT"});
  await new Promise(resolve=>setImmediate(resolve));
  const latest=session.start(context,"catalog",{view:"STRICT"});
  second.resolve(catalog());await latest;first.reject(new QueryError("old restart",409));await old;
  assert.equal(state.error,null);assert.equal(state.rows.length,1);
});
test("close cancels ignored success; independent support cancellation leaves catalog untouched",async()=>{
  const request=defer();let state;const session=createHistorySession(s=>state=s,()=>request.promise);
  const pending=session.start(context,"catalog",{view:"STRICT"});session.cancel();request.resolve(catalog());await pending;assert.equal(state.page,null);
  let catalogState;const catalogSession=createHistorySession(s=>catalogState=s,async()=>catalog());await catalogSession.start(context,"catalog",{view:"STRICT"});session.cancel();assert.equal(catalogState.rows.length,1);
});
test("empty continuation accumulates correctly and every request retains handle",async()=>{
  let state;const paths=[];const session=createHistorySession(s=>state=s,async path=>{paths.push(path);const body=timeline();if(paths.length===2){body.rows=[{release_id:11,parent_release_id:null,previous:{release_id:null,security_id:7,selection:null},replacement:endpoint,delta:{status:"NOT_COMPUTED",value_decimal_text:null}}];body.next_cursor=null;}return body;});
  await session.start(context,"timeline",{handle:"qualified-selector",selector});await session.more();assert.equal(state.rows.length,1);assert.ok(paths.every(p=>p.includes("handle=qualified-selector")));
});
test("journal409 remains journal-only; timeline409 never requests source restart",async()=>{
  for(const operation of ["timeline","source-events"]){let state;const s=createHistorySession(v=>state=v,async()=>{throw new QueryError("changed",409);});await s.start(context,operation,{handle:"qualified-selector",selector,source_item_id:3});assert.equal(state.restartJournal,operation==="source-events");}
});
test("URLs admit only safe public SEC or EDINET locations",()=>{
  for(const value of ["https://user:pass@www.sec.gov/a","https://www.sec.gov/a?key=secret","https://www.sec.gov/a#x","javascript:alert(1)","file:///a","https://private.example/a"])assert.equal(publicHistoryUrl(value),null);
  assert.equal(publicHistoryUrl("https://www.sec.gov/Archives/a.htm"),"https://www.sec.gov/Archives/a.htm");
});

test("unsupported service capabilities cannot reach figure reads",async()=>{
  let state;const paths=[];const session=actualSession(v=>state=v,async path=>{paths.push(path);return {...capabilities,figure_history_operations:["timeline"]};});
  await session.start(context,"catalog",{view:"STRICT"});assert.deepEqual(paths,["capabilities"]);assert.match(state.error,/unavailable/);
  assert.throws(()=>checkHistoryCapabilities({...capabilities,figure_history_complete:true}));
});

test("redacted identifier is unavailable, never reconstructed from another label",()=>{
  const body=structuredClone(catalog());body.rows[0].selector.metric_path=null;body.rows[0].endpoint.selection.metric_path=null;
  assert.equal(checkHistoryPage(body,context,"catalog",{view:"STRICT"}).rows[0].selector.metric_path,null);
});

for(const [name,mutate] of [
 ["pin",b=>b.endpoint.release_id=99], ["security",b=>b.endpoint.security_id=8], ["figure",b=>b.endpoint.selection.metric_path="other"],
 ["previous release",b=>b.rows[0].previous.release_id=9], ["previous view",b=>b.rows[0].previous.selection.view="ASSUMED"],
 ["replacement period",b=>b.rows[0].replacement.selection.period.kind="INSTANT"], ["replacement security",b=>b.rows[0].replacement.security_id=8]
])test(`reject endpoint identity mismatch ${name}`,()=>{
 const body=structuredClone(timeline());body.rows=[{release_id:11,parent_release_id:10,previous:{...structuredClone(endpoint),release_id:10},replacement:structuredClone(endpoint),delta:{status:"NOT_COMPUTED",value_decimal_text:null}}];
 mutate(body);assert.throws(()=>checkHistoryPage(body,context,"timeline",{selector}));
});
test("different ancestor values and units keep their own actual context",()=>{
 const body=structuredClone(timeline());body.rows=[{release_id:11,parent_release_id:10,previous:{...structuredClone(endpoint),release_id:10},replacement:structuredClone(endpoint),delta:{status:"NOT_COMPUTED",value_decimal_text:null}}];
 body.rows[0].previous.selection.value_decimal_text="90.0000";body.rows[0].previous.selection.unit={currency:"JPY"};
 assert.equal(checkHistoryPage(body,context,"timeline",{selector}),body);
});
