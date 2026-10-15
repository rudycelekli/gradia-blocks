// Compile media.ts to CommonJS, then set GRADIA_FEEDBACK_COMPILED_MEDIA.
const {test}=require('node:test');const assert=require('node:assert/strict');
const {mediaLimits,validateAttachment}=require(process.env.GRADIA_FEEDBACK_COMPILED_MEDIA);
const image=(size,type='image/png')=>new Blob([new Uint8Array(size)],{type});
const item=blob=>({id:'fixture',kind:'photo',blob});
test('media bounds reject empty, wrong MIME, per-item, count and aggregate overflow',()=>{
 const limits=mediaLimits({maxAttachmentBytes:8,maxTotalAttachmentBytes:10,maxAttachments:2});
 assert.equal(validateAttachment('photo',image(4),[],limits),null);
 assert.match(validateAttachment('photo',image(0),[],limits),/empty/);
 assert.match(validateAttachment('photo',image(4,'image/svg+xml'),[],limits),/PNG/);
 assert.match(validateAttachment('video',image(4),[],limits),/WebM/);
 assert.match(validateAttachment('photo',image(9),[],limits),/exceeds/);
 assert.match(validateAttachment('photo',image(4),[item(image(4)),item(image(4))],limits),/up to/);
 assert.match(validateAttachment('photo',image(7),[item(image(4))],limits),/total/);
 assert.equal(validateAttachment('video',image(4,'video/webm;codecs=vp8'),[],limits),null);
});
test('host limits can tighten but cannot exceed protocol hard bounds',()=>{
 const limits=mediaLimits({maxAttachments:100,maxAttachmentBytes:1e12,maxTotalAttachmentBytes:1e12,maxRecordingSeconds:100,maxAudioBytes:1e12});
 assert.deepEqual(limits,{maxAudioBytes:262144,maxAttachments:8,maxAttachmentBytes:16777216,maxTotalAttachmentBytes:33554432,maxRecordingSeconds:60});
 assert.equal(mediaLimits({maxAttachmentBytes:NaN,maxRecordingSeconds:0}).maxAttachmentBytes,5242880);
 assert.equal(mediaLimits({maxAudioBytes:128}).maxAudioBytes,128);
});
