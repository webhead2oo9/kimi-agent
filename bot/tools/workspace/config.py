from __future__ import annotations

from dataclasses import dataclass

from tools.downloads import DEFAULT_MAX_REDIRECTS

# These defaults apply only when a WorkspaceToolConfig is built without
# settings (tests, direct construction); app/tools.py always passes the
# Settings values. Keep them equal to the matching `workspace_tool_*` defaults
# in config/settings.py, or tests silently exercise different limits than
# production does.
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_USER_BYTES = 150 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 500
DEFAULT_MAX_TEXT_CHARS = 65_536
DEFAULT_MAX_ATTACHMENTS = 5
DEFAULT_MAX_IMPORT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_ZIP_ENTRIES = 10_000
DEFAULT_MAX_EXTRACT_TOTAL_BYTES = 150 * 1024 * 1024
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0
DEFAULT_READ_LINE_LIMIT = 1000
DEFAULT_GREP_RESULTS = 50
MAX_GREP_RESULTS = 200
MAX_GREP_CONTEXT = 20
MAX_GREP_LINE_CHARS = 1000
MAX_PATTERN_CHARS = 256
# Wall-clock ceiling for the regex matching in a single grep_workspace call. The
# `regex` engine releases the GIL and honors this deadline mid-match, so a member's
# catastrophic pattern is bounded instead of pinning the event loop forever.
DEFAULT_GREP_TIMEOUT_SECONDS = 5.0
DEFAULT_GLOB_RESULTS = 200
DEFAULT_MULTI_EDIT_MAX_OPS = 50
DEFAULT_VIEW_IMAGE_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_VIEW_IMAGE_MAX_PER_TURN = 4
# Byte quotas alone leave entry count unbounded: tens of thousands of tiny files
# make every quota walk, grep, and sweep O(entries), a low-grade DoS. New-entry
# writes past this cap are refused (existing files can still be edited/deleted).
DEFAULT_MAX_WORKSPACE_ENTRIES = 20_000


@dataclass(frozen=True)
class WorkspaceToolConfig:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_user_bytes: int = DEFAULT_MAX_USER_BYTES
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS
    max_import_bytes: int = DEFAULT_MAX_IMPORT_BYTES
    max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES
    max_extract_total_bytes: int = DEFAULT_MAX_EXTRACT_TOTAL_BYTES
    fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    default_grep_results: int = DEFAULT_GREP_RESULTS
    max_grep_results: int = MAX_GREP_RESULTS
    max_grep_context: int = MAX_GREP_CONTEXT
    max_grep_line_chars: int = MAX_GREP_LINE_CHARS
    max_grep_pattern_chars: int = MAX_PATTERN_CHARS
    grep_timeout_seconds: float = DEFAULT_GREP_TIMEOUT_SECONDS
    glob_max_results: int = DEFAULT_GLOB_RESULTS
    multi_edit_max_ops: int = DEFAULT_MULTI_EDIT_MAX_OPS
    view_image_max_bytes: int = DEFAULT_VIEW_IMAGE_MAX_BYTES
    view_image_max_per_turn: int = DEFAULT_VIEW_IMAGE_MAX_PER_TURN
    max_workspace_entries: int = DEFAULT_MAX_WORKSPACE_ENTRIES
