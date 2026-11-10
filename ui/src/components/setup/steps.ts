import type { ComponentType } from "react";
import type { SetupState, StepId } from "../../setup";
import { AutonomyStep } from "./Autonomy";
import { CodeStep } from "./Code";
import { ModelStep } from "./Model";
import { SlackStep } from "./Slack";
import { TeamStep } from "./Team";
import { TicketsStep } from "./Tickets";
import { WelcomeStep } from "./Welcome";

/** The step components by the id the worker gives each step. The onboarding shows them one at a time in the worker's
 *  order; settings shows the same ones as sections. */
export const STEPS: Record<StepId, ComponentType<{ state: SetupState }>> = {
  welcome: WelcomeStep,
  model: ModelStep,
  slack: SlackStep,
  code: CodeStep,
  team: TeamStep,
  tickets: TicketsStep,
  autonomy: AutonomyStep,
};
