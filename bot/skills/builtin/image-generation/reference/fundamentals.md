# Image prompting fundamentals

Adapted for Bram from OpenAI's
[Image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting).
The source's prompting principles are retained here while its broader API
parameter examples are narrowed to Bram's actual `generate_image` contract.

## Begin with the deliverable

State what the image is for before listing decoration. “A product listing hero
image,” “a high-school biology handout,” and “a four-panel social comic” imply
different framing, density, typography, and finish. A useful brief usually has
these parts:

1. **Deliverable and audience:** what artifact should exist and who will read or
   use it.
2. **Canvas and composition:** portrait, landscape, or square; camera distance;
   hierarchy; subject placement; empty space reserved for copy.
3. **Subject and action:** who or what is visible, body framing, gaze, gesture,
   interaction, and relative scale.
4. **Visible treatment:** medium, materials, surface texture, light direction,
   palette, atmosphere, and degree of realism.
5. **Required content:** exact wording, labels, quantities, objects, or factual
   relationships.
6. **Constraints:** exclusions and, for edits, everything that must remain
   unchanged.

Labeled sections make long briefs maintainable, but they are not magic syntax.
A compact paragraph can work just as well. Choose the structure that makes it
easy to notice contradictions and update one requirement.

### Reusable brief skeleton

```text
Deliverable: [artifact and use]
Audience: [viewer]
Canvas/composition: [orientation, framing, placement, hierarchy]
Subject/action: [visible subject, pose, interaction]
Appearance: [medium, materials, light, color, texture]
Required content: [exact text, labels, objects, data]
Preserve (edits): [identity, geometry, layout, lighting, background, etc.]
Change only (edits): [bounded change]
Exclude: [extra text, marks, scenery, accessories, visual treatments]
```

Do not pad a prompt with adjectives that do no visible work. “Premium” becomes
actionable when paired with clean kerning, controlled reflections, fine material
texture, restrained color, and deliberate spacing. “Cinematic” becomes
actionable through scale, lens-like framing, time of day, contrast, haze, rain,
or motivated light. Conversely, if the user wants an ordinary documentary
photograph, explicitly reject poster-like grading and excessive retouching.

## Make visible details concrete

If realism matters, say “photorealistic” or “a real photograph” and describe
evidence of reality: pores, wear, fibers, uneven reflections, contact shadows,
weather, or small environmental imperfections. Camera and film terminology is a
visual cue, not a guarantee of physical optics. Use it to communicate framing
and texture—eye-level medium shot, shallow depth of field, subtle grain—without
claiming an exact lens simulation.

For people, mention the part of the body that must be in frame and the action in
physical terms. Examples include “full figure with both feet visible,” “eyes on
the open notebook,” or “both hands naturally holding the handlebars.” State
relative placement when several people or objects interact. If an edit must
preserve a person, enumerate identity-bearing features instead of relying on
“same person.”

For scenes, distinguish subject from environment and specify their relationship.
“Small hiker in the lower-left against a vast foggy ridge” controls scale more
clearly than “epic mountain scene.” For layouts, name whitespace and reading
order. For objects, name material, geometry, label location, wear, and shadow
behavior.

## Exact text needs its own specification

Put required text in quotation marks and state where it appears, how many times,
and whether any other text is allowed. Unusual names can be spelled character by
character in addition to the quoted form. Describe typography in visual terms:
weight, case, family class, contrast, alignment, tracking, and size hierarchy.

```text
Headline (exactly once): "MAKE ROOM FOR MORNING"
Position: upper-left in the reserved negative space
Typography: bold condensed sans serif, all caps, high contrast, generous tracking
No other words, captions, labels, signatures, watermarks, or logos.
```

After generation, verify spelling, punctuation, duplication, omission, and
legibility at the intended display size. Dense copy, tiny labels, and mixed font
systems are higher-risk. A `medium` or `high` quality hint may help, but it does
not replace inspection. If exact text is mission-critical and a deterministic
text-layout or compositing tool is actually available, generate the visual base
and add the approved text deterministically. Do not claim to have done so when
that tool is unavailable.

## Separate edits into change and preserve sets

An edit prompt should answer two different questions:

- What is allowed to change?
- What evidence proves the rest remained stable?

Use “change only…” for a local operation, then list preserved identity,
geometry, layout, camera angle, crop, light, shadows, color balance, labels,
surrounding objects, and background as applicable. Also name subtle properties
that image edits often disturb: saturation, contrast, arrow direction, hand
pose, reflections, typography, and edge detail.

Repeated edits can drift even when preservation was stated previously. Restate
critical constraints on every call. Prompting is not a pixel lock. When a region
must remain byte- or pixel-identical and compositing tools are available, merge
the approved changed region into the original deterministically. Otherwise be
honest that semantic preservation was requested but exact pixel preservation
was not proven.

## Give every reference a role

Never say “use the references” and leave the mapping implicit. Use the order of
the observed `reference_paths` or `reference_attachments` and assign roles:

```text
Reference 1: base scene; preserve its crop, architecture, and lighting.
Reference 2: subject identity; preserve face, coat markings, and proportions.
Reference 3: garment construction only; transfer its cut and fabric, not its model.
Change: place the subject from reference 2 into reference 1 wearing the garment
from reference 3. Match the base scene's perspective, contact shadows, and color
temperature. Do not import either reference background.
```

Only describe inputs that were actually observed. If the task depends on an
unavailable original, product photograph, or prior output, ask the user to
provide it rather than imagining one. Refer to the exact saved path or filename
in the tool call, and keep the prose roles aligned with that order.

## Choose sensible Bram hints

Composition should drive size. Use `1024x1536` for posters, cards, people, and
vertical explainers; `1536x1024` for rooms, landscapes, slides, wide diagrams,
and interface boards; `1024x1024` for centered marks, icons, and square social
assets. Use `auto` when orientation is not important. These are provider hints;
the tool's `actual.size` reports the decoded result.

Start with `quality: auto` unless small text, dense labels, fine identity detail,
or the user's bar for finish suggests `medium` or `high`. Do not reflexively use
the highest setting, and do not promise that a label maps to a fixed visual or
latency level across operator-selected models.

Use `background: transparent` only when the requested asset needs isolation, and
also state “fully transparent background” in the prompt. Ask for a crisp
silhouette and prohibit a solid backdrop, scenery, and painted checkerboard.
Bram still saves PNG, but PNG alone does not prove an alpha channel. The tool
does not inspect actual alpha; verify it only with an image-analysis facility
that can truly read the returned file, or tell the user it remains to be checked.

## Factual and instructional visuals

Image generation can arrange supplied facts attractively; it is not a source of
truth. For infographics, scientific diagrams, charts, maps, timelines, and
historical scenes, give the verified facts directly and check both labels and
relationships. Confirm arrow direction, ordering, units, axes, legend mapping,
dates, attire, artifacts, and geography as applicable.

Never reuse sample statistics or citations as if they were real. Obtain and
verify current data before putting it in a slide. If exact numerical geometry
matters—a bar whose height must encode 37 rather than merely look plausible—and
a plotting or rendering tool is available, create the chart deterministically.
Image generation may still supply a surrounding illustration or visual style.
If deterministic tooling is not available, frame the result as a concept image
and do not claim data-accurate plotting.

## Act without needless questions

When the user says “make a friendly square icon of a sleepy moon,” a sensible
first pass can choose centered composition, simple shapes, dark blue and warm
cream, no text, and `1024x1024`. Ask when the choice carries user intent rather
than routine craft: exact brand copy, whose identity to use, which of several
uploaded images is the base, whether an asset must be transparent, or which
verified values belong in a chart.

Generate one result unless the user asks for variations. A tool budget is an
upper bound, not a target. Inspect the returned result when possible before
choosing a revision; otherwise return it and state what still needs human visual
verification.
