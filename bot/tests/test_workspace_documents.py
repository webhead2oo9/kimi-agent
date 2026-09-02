"""extract_document_text: PDF and Office conversion."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from tools.workspace import documents as workspace_documents
from tools.workspace import (
    WorkspaceToolConfig,
)

from tests.workspace_tool_helpers import (
    WS,
    _make_ctx,
    _register,
    _write_docx,
    _write_pdf,
)


@pytest.mark.asyncio
async def test_extract_document_text_saves_pdf_text_for_read_file(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    pdf_path = mgr.user_files_dir(WS) / "papers" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    _write_pdf(
        pdf_path,
        [
            "First page introduction about PRISMA.",
            "Second page methods and eligibility criteria.",
        ],
    )

    result = await reg.dispatch(
        "extract_document_text",
        {"path": "papers/paper.pdf"},
        ctx,
    )

    parsed = json.loads(result)
    entry = next(item for item in reg.get_all_tools() if item.name == "extract_document_text")
    extracted = mgr.user_files_dir(WS) / "papers" / "paper.extracted.txt"
    assert parsed["source_path"] == "papers/paper.pdf"
    assert parsed["path"] == "papers/paper.extracted.txt"
    assert parsed["page_count"] == 2
    assert parsed["pages_extracted"] == 2
    assert parsed["context_is_untrusted"] is True
    assert entry.untrusted is True
    assert parsed["read_next"] == {
        "tool": "read_file",
        "args": {
            "path": "papers/paper.extracted.txt",
            "offset": 1,
            "limit": 1000,
        },
    }
    assert "First page introduction" in parsed["excerpt"]
    assert "Second page methods" in extracted.read_text(encoding="utf-8")
    assert ctx.outbox.output_files == ()


@pytest.mark.asyncio
async def test_extract_document_text_rejects_pdf_above_page_limit(
    tmp_path: Path,
) -> None:
    config = WorkspaceToolConfig(max_pdf_pages=2)
    reg, mgr = _register(tmp_path, config)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    pdf_path = mgr.user_files_dir(WS) / "too-many-pages.pdf"
    _write_pdf(pdf_path, ["one", "two", "three"])

    result = json.loads(
        await reg.dispatch(
            "extract_document_text",
            {"path": "too-many-pages.pdf"},
            ctx,
        )
    )

    assert result["error"] == "PDF is too complex (limit 2 pages; found 3)"
    assert not (mgr.user_files_dir(WS) / "too-many-pages.extracted.txt").exists()


@pytest.mark.asyncio
async def test_extract_document_text_stops_loading_pdf_pages_at_output_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = WorkspaceToolConfig(
        max_read_bytes=256,
        max_file_bytes=10_000,
        max_pdf_pages=20,
    )
    reg, mgr = _register(tmp_path, config)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    pdf_path = mgr.user_files_dir(WS) / "large.pdf"
    pdf_path.write_bytes(b"placeholder")
    loaded_pages: list[int] = []

    class FakePage:
        def get_text(self, mode: str) -> str:
            assert mode == "text"
            return "x" * 200

    class FakeDocument:
        page_count = 20

        def load_page(self, page_index: int) -> FakePage:
            loaded_pages.append(page_index)
            return FakePage()

        def close(self) -> None:
            return None

    monkeypatch.setattr(workspace_documents.pymupdf, "open", lambda _path: FakeDocument())

    result = json.loads(await reg.dispatch("extract_document_text", {"path": "large.pdf"}, ctx))
    extracted = mgr.user_files_dir(WS) / "large.extracted.txt"

    assert result["truncated"] is True
    assert result["page_count"] == 20
    assert result["pages_extracted"] == len(loaded_pages)
    assert 0 < len(loaded_pages) < result["page_count"]
    assert extracted.stat().st_size <= config.max_read_bytes
    assert "[TRUNCATED]" in extracted.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_extract_document_text_converts_docx_to_markdown(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    docx_path = mgr.user_files_dir(WS) / "docs" / "report.docx"
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    _write_docx(docx_path, ["Introduction heading", "Body paragraph text."])

    result = await reg.dispatch(
        "extract_document_text",
        {"path": "docs/report.docx"},
        ctx,
    )

    parsed = json.loads(result)
    converted = mgr.user_files_dir(WS) / "docs" / "report.converted.md"
    assert parsed["source_path"] == "docs/report.docx"
    assert parsed["path"] == "docs/report.converted.md"
    assert parsed["format"] == "docx"
    assert parsed["context_is_untrusted"] is True
    assert "Introduction heading" in parsed["excerpt"]
    assert "Body paragraph text." in converted.read_text(encoding="utf-8")
    assert parsed["read_next"]["tool"] == "read_file"
    assert parsed["read_next"]["args"]["path"] == "docs/report.converted.md"


@pytest.mark.asyncio
async def test_extract_document_text_converts_csv_to_markdown(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    csv_path = mgr.user_files_dir(WS) / "data.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")

    result = await reg.dispatch(
        "extract_document_text",
        {"path": "data.csv"},
        ctx,
    )

    parsed = json.loads(result)
    assert parsed["source_path"] == "data.csv"
    assert parsed["path"] == "data.converted.md"
    assert parsed["format"] == "csv"
    assert "alpha" in parsed["excerpt"]
    assert "|" in parsed["excerpt"]


@pytest.mark.asyncio
async def test_extract_document_text_rejects_delimiter_heavy_csv_before_conversion(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    csv_path = mgr.user_files_dir(WS) / "cell-bomb.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("," * workspace_documents.MAX_CSV_CELLS, encoding="utf-8")

    result = json.loads(await reg.dispatch("extract_document_text", {"path": "cell-bomb.csv"}, ctx))

    assert "too complex" in result["error"]
    assert "cells" in result["error"]
    assert not (mgr.user_files_dir(WS) / "cell-bomb.converted.md").exists()


@pytest.mark.asyncio
async def test_extract_document_text_serializes_office_conversions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    first = _make_ctx()
    second = _make_ctx()
    second.user_id = "user456"
    for ctx in (first, second):
        ctx.activated_tools.add("extract_document_text")
        source = mgr.user_files_dir(ctx.workspace_key) / "report.docx"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"placeholder")

    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def fake_convert(path: Path, max_output_bytes: int) -> workspace_documents.OfficeConversion:
        nonlocal active, peak_active
        assert max_output_bytes > 0
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return workspace_documents.OfficeConversion(markdown=str(path.name), format="docx")

    monkeypatch.setattr(workspace_documents, "_convert_office_document", fake_convert)

    results = await asyncio.gather(
        reg.dispatch("extract_document_text", {"path": "report.docx"}, first),
        reg.dispatch("extract_document_text", {"path": "report.docx"}, second),
    )

    assert all("error" not in json.loads(result) for result in results)
    assert peak_active == 1


@pytest.mark.asyncio
async def test_extract_document_text_serializes_pdf_and_office_parsers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    pdf_ctx = _make_ctx()
    office_ctx = _make_ctx()
    office_ctx.user_id = "user456"
    for ctx, filename in ((pdf_ctx, "paper.pdf"), (office_ctx, "report.docx")):
        ctx.activated_tools.add("extract_document_text")
        source = mgr.user_files_dir(ctx.workspace_key) / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"placeholder")

    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def enter_parser() -> None:
        nonlocal active, peak_active
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1

    def fake_extract_pdf(
        path: Path,
        display_path: str,
        *,
        max_pages: int,
        max_output_bytes: int,
    ) -> workspace_documents.PdfTextExtraction:
        assert path.name == display_path == "paper.pdf"
        assert max_pages > 0
        assert max_output_bytes > 0
        enter_parser()
        return workspace_documents.PdfTextExtraction(
            page_count=1,
            pages_extracted=1,
            text="pdf",
        )

    def fake_convert(path: Path, max_output_bytes: int) -> workspace_documents.OfficeConversion:
        assert path.name == "report.docx"
        assert max_output_bytes > 0
        enter_parser()
        return workspace_documents.OfficeConversion(markdown="office", format="docx")

    monkeypatch.setattr(workspace_documents, "_extract_pdf_text", fake_extract_pdf)
    monkeypatch.setattr(workspace_documents, "_convert_office_document", fake_convert)

    results = await asyncio.gather(
        reg.dispatch("extract_document_text", {"path": "paper.pdf"}, pdf_ctx),
        reg.dispatch("extract_document_text", {"path": "report.docx"}, office_ctx),
    )

    assert all("error" not in json.loads(result) for result in results)
    assert peak_active == 1


@pytest.mark.asyncio
async def test_cancelled_dispatch_holds_parser_slot_until_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    pdf_ctx = _make_ctx()
    office_ctx = _make_ctx()
    office_ctx.user_id = "user456"
    for ctx, filename in ((pdf_ctx, "paper.pdf"), (office_ctx, "report.docx")):
        ctx.activated_tools.add("extract_document_text")
        source = mgr.user_files_dir(ctx.workspace_key) / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"placeholder")

    state_lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    active = 0
    peak_active = 0

    def fake_extract_pdf(
        path: Path,
        display_path: str,
        *,
        max_pages: int,
        max_output_bytes: int,
    ) -> workspace_documents.PdfTextExtraction:
        nonlocal active, peak_active
        assert path.name == display_path == "paper.pdf"
        assert max_pages > 0
        assert max_output_bytes > 0
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        first_started.set()
        try:
            release_first.wait(timeout=2.0)
        finally:
            with state_lock:
                active -= 1
        return workspace_documents.PdfTextExtraction(
            page_count=1,
            pages_extracted=1,
            text="pdf",
        )

    def fake_convert(path: Path, max_output_bytes: int) -> workspace_documents.OfficeConversion:
        nonlocal active, peak_active
        assert path.name == "report.docx"
        assert max_output_bytes > 0
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        second_started.set()
        with state_lock:
            active -= 1
        return workspace_documents.OfficeConversion(markdown="office", format="docx")

    monkeypatch.setattr(workspace_documents, "_extract_pdf_text", fake_extract_pdf)
    monkeypatch.setattr(workspace_documents, "_convert_office_document", fake_convert)

    first_dispatch = asyncio.create_task(
        reg.dispatch("extract_document_text", {"path": "paper.pdf"}, pdf_ctx)
    )
    second_dispatch: asyncio.Task[str] | None = None
    try:
        assert await asyncio.to_thread(first_started.wait, 1.0)
        first_dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_dispatch

        second_dispatch = asyncio.create_task(
            reg.dispatch(
                "extract_document_text",
                {"path": "report.docx"},
                office_ctx,
            )
        )
        await asyncio.sleep(0.05)

        assert not second_started.is_set()
        assert not second_dispatch.done()

        release_first.set()
        result = json.loads(await asyncio.wait_for(second_dispatch, timeout=1.0))

        assert "error" not in result
        assert second_started.is_set()
        assert peak_active == 1
    finally:
        release_first.set()
        if not first_dispatch.done():
            first_dispatch.cancel()
        pending = [task for task in (first_dispatch, second_dispatch) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_extract_document_text_caps_output_at_read_file_limit(
    tmp_path: Path,
) -> None:
    config = WorkspaceToolConfig(max_read_bytes=64, max_file_bytes=10_000)
    reg, mgr = _register(tmp_path, config)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    csv_path = mgr.user_files_dir(WS) / "large.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("name,value\n" + "alpha,12345\n" * 20, encoding="utf-8")

    converted_result = json.loads(
        await reg.dispatch("extract_document_text", {"path": "large.csv"}, ctx)
    )
    converted = mgr.user_files_dir(WS) / "large.converted.md"
    read_result = await reg.dispatch("read_file", {"path": "large.converted.md"}, ctx)

    assert converted_result["truncated"] is True
    assert converted.stat().st_size <= config.max_read_bytes
    assert "exceeds read limit" not in read_result
    assert "[TRUNCATED]" in read_result


@pytest.mark.asyncio
async def test_extract_document_text_enforces_workspace_entry_limit(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_workspace_entries=1))
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    csv_path = mgr.user_files_dir(WS) / "data.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")

    result = json.loads(await reg.dispatch("extract_document_text", {"path": "data.csv"}, ctx))

    assert "too many files" in result["error"]
    assert not (mgr.user_files_dir(WS) / "data.converted.md").exists()


@pytest.mark.asyncio
async def test_extract_document_text_rejects_unsupported_format(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    bad_path = mgr.user_files_dir(WS) / "image.png"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"\x89PNG\r\n\x1a\n fake image data")

    result = await reg.dispatch(
        "extract_document_text",
        {"path": "image.png"},
        ctx,
    )

    parsed = json.loads(result)
    assert "error" in parsed
    assert "unsupported" in parsed["error"]


@pytest.mark.asyncio
async def test_extract_document_text_reports_malformed_office_doc(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_document_text")
    bad_docx = mgr.user_files_dir(WS) / "broken.docx"
    bad_docx.parent.mkdir(parents=True, exist_ok=True)
    bad_docx.write_bytes(b"not a real docx file")

    result = await reg.dispatch(
        "extract_document_text",
        {"path": "broken.docx"},
        ctx,
    )

    parsed = json.loads(result)
    assert "error" in parsed
    assert "malformed" in parsed["error"]
