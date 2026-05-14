// Cloudflare Pages Function — proxy /api/* to Railway backend
// The supplier API runs at hermes-agent-production-2356.up.railway.app:8000 internally
// But only port 8642 is public. We proxy through the Hermes gateway's own API.

export async function onRequest(context) {
  const url = new URL(context.request.url);
  const apiPath = url.pathname + url.search;
  
  // Try the internal Railway service first (port 8000 is internal only)
  // Then fall back to the public Hermes gateway (port 8642)
  const backends = [
    `http://localhost:8000${apiPath}`,
    `http://hermes-agent.railway.internal:8000${apiPath}`,
  ];
  
  for (const backendUrl of backends) {
    try {
      const resp = await fetch(backendUrl, {
        method: context.request.method,
        headers: {
          'Content-Type': context.request.headers.get('Content-Type') || 'application/json',
        },
        body: context.request.method !== 'GET' && context.request.method !== 'HEAD' 
          ? await context.request.text() 
          : undefined,
      });
      
      // Return the response from the backend
      const respBody = await resp.text();
      return new Response(respBody, {
        status: resp.status,
        headers: {
          'Content-Type': resp.headers.get('Content-Type') || 'application/json',
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, PATCH, PUT, DELETE, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
        },
      });
    } catch (e) {
      continue; // try next backend
    }
  }
  
  // All backends failed
  return new Response(JSON.stringify({ 
    error: 'Backend API unavailable',
    message: 'Start the supplier API server to enable discovery, enrichment, and outreach.',
    command: 'python3 -m uvicorn api:app --host 0.0.0.0 --port 8000'
  }), {
    status: 503,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    },
  });
}

// Handle CORS preflight
export async function onRequestOptions(context) {
  return new Response(null, {
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, PATCH, PUT, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
    },
  });
}
