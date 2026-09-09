# Generation recipes

These are adapted, deployment-safe patterns based on OpenAI's
[Image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting).
They are starting structures, not text to copy blindly. Replace content with the
user's actual subject, facts, brand, and intended use.

## Documentary photography

Build a photographic brief from subject, action, framing, light, surface detail,
and exclusions. “Real photograph” plus ordinary imperfections helps distinguish
documentary realism from polished concept art.

```text
Deliverable: portrait-oriented documentary photograph for an editorial profile.
Scene: an older bicycle mechanic tightening a brake cable in a compact street-side
workshop; a patient terrier lies beside the tool cabinet.
Framing: eye-level medium shot, hands and workbench visible, candid off-center
composition, modest depth of field.
Light/texture: overcast window light, natural skin texture, grease on fingertips,
scratched metal tools, worn cotton apron, restrained color, subtle film grain.
Tone: attentive and unposed, like a real observed moment.
Exclude: beauty retouching, glossy advertising light, theatrical color grading,
perfectly clean surfaces, text, logos, or watermarks.
```

For a wide rainy or neon scene, do not rely on “moody.” Name the scale of the
subject, wet pavement reflections, sign colors, rain density, night exposure,
and where the key light originates. For an action shot, specify gaze, limbs,
contact with objects, and whether motion should freeze or blur.

## Process infographic

State the process, audience, reading direction, stages, and relationships. Make
technical components an explicit checklist and reserve enough whitespace for
labels.

```text
Create a portrait technical infographic titled "How a Heat-Pump Water Heater Works"
for a homeowner who understands basic household systems.

Flow, top to bottom:
1. Room air enters through the intake.
2. Refrigerant absorbs heat at the evaporator.
3. The compressor raises refrigerant pressure and temperature.
4. A condenser coil transfers heat into the tank.
5. Cooled, dehumidified air exits; hot water leaves through the outlet.

Required components: intake fan, filter, evaporator, compressor, condenser coil,
insulated tank, cold-water inlet, hot-water outlet, condensate drain.
Use clear arrows, a small color legend for air/water/refrigerant, short readable
labels, cutaway icons, white background, and consistent flat line art.
Do not invent performance numbers, certifications, or extra components.
```

Verify every component, arrow, and label. If relationships are safety-critical,
the generated image should not be treated as an authoritative schematic.

## Advertisement with exact copy

Keep the copy specification isolated from visual art direction. State exact
count and prohibit unrelated marks.

```text
Create a polished streetwear campaign photograph for an original label named KITE.
Three friends wait outside a late-night diner, relaxed and naturally interacting;
clean composition, energetic cobalt and warm-red accents, premium fashion-photo
texture, room for a headline above their shoulders.

Render this tagline exactly once: "MAKE THE NIGHT YOURS."
Use bold modern sans-serif type, clearly legible and integrated into the layout.
No other text, no unrelated logos, no signature, and no watermark.
```

Inspect letterforms and punctuation rather than assuming quoted text is exact.
If the user supplies a brand guide, use it only as an observed reference and
state which visual attributes it controls.

## Original logo or transparent mark

Ask for a strong silhouette, simple geometry, negative space, scale tolerance,
and original non-infringing design. A generation is still a raster PNG; words
such as “vector-like” describe appearance, not file format.

```text
Design one original centered mark for "North Orchard," a small cider maker.
Combine an abstract apple leaf with a north-pointing shape using clean, flat,
vector-like geometry and balanced negative space. Warm, understated, and timeless;
legible as a bottle-cap icon and on a storefront sign. Minimal strokes, no scene,
no mockup, no gradients unless the form genuinely needs one.

Background: fully transparent. Leave generous padding and crisp clean edges.
Do not draw a checkerboard or colored rectangle. No trademarked imagery,
watermarks, or extra copy.
```

Use `background: transparent` in addition to the prompt. Do not call the PNG
transparent unless actual alpha was inspected. If the user needs several logo
directions, each Kimi call still produces one image; ask before spending calls
on additional directions unless variations were explicitly requested.

## Historically grounded scene

Place and date establish a context, but model inference is not verification.
List the historically load-bearing elements and later inspect them.

```text
Create a realistic street photograph set near a London Underground entrance in
October 1940. Civilians in varied, period-appropriate everyday clothing move
past sandbags and blackout signage during daylight; restrained documentary
composition, realistic materials, no modern vehicles or contemporary branding.
Avoid heroic staging or movie-poster lighting.
```

Check garments, transit details, signs, architecture, objects, and any visible
text against reliable sources. A famous place/date prompt can improve context,
but it does not certify historical accuracy.

## Four-beat comic

Translate narrative into one visible action per panel. Define panel order,
character anchors, and whether text is allowed.

```text
Create a vertical four-panel wordless comic with consistent characters and room
between panels.
Panel 1: a student leaves the apartment; a small orange cat watches from behind
the curtain, paws high on the sill.
Panel 2: the latch closes; the cat turns toward the quiet room, ears forward.
Panel 3: the cat sprawls regally across the desk chair amid one torn paper scrap,
a diagonal beam of sun making the moment feel triumphant.
Panel 4: the student returns; the cat sits neatly by the door with an innocent
expression while the chair is visible in the background.
Style: warm ink-and-watercolor newspaper comic, readable silhouettes, stable room
layout and cat markings. No captions, speech bubbles, logos, or watermarks.
```

Keep the beats concrete. Too many simultaneous actions in one panel undermine
pacing and character continuity.

## Usable interface preview

Describe a product that already exists rather than asking for abstract “UI
concept art.” Specify hierarchy, realistic controls, spacing, and device frame.

```text
Create a realistic portrait mobile-app preview for a neighborhood tool library.
Show a compact header with today's opening status, a search field, three available
tools with thumbnail/category/due-date rows, a small "Ready for pickup" module,
and bottom navigation for Browse, Loans, and Account. White canvas, forest-green
accents, clear sans-serif typography, generous tap targets, minimal decoration,
consistent spacing. Place the finished UI inside a generic modern phone frame.
It should look implementable, not futuristic concept art. No lorem ipsum.
```

Treat generated UI as a visual mockup, not working code. Verify labels, hierarchy,
and repeated components. Use code-native layout when exact pixels, accessibility,
or production behavior is required and such tooling is available.

## Scientific classroom diagram

Use an instructional-design brief: learner level, lesson objective, required
components, relationships, consistent icon system, and prohibited distractions.

```text
Create a landscape classroom diagram titled "DNA Replication: One Strand at a Time"
for students aged 15–17. Show one replication fork and label helicase, leading
strand, lagging strand, DNA polymerase, RNA primer, Okazaki fragments, and ligase.
Use directional arrows and a small 5-prime/3-prime legend. Clean white background,
flat scientific icons, consistent color roles, readable labels, and ample spacing.
Do not show transcription, ribosomes, cell division, or decorative laboratory gear.
```

Verify the directionality, label placement, molecular relationships, and audience
appropriateness. Higher quality can help dense labels but cannot validate science.

## Slide or chart with verified data

Prompt this as an artifact specification. Provide only verified numbers, units,
date ranges, labels, and source notes. Never convert plausible-looking sample
figures into claims.

```text
Create one 16:9 landscape briefing slide titled "Regional Solar Adoption."
Use only the verified dataset supplied in this request. Left: one-sentence takeaway
in 28-point-equivalent bold type. Right: a clean horizontal bar chart, categories
in the supplied order, zero baseline, values printed at each bar end, unit shown
once in the axis title. Footer: the exact supplied source name and publication
date. White background, navy and amber accents, modern sans serif, strong reading
hierarchy, generous margins. No stock photos, 3D effects, gradients, decorative
icons, invented citations, or invented values.
```

If no verified dataset was supplied, ask for it or omit numbers and label the
result as a layout concept. For exact data encoding, generate the chart with a
deterministic plotting tool if one is actually available; do not rely on an image
model to produce numerically faithful bar lengths, axes, or footnotes.

## Holiday or commemorative card

Describe physical materials and lighting as well as emotional tone, especially
for a photographed paper or pop-up treatment.

```text
Create a portrait winter greeting card featuring a well-loved wooden train resting
in an open keepsake drawer beside a frosted window. Fine wood scratches, a repaired
wheel, layered paper snowflakes, visible paper fibers and folds, soft studio light,
warm lamplight against cool snow, nostalgic but restrained premium print finish.

Include only this exact line: "Warm wishes, carried home."
Original artwork; no trademarks, logo, signature, watermark, or additional words.
```

Check the exact copy and whether the emotional signal comes from visible details,
not generic bokeh alone.

## Collectible product and packaging

Specify product geometry, materials, package construction, camera treatment, and
print hierarchy. Keep the design original and avoid recognizable protected marks.

```text
Create a premium retail product photograph of an original collectible tin robot
with rounded shoulders, a small chest dial, brushed-metal panels, and lightly worn
paint edges, sealed in a clear window box with a midnight-blue paper insert.
Straight-on three-quarter product view, controlled studio reflections, realistic
cardboard and molded-plastic texture, shallow depth of field, crisp package print.

Packaging text, exactly once: "MIDWINTER WORKSHOP EDITION"
No other text, trademarks, familiar franchise styling, logos, or watermarks.
```

Inspect product proportions, transparent-plastic reflections, label spelling, and
whether extra pseudo-copy appeared. Packaging concepts are visual explorations,
not print-ready dielines unless rebuilt with deterministic design tools.
