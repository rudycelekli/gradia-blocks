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
