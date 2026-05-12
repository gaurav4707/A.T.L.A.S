import type { AvatarState } from "../types";

interface AvatarPanelProps {
  state: AvatarState;
}

export function AvatarPanel({ state }: AvatarPanelProps) {
  return (
    <section className="panel avatar-panel">
      <div className={`avatar-core avatar-${state}`}>
        <span className="ring ring-1" />
        <span className="ring ring-2" />
        <span className="ring ring-3" />
      </div>
      <p className="atlas-label">ATLAS</p>
    </section>
  );
}
