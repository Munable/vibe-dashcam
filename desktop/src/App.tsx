import {
  Activity,
  ChevronDown,
  ChevronUp,
  Clipboard,
  Download,
  Languages,
  Pause,
  Pin,
  Play,
  Radar,
  Settings,
  TestTube2,
  X
} from "lucide-react";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow, LogicalSize, PhysicalPosition } from "@tauri-apps/api/window";
import { useEffect, useMemo, useRef, useState } from "react";

const API = "http://localhost:8080";

type Lang = "en" | "zh";
type StatsScope = "today" | "all";
type BoardMode = "success" | "failure";
type WindowSizeName = "small" | "large";

type TraceEvent = {
  event_type?: string;
  source_kind?: string;
  skill_name?: string;
  skill_source?: string;
  tool_name?: string;
  client?: string;
  ai_output?: string;
  user_input?: string;
  call_id?: string;
  skill_evidence?: string;
  model?: string;
};

type CaseReview = {
  status?: "pending" | "reviewed" | "unavailable";
  verdict?: "tool_fault" | "model_fault" | "unclear";
  confidence?: number;
  one_sentence_summary?: string;
  reason?: string;
  model?: string;
};

type CaseItem = {
  id?: string;
  source_kind?: string;
  created_at?: string;
  evidence_type?: string;
  suspected_skill?: string;
  source_type?: string;
  source_target?: string;
  source_display_name?: string;
  source_detail?: string;
  source_call_id?: string;
  source_model?: string;
  wasted_tokens?: number;
  wasted_cost?: number;
  summary?: string;
  recent_events?: TraceEvent[];
  case_review?: CaseReview;
};

type SkillRow = {
  target: string;
  success: number;
  failure: number;
  tokens: number;
  last_seen?: string;
};

type SourceStatus = {
  codex_session?: {
    active?: boolean;
    path?: string | null;
    last_seen_at?: string | null;
    last_event_name?: string | null;
  };
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
  saved_case_count?: number;
  last_event_at?: string | null;
  latest_case?: CaseItem | null;
  skill_board?: SkillRow[];
  source_status?: SourceStatus;
  review_model?: string;
  review_default_model?: string;
  review_model_options?: string[];
  stats_scope?: StatsScope;
  display_ok_event_count?: number;
  display_failure_count?: number;
};

type DiagnosticsPayload = {
  ok?: boolean;
  codex_cli?: { found?: boolean; path?: string | null };
  codex_home?: { exists?: boolean; path?: string | null };
  codex_sessions?: { exists?: boolean; path?: string | null; active?: boolean; last_seen_at?: string | null };
  local_config?: { exists?: boolean; path?: string | null };
  local_history?: { exists?: boolean; path?: string | null };
  review_model?: string;
  review_model_options?: string[];
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
  skill_board: [],
  review_model: "default",
  review_default_model: "default",
  review_model_options: ["default"],
  stats_scope: "today",
  display_ok_event_count: 0,
  display_failure_count: 0
};

const WINDOW_SIZE_KEY = "vibe-dashcam-window-size";
const WINDOW_SIZES: Record<WindowSizeName, { width: number; height: number }> = {
  small: { width: 320, height: 396 },
  large: { width: 430, height: 500 }
};

const COPY = {
  en: {
    localOnly: "Local only",
    settings: "Settings",
    closeSettings: "Close settings",
    listening: "Listening",
    paused: "Paused",
    starting: "Starting",
    portError: "Port error",
    portOccupied: "Port 8080 occupied",
    coreMissing: "Local engine missing",
    coreStartFailed: "Local engine failed",
    coreOffline: "Local engine offline",
    liveCodex: "Reading Codex activity",
    hookOnly: "Hook only",
    waitingCodex: "Waiting for Codex activity",
    never: "never",
    justNow: "just now",
    clean: "success",
    flagged: "failed",
    flag: "failed",
    successBoardTitle: "Successful Skill/MCP",
    failureBoardTitle: "Failed Skill/MCP",
    noSuccessRecords: "No success records yet",
    noFailureRecords: "No failures yet",
    boardDetail: "Details",
    lastSeen: "Last seen",
    successDetail: "Completed without a flagged Skill/MCP failure.",
    boardTitle: "Frequent issues",
    noRealRecords: "No real runs yet",
    overviewLabel: "Skill/MCP success and failure counts",
    failureDetail: "Failure reason",
    noReceipt: "No failure",
    noCaseYet: "Listening · no failures yet",
    noEvidence: "Only real Skill/MCP failures show here.",
    estimatedReceipt: "Failure receipt",
    type: "Type",
    source: "Source",
    target: "Target",
    detail: "Detail",
    callId: "Call ID",
    provenance: "Identity",
    sourceTypes: {
      skill_file_read: "Skill file read",
      mcp_tool_call: "MCP tool call",
      tool_context: "Tool context",
      unknown: "Unknown source"
    },
    estimatedTokens: "estimated tokens",
    cost: "Cost",
    recentTraces: "Recent traces",
    evidenceCaptured: "Evidence candidate captured.",
    failureJudgement: "Failure judgement",
    localRuleFailure: "Local rule caught a hard Skill/MCP failure. Codex is checking the nearby context.",
    savedCases: "History",
    noSavedCases: "No history yet",
    pause: "Pause",
    resume: "Resume",
    export: "Export",
    copy: "Copy",
    exported: "Exported local receipt",
    exportFailed: "Export failed",
    copied: "Copied receipt",
    copyFailed: "Copy failed",
    language: "Language",
    englishUi: "English UI",
    chineseUi: "Chinese UI",
    languageChanged: "Language changed",
    reviewModel: "Codex review model",
    reviewModelValue: "Review model",
    codexDefaultModel: "Codex default",
    selectedModel: "Selected",
    unsavedModel: "Unsaved",
    savedModel: "Saved",
    sourceModel: "Session model",
    reviewModelHelp: "Used for soft-failure checks and case review.",
    saveModel: "Save model",
    modelSaved: "Model saved",
    storage: "Storage",
    statistics: "Statistics",
    todayStats: "Today",
    allStats: "All-time",
    statsScopeChanged: "Stats range changed",
    clearOldHistory: "Clear old history",
    oldHistoryCleared: "Old history cleared",
    clearHistoryFailed: "Clear failed",
    window: "Window",
    windowSize: "Size",
    windowSizeChanged: "Window size changed",
    sizeLabels: {
      small: "S",
      large: "L"
    },
    keepOnTop: "Keep on top",
    on: "On",
    off: "Off",
    windowControlUnavailable: "Window control unavailable",
    diagnostics: "Diagnostics",
    configCheck: "Config check",
    runConfigCheck: "Check config",
    codexCli: "Codex CLI",
    codexSessions: "Codex sessions",
    localConfig: "Local config",
    localHistory: "Local history",
    modelsFound: "Models found",
    checkOk: "OK",
    checkMissing: "Missing",
    demoSample: "Demo sample",
    demoLoaded: "Demo sample loaded",
    waiting: "Waiting",
    sourceNames: {
      codex_session: "Codex session",
      hook: "Hook",
      demo: "Demo",
      local: "Local"
    },
    review: {
      label: "Failure analysis",
      localSignal: "Local signal",
      reviewed: "Codex reviewed",
      pending: "Analysis pending",
      unavailable: "Analysis unavailable",
      fallback: "Reviewed by local Codex"
    },
    verdicts: {
      tool_fault: "Tool fault",
      model_fault: "Model fault",
      unclear: "Unclear"
    }
  },
  zh: {
    localOnly: "仅本机",
    settings: "设置",
    closeSettings: "关闭设置",
    listening: "监听中",
    paused: "已暂停",
    starting: "启动中",
    portError: "端口异常",
    portOccupied: "8080 端口被占用",
    coreMissing: "本地引擎缺失",
    coreStartFailed: "本地引擎启动失败",
    coreOffline: "本地引擎离线",
    liveCodex: "正在读取 Codex 活动",
    hookOnly: "仅 Hook",
    waitingCodex: "等待 Codex 活动",
    never: "从未",
    justNow: "刚刚",
    clean: "成功",
    flagged: "失败",
    flag: "失败",
    successBoardTitle: "成功的 Skill/MCP",
    failureBoardTitle: "失败的 Skill/MCP",
    noSuccessRecords: "暂无成功记录",
    noFailureRecords: "暂无失败记录",
    boardDetail: "详情",
    lastSeen: "最近出现",
    successDetail: "本次没有抓到 Skill/MCP 失败。",
    boardTitle: "高频失败项",
    noRealRecords: "暂无真实运行记录",
    overviewLabel: "Skill/MCP 成功和失败统计",
    failureDetail: "失败原因",
    noReceipt: "暂无失败",
    noCaseYet: "监听中 · 暂无失败",
    noEvidence: "这里只显示真实 Skill/MCP 失败。",
    estimatedReceipt: "失败账单",
    type: "类型",
    source: "来源",
    target: "目标",
    detail: "细节",
    callId: "调用 ID",
    provenance: "命名",
    sourceTypes: {
      skill_file_read: "读取 Skill 文件",
      mcp_tool_call: "MCP 工具调用",
      tool_context: "工具上下文",
      unknown: "未知来源"
    },
    estimatedTokens: "预估 tokens",
    cost: "费用",
    recentTraces: "最近记录",
    evidenceCaptured: "已抓到一条候选证据。",
    failureJudgement: "失败判断",
    localRuleFailure: "本地规则抓到 Skill/MCP 硬失败，Codex 正在复核附近上下文。",
    savedCases: "历史记录",
    noSavedCases: "暂无历史",
    pause: "暂停",
    resume: "继续",
    export: "导出",
    copy: "复制",
    exported: "已导出本地账单",
    exportFailed: "导出失败",
    copied: "账单已复制",
    copyFailed: "复制失败",
    language: "语言",
    englishUi: "英文界面",
    chineseUi: "中文界面",
    languageChanged: "语言已切换",
    reviewModel: "Codex 复核模型",
    reviewModelValue: "复核模型",
    codexDefaultModel: "Codex 默认模型",
    selectedModel: "当前选择",
    unsavedModel: "未保存",
    savedModel: "已保存",
    sourceModel: "会话模型",
    reviewModelHelp: "用于软失败语义判断和失败复核。",
    saveModel: "保存模型",
    modelSaved: "模型已保存",
    storage: "存储",
    statistics: "统计",
    todayStats: "今日",
    allStats: "总计",
    statsScopeChanged: "统计范围已切换",
    clearOldHistory: "清理旧历史",
    oldHistoryCleared: "旧历史已清理",
    clearHistoryFailed: "清理失败",
    window: "窗口",
    windowSize: "大小",
    windowSizeChanged: "窗口大小已切换",
    sizeLabels: {
      small: "小",
      large: "大"
    },
    keepOnTop: "保持置顶",
    on: "开",
    off: "关",
    windowControlUnavailable: "窗口控制不可用",
    diagnostics: "诊断",
    configCheck: "配置检测",
    runConfigCheck: "检测配置",
    codexCli: "Codex CLI",
    codexSessions: "Codex 会话",
    localConfig: "本地配置",
    localHistory: "本地记录",
    modelsFound: "模型数量",
    checkOk: "正常",
    checkMissing: "缺失",
    demoSample: "示例样本",
    demoLoaded: "示例已生成",
    waiting: "等待中",
    sourceNames: {
      codex_session: "Codex 会话",
      hook: "Hook",
      demo: "演示",
      local: "本机"
    },
    review: {
      label: "失败分析",
      localSignal: "本地信号",
      reviewed: "Codex 已复核",
      pending: "分析中",
      unavailable: "分析不可用",
      fallback: "已由本地 Codex 复核"
    },
    verdicts: {
      tool_fault: "工具问题",
      model_fault: "模型问题",
      unclear: "不确定"
    }
  }
} as const;

type Copy = (typeof COPY)[Lang];

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

function initialLanguage(): Lang {
  const saved = window.localStorage.getItem("vibe-dashcam-language");
  if (saved === "en" || saved === "zh") return saved;
  return window.navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

function initialWindowSize(): WindowSizeName {
  const saved = window.localStorage.getItem(WINDOW_SIZE_KEY);
  return saved === "large" ? "large" : "small";
}

function formatTime(value: string | null | undefined, lang: Lang, copy: Copy) {
  if (!value) return copy.never;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return copy.justNow;
  return date.toLocaleTimeString(lang === "zh" ? "zh-CN" : undefined, {
    hour: "2-digit",
    minute: "2-digit"
  });
}

function formatReceipt(item: CaseItem | null | undefined, lang: Lang) {
  const copy = COPY[lang];
  if (!item) return `${copy.noReceipt}.`;
  const traces = (item.recent_events || [])
    .slice(-3)
    .map((event) => {
      const target = traceTarget(event);
      const snippet = event.ai_output || event.user_input || event.event_type || "";
      return `- ${event.event_type || "event"} | ${displayTarget(target)}: ${String(snippet).slice(0, 160)}`;
    })
    .join("\n");
  const cost = typeof item.wasted_cost === "number" ? item.wasted_cost.toFixed(4) : "?";
  return [
    copy.estimatedReceipt,
    `${copy.type}: ${item.evidence_type || "Evidence"}`,
    `${copy.source}: ${sourceName(item.source_kind, copy)}`,
    `${copy.provenance}: ${sourceTypeName(item.source_type, copy)}`,
    `${copy.target}: ${displayTarget(caseTarget(item))}`,
    `${copy.detail}: ${caseDetail(item)}`,
    item.source_call_id ? `${copy.callId}: ${item.source_call_id}` : "",
    `${copy.estimatedTokens}: ${item.wasted_tokens ?? "?"}`,
    `${copy.cost}: $${cost}`,
    reviewReceiptLine(item.case_review, copy),
    "",
    item.summary || copy.evidenceCaptured,
    traces ? `\n${copy.recentTraces}:\n${traces}` : ""
  ].join("\n");
}

function displayTarget(value?: string | null) {
  if (!value) return "unknown";
  if (value.startsWith("mcp__")) {
    return value.slice(5).replaceAll("__", ".");
  }
  const normalized = value.replaceAll("\\", "/");
  if (normalized.endsWith("/SKILL.md")) {
    const parts = normalized.split("/");
    return parts[parts.length - 2] || value;
  }
  return value;
}

function caseTarget(item: CaseItem | null | undefined) {
  return item?.source_display_name || item?.source_target || item?.suspected_skill || "unknown";
}

function caseMatchesTarget(item: CaseItem | null | undefined, target: string) {
  return [caseTarget(item), item?.source_display_name, item?.source_target, item?.suspected_skill].includes(target);
}

function caseDetail(item: CaseItem | null | undefined) {
  return item?.source_detail || item?.source_target || item?.suspected_skill || "unknown";
}

function traceTarget(event: TraceEvent) {
  return event.skill_name || event.tool_name || event.client || "unknown";
}

function traceDetail(event: TraceEvent) {
  return event.skill_source || event.tool_name || event.call_id || event.ai_output || event.user_input || "";
}

function sourceTypeName(value: string | null | undefined, copy: Copy) {
  if (value === "skill_file_read") return copy.sourceTypes.skill_file_read;
  if (value === "mcp_tool_call") return copy.sourceTypes.mcp_tool_call;
  if (value === "tool_context") return copy.sourceTypes.tool_context;
  return copy.sourceTypes.unknown;
}

function targetKind(value: string | null | undefined) {
  if (!value) return "Skill";
  if (value.startsWith("mcp__")) return "MCP";
  if (value.replaceAll("\\", "/").endsWith("/SKILL.md")) return "Skill";
  return "Skill";
}

function sourceName(value: string | null | undefined, copy: Copy) {
  if (value === "codex_session") return copy.sourceNames.codex_session;
  if (value === "hook") return copy.sourceNames.hook;
  if (value === "demo") return copy.sourceNames.demo;
  return copy.sourceNames.local;
}

function coreStatusLabel(value: string, copy: Copy) {
  if (value === "port_occupied") return copy.portOccupied;
  if (value === "core_missing") return copy.coreMissing;
  if (value === "core_start_failed") return copy.coreStartFailed;
  return copy.coreOffline;
}

function stateLabel(state: AppState, apiOnline: boolean, coreStatus: string, copy: Copy) {
  if (!apiOnline) return coreStatusLabel(coreStatus, copy);
  if (state.server_error) return copy.portError;
  if (state.paused) return copy.paused;
  if (state.listening) return copy.listening;
  return copy.starting;
}

function reviewReceiptLine(review: CaseReview | undefined, copy: Copy) {
  if (!review) return `${copy.review.label}: ${copy.review.pending}`;
  if (review.status === "reviewed") {
    return `${copy.review.label}: ${verdictName(review.verdict, copy)} (${Math.round((review.confidence || 0) * 100)}%)`;
  }
  if (review.status === "unavailable") return `${copy.review.label}: ${copy.review.unavailable}`;
  return `${copy.review.label}: ${copy.review.pending}`;
}

function reviewSummary(review: CaseReview | undefined, copy: Copy) {
  if (!review || review.status === "pending") return copy.review.pending;
  if (review.status === "unavailable") return copy.review.unavailable;
  return `${verdictName(review.verdict, copy)} · ${review.one_sentence_summary || copy.review.fallback}`;
}

function reviewStage(review: CaseReview | undefined, copy: Copy) {
  if (review?.status === "reviewed") return copy.review.reviewed;
  if (review?.status === "unavailable") return copy.review.unavailable;
  return copy.review.localSignal;
}

function failureJudgement(item: CaseItem | null | undefined, copy: Copy) {
  const review = item?.case_review;
  if (review?.status === "reviewed" && review.one_sentence_summary) {
    return review.one_sentence_summary;
  }
  if (review?.status === "unavailable") {
    return copy.review.unavailable;
  }
  return item ? copy.localRuleFailure : copy.noEvidence;
}

function verdictName(value: CaseReview["verdict"] | undefined, copy: Copy) {
  if (value === "tool_fault") return copy.verdicts.tool_fault;
  if (value === "model_fault") return copy.verdicts.model_fault;
  return copy.verdicts.unclear;
}

const MODEL_LABELS: Record<string, string> = {
  "gpt-5.5": "GPT-5.5",
  "gpt-5.4": "GPT-5.4",
  "gpt-5.4-mini": "GPT-5.4 Mini",
  "gpt-5.3-codex-spark": "GPT-5.3 Codex Spark",
};

function modelName(value: string | null | undefined, copy: Copy, defaultModel?: string | null) {
  if (value && value !== "default") return MODEL_LABELS[value] || value;
  return defaultModel && defaultModel !== "default"
    ? `${copy.codexDefaultModel} (${MODEL_LABELS[defaultModel] || defaultModel})`
    : copy.codexDefaultModel;
}

export function App() {
  const [lang, setLang] = useState<Lang>(initialLanguage);
  const [state, setState] = useState<AppState>(emptyState);
  const [cases, setCases] = useState<CaseItem[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [boardMode, setBoardMode] = useState<BoardMode>("failure");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [selectedBoardTarget, setSelectedBoardTarget] = useState<string | null>(null);
  const [apiOnline, setApiOnline] = useState(false);
  const [coreStatus, setCoreStatus] = useState("starting");
  const [flashId, setFlashId] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState("");
  const [demoCase, setDemoCase] = useState<CaseItem | null>(null);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsPayload | null>(null);
  const [modelDraft, setModelDraft] = useState("default");
  const [windowSize, setWindowSize] = useState<WindowSizeName>(initialWindowSize);
  const [alwaysOnTop, setAlwaysOnTop] = useState(() => window.localStorage.getItem("vibe-dashcam-always-on-top") === "true");
  const latestCaseIdRef = useRef<string | null>(null);
  const copy = COPY[lang];

  async function refresh() {
    try {
      let launchStatus = "ok";
      try {
        launchStatus = await invoke<string>("core_launch_status");
      } catch {
        launchStatus = "ok";
      }
      if (launchStatus === "port_occupied" || launchStatus === "core_missing" || launchStatus === "core_start_failed") {
        setApiOnline(false);
        setCoreStatus(launchStatus);
        setState(emptyState);
        setCases([]);
        return;
      }
      const nextState = await requestJson<AppState>("/state");
      setApiOnline(true);
      setCoreStatus(launchStatus);
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
      try {
        setCoreStatus(await invoke<string>("core_launch_status"));
      } catch {
        setCoreStatus("core_offline");
      }
    }
  }

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 900);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    invoke("set_window_always_on_top", { enabled: alwaysOnTop }).catch(() => undefined);
  }, []);

  useEffect(() => {
    applyWindowSize(windowSize, false);
  }, []);

  useEffect(() => {
    if (!settingsOpen) {
      setModelDraft(state.review_model || "default");
    }
  }, [settingsOpen, state.review_model]);

  const selectedCase = useMemo(() => {
    return cases.find((item) => item.id === selectedCaseId) || state.latest_case || cases[0] || null;
  }, [cases, selectedCaseId, state.latest_case]);
  const hasFailureCase = Boolean(selectedCase);

  const success = Number(state.display_ok_event_count ?? state.ok_event_count ?? 0);
  const failures = Number(state.display_failure_count ?? state.failure_count ?? 0);
  const totalEvents = success + failures;
  const successPct = totalEvents ? Math.round((success / totalEvents) * 100) : 0;
  const failurePct = totalEvents ? 100 - successPct : 0;
  const rows = state.skill_board || [];
  const successRows = rows.filter((row) => row.success > 0).slice(0, 8);
  const failureRows = rows.filter((row) => row.failure > 0).slice(0, 8);
  const boardRows = boardMode === "success" ? successRows : failureRows;
  const boardTitle = boardMode === "success" ? copy.successBoardTitle : copy.failureBoardTitle;
  const boardEmpty = boardMode === "success" ? copy.noSuccessRecords : copy.noFailureRecords;
  const reviewModelOptions = useMemo(() => {
    const values = ["default", ...(state.review_model_options || []), state.review_model || ""];
    return Array.from(new Set(values.filter(Boolean)));
  }, [state.review_model_options, state.review_model]);
  const savedReviewModel = state.review_model || "default";
  const modelChanged = modelDraft !== savedReviewModel;
  const codexSource = state.source_status?.codex_session;
  const sourceLabel = codexSource?.active && codexSource?.last_seen_at
    ? copy.liveCodex
    : state.tailer_error === "codex_sessions_not_found"
      ? copy.hookOnly
      : copy.waitingCodex;
  useEffect(() => {
    if (!hasFailureCase && expanded) {
      setExpanded(false);
    }
  }, [expanded, hasFailureCase]);

  function setUiLanguage(next: Lang) {
    setLang(next);
    window.localStorage.setItem("vibe-dashcam-language", next);
    setSaveNote(COPY[next].languageChanged);
    window.setTimeout(() => setSaveNote(""), 1200);
  }

  async function setKeepOnTop(next: boolean) {
    setAlwaysOnTop(next);
    window.localStorage.setItem("vibe-dashcam-always-on-top", String(next));
    try {
      await invoke("set_window_always_on_top", { enabled: next });
      setSaveNote(next ? `${copy.keepOnTop}: ${copy.on}` : `${copy.keepOnTop}: ${copy.off}`);
    } catch {
      setSaveNote(copy.windowControlUnavailable);
    }
    window.setTimeout(() => setSaveNote(""), 1400);
  }

  async function applyWindowSize(next: WindowSizeName, notify = true) {
    const size = WINDOW_SIZES[next];
    const appWindow = getCurrentWindow();
    try {
      const [position, oldSize] = await Promise.all([
        appWindow.outerPosition(),
        appWindow.outerSize()
      ]);
      await appWindow.setSize(new LogicalSize(size.width, size.height));
      const newSize = await appWindow.outerSize();
      await appWindow.setPosition(new PhysicalPosition(
        position.x + oldSize.width - newSize.width,
        position.y + oldSize.height - newSize.height
      ));
      setWindowSize(next);
      window.localStorage.setItem(WINDOW_SIZE_KEY, next);
      if (notify) {
        setSaveNote(`${copy.windowSizeChanged}: ${copy.sizeLabels[next]}`);
        window.setTimeout(() => setSaveNote(""), 1200);
      }
    } catch {
      if (notify) {
        setSaveNote(copy.windowControlUnavailable);
        window.setTimeout(() => setSaveNote(""), 1400);
      }
    }
  }

  async function setStatsScope(next: StatsScope) {
    const payload = await requestJson<{ state: AppState }>("/control/stats-scope", {
      method: "POST",
      body: JSON.stringify({ scope: next })
    });
    setState(payload.state);
    setSelectedCaseId(null);
    setSaveNote(copy.statsScopeChanged);
    window.setTimeout(() => setSaveNote(""), 1200);
    await refresh();
  }

  async function clearOldHistory() {
    try {
      const payload = await requestJson<{ state: AppState; removed: number }>("/cases/clear-old", {
        method: "POST",
        body: "{}"
      });
      setState(payload.state);
      setSelectedCaseId(null);
      setSaveNote(`${copy.oldHistoryCleared}: ${payload.removed}`);
      await refresh();
    } catch {
      setSaveNote(copy.clearHistoryFailed);
    }
    window.setTimeout(() => setSaveNote(""), 1600);
  }

  async function togglePause() {
    const payload = await requestJson<{ state: AppState }>("/control/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !state.paused })
    });
    setState(payload.state);
  }

  async function saveReviewModel() {
    const payload = await requestJson<{ state: AppState; review_model: string }>("/control/review-model", {
      method: "POST",
      body: JSON.stringify({ model: modelDraft })
    });
    setState(payload.state);
    setModelDraft(payload.review_model || "default");
    setSaveNote(copy.modelSaved);
    window.setTimeout(() => setSaveNote(""), 1400);
  }

  async function testCapture() {
    const payload = await requestJson<{ demo_case?: CaseItem }>("/test-capture", { method: "POST", body: "{}" });
    setDemoCase(payload.demo_case || null);
    setSaveNote(copy.demoLoaded);
    window.setTimeout(() => setSaveNote(""), 1200);
    await refresh();
  }

  async function checkConfig() {
    const payload = await requestJson<DiagnosticsPayload>("/diagnostics");
    setDiagnostics(payload);
  }

  async function exportCase() {
    if (!selectedCase) return;
    try {
      const payload = await requestJson<{ path: string; exists?: boolean }>("/cases/save", {
        method: "POST",
        body: JSON.stringify({ case_id: selectedCase.id })
      });
      setSaveNote(payload.exists === false ? copy.exportFailed : copy.exported);
      console.info(payload.path);
    } catch {
      setSaveNote(copy.exportFailed);
    }
    window.setTimeout(() => setSaveNote(""), 1600);
  }

  async function copyReceipt() {
    try {
      await navigator.clipboard.writeText(formatReceipt(selectedCase, lang));
      setSaveNote(copy.copied);
    } catch {
      setSaveNote(copy.copyFailed);
    }
    window.setTimeout(() => setSaveNote(""), 1600);
  }

  function startWindowDrag(event: React.MouseEvent<HTMLElement>) {
    if (event.button !== 0) return;
    const target = event.target as HTMLElement;
    if (target.closest("button, select, input, textarea, a")) return;
    event.preventDefault();
    getCurrentWindow().startDragging().catch(() => undefined);
  }

  return (
    <main className="shell">
      <section
        className={`glass-card ${expanded ? "is-expanded" : ""} ${hasFailureCase ? "has-case" : "no-case"}`}
        onMouseDown={startWindowDrag}
      >
        <div className="drag-strip" onMouseDown={startWindowDrag} aria-hidden="true" />
        <header className="topbar" data-tauri-drag-region>
          <div className="brand">
            <Radar size={18} />
            <div>
              <strong>Vibe-Dashcam</strong>
              <span>{copy.localOnly}</span>
            </div>
          </div>
          <div className="top-actions">
            <span
              className={`status-dot ${apiOnline && state.listening && !state.paused ? "ok" : "warn"}`}
              role="status"
              aria-label={stateLabel(state, apiOnline, coreStatus, copy)}
              title={stateLabel(state, apiOnline, coreStatus, copy)}
            />
            <div className="segmented-control size-control" aria-label={copy.windowSize}>
              {(Object.keys(WINDOW_SIZES) as WindowSizeName[]).map((size) => (
                <button
                  key={size}
                  type="button"
                  className={windowSize === size ? "active" : ""}
                  onClick={() => applyWindowSize(size)}
                  title={`${copy.windowSize}: ${copy.sizeLabels[size]}`}
                >
                  {copy.sizeLabels[size]}
                </button>
              ))}
            </div>
            <button
              className="icon-button"
              onClick={() => {
                setModelDraft(state.review_model || "default");
                setSettingsOpen(true);
              }}
              aria-label={copy.settings}
            >
              <Settings size={17} />
            </button>
          </div>
        </header>

        <div className="status-line">
          <span>{stateLabel(state, apiOnline, coreStatus, copy)}</span>
          <span>{sourceLabel} / {formatTime(state.last_event_at, lang, copy)}</span>
        </div>

        <section className="split-panel" aria-label={copy.overviewLabel}>
          <div className="split-copy">
            <button className={`split-count success ${boardMode === "success" ? "active" : ""}`} onClick={() => {
              setBoardMode("success");
              setSelectedBoardTarget(null);
            }}>
              {success} {copy.clean}
            </button>
            <button className={`split-count failure ${boardMode === "failure" ? "active" : ""}`} onClick={() => {
              setBoardMode("failure");
              setSelectedBoardTarget(null);
            }}>
              {failures} {copy.flagged}
            </button>
          </div>
          <div className="split-bar">
            <button className="success-bar" style={{ width: `${successPct}%` }} onClick={() => {
              setBoardMode("success");
              setSelectedBoardTarget(null);
            }} aria-label={copy.successBoardTitle} />
            <button className="failure-bar" style={{ width: `${failurePct}%` }} onClick={() => {
              setBoardMode("failure");
              setSelectedBoardTarget(null);
            }} aria-label={copy.failureBoardTitle} />
          </div>
        </section>

        {hasFailureCase && (
        <section className={`latest ${expanded ? "expanded" : ""} has-case ${flashId === selectedCase?.id ? "flash" : ""}`}>
          <button
            className="latest-head"
            onClick={() => {
              if (hasFailureCase) setExpanded(!expanded);
            }}
            disabled={!hasFailureCase}
          >
            <div>
              <span>{selectedCase ? copy.failureDetail : copy.noReceipt}</span>
              <strong title={selectedCase ? displayTarget(caseTarget(selectedCase)) : copy.noCaseYet}>
                {selectedCase ? displayTarget(caseTarget(selectedCase)) : copy.noCaseYet}
              </strong>
            </div>
            {hasFailureCase && (expanded ? <ChevronUp size={18} /> : <ChevronDown size={18} />)}
          </button>
          {expanded && (
            <div className="latest-body">
              <div className="judgement-card">
                <div className="judgement-head">
                  <span>{copy.failureJudgement}</span>
                  <small className={selectedCase?.case_review?.status || "pending"}>{reviewStage(selectedCase?.case_review, copy)}</small>
                </div>
                <p>{failureJudgement(selectedCase, copy)}</p>
              </div>
              <div className="case-meta">
                <span>{selectedCase ? sourceName(selectedCase.source_kind, copy) : copy.waiting}</span>
                <span>{selectedCase?.wasted_tokens ?? 0} {copy.estimatedTokens}</span>
              </div>
              {selectedCase && (
                <div className="source-grid" aria-label={copy.provenance}>
                  <span>{copy.type}</span>
                  <b>{sourceTypeName(selectedCase.source_type, copy)}</b>
                  <span>{copy.target}</span>
                  <b title={caseTarget(selectedCase)}>{displayTarget(caseTarget(selectedCase))}</b>
                  {(selectedCase.source_model || selectedCase.case_review?.model || state.review_model) && (
                    <>
                      <span>{copy.sourceModel}</span>
                      <b>{selectedCase.source_model || selectedCase.case_review?.model || state.review_model}</b>
                    </>
                  )}
                  {selectedCase.source_call_id && (
                    <>
                      <span>{copy.callId}</span>
                      <b title={selectedCase.source_call_id}>{selectedCase.source_call_id}</b>
                    </>
                  )}
                </div>
              )}
              <div className={`review-line ${selectedCase?.case_review?.status || "pending"}`}>
                <span>{reviewSummary(selectedCase?.case_review, copy)}</span>
                {selectedCase?.case_review?.status === "reviewed" && (
                  <small>{Math.round((selectedCase.case_review.confidence || 0) * 100)}%</small>
                )}
              </div>
              <div className="trace-list">
                {(selectedCase?.recent_events || []).slice(-3).map((event, index) => (
                  <span key={`${event.event_type}-${index}`}>
                    <b>{event.event_type || "event"}</b>
                    <i title={traceDetail(event)}>
                      {displayTarget(traceTarget(event))}
                      {traceDetail(event) ? ` / ${traceDetail(event)}` : ""}
                    </i>
                  </span>
                ))}
              </div>
              <div className="detail-actions">
                <button onClick={exportCase} disabled={!selectedCase}><Download size={14} />{copy.export}</button>
                <button onClick={copyReceipt} disabled={!selectedCase}><Clipboard size={14} />{copy.copy}</button>
              </div>
            </div>
          )}
        </section>
        )}

        {!expanded && (boardRows.length > 0 || rows.length > 0 || boardMode) && (
          <section className="board">
            <div className="section-title">
              <span>{boardTitle}</span>
              <Activity size={14} />
            </div>
            <div className="board-list">
              {boardRows.length === 0 && <div className="empty">{boardEmpty}</div>}
              {boardRows.map((row) => {
                const rowTotal = Math.max(row.success + row.failure, 1);
                const rowFailPct = Math.round((row.failure / rowTotal) * 100);
                const rowCase = caseMatchesTarget(state.latest_case, row.target)
                  ? state.latest_case
                  : cases.find((item) => caseMatchesTarget(item, row.target));
                const selected = selectedBoardTarget === row.target;
                return (
                  <div className={`board-item ${selected ? "selected" : ""}`} key={row.target}>
                    <button
                      className={`board-row ${boardMode === "failure" ? "has-fail" : "clean"}`}
                      onClick={() => {
                        setSelectedBoardTarget(selected ? null : row.target);
                        if (rowCase?.id) setSelectedCaseId(rowCase.id);
                        setExpanded(false);
                      }}
                      title={displayTarget(row.target)}
                    >
                      <div className="row-main">
                        <div className="target-title">
                          <b>{targetKind(row.target)}</b>
                          <span>{displayTarget(row.target)}</span>
                        </div>
                        <small>{boardMode === "success" ? row.success : row.failure} {boardMode === "success" ? copy.clean : copy.flag}</small>
                      </div>
                      <div className="row-bar">
                        <i className="row-ok" style={{ width: `${100 - rowFailPct}%` }} />
                        <i className="row-bad" style={{ width: `${rowFailPct}%` }} />
                      </div>
                    </button>
                    {selected && (
                      <div className={`board-detail ${boardMode}`}>
                        <div className="board-detail-head">
                          <span>{copy.boardDetail}</span>
                          <b>{boardMode === "failure" ? sourceName(rowCase?.source_kind, copy) : targetKind(row.target)}</b>
                        </div>
                        {boardMode === "failure" ? (
                          <>
                            <p>{rowCase ? failureJudgement(rowCase, copy) : copy.noEvidence}</p>
                            <div className="mini-meta">
                              <span>{copy.source}: {sourceTypeName(rowCase?.source_type, copy)}</span>
                              <span>{copy.estimatedTokens}: {rowCase?.wasted_tokens ?? row.tokens}</span>
                            </div>
                            {rowCase?.case_review && (
                              <div className={`review-line compact ${rowCase.case_review.status || "pending"}`}>
                                <span>{reviewSummary(rowCase.case_review, copy)}</span>
                              </div>
                            )}
                          </>
                        ) : (
                          <>
                            <p>{copy.successDetail}</p>
                            <div className="mini-meta">
                              <span>{copy.clean}: {row.success}</span>
                              <span>{copy.lastSeen}: {formatTime(row.last_seen, lang, copy)}</span>
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        <footer className="actions">
          <button onClick={togglePause} disabled={!apiOnline}>
            {state.paused ? <Play size={15} /> : <Pause size={15} />}
            {state.paused ? copy.resume : copy.pause}
          </button>
        </footer>
        {saveNote && <div className="toast">{saveNote}</div>}
      </section>

      {settingsOpen && (
        <aside className="settings-drawer">
          <div
            className="drawer-head"
            onMouseDown={startWindowDrag}
          >
            <strong>{copy.settings}</strong>
            <button className="icon-button" onClick={() => setSettingsOpen(false)} aria-label={copy.closeSettings}>
              <X size={17} />
            </button>
          </div>
          <div className="setting-block">
            <strong>{copy.language}</strong>
            <div className="setting-row">
              <span><Languages size={12} />{lang === "zh" ? copy.chineseUi : copy.englishUi}</span>
              <div className="segmented-control">
                <button type="button" className={lang === "en" ? "active" : ""} onClick={() => setUiLanguage("en")}>EN</button>
                <button type="button" className={lang === "zh" ? "active" : ""} onClick={() => setUiLanguage("zh")}>中</button>
              </div>
            </div>
          </div>
          <div className="setting-block">
            <strong>{copy.reviewModel}</strong>
            <div className="model-summary">
              <span>{copy.selectedModel}</span>
              <b>{modelName(modelDraft, copy, state.review_default_model)}</b>
              {modelChanged && <em>{copy.unsavedModel}</em>}
            </div>
            <label className="model-control">
              <span>{copy.reviewModelValue}</span>
              <select
                value={modelDraft}
                onChange={(event) => setModelDraft(event.target.value)}
              >
                {reviewModelOptions.map((model) => (
                  <option key={model} value={model}>{modelName(model, copy, state.review_default_model)}</option>
                ))}
              </select>
            </label>
            <small>{copy.reviewModelHelp}</small>
            <button className="demo-button" onClick={saveReviewModel} disabled={!apiOnline || modelDraft === savedReviewModel}>
              {copy.saveModel}
            </button>
          </div>
          <div className="setting-block">
            <strong>{copy.storage}</strong>
            <div className="setting-row">
              <span><Activity size={12} />{copy.statistics}</span>
              <div className="segmented-control">
                <button
                  type="button"
                  className={(state.stats_scope || "today") === "today" ? "active" : ""}
                  onClick={() => setStatsScope("today")}
                >
                  {copy.todayStats}
                </button>
                <button
                  type="button"
                  className={state.stats_scope === "all" ? "active" : ""}
                  onClick={() => setStatsScope("all")}
                >
                  {copy.allStats}
                </button>
              </div>
            </div>
            <button className="demo-button" onClick={clearOldHistory} disabled={!apiOnline}>
              {copy.clearOldHistory}
            </button>
          </div>
          <div className="setting-block">
            <strong>{copy.window}</strong>
            <div className="setting-row">
              <span><Pin size={12} />{copy.keepOnTop}</span>
              <button
                type="button"
                className={`switch ${alwaysOnTop ? "on" : ""}`}
                aria-pressed={alwaysOnTop}
                onClick={() => setKeepOnTop(!alwaysOnTop)}
              >
                <i />
                <b>{alwaysOnTop ? copy.on : copy.off}</b>
              </button>
            </div>
          </div>
          <div className="setting-block">
            <strong>{copy.diagnostics}</strong>
            <button className="demo-button" onClick={checkConfig} disabled={!apiOnline}>
              <Activity size={14} />
              {copy.runConfigCheck}
            </button>
            {diagnostics && (
              <div className="diagnostic-list" aria-label={copy.configCheck}>
                <span>
                  <b className={diagnostics.codex_cli?.found ? "ok" : "warn"} />
                  {copy.codexCli}
                  <i>{diagnostics.codex_cli?.found ? copy.checkOk : copy.checkMissing}</i>
                </span>
                <span title={diagnostics.codex_sessions?.path || ""}>
                  <b className={diagnostics.codex_sessions?.exists ? "ok" : "warn"} />
                  {copy.codexSessions}
                  <i>{diagnostics.codex_sessions?.active ? copy.liveCodex : diagnostics.codex_sessions?.exists ? copy.waitingCodex : copy.checkMissing}</i>
                </span>
                <span title={diagnostics.local_config?.path || ""}>
                  <b className={diagnostics.local_config?.exists ? "ok" : "warn"} />
                  {copy.localConfig}
                  <i>{diagnostics.local_config?.exists ? copy.checkOk : copy.checkMissing}</i>
                </span>
                <span title={diagnostics.local_history?.path || ""}>
                  <b className={diagnostics.local_history?.exists ? "ok" : "warn"} />
                  {copy.localHistory}
                  <i>{diagnostics.local_history?.exists ? copy.checkOk : copy.checkMissing}</i>
                </span>
                <span>
                  <b className={(diagnostics.review_model_options?.length || 0) > 1 ? "ok" : "warn"} />
                  {copy.modelsFound}
                  <i>{diagnostics.review_model_options?.length || 0}</i>
                </span>
              </div>
            )}
            <button className="demo-button" onClick={testCapture} disabled={!apiOnline}>
              <TestTube2 size={14} />
              {copy.demoSample}
            </button>
            {demoCase && <span>{displayTarget(caseTarget(demoCase))} · {demoCase.wasted_tokens ?? 0} {copy.estimatedTokens}</span>}
          </div>
        </aside>
      )}
    </main>
  );
}
