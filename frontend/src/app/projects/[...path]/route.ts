import { proxyToBackend } from "@/lib/server/backend-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{
    path: string[];
  }>;
};

async function handleProxy(request: Request, context: RouteContext): Promise<Response> {
  const { path } = await context.params;
  return proxyToBackend(request, `/projects/${path.join("/")}`);
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function HEAD(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}
