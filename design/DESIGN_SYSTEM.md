# freeflo — Design System

> **This file is the source of truth for how freeflo looks and feels.**
> Before building or changing any surface — the app, the website, onboarding,
> emails, screenshots, the logo — read this first and conform to it. If a
> decision isn't covered here, follow the nearest Apple Human Interface
> Guideline and then add the ruling back to this file.

**Reference implementations** (open in a browser, treat as the visual spec):

| File | Canvas | What it shows |
|------|--------|---------------|
| `design/design-system.html` | Light | The full system, laid out like an apple.com page — tokens, type, bento, components, motion. |
| `design/brand.html` | Dark | Logo lockups. **A + 1 is locked.** |
| `design/app-icon-concepts.html` | Dark | macOS app-icon concepts. **04 · Monogram Bars is locked** (see §2). |
| `design/app-window.html` | Dark | The macOS app / menu-bar popover. |
| `design/onboarding.html` | Dark | First-run flow. |
| `design/vision.html` | Dark | Internal product-vision narrative. |
| `docs/index.html` | Dark | The public marketing site. |

---

## 1 · Principles

The same three ideas that anchor Apple's HIG. Everything below serves them.

1. **Clarity.** Legible type at every size, precise icons, and one reserved
   accent. Negative space does the heavy lifting. When in doubt, remove.
2. **Deference.** The UI steps back so the user's words come forward. Chrome is
   quiet; content is loud. freeflo's best interface is almost no interface.
3. **Depth.** Layered materials and realistic motion convey hierarchy and place
   without a single line of instruction.

**The feel we're aiming for:** _designed to disappear._ It should feel like
freeflo was always part of the Mac.

---

## 2 · Brand

**Locked lockup: `A + 1` — living waveform chip + chromed uppercase wordmark.**
Neutral / monochrome only. Never colorize the mark.

- **Mark:** a rounded chip (`radius 13px` at 44px, `7px` at 26px) with a
  near-black gradient face (`linear-gradient(155deg,#2a2a33,#0a0a0e)`) holding
  4–5 animated equalizer bars in chrome (`#fff → #c6c6cd`). The bars breathe
  (`scaleY .5 → 1`), and freeze via `prefers-reduced-motion`.
- **Wordmark:** `FREEFLO`, uppercase, SF Pro Display, weight 600–800, letter-spacing
  `.14–.15em`, filled with the chrome gradient
  `linear-gradient(180deg,#fff 0%,#d4d4da 46%,#f6f6f8 56%,#9a9aa3 100%)`.
  On light backgrounds, darken the chrome (`#2a2a30 → #5c5c66`).
- **Scales:** menu-bar (22–26px chip) · header (30–44px) · favicon (chip only).

**Don't:** add color to the mark · use the old blue→purple gradient · set the
wordmark lowercase in a header lockup · use heavy drop shadows · stretch or
recolor the chrome.

### macOS app icon (Dock / Finder / Spotlight)

**Locked separately from the A+1 lockup:** the app icon is the **Monogram
Bars** mark — three chrome pill-bars forming an "F" (top bar + mid bar + full
-height stem), on the same near-black gradient face as the chip
(`linear-gradient(155deg,#2a2a33,#0a0a0e)`), full-bleed in a rounded-square
(`border-radius ≈ 22.3%` of canvas) with a soft ambient drop shadow baked into
the PNG (legacy `.icns`, not an Xcode asset catalog — macOS does not
auto-mask/auto-shadow it). Chosen over the waveform chip itself because the
chip's bars smear at 16–32px (Finder list view / Dock-hover-off sizes);
the F-monogram reads cleanly at every size down to 16px.

Source of truth: `design/app-icon-concepts.html` (concept **04**, "Monogram
Bars"), rendered to `freeflo.icns` at the repo root. Regenerate by re-rendering
that concept's `.shape-fmono` markup at 16/32/64/128/256/512/1024px (transparent
background) and packing with `iconutil -c icns`.

**Don't:** substitute the waveform-chip mark for the Dock icon · add color ·
let the corner-radius/shadow drift from the values above.

---

## 3 · Color

freeflo is **strictly monochrome — no accent color** (locked 2026-07-27). The
brand spine is neutral: ink · chrome · black · off-white · grays. "The single
accent is light itself." Emphasis is carried by **weight, contrast, and size**,
never by hue. Apple uses blue; freeflo deliberately does not — the restraint
_is_ the identity.

### Neutral spine (identity — same everywhere)

| Token | Hex | Use |
|-------|-----|-----|
| Ink | `#1d1d1f` | Primary text on light |
| Chrome | gradient (see Brand) | Wordmark, hero words |
| Black | `#000000` | True-black app / focus |

### Light canvas — marketing, docs, light onboarding

| Token | Hex | Use |
|-------|-----|-----|
| `--ink` | `#1d1d1f` | Primary text |
| `--text-2` | `#6e6e73` | Secondary text |
| `--text-3` | `#86868b` | Tertiary / captions |
| `--canvas` | `#ffffff` | Base page |
| `--canvas-2` | `#f5f5f7` | Apple's signature off-white section |
| `--hairline` | `rgba(0,0,0,.10)` | Separators |

### Dark canvas — the app, menu-bar popover, focus, current site

| Token | Hex | Use |
|-------|-----|-----|
| `--bg` | `#08080a` | Base |
| `--bg-2` | `#0c0c0f` | Recessed |
| `--surface` | `#111116` | Cards |
| `--surface-2` | `#17171c` | Raised |
| `--surface-3` | `#1e1e24` | Highest |
| `--text` | `#f5f5f6` | Primary text |
| `--muted` | `#a0a0a8` | Secondary |
| `--faint` | `#6b6b74` | Tertiary / mono kickers |
| `--line` / `--line-2` / `--line-3` | `rgba(255,255,255,.07 / .12 / .20)` | Hairlines / brackets |

### Action (monochrome)

Actions use ink/white fills and neutral links — **no color**.

| Token | Hex | Use |
|-------|-----|-----|
| `--action` | `#1d1d1f` | Primary CTA fill on light (white text) |
| `--action-hover` | `#000000` | Primary CTA hover on light |
| `--action-dark` | `#f5f5f6` | Primary CTA fill on dark (ink text) |
| `--link` | `#1d1d1f` | Inline links on light |
| `--link-dark` | `#f5f5f6` | Inline links on dark |

Keep the palette this small. **No color accents** — not blue, and definitely
not the iOS candy system colors (green/yellow/orange/pink/purple). The only
permitted hue is a single semantic red, and only for genuine destructive/error
states — never decoration.

---

## 4 · Typography

**San Francisco throughout.** SF Pro **Display** for headlines, SF Pro **Text**
for body. Headlines are **semibold (600)** with negative tracking — never black
weights. Body sits at exactly **−0.022em**, like apple.com.

```css
--font-display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
--font-text:    -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
--font-mono:    "SF Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
```

| Role | Size (max) | Line | Weight | Tracking | Font |
|------|-----------|------|--------|----------|------|
| Display | `clamp(48px, 8vw, 96px)` | 1.04 | 600 | −0.015em | Display |
| Headline | `clamp(34px, 5vw, 56px)` | 1.07 | 600 | −0.012em | Display |
| Title | `clamp(24px, 3vw, 32px)` | 1.12 | 600 | −0.008em | Display |
| Lead | `clamp(19px, 2.4vw, 28px)` | 1.28 | 400 | −0.01em | Display |
| Body | 17px | 1.47 | 400 | −0.022em | Text |
| Caption | 14px | 1.43 | 400 | −0.016em | Text |
| Mono | 13px | — | 400 | 0 | Mono |
| Kicker | 12px | — | 600 | +0.06em, UPPERCASE | Text |

**Rules:** one Display or Headline per view. Leading tightens as size grows.
Secondary copy uses `--text-2` (light) / `--muted` (dark). Mono kickers with
wide tracking are the "engineered" voice — use for eyebrows and metadata.

---

## 5 · Layout & grid

- **Content widths:** marketing `max-width 1024px`; classic reading column
  `980px`; site `1080px`. Center everything; gutter `22px`.
- **Section rhythm:** generous. Full sections `~110px` vertical padding; tight
  `~72px`. Whitespace is the primary layout tool — err toward more.
- **Bento grid** is the signature storytelling unit: rounded tiles of mixed
  weight (`span 2` wide, `span 2` tall) so one idea breathes while supporting
  facts line up. One tile per bento may go dark or blue for emphasis.
- **Alternate canvases** down a page (white → `#f5f5f7` → white → black) to
  segment the story, exactly like an apple.com product page.

---

## 6 · Components

### Buttons
- Shape: **full pill**, `border-radius: 980px`. Weight **400** (regular).
- Primary (light): ink fill `#1d1d1f`, white text; hover `#000`. Padding
  `11px 22px` (`13px 28px` for large, 19px text).
- Primary (dark): white/chrome fill `#f5f5f6`, ink text; hover `#fff`.
- Secondary: subtle fill (`rgba(0,0,0,.06)` light / `rgba(255,255,255,.10)` dark),
  ink/white text.
- Active state: `transform: scale(.98)`. No heavy shadows. No color.

### Links — the "Learn more ›" chevron
Inline text link in `--link` (ink) / `--link-dark` (white) with a trailing `›`
that nudges `+2px` on hover; underline on hover only. This is freeflo's primary
tertiary action — prefer it over a third button. The chevron, not color, marks
it as interactive.

### Surfaces & cards
Soft-cornered cards: `radius 18px` (card) / `28px` (tile). Hairline border +
optional whisper shadow (`0 4px 20px rgba(0,0,0,.06)`). Apple leans flat —
shadows are for genuine elevation only.

### Materials / vibrancy
The macOS `NSVisualEffectView` look for the popover and optional Glass theme:
`backdrop-filter: blur(24px) saturate(140%)` over live color, with a
`rgba(255,255,255,.16)` fill and `rgba(255,255,255,.28)` hairline.

### Controls (app)
iOS/macOS-native patterns: toggle switches, segmented controls, and grouped
lists are fine **inside the app**, styled to these tokens — but they are not the
marketing language. Don't fill a marketing page with app widgets (that was the
old mistake).

---

## 7 · Radius scale

| Name | Value | Use |
|------|-------|-----|
| Control | `10px` | Inputs, small controls |
| Card | `18px` | Cards |
| Tile | `28px` | Bento tiles, large surfaces |
| Hero | `34px` | Hero media |
| Pill | `980px` | Buttons, toggles |

## 8 · Spacing — 8-point rhythm

`4 · 8 · 16 · 24 · 40 · 64 · 88`. Build padding, gaps, and stacks from this
scale. Section padding lives at the top of the scale (72–110).

---

## 9 · Motion

Physical, not flashy. **Standard ease:** `cubic-bezier(.28,.11,.32,1)`,
duration `~0.4s`.

- **Fade** — opacity, for change of content.
- **Rise** — small `translateY`, for reveals on scroll.
- **Spring** — `cubic-bezier(.5,1.4,.4,1)` scale, for arrival/attention.
- The waveform mark loops gently.

Always honor `prefers-reduced-motion: reduce` — freeze loops, drop transforms.

---

## 10 · Iconography

Prefer **SF Symbols** (system, weight-matched to text). Line icons are 1.9px
stroke, rounded caps/joins, monochrome. No filled multicolor icons.

---

## 11 · Voice & tone

Plain, confident, quiet. Short sentences. Lead with the user's benefit
("Talk to your Mac."), not the mechanism. Never oversell. Privacy is stated as
fact, not a badge. Lowercase "freeflo" in running prose; `FREEFLO` only as the
chrome wordmark.

---

## 12 · Do / Don't

**Do** — huge SF semibold headlines · tons of whitespace · ink/chrome for action ·
neutral monochrome brand · pill CTAs · chevron links · bento grids · alternating
canvases · restraint.

**Don't** — **any accent color** (no blue, no iOS `#007aff`, no candy system
colors) · heavy 700–800 body/headings · cramped component-grid dumps · colorized
logo · lowercase wordmark in lockups · shadow-heavy skeuomorphism · more than one
Display per view.

---

## 13 · Token reference (copy-paste)

```css
:root {
  /* type */
  --font-display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
  --font-text:    -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
  --font-mono:    "SF Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;

  /* light canvas */
  --ink:#1d1d1f; --text-2:#6e6e73; --text-3:#86868b;
  --canvas:#ffffff; --canvas-2:#f5f5f7; --hairline:rgba(0,0,0,.10);

  /* dark canvas */
  --bg:#08080a; --bg-2:#0c0c0f;
  --surface:#111116; --surface-2:#17171c; --surface-3:#1e1e24;
  --text:#f5f5f6; --muted:#a0a0a8; --faint:#6b6b74;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.12); --line-3:rgba(255,255,255,.20);

  /* action (monochrome — no color) */
  --action:#1d1d1f; --action-hover:#000000; --action-dark:#f5f5f6;
  --link:#1d1d1f; --link-dark:#f5f5f6;

  /* brand chrome */
  --chrome:linear-gradient(180deg,#fff 0%,#d4d4da 46%,#f6f6f8 56%,#9a9aa3 100%);

  /* radius */
  --r-control:10px; --r-card:18px; --r-tile:28px; --r-hero:34px; --r-pill:980px;

  /* motion */
  --ease:cubic-bezier(.28,.11,.32,1); --ease-spring:cubic-bezier(.5,1.4,.4,1); --dur:.4s;
}
```

---

## 14 · Changelog

- **v2.2 (2026-08-02)** — Locked the **macOS app icon**: Monogram Bars
  (chrome F built from three pill-bars), replacing the old generic blue
  "ribbon F" `freeflo.icns`. Deliberately a different mark from the A+1
  waveform chip — chosen for legibility at 16–32px. See §2.
- **v2.1 (2026-07-27)** — Locked **strictly monochrome, no accent color**.
  Replaced System Blue CTAs/links with ink (light) / white-chrome (dark) fills;
  removed all hue from the reference implementation.
- **v2.0 (2026-07-27)** — Reset onto Apple's marketing/product design language.
  Introduced the SF-semibold negative-tracking type ramp, the ink/off-white
  light canvas, pill CTAs, chevron links, and the bento grid. Corrected the
  brand mark back to the locked neutral chrome lockup (removed the stray
  blue→purple gradient). Canonical spec written to this file.
