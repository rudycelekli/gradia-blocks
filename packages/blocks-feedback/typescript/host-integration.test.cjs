// Compile host-integration.ts and dependencies, then set GRADIA_FEEDBACK_COMPILED_HOST_INTEGRATION.
const {test}=require('node:test');const assert=require('node:assert/strict');
const {configuredWidgetProps,feedbackWidgetProps}=require(process.env.GRADIA_FEEDBACK_COMPILED_HOST_INTEGRATION);
const configuration={block_id:'gradia.feedback',version:'0.1.0',configuration_digest:'test',features:{enabled:true,audio:true,photo:true,screenshot:false,video:true,triage:false},design:{accent:'#123456'},content:{title:'Report a problem',launcher_label:'Give feedback'},config:{categories:['issue','idea'],max_message_chars:10000,max_audio_bytes:128,max_attachments:2,max_attachment_bytes:4096,max_total_attachment_bytes:8192}};
test('host adapter maps server flags, categories and limits and requires client acknowledgment',async()=>{
 let observed;const client={getConfig:async()=>configuration,submit:async(input,key)=>{observed={input,key};return {status:'acknowledged',feedback:{id:'report',revision:2}};}};
 const props=await feedbackWidgetProps(client,'host-app');
 assert.equal(props.photoEnabled,true);assert.equal(props.screenshotEnabled,false);assert.equal(props.videoEnabled,true);assert.equal(props.audioEnabled,true);assert.equal(props.maxMessageChars,10000);
 assert.deepEqual(props.categories,['issue','idea']);assert.deepEqual(props.limits,{maxAudioBytes:128,maxAttachments:2,maxAttachmentBytes:4096,maxTotalAttachmentBytes:8192});
 const input={message:'Test',category:'issue',context:{app_id:'host-app'}};
 assert.deepEqual(await props.submit(input,'stable'),{delivery:'acknowledged',id:'report',revision:2});assert.deepEqual(observed,{input,key:'stable'});
 assert.equal(configuredWidgetProps(client,{...configuration,features:{...configuration.features,enabled:false}},'host-app').enabled,false);
});
