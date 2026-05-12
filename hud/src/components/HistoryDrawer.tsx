import { useEffect, useMemo, useState } from "react";
import { atlasFetch } from "../lib/atlasApi";
import type { AtlasSettings } from "../types";

interface HistoryRow {
  id?: number;
  timestamp?: string;
  raw_command?: string;
  success?: number | boolean;
}

interface HistoryDrawerProps {
  open: boolean;
  settings: AtlasSettings;
  onClose: () => void;
  onRerun: (text: string) => Promise<void>;
}

export function HistoryDrawer({ open, settings, onClose, onRerun }: HistoryDrawerProps) {
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [query, setQuery] = useState("");

  const endpoint = useMemo(() => {
    if (!query.trim()) {
      return "/history?n=20";
    }
    return `/history?q=${encodeURIComponent(query.trim())}`;
  }, [query]);

  useEffect(() => {
    if (!open) {
      return;
    }

    let cancelled = false;
    const load = async () => {
      const response = await atlasFetch(settings, endpoint);
      if (!response.ok) {
        return;
      }
      const data = (await response.json()) as HistoryRow[];
      if (!cancelled) {
        setRows(data || []);
      }
    };

    void load();
    const timeout = setTimeout(() => {
      void load();
    }, 250);

    return () => {
      cancelled = true;
      clearTimeout(timeout);
    };
  }, [endpoint, open, settings]);

  return (
    <aside className={`history-drawer ${open ? "open" : ""}`}>
      <div className="history-head">
        <h3>History</h3>
        <button className="icon-btn" onClick={onClose} aria-label="Close history">
          X
        </button>
      </div>
      <input
        className="history-search"
        placeholder="Search commands"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <div className="history-list">
        {rows.map((row, idx) => {
          const ok = Boolean(row.success);
          const raw = row.raw_command || "";
          return (
            <div key={`${row.id || idx}-${raw}`} className="history-item">
              <div className="history-item-main">
                <span className={`dot ${ok ? "ok" : "fail"}`} />
                <div>
                  <p className="history-time">{row.timestamp || ""}</p>
                  <p className="history-command">{raw}</p>
                </div>
              </div>
              <button className="btn-secondary" onClick={() => void onRerun(raw)} disabled={!raw}>
                Rerun
              </button>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
