"""
Inject supplier API proxy into the running Hermes gateway.

This patches the Hermes gateway's FastAPI app at runtime to:
1. Serve the supplier dashboard at /supplier/
2. Proxy /api/briefs, /api/suppliers, etc. to the supplier API on localhost:8000
3. Exempt supplier API paths from the gateway's auth middleware

Usage:
    python3 inject_supplier_proxy.py
    # Then restart the gateway
"""
import sys, os, re

# Path to the Hermes web_server module
WEB_SERVER_PATH = "/opt/hermes-agent/hermes_cli/web_server.py"

def inject():
    with open(WEB_SERVER_PATH, "r") as f:
        src = f.read()

    # 1. Add supplier API paths to _PUBLIC_API_PATHS
    if "api/briefs" not in src:
        src = src.replace(
            '"/api/dashboard/plugins/rescan",',
            '"/api/dashboard/plugins/rescan",\n'
            '    # Supplier API (injected by inject_supplier_proxy.py)\n'
            '    "/api/briefs", "/api/suppliers", "/api/conversations",\n'
            '    "/api/health", "/api/rebuild-dashboard",\n'
        )
        # Also add path prefix matches
        old_check = 'path.startswith("/api/") and path not in _PUBLIC_API_PATHS and not path.startswith("/api/plugins/")'
        new_check = 'path.startswith("/api/") and path not in _PUBLIC_API_PATHS and not path.startswith("/api/plugins/") and not path.startswith("/api/briefs/") and not path.startswith("/api/suppliers/") and not path.startswith("/api/conversations/")'
        src = src.replace(old_check, new_check)

    # 2. Add proxy import and mount at the end (before the static file mount)
    proxy_code = '''
# ─── Supplier API Proxy (injected) ─────────────────────────────────
import httpx as _httpx

_supplier_client = _httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120.0)

async def _supplier_proxy(request: Request):
    """Reverse proxy to the supplier API on port 8000."""
    path = request.url.path
    # Strip /supplier prefix if present, otherwise forward as-is
    if path.startswith("/supplier"):
        path = path[len("/supplier"):] or "/"
    url = f"http://127.0.0.1:8000{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # Forward the request
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("authorization", None)
    headers.pop("x-hermes-session-token", None)

    resp = await _supplier_client.request(
        method=request.method,
        url=url,
        headers=headers,
        content=body if body else None,
    )

    # Build response
    resp_headers = dict(resp.headers)
    for hop in ["transfer-encoding", "connection", "content-encoding", "content-length"]:
        resp_headers.pop(hop, None)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )

# Mount supplier proxy routes — must come BEFORE the catch-all static mount
_supplier_paths = [
    "/api/briefs", "/api/suppliers", "/api/conversations",
    "/api/health", "/api/rebuild-dashboard",
]
for _sp in _supplier_paths:
    app.add_api_route(_sp, _supplier_proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    app.add_api_route(_sp + "/{path:path}", _supplier_proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])

# Serve the supplier dashboard at /supplier/
@app.get("/supplier/{path:path}")
async def _supplier_dashboard_proxy(path: str, request: Request):
    """Serve the supplier dashboard or proxy API calls."""
    if path.startswith("api/"):
        request.scope["path"] = "/" + path
        request.scope["raw_path"] = ("/" + path).encode()
        return await _supplier_proxy(request)
    # Serve static dashboard files
    dashboard_dir = "/data/luxury_towel_suppliers/deploy"
    file_path = os.path.join(dashboard_dir, path) if path else os.path.join(dashboard_dir, "index.html")
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # Fallback to index.html for SPA routing
    return FileResponse(os.path.join(dashboard_dir, "index.html"))

@app.get("/supplier")
async def _supplier_dashboard_root(request: Request):
    return FileResponse("/data/luxury_towel_suppliers/deploy/index.html")
# ─── End Supplier Proxy ───────────────────────────────────────────
'''

    if "_supplier_client" not in src:
        # Find the last route definition or the static file mount
        # Insert before the static file serving section
        static_marker = "# ─── Static files"
        if static_marker in src:
            src = src.replace(static_marker, proxy_code + "\n" + static_marker)
        else:
            # Append at end
            src += proxy_code

    with open(WEB_SERVER_PATH, "w") as f:
        f.write(src)

    print("✅ Injected supplier API proxy into Hermes gateway")
    print("   - Supplier API routes added to public paths (no auth required)")
    print("   - Proxy routes mounted: /api/briefs, /api/suppliers, /api/conversations, /api/health")
    print("   - Dashboard served at: /supplier/")
    print("   - Restart gateway with: kill -USR1 <gateway_pid>")

if __name__ == "__main__":
    inject()
