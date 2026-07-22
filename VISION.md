# freeflo — Vision

> **North star:** Let a person talk to their machine the way they talk to a person —
> and get things done faster. Voice-to-text is chapter one, not the whole book.

This document is the "why" behind freeflo. It explains what we are trying to build,
the beliefs that shape every decision, and the arc from where we are today to where we
are going. A visual version of this same story lives at [`design/vision.html`](design/vision.html).

---

## The problem

Talking to another person is the highest-bandwidth, lowest-friction interface humans have.
Talking to our machines is still the opposite: menus, windows, shortcuts, copy-paste,
context switches. Every day we translate what we *mean* into what the machine *accepts*,
and that translation is where time and flow are lost.

We want to erase that translation layer. **You should be able to say what you want, and
your machine should do it** — no window to open, no app to switch to, no cloud round-trip.

## The vision

freeflo is the ambient layer between a person and the devices they own — starting on the
Mac, expanding to every device a person holds. It begins with dictation because that is the
most universal, immediate win: hold a key, speak, and your words are typed anywhere. But the
mission is bigger than transcription:

**How do we make *every* interaction with your system better than it was before?**

The arc runs across domains, each one added the same way — by letting you speak instead of
click:

- **Say it → typed** (today) — dictation into any app, on-device.
- **Manage your time by speaking** — capture, schedule, and reshape your day out loud.
- **Manage your work by speaking** — tasks, notes, and context handled by voice.
- **Drive your machine by speaking** — control the laptop itself, not just fill text fields.
- **Work better with LLMs** — talk to models with the same privacy guarantees as everything else.
- **…and beyond your Mac** — the same interface across the other devices you own.

Voice is the first modality, not the last. The through-line is: *communicate with technology
the way you communicate with people, and make your working life easier.*

## What we believe (principles)

These are non-negotiable. They constrain the roadmap, the architecture, and the business.

1. **On-device first.** Everything that can run locally, runs locally. This buys two things
   at once: **low latency** (no network round-trip) and **data ownership** (see below).

2. **You own your data — it never leaves your machine.** What you say and what you dictate is
   yours. Transcribed text and audio *never* leave the device. This is enforced by
   architecture (the model runs on your Mac), not by a policy promise. Telemetry, when you
   allow it, is metadata and profile only, consented, and switchable off anytime.

3. **Free, or at most a small one-time price.** No subscriptions, no word limits, no rent on
   your own voice. Because the compute lives on your device, there is no server bill to pass
   on to you. If we ever charge, it is a minimal one-time price for a capability — never a
   meter on how much you use your own machine.

4. **Built for people who live in their systems — especially in tech.** Our users are
   professionals, and many are technical. We build a serious tool for people who work all day
   on their machines, not a novelty. The bar is "indispensable," not "fun to try once."

5. **A product that evolves with its users.** freeflo is not a one-feature company that ships
   a gimmick and disappears. It is a product that grows with the people who use it. The
   in-app **feature request** channel exists precisely so we hear what our users need and
   evolve the product *with* them — proof that we are here for the long arc, not the demo.

6. **Privacy is the design language, too.** Neutral, precise, futuristic, permanent-feeling.
   The way freeflo looks signals what it is: a serious, private, on-device tool you can trust —
   not a disposable app.

## Business & pricing philosophy

- On-device compute means near-zero marginal cost per user, which is *what makes* "free /
  minimal one-time price" sustainable — it's a consequence of the architecture, not a
  loss-leader promise.
- No backend today. Everything runs on-device plus managed free services (updates via GitHub
  Releases; consented analytics/crash reporting via managed tiers). A backend gets added only
  on a concrete trigger — owning our data end-to-end, server-side logic (licensing, accounts),
  or hosted LLM features — never by default.
- The moat is trust plus depth across domains, compounding as we listen and expand.

## Design language & why

The brand is deliberately neutral and monochrome: a living waveform chip + a chromed,
uppercase wordmark (lockup **A+1**), hairline structure, HUD/terminal precision cues. It reads
as *powerful, futuristic, and permanent*. We chose this because the product is a trust product:
it handles your voice and your data on your own machine. The visual language has to say
"serious tool that will endure," not "fun feature that will vanish." The full brand system
lives in [`design/brand.html`](design/brand.html); the app and onboarding prototypes in
[`design/onboarding.html`](design/onboarding.html) and [`design/app-window.html`](design/app-window.html).

## How we listen

The **feature request** surface inside the app is a first-class part of the product, not an
afterthought. It is how we understand what our users want and how we earn the right to evolve
the product alongside them. Every request is a signal about which domain to open next.

## Guardrail (never crossed)

> Transcribed text and audio **never** leave the device. Telemetry is metadata + profile only,
> and always consented. If a future feature cannot honor this, it either runs on-device or it
> does not ship without explicit, informed, opt-in consent.

## Where the roadmap lives

The concrete, phased build plan (reliability, onboarding + consent, telemetry, backup
hardening, theming, and the groundwork toward the assistant domains) is tracked in
[`.context/build-plan.md`](.context/build-plan.md). This document is the *why*; that one is
the *what and when*.
