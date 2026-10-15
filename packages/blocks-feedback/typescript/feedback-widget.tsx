"use client";
import { useEffect, useId, useRef, useState } from "react";
import { baseMime, blobBase64, mediaLimits, screenshotBlob, validateAttachment, type AttachmentKind, type AttachmentMime, type MediaLimits, type PendingAttachment } from "./media";
import type { AudioMime } from "./client";
export type FeedbackInput = { message:string; category:string; context:{app_id:string}; audio?:{mime_type:AudioMime;data_base64:string}; attachments?:{kind:AttachmentKind;mime_type:AttachmentMime;data_base64:string}[] };
export type FeedbackDelivery = { delivery:"preview"|"acknowledged";id:string;revision:number };
export type FeedbackWidgetProps = {
 enabled?:boolean; categories?:readonly string[]; maxMessageChars?:number; heading?:string; messageLabel?:string; successMessage?:string; buttonLabel?:string; accent?:string; radius?:number; density?:"comfortable"|"compact"; audioEnabled?:boolean; categoriesEnabled?:boolean; photoEnabled?:boolean; screenshotEnabled?:boolean; videoEnabled?:boolean; appId?:string;
 /** Host and server limits must agree. Browser values can tighten but never replace server enforcement. */
 limits?:Partial<MediaLimits>; submit:(input:FeedbackInput,idempotencyKey:string)=>Promise<FeedbackDelivery>;
};
const panel:React.CSSProperties={marginTop:20,border:"1px solid #d6d0c7",padding:16};
const button:React.CSSProperties={minHeight:44,padding:"8px 14px",border:"1px solid #d6d0c7",borderRadius:6};
function AttachmentPreview({item,remove,disabled}:{item:PendingAttachment;remove:()=>void;disabled:boolean}){
 const [url,setUrl]=useState("");
 useEffect(()=>{const next=URL.createObjectURL(item.blob);setUrl(next);return()=>URL.revokeObjectURL(next);},[item.blob]);
 return <div style={{marginTop:12,padding:12,border:"1px solid #d6d0c7"}}><p>{item.kind} · {Math.ceil(item.blob.size/1024)} KiB</p>{url&&(item.kind==="video"?<video controls preload="metadata" src={url} style={{maxWidth:"100%",maxHeight:240}}/>:<img src={url} alt={`Attached ${item.kind} preview`} style={{maxWidth:"100%",maxHeight:240,objectFit:"contain"}}/>)}<button type="button" disabled={disabled} onClick={remove} style={button}>Remove {item.kind}</button></div>;
}
type CaptureKind="audio"|"video"|"screenshot";
type CaptureSession={kind:CaptureKind;stream:MediaStream;recorder?:MediaRecorder;timer?:ReturnType<typeof setTimeout>;discard:boolean};
/** Portable React reference. Host supplies transport, identity, storage and credentials. No capture/upload on mount. */
export function FeedbackWidget({enabled=true,categories=["general","bug","idea"],maxMessageChars=8000,heading="Tell us what could work better.",messageLabel="Your feedback",successMessage="Your feedback was acknowledged by the server.",buttonLabel="Send feedback",accent="#a84416",radius=6,density="comfortable",audioEnabled=false,categoriesEnabled=true,photoEnabled=false,screenshotEnabled=false,videoEnabled=false,appId="reference-preview",limits:requestedLimits,submit}:FeedbackWidgetProps){
 const uid=useId();const choices=categories.filter(value=>typeof value==="string"&&value.trim()).slice(0,20);if(!choices.length)choices.push("general");const messageLimit=Number.isFinite(maxMessageChars)?Math.min(10000,Math.max(1,Math.floor(maxMessageChars))):8000;const limits=mediaLimits(requestedLimits);
 const [message,setMessage]=useState("");const [category,setCategory]=useState<FeedbackInput["category"]>("general");const [clip,setClip]=useState<Blob|null>(null);const [clipUrl,setClipUrl]=useState("");
 const [attachments,setAttachments]=useState<PendingAttachment[]>([]);const [captureKind,setCaptureKind]=useState<CaptureKind|null>(null);const [acquiring,setAcquiring]=useState(false);const [busy,setBusy]=useState(false);const [notice,setNotice]=useState("");const [problem,setProblem]=useState("");
 const capture=useRef<CaptureSession|null>(null);const acquiringRef=useRef(false);const busyRef=useRef(false);const mounted=useRef(true);const attachmentsRef=useRef(attachments);attachmentsRef.current=attachments;const limitsRef=useRef(limits);limitsRef.current=limits;
 const allowed=useRef({audio:enabled&&audioEnabled,photo:enabled&&photoEnabled,screenshot:enabled&&screenshotEnabled,video:enabled&&videoEnabled});allowed.current={audio:enabled&&audioEnabled,photo:enabled&&photoEnabled,screenshot:enabled&&screenshotEnabled,video:enabled&&videoEnabled};
 const sendingPolicy=useRef("");sendingPolicy.current=JSON.stringify({enabled,audioEnabled,photoEnabled,screenshotEnabled,videoEnabled,categoriesEnabled,choices,messageLimit,limits});const captureEpoch=useRef(0);const identity=useRef<{payload:string;key:string}|null>(null);
 function release(session:CaptureSession){if(session.timer)clearTimeout(session.timer);session.stream.getTracks().forEach(track=>track.stop());if(capture.current===session)capture.current=null;}
 function stop(discard=false){const session=capture.current;if(!session)return;session.discard ||= discard;if(session.timer)clearTimeout(session.timer);if(session.recorder&&session.recorder.state!=="inactive"){try{session.recorder.stop();}catch{release(session);}}else release(session);session.stream.getTracks().forEach(track=>track.stop());if(mounted.current)setCaptureKind(null);}
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;captureEpoch.current++;stop(true);};},[]);
 useEffect(()=>{captureEpoch.current++;const active=capture.current;if(active&&!allowed.current[active.kind])stop(true);if(!enabled||!audioEnabled)setClip(null);setAttachments(items=>items.filter(item=>allowed.current[item.kind]));},[enabled,audioEnabled,photoEnabled,screenshotEnabled,videoEnabled]);
 useEffect(()=>{if(!clip){setClipUrl("");return;}const url=URL.createObjectURL(clip);setClipUrl(url);return()=>URL.revokeObjectURL(url);},[clip]);
 function addAttachment(kind:AttachmentKind,blob:Blob){
  if(!mounted.current||!allowed.current[kind])return;
  const error=validateAttachment(kind,blob,attachmentsRef.current,limitsRef.current);if(error){setProblem(error);return;}
  const next=[...attachmentsRef.current,{id:crypto.randomUUID(),kind,blob}];attachmentsRef.current=next;setAttachments(next);setProblem("");
 }
 function acceptClip(blob:Blob){if(!mounted.current||!allowed.current.audio)return;if(!blob.size){setProblem("This audio note is empty.");return;}if(blob.size>limitsRef.current.maxAudioBytes){setProblem(`Audio exceeds ${Math.ceil(limitsRef.current.maxAudioBytes/1024)} KiB. Record a shorter note.`);return;}if(!["audio/webm","audio/ogg","audio/wav"].includes(baseMime(blob))){setProblem("Use a WebM, Ogg or WAV audio note.");return;}setClip(blob);setProblem("");}
 async function start(kind:CaptureKind,source:"microphone"|"camera"|"screen"){
  if(acquiringRef.current||capture.current||busyRef.current||!allowed.current[kind])return;
  if(kind!=="audio"&&attachmentsRef.current.length>=limitsRef.current.maxAttachments){setProblem("Remove an attachment before capturing another.");return;}
  acquiringRef.current=true;setAcquiring(true);setProblem("");setNotice("");
  const epoch=captureEpoch.current;let input:MediaStream|undefined;
  try{
   if(!navigator.mediaDevices)throw new Error("Media capture is unavailable. Attach a supported file instead.");
   if(kind!=="screenshot"&&typeof MediaRecorder==="undefined")throw new Error("Recording is unavailable. Attach a supported file instead.");
   if(source==="screen"){
    if(!navigator.mediaDevices.getDisplayMedia)throw new Error("Screen capture is unavailable. Attach an image or video instead.");
    // Browser permission picker is invoked directly from this explicit click. No tab preselection.
    input=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});
   }else input=await navigator.mediaDevices.getUserMedia(source==="camera"?{video:{width:{ideal:1280},height:{ideal:720}},audio:false}:{audio:true});
   if(!mounted.current||!allowed.current[kind]||epoch!==captureEpoch.current){input.getTracks().forEach(track=>track.stop());return;}
   const session:CaptureSession={kind,stream:input,discard:false};capture.current=session;
   if(kind==="screenshot"){
    setCaptureKind(kind);const image=await screenshotBlob(input);release(session);if(!session.discard)addAttachment("screenshot",image);if(mounted.current)setCaptureKind(null);return;
   }
   const mime=(kind==="audio"?["audio/webm","audio/ogg"]:["video/webm;codecs=vp8","video/webm","video/mp4"]).find(value=>MediaRecorder.isTypeSupported(value));
   if(!mime)throw new Error("This browser cannot record a supported format. Attach a file instead.");
   const recorder=new MediaRecorder(input,{mimeType:mime,...(kind==="video"?{videoBitsPerSecond:800000}: {})});session.recorder=recorder;const chunks:Blob[]=[];let bytes=0,oversized=false;
   const byteLimit=kind==="audio"?limitsRef.current.maxAudioBytes:Math.min(limitsRef.current.maxAttachmentBytes,limitsRef.current.maxTotalAttachmentBytes-attachmentsRef.current.reduce((n,item)=>n+item.blob.size,0));
   if(byteLimit<1)throw new Error("Attachments exceed the total byte limit. Remove an attachment first.");
   recorder.ondataavailable=event=>{bytes+=event.data.size;if(bytes>byteLimit){oversized=true;chunks.length=0;if(capture.current===session)stop();}else if(!oversized)chunks.push(event.data);};
   recorder.onerror=()=>{session.discard=true;if(capture.current===session)stop(true);if(mounted.current)setProblem("Recording failed. No clip was attached. Try a file or written note.");};
   recorder.onstop=()=>{release(session);if(!mounted.current)return;setCaptureKind(null);if(session.discard||!allowed.current[kind])return;if(oversized){setProblem("Recording exceeded the attachment byte limit. Record a shorter clip.");return;}const blob=new Blob(chunks,{type:mime});if(kind==="audio")acceptClip(blob);else addAttachment("video",blob);};
   input.getTracks().forEach(track=>track.addEventListener("ended",()=>{if(capture.current===session)stop();},{once:true}));
   recorder.start(250);setCaptureKind(kind);session.timer=setTimeout(()=>{if(capture.current===session)stop();},(kind==="audio"?15:limitsRef.current.maxRecordingSeconds)*1000);
  }catch(error){if(capture.current?.stream===input)stop(true);input?.getTracks().forEach(track=>track.stop());if(mounted.current){setCaptureKind(null);setProblem(error instanceof Error&&error.name!=="NotAllowedError"?error.message:"Capture permission was not granted. You can still attach a file or write a note.");}}
  finally{acquiringRef.current=false;if(mounted.current)setAcquiring(false);}
 }
 async function send(event:React.FormEvent){
  event.preventDefault();if(!enabled||busyRef.current||capture.current||acquiringRef.current)return;setProblem("");setNotice("");busyRef.current=true;setBusy(true);const policyAtStart=sendingPolicy.current;
  try{
   const input:FeedbackInput={message:message.trim(),category:categoriesEnabled&&choices.includes(category)?category:choices[0],context:{app_id:appId}};if(input.message.length>messageLimit)throw new Error("Message exceeds the configured character limit.");if(!input.message)throw new Error("Write a short message first.");
   if(audioEnabled&&clip){if(clip.size>limitsRef.current.maxAudioBytes)throw new Error("Audio exceeds the configured byte limit.");input.audio={mime_type:baseMime(clip) as AudioMime,data_base64:await blobBase64(clip)};}
   const selected=attachmentsRef.current.filter(item=>allowed.current[item.kind]);const accepted:PendingAttachment[]=[];
   for(const item of selected){const error=validateAttachment(item.kind,item.blob,accepted,limitsRef.current);if(error)throw new Error(error);accepted.push(item);}
   if(selected.length)input.attachments=await Promise.all(selected.map(async item=>({kind:item.kind,mime_type:baseMime(item.blob) as AttachmentMime,data_base64:await blobBase64(item.blob)})));
   if(!mounted.current||sendingPolicy.current!==policyAtStart)throw new Error("Feedback settings changed during preparation. Review your attachments and send again.");
   const payload=JSON.stringify(input);if(identity.current?.payload!==payload)identity.current={payload,key:crypto.randomUUID()};
   const result=await submit(input,identity.current.key);
   if(!result||!["preview","acknowledged"].includes(result.delivery)||typeof result.id!=="string"||!result.id.trim()||!Number.isInteger(result.revision)||result.revision<1)throw new Error("Delivery could not be confirmed. Reconcile the same request before retrying.");
   if(!mounted.current)return;setNotice(result.delivery==="preview"?"Added to this browser preview. Nothing was uploaded.":successMessage);setMessage("");setClip(null);attachmentsRef.current=[];setAttachments([]);identity.current=null;
  }catch(error){if(mounted.current)setProblem(error instanceof Error?error.message:"Delivery could not be confirmed. Reconcile before retrying.");}finally{busyRef.current=false;if(mounted.current)setBusy(false);}
 }
 const controlsDisabled=!enabled||busy||acquiring||captureKind!==null;
 const color=/^#[0-9a-f]{6}$/i.test(accent)?accent:"#a84416";
 const field:React.CSSProperties={width:"100%",padding:density==="compact"?"8px":"12px",border:"1px solid #d6d0c7",background:"#faf8f5",color:"#1a1815",borderRadius:Number.isFinite(radius)?Math.max(0,Math.min(32,radius)):6,font:"inherit"};
 const fileInput=(kind:"photo"|"video")=><><label htmlFor={uid+"-"+kind} style={{display:"block",marginTop:12}}>Attach {kind==="photo"?"PNG, JPEG or WebP image":"WebM or MP4 video"}</label><input id={uid+"-"+kind} type="file" accept={kind==="photo"?"image/png,image/jpeg,image/webp":"video/webm,video/mp4"} disabled={controlsDisabled} onChange={e=>{const file=e.target.files?.[0];if(file)addAttachment(kind,file);e.currentTarget.value="";}} style={{maxWidth:"100%",marginTop:8,fontSize:12}}/></>;
 function pasted(event:React.ClipboardEvent<HTMLFormElement>){
  if(!enabled||!photoEnabled||controlsDisabled)return;
  const files=Array.from(event.clipboardData.items).filter(item=>item.kind==="file"&&item.type.startsWith("image/")).map(item=>item.getAsFile()).filter((item):item is File=>item!==null);
  if(files.length){event.preventDefault();for(const file of files)addAttachment("photo",file);}
 }
 return <form onSubmit={send} onPaste={pasted} aria-label="Feedback block" style={{color:"#1a1815",fontFamily:"inherit"}}>
  <h3 style={{fontSize:"1.5rem",lineHeight:1.3,marginBottom:20}}>{heading}</h3>
  <label style={{display:"block",marginBottom:6}} htmlFor={uid+"-message"}>{messageLabel}</label><textarea id={uid+"-message"} value={message} onChange={e=>setMessage(e.target.value)} maxLength={messageLimit} required rows={4} style={field} disabled={!enabled||busy}/>
  {categoriesEnabled&&<div style={{marginTop:16}}><label htmlFor={uid+"-category"} style={{display:"block",marginBottom:6}}>Category</label><select id={uid+"-category"} value={choices.includes(category)?category:choices[0]} onChange={e=>setCategory(e.target.value as FeedbackInput["category"])} style={field} disabled={!enabled||busy}>{choices.map(value=><option key={value} value={value}>{value}</option>)}</select></div>}
  {audioEnabled&&<fieldset style={panel}><legend>Optional audio note</legend><p style={{fontSize:12,marginBottom:10}}>Microphone starts only when you click Record. Up to 15 seconds or {Math.ceil(limits.maxAudioBytes/1024)} KiB. No transcription.</p><button type="button" onClick={()=>start("audio","microphone")} disabled={controlsDisabled} style={button}>Record audio</button><label htmlFor={uid+"-audio"} style={{display:"block",marginTop:12}}>Or attach WebM, Ogg or WAV</label><input id={uid+"-audio"} type="file" accept="audio/webm,audio/ogg,audio/wav" disabled={controlsDisabled} onChange={e=>{const file=e.target.files?.[0];if(file)acceptClip(file);e.currentTarget.value="";}} style={{maxWidth:"100%",marginTop:8,fontSize:12}}/>{clipUrl&&<div style={{marginTop:12}}><audio controls src={clipUrl} style={{maxWidth:"100%"}}/><button type="button" disabled={busy} onClick={()=>setClip(null)} style={button}>Remove audio</button></div>}</fieldset>}
  {(photoEnabled||screenshotEnabled||videoEnabled)&&<fieldset style={panel}><legend>Optional visual evidence</legend><p style={{fontSize:12,lineHeight:1.6}}>Review before sending. Up to {limits.maxAttachments} attachments, {Math.floor(limits.maxAttachmentBytes/1024)} KiB each, {Math.floor(limits.maxTotalAttachmentBytes/1024)} KiB total. Check images and recordings for private information. Nothing uploads until you send.</p>
   {photoEnabled&&<>{fileInput("photo")}<label htmlFor={uid+"-camera-photo"} style={{display:"block",marginTop:12}}>Or take a photo on a supported mobile device</label><input id={uid+"-camera-photo"} type="file" accept="image/png,image/jpeg,image/webp" capture="environment" disabled={controlsDisabled} onChange={e=>{const file=e.target.files?.[0];if(file)addAttachment("photo",file);e.currentTarget.value="";}} style={{maxWidth:"100%",marginTop:8,fontSize:12}}/><p style={{fontSize:12}}>You can also paste an image into this form using your device’s Paste action.</p></>}
   {screenshotEnabled&&<div style={{marginTop:12}}><button type="button" disabled={controlsDisabled} onClick={()=>start("screenshot","screen")} style={button}>Capture screenshot</button><p style={{fontSize:12}}>Choose a tab, window or screen in the browser permission picker. Capture stops after one frame.</p><label htmlFor={uid+"-screenshot-file"} style={{display:"block",marginTop:12}}>Or attach a screenshot (PNG, JPEG or WebP)</label><input id={uid+"-screenshot-file"} type="file" accept="image/png,image/jpeg,image/webp" disabled={controlsDisabled} onChange={e=>{const file=e.target.files?.[0];if(file)addAttachment("screenshot",file);e.currentTarget.value="";}} style={{maxWidth:"100%",marginTop:8,fontSize:12}}/></div>}
   {videoEnabled&&<div style={{marginTop:12}}><button type="button" disabled={controlsDisabled} onClick={()=>start("video","camera")} style={button}>Record camera video</button>{" "}<button type="button" disabled={controlsDisabled} onClick={()=>start("video","screen")} style={button}>Record screen video</button><p style={{fontSize:12}}>Up to {limits.maxRecordingSeconds} seconds or the byte limit. Video recording does not include microphone/system audio.</p>{fileInput("video")}</div>}
   {attachments.map(item=><AttachmentPreview key={item.id} item={item} disabled={busy} remove={()=>{const next=attachmentsRef.current.filter(entry=>entry.id!==item.id);attachmentsRef.current=next;setAttachments(next);}}/>)}
  </fieldset>}
  {captureKind&&captureKind!=="screenshot"&&<button type="button" style={{...button,marginTop:12}} onClick={()=>stop()}>Stop recording</button>}
  <button type="submit" disabled={controlsDisabled} style={{marginTop:20,minHeight:48,padding:"12px 22px",borderRadius:999,background:color,color:"#faf8f5",opacity:controlsDisabled?0.6:1}}>{busy?"Sending…":buttonLabel}</button>
  <p role="status" style={{fontSize:13,lineHeight:1.6,marginTop:12}}>{notice||(acquiring?"Waiting for capture permission…":captureKind==="screenshot"?"Capturing the selected screen…":captureKind?"Recording. Stops automatically at the configured limit.":"")}</p>
  {problem&&<p role="alert" style={{fontSize:13,lineHeight:1.6,marginTop:12,color:"#9b3a2e"}}>{problem}</p>}
 </form>;
}
