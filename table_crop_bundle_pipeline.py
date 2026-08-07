#!/usr/bin/env python3


from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable, Sequence

import pdfplumber
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont


PIPELINE_VERSION = "9.0.0"


# ---------------------------------------------------------------------------
# Manual overrides
# ---------------------------------------------------------------------------

TITLE_MATCH_INDEX_OVERRIDES: dict[tuple[int, str, int], int] = {}
TITLE_BBOX_OVERRIDES: dict[tuple[int, str, int], list[float]] = {}
TABLE_BBOX_OVERRIDES: dict[tuple[int, str, int], list[float]] = {}
SKIP_TABLES: set[tuple[int, str, int]] = set()
DISABLE_AUTO_WIDTH_EXPANSION: set[tuple[int, str, int]] = set()
DISABLE_PRE_GUIDE_FULL_WIDTH: set[tuple[int, str, int]] = set()

# Per-page stable lane overrides. Page keys are zero-based.
# Example:
# COLUMN_LANE_OVERRIDES = {
#     0: [[36, 295], [295, 529], [529, 731], [731, 991]],
# }
COLUMN_LANE_OVERRIDES: dict[int, list[list[float]]] = {}


@dataclass
class PipelineConfig:
    target_dpi: int = 250
    pdf_coordinate_dpi: int = 72
    line_y_tolerance_points: float = 3.0
    horizontal_gap_split_points: float = 10.0
    min_text_segments_on_titled_page: int = 3
    use_text_cache: bool = True
    force_text_cache: bool = False
    crop_padding_pixels: int = 3

    # Minimum meaningful horizontal overlap used when one lower title closes a table.
    table_min_x_overlap_pixels: float = 5.0
    table_min_x_overlap_ratio: float = 0.05

    # Text-to-cell matching.
    min_text_coverage_for_cell_match: float = 0.35

    # Stable column-lane detection.
    auto_expand_title_width: bool = True
    column_guide_min_titles: int = 2
    column_guide_min_envelope_coverage: float = 0.45
    column_guide_min_center_span: float = 0.25
    title_row_min_vertical_overlap: float = 0.60
    # Inclusive same-row end markers participate in lane construction and
    # reserve their structural cell from neighboring table starts.
    marker_lane_min_vertical_overlap: float = 0.40
    title_border_tolerance_pixels: float = 3.0
    guide_border_vertical_margin_pixels: float = 15.0

    # Optional final border-line selectors from TXT, for example:
    #     COSTS /// U1 D1 L2 R1
    # Candidate cell-border coordinates are clustered with this tolerance.
    manual_line_coordinate_tolerance_pixels: float = 3.0
    # A candidate border must cover at least this fraction of the opposite
    # dimension of the automatically detected table rectangle. Existing outer
    # bbox edges are always retained as candidates.
    manual_line_min_coverage_ratio: float = 0.20

    # Full-width headers above the main guide row.
    auto_expand_pre_guide_singletons: bool = True
    pre_guide_y_tolerance_pixels: float = 4.0
    pre_guide_singleton_min_width_ratio: float = 0.20

    # Missing titles stop the run unless --allow-missing is used.
    allow_missing_titles: bool = False

    # Create one extra slice per page when text remains below the lowest table.
    # Detection uses the PDF text layer, so blank whitespace does not create a crop.
    extract_trailing_content: bool = True
    trailing_content_min_gap_pixels: float = 1.0
    trailing_content_min_alnum_characters: int = 3
    trailing_content_top_padding_pixels: int = 8
    trailing_content_bottom_padding_pixels: int = 24
    trailing_content_horizontal_padding_pixels: int = 8
    trailing_content_use_page_content_width: bool = True
    # Once residual text is detected, keep the complete page tail so signature
    # rules, stamps, or later text are not accidentally clipped.
    trailing_content_crop_to_page_bottom: bool = True


@dataclass
class TextSegment:
    text: str
    bbox: list[float]
    confidence: float | None = 1.0
    chars: list[Any] = field(default_factory=list)
    words: list[Any] = field(default_factory=list)


@dataclass
class PageText:
    text_segments: list[TextSegment] = field(default_factory=list)
    # Page-level words are preserved so a TXT title can be matched across
    # multiple visual segments. This prevents a short word elsewhere on the
    # page (for example "Depth") from winning over "DEPTH 06:00".
    words: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TitleRequest:
    title: str
    end_marker: str | None = None
    # None for automatic boundaries, "exclusive" for `--`, and "inclusive"
    # for `---` (or any longer hyphen run).
    end_marker_mode: str | None = None
    boundary_separator: str | None = None
    # One-based final border-line selectors parsed from `///`, e.g.
    # {"U": 1, "D": 1, "L": 2, "R": 1}.
    line_selectors: dict[str, int] = field(default_factory=dict)
    raw: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "end_marker": self.end_marker,
            "end_marker_mode": self.end_marker_mode,
            "boundary_separator": self.boundary_separator,
            "line_selectors": dict(self.line_selectors),
            "raw": self.raw or self.title,
        }


PAGE_HEADER_PATTERN = re.compile(
    r"""
    ^\s*
    (?:
        \[\s*page\s*(\d+)\s*\]
        |
        page\s*(\d+)\s*:?
        |
        \#\s*page\s*(\d+)\s*
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

INLINE_TITLE_PATTERN = re.compile(r"^\s*(\d+)\s*(?:\||;|\t)\s*(.+?)\s*$")


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def package_version(package_name: str) -> str:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def require_runtime_dependencies() -> None:
    if shutil.which("pdftoppm") is None:
        raise RuntimeError(
            "Poppler was not found. Install it and make sure `pdftoppm` is on PATH.\n"
            "macOS: brew install poppler\n"
            "Ubuntu/Debian: sudo apt-get install poppler-utils\n"
            "Windows: install a Poppler build and add its bin directory to PATH."
        )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("–", "-").replace("—", "-").replace("−", "-")
    value = re.sub(r"\s+", " ", value)
    return value.strip().casefold()


def compact_text(value: str) -> str:
    value = normalize_text(value)
    return "".join(character for character in value if character.isalnum())


def safe_slug(value: str, limit: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    characters: list[str] = []

    for character in value:
        if character.isalnum():
            characters.append(character)
        elif character in {" ", "_", "-", "."}:
            characters.append("-")

    slug = re.sub(r"-+", "-", "".join(characters)).strip("-.")
    return (slug or "untitled")[:limit]


def bbox_key(bbox: Sequence[float]) -> tuple[int, int, int, int]:
    return tuple(round(float(value)) for value in bbox)  # type: ignore[return-value]


def bbox_area(bbox: Sequence[float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(
        0.0, float(bbox[3]) - float(bbox[1])
    )


def intersection_area(bbox1: Sequence[float], bbox2: Sequence[float]) -> float:
    left = max(float(bbox1[0]), float(bbox2[0]))
    top = max(float(bbox1[1]), float(bbox2[1]))
    right = min(float(bbox1[2]), float(bbox2[2]))
    bottom = min(float(bbox1[3]), float(bbox2[3]))
    return max(0.0, right - left) * max(0.0, bottom - top)


def interval_overlap_width(
    interval1: Sequence[float], interval2: Sequence[float]
) -> float:
    return max(
        0.0,
        min(float(interval1[1]), float(interval2[1]))
        - max(float(interval1[0]), float(interval2[0])),
    )


def bbox_x_interval(bbox: Sequence[float]) -> tuple[float, float]:
    return float(bbox[0]), float(bbox[2])


def horizontal_overlap_width(
    bbox1: Sequence[float], bbox2: Sequence[float]
) -> float:
    return interval_overlap_width(bbox_x_interval(bbox1), bbox_x_interval(bbox2))


def vertical_overlap_ratio(
    bbox1: Sequence[float], bbox2: Sequence[float]
) -> float:
    top = max(float(bbox1[1]), float(bbox2[1]))
    bottom = min(float(bbox1[3]), float(bbox2[3]))
    overlap = max(0.0, bottom - top)
    height1 = max(0.0, float(bbox1[3]) - float(bbox1[1]))
    height2 = max(0.0, float(bbox2[3]) - float(bbox2[1]))
    smaller = min(height1, height2)
    return overlap / smaller if smaller > 0 else 0.0


def center_inside(inner: Sequence[float], outer: Sequence[float]) -> bool:
    center_x = (float(inner[0]) + float(inner[2])) / 2
    center_y = (float(inner[1]) + float(inner[3])) / 2
    return (
        float(outer[0]) <= center_x <= float(outer[2])
        and float(outer[1]) <= center_y <= float(outer[3])
    )


def clamp_bbox(
    bbox: Sequence[float], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    left = max(0, min(image_width, int(round(float(bbox[0])))))
    top = max(0, min(image_height, int(round(float(bbox[1])))))
    right = max(left + 1, min(image_width, int(round(float(bbox[2])))))
    bottom = max(top + 1, min(image_height, int(round(float(bbox[3])))))
    return left, top, right, bottom


def union_interval_width(intervals: Iterable[Sequence[float]]) -> float:
    normalized = sorted(
        (float(interval[0]), float(interval[1]))
        for interval in intervals
        if float(interval[1]) > float(interval[0])
    )
    if not normalized:
        return 0.0

    total = 0.0
    current_left, current_right = normalized[0]
    for left, right in normalized[1:]:
        if left <= current_right:
            current_right = max(current_right, right)
        else:
            total += current_right - current_left
            current_left, current_right = left, right
    total += current_right - current_left
    return total


def json_dump(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Titles TXT
# ---------------------------------------------------------------------------

def parse_line_selectors(
    selector_text: str,
    line_number: int,
    raw_line: str,
) -> dict[str, int]:
    """Parse one-based U/D/L/R border-line selectors after `///`."""
    normalized = selector_text.replace(",", " ").strip()
    if not normalized:
        raise ValueError(
            f"Missing line selectors after /// at TXT line {line_number}: {raw_line}"
        )

    selectors: dict[str, int] = {}
    for token in normalized.split():
        match = re.fullmatch(r"([UDLRudlr])\s*(\d+)", token)
        if match is None:
            raise ValueError(
                "Invalid line-selector token "
                f"'{token}' at TXT line {line_number}. "
                "Use tokens such as U1 D1 L2 R1."
            )
        side = match.group(1).upper()
        index = int(match.group(2))
        if index < 1:
            raise ValueError(
                f"Line-selector indexes are one-based at TXT line {line_number}: "
                f"{raw_line}"
            )
        if side in selectors:
            raise ValueError(
                f"Duplicate {side} selector at TXT line {line_number}: {raw_line}"
            )
        selectors[side] = index

    return selectors


def parse_title_request(line: str, line_number: int) -> TitleRequest:
    """Parse one TXT entry.

    A plain line keeps the original automatic behavior:
        DEPTH 06:00

    Exactly two hyphens define an EXCLUSIVE boundary. The marker identifies
    the next cell/section and is not included in the crop:
        MUD PROPERTIES -- ADDITIVES

    Three or more hyphens define an INCLUSIVE ending cell/section:
        DEPTH 06:00 --- MAASP

    Three forward slashes introduce optional final border-line selectors:
        COSTS /// U1 D1 L2 R1

    The selector stage runs after all automatic and explicit-marker logic.
    U/D select horizontal cell-border lines counted from the top/bottom;
    L/R select vertical cell-border lines counted from the left/right.
    """
    raw_line = line.strip()

    slash_matches = list(re.finditer(r"(?<!/)/{3}(?!/)", raw_line))
    if len(slash_matches) > 1:
        raise ValueError(
            f"Only one /// selector block is allowed at TXT line {line_number}: "
            f"{raw_line}"
        )

    if slash_matches:
        slash_match = slash_matches[0]
        boundary_text = raw_line[: slash_match.start()].strip()
        selector_text = raw_line[slash_match.end() :].strip()
        line_selectors = parse_line_selectors(
            selector_text, line_number, raw_line
        )
    else:
        boundary_text = raw_line
        line_selectors = {}

    separator_match = re.search(r"(?<!-)(-{2,})(?!-)", boundary_text)
    if separator_match is None:
        title = boundary_text.strip()
        end_marker = None
        end_marker_mode = None
        boundary_separator = None
    else:
        boundary_separator = separator_match.group(1)
        title = boundary_text[: separator_match.start()].strip()
        end_marker = boundary_text[separator_match.end() :].strip()
        end_marker_mode = (
            "exclusive" if len(boundary_separator) == 2 else "inclusive"
        )

    if not title:
        raise ValueError(
            f"Missing starting title at TXT line {line_number}: {raw_line}"
        )
    if separator_match is not None and not end_marker:
        raise ValueError(
            f"Missing explicit end marker at TXT line {line_number}: {raw_line}"
        )

    return TitleRequest(
        title=title,
        end_marker=end_marker,
        end_marker_mode=end_marker_mode,
        boundary_separator=boundary_separator,
        line_selectors=line_selectors,
        raw=raw_line,
    )


def load_titles_from_txt(path: Path, page_count: int) -> list[list[TitleRequest]]:
    text = path.read_text(encoding="utf-8-sig")
    titles_by_page: dict[int, list[TitleRequest]] = {}
    current_page = 0
    explicit_page_seen = False

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        page_match = PAGE_HEADER_PATTERN.match(line)
        if page_match:
            page_number = next(
                int(value) for value in page_match.groups() if value is not None
            )
            if page_number < 1:
                raise ValueError(
                    f"Invalid page number at TXT line {line_number}: {line}"
                )
            current_page = page_number - 1
            explicit_page_seen = True
            titles_by_page.setdefault(current_page, [])
            continue

        inline_match = INLINE_TITLE_PATTERN.match(line)
        if inline_match:
            page_number = int(inline_match.group(1))
            raw_title = inline_match.group(2).strip()
            if page_number < 1:
                raise ValueError(
                    f"Invalid page number at TXT line {line_number}: {line}"
                )

            # Subsequent bare lines stay on this inline page.
            current_page = page_number - 1
            titles_by_page.setdefault(current_page, []).append(
                parse_title_request(raw_title, line_number)
            )
            explicit_page_seen = True
            continue

        if line.startswith("//") or line.startswith("#"):
            continue

        if not explicit_page_seen:
            current_page = 0
        titles_by_page.setdefault(current_page, []).append(
            parse_title_request(line, line_number)
        )

    invalid_pages = sorted(
        page_number + 1
        for page_number in titles_by_page
        if page_number < 0 or page_number >= page_count
    )
    if invalid_pages:
        raise ValueError(f"TXT references nonexistent PDF pages: {invalid_pages}")

    result = [
        list(titles_by_page.get(page_number, []))
        for page_number in range(page_count)
    ]
    if not any(result):
        raise ValueError("No titles were found in the TXT file.")
    return result


def title_occurrence_keys(
    titles_per_page: list[list[TitleRequest]],
) -> list[list[tuple[int, str, int]]]:
    keys_by_page: list[list[tuple[int, str, int]]] = []
    for page_number, requests in enumerate(titles_per_page):
        counters: dict[str, int] = {}
        page_keys: list[tuple[int, str, int]] = []
        for request in requests:
            title = request.title
            occurrence = counters.get(title, 0)
            page_keys.append((page_number, title, occurrence))
            counters[title] = occurrence + 1
        keys_by_page.append(page_keys)
    return keys_by_page


def title_requests_to_json(
    titles_per_page: list[list[TitleRequest]],
) -> list[list[dict[str, Any]]]:
    return [
        [request.to_json() for request in page_requests]
        for page_requests in titles_per_page
    ]


# ---------------------------------------------------------------------------
# PDF text layer and cache
# ---------------------------------------------------------------------------

def group_words_by_visual_line(
    words: list[dict[str, Any]], y_tolerance: float
) -> list[list[dict[str, Any]]]:
    sorted_words = sorted(
        words, key=lambda word: (float(word["top"]), float(word["x0"]))
    )
    lines: list[list[dict[str, Any]]] = []
    centers: list[float] = []

    for word in sorted_words:
        center_y = (float(word["top"]) + float(word["bottom"])) / 2
        best_index: int | None = None
        best_distance = float("inf")

        for index, known_center in enumerate(centers):
            distance = abs(center_y - known_center)
            if distance <= y_tolerance and distance < best_distance:
                best_index = index
                best_distance = distance

        if best_index is None:
            lines.append([word])
            centers.append(center_y)
        else:
            lines[best_index].append(word)
            row_centers = [
                (float(item["top"]) + float(item["bottom"])) / 2
                for item in lines[best_index]
            ]
            centers[best_index] = sum(row_centers) / len(row_centers)

    for line in lines:
        line.sort(key=lambda word: float(word["x0"]))

    return sorted(
        lines,
        key=lambda line: (
            min(float(word["top"]) for word in line),
            min(float(word["x0"]) for word in line),
        ),
    )


def split_line_on_large_gaps(
    line_words: list[dict[str, Any]], gap_threshold: float
) -> list[list[dict[str, Any]]]:
    if not line_words:
        return []

    segments = [[line_words[0]]]
    for previous, current in zip(line_words, line_words[1:]):
        gap = float(current["x0"]) - float(previous["x1"])
        if gap > gap_threshold:
            segments.append([current])
        else:
            segments[-1].append(current)
    return segments


def scale_pdf_word(word: dict[str, Any], scale: float) -> dict[str, Any]:
    return {
        "text": str(word.get("text", "")),
        "x0": float(word["x0"]) * scale,
        "x1": float(word["x1"]) * scale,
        "top": float(word["top"]) * scale,
        "bottom": float(word["bottom"]) * scale,
    }


def segment_to_text_segment(
    segment: Sequence[dict[str, Any]], scale: float
) -> TextSegment | None:
    text = " ".join(str(word["text"]) for word in segment).strip()
    if not text:
        return None

    scaled_words = [scale_pdf_word(word, scale) for word in segment]
    return TextSegment(
        text=text,
        bbox=[
            min(float(word["x0"]) for word in scaled_words),
            min(float(word["top"]) for word in scaled_words),
            max(float(word["x1"]) for word in scaled_words),
            max(float(word["bottom"]) for word in scaled_words),
        ],
        words=scaled_words,
    )


def extract_text_layer(pdf_path: Path, config: PipelineConfig) -> list[PageText]:
    scale = config.target_dpi / config.pdf_coordinate_dpi
    pages: list[PageText] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages):
            words = page.extract_words(
                use_text_flow=False,
                keep_blank_chars=False,
                x_tolerance=2,
                y_tolerance=2,
            ) or []

            segments: list[TextSegment] = []
            for visual_line in group_words_by_visual_line(
                words, config.line_y_tolerance_points
            ):
                for segment_words in split_line_on_large_gaps(
                    visual_line, config.horizontal_gap_split_points
                ):
                    segment = segment_to_text_segment(segment_words, scale)
                    if segment is not None:
                        segments.append(segment)

            segments.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
            scaled_page_words = [scale_pdf_word(word, scale) for word in words]
            scaled_page_words.sort(
                key=lambda item: (float(item["top"]), float(item["x0"]))
            )
            pages.append(
                PageText(text_segments=segments, words=scaled_page_words)
            )
            print(
                f"Text page {page_number + 1}: "
                f"{len(words)} words -> {len(segments)} segments"
            )

    return pages


def text_pages_to_json(pages: list[PageText]) -> list[dict[str, Any]]:
    return [
        {
            "text_segments": [asdict(segment) for segment in page.text_segments],
            "words": page.words,
        }
        for page in pages
    ]


def text_pages_from_json(payload: list[dict[str, Any]]) -> list[PageText]:
    return [
        PageText(
            text_segments=[
                TextSegment(**segment)
                for segment in page.get("text_segments", [])
            ],
            words=list(page.get("words", [])),
        )
        for page in payload
    ]


def build_cache_signature(
    pdf_path: Path, page_count: int, config: PipelineConfig
) -> dict[str, Any]:
    stat = pdf_path.stat()
    return {
        "pdf_sha256": sha256_file(pdf_path),
        "pdf_size": stat.st_size,
        "pdf_mtime_ns": stat.st_mtime_ns,
        "page_count": page_count,
        "target_dpi": config.target_dpi,
        "pdf_coordinate_dpi": config.pdf_coordinate_dpi,
        "line_y_tolerance_points": config.line_y_tolerance_points,
        "horizontal_gap_split_points": config.horizontal_gap_split_points,
        "pdfplumber_version": package_version("pdfplumber"),
        "text_cache_schema": 2,
        "pipeline_version": PIPELINE_VERSION,
    }


def load_or_extract_text_layer(
    pdf_path: Path,
    cache_path: Path,
    page_count: int,
    config: PipelineConfig,
) -> tuple[list[PageText], dict[str, Any], bool]:
    signature = build_cache_signature(pdf_path, page_count, config)

    if config.use_text_cache and cache_path.exists() and not config.force_text_cache:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("signature") == signature:
                print(f"Using valid text cache: {cache_path.name}")
                return (
                    text_pages_from_json(payload.get("pages", [])),
                    signature,
                    True,
                )
            print("Text cache is stale and will be rebuilt.")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"Text cache could not be read and will be rebuilt: {exc}")

    pages = extract_text_layer(pdf_path, config)
    if config.use_text_cache:
        json_dump(
            cache_path,
            {
                "signature": signature,
                "pages": text_pages_to_json(pages),
            },
        )
        print(f"Saved text cache: {cache_path.name}")
    return pages, signature, False


# ---------------------------------------------------------------------------
# PDF cells and title matching
# ---------------------------------------------------------------------------

def find_cells_and_page_sizes(
    pdf_path: Path, config: PipelineConfig
) -> tuple[list[list[list[float]]], list[list[float]]]:
    scale = config.target_dpi / config.pdf_coordinate_dpi
    all_cells: list[list[list[float]]] = []
    page_sizes: list[list[float]] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages):
            raw_cells = page.debug_tablefinder().cells
            scaled_cells = [
                [float(coordinate) * scale for coordinate in cell]
                for cell in raw_cells
            ]
            all_cells.append(scaled_cells)
            page_sizes.append([float(page.width) * scale, float(page.height) * scale])
            print(f"Cells page {page_number + 1}: {len(scaled_cells)}")

    return all_cells, page_sizes


def title_text_score(segment_text: str, title: str) -> float:
    segment_normalized = normalize_text(segment_text)
    title_normalized = normalize_text(title)
    if not segment_normalized or not title_normalized:
        return 0.0

    if segment_normalized == title_normalized:
        return 4.0
    if compact_text(segment_text) == compact_text(title):
        return 3.5
    if title_normalized in segment_normalized:
        return 2.5
    if compact_text(title) and compact_text(title) in compact_text(segment_text):
        return 2.0
    if segment_normalized in title_normalized:
        return 1.0
    return 0.0


def word_span_bbox(words: Sequence[dict[str, Any]]) -> list[float]:
    return [
        min(float(word["x0"]) for word in words),
        min(float(word["top"]) for word in words),
        max(float(word["x1"]) for word in words),
        max(float(word["bottom"]) for word in words),
    ]


def find_word_span_candidates(
    page_text: PageText,
    title: str,
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    """Find a title as a contiguous word span, even across split segments.

    PDF text extraction can split `DEPTH 06:00` into separate visual segments.
    Searching the page-level words prevents a nearby standalone `Depth` from
    being selected merely because it sits inside a cleaner table cell.
    """
    if not page_text.words:
        return []

    line_tolerance = (
        config.line_y_tolerance_points
        * config.target_dpi
        / config.pdf_coordinate_dpi
    )
    query_word_count = max(1, len(normalize_text(title).split()))
    minimum_span = max(1, query_word_count - 1)
    maximum_span_extra = 3
    candidates: list[dict[str, Any]] = []

    for line_index, line_words in enumerate(
        group_words_by_visual_line(page_text.words, line_tolerance)
    ):
        line_words = sorted(line_words, key=lambda word: float(word["x0"]))
        for start_index in range(len(line_words)):
            maximum_end = min(
                len(line_words),
                start_index + query_word_count + maximum_span_extra,
            )
            for end_index in range(start_index + 1, maximum_end + 1):
                span = line_words[start_index:end_index]
                if len(span) < minimum_span:
                    continue

                span_text = " ".join(str(word["text"]) for word in span).strip()
                score = title_text_score(span_text, title)
                if score <= 0:
                    continue

                # For a multi-word query, a one-word partial match is only a
                # last-resort fallback. Exact/compact phrase matches score >= 3.5.
                partial_penalty = (
                    0.75
                    if query_word_count > 1
                    and len(span) < query_word_count
                    and score <= 1.0
                    else 0.0
                )
                candidates.append(
                    {
                        "segment_index": None,
                        "line_index": line_index,
                        "word_start_index": start_index,
                        "word_end_index": end_index,
                        "text": span_text,
                        "bbox": word_span_bbox(span),
                        "text_score": score - partial_penalty,
                        "candidate_source": "word_span",
                    }
                )

    return candidates


def find_text_candidates_on_page(
    page_text: PageText,
    title: str,
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    candidates = find_word_span_candidates(page_text, title, config)

    # Segment matching remains as a compatibility fallback for old caches or
    # unusual PDFs whose words could not be retained.
    for segment_index, segment in enumerate(page_text.text_segments):
        score = title_text_score(segment.text, title)
        if score <= 0:
            continue
        candidates.append(
            {
                "segment_index": segment_index,
                "text": segment.text,
                "bbox": list(map(float, segment.bbox)),
                "text_score": score,
                "candidate_source": "text_segment",
            }
        )

    if not candidates:
        return []

    # Keep only candidates close to the strongest textual match before any
    # geometry is considered. This is the important guard against selecting a
    # standalone `Depth` instead of the requested `DEPTH 06:00`.
    strongest = max(float(item["text_score"]) for item in candidates)
    candidates = [
        item
        for item in candidates
        if float(item["text_score"]) >= strongest - 0.25
    ]

    # Deduplicate equal localized boxes, preferring the stronger/more precise
    # word-span candidate.
    best_by_bbox: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for item in candidates:
        key = bbox_key(item["bbox"])
        previous = best_by_bbox.get(key)
        item_rank = (
            float(item["text_score"]),
            1 if item.get("candidate_source") == "word_span" else 0,
            -bbox_area(item["bbox"]),
        )
        previous_rank = (
            float(previous["text_score"]),
            1 if previous.get("candidate_source") == "word_span" else 0,
            -bbox_area(previous["bbox"]),
        ) if previous else None
        if previous is None or item_rank > previous_rank:
            best_by_bbox[key] = item

    return list(best_by_bbox.values())



def bbox_center(bbox: Sequence[float]) -> tuple[float, float]:
    return (
        (float(bbox[0]) + float(bbox[2])) / 2,
        (float(bbox[1]) + float(bbox[3])) / 2,
    )


def horizontal_overlap_ratio(
    bbox1: Sequence[float], bbox2: Sequence[float]
) -> float:
    width1 = max(0.0, float(bbox1[2]) - float(bbox1[0]))
    width2 = max(0.0, float(bbox2[2]) - float(bbox2[0]))
    smaller = min(width1, width2)
    if smaller <= 0:
        return 0.0
    return horizontal_overlap_width(bbox1, bbox2) / smaller


def infer_explicit_boundary_direction(
    start_bbox: Sequence[float],
    marker_bbox: Sequence[float],
    tolerance: float = 5.0,
) -> str | None:
    """Infer whether an explicit marker closes bottom, right, or both.

    The title cell is the crop origin. A marker sharing the same row and lying
    to the right closes the right edge. A lower marker closes the bottom. A
    lower marker that is also clearly outside the title column closes both.
    """
    start_x, start_y = bbox_center(start_bbox)
    marker_x, marker_y = bbox_center(marker_bbox)
    same_row = vertical_overlap_ratio(start_bbox, marker_bbox) >= 0.50
    same_column = horizontal_overlap_ratio(start_bbox, marker_bbox) >= 0.20
    is_right = marker_x > start_x + tolerance
    is_below = marker_y > start_y + tolerance

    if same_row and is_right:
        return "right"

    if is_below:
        extends_right = float(marker_bbox[2]) > float(start_bbox[2]) + tolerance
        if is_right and extends_right and not same_column:
            return "bottom_right"
        return "bottom"

    # A slightly misaligned cell can still be a right boundary as long as it
    # is not completely above the title row.
    vertically_plausible = (
        float(marker_bbox[3]) >= float(start_bbox[1]) - tolerance
    )
    if is_right and vertically_plausible:
        return "right"

    return None


def collect_text_cell_matches(
    page_text: PageText,
    cells: Sequence[Sequence[float]],
    search_text: str,
    config: PipelineConfig,
) -> tuple[list[dict[str, Any]], int]:
    candidates = find_text_candidates_on_page(page_text, search_text, config)
    matches: list[dict[str, Any]] = []

    for candidate in candidates:
        cell, metrics = best_cell_for_text(candidate["bbox"], cells, config)

        if cell is None:
            # Some report headers place labels in open areas rather than in a
            # closed tablefinder cell. Keep the exact text location as a
            # virtual anchor instead of discarding it and selecting a wrong
            # occurrence elsewhere on the page.
            cell = list(map(float, candidate["bbox"]))
            metrics = {
                "text_coverage": 1.0,
                "vertical_coverage": 1.0,
                "center_inside": 1.0,
                "geometry_score": 0.0,
            }
            cell_source = "localized_text_anchor"
        else:
            cell_source = "detected_cell"

        matches.append(
            {
                **candidate,
                "cell": cell,
                "cell_source": cell_source,
                **metrics,
                "combined_score": float(candidate["text_score"])
                + float(metrics["geometry_score"]),
            }
        )

    # Keep one best match per physical or virtual anchor.
    best_by_cell: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for item in matches:
        key = bbox_key(item["cell"])
        previous = best_by_cell.get(key)
        rank = (
            float(item["text_score"]),
            1 if item["cell_source"] == "detected_cell" else 0,
            float(item["combined_score"]),
            -bbox_area(item["bbox"]),
        )
        previous_rank = (
            float(previous["text_score"]),
            1 if previous["cell_source"] == "detected_cell" else 0,
            float(previous["combined_score"]),
            -bbox_area(previous["bbox"]),
        ) if previous else None
        if previous is None or rank > previous_rank:
            best_by_cell[key] = item

    return list(best_by_cell.values()), len(candidates)


def select_explicit_end_marker(
    page_text: PageText,
    cells: Sequence[Sequence[float]],
    marker_text: str,
    start_text_bbox: Sequence[float],
    config: PipelineConfig,
) -> tuple[dict[str, Any] | None, int, int]:
    matches, candidate_count = collect_text_cell_matches(
        page_text, cells, marker_text, config
    )
    start_x, start_y = bbox_center(start_text_bbox)
    plausible: list[dict[str, Any]] = []

    for item in matches:
        marker_anchor = item["bbox"]
        direction = infer_explicit_boundary_direction(
            start_text_bbox, marker_anchor
        )
        if direction is None:
            continue

        marker_x, marker_y = bbox_center(marker_anchor)
        delta_x = abs(marker_x - start_x)
        delta_y = abs(marker_y - start_y)
        if direction == "right":
            geometric_distance = delta_x + (4.0 * delta_y)
        elif direction == "bottom":
            geometric_distance = delta_y + (2.0 * delta_x)
        else:
            geometric_distance = (delta_x**2 + delta_y**2) ** 0.5

        plausible.append(
            {
                **item,
                "boundary_direction": direction,
                "geometric_distance": geometric_distance,
            }
        )

    plausible.sort(
        key=lambda item: (
            -item["text_score"],
            item["geometric_distance"],
            -item["combined_score"],
            item["cell"][1],
            item["cell"][0],
        )
    )
    return (plausible[0] if plausible else None), candidate_count, len(matches)


def best_cell_for_text(
    text_bbox: Sequence[float],
    cells: Sequence[Sequence[float]],
    config: PipelineConfig,
) -> tuple[list[float] | None, dict[str, float]]:
    text_area = bbox_area(text_bbox)
    if text_area <= 0:
        return None, {}

    best_cell: list[float] | None = None
    best_metrics: dict[str, float] = {}
    best_score = -float("inf")

    for cell in cells:
        overlap_area = intersection_area(text_bbox, cell)
        text_coverage = overlap_area / text_area
        contains_center = center_inside(text_bbox, cell)
        vertical_coverage = vertical_overlap_ratio(text_bbox, cell)

        if (
            not contains_center
            and text_coverage < config.min_text_coverage_for_cell_match
        ):
            continue

        cell_area = max(1.0, bbox_area(cell))
        area_ratio = min(20.0, cell_area / text_area)
        geometry_score = (
            (3.0 if contains_center else 0.0)
            + (2.0 * text_coverage)
            + (0.5 * vertical_coverage)
            - (0.01 * area_ratio)
        )

        if geometry_score > best_score:
            best_score = geometry_score
            best_cell = list(map(float, cell))
            best_metrics = {
                "text_coverage": text_coverage,
                "vertical_coverage": vertical_coverage,
                "center_inside": 1.0 if contains_center else 0.0,
                "geometry_score": geometry_score,
            }

    return best_cell, best_metrics


def find_title_cells(
    text_pages: list[PageText],
    all_cells: list[list[list[float]]],
    titles_per_page: list[list[TitleRequest]],
    config: PipelineConfig,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    title_cells: dict[int, list[dict[str, Any]]] = {}
    missing_titles: list[dict[str, Any]] = []
    missing_end_markers: list[dict[str, Any]] = []
    occurrence_keys = title_occurrence_keys(titles_per_page)

    for page_number, requests in enumerate(titles_per_page):
        page_results: list[dict[str, Any]] = []
        used_cells: set[tuple[int, int, int, int]] = set()
        last_selected_y = -float("inf")

        for title_index, (request, override_key) in enumerate(
            zip(requests, occurrence_keys[page_number])
        ):
            title = request.title
            matches, candidate_count = collect_text_cell_matches(
                text_pages[page_number],
                all_cells[page_number],
                title,
                config,
            )
            matches.sort(
                key=lambda item: (
                    item["cell"][1],
                    item["cell"][0],
                    -item["combined_score"],
                )
            )

            selected: dict[str, Any] | None = None
            if override_key in TITLE_MATCH_INDEX_OVERRIDES:
                requested = TITLE_MATCH_INDEX_OVERRIDES[override_key]
                if 0 <= requested < len(matches):
                    selected = matches[requested]
            else:
                unused = [
                    item for item in matches if bbox_key(item["cell"]) not in used_cells
                ]
                ordered = [
                    item for item in unused if item["cell"][1] >= last_selected_y - 5
                ]
                pool = ordered or unused
                if pool:
                    selected = pool[0]

            if selected is None:
                missing_titles.append(
                    {
                        "page": page_number + 1,
                        "title": title,
                        "end_marker": request.end_marker,
                        "end_marker_mode": request.end_marker_mode,
                        "line_selectors": dict(request.line_selectors),
                        "occurrence": override_key[2] + 1,
                        "candidate_text_matches": candidate_count,
                        "usable_cell_matches": len(matches),
                    }
                )
                continue

            selected_bbox = list(map(float, selected["cell"]))
            if override_key in TITLE_BBOX_OVERRIDES:
                selected_bbox = list(map(float, TITLE_BBOX_OVERRIDES[override_key]))

            end_marker_match: dict[str, Any] | None = None
            if request.end_marker:
                (
                    end_marker_match,
                    marker_candidate_count,
                    marker_cell_match_count,
                ) = select_explicit_end_marker(
                    text_pages[page_number],
                    all_cells[page_number],
                    request.end_marker,
                    selected["bbox"],
                    config,
                )
                if end_marker_match is None:
                    missing_end_markers.append(
                        {
                            "page": page_number + 1,
                            "title": title,
                            "end_marker": request.end_marker,
                            "end_marker_mode": request.end_marker_mode,
                            "boundary_separator": request.boundary_separator,
                            "occurrence": override_key[2] + 1,
                            "candidate_text_matches": marker_candidate_count,
                            "usable_cell_matches": marker_cell_match_count,
                            "reason": (
                                "marker_not_found_or_not_positioned_below_or_right"
                            ),
                        }
                    )
                    if not config.allow_missing_titles:
                        # Do not emit a silently auto-bounded table when the TXT
                        # explicitly requested a deterministic boundary.
                        continue

            used_cells.add(bbox_key(selected["cell"]))
            last_selected_y = selected_bbox[1]

            page_results.append(
                {
                    "title": title,
                    "title_request_raw": request.raw,
                    "title_index": title_index,
                    "occurrence_index": override_key[2],
                    "override_key": [page_number, title, override_key[2]],
                    "bbox": selected_bbox.copy(),
                    "original_bbox": selected_bbox.copy(),
                    "matched_text": selected["text"],
                    "text_bbox": selected["bbox"],
                    "text_match_score": selected["text_score"],
                    "text_coverage": selected["text_coverage"],
                    "geometry_score": selected["geometry_score"],
                    "title_anchor_source": selected.get("cell_source"),
                    "title_candidate_source": selected.get("candidate_source"),
                    "end_marker": request.end_marker,
                    "end_marker_mode": request.end_marker_mode,
                    "boundary_separator": request.boundary_separator,
                    "line_selectors": dict(request.line_selectors),
                    "end_marker_bbox": (
                        list(map(float, end_marker_match["cell"]))
                        if end_marker_match
                        else None
                    ),
                    "end_marker_direction": (
                        end_marker_match["boundary_direction"]
                        if end_marker_match
                        else None
                    ),
                    "end_marker_matched_text": (
                        end_marker_match["text"] if end_marker_match else None
                    ),
                    "end_marker_text_bbox": (
                        end_marker_match["bbox"] if end_marker_match else None
                    ),
                    "end_marker_text_score": (
                        end_marker_match["text_score"] if end_marker_match else None
                    ),
                    "end_marker_geometry_score": (
                        end_marker_match["geometry_score"]
                        if end_marker_match
                        else None
                    ),
                    "end_marker_anchor_source": (
                        end_marker_match.get("cell_source")
                        if end_marker_match
                        else None
                    ),
                    "end_marker_candidate_source": (
                        end_marker_match.get("candidate_source")
                        if end_marker_match
                        else None
                    ),
                }
            )

        title_cells[page_number] = page_results

    if (missing_titles or missing_end_markers) and not config.allow_missing_titles:
        raise RuntimeError(
            "Some requested titles or explicit end markers could not be mapped "
            "to table cells. Run with --allow-missing only when this is "
            "intentional.\n"
            + json.dumps(
                {
                    "missing_titles": missing_titles,
                    "missing_end_markers": missing_end_markers,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    return title_cells, missing_titles, missing_end_markers


# ---------------------------------------------------------------------------
# Stable lanes and title-width expansion
# ---------------------------------------------------------------------------

def cluster_titles_into_rows(
    page_titles: list[dict[str, Any]], config: PipelineConfig
) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []

    for title in sorted(
        page_titles,
        key=lambda item: (item["original_bbox"][1], item["original_bbox"][0]),
    ):
        best_row: list[dict[str, Any]] | None = None
        best_overlap = 0.0

        for row in rows:
            overlap = max(
                vertical_overlap_ratio(title["original_bbox"], member["original_bbox"])
                for member in row
            )
            if (
                overlap >= config.title_row_min_vertical_overlap
                and overlap > best_overlap
            ):
                best_row = row
                best_overlap = overlap

        if best_row is None:
            rows.append([title])
        else:
            best_row.append(title)

    for row in rows:
        row.sort(key=lambda item: (item["original_bbox"][0], item["original_bbox"][2]))

    return sorted(
        rows,
        key=lambda row: (
            min(item["original_bbox"][1] for item in row),
            min(item["original_bbox"][0] for item in row),
        ),
    )


def unique_sorted_coordinates(values: Sequence[float], tolerance: float) -> list[float]:
    values = sorted(map(float, values))
    if not values:
        return []

    groups: list[list[float]] = [[values[0]]]
    for value in values[1:]:
        if abs(value - groups[-1][-1]) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def page_content_x_bounds(
    page_cells: Sequence[Sequence[float]], page_width: float
) -> tuple[float, float]:
    if page_cells:
        return (
            min(float(cell[0]) for cell in page_cells),
            max(float(cell[2]) for cell in page_cells),
        )
    return 0.0, float(page_width)


def add_same_row_end_markers_as_layout_anchors(
    page_titles: list[dict[str, Any]],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    """Return title anchors plus inclusive right-side marker cells.

    An entry such as ``MUD PROPERTIES --- ADDITIVES`` means that ADDITIVES
    belongs to the first crop. Even though ADDITIVES is not a standalone TXT
    title, it is still a real horizontal structural block. Adding it to lane
    detection prevents a following title such as SOLIDS CONTROL from expanding
    leftward across the ADDITIVES cell.

    Only markers sharing the title row are added. Bottom or bottom-right markers
    must not influence the page's horizontal lane model.
    """
    anchors = list(page_titles)
    occupied = {bbox_key(item["original_bbox"]) for item in page_titles}
    synthetic_index = -1

    for owner in page_titles:
        if owner.get("end_marker_mode") != "inclusive":
            continue

        marker_bbox = owner.get("end_marker_bbox")
        if not marker_bbox:
            continue

        start_reference = owner.get("text_bbox") or owner["original_bbox"]
        marker_reference = owner.get("end_marker_text_bbox") or marker_bbox
        if (
            vertical_overlap_ratio(start_reference, marker_reference)
            < config.marker_lane_min_vertical_overlap
        ):
            continue

        start_center_x, _ = bbox_center(start_reference)
        marker_center_x, _ = bbox_center(marker_reference)
        if marker_center_x <= start_center_x + 1.0:
            continue

        marker_key = bbox_key(marker_bbox)
        if marker_key in occupied:
            continue

        occupied.add(marker_key)
        anchors.append(
            {
                "title": owner.get("end_marker") or "__end_marker__",
                "title_index": synthetic_index,
                "occurrence_index": 0,
                "bbox": list(map(float, marker_bbox)),
                "original_bbox": list(map(float, marker_bbox)),
                "layout_only": True,
                "layout_owner_title": owner.get("title"),
            }
        )
        synthetic_index -= 1

    return anchors


def find_column_guide_row(
    page_titles: list[dict[str, Any]],
    content_left: float,
    content_right: float,
    config: PipelineConfig,
) -> list[dict[str, Any]] | None:
    content_width = max(1.0, content_right - content_left)
    candidates: list[tuple[float, float, list[dict[str, Any]]]] = []

    for row in cluster_titles_into_rows(page_titles, config):
        if len(row) < config.column_guide_min_titles:
            continue

        intervals = [
            [
                max(content_left, float(item["original_bbox"][0])),
                min(content_right, float(item["original_bbox"][2])),
            ]
            for item in row
        ]
        row_left = min(interval[0] for interval in intervals)
        row_right = max(interval[1] for interval in intervals)
        envelope_coverage = max(0.0, row_right - row_left) / content_width
        union_coverage = union_interval_width(intervals) / content_width

        centers = sorted((interval[0] + interval[1]) / 2 for interval in intervals)
        center_span = (
            (centers[-1] - centers[0]) / content_width if len(centers) > 1 else 0.0
        )

        if (
            envelope_coverage >= config.column_guide_min_envelope_coverage
            and center_span >= config.column_guide_min_center_span
        ):
            # Favor broad, multi-title rows but keep top-to-bottom order decisive.
            score = envelope_coverage + union_coverage + min(len(row), 8) * 0.05
            top = min(float(item["original_bbox"][1]) for item in row)
            candidates.append((top, -score, row))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def guide_row_top(guide_row: list[dict[str, Any]] | None) -> float | None:
    if not guide_row:
        return None
    return min(float(item["original_bbox"][1]) for item in guide_row)


def cells_near_guide_row(
    page_cells: Sequence[Sequence[float]],
    guide_row: list[dict[str, Any]],
    config: PipelineConfig,
) -> list[Sequence[float]]:
    guide_top = min(float(item["original_bbox"][1]) for item in guide_row)
    guide_bottom = max(float(item["original_bbox"][3]) for item in guide_row)
    margin = config.guide_border_vertical_margin_pixels

    nearby = [
        cell
        for cell in page_cells
        if float(cell[3]) >= guide_top - margin
        and float(cell[1]) <= guide_bottom + margin
    ]
    return nearby or list(page_cells)


def choose_border_between_titles(
    page_borders: Sequence[float],
    left_title: dict[str, Any],
    right_title: dict[str, Any],
    minimum_x: float,
    maximum_x: float,
) -> float:
    left_bbox = left_title["original_bbox"]
    right_bbox = right_title["original_bbox"]
    left_center = (float(left_bbox[0]) + float(left_bbox[2])) / 2
    right_center = (float(right_bbox[0]) + float(right_bbox[2])) / 2
    target = (float(left_bbox[2]) + float(right_bbox[0])) / 2

    candidates = [
        border
        for border in page_borders
        if minimum_x < border < maximum_x and left_center < border < right_center
    ]
    return min(candidates, key=lambda border: abs(border - target)) if candidates else target


def build_stable_column_lanes(
    page_number: int,
    page_titles: list[dict[str, Any]],
    page_cells: Sequence[Sequence[float]],
    page_width: float,
    config: PipelineConfig,
) -> tuple[list[list[float]], list[dict[str, Any]] | None]:
    content_left, content_right = page_content_x_bounds(page_cells, page_width)
    layout_anchors = add_same_row_end_markers_as_layout_anchors(
        page_titles, config
    )
    guide_row = find_column_guide_row(
        layout_anchors, content_left, content_right, config
    )

    if page_number in COLUMN_LANE_OVERRIDES:
        lanes = [
            [float(lane[0]), float(lane[1])]
            for lane in COLUMN_LANE_OVERRIDES[page_number]
        ]
        return sorted(lanes, key=lambda lane: lane[0]), guide_row

    if not guide_row:
        return [[content_left, content_right]], None

    relevant_cells = cells_near_guide_row(page_cells, guide_row, config)
    page_borders = unique_sorted_coordinates(
        [
            coordinate
            for cell in relevant_cells
            for coordinate in (float(cell[0]), float(cell[2]))
        ],
        config.title_border_tolerance_pixels,
    )

    boundaries = [
        choose_border_between_titles(
            page_borders,
            left_title,
            right_title,
            content_left,
            content_right,
        )
        for left_title, right_title in zip(guide_row, guide_row[1:])
    ]

    lane_edges = [content_left, *boundaries, content_right]
    lanes = [
        [float(lane_edges[index]), float(lane_edges[index + 1])]
        for index in range(len(lane_edges) - 1)
        if lane_edges[index + 1] > lane_edges[index]
    ]
    return lanes, guide_row


def parent_lane_span_for_title(
    original_bbox: Sequence[float],
    lanes: Sequence[Sequence[float]],
    config: PipelineConfig,
) -> list[float]:
    containing_lanes: list[Sequence[float]] = []

    for lane in lanes:
        lane_center = (float(lane[0]) + float(lane[1])) / 2
        if (
            float(original_bbox[0]) - config.title_border_tolerance_pixels
            <= lane_center
            <= float(original_bbox[2]) + config.title_border_tolerance_pixels
        ):
            containing_lanes.append(lane)

    if not containing_lanes:
        best_lane = max(
            lanes,
            key=lambda lane: interval_overlap_width(
                bbox_x_interval(original_bbox), lane
            ),
        )
        containing_lanes = [best_lane]

    return [
        min(float(lane[0]) for lane in containing_lanes),
        max(float(lane[1]) for lane in containing_lanes),
    ]


def is_pre_guide_singleton_row(
    row_titles: list[dict[str, Any]],
    guide_top: float | None,
    content_left: float,
    content_right: float,
    config: PipelineConfig,
) -> bool:
    if (
        not config.auto_expand_pre_guide_singletons
        or guide_top is None
        or len(row_titles) != 1
    ):
        return False

    original = row_titles[0]["original_bbox"]
    if float(original[3]) > guide_top + config.pre_guide_y_tolerance_pixels:
        return False

    content_width = max(1.0, content_right - content_left)
    original_width = max(0.0, float(original[2]) - float(original[0]))
    return original_width / content_width >= config.pre_guide_singleton_min_width_ratio


def expand_one_title_row_inside_lanes(
    page_number: int,
    row_titles: list[dict[str, Any]],
    page_cells: Sequence[Sequence[float]],
    lanes: Sequence[Sequence[float]],
    guide_top: float | None,
    content_left: float,
    content_right: float,
    config: PipelineConfig,
) -> None:
    if len(row_titles) == 1 and is_pre_guide_singleton_row(
        row_titles, guide_top, content_left, content_right, config
    ):
        item = row_titles[0]
        key = (page_number, item["title"], item["occurrence_index"])
        if (
            key not in DISABLE_PRE_GUIDE_FULL_WIDTH
            and key not in DISABLE_AUTO_WIDTH_EXPANSION
        ):
            original = item["original_bbox"]
            item["parent_lane"] = [content_left, content_right]
            item["bbox"] = [
                content_left,
                float(original[1]),
                content_right,
                float(original[3]),
            ]
            item["auto_width_expanded"] = True
            item["pre_guide_full_width"] = True
            return

    page_borders = unique_sorted_coordinates(
        [
            coordinate
            for cell in page_cells
            for coordinate in (float(cell[0]), float(cell[2]))
        ],
        config.title_border_tolerance_pixels,
    )

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in row_titles:
        parent_span = parent_lane_span_for_title(item["original_bbox"], lanes, config)
        item["parent_lane"] = parent_span
        item["pre_guide_full_width"] = False
        groups.setdefault((round(parent_span[0]), round(parent_span[1])), []).append(item)

    for group_items in groups.values():
        group_items.sort(
            key=lambda item: (item["original_bbox"][0], item["original_bbox"][2])
        )
        parent_left = min(item["parent_lane"][0] for item in group_items)
        parent_right = max(item["parent_lane"][1] for item in group_items)

        boundaries = [
            choose_border_between_titles(
                page_borders, left_title, right_title, parent_left, parent_right
            )
            for left_title, right_title in zip(group_items, group_items[1:])
        ]

        for index, item in enumerate(group_items):
            key = (page_number, item["title"], item["occurrence_index"])
            if key in DISABLE_AUTO_WIDTH_EXPANSION:
                item["auto_width_expanded"] = False
                continue

            original = item["original_bbox"]
            expanded_x0 = parent_left if index == 0 else boundaries[index - 1]
            expanded_x1 = (
                parent_right if index == len(group_items) - 1 else boundaries[index]
            )
            expanded_x0 = min(float(original[0]), float(expanded_x0))
            expanded_x1 = max(float(original[2]), float(expanded_x1))

            item["bbox"] = [
                expanded_x0,
                float(original[1]),
                expanded_x1,
                float(original[3]),
            ]
            item["auto_width_expanded"] = (
                abs(expanded_x0 - float(original[0])) > 0.5
                or abs(expanded_x1 - float(original[2])) > 0.5
            )


def enforce_inclusive_marker_ownership(
    page_titles: list[dict[str, Any]],
    config: PipelineConfig,
) -> None:
    """Keep neighboring table bboxes out of inclusive marker cells.

    Lane detection normally provides the correct split. This second pass is a
    deterministic safety guard for irregular pages where the guide row is
    inferred from another part of the form.
    """
    barriers: list[dict[str, Any]] = []
    for owner in page_titles:
        if owner.get("end_marker_mode") != "inclusive":
            continue
        marker_bbox = owner.get("end_marker_bbox")
        if not marker_bbox:
            continue

        start_reference = owner.get("text_bbox") or owner["original_bbox"]
        marker_reference = owner.get("end_marker_text_bbox") or marker_bbox
        if (
            vertical_overlap_ratio(start_reference, marker_reference)
            < config.marker_lane_min_vertical_overlap
        ):
            continue

        start_center_x, _ = bbox_center(start_reference)
        marker_center_x, _ = bbox_center(marker_reference)
        if marker_center_x <= start_center_x + 1.0:
            continue

        barriers.append(
            {
                "owner": owner,
                "bbox": list(map(float, marker_bbox)),
                "marker": owner.get("end_marker"),
            }
        )

    for item in page_titles:
        item.setdefault("layout_constraints", [])
        title_reference = item.get("text_bbox") or item["original_bbox"]
        title_center_x, _ = bbox_center(title_reference)

        for barrier in barriers:
            if barrier["owner"] is item:
                continue

            marker_bbox = barrier["bbox"]
            if (
                vertical_overlap_ratio(title_reference, marker_bbox)
                < config.marker_lane_min_vertical_overlap
            ):
                continue

            marker_center_x, _ = bbox_center(marker_bbox)
            old_bbox = list(map(float, item["bbox"]))
            new_bbox = old_bbox.copy()

            # A title to the right cannot expand into the marker cell.
            if marker_center_x < title_center_x:
                candidate_left = float(marker_bbox[2])
                if candidate_left < float(new_bbox[2]) - 1.0:
                    new_bbox[0] = max(float(new_bbox[0]), candidate_left)

            # Symmetric guard for a title to the left of a marker owned by a
            # different table. The marker's owner itself is excluded above.
            elif marker_center_x > title_center_x:
                candidate_right = float(marker_bbox[0])
                if candidate_right > float(new_bbox[0]) + 1.0:
                    new_bbox[2] = min(float(new_bbox[2]), candidate_right)

            original = item["original_bbox"]
            # Never clamp away the title's own detected cell.
            new_bbox[0] = min(float(new_bbox[0]), float(original[0]))
            new_bbox[2] = max(float(new_bbox[2]), float(original[2]))

            if (
                new_bbox[0] < new_bbox[2]
                and (
                    abs(new_bbox[0] - old_bbox[0]) > 0.5
                    or abs(new_bbox[2] - old_bbox[2]) > 0.5
                )
            ):
                item["bbox"] = new_bbox
                item["layout_constraints"].append(
                    {
                        "type": "inclusive_end_marker_ownership",
                        "marker": barrier["marker"],
                        "marker_bbox": marker_bbox,
                        "bbox_before": old_bbox,
                        "bbox_after": new_bbox,
                    }
                )


def expand_title_cell_widths(
    title_cells: dict[int, list[dict[str, Any]]],
    all_cells: list[list[list[float]]],
    page_sizes: list[list[float]],
    config: PipelineConfig,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[int, list[list[float]]],
    dict[int, float | None],
]:
    lanes_by_page: dict[int, list[list[float]]] = {}
    guide_top_by_page: dict[int, float | None] = {}

    if not config.auto_expand_title_width:
        return title_cells, lanes_by_page, guide_top_by_page

    for page_number, page_titles in title_cells.items():
        if not page_titles:
            lanes_by_page[page_number] = []
            guide_top_by_page[page_number] = None
            continue

        content_left, content_right = page_content_x_bounds(
            all_cells[page_number], page_sizes[page_number][0]
        )
        lanes, guide_row = build_stable_column_lanes(
            page_number,
            page_titles,
            all_cells[page_number],
            page_sizes[page_number][0],
            config,
        )
        current_guide_top = guide_row_top(guide_row)

        for item in page_titles:
            item["page_number"] = page_number

        for row_titles in cluster_titles_into_rows(page_titles, config):
            expand_one_title_row_inside_lanes(
                page_number,
                row_titles,
                all_cells[page_number],
                lanes,
                current_guide_top,
                content_left,
                content_right,
                config,
            )

        enforce_inclusive_marker_ownership(page_titles, config)

        lanes_by_page[page_number] = lanes
        guide_top_by_page[page_number] = current_guide_top

    return title_cells, lanes_by_page, guide_top_by_page


# ---------------------------------------------------------------------------
# Table boundaries and image output
# ---------------------------------------------------------------------------

def has_meaningful_x_overlap(
    bbox1: Sequence[float], bbox2: Sequence[float], config: PipelineConfig
) -> bool:
    overlap = horizontal_overlap_width(bbox1, bbox2)
    width1 = max(0.0, float(bbox1[2]) - float(bbox1[0]))
    width2 = max(0.0, float(bbox2[2]) - float(bbox2[0]))
    smaller_width = min(width1, width2)
    if smaller_width <= 0:
        return False

    required = max(
        config.table_min_x_overlap_pixels,
        smaller_width * config.table_min_x_overlap_ratio,
    )
    return overlap >= required


def lane_fallback_bottom(
    title_bbox: Sequence[float],
    page_cells: Sequence[Sequence[float]],
    page_height: float,
    config: PipelineConfig,
) -> float:
    relevant_cells = [
        cell
        for cell in page_cells
        if float(cell[3]) >= float(title_bbox[3])
        and has_meaningful_x_overlap(title_bbox, cell, config)
    ]
    if relevant_cells:
        return max(float(cell[3]) for cell in relevant_cells)
    return float(page_height)


def horizontal_span_for_anchor(
    anchor_bbox: Sequence[float],
    lanes: Sequence[Sequence[float]],
    page_cells: Sequence[Sequence[float]],
    page_width: float,
) -> list[float]:
    """Return the structural horizontal lane containing an anchor.

    Inclusive `START --- END` boundaries cover the complete ending lane.
    Exclusive `START -- NEXT` boundaries use the returned lane's starting edge
    as the first structural region outside the requested crop.
    """
    available_lanes = [list(map(float, lane)) for lane in lanes]
    if not available_lanes:
        left, right = page_content_x_bounds(page_cells, page_width)
        return [left, right]

    center_x, _ = bbox_center(anchor_bbox)
    containing = [
        lane
        for lane in available_lanes
        if float(lane[0]) - 1.0 <= center_x <= float(lane[1]) + 1.0
    ]
    if containing:
        return min(
            containing,
            key=lambda lane: (
                float(lane[1]) - float(lane[0]),
                abs(((float(lane[0]) + float(lane[1])) / 2) - center_x),
            ),
        )

    return max(
        available_lanes,
        key=lambda lane: interval_overlap_width(
            bbox_x_interval(anchor_bbox), lane
        ),
    )


def nearest_cell_top_for_anchor(
    anchor_bbox: Sequence[float],
    page_cells: Sequence[Sequence[float]],
) -> float:
    """Snap a text-only anchor upward to the nearest plausible table border."""
    anchor_top = float(anchor_bbox[1])
    center_x, _ = bbox_center(anchor_bbox)
    candidates = [
        float(cell[1])
        for cell in page_cells
        if float(cell[1]) <= anchor_top + 1.0
        and float(cell[0]) - 1.0 <= center_x <= float(cell[2]) + 1.0
    ]
    return max(candidates) if candidates else anchor_top



def cluster_border_segments(
    segments: Sequence[tuple[float, float, float, bool]],
    tolerance: float,
    opposite_span: float,
    min_coverage_ratio: float,
) -> list[dict[str, Any]]:
    """Cluster near-identical cell-border coordinates and measure coverage."""
    if not segments:
        return []

    sorted_segments = sorted(segments, key=lambda item: item[0])
    groups: list[list[tuple[float, float, float, bool]]] = []

    for segment in sorted_segments:
        coordinate = float(segment[0])
        if not groups:
            groups.append([segment])
            continue

        known_coordinate = sum(float(item[0]) for item in groups[-1]) / len(
            groups[-1]
        )
        if abs(coordinate - known_coordinate) <= tolerance:
            groups[-1].append(segment)
        else:
            groups.append([segment])

    candidates: list[dict[str, Any]] = []
    denominator = max(1.0, float(opposite_span))
    for group in groups:
        coordinate = sum(float(item[0]) for item in group) / len(group)
        coverage = union_interval_width(
            (float(item[1]), float(item[2])) for item in group
        )
        coverage_ratio = min(1.0, coverage / denominator)
        includes_bbox_edge = any(bool(item[3]) for item in group)

        if coverage_ratio < min_coverage_ratio and not includes_bbox_edge:
            continue

        candidates.append(
            {
                "coordinate": coordinate,
                "coverage_pixels": coverage,
                "coverage_ratio": coverage_ratio,
                "segment_count": len(group),
                "includes_detected_bbox_edge": includes_bbox_edge,
            }
        )

    return sorted(candidates, key=lambda item: float(item["coordinate"]))


def border_line_candidates(
    table_bbox: Sequence[float],
    page_cells: Sequence[Sequence[float]],
    config: PipelineConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return vertical and horizontal structural lines inside a table bbox.

    Cell rectangle sides are used as line segments. Repeated collinear segments
    are merged, so a grid line that is split across many rows still counts as
    one candidate. The four automatically detected bbox edges are inserted as
    full-coverage candidates, preserving U1/D1/L1/R1 as no-op defaults.
    """
    left, top, right, bottom = map(float, table_bbox)
    tolerance = config.manual_line_coordinate_tolerance_pixels
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)

    vertical_segments: list[tuple[float, float, float, bool]] = [
        (left, top, bottom, True),
        (right, top, bottom, True),
    ]
    horizontal_segments: list[tuple[float, float, float, bool]] = [
        (top, left, right, True),
        (bottom, left, right, True),
    ]

    for cell in page_cells:
        cell_left, cell_top, cell_right, cell_bottom = map(float, cell)

        clipped_top = max(top, cell_top)
        clipped_bottom = min(bottom, cell_bottom)
        if clipped_bottom > clipped_top:
            for coordinate in (cell_left, cell_right):
                if left - tolerance <= coordinate <= right + tolerance:
                    vertical_segments.append(
                        (coordinate, clipped_top, clipped_bottom, False)
                    )

        clipped_left = max(left, cell_left)
        clipped_right = min(right, cell_right)
        if clipped_right > clipped_left:
            for coordinate in (cell_top, cell_bottom):
                if top - tolerance <= coordinate <= bottom + tolerance:
                    horizontal_segments.append(
                        (coordinate, clipped_left, clipped_right, False)
                    )

    vertical = cluster_border_segments(
        vertical_segments,
        tolerance,
        height,
        config.manual_line_min_coverage_ratio,
    )
    horizontal = cluster_border_segments(
        horizontal_segments,
        tolerance,
        width,
        config.manual_line_min_coverage_ratio,
    )
    return vertical, horizontal


def select_numbered_border_line(
    candidates: Sequence[dict[str, Any]],
    side: str,
    one_based_index: int,
    page_number: int,
    title: str,
) -> dict[str, Any]:
    """Select a border line counted inward from one page-facing side."""
    ordered = list(candidates)
    if side in {"R", "D"}:
        ordered.reverse()

    if one_based_index > len(ordered):
        coordinates = [round(float(item["coordinate"]), 2) for item in ordered]
        raise ValueError(
            f"TXT line selector {side}{one_based_index} for '{title}' on "
            f"page {page_number + 1} cannot be applied: only {len(ordered)} "
            f"candidate lines were found from that side ({coordinates})."
        )

    selected = dict(ordered[one_based_index - 1])
    selected["side"] = side
    selected["requested_index"] = one_based_index
    return selected


def apply_txt_line_selectors(
    table_bbox: Sequence[float],
    page_cells: Sequence[Sequence[float]],
    selectors: dict[str, int],
    config: PipelineConfig,
    page_number: int,
    title: str,
) -> tuple[list[float], dict[str, Any]]:
    """Apply `/// U# D# L# R#` as the final bbox adjustment stage."""
    original_bbox = list(map(float, table_bbox))
    vertical, horizontal = border_line_candidates(
        original_bbox, page_cells, config
    )

    selected_lines: dict[str, dict[str, Any]] = {}
    adjusted = original_bbox.copy()

    for side in ("U", "D", "L", "R"):
        if side not in selectors:
            continue
        pool = horizontal if side in {"U", "D"} else vertical
        selected = select_numbered_border_line(
            pool,
            side,
            int(selectors[side]),
            page_number,
            title,
        )
        selected_lines[side] = selected
        coordinate = float(selected["coordinate"])
        if side == "U":
            adjusted[1] = coordinate
        elif side == "D":
            adjusted[3] = coordinate
        elif side == "L":
            adjusted[0] = coordinate
        else:
            adjusted[2] = coordinate

    if adjusted[2] <= adjusted[0] + 1.0:
        raise ValueError(
            f"TXT line selectors for '{title}' on page {page_number + 1} "
            f"produce an invalid horizontal range: {adjusted[0]:.2f}.."
            f"{adjusted[2]:.2f}."
        )
    if adjusted[3] <= adjusted[1] + 1.0:
        raise ValueError(
            f"TXT line selectors for '{title}' on page {page_number + 1} "
            f"produce an invalid vertical range: {adjusted[1]:.2f}.."
            f"{adjusted[3]:.2f}."
        )

    metadata = {
        "selectors": dict(selectors),
        "bbox_before": original_bbox,
        "bbox_after": adjusted.copy(),
        "selected_lines": selected_lines,
        "vertical_candidates": vertical,
        "horizontal_candidates": horizontal,
    }
    return adjusted, metadata


def compute_table_records(
    title_cells: dict[int, list[dict[str, Any]]],
    all_cells: list[list[list[float]]],
    page_sizes: list[list[float]],
    lanes_by_page: dict[int, list[list[float]]],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    global_index = 0

    for page_number in sorted(title_cells):
        sorted_titles = sorted(
            title_cells[page_number],
            key=lambda item: (item["bbox"][1], item["bbox"][0], item["title_index"]),
        )

        for local_index, current in enumerate(sorted_titles):
            key = (page_number, current["title"], current["occurrence_index"])
            if key in SKIP_TABLES:
                continue

            global_index += 1
            x0, y0, x1, y1 = map(float, current["bbox"])
            closing_title: str | None = None
            boundary_source = "lane_cell_fallback"
            automatic_bbox: list[float] | None = None

            if key in TABLE_BBOX_OVERRIDES:
                table_bbox = list(map(float, TABLE_BBOX_OVERRIDES[key]))
                boundary_source = "manual_override"
            else:
                table_bottom = lane_fallback_bottom(
                    current["bbox"],
                    all_cells[page_number],
                    page_sizes[page_number][1],
                    config,
                )

                for next_title in sorted_titles[local_index + 1 :]:
                    next_bbox = next_title["bbox"]
                    if (
                        float(next_bbox[1]) > y1
                        and has_meaningful_x_overlap(current["bbox"], next_bbox, config)
                    ):
                        closing_title = next_title["title"]
                        table_bottom = float(next_bbox[1])
                        boundary_source = "next_overlapping_title"
                        break

                automatic_bbox = [x0, y0, x1, max(table_bottom, y1)]
                table_bbox = automatic_bbox.copy()

                marker_bbox = current.get("end_marker_bbox")
                marker_direction = current.get("end_marker_direction")
                marker_mode = current.get("end_marker_mode") or "inclusive"
                if marker_bbox and marker_direction:
                    marker_x0, marker_y0, marker_x1, marker_y1 = map(
                        float, marker_bbox
                    )

                    start_anchor = current.get("text_bbox") or current["original_bbox"]
                    marker_anchor = (
                        current.get("end_marker_text_bbox") or marker_bbox
                    )
                    page_lanes = lanes_by_page.get(page_number, [])
                    start_span = horizontal_span_for_anchor(
                        start_anchor,
                        page_lanes,
                        all_cells[page_number],
                        page_sizes[page_number][0],
                    )
                    marker_span = horizontal_span_for_anchor(
                        marker_anchor,
                        page_lanes,
                        all_cells[page_number],
                        page_sizes[page_number][0],
                    )

                    # Always retain the structural left/top edge belonging to
                    # the requested starting title. For open header labels,
                    # snap the top to the nearest table border.
                    table_bbox[0] = min(
                        float(table_bbox[0]),
                        float(start_span[0]),
                        float(start_anchor[0]),
                    )
                    if current.get("title_anchor_source") == "localized_text_anchor":
                        table_bbox[1] = min(
                            float(table_bbox[1]),
                            nearest_cell_top_for_anchor(
                                start_anchor, all_cells[page_number]
                            ),
                        )

                    if marker_mode == "exclusive":
                        # `START -- NEXT` treats NEXT as the first cell/section
                        # outside the requested crop. This is useful for
                        # adjacent tables such as `MUD PROPERTIES -- ADDITIVES`.
                        if marker_direction in {"right", "bottom_right"}:
                            marker_lane_left = float(marker_span[0])
                            # Prefer the marker lane's structural start when it
                            # is genuinely to the right of the crop origin. If
                            # start and marker were grouped into one broad lane,
                            # fall back to the marker cell's own left edge.
                            if marker_lane_left > float(table_bbox[0]) + 1.0:
                                exclusive_right = min(marker_x0, marker_lane_left)
                            else:
                                exclusive_right = marker_x0
                            table_bbox[2] = max(
                                float(table_bbox[0]) + 1.0,
                                exclusive_right,
                            )
                        if marker_direction in {"bottom", "bottom_right"}:
                            exclusive_bottom = marker_y0
                            if current.get("end_marker_anchor_source") == "localized_text_anchor":
                                exclusive_bottom = nearest_cell_top_for_anchor(
                                    marker_anchor, all_cells[page_number]
                                )
                            table_bbox[3] = max(
                                float(table_bbox[1]) + 1.0,
                                exclusive_bottom,
                            )
                        boundary_source = (
                            f"exclusive_end_marker_{marker_direction}_lane_start"
                        )
                    else:
                        if marker_direction in {"right", "bottom_right"}:
                            # `START --- END` includes the END section and closes
                            # at its far structural lane edge.
                            table_bbox[2] = max(
                                float(table_bbox[0]) + 1.0,
                                marker_x1,
                                float(marker_span[1]),
                            )
                        if marker_direction in {"bottom", "bottom_right"}:
                            table_bbox[3] = max(y1, marker_y1)
                        boundary_source = (
                            f"inclusive_end_marker_{marker_direction}_lane_span"
                        )
                    closing_title = None

            line_selector_metadata: dict[str, Any] | None = None
            line_selectors = current.get("line_selectors") or {}
            if line_selectors:
                table_bbox, line_selector_metadata = apply_txt_line_selectors(
                    table_bbox,
                    all_cells[page_number],
                    dict(line_selectors),
                    config,
                    page_number,
                    current["title"],
                )
                boundary_source = f"{boundary_source}+txt_line_selectors"
                selected_summary = ", ".join(
                    f"{side}{line_selectors[side]}="
                    f"{line_selector_metadata['selected_lines'][side]['coordinate']:.2f}"
                    for side in ("U", "D", "L", "R")
                    if side in line_selectors
                )
                print(
                    f"Applied TXT line selectors on page {page_number + 1} "
                    f"for '{current['title']}': {selected_summary}"
                )

            filename = (
                f"slice__{global_index:04d}__p{page_number + 1:03d}__"
                f"{safe_slug(current['title'])}.png"
            )
            records.append(
                {
                    "record_type": "table",
                    "slice_id": f"T{global_index:04d}",
                    "table_id": f"T{global_index:04d}",
                    "sequence": global_index,
                    "page_number": page_number,
                    "page": page_number + 1,
                    "title": current["title"],
                    "title_request_raw": current.get("title_request_raw"),
                    "occurrence_index": current["occurrence_index"],
                    "title_bbox": list(map(float, current["bbox"])),
                    "table_bbox": table_bbox,
                    "automatic_table_bbox_before_explicit_marker": automatic_bbox,
                    "closing_title": closing_title,
                    "boundary_source": boundary_source,
                    "end_marker": current.get("end_marker"),
                    "end_marker_mode": current.get("end_marker_mode"),
                    "boundary_separator": current.get("boundary_separator"),
                    "line_selectors": dict(current.get("line_selectors") or {}),
                    "line_selector_metadata": line_selector_metadata,
                    "end_marker_bbox": current.get("end_marker_bbox"),
                    "end_marker_direction": current.get("end_marker_direction"),
                    "end_marker_matched_text": current.get(
                        "end_marker_matched_text"
                    ),
                    "title_text_bbox": current.get("text_bbox"),
                    "title_anchor_source": current.get("title_anchor_source"),
                    "title_candidate_source": current.get(
                        "title_candidate_source"
                    ),
                    "layout_constraints": current.get(
                        "layout_constraints", []
                    ),
                    "end_marker_text_bbox": current.get(
                        "end_marker_text_bbox"
                    ),
                    "end_marker_anchor_source": current.get(
                        "end_marker_anchor_source"
                    ),
                    "end_marker_candidate_source": current.get(
                        "end_marker_candidate_source"
                    ),
                    "crop_filename": filename,
                }
            )

    return records


def compute_trailing_content_records(
    text_pages: list[PageText],
    table_records: list[dict[str, Any]],
    all_cells: list[list[list[float]]],
    page_sizes: list[list[float]],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    """Create one crop per page for text below the lowest extracted table.

    This is intentionally text-driven: pure whitespace does not create an image.
    A small bottom padding is retained so signature lines and rules immediately
    below the final text line remain visible in the resulting slice.
    """
    if not config.extract_trailing_content:
        return []

    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for record in table_records:
        tables_by_page.setdefault(int(record["page_number"]), []).append(record)

    next_sequence = max(
        (int(record["sequence"]) for record in table_records),
        default=0,
    )
    trailing_records: list[dict[str, Any]] = []
    trailing_index = 0

    for page_number, page_tables in sorted(tables_by_page.items()):
        if page_number >= len(text_pages):
            continue

        page_width, page_height = map(float, page_sizes[page_number])
        lowest_table_bottom = max(
            float(record["table_bbox"][3]) for record in page_tables
        )
        minimum_text_top = (
            lowest_table_bottom + config.trailing_content_min_gap_pixels
        )

        candidates: list[TextSegment] = []
        for segment in text_pages[page_number].text_segments:
            segment_bbox = segment.bbox
            if float(segment_bbox[1]) < minimum_text_top:
                continue

            segment_area = bbox_area(segment_bbox)
            if segment_area <= 0:
                continue

            # Defensive check for unusual/manual table boxes that still overlap
            # the segment even though its top is below the global table boundary.
            covered_ratio = max(
                (
                    intersection_area(segment_bbox, record["table_bbox"])
                    / segment_area
                    for record in page_tables
                ),
                default=0.0,
            )
            if covered_ratio >= 0.50:
                continue

            candidates.append(segment)

        alnum_count = sum(
            1
            for segment in candidates
            for character in normalize_text(segment.text)
            if character.isalnum()
        )
        if alnum_count < config.trailing_content_min_alnum_characters:
            continue

        text_left = min(float(segment.bbox[0]) for segment in candidates)
        text_top = min(float(segment.bbox[1]) for segment in candidates)
        text_right = max(float(segment.bbox[2]) for segment in candidates)
        text_bottom = max(float(segment.bbox[3]) for segment in candidates)

        if config.trailing_content_use_page_content_width:
            content_left, content_right = page_content_x_bounds(
                all_cells[page_number], page_width
            )
            slice_left = min(content_left, text_left)
            slice_right = max(content_right, text_right)
        else:
            slice_left = text_left
            slice_right = text_right

        # The tail starts exactly where the lowest extracted table finishes.
        # By default it extends to the physical page bottom. Text detection is
        # only used as a guard against producing a blank/whitespace-only slice.
        slice_bottom = (
            page_height
            if config.trailing_content_crop_to_page_bottom
            else min(
                page_height,
                text_bottom + config.trailing_content_bottom_padding_pixels,
            )
        )
        slice_bbox = [
            max(
                0.0,
                slice_left - config.trailing_content_horizontal_padding_pixels,
            ),
            max(0.0, lowest_table_bottom),
            min(
                page_width,
                slice_right + config.trailing_content_horizontal_padding_pixels,
            ),
            slice_bottom,
        ]
        if bbox_area(slice_bbox) <= 1.0:
            continue

        trailing_index += 1
        next_sequence += 1
        filename = (
            f"slice__{next_sequence:04d}__p{page_number + 1:03d}__"
            "remaining-content.png"
        )
        trailing_records.append(
            {
                "record_type": "trailing_content",
                "slice_id": f"R{trailing_index:04d}",
                "sequence": next_sequence,
                "page_number": page_number,
                "page": page_number + 1,
                "title": "REMAINING PAGE CONTENT",
                "table_bbox": slice_bbox,
                "boundary_source": "remaining_text_below_lowest_table",
                "crop_to_page_bottom": config.trailing_content_crop_to_page_bottom,
                "lowest_table_bottom": lowest_table_bottom,
                "text_segment_count": len(candidates),
                "alnum_character_count": alnum_count,
                "detected_text": [segment.text for segment in candidates],
                "detected_text_bboxes": [segment.bbox for segment in candidates],
                "crop_filename": filename,
            }
        )

    return trailing_records


def render_pdf_page(pdf_path: Path, page_number: int, dpi: int) -> Image.Image:
    images = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=page_number + 1,
        last_page=page_number + 1,
    )
    if not images:
        raise RuntimeError(f"Could not render PDF page {page_number + 1}.")
    return images[0].convert("RGB")


def load_annotation_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def draw_table_annotation(
    image: Image.Image,
    record: dict[str, Any],
    font: ImageFont.ImageFont,
) -> None:
    draw = ImageDraw.Draw(image)
    bbox = record["table_bbox"]
    draw.rectangle(bbox, outline=(20, 90, 220), width=4)

    record_id = record.get("slice_id") or record.get("table_id") or "SLICE"
    label = f"{record_id}  {record['title']}"
    try:
        text_box = draw.textbbox((0, 0), label, font=font)
        label_width = text_box[2] - text_box[0]
        label_height = text_box[3] - text_box[1]
    except AttributeError:
        label_width, label_height = draw.textsize(label, font=font)

    label_x = max(0, int(round(float(bbox[0]))))
    preferred_y = int(round(float(bbox[1]))) - label_height - 8
    label_y = max(0, preferred_y)
    draw.rectangle(
        [label_x, label_y, label_x + label_width + 10, label_y + label_height + 6],
        fill=(255, 255, 255),
        outline=(20, 90, 220),
        width=2,
    )
    draw.text((label_x + 5, label_y + 3), label, fill=(0, 0, 0), font=font)


def save_crops_and_all_tables_map(
    pdf_path: Path,
    output_dir: Path,
    records: list[dict[str, Any]],
    config: PipelineConfig,
) -> Path:
    records_by_page: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        records_by_page.setdefault(record["page_number"], []).append(record)

    for page_records in records_by_page.values():
        page_records.sort(key=lambda item: int(item.get("sequence", 0)))

    annotated_pages: list[Image.Image] = []
    font = load_annotation_font(max(14, config.target_dpi // 14))

    for page_number in sorted(records_by_page):
        page_image = render_pdf_page(pdf_path, page_number, config.target_dpi)
        annotated_page = page_image.copy()

        for record in records_by_page[page_number]:
            padded_bbox = [
                float(record["table_bbox"][0]) - config.crop_padding_pixels,
                float(record["table_bbox"][1]) - config.crop_padding_pixels,
                float(record["table_bbox"][2]) + config.crop_padding_pixels,
                float(record["table_bbox"][3]) + config.crop_padding_pixels,
            ]
            crop_box = clamp_bbox(padded_bbox, page_image.width, page_image.height)
            crop = page_image.crop(crop_box)
            crop_path = output_dir / record["crop_filename"]
            crop.save(crop_path, format="PNG")
            record["crop_bbox_with_padding"] = list(crop_box)
            record["crop_path"] = record["crop_filename"]
            record_label = (
                "remaining page content"
                if record.get("record_type") == "trailing_content"
                else "table"
            )
            print(f"Saved {record_label} crop: {record['crop_filename']}")

            draw_table_annotation(annotated_page, record, font)

        annotated_pages.append(annotated_page.convert("RGB"))

    if not annotated_pages:
        raise RuntimeError("No table records were produced; all-tables map cannot be created.")

    map_path = output_dir / "map__all_tables.pdf"
    annotated_pages[0].save(
        map_path,
        format="PDF",
        save_all=True,
        append_images=annotated_pages[1:],
        resolution=float(config.target_dpi),
    )
    print(f"Saved all-tables map: {map_path.name}")
    return map_path


# ---------------------------------------------------------------------------
# Output-folder lifecycle and pipeline
# ---------------------------------------------------------------------------

def prepare_output_folder(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_patterns = [
        "slice__*.png",
        "map__all_tables.pdf",
        "data__tables.json",
    ]
    for pattern in generated_patterns:
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def validate_text_layer_strength(
    text_pages: list[PageText],
    titles_per_page: list[list[TitleRequest]],
    config: PipelineConfig,
) -> None:
    weak_pages = [
        {
            "page": page_number + 1,
            "text_segments": len(text_pages[page_number].text_segments),
        }
        for page_number, titles in enumerate(titles_per_page)
        if titles
        and len(text_pages[page_number].text_segments)
        < config.min_text_segments_on_titled_page
    ]
    if weak_pages:
        raise RuntimeError(
            "The PDF text layer is missing or too weak on titled pages: "
            + json.dumps(weak_pages, ensure_ascii=False)
        )


def run_pipeline(
    pdf_path: Path,
    titles_path: Path,
    output_dir: Path,
    config: PipelineConfig,
) -> dict[str, Any]:
    require_runtime_dependencies()

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if not titles_path.is_file():
        raise FileNotFoundError(f"Titles TXT not found: {titles_path}")

    prepare_output_folder(output_dir)
    cache_path = output_dir / "cache__text_layer.json"
    results_path = output_dir / "data__tables.json"

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
    titles_per_page = load_titles_from_txt(titles_path, page_count)

    text_pages, cache_signature, cache_hit = load_or_extract_text_layer(
        pdf_path, cache_path, page_count, config
    )
    if len(text_pages) != page_count:
        raise RuntimeError(
            f"Text cache/extraction page count mismatch: {len(text_pages)} != {page_count}"
        )

    validate_text_layer_strength(text_pages, titles_per_page, config)
    all_cells, page_sizes = find_cells_and_page_sizes(pdf_path, config)

    title_cells, missing_titles, missing_end_markers = find_title_cells(
        text_pages, all_cells, titles_per_page, config
    )
    title_cells, lanes_by_page, guide_top_by_page = expand_title_cell_widths(
        title_cells, all_cells, page_sizes, config
    )

    table_records = compute_table_records(
        title_cells, all_cells, page_sizes, lanes_by_page, config
    )
    trailing_content_records = compute_trailing_content_records(
        text_pages,
        table_records,
        all_cells,
        page_sizes,
        config,
    )
    output_records = sorted(
        [*table_records, *trailing_content_records],
        key=lambda item: int(item["sequence"]),
    )
    map_path = save_crops_and_all_tables_map(
        pdf_path, output_dir, output_records, config
    )

    payload = {
        "pipeline": "single_folder_table_bundle_v9_txt_line_selectors",
        "pipeline_version": PIPELINE_VERSION,
        "source_pdf": str(pdf_path.resolve()),
        "source_titles_txt": str(titles_path.resolve()),
        "output_folder": str(output_dir.resolve()),
        "output_naming": {
            "crop_pattern": "slice__NNNN__pPPP__title.png",
            "trailing_crop_pattern": (
                "slice__NNNN__pPPP__remaining-content.png"
            ),
            "all_tables_map": map_path.name,
            "table_metadata": results_path.name,
            "text_cache": cache_path.name,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "pdfplumber": package_version("pdfplumber"),
            "pdf2image": package_version("pdf2image"),
            "Pillow": package_version("Pillow"),
        },
        "config": asdict(config),
        "cache": {
            "hit": cache_hit,
            "signature": cache_signature,
        },
        "page_count": page_count,
        "titles_per_page": title_requests_to_json(titles_per_page),
        "text_segments_per_page": [
            len(page.text_segments) for page in text_pages
        ],
        "cells_per_page": [len(cells) for cells in all_cells],
        "missing_titles": missing_titles,
        "missing_end_markers": missing_end_markers,
        "column_lanes_by_page": {
            str(page_number + 1): lanes
            for page_number, lanes in lanes_by_page.items()
        },
        "guide_top_by_page": {
            str(page_number + 1): value
            for page_number, value in guide_top_by_page.items()
        },
        "title_cells": {
            str(page_number + 1): values
            for page_number, values in title_cells.items()
        },
        "tables": table_records,
        "table_count": len(table_records),
        "trailing_content_slices": trailing_content_records,
        "trailing_content_count": len(trailing_content_records),
        "all_slices": output_records,
        "slice_count": len(output_records),
    }
    json_dump(results_path, payload)
    print(f"Saved metadata: {results_path.name}")
    print(f"Bundle folder: {output_dir.resolve()}")
    return payload


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Crop titled PDF tables into one flat output folder. "
            "Use TITLE -- NEXT_CELL for an exclusive boundary, "
            "TITLE --- END_CELL for an inclusive boundary, and "
            "TITLE /// U1 D1 L2 R1 for final numbered border-line selection."
        )
    )
    parser.add_argument("pdf", type=Path, help="Input PDF path")
    parser.add_argument(
        "--titles",
        type=Path,
        default=None,
        help="Titles TXT path; default is the PDF path with .txt suffix",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Flat output folder; default is "
            "output/<pdf-stem>__table_bundle"
        ),
    )
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help=(
            "Continue even when a requested title or explicit end marker "
            "is not found"
        ),
    )
    parser.add_argument(
        "--force-text-cache",
        action="store_true",
        help="Rebuild cache__text_layer.json even if its signature is valid",
    )
    parser.add_argument(
        "--no-text-cache",
        action="store_true",
        help="Do not read or write the text-layer cache",
    )
    parser.add_argument(
        "--no-trailing-content",
        action="store_true",
        help=(
            "Do not create a remaining-content slice for text below the "
            "lowest table on each page"
        ),
    )
    parser.add_argument(
        "--trailing-min-chars",
        type=int,
        default=3,
        help=(
            "Minimum number of alphanumeric characters below the lowest table "
            "before a remaining-content slice is created (default: 3)"
        ),
    )
    parser.add_argument(
        "--trailing-bottom-padding",
        type=int,
        default=24,
        help=(
            "Extra pixels below the last detected text line when "
            "--tight-trailing-content is used (default: 24)"
        ),
    )
    parser.add_argument(
        "--tight-trailing-content",
        action="store_true",
        help=(
            "Crop the remaining-content slice tightly around detected text. "
            "Default behavior keeps the complete page tail down to page bottom."
        ),
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    titles_path = (
        args.titles.expanduser().resolve()
        if args.titles is not None
        else pdf_path.with_suffix(".txt")
    )
    output_dir = (
        args.output.expanduser().resolve()
        if args.output is not None
        else (Path.cwd() / "output" / f"{pdf_path.stem}__table_bundle").resolve()
    )

    config = PipelineConfig(
        target_dpi=args.dpi,
        allow_missing_titles=args.allow_missing,
        force_text_cache=args.force_text_cache,
        use_text_cache=not args.no_text_cache,
        extract_trailing_content=not args.no_trailing_content,
        trailing_content_min_alnum_characters=max(1, args.trailing_min_chars),
        trailing_content_bottom_padding_pixels=max(
            0, args.trailing_bottom_padding
        ),
        trailing_content_crop_to_page_bottom=not args.tight_trailing_content,
    )

    try:
        run_pipeline(pdf_path, titles_path, output_dir, config)
    except Exception as exc:  # noqa: BLE001 - CLI should report a clean failure.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
