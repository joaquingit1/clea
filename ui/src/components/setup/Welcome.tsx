import { useState } from "react";
import { getLang, setLang, t, useLang, type Lang } from "../../i18n";
import { api, errorText, refreshSetup, type SetupState } from "../../setup";
import { AppIcon } from "../ui";
import { ErrorText, Field, Segmented, TextInput } from "./fields";
import { Invalid, StepFrame, useFrame } from "./frame";

// each language under its own name, so it can be found by someone who does not read the other
const LANGS: { value: Lang; label: string }[] = [
  { value: "en", label: "English" },
  { value: "es", label: "Español" },
];

/** Who the user is and the language Clea speaks. The switch changes the whole app as it is pressed; once there is a
 *  name to save it with, it is saved as it is pressed too, so the choice never depends on a second button. */
export function WelcomeStep({ state }: { state: SetupState }) {
  const f = useFrame();
  const lang = useLang();
  const [name, setName] = useState(state.user.name);
  const [missing, setMissing] = useState(false);
  const [langError, setLangError] = useState<string | null>(null);

  const save = async () => {
    const n = name.trim();
    if (!n) {
      setMissing(true);
      throw new Invalid();
    }
    await api.profile(n, getLang());
    await refreshSetup();
  };

  const pick = async (next: Lang) => {
    const before = getLang();
    if (next === before) return;
    setLang(next);
    setLangError(null);
    const n = state.user.name || name.trim();
    if (!n) return; // first run, no name yet: "Get started" saves both
    try {
      await api.profile(n, next);
      await refreshSetup();
    } catch (e) {
      setLang(before);
      setLangError(errorText(e));
    }
  };

  const onboarding = f.mode === "onboarding";
  return (
    <StepFrame
      title={t("welcome.title")}
      lead={onboarding ? t("welcome.lead") : null}
      hero={onboarding ? <AppIcon size={64} intro /> : undefined}
      save={save}
      dirty={name.trim() !== state.user.name}
      submit={onboarding ? t("welcome.start") : undefined}
    >
      <Field label={t("welcome.name")} error={missing ? t("welcome.nameRequired") : null} hint={t("welcome.nameHint")}>
        {(p) => (
          <TextInput
            {...p}
            value={name}
            autoComplete="name"
            autoCapitalize="words"
            placeholder={t("welcome.namePlaceholder")}
            onChange={(e) => {
              setName(e.target.value);
              setMissing(false);
            }}
          />
        )}
      </Field>
      <div>
        <p className="mb-1.5 text-[13px] font-medium text-fg">{t("welcome.language")}</p>
        <Segmented name="lang" label={t("welcome.language")} value={lang} onChange={pick} options={LANGS} fill={onboarding} />
        {langError ? <ErrorText>{langError}</ErrorText> : null}
      </div>
      {onboarding ? <p className="text-center text-[12.5px] leading-snug text-pretty text-fg-3">{t("welcome.time")}</p> : null}
    </StepFrame>
  );
}
