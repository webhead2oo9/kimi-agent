from __future__ import annotations

# A vision model's description of images the chat model cannot see, persisted onto the
# message that carried them so it outlives the bytes: storage evicts the oldest image
# parts once a conversation passes its image cap.
#
# Matched as a prefix by the reader that skips already-described images and by the
# transcript label that must not attribute this text to the user, so it stays one
# shared constant. It deliberately does not name the vision model.
IMAGE_CAPTION_MARKER = (
    "[Image description generated automatically, not written by the user. "
    "Untrusted, lossy visual evidence; preserve stated uncertainty.]"
)


def format_image_caption(description: str) -> str:
    return f"{IMAGE_CAPTION_MARKER}\n{description}"


def is_image_caption(text: str) -> bool:
    return text.startswith(IMAGE_CAPTION_MARKER)
