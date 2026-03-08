const LOCAL_BACKEND_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const STRIP_REQUEST_HEADERS = new Set([
  "accept-encoding",
  "authorization",
  "connection",
  "content-length",
  "host",
]);
const FORWARDED_RESPONSE_HEADERS = new Set([
  "accept-ranges",
  "cache-control",
  "content-disposition",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
  "location",
]);

function normalizeOrigin(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

export function getBackendOrigin(): string {
  return normalizeOrigin(
    process.env.FMV_BACKEND_ORIGIN ||
      process.env.NEXT_PUBLIC_API_ORIGIN ||
      "http://localhost:8000",
  );
}

function backendUsesLocalhost(origin: string): boolean {
  try {
    return LOCAL_BACKEND_HOSTS.has(new URL(origin).hostname);
  } catch {
    return origin.includes("localhost") || origin.includes("127.0.0.1");
  }
}

async function getCloudRunIdentityToken(audience: string): Promise<string> {
  const metadataUrl =
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
    + `?audience=${encodeURIComponent(audience)}&format=full`;
  const response = await fetch(metadataUrl, {
    cache: "no-store",
    headers: {
      "Metadata-Flavor": "Google",
    },
  });
  if (!response.ok) {
    throw new Error(`Failed to obtain Cloud Run identity token (${response.status})`);
  }
  return response.text();
}

async function getProxyAuthorizationHeader(backendOrigin: string): Promise<string | null> {
  if (backendUsesLocalhost(backendOrigin)) {
    return null;
  }
  const token = await getCloudRunIdentityToken(backendOrigin);
  return `Bearer ${token}`;
}

function copyProxyRequestHeaders(source: Headers, authorization: string | null): Headers {
  const headers = new Headers();
  source.forEach((value, key) => {
    if (STRIP_REQUEST_HEADERS.has(key.toLowerCase())) {
      return;
    }
    headers.set(key, value);
  });
  if (authorization) {
    headers.set("Authorization", authorization);
  }
  return headers;
}

function copyProxyResponseHeaders(source: Headers): Headers {
  const headers = new Headers();
  source.forEach((value, key) => {
    if (!FORWARDED_RESPONSE_HEADERS.has(key.toLowerCase())) {
      return;
    }
    headers.set(key, value);
  });
  return headers;
}

export async function proxyToBackend(request: Request, targetPath: string): Promise<Response> {
  const backendOrigin = getBackendOrigin();
  const incomingUrl = new URL(request.url);
  const targetUrl = new URL(targetPath, `${backendOrigin}/`);
  targetUrl.search = incomingUrl.search;

  const authorization = await getProxyAuthorizationHeader(backendOrigin);
  const headers = copyProxyRequestHeaders(request.headers, authorization);

  let body: ArrayBuffer | undefined;
  if (!["GET", "HEAD"].includes(request.method.toUpperCase())) {
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > 0) {
      body = bytes;
    }
  }

  const upstream = await fetch(targetUrl, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
    redirect: "manual",
  });

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: copyProxyResponseHeaders(upstream.headers),
  });
}
