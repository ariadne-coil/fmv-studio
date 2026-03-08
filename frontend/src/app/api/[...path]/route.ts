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
  return proxyToBackend(request, `/api/${path.join("/")}`);
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function PUT(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function DELETE(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function PATCH(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}

export async function HEAD(request: Request, context: RouteContext): Promise<Response> {
  return handleProxy(request, context);
}
