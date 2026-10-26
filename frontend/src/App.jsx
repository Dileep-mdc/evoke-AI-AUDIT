import { useEffect, useMemo, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createScan, downloadUrl, getFixes, getParameter, getProgress, getRecheck, getReport, getStatus, listScans, startRecheck, workbookUrl } from "./api";
import logoIcon from "./assets/logo-icon.png";

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

// 1823456 -> "30m 23.5s"; 4200 -> "4.2s". Matches the workbook's Scraping Time.
function duration(ms) {
  if (ms == null || Number.isNaN(ms)) return "—";
  const minutes = Math.floor(ms / 60000);
  const seconds = ((ms % 60000) / 1000).toFixed(1);
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function spanMs(from, to) {
  if (!from || !to) return null;
  return new Date(to) - new Date(from);
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
  if (p.status === "N/A") return "Not applicable";
  return p.status;
}

// "N/A" is not a usable CSS class name, so its badge is styled as "NA".
const badgeClass = (status) => (status === "N/A" ? "NA" : status);

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
  if (typeof value === "boolean") {
    const yesIsBad = /block|broken|missing|error|fail|duplicate|thin|generic|starved|chain/i.test(keyHint || "");
    return <Chip tone={value === yesIsBad ? "bad" : "good"}>{value ? "Yes" : "No"}</Chip>;
  }
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

function citationsOf(evidence) {
  const ev = parseEvidence(evidence);
  return ev && Array.isArray(ev.citations) ? ev.citations : [];
}

function shortSource(source) {
  try {
    const u = new URL(source);
    const path = u.pathname.length > 1 ? u.pathname : "";
    return u.hostname.replace(/^www\./, "") + path;
  } catch {
    return source;
  }
}

function SourceLink({ source }) {
  if (!/^https?:\/\//i.test(source)) return <span>{source}</span>;
  return (
    <a href={source} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} title={source}>
      {shortSource(source)}
    </a>
  );
}

const CITATIONS_SHOWN = 25;

function CitationList({ citations }) {
  const [all, setAll] = useState(false);
  if (!citations.length) return null;
  const shown = all ? citations : citations.slice(0, CITATIONS_SHOWN);
  return (
    <div className="citations">
      <h4>Citations ({citations.length})</h4>
      <ol>
        {shown.map((c, i) => (
          <li key={i}>
            <SourceLink source={c.source} />
            {c.detail && <span className="citation-detail"> — {c.detail}</span>}
          </li>
        ))}
      </ol>
      {citations.length > CITATIONS_SHOWN && (
        <button className="btn btn-ghost" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${citations.length}`}
        </button>
      )}
    </div>
  );
}

// Only a PASS may read as "nothing to do". A row whose score was brought back to its
// rules-based value can have lost the advice it had while the AI called it a pass.
function fixText(p) {
  if (p.recommendation) return p.recommendation;
  if (p.status === "PASS") return "No change required.";
  if (p.status === "UNKNOWN") return "Not measured, so no fix can be recommended yet.";
  if (p.status === "N/A") return "Not applicable to this site, so there is nothing to fix.";
  return "Improve this parameter against its Score Logic; see the evidence for what fell short.";
}

// A check's number within its pillar: "TECH-01" -> "1", "ON-20" -> "20". The pillar tab and
// the panel heading already say which pillar it belongs to.
function paramNumber(id) {
  return String(id || "").replace(/^[A-Za-z]+-0*/, "") || id;
}

const SECTION_NAMES = { technical: "Technical", on_page: "On-Page", off_page: "Off-Page" };

const PAGE_FIX_STEP = 50;

// Every page this check found needing a change, with the problem and the fix, from the
// server's fix-by-page list (parameters/page_fixes.py). Loaded when the panel opens: on a
// large site it is thousands of rows, which the report itself does not carry.
function PageFixes({ scanId, parameterId }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState("");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(PAGE_FIX_STEP);

  useEffect(() => {
    let alive = true;
    setData(null);
    setFailed("");
    setQuery("");
    setShown(PAGE_FIX_STEP);
    getFixes(scanId, parameterId)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setFailed(e.message));
    return () => { alive = false; };
  }, [scanId, parameterId]);

  const filtered = useMemo(() => {
    const rows = data?.fixes || [];
    const q = query.trim().toLowerCase();
    return q ? rows.filter((f) => `${f.target} ${f.issue} ${f.fix}`.toLowerCase().includes(q)) : rows;
  }, [data, query]);

  if (failed) {
    return (
      <div className="drawer-fact">
        <span className="drawer-fact-label">Fix by page</span>
        <span className="ev-empty">Could not load the page list: {failed}</span>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="drawer-fact">
        <span className="drawer-fact-label">Fix by page</span>
        <span className="ev-empty">Loading…</span>
      </div>
    );
  }
  if (data.scope === "site" || data.total === 0) {
    return (
      <div className="drawer-fact">
        <span className="drawer-fact-label">Fix by page</span>
        <span>{data.note}</span>
      </div>
    );
  }
  const more = filtered.length - shown;
  return (
    <div className="drawer-fact page-fixes">
      <span className="drawer-fact-label">
        Fix by page · {data.total.toLocaleString()} {data.total === 1 ? "page or item" : "pages or items"}
      </span>
      {data.note && <p className="page-fixes-note">{data.note}</p>}
      {data.total > 10 && (
        <input
          className="page-fixes-search"
          type="search"
          placeholder="Filter by URL or problem"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setShown(PAGE_FIX_STEP); }}
        />
      )}
      {query && <p className="page-fixes-note">{filtered.length.toLocaleString()} of {data.total.toLocaleString()} match</p>}
      <ol className="page-fixes-list">
        {filtered.slice(0, shown).map((f, i) => (
          <li key={`${f.target}-${i}`}>
            {/^https?:\/\//i.test(f.target)
              ? <a className="ev-link page-fix-target" href={f.target} target="_blank" rel="noreferrer">{f.target}</a>
              : <span className="page-fix-target">{f.target}</span>}
            <div className="page-fix-line"><span className="page-fix-tag wrong">Problem</span>{f.issue}</div>
            <div className="page-fix-line"><span className="page-fix-tag do">Fix</span>{f.fix}</div>
          </li>
        ))}
      </ol>
      <div className="page-fixes-actions">
        {more > 0 && (
          <button className="btn btn-ghost" onClick={() => setShown((s) => s + PAGE_FIX_STEP)}>
            Show {Math.min(PAGE_FIX_STEP, more).toLocaleString()} more ({more.toLocaleString()} left)
          </button>
        )}
        <a className="btn btn-ghost" href={workbookUrl(scanId)}>Download all as Excel</a>
      </div>
    </div>
  );
}

// Points are shown to 2 decimals: enough to add up, without the noise of a third digit.
const pts = (n) => fmt(n, 2);

function ScoreCalculation({ p }) {
  const ev = parseEvidence(p.evidence) || {};
  const measured = p.status !== "UNKNOWN" && p.score != null;
  const modelScore = typeof ev.model_score === "number" ? ev.model_score : null;

  const steps = [
    p.logic && { label: "What it checks", text: p.logic.checks },
    p.logic && { label: "How it is scored", text: p.logic.scoring },
    measured && p.working && { label: "Result for this site", text: p.working, result: true },
  ].filter(Boolean);
  const tone = p.status === "PASS" ? "good" : p.status === "PARTIAL" ? "mid" : "bad";

  return (
    <div className="calc">
      <h3>How this score is calculated</h3>
      {steps.length > 0 && (
        <ol className="calc-steps">
          {steps.map((s, i) => (
            <li key={s.label} className={s.result ? `calc-step result ${tone}` : "calc-step"}>
              <span className="calc-step-n">{i + 1}</span>
              <div>
                <span className="calc-step-label">{s.label}</span>
                <p>{s.text}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
      {measured && ev.no_data && (
        <p className="calc-note">
          <span className="badge FAIL">FAIL</span>{" "}
          Nothing this check looks for was found on the site{ev.no_data_reason ? ` (${ev.no_data_reason})` : ""}, so it scores 0.
        </p>
      )}
      {p.status === "N/A" && (
        <p className="calc-note">
          <span className="badge NA">N/A</span>{" "}
          This check does not apply to this site{ev.does_not_apply ? `: ${ev.does_not_apply}` : ""}. It is left out of the
          score rather than counted as 0, and its share of points is spread across the other checks in this pillar, so it
          neither helps nor hurts the overall score.
        </p>
      )}
      {!measured && p.status !== "N/A" && (
        <p className="calc-note">
          <span className={`badge ${badgeClass(p.status)}`}>{p.status}</span>{" "}
          This check could not be measured, so it is skipped rather than counted as 0. Its share of points is spread
          across the other checks in this pillar, so it neither helps nor hurts the overall score.
        </p>
      )}
      {measured && modelScore != null && Math.abs(modelScore - p.score) >= 0.1 && (
        <p className="calc-note">The AI review suggested {fmt(modelScore)}; the score follows the rule, so the AI suggestion is shown for reference only.</p>
      )}
    </div>
  );
}

function EvidencePanel({ evidence, error }) {
  const ev = parseEvidence(evidence);
  const isPlainObject = ev && typeof ev === "object" && !Array.isArray(ev);
  const errorText = error ? String(error).trim().toLowerCase() : null;
  const duplicatesError = (v) => errorText && typeof v === "string" && v.trim().toLowerCase() === errorText;
  const rawSummary = isPlainObject && typeof ev.summary === "string" ? ev.summary : null;
  const summary = rawSummary && !duplicatesError(rawSummary) ? rawSummary : null;
  const detailRows = isPlainObject
    ? Object.entries(ev).filter(([k, v]) => k !== "summary" && k !== "trace" && k !== "citations" && !duplicatesError(v))
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
      <CitationList citations={citationsOf(evidence)} />
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

function money(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `$${Number(n).toFixed(n > 0 && n < 0.01 ? 4 : 2)}`;
}

function Icon({ name }) {
  const common = { width: 18, height: 18, fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" };
  if (name === "home") return <svg {...common}><path d="M3 10.5 9 4l6 6.5" /><path d="M5 9.5V16h8V9.5" /></svg>;
  if (name === "chart") return <svg {...common}><path d="M2 10.5h3l1.8-5.2L10 15l1.8-8.2 1.4 3.7H16" /></svg>;
  if (name === "folder") return <svg {...common}><path d="M3 6h5l2 2h7v8H3z" /></svg>;
  if (name === "export") return <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M8 10V3" /><path d="m5 5 3-3 3 3" /><path d="M3 11v3h10v-3" /></svg>;
  if (name === "rerun") return <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" /><path d="M13.5 2.5v3h-3" /></svg>;
  if (name === "plus") return <svg width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 2v10M2 7h10" /></svg>;
  if (name === "globe") return <svg {...common} width="20" height="20"><circle cx="9" cy="9" r="7" /><path d="M2 9h14M9 2c2.2 2 3.5 4.5 3.5 7s-1.3 5-3.5 7c-2.2-2-3.5-4.5-3.5-7s1.3-5 3.5-7z" /></svg>;
  return null;
}

function Logo() {
  return <img src={logoIcon} alt="" width="26" height="26" style={{ display: "block", objectFit: "contain" }} />;
}

// The sidebar's pages. Each is a real URL, so Back, refresh and a copied link all keep your place.
const RAIL = [
  { path: "/", icon: "home", label: "New report", active: (p) => p === "/" },
  { path: "/analytics", icon: "chart", label: "Analytics", active: (p) => p.startsWith("/report/") || p.startsWith("/scan/") || p === "/analytics" },
  { path: "/reports", icon: "folder", label: "Reports", active: (p) => p.startsWith("/reports") },
];

function Shell({ children, onSearch, search, toast }) {
  const [open, setOpen] = useState(null);
  const [draft, setDraft] = useState("");
  const nav = useNavigate();
  const { pathname } = useLocation();
  // Pages with their own search (a report's checks, the reports list) filter as you type; on any
  // other page, Enter searches the reports.
  const value = onSearch ? search : draft;

  return (
    <div className="app">
      <header className="topnav">
        <button className="logo-btn" title="New report" aria-label="New report" onClick={() => nav("/")}><Logo /></button>
        <div className="search-wrap">
          <input
            value={value}
            onChange={(e) => (onSearch ? onSearch(e.target.value) : setDraft(e.target.value))}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !onSearch && draft.trim()) nav(`/reports?q=${encodeURIComponent(draft.trim())}`);
            }}
            placeholder={pathname.startsWith("/report/") ? "Search this report's checks" : "Search reports"}
            aria-label="Search"
          />
        </div>
        <div className="top-links">
          <button className="avatar" onClick={() => setOpen(open === "profile" ? null : "profile")} title="Account">A</button>
        </div>
        {open === "profile" && (
          <div className="dropdown" onMouseLeave={() => setOpen(null)}>
            <button onClick={() => { setOpen(null); nav("/settings"); }}>Settings</button>
          </div>
        )}
      </header>
      <div className="workspace">
        <nav className="rail" aria-label="Main">
          {RAIL.map((item) => {
            const active = item.active(pathname);
            return (
              <button
                key={item.path}
                className={`rail-btn ${active ? "active" : ""}`}
                title={item.label}
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                onClick={() => nav(item.path)}
              >
                <Icon name={item.icon} />
                <span className="rail-label">{item.label}</span>
              </button>
            );
          })}
        </nav>
        <main className="stage">{children}</main>
      </div>
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function Landing() {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [industry, setIndustry] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState(null);

  useEffect(() => {
    getStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  async function start() {
    const target = url.trim();
    if (!target) {
      setError("Enter a website URL, or the path of a saved site folder, to start a report.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const scan = await createScan(target, { industry });
      nav(`/scan/${scan.scan_id}`);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

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
          placeholder="Enter your website or a saved site folder path"
          onKeyDown={(e) => e.key === "Enter" && start()}
        />
        <button className="btn-hero-primary" onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Start Your Audit"}
        </button>
      </div>
      <div className="hero-options">
        <label htmlFor="industry">Industry</label>
        <select id="industry" value={industry} onChange={(e) => setIndustry(e.target.value)} disabled={busy}>
          <option value="">Detect automatically</option>
          {(status?.platforms?.industries || []).map((i) => (
            <option key={i.key} value={i.key}>{i.label}</option>
          ))}
        </select>
      </div>
      <p className="hero-option-hint">
        {industry
          ? "Checks, review sites and content expectations follow this industry."
          : "The industry is read from the site's own pages; pick one if it should be audited as something else."}
      </p>
      {error && <p className="hero-error">{error}</p>}
    </div>
  );
}

// Words for the scan's phase. "queued" is the brief moment before the crawl starts.
const PHASES = { queued: "Starting", crawling: "Reading the website", evaluating: "Scoring the checks" };
const POLL_MS = 1500;
const POLL_TIMEOUT_MS = 30000;
// A gap this long since the last update means the server is busy, not gone.
const BUSY_AFTER_MS = 6000;

function savedProgress(scanId) {
  try {
    return JSON.parse(sessionStorage.getItem(`scan-progress-${scanId}`) || "null");
  } catch {
    return null;
  }
}

function Scanning() {
  const { scanId } = useParams();
  const nav = useNavigate();
  // Start from the last progress this tab saw, so a reload never drops back to an empty card.
  const [p, setP] = useState(() => savedProgress(scanId));
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    let fails = 0;
    let timer = null;
    let lastUpdate = Date.now();
    const busyCheck = setInterval(() => alive && setBusy(Date.now() - lastUpdate > BUSY_AFTER_MS), 1000);

    // One request at a time: the next poll is scheduled only after this one settles, so a slow
    // response cannot pile further requests up behind it.
    const tick = async () => {
      const ctrl = new AbortController();
      const cut = setTimeout(() => ctrl.abort(), POLL_TIMEOUT_MS);
      try {
        const data = await getProgress(scanId, ctrl.signal);
        if (!alive) return;
        fails = 0;
        lastUpdate = Date.now();
        setBusy(false);
        setErr("");
        setP(data);
        try { sessionStorage.setItem(`scan-progress-${scanId}`, JSON.stringify(data)); } catch { /* storage unavailable */ }
        if (data.status === "completed") {
          nav(`/report/${scanId}`, { replace: true });
          return;
        }
        if (data.status === "error") {
          setErr("Scan failed. Start a new report with a full URL such as https://www.example.com");
          return;
        }
      } catch (e) {
        if (!alive) return;
        // A request cut off by the timeout is the server being busy; a refused one is it being down.
        if (e.name !== "AbortError") fails += 1;
        if (fails >= 3) setErr("Waiting for the server to reconnect…");
      } finally {
        clearTimeout(cut);
      }
      if (alive) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => { alive = false; clearTimeout(timer); clearInterval(busyCheck); };
  }, [scanId, nav]);

  const pipes = [
    ["Technical", p?.technical_completed, p?.technical_total],
    ["On-Page", p?.onpage_completed, p?.onpage_total],
    ["Off-Page", p?.offpage_completed, p?.offpage_total],
  ];

  return (
    <div className="card" style={{ maxWidth: 720, margin: "40px auto" }}>
      <div className="kicker">Scan in progress</div>
      <h1 style={{ fontSize: 28 }}>{p?.domain || "Connecting to your audit…"}</h1>
      <p style={{ color: "var(--muted)" }}>
        {!p
          ? "Loading the audit's progress."
          : p.status === "crawling"
          ? "Reading every page of the website. This usually takes a few minutes."
          : "Scores stay hidden until every runnable check finishes."}
      </p>
      {pipes.map(([name, done, total]) => (
        <div className="pipe" key={name}>
          <div className="pipe-top"><span>{name}</span><span>{p ? `${done || 0}/${total || 0}` : "—"}</span></div>
          <div className="bar"><span style={{ width: `${p ? ((done || 0) / Math.max(1, total || 0)) * 100 : 0}%` }} /></div>
        </div>
      ))}
      {p && (
        <p style={{ color: "var(--muted)" }}>Overall {Math.round(p.progress_percent || 0)}% · {PHASES[p.status] || p.status}</p>
      )}
      {p?.crawl_duration_ms != null && (
        <p style={{ color: "var(--muted)" }}>Scraping finished in {duration(p.crawl_duration_ms)}</p>
      )}
      {busy && !err && (
        <p className="scan-busy">Working through the heavier checks. Progress will update in a moment.</p>
      )}
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
  { key: "parameter_id", label: "#" },
  { key: "name", label: "Parameter" },
  { key: "score", label: "Score" },
  { key: "issues", label: "Issues" },
  { key: "recommendation", label: "Recommended Fix" },
  { key: "citation", label: "Citation" },
];

function sortValue(p, key) {
  if (key === "score") return p.score ?? -1;
  if (key === "issues") return issueText(p);
  if (key === "recommendation") return p.recommendation || "";
  if (key === "citation") return citationsOf(p.evidence).length;
  if (key === "parameter_id") return p.parameter_id || "";
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
      // numeric: true so ids sort by their number, not as text -- a sub-numbered id such as
      // "ON-5.1" (before the renumbering) would otherwise land after "ON-20".
      if (typeof av === "string") return av.localeCompare(bv, undefined, { numeric: true }) * sort.dir;
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
      <table className="param-table">
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
                <td className="param-id-cell"><span className="param-id">{paramNumber(p.parameter_id)}</span></td>
                <td>{p.name}</td>
                <td><span className={`points ${tone}`}>{p.score == null ? "—" : `${fmt(p.score, 0)}%`}</span></td>
                <td className="issue-cell">{issueText(p)}</td>
                <td>{p.status === "PASS" && !p.recommendation ? "—" : fixText(p)}</td>
                <td className="citation-cell">
                  {(() => {
                    const cites = citationsOf(p.evidence);
                    if (!cites.length) return "—";
                    return (
                      <>
                        <SourceLink source={cites[0].source} />
                        {cites.length > 1 && <span className="citation-more"> +{cites.length - 1} more</span>}
                      </>
                    );
                  })()}
                </td>
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
      ? "Scan stopped before finishing · run it again"
      : `In progress · ${Math.round(scan.progress_percent || 0)}%`;

  return (
    <div className="history-card">
      <div className="history-icon">
        {!iconError && __FAVICON_SERVICE__ ? (
          <img
            src={`${__FAVICON_SERVICE__}${encodeURIComponent(scan.domain)}`}
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
        {scan.status === "completed" ? "View Report" : scan.status === "error" ? "View Details" : "View Progress"}
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
  const [freshBusy, setFreshBusy] = useState(false);
  const [report, setReport] = useState(null);
  const [err, setErr] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(null);
  const [toast, setToast] = useState("");
  const [activeSection, setActiveSection] = useState("technical");
  // A re-check of this report's unmeasured checks: {state, done, total, message} from the server.
  const [recheck, setRecheck] = useState(null);

  useEffect(() => {
    // Dashboard stays mounted when navigating from one report straight to another, so the
    // previous report's state is reset here rather than relying on a fresh mount.
    setReport(null);
    setErr("");
    setSelected(null);
    getReport(scanId).then(setReport).catch((e) => setErr(e.message));
  }, [scanId]);


  // Pick up a re-check already running for this report (started before a page reload).
  useEffect(() => {
    setRecheck(null);
    getRecheck(scanId).then((r) => r.state === "running" && setRecheck(r)).catch(() => {});
  }, [scanId]);

  // While a re-check runs, follow it; when it ends, show the rebuilt report.
  useEffect(() => {
    if (recheck?.state !== "running") return undefined;
    let alive = true;
    const timer = setInterval(async () => {
      try {
        const r = await getRecheck(scanId);
        if (!alive) return;
        setRecheck(r);
        if (r.state !== "running") {
          if (r.state === "done") setReport(await getReport(scanId));
          notify(r.state === "done" ? `Re-check finished: ${r.message}` : r.message, 6000);
          setRecheck(null);
        }
      } catch {
        /* a missed poll is retried on the next tick */
      }
    }, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [recheck?.state, scanId]);

  function notify(msg, ms = 2200) {
    setToast(msg);
    setTimeout(() => setToast(""), ms);
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

  // Weights and per-pillar contributions both come from the report. The backend owns every
  // calculation; this file only formats what it is given.
  // The weights the overall score actually used: the stated 35 / 40 / 25 unless a whole pillar
  // could not be measured, in which case the others are rescaled and the dropped one is 0.
  const weights = report?.effective_weights || report?.weights || {};
  const contributions = report?.category_contributions || {};
  const pillars = [
    { key: "technical", name: "Technical", color: "#2563eb" },
    { key: "on_page", name: "On-Page", color: "#14b8a6" },
    { key: "off_page", name: "Off-Page", color: "#f59e0b" },
  ].map((p) => ({
    ...p,
    score: cats[p.key],
    weight: weights[p.key],
    contribution: contributions[p.key],
    measurable: (report?.category_points_measurable || {})[p.key],
  }));

  const rowsBySection = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out = {};
    for (const key of ["technical", "on_page", "off_page"]) {
      out[key] = params
        .filter((p) => p.section === key)
        .filter((p) => !q || p.name.toLowerCase().includes(q) || p.parameter_id.toLowerCase().includes(q) || (p.recommendation || "").toLowerCase().includes(q));
    }
    return out;
  }, [params, search]);


  if (err) {
    return (
      <Shell search={search} onSearch={setSearch} toast={toast}>
        <div className="card"><h2>Report unavailable</h2><p>{err}</p><button className="btn btn-primary" onClick={() => nav("/")}>Create New Report</button></div>
      </Shell>
    );
  }
  if (!report) {
    return <Shell search={search} onSearch={setSearch}><div className="card">Loading report…</div></Shell>;
  }

  // A new audit of the same site, with the same industry profile and region as this report.
  async function runFreshAudit() {
    setFreshBusy(true);
    try {
      const scan = await createScan(report.input_url, {
        industry: report.industry && !report.industry.detected ? report.industry.key : null,
        region: report.platforms?.region || null,
      });
      nav(`/scan/${scan.scan_id}`);
    } catch (e) {
      setFreshBusy(false);
      notify(`Could not start a fresh audit: ${e.message}`, 5000);
    }
  }

  function exportPdf() {
    window.location.href = downloadUrl(scanId);
    notify("Preparing PDF download");
  }

  // Only the checks that could not be measured are run again, against the pages this audit
  // already read; every other result stays as it is and the report is rebuilt in place.
  const unmeasured = params.filter((p) => p.status === "UNKNOWN");
  // Whether the site let the audit read it (crawler/discover.py site_access). Empty on reports
  // saved before this was recorded, which then show exactly what they always did.
  const access = report.site_access || {};
  async function startRecheckRun() {
    setRecheck({ state: "running", done: 0, total: unmeasured.length, message: "Starting" });
    try {
      setRecheck(await startRecheck(scanId));
    } catch (e) {
      setRecheck(null);
      notify(`Could not start the re-check: ${e.message}`, 5000);
    }
  }
  const rechecking = recheck?.state === "running";
  const recheckLabel = !rechecking
    ? `Re-check unmeasured (${unmeasured.length})`
    : recheck.message?.startsWith("Re-checking")
      ? `Re-checking ${recheck.done}/${recheck.total}…`
      : `${recheck.message || "Starting"}…`;

  return (
    <Shell search={search} onSearch={setSearch} toast={toast}>
        <>
          <div className="page-head">
            <div>
              <h1>{report.domain}</h1>
              {report.industry?.label && (
                <p>
                  Audited as <b>{report.industry.label}</b>
                  {report.industry.detected ? " (detected from the site)" : " (chosen for this audit)"}
                </p>
              )}
              {report.rechecked && <p>Re-checked</p>}
              {access.blocked && <p className="access-note blocked">{access.reason}</p>}
              {!access.blocked && access.pages_refused > 0 && (
                <p className="access-note partial">
                  {access.pages_refused.toLocaleString()} of {access.pages_fetched.toLocaleString()} pages were refused by
                  the site (HTTP 401/403/429) or served a bot-check page, so they are left out of the scores.
                </p>
              )}
            </div>
            <div className="head-actions">
              <button className="btn btn-ghost" onClick={runFreshAudit} disabled={freshBusy}
                      title="Read the website again and score it from scratch">
                <Icon name="rerun" /> {freshBusy ? "Starting…" : "Run fresh audit"}
              </button>
              {(rechecking || unmeasured.length > 0) && (
                <button
                  className="btn btn-ghost"
                  onClick={startRecheckRun}
                  disabled={rechecking}
                  title={`Runs only ${unmeasured.map((p) => p.parameter_id).join(", ")} again, using the pages this audit already read`}
                >
                  <Icon name="rerun" /> {recheckLabel}
                </button>
              )}
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
                    <tr><th>Pillar</th><th>Weight</th><th>Score/100</th><th>Points earned</th><th>Band</th></tr>
                  </thead>
                  <tbody>
                    {pillars.map((p) => {
                      const b = band(p.score);
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
                          <td>{p.weight == null ? "—" : `${Math.round(p.weight * 100)}%`}</td>
                          <td>{fmt(p.score)}</td>
                          <td>{p.measurable != null ? `${fmt(p.contribution, 2)} / ${fmt(p.measurable, 2)}` : fmt(p.contribution, 2)}</td>
                          <td className={`band ${b.cls}`}>{b.label}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <div className="overall-score-line">
                  Overall Score <span className={`overall-score-value band ${overallBand.cls}`}>{fmt(overall)}</span>
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

      {selected && (
        <div className="drawer-back" onClick={() => setSelected(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <div className="kicker">{SECTION_NAMES[selected.section] || ""} · {paramNumber(selected.parameter_id)}</div>
                <h2>{selected.name}</h2>
                {(selected.definition || params.find((p) => p.parameter_id === selected.parameter_id)?.definition) && (
                  <p className="drawer-definition">
                    {selected.definition || params.find((p) => p.parameter_id === selected.parameter_id)?.definition}
                  </p>
                )}
              </div>
              <button className="btn btn-ghost" onClick={() => setSelected(null)} aria-label="Close">Close</button>
            </div>
            <p className="drawer-meta">
              <span className={`badge ${badgeClass(selected.status)}`}>{selected.status}</span>
              <span className="drawer-score">Score <b>{selected.score != null ? `${fmt(selected.score)} / 100` : "—"}</b></span>
              {(selected.status === "UNKNOWN" || selected.status === "N/A") && <span className="drawer-note">Excluded from the overall score</span>}
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
              <span>{(() => {
                const note = parseEvidence(selected.evidence)?.cross_check;
                const text = fixText(selected);
                return note && text.startsWith(note) ? text.slice(note.length).trim() || text : text;
              })()}</span>
            </div>
            <PageFixes scanId={scanId} parameterId={selected.parameter_id} />
            {parseEvidence(selected.evidence)?.cross_check && (
              <div className="drawer-fact">
                <span className="drawer-fact-label">Read together with other checks</span>
                <span>{parseEvidence(selected.evidence).cross_check}</span>
              </div>
            )}
            <ScoreCalculation p={selected} />
            <EvidencePanel evidence={selected.evidence} error={selected.error} />
          </div>
        </div>
      )}
    </Shell>
  );
}

function HomePage() {
  return <Shell><Landing /></Shell>;
}

function ReportsPage() {
  const [params, setParams] = useSearchParams();
  const [scans, setScans] = useState(null);
  const nav = useNavigate();
  const q = params.get("q") || "";
  useEffect(() => {
    let alive = true;
    const load = () => listScans().then((d) => alive && setScans(d)).catch(() => alive && setScans((prev) => prev || []));
    load();
    // Audits in progress move; the list follows them without a manual refresh.
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  // Audits that stopped before finishing have no report to open, so they are not listed.
  const shown = (scans || [])
    .filter((s) => s.status !== "error")
    .filter((s) => !q || s.domain.toLowerCase().includes(q.toLowerCase()));
  return (
    <Shell search={q} onSearch={(v) => setParams(v ? { q: v } : {}, { replace: true })}>
      <div className="panel">
        <div className="page-head">
          <div><h1 style={{ fontSize: 24 }}>Reports</h1><p>Every audit, newest first. Open one to see its report or progress.</p></div>
          <button className="btn btn-primary" onClick={() => nav("/")}><Icon name="plus" /> Create New Report</button>
        </div>
        {scans === null ? (
          <div className="card">Loading reports…</div>
        ) : q && !shown.length ? (
          <div className="card">No reports match “{q}”.</div>
        ) : (
          <ScanHistoryGrid
            scans={shown}
            onOpen={(s) => nav(s.status === "completed" ? `/report/${s.scan_id}` : `/scan/${s.scan_id}`)}
          />
        )}
      </div>
    </Shell>
  );
}

// Analytics is the latest finished report: the page you most often want after an audit.
function AnalyticsPage() {
  const [target, setTarget] = useState(undefined);
  const nav = useNavigate();
  useEffect(() => {
    listScans()
      .then((d) => setTarget(d.find((s) => s.status === "completed")?.scan_id || null))
      .catch(() => setTarget(null));
  }, []);
  if (target) return <Navigate to={`/report/${target}`} replace />;
  return (
    <Shell>
      {target === undefined ? (
        <div className="card">Opening your latest report…</div>
      ) : (
        <div className="card panel">
          <h2>No finished reports yet</h2>
          <p>Run an audit and its report will open here when it finishes.</p>
          <button className="btn btn-primary" onClick={() => nav("/")}><Icon name="plus" /> Create New Report</button>
        </div>
      )}
    </Shell>
  );
}

function SettingsPage() {
  return (
    <Shell>
      <div className="card panel"><h2>Settings</h2><p>Account and report preferences will appear here.</p></div>
    </Shell>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/reports" element={<ReportsPage />} />
      <Route path="/analytics" element={<AnalyticsPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/scan/:scanId" element={<ScanPage />} />
      <Route path="/report/:scanId" element={<Dashboard />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function ScanPage() {
  return (
    <Shell>
      <Scanning />
    </Shell>
  );
}
