// Calls to the protocol tool server (which relays run calls to the master node).

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function request(path, { method = "GET", json, body } = {}) {
  const headers = {};
  if (json !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, { method, headers, body: json !== undefined ? JSON.stringify(json) : body });
  } catch {
    throw new ApiError("Cannot reach the protocol tool server", 0);
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      message = formatDetail((await response.json()).detail) ?? message;
    } catch {
      // Not JSON; keep the status line.
    }
    throw new ApiError(message, response.status);
  }
  return response;
}

export async function api(path, options) {
  const response = await request(path, options);
  return response.status === 204 ? null : response.json();
}

function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const where = (d.loc || []).filter((p) => p !== "body").join(".");
        const msg = String(d.msg || "").replace(/^Value error, /, "");
        return where ? `${where}: ${msg}` : msg;
      })
      .join("\n");
  }
  return null;
}

// Reads a text/event-stream response body as parsed JSON events.
export async function* readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const data = chunk
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (data) yield JSON.parse(data);
    }
  }
}
