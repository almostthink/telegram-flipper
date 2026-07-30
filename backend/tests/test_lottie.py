"""Проверки извлечения цвета модели из её анимации.

Цвет модели не отдаёт ни одна площадка, но ``modelStickerKey`` указывает
на ``.json`` — Lottie-анимацию, где цвета лежат векторными заливками.
Отсюда цвет и берётся, без декодирования картинок.
"""

from __future__ import annotations

from app.analytics.lottie import dominant_color


def fill(red: float, green: float, blue: float) -> dict:
    return {"ty": "fl", "c": {"a": 0, "k": [red, green, blue, 1]}}


def stroke(red: float, green: float, blue: float) -> dict:
    return {"ty": "st", "c": {"a": 0, "k": [red, green, blue, 1]}}


def layers(*shapes) -> dict:
    return {"v": "5.7.4", "layers": [{"ty": 4, "shapes": list(shapes)}]}


def test_single_fill_is_the_color():
    color = dominant_color(layers(fill(0.2, 0.7, 0.3)))
    assert color is not None
    assert color.rgb[1] > color.rgb[0] and color.rgb[1] > color.rgb[2], "должен быть зелёным"


def test_most_frequent_fill_wins():
    """Частота — приближение площади: крупная заливка повторяется."""
    document = layers(
        fill(0.9, 0.1, 0.1),
        fill(0.9, 0.1, 0.1),
        fill(0.9, 0.1, 0.1),
        fill(0.1, 0.1, 0.9),
    )
    color = dominant_color(document)
    assert color is not None
    assert color.rgb[0] > color.rgb[2], "красного больше, чем синего"


def test_strokes_are_ignored():
    """Обводки почти всегда чёрные.

    Если считать их цветом подарка, монохромными на чёрном фоне окажутся
    все модели подряд, и признак перестанет что-либо значить.
    """
    document = layers(
        stroke(0, 0, 0),
        stroke(0, 0, 0),
        stroke(0, 0, 0),
        stroke(0, 0, 0),
        fill(0.2, 0.7, 0.3),
    )
    color = dominant_color(document)
    assert color is not None
    assert not color.is_achromatic
    assert color.rgb[1] > color.rgb[0]


def test_chromatic_beats_more_numerous_greys():
    """Тени и блики есть в любой модели и всегда многочисленнее."""
    document = layers(
        fill(0.05, 0.05, 0.05),
        fill(0.05, 0.05, 0.05),
        fill(0.95, 0.95, 0.95),
        fill(0.95, 0.95, 0.95),
        fill(0.9, 0.2, 0.2),
    )
    color = dominant_color(document)
    assert color is not None
    assert not color.is_achromatic


def test_truly_monochrome_model_returns_its_grey():
    """Если цветного нет вовсе, серое — единственный честный ответ."""
    document = layers(fill(0.05, 0.05, 0.05), fill(0.05, 0.05, 0.06))
    color = dominant_color(document)
    assert color is not None
    assert color.is_achromatic


def test_close_shades_are_counted_together():
    """Иначе крупная заливка проигрывает сама себе из-за мелких оттенков."""
    document = layers(
        fill(0.20, 0.70, 0.30),
        fill(0.21, 0.71, 0.31),
        fill(0.19, 0.69, 0.29),
        fill(0.90, 0.10, 0.10),
        fill(0.90, 0.10, 0.10),
    )
    color = dominant_color(document)
    assert color is not None
    assert color.rgb[1] > color.rgb[0], "зелёного суммарно больше"


def test_animated_color_uses_the_first_keyframe():
    document = layers(
        {"ty": "fl", "c": {"a": 1, "k": [{"s": [0.2, 0.7, 0.3, 1]}, {"s": [0.9, 0.1, 0.1, 1]}]}}
    )
    color = dominant_color(document)
    assert color is not None
    assert color.rgb[1] > color.rgb[0]


def test_gradient_stops_are_read():
    document = layers(
        {
            "ty": "gf",
            "g": {"p": 2, "k": {"a": 0, "k": [0, 0.2, 0.7, 0.3, 1, 0.25, 0.75, 0.35]}},
        }
    )
    color = dominant_color(document)
    assert color is not None
    assert color.rgb[1] > color.rgb[0]


def test_components_in_bytes_are_accepted():
    """Lottie допускает и 0..1, и 0..255."""
    document = layers({"ty": "fl", "c": {"a": 0, "k": [51, 178, 76, 255]}})
    color = dominant_color(document)
    assert color is not None
    assert color.rgb == (51, 178, 76)


def test_broken_document_yields_nothing():
    for bad in (None, {}, [], {"layers": "не список"}, {"ty": "fl", "c": {"k": ["зелёный"]}}):
        assert dominant_color(bad) is None


def test_deeply_nested_shapes_are_found():
    """Заливки лежат в группах внутри групп."""
    document = {
        "layers": [
            {"shapes": [{"ty": "gr", "it": [{"ty": "gr", "it": [fill(0.2, 0.7, 0.3)]}]}]}
        ]
    }
    color = dominant_color(document)
    assert color is not None
    assert color.rgb[1] > color.rgb[0]
