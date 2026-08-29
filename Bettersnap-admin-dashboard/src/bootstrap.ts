async function bootstrap() {
  const query = new URLSearchParams(window.location.search)
  const hash = new URLSearchParams(window.location.hash.slice(1))
  const hasAuthResponse = [query,hash].some(params =>
    params.has('state') && (params.has('code') || params.has('error')),
  )

  if (hasAuthResponse) {
    const { broadcastResponseToMainFrame } = await import('@azure/msal-browser/redirect-bridge')
    await broadcastResponseToMainFrame()
    return
  }

  await import('./main')
}

void bootstrap()
