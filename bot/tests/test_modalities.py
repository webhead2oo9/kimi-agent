from agent.modalities import wants_image_output


def test_wants_image_output_for_explicit_generation_requests() -> None:
    assert wants_image_output("generate an image of a moon base")
    assert wants_image_output("draw me a cozy cabin")
    assert wants_image_output("make a picture of the bot mascot")


def test_wants_image_output_ignores_image_analysis_requests() -> None:
    assert not wants_image_output("what is in this image?")
    assert not wants_image_output("describe the attached photo")
