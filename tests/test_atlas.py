import pytest

from packer.atlas import pack, paginate, rotate_rect, uv_rect


def rect(name, w, h, **kwargs):
    data = {"name": name, "w": w, "h": h}
    data.update(kwargs)
    return data


def test_pack_single_rect():
    result = pack([rect("button", 16, 8)], (32, 32))
    assert len(result) == 1


def test_pack_two_rects_do_not_overlap():
    result = pack([rect("a", 16, 16), rect("b", 16, 16)], (32, 32))
    assert result[0]["x"] != result[1]["x"] or result[0]["y"] != result[1]["y"]


def test_pack_applies_padding():
    result = pack([rect("a", 8, 8), rect("b", 8, 8)], (32, 32), padding=4)
    a, b = result
    assert abs(a["x"] - b["x"]) >= 4 or abs(a["y"] - b["y"]) >= 4


def test_pack_sets_page_index():
    result = pack([rect("a", 8, 8)], (32, 32))
    assert result[0]["page"] == 0


def test_pack_preserves_input_order():
    result = pack([rect("z", 8, 8), rect("a", 8, 8)], (32, 32))
    assert [item["name"] for item in result] == ["z", "a"]


def test_pack_ignores_zero_area_and_keeps_others():
    result = pack([rect("empty", 0, 0), rect("button", 8, 8)], (32, 32))
    assert [item["name"] for item in result] == ["button"]


def test_uv_rect_maps_pixels_to_normalized_coordinates():
    result = uv_rect({"x": 16, "y": 8, "w": 8, "h": 8}, (32, 32))
    assert result == pytest.approx((0.5, 0.25, 0.75, 0.5))


def test_uv_rect_handles_rotated_rect():
    result = uv_rect({"x": 0, "y": 0, "w": 8, "h": 16, "rotated": True}, (32, 32))
    assert result == pytest.approx((0.0, 0.0, 0.5, 0.25))


def test_rotate_rect_swaps_dimensions():
    result = rotate_rect(rect("a", 16, 8), True)
    assert result["w"] == 8
    assert result["h"] == 16
    assert result["rotated"] is True


def test_paginate_single_page():
    result = paginate([rect("a", 16, 16)], (32, 32))
    assert len(result) == 1


def test_paginate_overflow_to_second_page():
    result = paginate([rect(str(i), 32, 32) for i in range(5)], (64, 64))
    assert len(result) == 2


def test_paginate_rejects_too_many_pages():
    with pytest.raises(ValueError):
        paginate([rect(str(i), 64, 64) for i in range(5)], (64, 64), max_pages=1)


def test_paginate_deduplicates_same_name():
    result = paginate([rect("same", 8, 8), rect("same", 8, 8)], (32, 32))
    assert len(result) == 1


def test_pack_rejects_oversized_rect():
    with pytest.raises(ValueError):
        pack([rect("huge", 64, 64)], (32, 32))


def test_paginate_page_count_for_mixed_sizes():
    result = paginate([rect("a", 32, 32), rect("b", 32, 32), rect("c", 32, 32)], (64, 64))
    assert len(result) == 1
