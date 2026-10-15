/** Apache-2.0. Typed HTTP client; no TypeScript server implementation is implied. */
export interface FeedbackContext { page_url?: string | null; app_id?: string | null }
export type AudioMime = 'audio/webm' | 'audio/ogg' | 'audio/wav';
export interface AudioInput { mime_type: AudioMime; data_base64: string }
export type AttachmentKind = 'photo' | 'screenshot' | 'video';
export type AttachmentMime = 'image/png' | 'image/jpeg' | 'image/webp' | 'video/webm' | 'video/mp4';
export interface AttachmentInput { kind: AttachmentKind; mime_type: AttachmentMime; data_base64: string }
export interface AttachmentMetadata { id: string; kind: AttachmentKind; mime_type: AttachmentMime; size_bytes: number; sha256: string }
export interface Submission { message: string; category?: string; context?: FeedbackContext; audio?: AudioInput | null; attachments?: AttachmentInput[] }
export type FeedbackStatus = 'new' | 'in_review' | 'resolved' | 'dismissed';
export interface Feedback {
  id: string; revision: number; status: FeedbackStatus; category: string; message: string;
  assignee: string | null; context: FeedbackContext; created_at: string; updated_at: string;
  audio: null | { mime_type: AudioMime; size_bytes: number; sha256: string };
  attachments: AttachmentMetadata[];
}
export interface Acknowledgment { status: 'acknowledged'; feedback: Feedback; replayed: boolean }
export interface Triage { expected_revision: number; status?: FeedbackStatus; category?: string; assignee?: string | null }
export interface BlockConfiguration {
  block_id: 'gradia.feedback'; version: string; configuration_digest: string;
  design: { accent: string; radius: number; density: 'comfortable' | 'compact'; placement: 'bottom-right' | 'bottom-left' | 'inline' };
  content: { title: string; launcher_label: string; message_label: string; success_message: string };
  features: { enabled: boolean; audio: boolean; triage: boolean; photo: boolean; screenshot: boolean; video: boolean };
  config: { categories: string[]; max_message_chars: number; max_audio_bytes: number; max_attachment_bytes: number; max_total_attachment_bytes: number; max_attachments: number };
}
export class FeedbackHttpError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); this.name = 'FeedbackHttpError'; }
}
export class FeedbackTransportUncertain extends Error {
  constructor(public original: unknown) { super('Response was not acknowledged. Reconcile by retrying the same payload and idempotency key.'); this.name = 'FeedbackTransportUncertain'; }
}
export interface ClientOptions {
  /** Use a same-origin authenticated BFF in browsers. Never embed an operator key in a public bundle. */
  baseUrl: string;
  /** Server-held credential provider, or a short-lived credential issued by the host. */
  token: () => string | Promise<string>;
  fetch?: typeof fetch;
  timeoutMs?: number;
}
export class FeedbackClient {
  constructor(private options: ClientOptions) {
    const parsed = new URL(options.baseUrl);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error('Use an HTTP(S) base URL without credentials, query or fragment');
    if (parsed.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)) throw new Error('HTTP is permitted only for loopback reference hosts');
    if (options.timeoutMs !== undefined && (!Number.isFinite(options.timeoutMs) || options.timeoutMs < 1 || options.timeoutMs > 120000)) throw new Error('timeoutMs must be between 1 and 120000');
  }
  private async request<T>(path: string, method = 'GET', body?: unknown, key?: string, binary: false | number = false): Promise<T> {
    const controller = new AbortController();
    let rejectDeadline!: (reason?: unknown) => void;
    const deadline = new Promise<never>((_, reject) => { rejectDeadline = reject; });
    const timer = setTimeout(() => { controller.abort(); rejectDeadline(new Error("Feedback request deadline exceeded")); }, this.options.timeoutMs ?? 10000);
    let response: Response;
    try {
      const token = await Promise.race([Promise.resolve().then(() => this.options.token()), deadline]);
      response = await Promise.race([(this.options.fetch ?? fetch)(this.options.baseUrl.replace(/\/$/, '') + path, {
        method, signal: controller.signal, redirect: 'error', cache: 'no-store',
        headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(key ? { 'Idempotency-Key': key } : {}) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }), deadline]);
      const payload = await Promise.race([response.ok && binary ? this.boundedBlob(response, binary) : response.json(), deadline]);
      if (!response.ok) throw new FeedbackHttpError(response.status, payload.error?.code ?? 'http_error', payload.error?.message ?? 'Feedback request refused');
      if ((method === 'POST' || method === 'PATCH') && (payload.status !== 'acknowledged' || typeof payload.feedback?.id !== 'string' || !payload.feedback.id.trim() || !Number.isInteger(payload.feedback?.revision) || payload.feedback.revision < 1)) throw new FeedbackTransportUncertain('Invalid acknowledgment');
      return payload as T;
    } catch (error) {
      if (error instanceof FeedbackHttpError || error instanceof FeedbackTransportUncertain) throw error;
      throw new FeedbackTransportUncertain(error);
    } finally { clearTimeout(timer); }
  }
  private async boundedBlob(response: Response, maximumBytes: number): Promise<Blob> {
    const declared = Number(response.headers.get('Content-Length'));
    if (Number.isFinite(declared) && declared > maximumBytes) { void response.body?.cancel(); throw new Error('Attachment download exceeds byte limit'); }
    const reader = response.body?.getReader();
    if (!reader) return new Blob([], {type: response.headers.get('Content-Type') ?? ''});
    const chunks: ArrayBuffer[] = []; let size = 0;
    try {
      while (true) { const {done, value} = await reader.read(); if (done) break; size += value.byteLength; if (size > maximumBytes) { void reader.cancel(); throw new Error('Attachment download exceeds byte limit'); } chunks.push(Uint8Array.from(value).buffer); }
      return new Blob(chunks, {type: response.headers.get('Content-Type') ?? ''});
    } finally { reader.releaseLock(); }
  }
  getConfig(): Promise<BlockConfiguration> { return this.request('/v1/feedback/config'); }
  submit(body: Submission, idempotencyKey: string): Promise<Acknowledgment> {
    if (!idempotencyKey || idempotencyKey.length > 128) throw new Error('Use a stable idempotency key of 1-128 characters');
    return this.request('/v1/feedback', 'POST', body, idempotencyKey);
  }
  /** Operator-only retrieval through the authenticated host/BFF. No credentials in URLs. */
  downloadAttachment(id: string, attachmentId: string): Promise<Blob> { return this.request(`/v1/feedback/${encodeURIComponent(id)}/attachments/${encodeURIComponent(attachmentId)}`, 'GET', undefined, undefined, 16 * 1024 * 1024); }
  downloadAudio(id: string): Promise<Blob> { return this.request(`/v1/feedback/${encodeURIComponent(id)}/audio`, 'GET', undefined, undefined, 262144); }
  get(id: string): Promise<Feedback> { return this.request(`/v1/feedback/${encodeURIComponent(id)}`); }
  list(limit = 25, cursor = ''): Promise<{ items: Feedback[]; next_cursor: string | null }> {
    return this.request(`/v1/feedback?${new URLSearchParams({ limit: String(limit), cursor })}`);
  }
  triage(id: string, body: Triage): Promise<{ status: 'acknowledged'; feedback: Feedback }> { return this.request(`/v1/feedback/${encodeURIComponent(id)}`, 'PATCH', body); }
  events(id: string, after = 0): Promise<{ items: Record<string, unknown>[]; next_after: number }> { return this.request(`/v1/feedback/${encodeURIComponent(id)}/events?after=${after}`); }
}
