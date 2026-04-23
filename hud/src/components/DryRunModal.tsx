import type { DryRunPayload } from "../types";

interface DryRunModalProps {
  dryRun: DryRunPayload | null;
  onCancel: () => void;
  onProceed: () => void;
}

function riskClass(risk: string): string {
  const normalized = risk.toLowerCase();
  if (normalized === "high" || normalized === "critical") {
    return "risk-high";
  }
  if (normalized === "medium") {
    return "risk-medium";
  }
  return "risk-low";
}

export function DryRunModal({ dryRun, onCancel, onProceed }: DryRunModalProps) {
  if (!dryRun) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="modal-card">
        <h3>Dry Run</h3>
        <p>
          <strong>ACTION</strong> {dryRun.action}
        </p>
        <p>
          <strong>TARGET</strong> {dryRun.target}
        </p>
        <p>
          <strong>RISK</strong> <span className={`risk-badge ${riskClass(dryRun.risk)}`}>{dryRun.risk}</span>
        </p>
        <p>
          <strong>GATE</strong> {dryRun.gate}
        </p>
        <div className="modal-actions">
          <button className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn-primary" onClick={onProceed}>
            Proceed
          </button>
        </div>
      </div>
    </div>
  );
}
