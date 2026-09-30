import { useEffect, useMemo, useState } from "react";
import { AdminApi } from "./api";
import { Playground } from "./components/Playground";
import { DemoApi } from "./demo";
import { KeyPage } from "./components/KeyPage";
import { KeysPage } from "./components/KeysPage";
import { Login } from "./components/Login";
import { DEFAULT_API_URL, loadSession, saveSession, type Session } from "./session";

// Hash routes, so the dashboard works as static files on any host: #/ and #/keys/<id>.
function useRoute(): string {
  const [route, setRoute] = useState(window.location.hash.slice(1) || "/");
  useEffect(() => {
    const onChange = () => setRoute(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function App() {
  const [session, setSession] = useState<Session | null>(loadSession);
  const route = useRoute();
  const api = useMemo(() => (session ? new AdminApi(session.baseUrl, session.adminKey) : null), [session]);

  function signIn(next: Session | null) {
    saveSession(next);
    setSession(next);
  }

  // The playground is public: no sign-in, and it only ever uses the demo routes.
  if (route === "/playground") {
    return (
      <main>
        <Playground api={new DemoApi(DEFAULT_API_URL)} />
      </main>
    );
  }
  if (!api) return <Login onSignIn={signIn} />;
  const keyId = /^\/keys\/([\w-]+)$/.exec(route)?.[1];

  return (
    <>
      <header>
        <a href="#/" className="brand">
          Tollgate
        </a>
        <span className="muted">{api.baseUrl}</span>
        <a href="#/playground">Playground</a>
        <button type="button" className="link" onClick={() => signIn(null)}>
          Sign out
        </button>
      </header>
      <main>{keyId ? <KeyPage key={keyId} api={api} keyId={keyId} /> : <KeysPage api={api} />}</main>
    </>
  );
}
