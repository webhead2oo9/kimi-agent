# Iteration and verification

Adapted for Kimi from OpenAI's
[Image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting).
The central method is simple: inspect the result, revise one condition, repeat
the preservation constraints, and stop when the user's requirement is met or
another paid call needs their direction.

## Inspect before revising

A successful tool response proves that a PNG was decoded, saved, and queued. It
does not prove that required words are correct, a face is unchanged, arrows point
the right way, or transparent pixels exist. Use an available image-view or
analysis tool only when it exposes the actual returned workspace file to the
model. Do not infer appearance from the prompt or accessible attachment
description; that description states intent, not inspection.

When the image is visible, compare it against a checklist derived from the brief:

- **Composition:** orientation, crop, hierarchy, count, placement, scale, empty
  space, body framing, gaze, and interactions.
- **Appearance:** intended medium, materials, textures, palette, light direction,
  contact shadows, reflections, depth, and level of realism.
- **Text:** exact characters, capitalization, punctuation, number of appearances,
  line breaks, readability, and absence of pseudo-copy.
- **Facts:** labels, units, sequence, arrows, axes, legend mapping, dates, and
  scientific or historical relationships.
- **Preservation:** identity, face, product geometry, labels, layout, camera,
  surrounding objects, and background.
- **Artifacts:** malformed hands, duplicated objects, broken edges, halos,
  warped geometry, accidental logos, signatures, or watermarks.
- **Transparency:** decoded alpha variation and edge quality, if and only if an
  actual alpha-inspection capability is available.

Also inspect the structured result. `requested` records effective Kimi hints;
`actual` establishes decoded size and PNG format; `provider_reported` repeats
allowlisted provider labels; `mismatches` identifies some metadata conflicts.
Neither provider labels nor a missing mismatch proves visual compliance.

If the model cannot view the output, return it without fabricating a review.
State the specific checks the user should make and offer a focused edit after
they report what is wrong.

## Revise one variable at a time

Use the prior returned PNG's observed workspace path as the next edit input.
Keep follow-ups narrow enough to attribute improvement or regression. Repeat
critical constraints even when context contains the earlier prompt.

Weak follow-up:

```text
Make it better and more wintry.
```

Controlled follow-up:

```text
Change only the weather and time of day: make the existing scene a quiet winter
evening with light falling snow and cool ambient light. Preserve billboard
geometry, road layout, camera position, product shape and label, exact headline,
typography, spacing, and every other object. Do not add new text or vehicles.
```

If the actual problem is a misspelled headline, revise the headline only. If the
layout is correct but the subject is too large, change scale and placement only.
Stacking typography, palette, pose, weather, camera, and object changes in one
retry makes drift hard to diagnose.

## Billboard sequence

This pattern demonstrates a base image followed by one-condition revisions. Use
it only when the product photograph actually exists.

Initial edit brief:

```text
Reference 1 supplies the product and must preserve its container geometry, cap,
colors, and printed label. Place it in a realistic roadside billboard mockup at
sunset, with believable billboard material, support structure, highway scale,
perspective, and warm directional light.

Billboard headline, exactly once: "CLEAR STARTS HERE"
Bold sans serif, high contrast, centered, clean kerning, fully legible.
No other copy, invented logos, signature, or watermark.
```

After inspecting the output, use its returned path for a narrow edit such as the
winter-evening prompt above. Do not regenerate from text alone if preserving the
approved layout matters. On every turn, repeat exact copy and product constraints
because edit history can drift.

Potential second revisions should each stand alone: reduce snowfall so the copy
is clear; shift only the headline upward; remove one accidental car; correct one
word. Do not consume the remaining per-turn budget simply because it exists.

## Character consistency across scenes

First create a reusable character reference with visually measurable anchors:
species or age, face shape, hair or markings, proportions, outfit, signature
object, palette, personality, and rendering medium.

```text
Create a portrait children's-book character sheet showing one original young
forest messenger. Round expressive face, short dark curls, moss-green hooded
cape, rust-orange tunic, soft brown boots, small acorn-shaped satchel; slightly
oversized head and short picture-book proportions; gentle eyes and quietly brave
expression. Hand-painted watercolor, soft graphite outlines, warm earth palette.
Simple pale woodland backdrop, no text, no watermark, no copyrighted character.
```

Inspect the sheet, then use its actual returned path as the character reference
for a new scene. Repeat defining anchors instead of saying only “same character.”

```text
Reference 1 is the approved character. Show the same forest messenger kneeling
beside a frightened hedgehog after a windstorm, lifting one small branch away
from its burrow. Preserve the same face, curls, eye shape, body proportions,
moss-green cape, rust tunic, brown boots, acorn satchel, watercolor palette, and
graphite line quality. New setting: damp woodland clearing with broken twigs and
soft morning light. Warm, reassuring mood. Do not redesign the character, change
the outfit, add text, or add a watermark.
```

Check face geometry, markings, proportions, costume pieces, colors, and medium.
For a book, keep one approved reference rather than allowing every new scene to
become the sole source of truth; serial edits can compound drift. Image prompting
can improve consistency but cannot guarantee a perfectly fixed character model.

## Decide whether another call is justified

One generation is normally enough for a direct request. A second call is
reasonable when the user explicitly asked for iterative refinement, or when an
inspected result has one clear correctable miss and the normal budget permits.
Ask first when the next call is an unsolicited alternate, broad creative retry,
or paid quality experiment.

If a provider call fails, remember that the logical call budget may already have
been consumed. Do not loop automatically. Correct local input errors before a
provider call; for a genuine provider failure, report it concisely and retry only
within the user's request and remaining budget.

Across multi-turn work, do not claim a prior call succeeded unless its result is
visible in conversation state. Do not refer to “the previous image” if no saved
path or attachment was actually observed.

## When deterministic tools are better

Use image generation for semantic visual synthesis. Use deterministic rendering
or compositing when exactness is the central requirement and the necessary tool
is actually available:

- plot supplied verified data when bar lengths, axes, ticks, and units must be
  mathematically faithful;
- typeset final copy when spelling, font metrics, and alignment must be exact;
- composite an approved edited region onto the untouched original when protected
  pixels must be identical;
- build a production UI, vector logo, print dieline, or scientific schematic in
  its native code/design format when editability and exact geometry matter.

This is conditional guidance, not permission to invent capabilities. If no such
tool is exposed, do not claim deterministic plotting, alpha inspection,
pixel-preserving compositing, vector output, or exact typesetting. Generate a
concept, explain its limits, and let the user choose the next step.

## Acceptance checklists by artifact

### Photograph or scene

- Correct subject count, framing, gaze, action, and scale.
- Lighting has a plausible source; feet and objects make contact with surfaces.
- Skin, fabric, weather, reflections, and wear match the requested realism.
- No accidental glamour treatment, text, logos, or poster grading.

### Logo, cutout, or product

- Silhouette is clear at small size; geometry and labels are intact.
- Padding and centering fit the intended placement.
- Edges have no fringe, unwanted shadow, backdrop, or checkerboard.
- Transparency is claimed only after actual alpha inspection.

### Infographic, diagram, slide, or chart

- Title and every label are accurate and legible.
- Arrows, stages, axes, units, legends, values, and relationships are correct.
- Data and citations came from verified user/research inputs, not a recipe.
- Reading order and spacing work at actual use size.
- Exact plots were rendered deterministically when required and possible.

### Edit with references

- Each reference contributed only its assigned role.
- The requested target changed and protected regions did not visibly drift.
- Identity, product geometry, layout, crop, light, and labels remain acceptable.
- Insertions have correct perspective, occlusion, edge quality, and contact shadow.

### Card, comic, or packaging

- Story beats or product hierarchy read in the intended order.
- Characters, props, and packaging geometry are consistent.
- Exact copy appears the requested number of times with no pseudo-copy.
- Originality constraints, trademarks, logos, and watermarks were respected.

Finish by reporting what was generated or edited, the returned workspace path
when useful, and only those properties actually observed. If visual inspection
was unavailable, say so in one sentence and identify the highest-risk checks.
