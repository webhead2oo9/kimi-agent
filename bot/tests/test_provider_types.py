from providers.types import (
    ContentPart,
    ContentPartType,
)


def test_text_and_image_parts_round_trip_as_plain_data() -> None:
    text = ContentPart.from_text("describe this")
    image = ContentPart.from_image_url(
        url="data:image/png;base64,abc123",
        media_type="image/png",
        detail="auto",
    )

    assert text.type is ContentPartType.TEXT
    assert text.text == "describe this"
    assert image.type is ContentPartType.IMAGE
    assert image.image_url == "data:image/png;base64,abc123"
    assert image.media_type == "image/png"
    assert image.detail == "auto"
