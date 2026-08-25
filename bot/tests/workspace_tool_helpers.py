"""Shared setup for the tools/workspace/ test modules.

Split out of the single 2,700-line test_workspace_tools.py so each module's
tests sit beside a name that says which module they cover.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pymupdf
import pytest

from agent.attachments import AttachmentRef
from workspace import WorkspaceManager, workspace_owner_key
from tools.registry import MessageContext, ToolRegistry
from tools.workspace import (
    WorkspaceToolConfig,
    init_workspace_tools,
)
from trust.tiers import TrustTier

# Workspaces are keyed per (user, guild); _make_ctx uses user "user123" in guild
# "g1", so files live under this composite owner key, not the bare user id.
WS = workspace_owner_key("user123", "g1")


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.touch()
        try:
            os.symlink(target, Path(tmp) / "link")
        except OSError:
            return False
    return True


# Windows only allows symlink creation with admin rights or Developer Mode; the
# symlink-refusal tests need to *create* links to prove the tools reject them.
_requires_symlinks = pytest.mark.skipif(
    not _can_symlink(), reason="symlink creation unavailable on this host"
)


def _make_ctx(tier: TrustTier = TrustTier.MEMBER) -> MessageContext:
    return MessageContext(
        user_id="user123",
        user_name="test",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=tier,
        context_key="g1:c1:main",
    )


def _register(
    tmp_path: Path, config: WorkspaceToolConfig | None = None
) -> tuple[ToolRegistry, WorkspaceManager]:
    reg = ToolRegistry()
    mgr = WorkspaceManager(base_dir=tmp_path)
    init_workspace_tools(reg, mgr, config=config or WorkspaceToolConfig())
    return reg, mgr


def _write_pdf(path: Path, pages: list[str]) -> None:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    """Write a minimal valid .docx (OOXML) with the given paragraph text."""
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        "<Relationships xmlns="
        '"http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="word/document.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


class _FakeImport:
    def __init__(self, payload: bytes, *, declared_size: int | None = None) -> None:
        self._payload = payload
        self.size = declared_size if declared_size is not None else len(payload)
        self.filename = "attachment"
        self.content_type: str | None = None

    async def read(self) -> bytes:
        return self._payload


def _ctx_with_attachments(*refs: AttachmentRef) -> MessageContext:
    ctx = _make_ctx()
    ctx.attachments = list(refs)
    return ctx


def _make_repo_tar(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in {
            "repo-abc/README.md": b"hello",
            "repo-abc/src/app.py": b"print(1)",
        }.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _image_ctx() -> MessageContext:
    ctx = _make_ctx()
    ctx.images_supported = True
    return ctx
