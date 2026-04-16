import { SettingsForm } from "../components/settings/SettingsForm";
import { useAppStore } from "../store/appStore";

export function SettingsPage() {
  const settings = useAppStore((state) => state.settings);
  const updateSettings = useAppStore((state) => state.updateSettings);

  return (
    <section>
      <div className="section-head">
        <div className="section-head-copy">
          <h2>Settings</h2>
        </div>
      </div>
      <SettingsForm initialValues={settings} onSave={updateSettings} />
    </section>
  );
}
