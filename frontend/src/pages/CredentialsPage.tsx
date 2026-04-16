import { CredentialsForm } from "../components/settings/CredentialsForm";
import { useAppStore } from "../store/appStore";

export function CredentialsPage() {
  const saveCredentials = useAppStore((state) => state.saveCredentials);
  const testCredentials = useAppStore((state) => state.testCredentials);
  const credentials = useAppStore((state) => state.credentials);
  const connectionTest = useAppStore((state) => state.connectionTest);

  return (
    <section className="page-grid">
      <div className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>API Credentials</h2>
          </div>
        </div>
        <p className="muted">
          Saved key: {credentials?.masked_api_key ?? "Not configured"}
          {credentials?.last_updated ? ` · Updated ${new Date(credentials.last_updated).toLocaleString()}` : ""}
        </p>
      </div>
      <CredentialsForm onSave={saveCredentials} onTest={testCredentials} />
      {connectionTest ? (
        <div className="card">
          <h3>Connection Test</h3>
          <p>{connectionTest.message}</p>
          {connectionTest.balance_usdt !== null && connectionTest.balance_usdt !== undefined ? (
            <p className="muted">USDT balance: {connectionTest.balance_usdt.toFixed(2)}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
