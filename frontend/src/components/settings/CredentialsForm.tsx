import { useState } from "react";

import { useAppStore } from "../../store/appStore";

type Props = {
  onSave: (payload: { api_key: string; public_key_pem: string; private_key_pem: string }) => Promise<void>;
  onTest: () => Promise<void>;
};

export function CredentialsForm({ onSave, onTest }: Props) {
  const [apiKey, setApiKey] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const isSaving = useAppStore((state) => Boolean(state.pendingActions["credentials:save"]));
  const isTesting = useAppStore((state) => Boolean(state.pendingActions["credentials:test"]));

  return (
    <form
      className="card credentials-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (isSaving) {
          return;
        }
        void onSave({ api_key: apiKey, public_key_pem: publicKey, private_key_pem: privateKey });
      }}
    >
      <label className="field">
        <span>API Key</span>
        <input value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
      </label>
      <label className="field">
        <span>Ed25519 Public Key</span>
        <textarea value={publicKey} onChange={(event) => setPublicKey(event.target.value)} rows={6} />
      </label>
      <label className="field">
        <span>Ed25519 Private Key</span>
        <textarea value={privateKey} onChange={(event) => setPrivateKey(event.target.value)} rows={8} />
      </label>
      <div className="actions">
        <button className="primary-button" type="submit" disabled={isSaving}>
          {isSaving ? "Saving..." : "Save Credentials"}
        </button>
        <button className="secondary-button" type="button" onClick={() => void onTest()} disabled={isTesting}>
          {isTesting ? "Testing..." : "Test Connection"}
        </button>
      </div>
    </form>
  );
}
