import { useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { createScan, downloadUrl, getParameter, getProgress, getReport, listScans } from "./api";

const WEIGHTS = { on_page: 0.4, off_page: 0.25, technical: 0.35 };

function band(score) {
  if (score == null) return { label: "Unknown", cls: "unknown" };
  if (score >= 90) return { label: "Excellent", cls: "excellent" };
  if (score >= 75) return { label: "Good", cls: "good" };
  if (score >= 60) return { label: "Fair", cls: "fair" };
  if (score >= 40) return { label: "Poor", cls: "poor" };
  return { label: "Critical", cls: "critical" };
}

function fmt(n, digits = 1) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits);
}

function friendlyError(error) {
  if (!error) return "";
  const t = String(error);
  if (/getaddrinfo|11001|Failed to resolve|Name or service not known|NameResolutionError/i.test(t)) {
    return "Could not reach this source. The hostname could not be resolved (DNS lookup failed).";
  }
  if (/timed? ?out|TimeoutException|ReadTimeout|ConnectTimeout/i.test(t)) {
    return "The request timed out before this source responded.";
  }
  if (/Connection refused|WinError 10061/i.test(t)) return "This source refused the connection.";
  if (/Connection reset|WinError 10054/i.test(t)) return "The connection was closed before a response was received.";
  if (/SSL|certificate|CERTIFICATE_VERIFY_FAILED/i.test(t)) return "The site's security certificate could not be verified.";
  if (/Network is unreachable|No route to host|ConnectError/i.test(t)) return "Could not connect to this source.";
  if (/^\[Errno |Traceback/i.test(t)) return "This source could not be reached. Retry the scan when the network is available.";
  return t;
}

function prettyKey(key) {
  return String(key || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function issueText(p) {
  if (p.status === "PASS") return "—";
  if (p.status === "UNKNOWN") return "Source unavailable";
  return p.status;
}

function parseEvidence(evidence) {
  if (evidence == null || evidence === "") return {};
  if (typeof evidence === "string") {
    try {
      return JSON.parse(evidence);
    } catch {
      return { raw: evidence };
    }
  }
  return evidence;
}

const STATUS_TONE = {
  ALLOWED: "good", ALLOW: "good", PASS: "good", PASSED: "good", TRUE: "good", YES: "good", OK: "good", FOUND: "good", PRESENT: "good", VALID: "good",
  BLOCKED: "bad", DENIED: "bad", DISALLOWED: "bad", FAIL: "bad", FAILED: "bad", FALSE: "bad", NO: "bad", ERROR: "bad", MISSING: "bad", INVALID: "bad", ABSENT: "bad",
  WARN: "warn", WARNING: "warn", PARTIAL: "warn",
  UNKNOWN: "neutral", NA: "neutral", "N/A": "neutral",
};

function statusTone(value) {
  const key = String(value).trim().toUpperCase();
  return STATUS_TONE[key];
}

function httpStatusTone(code) {
  if (code >= 200 && code < 300) return "good";
  if (code >= 300 && code < 400) return "warn";
  return "bad";
}

function Chip({ tone = "neutral", children }) {
  return <span className={`ev-chip ev-chip-${tone}`}>{children}</span>;
}

function isLongText(s) {
  return s.length > 140 || s.includes("\n");
}

function EvidenceValue({ value, keyHint }) {
  if (value == null || value === "") return <span className="ev-empty">Not recorded</span>;
  if (typeof value === "boolean") return <Chip tone={value ? "good" : "bad"}>{value ? "Yes" : "No"}</Chip>;
  if (typeof value === "number") {
    const hintsStatus = /status/i.test(keyHint || "") && value >= 100 && value < 600;
    if (hintsStatus) return <Chip tone={httpStatusTone(value)}>{value}</Chip>;
    return <span>{Number.isInteger(value) ? value.toLocaleString() : value}</span>;
  }
  if (typeof value === "string") {
    if (/^https?:\/\//i.test(value)) {
      return <a className="ev-link" href={value} target="_blank" rel="noreferrer">{value}</a>;
    }
    const tone = statusTone(value);
    if (tone && value.length <= 24) return <Chip tone={tone}>{value}</Chip>;
    const nice = friendlyError(value) || value;
    if (isLongText(nice)) return <pre className="ev-snippet">{nice}</pre>;
    return <span>{nice}</span>;
  }
  if (Array.isArray(value)) {
    if (!value.length) return <span className="ev-empty">None found</span>;
    if (value.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
      const cols = [...new Set(value.flatMap((row) => Object.keys(row || {})))].slice(0, 8);
      return (
        <div className="ev-table-wrap">
          <table className="ev-table">
            <thead>
              <tr>{cols.map((c) => <th key={c}>{prettyKey(c)}</th>)}</tr>
            </thead>
            <tbody>
              {value.slice(0, 20).map((row, i) => (
                <tr key={i}>
                  {cols.map((c) => (
                    <td key={c}><EvidenceValue value={row[c]} keyHint={c} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {value.length > 20 && <p className="ev-more">+ {value.length - 20} more</p>}
        </div>
      );
    }
    return (
      <ul className="ev-list">
        {value.slice(0, 20).map((item, i) => (
          <li key={i}><EvidenceValue value={item} /></li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    const rows = Object.entries(value).filter(([k]) => k !== "trace");
    if (!rows.length) return <span className="ev-empty">No details recorded</span>;
    return (
      <dl className="ev-dl">
        {rows.map(([k, v]) => (
          <div key={k} className="ev-row">
            <dt>{prettyKey(k)}</dt>
            <dd><EvidenceValue value={v} keyHint={k} /></dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span>{String(value)}</span>;
}

function EvidencePanel({ evidence, error }) {
  const ev = parseEvidence(evidence);
  const isPlainObject = ev && typeof ev === "object" && !Array.isArray(ev);
  const errorText = error ? String(error).trim().toLowerCase() : null;
  const duplicatesError = (v) => errorText && typeof v === "string" && v.trim().toLowerCase() === errorText;
  const rawSummary = isPlainObject && typeof ev.summary === "string" ? ev.summary : null;
  const summary = rawSummary && !duplicatesError(rawSummary) ? rawSummary : null;
  const detailRows = isPlainObject
    ? Object.entries(ev).filter(([k, v]) => k !== "summary" && k !== "trace" && !duplicatesError(v))
    : [];
  const json = JSON.stringify(ev, null, 2);
  const hasEvidence = summary || detailRows.length > 0;

  return (
    <div className="evidence">
      <h3>Evidence</h3>
      {error && (
        <div className="evidence-alert">
          <b>Unavailable</b>
          <p>{friendlyError(error)}</p>
        </div>
      )}
      {summary && <p className="evidence-lead">{summary}</p>}
      {detailRows.length > 0 && (
        <dl className="ev-dl evidence-structured">
          {detailRows.map(([k, v]) => (
            <div key={k} className="ev-row">
              <dt>{prettyKey(k)}</dt>
              <dd><EvidenceValue value={v} keyHint={k} /></dd>
            </div>
          ))}
        </dl>
      )}
      {!hasEvidence && !error && <p className="ev-empty evidence-none">No evidence was recorded for this parameter.</p>}
      {json && json !== "{}" && (
        <div className="evidence-raw">
          <h4 className="evidence-raw-label">Raw evidence (JSON)</h4>
          <pre className="evidence-json">{json}</pre>
        </div>
      )}
    </div>
  );
}

function Icon({ name }) {
  const common = { width: 18, height: 18, fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" };
  if (name === "home") return <svg {...common}><path d="M3 10.5 9 4l6 6.5" /><path d="M5 9.5V16h8V9.5" /></svg>;
  if (name === "chart") return <svg {...common}><path d="M2 10.5h3l1.8-5.2L10 15l1.8-8.2 1.4 3.7H16" /></svg>;
  if (name === "folder") return <svg {...common}><path d="M3 6h5l2 2h7v8H3z" /></svg>;
  if (name === "export") return <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M8 10V3" /><path d="m5 5 3-3 3 3" /><path d="M3 11v3h10v-3" /></svg>;
  if (name === "plus") return <svg width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 2v10M2 7h10" /></svg>;
  if (name === "globe") return <svg {...common} width="20" height="20"><circle cx="9" cy="9" r="7" /><path d="M2 9h14M9 2c2.2 2 3.5 4.5 3.5 7s-1.3 5-3.5 7c-2.2-2-3.5-4.5-3.5-7s1.3-5 3.5-7z" /></svg>;
  return null;
}

function Logo() {
  return (
    <svg width="26" height="26" viewBox="0 0 40 40" fill="none">
      <defs>
        <linearGradient id="logoGrad" x1="4" y1="30" x2="34" y2="6" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#1d4ed8" />
          <stop offset="0.55" stopColor="#60a5fa" />
          <stop offset="1" stopColor="#f97316" />
        </linearGradient>
      </defs>
      <path
        d="M32 10c-8.5 0-16 5.6-16 13.4 0 5.6 3.9 9 8 9 3.6 0 6.2-2.4 6.2-5.5 0-2.4-1.7-4.1-3.9-4.1-1.5 0-2.6.9-2.6 2.1"
        stroke="url(#logoGrad)"
        strokeWidth="3.6"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

function Shell({ children, view, onView, onSearch, search, toast }) {
  const [open, setOpen] = useState(null);
  const nav = useNavigate();
  const rail = [
    ["home", "home", "New report"],
    ["analytics", "chart", "Analytics"],
    ["reports", "folder", "Reports"],
  ];

  return (
    <div className="app">
      <header className="topnav">
        <button className="logo-btn" title="CiteSight" onClick={() => { onView("home"); nav("/"); }}><Logo /></button>
        <div className="search-wrap">
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") onView("analytics"); }}
            placeholder="Search"
          />
        </div>
        <div className="top-links">
          <button className="avatar" onClick={() => setOpen(open === "profile" ? null : "profile")} title="Account">A</button>
        </div>
        {open === "profile" && (
          <div className="dropdown" onMouseLeave={() => setOpen(null)}>
            <button onClick={() => { onView("settings"); setOpen(null); }}>Settings</button>
          </div>
        )}
      </header>
      <div className="workspace">
        <nav className="rail">
          {rail.map(([id, icon, label]) => (
            <button
              key={id}
              className={`rail-btn ${view === id || (id === "analytics" && view === "analytics") ? "active" : ""}`}
              title={label}
              onClick={() => {
                if (id === "home") nav("/");
                onView(id === "home" ? "home" : id);
              }}
            >
              <Icon name={icon} />
            </button>
          ))}
        </nav>
        <main className="stage">{children}</main>
      </div>
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function Landing({ onStarted }) {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function start() {
    const target = url.trim();
    if (!target) {
      setError("Enter a public website URL to start a report.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const scan = await createScan(target);
      onStarted?.(scan);
      nav(`/scan/${scan.scan_id}`);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  const features = [
    { emoji: "🎯", title: "Track your brand's AI visibility", detail: "See your brand visibility across AI platforms and identify key areas to improve." },
    { emoji: "📊", title: "Get insights on competitors", detail: "Identify competitor mentions and discover ways to improve your visibility." },
    { emoji: "👥", title: "Turn insights into growth", detail: "Find content opportunities to improve your visibility in AI and search." },
  ];

  return (
    <div className="hero">
      <div className="hero-kicker">AI Brand Narrative Tracking</div>
      <h1 className="hero-title">Increase Visibility Across Every Search Channel</h1>
      <p className="hero-sub">
        Understand how your brand appears in AI-generated answers. Monitor visibility, sentiment and brand mentions across top AI platforms.
      </p>
      <div className="hero-input-pill">
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Enter Your Website"
          onKeyDown={(e) => e.key === "Enter" && start()}
        />
        <button className="btn-hero-primary" onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Start Your Audit"}
        </button>
      </div>
      {error && <p className="hero-error">{error}</p>}
      <div className="hero-sub-heading">Understand where your brand stands in AI search:</div>
      <div className="feature-grid">
        {features.map((f) => (
          <div className="feature-card" key={f.title}>
            <span className="emoji">{f.emoji}</span>
            <b>{f.title}</b>
            <p>{f.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function Scanning() {
  const { scanId } = useParams();
  const nav = useNavigate();
  const [p, setP] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    let fails = 0;
    const tick = async () => {
      try {
        const data = await getProgress(scanId);
        if (!alive) return;
        fails = 0;
        setErr("");
        setP(data);
        if (data.status === "completed") nav(`/report/${scanId}`, { replace: true });
        if (data.status === "error") setErr("Scan failed. Start a new report with a full URL such as https://www.evoketechnologies.com");
      } catch (e) {
        fails += 1;
        if (alive && fails >= 3) setErr("Waiting for the server to reconnect…");
      }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(id); };
  }, [scanId, nav]);

  const pipes = p
    ? [
        ["Technical", p.technical_completed, p.technical_total],
        ["On-Page", p.onpage_completed, p.onpage_total],
        ["Off-Page", p.offpage_completed, p.offpage_total],
      ]
    : [];

  return (
    <div className="card" style={{ maxWidth: 720, margin: "40px auto" }}>
      <div className="kicker">Scan in progress</div>
      <h1 style={{ fontSize: 28 }}>{p?.domain || "Evaluating website"}</h1>
      <p style={{ color: "var(--muted)" }}>
        {p?.status === "crawling"
          ? "Fetching live pages and sitemaps. This usually takes under a minute."
          : "Scores stay hidden until every runnable check finishes."}
      </p>
      {pipes.map(([name, done, total]) => (
        <div className="pipe" key={name}>
          <div className="pipe-top"><span>{name}</span><span>{done}/{total}</span></div>
          <div className="bar"><span style={{ width: `${(done / Math.max(1, total)) * 100}%` }} /></div>
        </div>
      ))}
      <p style={{ color: "var(--muted)" }}>Overall {Math.round(p?.progress_percent || 0)}% · {p?.status || "queued"}</p>
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
    </div>
  );
}

const BAND_COLORS = { excellent: "#7c3aed", good: "#22c55e", fair: "#06b6d4", poor: "#f97316", critical: "#ef4444", unknown: "#c3cad4" };

function MiniGauge({ value, label }) {
  const b = band(value);
  const color = BAND_COLORS[b.cls];
  const cx = 64, cy = 64, r = 52, w = 11;
  const circ = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(100, value ?? 0));
  const dash = (v / 100) * circ;
  return (
    <div className="mini-gauge">
      <svg viewBox="0 0 128 128" width="128" height="128" aria-label={`${label} score ${fmt(value)} percent`}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="#eef2f5" strokeWidth={w} />
        {value != null && (
          <circle
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={w}
            strokeLinecap="round"
            strokeDasharray={`${dash} ${circ}`}
            transform={`rotate(-90 ${cx} ${cy})`}
          />
        )}
        <text x={cx} y={cy + 1} textAnchor="middle" dominantBaseline="middle" fontSize="19" fontWeight="700" fill="#111827">
          {fmt(value)}%
        </text>
      </svg>
      <div className="mini-gauge-label">{label}</div>
    </div>
  );
}

const SORT_COLUMNS = [
  { key: "name", label: "Parameter" },
  { key: "score", label: "Score" },
  { key: "issues", label: "Issues" },
  { key: "recommendation", label: "Recommended Fix" },
];

function sortValue(p, key) {
  if (key === "score") return p.score ?? -1;
  if (key === "issues") return issueText(p);
  if (key === "recommendation") return p.recommendation || "";
  return p.name || "";
}

function ParameterTable({ title, rows, onSelect, tabs, activeTab, onTabChange }) {
  const [sort, setSort] = useState({ key: null, dir: 1 });

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: -s.dir } : { key, dir: 1 }));
  }

  const sortedRows = useMemo(() => {
    if (!sort.key) return rows;
    return [...rows].sort((a, b) => {
      const av = sortValue(a, sort.key);
      const bv = sortValue(b, sort.key);
      if (typeof av === "string") return av.localeCompare(bv) * sort.dir;
      return (av - bv) * sort.dir;
    });
  }, [rows, sort]);

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="summary-head">
        <h2>{title}</h2>
        {tabs && (
          <div className="segmented">
            {tabs.map((t) => (
              <button key={t.key} className={`seg-btn ${activeTab === t.key ? "active" : ""}`} onClick={() => onTabChange(t.key)}>
                {t.name}
              </button>
            ))}
          </div>
        )}
      </div>
      <table>
        <thead>
          <tr>
            {SORT_COLUMNS.map((c) => (
              <th key={c.key} className="sortable" onClick={() => toggleSort(c.key)}>
                {c.label}
                <span className="sort-arrow">{sort.key === c.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((p) => {
            const tone = p.score == null ? "na" : p.score >= 75 ? "hi" : p.score >= 40 ? "mid" : "lo";
            return (
              <tr key={p.parameter_id} className="clickable" onClick={() => onSelect(p)}>
                <td>{p.name}</td>
                <td><span className={`points ${tone}`}>{p.score == null ? "—" : `${fmt(p.score, 0)}%`}</span></td>
                <td className="issue-cell">{issueText(p)}</td>
                <td>{p.recommendation || "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ScanHistoryCard({ scan, featured, onOpen }) {
  const [iconError, setIconError] = useState(false);
  const desc =
    scan.status === "completed"
      ? `Completed · Score ${fmt(scan.overall_score, 1)}/100`
      : scan.status === "error"
      ? "Scan failed · retry to try again"
      : "Scan in progress";

  return (
    <div className="history-card">
      <div className="history-icon">
        {!iconError ? (
          <img
            src={`https://www.google.com/s2/favicons?sz=64&domain=${scan.domain}`}
            alt=""
            onError={() => setIconError(true)}
          />
        ) : (
          <Icon name="globe" />
        )}
      </div>
      <b className="history-name">{scan.domain}</b>
      <p className="history-desc">{desc}</p>
      <button className={`btn history-btn ${featured ? "btn-primary" : "btn-ghost"}`} onClick={onOpen}>
        View Details
      </button>
    </div>
  );
}

function ScanHistoryGrid({ scans, onOpen }) {
  if (!scans.length) {
    return <div className="card">No reports yet. Create a new report to get started.</div>;
  }
  return (
    <div className="history-grid">
      {scans.map((s, i) => (
        <ScanHistoryCard key={s.scan_id} scan={s} featured={i === 0} onOpen={() => onOpen(s)} />
      ))}
    </div>
  );
}

function Dashboard() {
  const { scanId } = useParams();
  const nav = useNavigate();
  const [report, setReport] = useState(null);
  const [err, setErr] = useState("");
  const [view, setView] = useState("analytics");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(null);
  const [scans, setScans] = useState([]);
  const [toast, setToast] = useState("");
  const [activeSection, setActiveSection] = useState("on_page");

  useEffect(() => {
    getReport(scanId).then(setReport).catch((e) => setErr(e.message));
  }, [scanId]);

  useEffect(() => {
    if (view === "reports") listScans().then(setScans).catch(() => setScans([]));
  }, [view]);

  function notify(msg) {
    setToast(msg);
    setTimeout(() => setToast(""), 2200);
  }

  async function openParameter(row) {
    setSelected(row);
    try {
      const full = await getParameter(scanId, row.parameter_id);
      setSelected((prev) => (prev && prev.parameter_id === row.parameter_id ? { ...prev, ...full } : prev));
    } catch {
      /* keep the row already shown from the report */
    }
  }

  const params = report?.parameters || [];
  const cats = report?.category_scores || {};
  const overall = report?.overall_score;
  const overallBand = band(overall);

  const pillars = [
    { key: "on_page", name: "On-Page", color: "#14b8a6", score: cats.on_page, weight: WEIGHTS.on_page },
    { key: "off_page", name: "Off-Page", color: "#f59e0b", score: cats.off_page, weight: WEIGHTS.off_page },
    { key: "technical", name: "Technical", color: "#2563eb", score: cats.technical, weight: WEIGHTS.technical },
  ];

  const rowsBySection = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out = {};
    for (const key of ["on_page", "off_page", "technical"]) {
      out[key] = params
        .filter((p) => p.section === key)
        .filter((p) => !q || p.name.toLowerCase().includes(q) || p.parameter_id.toLowerCase().includes(q) || (p.recommendation || "").toLowerCase().includes(q));
    }
    return out;
  }, [params, search]);

  if (err) {
    return (
      <Shell view="home" onView={setView} search={search} onSearch={setSearch} toast={toast}>
        <div className="card"><h2>Report unavailable</h2><p>{err}</p><button className="btn btn-primary" onClick={() => nav("/")}>Create New Report</button></div>
      </Shell>
    );
  }
  if (!report) {
    return <Shell view="analytics" onView={setView} search={search} onSearch={setSearch}><div className="card">Loading report…</div></Shell>;
  }

  function exportPdf() {
    window.location.href = downloadUrl(scanId);
    notify("Preparing PDF download");
  }

  return (
    <Shell view={view} onView={(v) => { setView(v); if (v === "home") nav("/"); }} search={search} onSearch={(v) => { setSearch(v); if (v) setView("analytics"); }} toast={toast}>
      {view === "analytics" && (
        <>
          <div className="page-head">
            <div>
              <h1>{report.domain}</h1>
              <p>A snapshot of your brand’s visibility across AI platforms.</p>
            </div>
            <div className="head-actions">
              <button className="btn btn-ghost" onClick={exportPdf}><Icon name="export" /> Export to PDF</button>
              <button className="btn btn-primary" onClick={() => nav("/")}><Icon name="plus" /> Create New Report</button>
            </div>
          </div>

          <div className="card visibility-card">
            <div className="card-head">
              <h2>AI Visibility</h2>
              <div className="status-line">Status: <span className={`band ${overallBand.cls}`}>{overallBand.label}</span></div>
            </div>
            <div className="visibility-body">
              <div className="visibility-table">
                <table>
                  <thead>
                    <tr><th>Pillar</th><th>Weight</th><th>Score/100</th><th>Weighted</th><th>Band</th></tr>
                  </thead>
                  <tbody>
                    {pillars.map((p) => {
                      const b = band(p.score);
                      const weighted = p.score == null ? null : p.score * p.weight;
                      return (
                        <tr
                          key={p.key}
                          className="clickable"
                          onClick={() => {
                            setActiveSection(p.key);
                            document.getElementById("params-summary")?.scrollIntoView({ behavior: "smooth", block: "start" });
                          }}
                        >
                          <td>{p.name}</td>
                          <td>{Math.round(p.weight * 100)}%</td>
                          <td>{fmt(p.score)}</td>
                          <td>{fmt(weighted)}</td>
                          <td className={`band ${b.cls}`}>{b.label}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <div className="overall-score-line">
                  Overall Score <span className={`overall-score-value band ${overallBand.cls}`}>{fmt(overall)}</span>
                  <span className="overall-formula">On-Page × 40% + Off-Page × 25% + Technical × 35%</span>
                </div>
              </div>
              <div className="gauge-row">
                {pillars.map((p) => (
                  <MiniGauge key={p.key} value={p.score} label={p.name} />
                ))}
              </div>
            </div>
            <div className="legend-row">
              <span className="band excellent">90+ Excellent</span>
              <span className="band good">75–89 Good</span>
              <span className="band fair">60–74 Fair</span>
              <span className="band poor">40–59 Poor</span>
              <span className="band critical">Under 40 Critical</span>
            </div>
          </div>

          <div id="params-summary">
            <ParameterTable
              title="Parameters Summary"
              rows={rowsBySection[activeSection] || []}
              onSelect={openParameter}
              tabs={pillars}
              activeTab={activeSection}
              onTabChange={setActiveSection}
            />
          </div>
        </>
      )}

      {view === "reports" && (
        <div className="panel">
          <div className="page-head">
            <div><h1 style={{ fontSize: 24 }}>Search History</h1><p>Explore historical search insights and findings.</p></div>
            <button className="btn btn-primary" onClick={() => nav("/")}><Icon name="plus" /> Create New Report</button>
          </div>
          <ScanHistoryGrid
            scans={scans}
            onOpen={(s) => { if (s.status === "completed") nav(`/report/${s.scan_id}`); else nav(`/scan/${s.scan_id}`); }}
          />
        </div>
      )}

      {view === "settings" && (
        <div className="card panel">
          <h2>Settings</h2>
          <p>Overall = On-Page × 40% + Off-Page × 25% + Technical × 35%. UNKNOWN is excluded from the denominator. Thresholds: Pass 90, Partial 60, Fail below 60.</p>
          <button className="btn btn-primary" onClick={exportPdf}>Export current report</button>
        </div>
      )}

      {selected && (
        <div className="drawer-back" onClick={() => setSelected(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <div className="kicker">{selected.parameter_id}</div>
                <h2>{selected.name}</h2>
              </div>
              <button className="btn btn-ghost" onClick={() => setSelected(null)}>Close</button>
            </div>
            <p className="drawer-meta">
              <span className={`badge ${selected.status}`}>{selected.status}</span>
              <span className="drawer-score">Score <b>{selected.score != null ? `${fmt(selected.score, 0)}%` : "—"}</b></span>
              {selected.status === "UNKNOWN" && <span className="drawer-note">Excluded from the overall score</span>}
            </p>
            <div className="drawer-fact">
              <span className="drawer-fact-label">Source</span>
              {selected.checked_url_or_source ? (
                /^https?:\/\//i.test(selected.checked_url_or_source) ? (
                  <a className="ev-link" href={selected.checked_url_or_source} target="_blank" rel="noreferrer">{selected.checked_url_or_source}</a>
                ) : (
                  <span>{selected.checked_url_or_source}</span>
                )
              ) : (
                <span className="ev-empty">Not recorded</span>
              )}
            </div>
            <div className="drawer-fact">
              <span className="drawer-fact-label">Recommended fix</span>
              <span>{selected.recommendation || "No change required."}</span>
            </div>
            <EvidencePanel evidence={selected.evidence} error={selected.error} />
          </div>
        </div>
      )}
    </Shell>
  );
}

function HomePage() {
  const [view, setView] = useState("home");
  const [search, setSearch] = useState("");
  const [toast, setToast] = useState("");
  const nav = useNavigate();

  function notify(msg) {
    setToast(msg);
    setTimeout(() => setToast(""), 2200);
  }

  let body = <Landing />;
  if (view === "reports") {
    body = <ReportsHome notify={notify} />;
  } else if (view === "settings") {
    body = <div className="card panel"><h2>Settings</h2><p>Overall = On-Page × 40% + Off-Page × 25% + Technical × 35%. UNKNOWN is excluded from the score.</p></div>;
  } else if (view === "analytics") {
    body = (
      <div className="card panel">
        <h2>Analytics</h2>
        <p>Open an existing report or create a new one to see the visibility dashboard.</p>
        <button className="btn btn-primary" onClick={() => setView("reports")}>View reports</button>
      </div>
    );
  }

  return (
    <Shell view={view} onView={(v) => { if (v === "home") { setView("home"); nav("/"); } else setView(v); }} search={search} onSearch={setSearch} toast={toast}>
      {body}
    </Shell>
  );
}

function ReportsHome() {
  const [scans, setScans] = useState([]);
  const nav = useNavigate();
  useEffect(() => { listScans().then(setScans).catch(() => setScans([])); }, []);
  return (
    <div className="panel">
      <div className="page-head">
        <div><h1 style={{ fontSize: 24 }}>Search History</h1><p>Explore historical search insights and findings.</p></div>
        <button className="btn btn-primary" onClick={() => nav("/")}><Icon name="plus" /> Create New Report</button>
      </div>
      <ScanHistoryGrid
        scans={scans}
        onOpen={(s) => nav(s.status === "completed" ? `/report/${s.scan_id}` : `/scan/${s.scan_id}`)}
      />
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/scan/:scanId" element={<ScanPage />} />
      <Route path="/report/:scanId" element={<Dashboard />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function ScanPage() {
  const [search, setSearch] = useState("");
  const nav = useNavigate();
  return (
    <Shell view="analytics" onView={(v) => { if (v === "home") nav("/"); }} search={search} onSearch={setSearch}>
      <Scanning />
    </Shell>
  );
}
