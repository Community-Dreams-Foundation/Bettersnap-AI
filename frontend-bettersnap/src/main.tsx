import { PublicClientApplication, EventType, AuthenticationResult } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import { msalConfig } from "./lib/authConfig";
import App from "./App.tsx";
import "./index.css";
import { createRoot } from "react-dom/client";

export const msalInstance = new PublicClientApplication(msalConfig);

const container = document.getElementById("root")! as HTMLElement & {
  __reactRoot?: ReturnType<typeof createRoot>;
};
const root = container.__reactRoot ?? createRoot(container);
container.__reactRoot = root;

msalInstance
  .initialize()
  .then(() => {
    // Set active account from existing session (popup login populates this)
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) {
      msalInstance.setActiveAccount(accounts[0]);
    }

    msalInstance.addEventCallback((event) => {
      if (event.eventType === EventType.LOGIN_SUCCESS && event.payload) {
        const payload = event.payload as AuthenticationResult;
        msalInstance.setActiveAccount(payload.account);
      }
    });

    // Handle redirect result IF one exists, but never let it crash the app.
    // Popup flow leaves no redirect request → no_token_request_cache_error is expected, swallow it.
    msalInstance
      .handleRedirectPromise()
      .then((result) => {
        if (result?.account) {
          msalInstance.setActiveAccount(result.account);
        }
      })
      .catch((err) => {
        console.warn("handleRedirectPromise (non-fatal):", err);
      });

    root.render(
      <MsalProvider instance={msalInstance}>
        <App msalInstance={msalInstance} />
      </MsalProvider>,
    );
  })
  .catch((err) => {
    // Only a REAL initialize() failure lands here now — genuinely fatal.
    console.error("MSAL initialize() failed:", err);
    root.render(
      <div style={{ padding: 24, fontFamily: "monospace", color: "#b00020" }}>
        Authentication failed to initialize. Open DevTools → Console for the exact error.
      </div>,
    );
  });
