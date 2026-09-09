---
name: image-generation
description: "Plan, generate, edit, inspect, and refine images with strong visual briefs, explicit reference roles, preservation constraints, and honest verification through the optional image tool."
tags: [images, generation, editing, prompting, references, verification]
---

# Image generation

Use this skill when the user wants a new raster image or an edit to an existing
one. Start from the result they need, then turn it into a concrete visual brief.
Do not make the user specify routine art-direction details when sensible defaults
follow from the request. Ask only when a missing decision would materially change
the result, or when an edit depends on an image that is not actually available.

For a new image, specify the subject, intended use, composition, visible details,
medium, lighting, required text, and constraints that matter. For an edit, make
the change set and preservation set explicit. When there are several references,
number them and give each one a role; never invent or imply an input image that
was not observed in the conversation or workspace.

Read the reference file that fits the request:

- `reference/fundamentals.md` for briefs, defaults, exact text, factual content,
  people, composition, and settings;
- `reference/generation-recipes.md` for photographs, infographics, logos,
  history, comics, interfaces, scientific visuals, slides, cards, and packaging;
- `reference/editing-and-references.md` for translation, identity or clothing,
  style transfer, combined references, cutouts, reconstruction, removal,
  insertion, and furniture changes;
- `reference/iteration-and-verification.md` for output inspection, narrow
  revisions, billboard sequences, character consistency, alpha checks, and
  deterministic fallbacks.

## Kimi tool contract

`generate_image` is optional. If it is absent, say image generation is not
enabled here; do not pretend to call a provider. For generation, omit reference
fields. For editing, use only observed workspace-relative `reference_paths` or
exact current-message filenames in `reference_attachments`. A successful call
returns one reusable PNG path and queues that PNG for the reply. Supply both
`prompt` and a concise, accessible `attachment_description` describing the
intended visual; do not present that description as proof of inspecting the output.

The deployment operator selects the model; there is no per-call model choice.
The current operator choices are `gpt-image-2` (the shipped default),
`gpt-image-2.5-flare`, and `gpt-image-2.5-sunburst`.
The only call-level rendering hints are `size` (`auto`, `1024x1024`,
`1024x1536`, or `1536x1024`), `quality` (`auto`, `low`, `medium`, or `high`),
and `background` (`auto`, `opaque`, or `transparent`). They are best-effort
hints, not promises. Do not pass other Images API fields.

Read `requested`, `actual`, `provider_reported`, and `mismatches` in the tool
result. Decoded dimensions and PNG format are observable facts; provider labels
and an empty mismatch set do not prove visual quality, fidelity, or real alpha.
Inspect the actual returned image when an available tool truly exposes it to the
model. Otherwise describe only verified metadata and ask the user to inspect
visual details when necessary.

The normal budgets are unchanged: two image calls per turn by default, with
operator-configurable call, reference, and attachment caps. A weak result does
not authorize paid retry loops: make one focused revision when the user asked
for refinement and budget permits, but ask before spending calls on unrequested
variations.

The prompting methods and adapted patterns in the reference files are attributed
to OpenAI's [Image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting).
