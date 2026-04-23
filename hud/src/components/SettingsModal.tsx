import { useEffect, useState } from "react";
import type { AtlasSettings } from "../types";

interface SettingsModalProps {
  open: boolean;
  settings: AtlasSettings;
  onClose: () => void;
  onSave: (next: AtlasSettings) => void;
}

export function SettingsModal({ open, settings, onClose, onSave }: SettingsModalProps) {
  const [apiBase, setApiBase] = useState(settings.apiBase);
  const [token, setToken] = useState(settings.token);

  useEffect(() => {
    setApiBase(settings.apiBase);
    setToken(settings.token);
  }, [settings]);

  if (!open) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal-card settings-modal">
        <h3>Settings</h3>
        <label>
          API Base URL
          <input
            value={apiBase}
            onChange={(event) => setApiBase(event.target.value)}
            placeholder="http://127.0.0.1:8000"
          />
        </label>
        <label>
          X-ATLAS-Token
          <input
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="Paste your API token"
          />
        </label>
        <div className="modal-actions">
          <button className="btn-secondary" onClick={onClose}>
            Close
          </button>
          <button
            className="btn-primary"
            onClick={() => onSave({ apiBase: apiBase.trim(), token: token.trim() })}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
