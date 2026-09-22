# iftools — DESIGN.md

> **Verdana Health Design System** — a calm, trustworthy system built for
> digital health platforms, telehealth dashboards, and patient-facing wellness
> applications. Its foundation of deep navy and soft sage greens evokes
> clinical precision tempered by warmth. It prioritizes readability,
> accessibility, and a sense of reassurance across every touchpoint.
>
> Bound design authority for iftools, supplied verbatim by the operator.

## Theme
Light. Background `#F8FAFC`, white surfaces, navy authority, sage reserved for
the interactive and the positive.

## Tokens — Colors

| Name | Value | Token | Role |
|------|-------|-------|------|
| Primary Navy | `#0F172A` | `--navy` | Primary actions, strong headers |
| Navy Deep | `#020617` | `--navy-deep` | Hover fill on primary actions |
| Secondary Slate | `#64748B` | `--slate` | Secondary text, borders |
| Tertiary Sage | `#059669` | `--sage` | Links, CTAs, highlights |
| Background | `#F8FAFC` | `--bg` | Page background |
| Surface | `#FFFFFF` | `--surface` | Card backgrounds |
| Surface 2 | `#F1F5F9` | `--surface-2` | Muted fills, hover, disabled input |
| Border | `#E2E8F0` | `--border` | 1px borders, dividers |
| Border Strong | `#CBD5E1` | `--border-strong` | Unchecked control borders |
| Text | `#0F172A` | `--text` | Primary text, headings |
| Text 2 | `#475569` | `--text-2` | Body copy |
| Text 3 | `#64748B` | `--text-3` | Captions, helper text |
| Text Muted | `#94A3B8` | `--text-muted` | Placeholders |
| Success | `#22C55E` | `--success` | Confirmed, healthy range |
| Success Deep | `#16A34A` | `--success-deep` | Status-chip text on tinted fill |
| Success Soft | `#DCFCE7` | `--success-soft` | Status-chip fill |
| Warning | `#EAB308` | `--warning` | Pending results, caution |
| Warning Deep | `#CA8A04` | `--warning-deep` | Status-chip text |
| Warning Soft | `#FEF9C3` | `--warning-soft` | Status-chip fill |
| Error | `#EF4444` | `--danger` | Critical, out of range |
| Error Deep | `#DC2626` | `--danger-deep` | Hover fill, status-chip text |
| Error Soft | `#FEE2E2` | `--danger-soft` | Status-chip fill |
| Info | `#0EA5E9` | `--info` | Informational, new feature |

**Sage rule:** sage is reserved for interactive elements and positive states
only. The primary visual rhythm is navy on white.

## Tokens — Typography

Plus Jakarta Sans (headline), DM Sans (body), Fira Code (mono / tabular data).

| Style | Family | Size | Weight | Line height |
|-------|--------|------|--------|-------------|
| Display | Plus Jakarta Sans | 40px | 700 | 1.15 |
| H1 | Plus Jakarta Sans | 32px | 700 | 1.2 |
| H2 | Plus Jakarta Sans | 24px | 600 | 1.25 |
| H3 | Plus Jakarta Sans | 20px | 600 | 1.3 |
| H4 | Plus Jakarta Sans | 16px | 500 | 1.35 |
| Body LG | DM Sans | 18px | 400 | 1.6 |
| Body | DM Sans | 16px | 400 | 1.6 |
| Body SM | DM Sans | 14px | 400 | 1.5 |
| Caption | DM Sans | 12px | 500 | 1.4 |
| Code | Fira Code | 14px | 400 | 1.6 |

## Tokens — Spacing
Base unit **8px**: xs 4 · sm 8 · md 16 · lg 24 · xl 32 · 2xl 48 · 3xl 64.

## Tokens — Border Radius
sm 4px (badges, tags) · DEFAULT 8px (buttons, cards, inputs) · md 12px
(modals, dropdowns) · lg 16px (large containers, hero) · full 9999px (avatars,
status dots).

## Tokens — Elevation
Gentle, diffused shadows — clinical yet approachable.

| Level | Value | Use |
|-------|-------|-----|
| sm | `0 1px 3px rgba(15,23,42,0.03)` | Buttons, chips |
| DEFAULT | `0 2px 6px rgba(15,23,42,0.05)` | Cards, dropdowns |
| md | `0 4px 16px rgba(15,23,42,0.07)` | Elevated cards |
| lg | `0 8px 32px rgba(15,23,42,0.10)` | Modals, panels |

## Components

### Buttons
- **Primary**: `#0F172A` fill, `#FFFFFF` text, no border, `#020617` hover.
- **Secondary**: transparent fill, `#0F172A` text, `1px #0F172A` border,
  `#0F172A0A` hover fill.
- **Ghost**: transparent, `#475569` text, no border, `#F1F5F9` hover fill.
- **Destructive**: `#EF4444` fill, `#FFFFFF` text, `#DC2626` hover.
- Sizes: sm (6px 14px / 14px / 32px), md (10px 22px / 14px / 42px),
  lg (12px 28px / 16px / 48px).
- Disabled: 0.4 opacity, `not-allowed` cursor, all hover/focus suppressed.

### Cards
- Default: `#FFFFFF` fill, `1px #E2E8F0` border, no shadow, 8px radius.
- Elevated: `#FFFFFF` fill, no border, md shadow, 8px radius.
- 24px padding; optional tinted header strip `#0F172A` with white text for
  category labels.

### Inputs
- Default: `1px #E2E8F0` border, white fill, no shadow.
- Hover: `1px #0F172A` border.
- Focus: `2px #0F172A` border + `3px #0F172A18` ring.
- Error: `2px #EF4444` border + `3px #EF444418` ring.
- Disabled: `1px #E2E8F0` border, `#F1F5F9` fill.
- 42px height, `10px 14px` padding, 8px radius, DM Sans 14px/500, `#0F172A`.
- Label: DM Sans 12px/400 `#475569`, 6px below. Helper 12px `#475569` 4px
  below. Error text 12px `#EF4444` 4px below.

### Chips
- Filter: `#F8FAFC` fill, `#0F172A` text, `1px #E2E8F0` border.
- Filter Active: `#0F172A` fill, `#FFFFFF` text, no border.
- Status Success: `#22C55E15` fill, `#16A34A` text, no border.
- Status Warning: `#EAB30815` fill, `#CA8A04` text, no border.
- Status Error: `#EF444415` fill, `#DC2626` text, no border.
- `4px 12px` padding, 4px radius, 12px/500, uppercase, `0.5px` tracking.

### Lists
48px row height, `8px 16px` padding, `1px #F1F5F9` divider, `#F8FAFC` hover,
`#0F172A06` active. DM Sans 16px/400 label, 14px/400 `#475569` description.

### Checkboxes / Radios
18×18px, 4px radius (checkbox) / full (radio). Unchecked: `1.5px #CBD5E1`
border on white. Checked: navy fill, white checkmark / 8px navy dot. Disabled:
40% opacity. 8px label spacing.

### Tooltips
`#0F172A` background, `#F8FAFC` DM Sans 12px/400 text, `6px 12px` padding,
8px radius, 6px triangle, 240px max width, 150ms show / 0ms hide.

## Do's and Don'ts
1. Do use navy + white contrast as the primary visual rhythm; sage is reserved
   for interactive elements and positive states only.
2. Do lean on generous whitespace — health interfaces should never feel cramped.
3. Do use the softer 8px radius consistently.
4. Don't introduce harsh neons or saturated accents.
5. Don't use condensed or decorative fonts.
6. Do use uppercase chip labels with tracking.
7. Don't overload dashboards with dense data; use progressive disclosure.
8. Do include clear iconography alongside text labels.
9. Don't use heavy drop shadows; the diffused elevation system maintains the
   clean, clinical aesthetic.
10. Do use Fira Code for clear tabular numeral alignment in data and vitals.
