// Run after compiling client.ts as CommonJS; set GRADIA_FEEDBACK_COMPILED_CLIENT.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {FeedbackClient, FeedbackTransportUncertain, FeedbackHttpError} = require(process.env.GRADIA_FEEDBACK_COMPILED_CLIENT);
const response = (value, status = 200) => async () => new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
const client = (fetch, token = () => 'server-only-fixture') => new FeedbackClient({baseUrl:'http://127.0.0.1:3191', token, fetch, timeoutMs:15});

test('POST and PATCH require explicit positive revision acknowledgment', async () => {
  for (const payload of [{}, {status:'acknowledged',feedback:{id:'x',revision:0}}, {status:'acknowledged',feedback:{id:' ',revision:1}}, {status:'acknowledged',feedback:{id:'x',revision:-1}}]) {
    const c = client(response(payload));
    await assert.rejects(c.submit({message:'x'},'stable-key'), FeedbackTransportUncertain);
    await assert.rejects(c.triage('x',{expected_revision:1,status:'resolved'}), FeedbackTransportUncertain);
  }
  const c = client(response({status:'acknowledged',feedback:{id:'x',revision:1}}));
  assert.equal((await c.triage('x',{expected_revision:1,status:'resolved'})).status,'acknowledged');
});
test('deadline bounds credential provider and noncooperative fetch', async () => {
  const hanging = () => new Promise(() => {});
  await assert.rejects(client(response({}), hanging).getConfig(), FeedbackTransportUncertain);
  await assert.rejects(client(hanging).getConfig(), FeedbackTransportUncertain);
});
test('known HTTP refusal is typed and no automatic replay occurs', async () => {
  let calls=0;
  const c=client(async () => {calls++;return new Response(JSON.stringify({error:{code:'revision_conflict',message:'Refresh revision'}}),{status:409});});
  await assert.rejects(c.triage('x',{expected_revision:1}), error => error instanceof FeedbackHttpError && error.code==='revision_conflict');
  assert.equal(calls,1);
});
test('attachment download is authenticated binary with deadline and typed refusal',async()=>{
 let observed;
 const c=client(async(url,options)=>{observed={url,options};return new Response(new Uint8Array([1,2,3]),{headers:{'Content-Type':'image/png'}});});
 const blob=await c.downloadAttachment('report/id','attachment/id');
 assert.equal(blob.type,'image/png');assert.equal(blob.size,3);
 assert.match(observed.url,/report%2Fid\/attachments\/attachment%2Fid$/);assert.doesNotMatch(observed.url,/fixture|token|Bearer/);
 assert.equal(observed.options.headers.Authorization,'Bearer server-only-fixture');
 assert.equal(observed.options.redirect,'error');
 await assert.rejects(client(response({error:{code:'not_found',message:'Absent'}},404)).downloadAttachment('x','y'),error=>error instanceof FeedbackHttpError&&error.code==='not_found');
 await assert.rejects(client(()=>new Promise(()=>{})).downloadAttachment('x','y'),FeedbackTransportUncertain);
});
test('media submission preserves typed attachment payload and acknowledgment',async()=>{
 const input={message:'visual issue',attachments:[{kind:'screenshot',mime_type:'image/png',data_base64:'AQID'}]};let observed;
 const c=client(async(url,options)=>{observed=JSON.parse(options.body);return new Response(JSON.stringify({status:'acknowledged',feedback:{id:'report',revision:1,attachments:[{id:'media'}]},replayed:false}));});
 assert.equal((await c.submit(input,'same-request')).feedback.attachments[0].id,'media');assert.deepEqual(observed,input);
});
test('binary download refuses declared and streamed oversized bodies',async()=>{
 await assert.rejects(client(async()=>new Response('x',{headers:{'Content-Length':'16777217'}})).downloadAttachment('x','y'),FeedbackTransportUncertain);
 const chunk=new Uint8Array(100000);let delivered=0,cancelled=false;
 const c=client(async()=>new Response(new ReadableStream({pull(controller){delivered++;controller.enqueue(chunk);},cancel(){cancelled=true;}})));
 await assert.rejects(c.downloadAudio('x'),FeedbackTransportUncertain);assert.equal(cancelled,true);assert.ok(delivered<8);
});
