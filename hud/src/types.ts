export type AvatarState = "idle" | "listening" | "speaking" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  timestamp: number;
}

export interface StatusPayload {
  model?: string;
  uptime_s?: number;
  session_memory?: boolean;
  voice_mode?: string;
}

export interface DryRunPayload {
  text?: string;
  action: string;
  target: string;
  risk: string;
  gate: string;
}

export interface WsPayload {
  type: string;
  data?: unknown;
}

export interface AtlasSettings {
  apiBase: string;
  token: string;
}
