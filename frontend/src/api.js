const headers = { "Content-Type": "application/json" };

async function read(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.message || `Request failed (${res.status})`);
  }
  return data;
}

export function createScan(url) {
  return fetch("/api/scans", { method: "POST", headers, body: JSON.stringify({ url }) }).then(read);
}

export function getProgress(id) {
  return fetch(`/api/scans/${id}/progress`).then(read);
}

export function getReport(id) {
  return fetch(`/api/scans/${id}/report`).then(read);
}

export function getParameter(id, parameterId) {
  return fetch(`/api/scans/${id}/parameters/${parameterId}`).then(read);
}

export function rerunUnscored(id) {
  return fetch(`/api/scans/${id}/rerun-unscored`, { method: "POST" }).then(read);
}

export function downloadUrl(id) {
  return `/api/scans/${id}/download`;
}

export function listScans() {
  return fetch("/api/scans").then(read);
}
