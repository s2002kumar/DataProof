import type { Step } from "@/lib/journey";

const MARKS = { good: "✓", warn: "!", bad: "✕", neutral: "•" } as const;
const TONES = { good: "fine", warn: "needs attention", bad: "failed", neutral: "for information" } as const;

/** The five review steps, in order, each with its status in words (colour only reinforces it). */
export function Journey({ steps, next }: { steps: Step[]; next: Step | null }) {
  return (
    <nav className="journey" aria-label="Review steps">
      <ol>
        {steps.map((step) => (
          <li key={step.id} className={`step ${step.tone}`}>
            <a href={`#${step.id}`} aria-current={next?.id === step.id ? "step" : undefined}>
              <span className="step-mark" aria-hidden="true">
                {MARKS[step.tone]}
              </span>
              <span className="step-title">
                {step.number}. {step.title}
              </span>
              <span className="step-status">
                {step.status}
                <span className="visually-hidden"> ({TONES[step.tone]})</span>
              </span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}
