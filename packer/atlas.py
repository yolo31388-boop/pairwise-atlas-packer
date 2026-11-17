"""UI atlas packer.

The public entry points are :func:`pack`, :func:`paginate`, :func:`rotate_rect`
and :func:`uv_rect`.

Coordinate model
----------------
A packed rect returned by :func:`pack` / :func:`paginate` has:

``x``, ``y``
    Top-left corner of the *placed* sprite, including any ``extrude`` border
    but excluding padding.  With ``extrude == 0`` this is the sprite itself.
``w``, ``h``
    Source dimensions of the sprite (before any atlas rotation).  For a
    rotated sprite the placed footprint is ``(h, w)``.
``slot_w``, ``slot_h``
    Placed footprint including the extrude border, excluding padding.
``content_x``, ``content_y``, ``content_w``, ``content_h``
    The actual image pixels inside the extrude border.
``rotated``
    Whether the sprite was rotated by 90 degrees.
``page``
    Zero based page index.

``padding`` is a gutter *per side* around every sprite.  It is best effort:
if reserving the requested gutter would prevent a page from being filled, the
packer reduces the gutter for that page so that capacity is not lost (a
32x32 sprite still fits four times into a 64x64 atlas with the default
``padding=1``).  Sprites never overlap; when a gutter is in effect the
distance between two sprite contents is at least ``2 * padding``.

``extrude`` grows the sprite by N pixels on every side (edge pixels are
duplicated by the image pipeline).  The UV rectangle therefore excludes the
extruded border so sampling never picks up a neighbouring sprite.

A rect may also carry trim / nine-slice metadata:

``trim``
    ``{"left": l, "top": t, "right": r, "bottom": b}``.  ``w``/``h`` are the
    trimmed dimensions.  ``original_w``/``original_h`` are recorded in the
    output (derived from the trim amounts when not supplied).
``pivot``
    Normalised ``(x, y)`` pivot in the *untrimmed* sprite.  It is remapped to
    the trimmed sprite and written back to the output.
``border`` (or ``nine_slice``)
    ``{"left": ..., "top": ..., "right": ..., "bottom": ...}``, copied to the
    output metadata as ``border``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["pack", "paginate", "rotate_rect", "uv_rect"]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _atlas_size(atlas_size: Sequence[int]) -> Tuple[int, int]:
    if len(atlas_size) != 2:
        raise ValueError("atlas_size must be a (width, height) pair")
    width, height = _int(atlas_size[0], "atlas width"), _int(atlas_size[1], "atlas height")
    if width <= 0 or height <= 0:
        raise ValueError("atlas dimensions must be positive")
    return width, height


def _extrude_pixels(value: Any) -> int:
    if value is True:
        return 1
    if value is False or value is None:
        return 0
    return max(0, _int(value, "extrude"))


def _padding_pixels(value: Any) -> int:
    if value is None:
        return 0
    return max(0, _int(value, "padding"))


def _border_metadata(value: Any) -> Optional[Dict[str, int]]:
    """Normalise a nine-slice border to a dict, or return ``None``."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            side: max(0, _int(value.get(side, 0), f"border.{side}"))
            for side in ("left", "top", "right", "bottom")
        }
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return {
            side: max(0, _int(amount, f"border.{side}"))
            for side, amount in zip(("left", "top", "right", "bottom"), value)
        }
    raise ValueError("border must be a dict or a four item sequence")


def _adjust_pivot(raw: Dict[str, Any], w: int, h: int, trim: Dict[str, int]) -> Any:
    """Remap a pivot from the original sprite into the trimmed sprite."""
    pivot = raw.get("pivot")
    if pivot is None:
        return None

    if isinstance(pivot, dict):
        px, py = pivot.get("x", 0), pivot.get("y", 0)
        template: Any = {"x": 0.0, "y": 0.0}
    else:
        px, py = pivot
        template = (0.0, 0.0)

    # Pivots are normalised unless the caller explicitly says otherwise.
    pixels = raw.get("pivot_in_pixels", False)
    if not pixels and 0.0 <= float(px) <= 1.0 and 0.0 <= float(py) <= 1.0:
        original_w = _int(raw.get("original_w", raw.get("source_w", w + trim["left"] + trim["right"])), "original_w")
        original_h = _int(raw.get("original_h", raw.get("source_h", h + trim["top"] + trim["bottom"])), "original_h")
        px = float(px) * original_w
        py = float(py) * original_h

    nx = (float(px) - trim["left"]) / w
    ny = (float(py) - trim["top"]) / h

    if isinstance(template, dict):
        return {"x": nx, "y": ny}
    return (nx, ny)


def _prepare_rect(raw: Dict[str, Any], index: int, atlas_size: Tuple[int, int]) -> Optional[Dict[str, Any]]:
    """Validate one input rect and turn it into the internal record form."""
    if not isinstance(raw, dict):
        raise ValueError("each rect must be a dict")
    name = raw.get("name", raw.get("id"))
    if name is None:
        raise ValueError("each rect needs a name")

    w = _int(raw.get("w", 0), "w")
    h = _int(raw.get("h", 0), "h")
    if w <= 0 or h <= 0:
        return None  # zero/negative area rects are ignored

    extrude = _extrude_pixels(raw.get("extrude", 0))
    trim_raw = raw.get("trim")
    trim = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    original_w: Optional[int] = None
    original_h: Optional[int] = None
    if trim_raw is not None:
        trim = _border_metadata(trim_raw) or trim
        original_w = _int(
            raw.get("original_w", raw.get("source_w", w + trim["left"] + trim["right"])),
            "original_w",
        )
        original_h = _int(
            raw.get("original_h", raw.get("source_h", h + trim["top"] + trim["bottom"])),
            "original_h",
        )

    border = _border_metadata(raw.get("border", raw.get("nine_slice")))
    pivot = _adjust_pivot(raw, w, h, trim) if trim_raw is not None else raw.get("pivot")

    atlas_w, atlas_h = atlas_size
    rotatable = bool(raw.get("rotatable", raw.get("allow_rotate", True)))
    # Padding is best effort, so the hard limit is the sprite (+extrude)
    # without any gutter.
    fits_plain = w + 2 * extrude <= atlas_w and h + 2 * extrude <= atlas_h
    fits_rotated = rotatable and h + 2 * extrude <= atlas_w and w + 2 * extrude <= atlas_h
    if not (fits_plain or fits_rotated):
        raise ValueError(
            f"rect {name!r} ({w}x{h}, extrude={extrude}) does not fit in atlas "
            f"{atlas_w}x{atlas_h}"
        )

    record: Dict[str, Any] = {
        "name": name,
        "w": w,
        "h": h,
        "extrude": extrude,
        "rotatable": rotatable,
        "index": index,
        "raw": raw,
        "trim": trim,
        "original_w": original_w,
        "original_h": original_h,
        "pivot": pivot,
        "border": border,
    }
    return record


def _prepare(
    rects: Iterable[Dict[str, Any]], atlas_size: Tuple[int, int]
) -> List[Dict[str, Any]]:
    """Filter, deduplicate (by name, first wins) and validate input rects."""
    seen = set()
    records: List[Dict[str, Any]] = []
    for index, raw in enumerate(rects):
        record = _prepare_rect(raw, index, atlas_size)
        if record is None:
            continue
        if record["name"] in seen:
            continue
        seen.add(record["name"])
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# MaxRects
# ---------------------------------------------------------------------------


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


class _MaxRects:
    """MaxRects free-rectangle packer with short-side-waste/contact scoring."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.free: List[Tuple[int, int, int, int]] = [(0, 0, width, height)]
        self.used: List[Tuple[int, int, int, int]] = []

    def _contact(self, x: int, y: int, w: int, h: int) -> int:
        contact = 0
        if x == 0:
            contact += h
        if y == 0:
            contact += w
        if x + w == self.width:
            contact += h
        if y + h == self.height:
            contact += w
        for px, py, pw, ph in self.used:
            if x == px + pw or x + w == px:
                contact += _overlap(y, y + h, py, py + ph)
            if y == py + ph or y + h == py:
                contact += _overlap(x, x + w, px, px + pw)
        return contact

    def _score(self, fx: int, fy: int, fw: int, fh: int, w: int, h: int) -> Tuple[int, ...]:
        left_w = fw - w
        left_h = fh - h
        short_waste = min(left_w, left_h)
        long_waste = max(left_w, left_h)
        area_waste = fw * fh - w * h
        contact = self._contact(fx, fy, w, h)
        # Short-side waste first, then shared edges (more contact = better),
        # then area waste.  The remaining fields make ties deterministic.
        return (short_waste, -contact, area_waste, long_waste, fy, fx)

    def place(
        self, w: int, h: int, allow_rotate: bool = True
    ) -> Optional[Tuple[int, int, int, int, bool]]:
        """Place a ``w`` x ``h`` slot, trying both orientations.

        Returns ``(x, y, placed_w, placed_h, rotated)`` or ``None``.
        """
        best: Optional[Tuple[Tuple[int, ...], int, int, int, bool]] = None
        for fx, fy, fw, fh in self.free:
            if w <= fw and h <= fh:
                candidate = (self._score(fx, fy, fw, fh, w, h), fx, fy, w, h, False)
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if allow_rotate and h <= fw and w <= fh:
                candidate = (self._score(fx, fy, fw, fh, h, w), fx, fy, h, w, True)
                if best is None or candidate[0] < best[0]:
                    best = candidate

        if best is None:
            return None

        _, x, y, placed_w, placed_h, rotated = best
        self._occupy(x, y, placed_w, placed_h)
        return x, y, placed_w, placed_h, rotated

    def _occupy(self, x: int, y: int, w: int, h: int) -> None:
        self.used.append((x, y, w, h))
        split: List[Tuple[int, int, int, int]] = []
        for fx, fy, fw, fh in self.free:
            if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
                split.append((fx, fy, fw, fh))
                continue
            if x > fx:
                split.append((fx, fy, x - fx, fh))
            if x + w < fx + fw:
                split.append((x + w, fy, fx + fw - (x + w), fh))
            if y > fy:
                split.append((fx, fy, fw, y - fy))
            if y + h < fy + fh:
                split.append((fx, y + h, fw, fy + fh - (y + h)))

        candidates = sorted({rect for rect in split if rect[2] > 0 and rect[3] > 0})
        self.free = [
            rect
            for rect in candidates
            if not any(
                other != rect
                and rect[0] >= other[0]
                and rect[1] >= other[1]
                and rect[0] + rect[2] <= other[0] + other[2]
                and rect[1] + rect[3] <= other[1] + other[3]
                for other in candidates
            )
        ]


# ---------------------------------------------------------------------------
# single page / multi page packing
# ---------------------------------------------------------------------------


def _sort_key(record: Dict[str, Any]) -> Tuple[Any, ...]:
    w, h = record["w"], record["h"]
    # Area first (large sprites are hardest), longer side next, then a stable
    # name/index tie break so repeated runs produce identical atlases.
    return (-(w * h), -max(w, h), -min(w, h), str(record["name"]), record["index"])


def _fill_page(
    records: Sequence[Dict[str, Any]], atlas_size: Tuple[int, int], padding: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Greedily fill one page, returning ``(placed, remaining)`` records."""
    packer = _MaxRects(*atlas_size)
    placed: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []

    for record in sorted(records, key=_sort_key):
        extrude = record["extrude"]
        # The slot includes the extrude halo plus a padding gutter per side.
        slot = 2 * extrude + 2 * padding
        position = packer.place(
            record["w"] + slot,
            record["h"] + slot,
            allow_rotate=record["rotatable"],
        )
        if position is None:
            remaining.append(record)
            continue

        sx, sy, placed_w, placed_h, rotated = position
        # x/y is the extruded sprite origin: padding is outside it, extrude is
        # inside it.
        outer_x = sx + padding
        outer_y = sy + padding
        slot_w = placed_w - 2 * padding
        slot_h = placed_h - 2 * padding
        content_w = slot_w - 2 * extrude
        content_h = slot_h - 2 * extrude

        placed.append(
            {
                "record": record,
                "x": outer_x,
                "y": outer_y,
                "slot_w": slot_w,
                "slot_h": slot_h,
                "content_w": content_w,
                "content_h": content_h,
                "rotated": rotated,
                "padding": padding,
            }
        )
    return placed, remaining


def _best_page(
    records: Sequence[Dict[str, Any]], atlas_size: Tuple[int, int], padding: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Pack one page with the largest gutter that does not lose capacity."""
    # Try the requested gutter down to zero; keep the largest gutter that
    # still places the maximum number of sprites on this page.
    if padding <= 64:
        gutters = range(padding, -1, -1)
    else:  # avoid pathological loops for silly inputs
        gutters = sorted({padding, padding // 2, 0}, reverse=True)

    best: Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]] = None
    for gutter in gutters:
        placed, remaining = _fill_page(records, atlas_size, gutter)
        if best is None or len(placed) > len(best[0]):
            best = (placed, remaining, gutter)
            if len(remaining) == 0:
                # More padding cannot beat a full page.
                break
    assert best is not None
    return best


def _output_rect(placed: Dict[str, Any], page: int) -> Dict[str, Any]:
    record = placed["record"]
    raw = record["raw"]
    out: Dict[str, Any] = dict(raw)  # preserve unknown user metadata
    out.update(
        {
            "name": record["name"],
            "x": placed["x"],
            "y": placed["y"],
            "w": record["w"],
            "h": record["h"],
            "rotated": placed["rotated"],
            "page": page,
            "slot_w": placed["slot_w"],
            "slot_h": placed["slot_h"],
            "content_x": placed["x"] + record["extrude"],
            "content_y": placed["y"] + record["extrude"],
            "content_w": placed["content_w"],
            "content_h": placed["content_h"],
            "padding": placed["padding"],
        }
    )
    if record["extrude"]:
        out["extrude"] = record["extrude"]

    if record["border"] is not None:
        out["border"] = dict(record["border"])

    if record["original_w"] is not None:
        out["trim"] = dict(record["trim"])
        out["original_w"] = record["original_w"]
        out["original_h"] = record["original_h"]
    if record["pivot"] is not None:
        out["pivot"] = record["pivot"]

    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def pack(
    rects: Iterable[Dict[str, Any]], atlas_size: Sequence[int], padding: int = 1
) -> List[Dict[str, Any]]:
    """Pack all sprites into a single page.

    Zero-area rects are ignored, duplicate names are packed once (first wins)
    and the result is returned in the original input order.  Raises
    ``ValueError`` for sprites that cannot fit, even when rotated.
    """
    atlas = _atlas_size(atlas_size)
    padding = _padding_pixels(padding)
    records = _prepare(rects, atlas)

    placed, remaining, _ = _best_page(records, atlas, padding)
    if remaining:
        names = ", ".join(str(record["name"]) for record in remaining)
        raise ValueError(f"sprites do not fit on one page: {names}")

    placed.sort(key=lambda item: item["record"]["index"])
    return [_output_rect(item, 0) for item in placed]


def paginate(
    rects: Iterable[Dict[str, Any]],
    atlas_size: Sequence[int],
    padding: int = 1,
    max_pages: int = 4,
) -> List[List[Dict[str, Any]]]:
    """Pack sprites into as many pages as needed, up to ``max_pages``.

    Each page is filled greedily by capacity; the page lists are returned in
    page order and each page keeps the original input order.  Raises
    ``ValueError`` if more than ``max_pages`` would be required (or if a
    sprite is larger than the atlas).
    """
    atlas = _atlas_size(atlas_size)
    padding = _padding_pixels(padding)
    max_pages = _int(max_pages, "max_pages")
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")

    remaining = _prepare(rects, atlas)
    pages: List[List[Dict[str, Any]]] = []

    while remaining:
        if len(pages) >= max_pages:
            raise ValueError(
                f"packing needs more than {max_pages} page(s) for "
                f"{len(remaining)} remaining sprite(s)"
            )

        placed, remaining, _ = _best_page(remaining, atlas, padding)
        if not placed:  # pragma: no cover - _prepare validates this
            raise ValueError("some sprites could not be placed")

        page = len(pages)
        placed.sort(key=lambda item: item["record"]["index"])
        pages.append([_output_rect(item, page) for item in placed])

    return pages


def rotate_rect(rect: Dict[str, Any], rotated: bool) -> Dict[str, Any]:
    """Return a copy of ``rect`` in the requested 90-degree orientation.

    Rotating swaps width and height (and every footprint related field).
    Calling it twice with the same orientation is a no-op.
    """
    out = dict(rect)
    target = bool(rotated)
    current = bool(rect.get("rotated", False))
    if target != current:
        for a, b in (
            ("w", "h"),
            ("slot_w", "slot_h"),
            ("content_w", "content_h"),
            ("footprint_w", "footprint_h"),
        ):
            if a in out and b in out:
                out[a], out[b] = out[b], out[a]
    out["rotated"] = target
    return out


def uv_rect(rect: Dict[str, Any], atlas_size: Sequence[int]) -> Tuple[float, float, float, float]:
    """Return ``(u0, v0, u1, v1)`` for a packed (or manually placed) rect.

    ``x``/``y`` are the top-left of the placed footprint, including any
    extrude border.  The extrude border is excluded from the UV rectangle so
    a sprite never samples its neighbour.  For rects without ``slot_w`` /
    ``slot_h`` a rotated rect is treated as a rotated *source* image, so its
    footprint is ``(h, w)``.
    """
    atlas_w, atlas_h = _atlas_size(atlas_size)
    x = _int(rect.get("x", 0), "x")
    y = _int(rect.get("y", 0), "y")
    w = _int(rect.get("w", 0), "w")
    h = _int(rect.get("h", 0), "h")

    if "slot_w" in rect and "slot_h" in rect:
        footprint_w = _int(rect["slot_w"], "slot_w")
        footprint_h = _int(rect["slot_h"], "slot_h")
    elif rect.get("rotated"):
        footprint_w, footprint_h = h, w
    else:
        footprint_w, footprint_h = w, h

    extrude = _extrude_pixels(rect.get("extrude", 0))
    inner_w = footprint_w - 2 * extrude
    inner_h = footprint_h - 2 * extrude
    if inner_w < 0 or inner_h < 0:
        raise ValueError("extrude is larger than the sprite footprint")

    u0 = (x + extrude) / atlas_w
    v0 = (y + extrude) / atlas_h
    u1 = (x + extrude + inner_w) / atlas_w
    v1 = (y + extrude + inner_h) / atlas_h
    return (u0, v0, u1, v1)
