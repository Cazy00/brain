# Asset briefs — what to generate, and exactly how

This file is the source of truth for `brain`'s visual assets. Every brief below
is **self-contained**: hand any one of them to an image-generation agent with no
other context and it has everything it needs.

Two tiers, and the split is deliberate:

- **Tier A — hand-authored SVG / mermaid (not generated).** Every diagram whose
  labels must be *exactly* right, because they name real files, real commands and
  real folders. Image models garble dense text, and a diagram that says
  `knowlege/archve/` is worse than no diagram. These are authored as code, live
  in the repo as text, and are diffable when the system changes.
- **Tier B — generated images (this is what you generate).** The pieces where
  visual quality matters more than label density: the mark, the hero, the social
  card, and two conceptual illustrations. Every one of these is specified to use
  **five words of text or fewer**, so a model can render it cleanly.

If a Tier B render comes back with mangled text, don't fight it — generate it
again with **zero** text and I'll set the type in SVG over the top.

---

## The visual system

Every asset shares this. Give this section to the generation agent verbatim
alongside the individual brief.

### Palette

| Role | Dark | Light | Used for |
|---|---|---|---|
| Base | `#0B0D10` | `#FAFAF8` | Background |
| Surface | `#14181D` | `#F0EFEA` | Cards, raised planes |
| Hairline | `#242A31` | `#DFDDD5` | 1px borders, grid lines |
| Ink | `#E8EAED` | `#16181C` | Primary text |
| Muted | `#7D8590` | `#6B7280` | Secondary text, inactive |
| **Accent** | `#D99A2B` | `#B87A14` | The one warm colour — current, canonical, alive |
| Cool | `#4A7DBF` | `#3A66A0` | Derived things: the index, the cache, the search layer |
| Affirm | `#4E9A6B` | `#3D7F55` | Passing, healthy, committed |
| Refuse | `#C4553D` | `#A8402B` | Blocked, refused, superseded-out |

The accent is a warm ochre/amber on near-black. That is the whole identity:
**ink and paper meets terminal.** Deliberately not the purple-to-blue gradient
every AI product ships — this is a tool about permanence and files, not about
magic.

### Typography

- Labels and UI: a neo-grotesque or geometric sans — Inter, Söhne, Suisse Int'l,
  Helvetica Now. Medium weight. Tight tracking.
- Anything that represents a path, command or filename: a monospace —
  JetBrains Mono, Berkeley Mono, IBM Plex Mono.
- No script, no serif, no display faces, no letterspaced all-caps titles.

### Hard rules (all assets)

- **No emojis anywhere.** Use drawn geometric icons if a symbol is needed.
  This is a standing constraint for everything shipped.
- No 3D bevels, no drop shadows used as decoration, no glow, no lens flare.
- No gradient meshes, no iridescence, no "AI aesthetic" purple/cyan.
- No people, no faces, no hands, no brains-as-organs, no glowing neural
  networks, no circuit-board-as-brain. That whole visual cliché is banned —
  this is a filing system, not a mind.
- Flat vector or precise 2D isometric only. Crisp 1px hairlines.
- Generous negative space. If it feels empty, it is probably right.
- Every asset needs a **dark variant and a light variant** — the README serves
  both via `<picture>` and `prefers-color-scheme`.

### Delivery

- Format: **SVG preferred**; if the tool only emits raster, PNG at **3×** the
  stated size with a transparent background where the brief says so.
- Naming: `docs/assets/<name>-dark.svg` and `docs/assets/<name>-light.svg`.
- Drop them in `docs/assets/` and tell me — I wire them into the README and docs.

---

## Tier B1 — The mark

**File:** `mark-dark.svg`, `mark-light.svg`
**Canvas:** 512 × 512, transparent background
**Text in image:** none

Design a single abstract mark for a tool called `brain` that stores knowledge as
plain markdown files in git, and refuses to serve outdated ones.

The concept to draw: **a stack of layers where exactly one layer is lit.** Four
to six horizontal planes viewed at a slight angle — like sheets of paper seen
edge-on, or strata in rock. The planes below are recessed and drawn in the muted
hairline colour; one plane near the top is solid, filled with the amber accent,
and sits very slightly proud of the others. Nothing else.

That is the entire product in one image: history is kept and visible underneath,
but only the current layer is presented as true.

Constraints:
- Purely geometric. Straight edges, consistent 1px hairlines, one filled shape.
- Must survive being rendered at 16 × 16 as a favicon — so: no more than six
  elements, no thin details, strong silhouette.
- Square-ish overall balance, optically centred, ~15% padding inside the canvas.
- Do not draw a head, a brain, a lightbulb, a node graph, a folder icon, or a
  database cylinder.

Dark variant: hairlines `#242A31`, recessed planes `#14181D`, lit plane `#D99A2B`.
Light variant: hairlines `#DFDDD5`, recessed planes `#F0EFEA`, lit plane `#B87A14`.

---

## Tier B2 — README hero banner

**File:** `hero-dark.png` (or `.svg`), `hero-light.png`
**Canvas:** 2400 × 800 (renders at 1200 × 400)
**Text in image:** the single word `brain`, lowercase, monospace — and nothing else

A wide, calm banner for the top of a GitHub README.

Composition: the word `brain` set in lowercase monospace, medium weight, sitting
on the **left third** of the canvas, optically centred vertically. To its right,
occupying the middle and right thirds, a quiet field of horizontal hairlines —
like ruled paper, or like the layers from the mark extended out into a landscape.
The lines are mostly the hairline colour; a small cluster of three or four of
them, roughly two-thirds across, are amber. Those amber lines are shorter than
the rest and slightly offset, as if a few records among thousands have been
picked out.

Feel: an archival index card. Quiet, precise, a bit austere. Not a product
splash, not a launch graphic.

Constraints:
- Background is the flat base colour, edge to edge, no vignette.
- The word `brain` must be crisp and correctly spelled — five letters, all
  lowercase, no ligatures, no stylisation. If the generator cannot render text
  cleanly, produce the banner **without any text** and say so; I will set the
  word in SVG.
- No tagline, no URL, no logo lockup, no icons.
- Leave the far-left ~8% and far-right ~8% quiet — the banner gets cropped
  differently on mobile GitHub.

---

## Tier B3 — Social / OG card

**File:** `og-card.png`
**Canvas:** 2400 × 1260 (renders at 1200 × 630) — **one variant only, dark**
**Text in image:** `brain` (large) and `a second brain that stays true` (small)

The image that appears when the repo is linked in Slack, X, or iMessage. It must
be legible as a 400px-wide thumbnail.

Composition: the mark from B1 at roughly 18% of the canvas width, top-left, with
comfortable margin. Below it, `brain` set large in lowercase monospace. Below
that, one line of smaller sans-serif text in the muted colour: `a second brain
that stays true`. The right half of the canvas carries the same horizontal
hairline field as the hero, fading toward the right edge, with one amber line
picked out.

Constraints:
- Dark base `#0B0D10` only. There is no light OG card.
- Both text strings must be spelled exactly as written above, all lowercase.
  Verify letter by letter before delivering.
- Nothing may sit within 6% of any edge except the hairline field, which may
  bleed off the right.
- No border, no rounded card, no shadow. Full bleed.

---

## Tier B4 — Conceptual illustration: "what a second brain usually does"

**File:** `concept-decay-dark.svg`, `concept-decay-light.svg`
**Canvas:** 1600 × 900
**Text in image:** two words only — `year one` and `year three`

This one earns its place in the README by making the *problem* legible in two
seconds, before any architecture is explained.

A single wide frame split into two halves by generous whitespace (no divider
line).

**Left half, labelled `year one`:** roughly twenty small horizontal bars
arranged in a loose grid, evenly spaced. All of them are solid amber. It reads
as small, tidy, and entirely trustworthy.

**Right half, labelled `year three`:** roughly two hundred of the same bars,
much smaller, densely packed in the same footprint. Only about fifteen of them
are amber. The rest are the muted grey — and crucially they are *visually
identical in every way except colour*, so nothing about the picture tells you
which ones are still true. A few grey bars sit in the middle of amber clusters.

The point being made, without a caption: the pile grew, the truth did not, and
they look the same from outside.

Constraints:
- Both halves occupy the same bounding-box footprint, so the density change is
  the whole story.
- The two labels are lowercase, small, muted, set beneath each cluster.
- No arrows between the halves, no "before/after" chrome, no X marks, no
  question marks, no red.
- Do not make the right half look chaotic or scattered — it must look *orderly*.
  Orderly and wrong is the point.

---

## Tier B5 — Conceptual illustration: "the gate"

**File:** `concept-gate-dark.svg`, `concept-gate-light.svg`
**Canvas:** 1600 × 900
**Text in image:** none

A quiet illustration of the idea that everything written is checked before it
is allowed to become permanent.

Composition, left to right across the frame:
1. On the left, a loose scatter of eight or nine small rectangles at varied
   angles, in the muted colour — unsorted incoming material.
2. In the centre, a single tall, narrow vertical slot — an aperture cut through
   a solid plane that spans the full height of the frame. Thin. Precise. The
   plane is the surface colour with a hairline edge; the slot is the base colour
   showing through.
3. On the right, the same rectangles but fewer — five or six — now perfectly
   aligned, evenly spaced, axis-parallel, and filled with the amber accent.
4. Two or three rectangles rest against the *left* face of the plane at an
   angle, clearly not passing through, drawn in the refuse colour `#C4553D` at
   low opacity.

Feel: a sorting machine drawn by someone who likes Swiss posters. Mechanical,
calm, no motion blur, no sparkle.

Constraints:
- Strictly orthographic. No perspective, no depth shading beyond flat fills.
- The aperture must read as *narrow* — a gate, not a doorway.
- No arrows, no chevrons, no motion lines, no conveyor belt, no funnel shape.
- Nothing anthropomorphic.

---

## Tier A — diagrams I author as code (no generation needed)

Listed here so the set is documented in one place, and so you know what you are
*not* being asked to make. These live as mermaid in the markdown, or as
hand-written SVG in `docs/assets/`, because their labels name real paths and
must change when the code changes.

| Diagram | Where it goes | Why it must be code |
|---|---|---|
| System architecture — substrate, toolbelt, MCP server, the agents that connect | `docs/how-it-works.md` | Names every real binary and config file |
| The loop — capture → `inbox/` → consolidate → canonical → retrieve | `README.md`, `docs/daily-use.md` | Names real commands and folders |
| Retrieval scope — which folders default search sees, which are opt-in, which are excluded | `docs/retrieval.md` | Must match `.rgignore` exactly |
| Supersede lifecycle — current → superseded → `archive/`, and out of search | `docs/note-contract.md` | Names real frontmatter fields |
| The commit gate — write → lint → pre-commit hook → CI → auto-push | `docs/how-it-works.md` | Must match the hooks and `gate.yml` |
| Consolidation's two-agent boundary — propose (writes) vs audit (read-only, digest-blind) → branch → human | `docs/consolidation.md` | Describes a security boundary; wrong labels here are dangerous |

---

## Priority

If you only generate some of these, generate them in this order:

1. **B3 — OG card.** Highest leverage per unit effort. Every link to the repo
   renders it, and a repo with no OG card looks unfinished.
2. **B1 — the mark.** Feeds the OG card, the favicon, and the docs header.
3. **B4 — the decay illustration.** The one image that makes a stranger
   understand why this exists rather than what it does.
4. **B2 — hero banner.** Nice, not load-bearing; the README reads fine without it.
5. **B5 — the gate.** Purely supporting.
