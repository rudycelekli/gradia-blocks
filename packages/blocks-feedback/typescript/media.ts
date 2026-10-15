/** Apache-2.0. Browser-side bounds are usability guards; hosts must enforce them too. */
export type AttachmentKind = "photo" | "screenshot" | "video";
export type AttachmentMime = "image/png" | "image/jpeg" | "image/webp" | "video/webm" | "video/mp4";
export type PendingAttachment = { id:string; kind:AttachmentKind; blob:Blob };
export type MediaLimits = { maxAttachments:number; maxAttachmentBytes:number; maxTotalAttachmentBytes:number; maxRecordingSeconds:number; maxAudioBytes:number };
const bounded=(value:number|undefined,fallback:number,maximum:number)=>value===undefined?fallback:Number.isFinite(value)&&value>=1?Math.min(Math.floor(value),maximum):fallback;
export function mediaLimits(input:Partial<MediaLimits>={}):MediaLimits {
 return {maxAudioBytes:bounded(input.maxAudioBytes,262144,262144),maxAttachments:bounded(input.maxAttachments,4,8),maxAttachmentBytes:bounded(input.maxAttachmentBytes,5*1024*1024,16*1024*1024),maxTotalAttachmentBytes:bounded(input.maxTotalAttachmentBytes,10*1024*1024,32*1024*1024),maxRecordingSeconds:bounded(input.maxRecordingSeconds,30,60)};
}
export const baseMime=(blob:Blob)=>blob.type.toLowerCase().split(";")[0].trim();
export function validateAttachment(kind:AttachmentKind,blob:Blob,existing:readonly PendingAttachment[],limits:MediaLimits):string|null {
 const mime=baseMime(blob);
 if(!(kind==="video"?["video/webm","video/mp4"]:["image/png","image/jpeg","image/webp"]).includes(mime))return kind==="video"?"Use a WebM or MP4 video.":"Use a PNG, JPEG or WebP image.";
 if(!blob.size)return "This attachment is empty.";
 if(blob.size>limits.maxAttachmentBytes)return `Attachment exceeds ${Math.floor(limits.maxAttachmentBytes/1024)} KiB. Use a smaller file or shorter recording.`;
 if(existing.length>=limits.maxAttachments)return `Keep up to ${limits.maxAttachments} attachments.`;
 if(existing.reduce((sum,item)=>sum+item.blob.size,0)+blob.size>limits.maxTotalAttachmentBytes)return "Attachments exceed the total byte limit. Remove an attachment first.";
 return null;
}
export function blobBase64(blob:Blob):Promise<string>{return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>{const result=String(reader.result);const comma=result.indexOf(",");if(comma<0)reject(new Error("Could not encode attachment."));else resolve(result.slice(comma+1));};reader.onerror=()=>reject(new Error("Could not read attachment."));reader.onabort=()=>reject(new Error("Attachment read was cancelled."));reader.readAsDataURL(blob);});}
/** Screenshot uses only the explicitly selected display stream, never the host DOM. */
export async function screenshotBlob(input:MediaStream,maxPixels=8000000):Promise<Blob>{
 const video=document.createElement("video");video.muted=true;video.playsInline=true;video.srcObject=input;
 let timer:ReturnType<typeof setTimeout>|undefined;
 try{
  await Promise.race([video.play(),new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new Error("The selected screen did not provide a frame.")),10000);})]);
  if(!input.getVideoTracks().some(track=>track.readyState==="live"))throw new Error("Screen sharing ended before capture.");
  const width=video.videoWidth,height=video.videoHeight;
  if(!width||!height)throw new Error("The selected screen did not provide a frame.");
  const scale=Math.min(1,Math.sqrt(maxPixels/(width*height)));const canvas=document.createElement("canvas");canvas.width=Math.max(1,Math.floor(width*scale));canvas.height=Math.max(1,Math.floor(height*scale));
  const context=canvas.getContext("2d");if(!context)throw new Error("Screenshot capture is unavailable.");context.drawImage(video,0,0,canvas.width,canvas.height);
  return await new Promise<Blob>((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(new Error("Could not encode screenshot.")),"image/png"));
 }finally{if(timer)clearTimeout(timer);video.pause();video.srcObject=null;}
}
