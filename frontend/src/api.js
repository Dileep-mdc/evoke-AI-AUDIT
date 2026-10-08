const headers = { "Content-Type": "application/json" };

async function read(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.message || `Request failed (${res.status})`);
  }
  return data;
}

export function createScan(url, options = {}) {
  const body = { url, industry: options.industry || null, region: options.region || null };
  return fetch("/api/scans", { method: "POST", headers, body: JSON.stringify(body) }).then(read);
}

export function getStatus() {
  return fetch("/api/status").then(read);
}

export function getProgress(id, signal) {
  return fetch(`/api/scans/${id}/progress`, { signal }).then(read);
}

export function getReport(id) {
  return fetch(`/api/scans/${id}/report`).then(read);
}

export function getParameter(id, parameterId) {
  return fetch(`/api/scans/${id}/parameters/${parameterId}`).then(read);
}

export function downloadUrl(id) {
  return `/api/scans/${id}/download`;
}

export function listScans() {
  return fetch("/api/scans").then(read);
}

// Re-run only a finished audit's unmeasured checks, against the pages it already read.
export function startRecheck(id) {
  return fetch(`/api/scans/${id}/recheck`, { method: "POST" }).then(read);
}

export function getRecheck(id) {
  return fetch(`/api/scans/${id}/recheck`).then(read);
}
