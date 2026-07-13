---
name: seat-layer-design
description: The Seat Layer frontend design language, extracted from that project's shipped pages, for reuse on Winfetti's web surfaces. Use whenever creating or restyling anything under web/. Covers the paper-and-ink token system, type rules, hairline structure patterns, the isometric-plane extrusion recipe for 3D product illustration, motion and copy rules, and the QA pass.
---

# Seat Layer design language

Warm paper and ink. Light, printed, calm. Product imagery gets real depth
through paper-cutout extrusion on an isometric plane; nothing else glows,
lifts, or floats. This file is the source of truth for Winfetti's web
surfaces; tokens live in `web/static/theme.css` and pages may not invent
parallel colors.

## Tokens

- Background warm paper `#F7F5F1`; panels white `#FFFFFF` / `#FBFAF7`;
  raised fills `#EFECE5`. Ink text `#1A1814`, dim `#5C574D`, faint
  `#8A8377`.
- Hairlines define all structure: `rgba(26,24,20,.10)` and `.16`. No card
  shadows, no elevation stacks.
- One wine accent `#8A3B4A` (hover `#6E2C39`). Soft green `#3E9B5F` for
  success and sold/sent states, `#C0392B` for danger. Sand `#B4A98E` for
  coin and tier fills.
- Illustration-only shades (allowed in SVG imagery, documented here and in
  theme.css): extrusion side `#D9D3C6`, coin side `#9A8F73`.
- Corner radius stays `2px`, the printed feel. `border-radius:999px` and
  `50%` only for actual circles (coins, seats, wheel hubs).

## Type

- One family: Inter (self-hosted variable, `fonts/inter-var.woff2`, no
  third-party requests). Headlines weight 650, tracking -.02 to -.03em,
  sentence case, never uppercase.
- Space Mono for two jobs only: microlabels (11px, letterspaced .14 to
  .22em, uppercase, the only uppercase on the page) and every number the
  server produces (balances, prices, counts, ids).
- Form labels are mono microlabels above the input. Errors below in
  danger, 11.5px.

## Structure patterns

- `.sitenav`: brand mark + name left, one boxed `.tabs` bar right; tabs
  become a full-width row under 720px.
- Hero and feature layouts are asymmetric: copy left, the product itself
  right. The illustration is always the product's own subject matter,
  never stock scene art.
- Grids are hairline-celled boxes (`.caps`, `.duo`): cells share one
  border, first cell may carry a tint of the accent at 6% to break
  same-ness. No floating cards.
- `.strip`: a mono-uppercase integration or fact bar, one hairline box.
- Sequential steps use mono numbers (01, 02, ...) only where order truly
  means something.
- Footer: hairline top border, mono 11px, faint.

## The 3D recipe (isometric paper-cutout extrusion)

This is how depth works, and the only way it works. It reads as printed
paper lifted off the page, not as CSS chrome.

1. The stage: a clipped container with an edge fade so the illustration
   dissolves into the page:
   `mask-image:linear-gradient(90deg,transparent,#000 10%,#000 90%,transparent)`.
2. The plane: one wrapper div tilted as a whole,
   `transform:perspective(1400px) rotateX(52deg) rotateZ(-33deg) scale(1.7)`.
   Everything 3D lives inside one SVG inside this plane so it all
   foreshortens together. Never tilt individual elements.
3. Extrusion: with the plane rotated -33deg, screen-down in local SVG
   coordinates is the unit vector `(-sin33, cos33) = (-0.545, 0.839)`.
   Every solid object is drawn twice: a side copy offset along that
   vector by its thickness (fill `#D9D3C6`, no stroke), then the true top
   face over it. Inner contents can extrude too, at a smaller depth, as a
   flattened silhouette copy (class `.ex`: same shade, text hidden).
4. One `filter:drop-shadow(12px 18px 18px rgba(26,24,20,.14))` on the
   whole SVG sells the tilt. No other shadows anywhere.
5. Objects that rotate in place (a wheel) keep their side copy static:
   a disc's extruded side is rotation-invariant, so only the top face
   group spins.
6. `prefers-reduced-motion`: the plane goes flat (`transform:none`) and
   all animation stops. The flat version must still be complete and
   correct.
7. Entry: groups fade in with a small stagger (60ms steps, .6s ease).
   Ongoing motion must carry meaning (seats selling, a wheel the user
   spun); nothing loops just to look alive.

## Motion

- Transitions 100-300ms. `:active` presses down 1px. No hover lift on
  anything that is not a primary action.
- At most one idle animation per page and it must depict something true.

## Copy

- No em dashes anywhere: copy, titles, comments, console output. Use a
  comma, colon, period, or "and".
- Complete plain sentences, concrete verbs, sentence case. Banned:
  elevate, unleash, seamless, supercharge, revolutionize.
- Page titles join with a middot: "Console · Winfetti".
- No emoji anywhere. No invented metrics or fake proof.

## QA pass (before calling any visual change done)

Render both pages at 1440px and 390px in a real browser and look at them.
Then these must all print nothing:

```sh
grep -rn "—" web/
grep -rniE "border-radius: *(999px|9999px)" web/ | grep -v "circle"
grep -rniE "seamless|unleash|elevate|supercharge|revolutionize" web/
grep -rn "linear-gradient" web/ | grep -v "mask-image"
for c in $(grep -rhoiE "#[0-9a-f]{6}\b" web/index.html web/console.html web/static/*.js web/static/favicon.svg | tr 'a-f' 'A-F' | sort -u); do
  grep -qi "${c#\#}" web/static/theme.css || echo "off-token color $c"
done
```

Accessibility floor: visible `:focus-visible` outlines everywhere, labels
on every input, body contrast 4.5:1 (mono microlabels 3:1), semantic
headings in order, reduced motion honored.
