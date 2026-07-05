import {
  Activity,
  ChevronDown,
  ChevronUp,
  Clipboard,
  Eye,
  Pause,
  Play,
  Radar,
  Save,
  Settings,
  TestTube2,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

const API = "http://localhost:8080";

type TraceEvent = {
  event_type?: string;
  skill_name?: string;
  tool_name?: string;
  client?: string;
  ai_output?: string;
  user_input?: string;
};

type CaseItem = {
  id?: string;
  created_at?: string;
  evidence_type?: string;
  suspected_skill?: string;
  wasted_tokens?: number;
  wasted_cost?: number;
  summary?: string;
  recent_events?: TraceEvent[];
};

type SkillRow = {
  target: string;
  success: number;
  failure: number;
  tokens: number;
  last_seen?: string;
};

type AppState = {
  listening?: boolean;
  paused?: boolean;
  hook_url?: string;
  server_error?: string | null;
  tailer_active?: boolean;
  tailer_error?: string | null;
  event_count?: number;
  ok_event_count?: number;
  failure_count?: number;
  case_count?: number;
  last_event_at?: string | null;
  latest_case?: CaseItem | null;
  skill_board?: SkillRow[];
};

const emptyState: AppState = {
  listening: false,
  paused: false,
  hook_url: `${API}/hook`,
  event_count: 0,
  ok_event_count: 0,
  failure_count: 0,
  case_count: 0,
  latest_case: null,
  skill_board: []
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

function formatTime(value?: string | null) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "just now";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatReceipt(item?: CaseItem | null) {
  if (!item) return "No token receipt yet.";
  const traces = (item.recent_events || [])
    .slice(-3)
    .map((event) => {
      const target = event.skill_name || event.tool_name || event.client || "unknown";
      const snippet = event.ai_output || event.user_input || event.event_type || "";
      return `- ${event.event_type || "event"} | ${target}: ${String(snippet).slice(0, 160)}`;
    })
    .join("\n");
  return [
    "Token receipt",
    `Type: ${item.evidence_type || "Evidence"}`,
    `Target: ${item.suspected_skill || "unknown"}`,
    `Tokens: ${item.wasted_tokens ?? "?"}`,
    `Cost: $${item.wasted_cost ?? "?"}`,
    "",
    item.summary || "Evidence candidate captured.",
    traces ? `\nRecent traces:\n${traces}` : ""
  ].join("\n");
}

function stateLabel(state: AppState, apiOnline: boolean) {
  if (!apiOnline) return "Core offline";
  if (state.server_error) return "Port error";
  if (state.paused) return "Paused";
  if (state.listening) return "Listening";
  return "Starting";
}

export function App() {
  const [state, setState] = useState<AppState>(emptyState);
  const [cases, setCases] = useState<CaseItem[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiOnline, setApiOnline] = useState(false);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState("");
  const latestCaseIdRef = useRef<string | null>(null);

  async function refresh() {
    try {
      const nextState = await requestJson<AppState>("/state");
      setApiOnline(true);
      setState(nextState);
      const caseId = nextState.latest_case?.id || null;
      if (caseId && caseId !== latestCaseIdRef.current) {
        latestCaseIdRef.current = caseId;
        setSelectedCaseId(caseId);
        setFlashId(caseId);
        window.setTimeout(() => setFlashId(null), 1400);
      }
      const casePayload = await requestJson<{ cases: CaseItem[] }>("/cases");
      setCases(casePayload.cases || []);
      if (caseId) {
        setSelectedCaseId((current) => current || caseId);
      }
    } catch {
      setApiOnline(false);
    }
  }

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 900);
    return () => window.clearInterval(timer);
  }, []);

  const selectedCase = useMemo(() => {
    return cases.find((item) => item.id === selectedCaseId) || state.latest_case || null;
  }, [cases, selectedCaseId, state.latest_case]);

  const success = Number(state.ok_event_count || 0);
  const failures = Number(state.failure_count || 0);
  const totalEvents = success + failures;
  const successPct = totalEvents ? Math.round((success / totalEvents) * 100) : 0;
  const failurePct = totalEvents ? 100 - successPct : 0;
  const rows = state.skill_board || [];
  const sourceLabel = state.tailer_active ? "Codex live" : "Hook only";

  async function togglePause() {
    const payload = await requestJson<{ state: AppState }>("/control/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !state.paused })
    });
    setState(payload.state);
  }

  async function testCapture() {
    await requestJson("/test-capture", { method: "POST", body: "{}" });
    await refresh();
  }

  async function saveCase() {
    if (!selectedCase) return;
    try {
      const payload = await requestJson<{ path: string; exists?: boolean }>("/cases/save", {
        method: "POST",
        body: JSON.stringify({ case_id: selectedCase.id })
      });
      setSaveNote(payload.exists === false ? "Save failed" : "Saved local receipt");
      console.info(payload.path);
    } catch {
      setSaveNote("Save failed");
    }
    window.setTimeout(() => setSaveNote(""), 1600);
  }

  async function copyReceipt() {
    try {
      await navigator.clipboard.writeText(formatReceipt(selectedCase));
      setSaveNote("Copied receipt");
    } catch {
      setSaveNote("Copy failed");
    }
    window.setTimeout(() => setSaveNote(""), 1600);
  }

  return (
    <main className="shell">
      <section className={`glass-card ${expanded ? "is-expanded" : ""}`}>
        <header className="topbar" data-tauri-drag-region>
          <div className="brand">
            <Radar size={18} />
            <div>
              <strong>Vibe-Dashcam</strong>
              <span>Local only</span>
            </div>
          </div>
          <div className="top-actions">
            <span className={`status-dot ${apiOnline && state.listening && !state.paused ? "ok" : "warn"}`} />
            <button className="icon-button" onClick={() => setSettingsOpen(true)} aria-label="Settings">
              <Settings size={17} />
            </button>
          </div>
        </header>

        <div className="status-line">
          <span>{stateLabel(state, apiOnline)}</span>
          <span>{sourceLabel} / {formatTime(state.last_event_at)}</span>
        </div>

        <section className="split-panel" aria-label="success failure overview">
          <div className="split-copy">
            <span>{success} clean</span>
            <span>{failures} crash</span>
          </div>
          <div className="split-bar">
            <div className="success-bar" style={{ width: `${successPct}%` }} />
            <div className="failure-bar" style={{ width: `${failurePct}%` }} />
          </div>
        </section>

        <section className="board">
          <div className="section-title">
            <span>Red / Black board</span>
            <Activity size={14} />
          </div>
          {rows.length === 0 ? (
            <div className="empty">Waiting for Codex Skill/MCP activity.</div>
          ) : (
            rows.slice(0, 5).map((row) => {
              const rowTotal = Math.max(row.success + row.failure, 1);
              const rowFailPct = Math.round((row.failure / rowTotal) * 100);
              return (
                <button
                  className={`board-row ${row.failure ? "has-fail" : "clean"}`}
                  key={row.target}
                  onClick={() => setExpanded(true)}
                >
                  <div className="row-main">
                    <span>{row.target}</span>
                    <small>{row.success} OK / {row.failure} BAD</small>
                  </div>
                  <div className="row-bar">
                    <i className="row-ok" style={{ width: `${100 - rowFailPct}%` }} />
                    <i className="row-bad" style={{ width: `${rowFailPct}%` }} />
                  </div>
                </button>
              );
            })
          )}
        </section>

        <section className={`latest ${expanded ? "expanded" : ""} ${flashId === selectedCase?.id ? "flash" : ""}`}>
          <button className="latest-head" onClick={() => setExpanded(!expanded)}>
            <div>
              <span>Latest token receipt</span>
              <strong>{selectedCase?.suspected_skill || "No case yet"}</strong>
            </div>
            {expanded ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
          </button>
          {expanded && (
            <div className="latest-body">
              <p>{selectedCase?.summary || "No evidence captured yet."}</p>
              <div className="case-meta">
                <span>{selectedCase?.evidence_type || "Waiting"}</span>
                <span>{selectedCase?.wasted_tokens ?? 0} tokens</span>
              </div>
              <div className="trace-list">
                {(selectedCase?.recent_events || []).slice(-3).map((event, index) => (
                  <span key={`${event.event_type}-${index}`}>
                    {event.event_type || "event"} / {event.skill_name || event.tool_name || event.client || "unknown"}
                  </span>
                ))}
              </div>
            </div>
          )}
        </section>

        <section className="recent">
          {cases.slice(0, 3).map((item) => (
            <button
              key={item.id}
              className={item.id === selectedCase?.id ? "recent-row active" : "recent-row"}
              onClick={() => {
                setSelectedCaseId(item.id || null);
                setExpanded(true);
              }}
            >
              <span>{item.suspected_skill || "unknown"}</span>
              <small>{item.wasted_tokens ?? 0}</small>
            </button>
          ))}
        </section>

        <footer className="actions">
          <button onClick={togglePause}>{state.paused ? <Play size={15} /> : <Pause size={15} />}{state.paused ? "Resume" : "Pause"}</button>
          <button onClick={testCapture}><TestTube2 size={15} />Probe</button>
          <button onClick={saveCase} disabled={!selectedCase}><Save size={15} />Save</button>
          <button onClick={copyReceipt} disabled={!selectedCase}><Clipboard size={15} />Copy</button>
        </footer>
        {saveNote && <div className="toast">{saveNote}</div>}
      </section>

      {settingsOpen && (
        <aside className="settings-drawer">
          <div className="drawer-head">
            <strong>Settings</strong>
            <button className="icon-button" onClick={() => setSettingsOpen(false)} aria-label="Close settings">
              <X size={17} />
            </button>
          </div>
          <SettingBlock title="Sources" lines={["Codex session logs", "Local hook payloads"]} />
          <SettingBlock title="Privacy" lines={["Secret redaction on", "Text truncation on", "No cloud upload"]} />
          <SettingBlock title="Storage" lines={["Local JSONL cases", "Save only on click"]} />
          <SettingBlock title="Window" lines={["Bottom-right minimap", "Always on top"]} />
        </aside>
      )}
    </main>
  );
}

function SettingBlock({ title, lines }: { title: string; lines: string[] }) {
  return (
    <div className="setting-block">
      <strong>{title}</strong>
      {lines.map((line) => (
        <span key={line}>
          <Eye size={12} />
          {line}
        </span>
      ))}
    </div>
  );
}
