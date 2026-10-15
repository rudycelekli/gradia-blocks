/** Apache-2.0. Explicit host adapter; does not discover credentials, storage or endpoints. */
import { FeedbackClient, type BlockConfiguration } from "./client";
import type { FeedbackWidgetProps } from "./feedback-widget";
/** Fetch configuration once per host session, then refresh explicitly when host policy changes. */
export async function feedbackWidgetProps(client:FeedbackClient,appId:string):Promise<FeedbackWidgetProps>{
 const configuration=await client.getConfig();
 return configuredWidgetProps(client,configuration,appId);
}
export function configuredWidgetProps(client:FeedbackClient,configuration:BlockConfiguration,appId:string):FeedbackWidgetProps {
 return {
  enabled:configuration.features.enabled,heading:configuration.content.title,messageLabel:configuration.content.message_label,successMessage:configuration.content.success_message,buttonLabel:configuration.content.launcher_label,accent:configuration.design.accent,radius:configuration.design.radius,density:configuration.design.density,appId,
  categories:configuration.config.categories,categoriesEnabled:configuration.config.categories.length>1,maxMessageChars:configuration.config.max_message_chars,
  audioEnabled:configuration.features.audio,photoEnabled:configuration.features.photo,screenshotEnabled:configuration.features.screenshot,videoEnabled:configuration.features.video,
  limits:{maxAudioBytes:configuration.config.max_audio_bytes,maxAttachments:configuration.config.max_attachments,maxAttachmentBytes:configuration.config.max_attachment_bytes,maxTotalAttachmentBytes:configuration.config.max_total_attachment_bytes},
  submit:async(input,idempotencyKey)=>{const result=await client.submit(input,idempotencyKey);return {delivery:"acknowledged",id:result.feedback.id,revision:result.feedback.revision};},
 };
}
/* Host example:
 const client=new FeedbackClient({baseUrl:hostOwnedBffUrl,token:()=>hostIssuedShortLivedToken});
 const props=await feedbackWidgetProps(client,"your-app");
 // Render <FeedbackWidget {...props}/> inside the host's layout/launcher.
 // Host BFF chooses identity/tenant and storage. Never ship a long-lived operator key.
 // Host must authenticate retrieval and enforce deletion, retention and consent policies.
 // Design placement remains a host layout responsibility; radius/density are applied to form fields.
*/
