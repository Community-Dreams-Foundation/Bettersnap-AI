import { createContext, useContext, useEffect, useRef, ReactNode } from "react";
import { useMsal, useIsAuthenticated } from "@azure/msal-react";
import { AccountInfo } from "@azure/msal-browser";
import { tokenRequest } from "@/lib/authConfig";
import { registerUser } from "@/lib/azureApi";

interface AuthContextType {
  user: AccountInfo | null;
  loading: boolean;
  signOut: () => Promise<void>;
  getAccessToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  loading: false,
  signOut: async () => {},
  getAccessToken: async () => null,
});

export const useAuth = () => useContext(AuthContext);

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const { instance, accounts, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  const user = isAuthenticated && accounts.length > 0 ? accounts[0] : null;
  const loading = inProgress !== "none";

  // ── Fire-once-per-account registration ──────────────────────────────────
  // After MSAL login completes and an account is available, ensure it is the
  // active account (so azureApi.getToken's getActiveAccount() resolves to this
  // signed-in user) and POST /users/register exactly once. This is NOT gated
  // behind a profile-exists check on purpose — the backend register handler is
  // itself the guard: it keys off the Entra `oid` and returns 200 ("already
  // exists") without inserting, or 201 on first insert. registerUser() treats
  // both as success, so calling it on every login is idempotent and safe.
  const registeredForRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!user) return;
    // MSAL must be idle before acquiring a token, otherwise acquireTokenSilent
    // (inside registerUser) can throw "interaction_in_progress".
    if (inProgress !== "none") return;

    // Make this the active account so azureApi.getToken() picks it up directly
    // rather than falling back to getAllAccounts()[0].
    if (!instance.getActiveAccount()) {
      instance.setActiveAccount(user);
    }

    // Already registered this account in this session — nothing to do.
    // homeAccountId is stable per signed-in user, so switching accounts
    // re-runs registration for the new one.
    if (registeredForRef.current === user.homeAccountId) return;
    // A call is already in flight — don't fire a duplicate on a re-render.
    if (inFlightRef.current) return;

    inFlightRef.current = true;
    const accountId = user.homeAccountId;
    registerUser()
      .then(() => {
        // ONLY a successful call marks the account as registered. 200
        // ("already exists") and 201 both resolve, so both count as success.
        registeredForRef.current = accountId;
      })
      .catch((err) => {
        // Failure (e.g. a 401 until aud/iss are confirmed against a live token)
        // does NOT mark the account registered — the guard stays unset so the
        // next login / effect run retries. Non-fatal: never blocks the app.
        console.error("registerUser failed:", err);
      })
      .finally(() => {
        inFlightRef.current = false;
      });
  }, [user, inProgress, instance]);

  const signOut = async () => {
    await instance.logoutRedirect({
      postLogoutRedirectUri: "/",
    });
  };

  const getAccessToken = async (): Promise<string | null> => {
    if (!user) return null;
    try {
      const response = await instance.acquireTokenSilent({
        ...tokenRequest,
        account: user,
      });
      return response.accessToken;
    } catch {
      await instance.acquireTokenRedirect(tokenRequest);
      return null;
    }
  };

  return <AuthContext.Provider value={{ user, loading, signOut, getAccessToken }}>{children}</AuthContext.Provider>;
};
