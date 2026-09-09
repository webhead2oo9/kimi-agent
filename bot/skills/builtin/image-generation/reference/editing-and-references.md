# Editing and multiple references

Adapted for Kimi from OpenAI's
[Image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting).
Every recipe below assumes the named inputs have first been observed. Do not call
`generate_image` with imaginary filenames or paths; ask for a missing source.

## Build an edit contract

Describe the base image, the permitted change, the protected features, and the
integration cues. For multiple inputs, align “Reference 1,” “Reference 2,” and so
on with the actual order supplied to `reference_paths` or
`reference_attachments`.

```text
Base: Reference 1.
Change only: [one bounded operation and its destination].
Use from other references: [specific subject/style/garment/object role].
Preserve: [identity, geometry, crop, layout, camera, light, shadows, text, background].
Integrate: [perspective, scale, contact, color temperature, reflections, grain].
Exclude: [new props, accessories, copy, logos, marks, broad restyling].
```

An image model may still redraw protected regions. “Preserve” is a semantic
instruction, not a promise of identical pixels. Inspect each output and use
deterministic compositing only when exact retention is required and that
capability is available.

## Translate text while preserving layout

Use the actual infographic or poster as the sole base. Supply the destination
language and, when accuracy matters, the approved translations rather than
asking the image model to author them.

```text
Reference 1 is the base infographic.
Replace only its English labels with the approved French strings listed below,
matching each source label one-to-one. Preserve canvas size, section positions,
icons, arrows, colors, type hierarchy, spacing, and all non-text pixels as closely
as possible. Keep product names unchanged. Remove every superseded English label;
do not add commentary or new symbols.

[source label] -> [approved French label]
...
```

Afterward, check every label, accents, leftover source-language fragments,
line breaks, overflow, arrows, and layout. “Do not change anything else” helps
but does not prove unchanged pixels. If layout or wording must be exact, use a
deterministic text-overlay workflow when available.

## Transfer style without importing the old subject

Define which visual attributes the style reference contributes and which it must
not contribute.

```text
Reference 1 controls visual style only: its limited palette, chunky pixel scale,
hard-edged shadows, and sparse off-white background.
Create a new subject: a courier riding a step-through bicycle, side view, full
bicycle visible. Preserve the reference's rendering language but do not copy its
characters, objects, layout, lettering, or background landmarks. No text.
```

“Same style” is often underspecified. Palette, mark-making, texture, medium,
contrast, line quality, and background treatment are useful independent axes.

## Preserve identity while changing clothing

Identity edits require a precise preserve list and clear garment roles. Use a
person photograph only when it is actually available and allowed by the request.

```text
Reference 1 is the base person and composition.
Reference 2 provides the jacket construction and fabric.
Reference 3 provides the trousers and shoes.

Change only the clothing on the person in Reference 1. Preserve facial structure,
skin tone, expression, eyes, hairline, hairstyle, age, body shape, proportions,
pose, hand position, and identity. Fit the referenced garments naturally to the
existing body and pose with believable seams, folds, occlusion, and fabric weight.
Match the base photograph's light direction, color temperature, grain, and
shadows. Preserve background, crop, camera angle, and image quality. Do not import
the clothing-reference models, poses, backgrounds, accessories, labels, logos,
or text.
```

Inspect likeness, face geometry, hands, body proportions, garment details,
background drift, and lighting. If exact identity preservation is a hard
requirement, disclose that prompting provides no deterministic guarantee.

## Combine a subject and a scene

Name the source and destination explicitly. Tell the model which image owns the
composition and lighting.

```text
Reference 1 is the base garden scene; preserve its framing, plants, bench, and
late-afternoon light. Reference 2 supplies only the small black-and-tan dog.
Place that dog on the paving beside the seated gardener, correctly scaled and
facing toward them. Preserve the dog's coat pattern, ears, muzzle, and body
proportions. Match the scene perspective, warm rim light, ground contact shadow,
and depth of field. Do not copy Reference 2's floor or background. Change nothing
else in Reference 1 and add no collar text or accessories.
```

Check scale, grounding, occlusion, gaze, edge quality, and whether either source
background leaked into the result.

## Isolate a product on transparent background

Use the observed product photograph as the base. Request isolation both in prose
and through Kimi's `background: transparent` hint.

```text
Extract only the product from Reference 1 onto a fully transparent background.
Center it with generous padding and a crisp natural silhouette. Preserve its
geometry, cap shape, printed label, color, gloss, and all identifying details.
Remove the environment and its cast shadow. Allow only subtle cleanup of edge
contamination; do not redesign, relight, smooth, or restyle the product. No white
or colored backdrop, scenery, checkerboard pattern, halo, fringe, added shadow,
text, or watermark.
```

Kimi returns PNG, but a white canvas or painted checkerboard can still be opaque.
Do not claim transparent alpha from filename, requested metadata, or provider
labels. Inspect the decoded alpha channel only with an available capable tool;
otherwise state that transparency must be checked.

## Turn a sketch into a realistic scene

Treat the sketch as structural authority, then specify plausible realism without
granting permission to redesign.

```text
Reference 1 is the approved sketch and controls layout.
Render it as a photorealistic small reading pavilion. Preserve the exact camera
view, footprint, roof pitch, opening positions, object count, relative scale, and
perspective. Interpret the indicated surfaces as weathered cedar, clear glass,
matte black steel, and pale concrete; use soft overcast daylight and physically
believable contact shadows. Do not add landscaping, people, furniture, signs,
copy, or architectural features absent from the sketch.
```

Compare silhouette, vanishing lines, positions, counts, and negative space—not
only whether the new materials look realistic.

## Remove one object locally

Name exactly one target and state what should plausibly fill the vacated area.

```text
Remove only the red paper cup from Reference 1. Reconstruct the table surface and
the small portion of wall behind it using the surrounding texture and light.
Preserve the person's face, pose, hands, clothing, every other table object,
crop, perspective, focus, colors, and shadows. Do not add a replacement object,
change the composition, or retouch the person.
```

Inspect the repaired boundary, repeated textures, stray fragments, hands,
reflections, shadows, and unrelated changes. A very short “remove X; change
nothing else” instruction is appropriate for simple cases, but the preserve list
helps when nearby details are fragile.

## Insert a person into a new scene

Define identity preservation, full-body framing, action, gaze, environmental
contact, and desired realism. Avoid granting broad restyling through vague
cinematic language.

```text
Reference 1 supplies the person's identity only.
Create a realistic dusk photograph of this person hurrying away from a rain-soaked
campsite while a fallen branch has collapsed one tent behind them. Show the full
body with both feet visible, body turned in motion, eyes looking toward the safe
trail rather than the camera, one hand holding a flashlight. Preserve facial
structure, skin tone, age, hairstyle, and body proportions. Dress them in plain
weatherproof camping clothes. Use believable wet fabric, mud, foot contact,
natural gray-blue dusk, and restrained documentary color. Do not copy the source
background, alter identity, exaggerate into poster art, or add text/logos.
```

Inspect the face, proportions, hands, feet, gaze, interaction, lighting, and
whether the result looks grounded rather than composited. State safety-relevant
action clearly when the scene contains hazards.

## Replace furniture surgically

The room photograph should own everything except the named furniture.

```text
Reference 1 is the base room photograph.
Replace only the two white dining chairs with simple oak spindle-back chairs.
Keep the chair count, positions, scale, and orientation. Match the existing
camera perspective, warm window light, floor contact shadows, occlusion behind
the table, and photographic grain. Preserve walls, floor, table, rug, windows,
plants, decorations, crop, exposure, and every other object. Do not redesign the
room or add new decor.
```

Check that both and only those chairs changed, legs meet the floor, occlusion is
correct, shadows agree with the windows, and room geometry did not warp.

## Handling more than two references

Keep roles orthogonal where possible. A useful ordering is base composition,
identity/object, clothing/shape, then palette/style. State conflicts explicitly:
if the base scene controls lighting but a product photo has studio light, tell the
model to preserve product geometry while adapting its illumination to the scene.

Do not include references merely because they are attached. Select only those
needed for the task, within the operator's configured maximum. If two uploads
share a filename, use their observed saved workspace paths. Avoid pronouns such
as “it” when several subjects exist; use “the dog from Reference 2” or “the red
chair in Reference 1.”

Before the call, validate the role map:

- Each numbered reference corresponds to one observed selected input.
- One reference clearly owns the base layout.
- Every transferred feature has one source and a destination.
- Preserve constraints cover identity, geometry, lighting, and labels that matter.
- Prohibited carryover—backgrounds, source people, props, or text—is named.
- The requested operation can fit in one output; unrelated changes are deferred.
