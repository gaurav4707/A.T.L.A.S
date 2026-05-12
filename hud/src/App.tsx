import { useEffect, useMemo, useRef, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { register, unregisterAll } from "@tauri-apps/plugin-global-shortcut";
import "./App.css";
import { AvatarPanel } from "./components/AvatarPanel";
import { ChatPanel } from "./components/ChatPanel";
import { DryRunModal } from "./components/DryRunModal";
import { HistoryDrawer } from "./components/HistoryDrawer";
import { SettingsModal } from "./components/SettingsModal";
import { StatusPanel } from "./components/StatusPanel";
import { atlasFetch, loadSettings, saveSettings } from "./lib/atlasApi";
import type { AtlasSettings, AvatarState, ChatMessage, DryRunPayload, StatusPayload, WsPayload } from "./types";

function App() {
  const [settings, setSettings] = useState<AtlasSettings>(() => loadSettings());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [currentStreamText, setCurrentStreamText] = useState("");
  const [systemStatus, setSystemStatus] = useState<StatusPayload | null>(null);
  const [avatarState, setAvatarState] = useState<AvatarState>("idle");
  const [isOnline, setIsOnline] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [dryRunPayload, setDryRunPayload] = useState<DryRunPayload | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [connectNonce, setConnectNonce] = useState(0);

  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const speakingResetRef = useRef<number | null>(null);
  const streamTextRef = useRef("");

  const wsUrl = useMemo(() => settings.apiBase.replace(/^http/, "ws") + "/ws", [settings.apiBase]);

  const pushMessage = (role: "user" | "assistant", text: string) => {
    if (!text.trim()) {
      return;
    }
    setMessages((prev) => [
      ...prev,
      {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        role,
        text,
        timestamp: Date.now(),
      },
    ]);
  };

  const sendCommand = async (text: string, execute = false) => {
    if (!isOnline) {
      pushMessage("user", text);
    }

    const response = await atlasFetch(settings, "/command", {
      method: "POST",
      body: JSON.stringify({ text, source: "api", execute }),
    });

    if (!response.ok) {
      const failure = `Command failed (${response.status})`;
      pushMessage("assistant", failure);
      setAvatarState("error");
      window.setTimeout(() => setAvatarState("idle"), 800);
      return;
    }

    const body = (await response.json()) as { result?: string };
    if (!streamTextRef.current && body.result) {
      pushMessage("assistant", body.result);
    }
  };

  const refreshStatus = async () => {
    const response = await atlasFetch(settings, "/status");
    if (!response.ok) {
      return;
    }
    const status = (await response.json()) as StatusPayload;
    setSystemStatus(status);
  };

  useEffect(() => {
    void refreshStatus();
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [settings]);

  useEffect(() => {
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setIsOnline(true);
      reconnectAttemptsRef.current = 0;
    };

    ws.onmessage = (event) => {
      let payload: WsPayload;
      try {
        payload = JSON.parse(event.data) as WsPayload;
      } catch {
        return;
      }

      if (payload.type === "user_message") {
        pushMessage("user", String(payload.data || ""));
      }

      if (payload.type === "listening_start") {
        setIsListening(true);
        setAvatarState("listening");
      }

      if (payload.type === "token") {
        setIsListening(false);
        setAvatarState("speaking");
        setCurrentStreamText((prev) => {
          const next = prev + String(payload.data || "");
          streamTextRef.current = next;
          return next;
        });
      }

      if (payload.type === "done") {
        setIsListening(false);
        const doneText = String(payload.data || "").trim();
        if (streamTextRef.current.trim()) {
          pushMessage("assistant", streamTextRef.current);
          streamTextRef.current = "";
          setCurrentStreamText("");
        } else if (doneText) {
          pushMessage("assistant", doneText);
        }
        if (speakingResetRef.current) {
          window.clearTimeout(speakingResetRef.current);
        }
        speakingResetRef.current = window.setTimeout(() => setAvatarState("idle"), 1000);
      }

      if (payload.type === "killswitch") {
        streamTextRef.current = "";
        setCurrentStreamText("");
        setIsListening(false);
        setAvatarState("idle");
      }

      if (payload.type === "error") {
        streamTextRef.current = "";
        setCurrentStreamText("");
        pushMessage("assistant", `Error: ${String(payload.data || "unknown error")}`);
        setAvatarState("error");
        window.setTimeout(() => setAvatarState("idle"), 800);
      }

      if (payload.type === "dry_run") {
        const data = (payload.data || {}) as Partial<DryRunPayload>;
        setDryRunPayload({
          text: data.text || "",
          action: String(data.action || "unknown"),
          target: String(data.target || "(no file specified)"),
          risk: String(data.risk || "low"),
          gate: String(data.gate || "none"),
        });
      }
    };

    ws.onclose = () => {
      setIsOnline(false);
      const nextAttempt = reconnectAttemptsRef.current + 1;
      reconnectAttemptsRef.current = nextAttempt;
      const delay = Math.min(5000, 250 * 2 ** nextAttempt);
      reconnectTimerRef.current = window.setTimeout(() => {
        setConnectNonce((v) => v + 1);
      }, delay);
    };

    ws.onerror = () => {
      ws.close();
    };

    return () => {
      ws.close();
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      if (speakingResetRef.current) {
        window.clearTimeout(speakingResetRef.current);
      }
    };
  }, [connectNonce, wsUrl]);

  useEffect(() => {
    const setupHotkeys = async () => {
      await register("Control+Space", async () => {
        const windowRef = getCurrentWindow();
        const visible = await windowRef.isVisible();
        if (visible) {
          await windowRef.hide();
        } else {
          await windowRef.show();
          await windowRef.setFocus();
        }
      });

      await register("Control+Shift+K", async () => {
        await atlasFetch(settings, "/command", {
          method: "POST",
          body: JSON.stringify({ text: "stop", source: "api" }),
        });
      });
    };

    void setupHotkeys();
    return () => {
      void unregisterAll();
    };
  }, [settings]);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    void listen("tray-mute", () => {
      void sendCommand("mute");
    }).then((fn) => {
      unlisten = fn;
    });
    return () => {
      unlisten?.();
    };
  }, [settings, isOnline]);

  const applySettings = (next: AtlasSettings) => {
    const normalized: AtlasSettings = {
      apiBase: next.apiBase || "http://127.0.0.1:8000",
      token: next.token,
    };
    saveSettings(normalized);
    setSettings(normalized);
    setShowSettings(false);
    setConnectNonce((v) => v + 1);
  };

  const proceedDryRun = async () => {
    if (!dryRunPayload?.text) {
      setDryRunPayload(null);
      return;
    }
    await sendCommand(dryRunPayload.text, true);
    setDryRunPayload(null);
  };

  return (
    <main className="app-shell">
      {!isOnline && (
        <div className="offline-banner">
          <span>ATLAS offline</span>
          <button className="btn-primary" onClick={() => setConnectNonce((v) => v + 1)}>
            Start
          </button>
        </div>
      )}

      <header className="app-header">
        <h1>ATLAS HUD</h1>
      </header>

      <section className="layout-grid">
        <ChatPanel
          messages={messages}
          currentStreamText={currentStreamText}
          listening={isListening}
          onSend={sendCommand}
        />
        <AvatarPanel state={avatarState} />
        <StatusPanel
          status={systemStatus}
          isOnline={isOnline}
          onMute={async () => sendCommand("mute")}
          onStop={async () => sendCommand("stop")}
          onOpenSettings={() => setShowSettings(true)}
          onOpenHistory={() => setShowHistory(true)}
        />
      </section>

      <HistoryDrawer
        open={showHistory}
        settings={settings}
        onClose={() => setShowHistory(false)}
        onRerun={async (text) => sendCommand(text)}
      />

      <SettingsModal
        open={showSettings}
        settings={settings}
        onClose={() => setShowSettings(false)}
        onSave={applySettings}
      />

      <DryRunModal dryRun={dryRunPayload} onCancel={() => setDryRunPayload(null)} onProceed={proceedDryRun} />
    </main>
  );
}

export default App;
