// Thin fetch wrappers around the FastAPI bridge in app/api.py.

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = body.detail;
    } catch {
      // keep the status-line detail
    }
    throw new Error(detail);
  }
  return response.json();
}

export function fetchCatalog() {
  return request("/api/requests");
}

export function fetchRequestDetails(requestId) {
  return request(`/api/requests/${encodeURIComponent(requestId)}`);
}

export function runAnalysis(body) {
  return request("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
