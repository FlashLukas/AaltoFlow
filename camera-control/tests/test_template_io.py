"""Template PNG + embedded-metadata round-trip."""

import numpy as np

from camera.template_io import Reference, load_template, save_template


def test_template_metadata_roundtrip(tmp_path):
    tpl = (np.random.default_rng(0).integers(0, 255, (40, 50))).astype(np.uint8)
    ref = Reference(
        template=tpl,
        array_center_offset_px=(-119.5, -0.2),
        meta={"points_x": 3, "points_y": 3, "dx_um": 1.5, "dy_um": 1.5,
              "angle_deg": 12.0, "objective_name": "20x - Zeiss NA 0.7",
              "pixel_size_x_um": 0.413, "pixel_size_y_um": 0.413,
              "selected_index_x": 2, "selected_index_y": 1},
    )
    path = tmp_path / "pattern.png"
    save_template(str(path), ref)

    back = load_template(str(path))
    assert back.template.shape == tpl.shape
    assert np.array_equal(back.template, tpl)
    assert np.allclose(back.array_center_offset_px, (-119.5, -0.2))
    assert back.meta["points_x"] == 3
    assert back.meta["dx_um"] == 1.5
    assert back.meta["angle_deg"] == 12.0
    assert back.meta["objective_name"] == "20x - Zeiss NA 0.7"


def test_plain_png_loads_without_metadata(tmp_path):
    from PIL import Image
    p = tmp_path / "plain.png"
    Image.fromarray(np.zeros((10, 10), np.uint8)).save(p)
    ref = load_template(str(p))
    assert ref.template.shape == (10, 10)
    assert ref.array_center_offset_px == (0.0, 0.0)
