import { Clock3, Settings } from "lucide-react";
import { formatUptime } from "../lib/atlasApi";
import type { StatusPayload } from "../types";

interface StatusPanelProps {
  status: StatusPayload | null;
  isOnline: boolean;
  onMute: () => Promise<void>;
  onStop: () => Promise<void>;
  onOpenSettings: () => void;
  onOpenHistory: () => void;
}

export function StatusPanel({
  status,
  isOnline,
  onMute,
  onStop,
  onOpenSettings,
  onOpenHistory,
}: StatusPanelProps) {
  const wakeWordOn = (status?.voice_mode || "").toLowerCase().includes("active");

  return (
    <section className="panel status-panel">
      <div className="status-head">
        <h2>Status</h2>
        <div>
          <button className="icon-btn" onClick={onOpenHistory} aria-label="Open history">
            <Clock3 size={14} />
          </button>
          <button className="icon-btn" onClick={onOpenSettings} aria-label="Open settings">
            <Settings size={14} />
          </button>
        </div>
      </div>

      <dl className="status-grid">
        <div>
          <dt>Connection</dt>
          <dd>{isOnline ? "Online" : "Offline"}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{status?.model || "Unknown"}</dd>
        </div>
        <div>
          <dt>Uptime</dt>
          <dd>{formatUptime(status?.uptime_s)}</dd>
        </div>
        <div>
          <dt>Memory</dt>
          <dd>{status?.session_memory ? "ON" : "OFF"}</dd>
        </div>
        <div>
          <dt>Wake Word</dt>
          <dd>{wakeWordOn ? "ON" : "OFF"}</dd>
        </div>
      </dl>

      <div className="status-actions">
        <button className="btn-secondary" onClick={() => void onMute()}>
          Mute
        </button>
        <button className="btn-danger" onClick={() => void onStop()}>
          STOP
        </button>
      </div>
    </section>
  );
}
