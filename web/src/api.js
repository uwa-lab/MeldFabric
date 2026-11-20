const meta = (name) =>
  document.querySelector(`meta[name="${name}"]`)?.content || "";
export const adminBase = meta("meldfabric-admin-base");
export const rootPath = meta("meldfabric-root-path");
const storageKey = `meldfabric_admin_key:${adminBase}`;
export const credentials = {
  get: () => sessionStorage.getItem(storageKey) || "",
  set: (value) =>
    value
      ? sessionStorage.setItem(storageKey, value)
      : sessionStorage.removeItem(storageKey),
};
const resolvePath = (path) =>
  path === "/admin" || path.startsWith("/admin/")
    ? adminBase + path.slice(6)
    : rootPath + path;
export function errorMessage(body) {
  const error = body?.error || body?.detail;
  return (
    error?.message ||
    (Array.isArray(error)
      ? error.map((item) => item.msg || "字段无效").join("；")
      : typeof body === "string"
        ? body
        : "操作失败，请检查输入或运行诊断。")
  );
}
export async function api(path, options = {}) {
  const { key = credentials.get(), ...request } = options;
  const url = resolvePath(path);
  const response = await fetch(url, {
    ...request,
    headers: {
      Authorization: `Bearer ${key}`,
      ...(request.body ? { "Content-Type": "application/json" } : {}),
      ...request.headers,
    },
  });
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  if (!response.ok) {
    const error = new Error(errorMessage(body));
    error.status = response.status;
    error.body = body;
    if (response.status === 401)
      window.dispatchEvent(new Event("meldfabric:unauthorized"));
    throw error;
  }
  return body;
}
export const post = (path, body = {}, options = {}) =>
  api(path, { ...options, method: "POST", body: JSON.stringify(body) });

export async function streamSSE(path, body, { signal, onEvent, onOpen } = {}) {
  const response = await fetch(resolvePath(path), {
    method: "POST",
    signal,
    headers: {
      Authorization: `Bearer ${credentials.get()}`,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    let payload = text;
    try {
      payload = JSON.parse(text);
    } catch {
      // Keep non-JSON gateway errors readable.
    }
    const error = new Error(errorMessage(payload));
    error.status = response.status;
    error.body = payload;
    if (response.status === 401)
      window.dispatchEvent(new Event("meldfabric:unauthorized"));
    throw error;
  }
  onOpen?.({
    runId: response.headers.get("X-MeldFabric-Playground-Run-ID"),
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = async (block) => {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    await onEvent?.(JSON.parse(data));
  };
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) await consume(block);
    if (done) break;
  }
  if (buffer.trim()) await consume(buffer);
}
