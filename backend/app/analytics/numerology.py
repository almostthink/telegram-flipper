"""Коллекционная ценность порядкового номера подарка.

У каждого подарка есть номер выпуска, и рынок платит за него надбавку,
никак не связанную с редкостью атрибутов. Подарок #1 стоит кратно дороже
подарка #40597 при полностью одинаковых модели, фоне и символе.

Ценятся, по убыванию:

* очень низкие номера — #1, затем однозначные, затем до сотни;
* повторы одной цифры — 777, 1111, 9999;
* круглые — 1000, 5000, 10000;
* последовательности — 1234, 4321;
* палиндромы — 1221, 12321;
* парные повторы — 1212, 6969.

Балл нормирован в 0..1 и служит признаком регрессии: коэффициент при нём
модель выучивает по фактическим сделкам, а не берёт из моих
представлений о красоте чисел. Значения ниже задают только форму шкалы,
а не величину надбавки к цене.

Важная оговорка для флиппинга: красивый номер поднимает цену, но сужает
круг покупателей. Такие лоты продаются дольше, и это учитывается не
здесь, а в оценке ликвидности и в доверии к оценке.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class NumberTrait:
    """Разбор номера: балл и человекочитаемое объяснение."""

    score: float
    label: str

    @property
    def is_notable(self) -> bool:
        """Номер, за который рынок вообще готов доплачивать."""
        return self.score >= 0.5


ORDINARY = NumberTrait(0.0, "обычный")


def _is_repdigit(text: str) -> bool:
    """Все цифры одинаковы: 777, 1111, 99999."""
    return len(text) >= 3 and len(set(text)) == 1


def _is_palindrome(text: str) -> bool:
    return len(text) >= 4 and text == text[::-1]


def _is_repeated_pair(text: str) -> bool:
    """Повтор пары цифр: 1212, 6969. Исключаем повтор одной цифры."""
    if len(text) != 4 or text[0] == text[1]:
        return False
    return text[:2] == text[2:]


def _is_sequence(text: str) -> bool:
    """Подряд идущие цифры в любом направлении: 123, 1234, 4321."""
    if len(text) < 3:
        return False
    digits = [int(char) for char in text]
    # strict=False намеренно: второй ряд короче на один элемент — это и есть
    # сравнение соседних цифр.
    steps = {second - first for first, second in zip(digits, digits[1:], strict=False)}
    return steps in ({1}, {-1})


def _round_score(number: int) -> float:
    """Круглые числа: чем больше нулей, тем заметнее."""
    if number >= 10_000 and number % 10_000 == 0:
        return 0.85
    if number >= 1_000 and number % 1_000 == 0:
        return 0.75
    if number >= 100 and number % 100 == 0:
        return 0.45
    return 0.0


def classify(number: int | None) -> NumberTrait:
    """Балл коллекционной ценности номера.

    Выбираем сильнейший из подошедших признаков: номер 1000 одновременно
    круглый и трёхзначный, и засчитывать надо лучшее свойство, а не сумму.
    """
    if number is None or number <= 0:
        return ORDINARY

    text = str(number)
    candidates: list[NumberTrait] = []

    # Низкие номера. Первый экземпляр — отдельная категория, дальше спад.
    if number == 1:
        candidates.append(NumberTrait(1.0, "первый выпущенный"))
    elif number <= 9:
        candidates.append(NumberTrait(0.92, "однозначный"))
    elif number <= 99:
        candidates.append(NumberTrait(0.6, "двузначный"))
    elif number <= 999:
        candidates.append(NumberTrait(0.3, "трёхзначный"))

    if _is_repdigit(text):
        # Чем длиннее повтор, тем реже он встречается.
        candidates.append(NumberTrait(min(0.75 + 0.07 * len(text), 0.98), f"повтор {text}"))

    round_value = _round_score(number)
    if round_value:
        candidates.append(NumberTrait(round_value, f"круглый {text}"))

    if _is_sequence(text):
        candidates.append(NumberTrait(0.7, f"последовательность {text}"))

    if _is_palindrome(text):
        candidates.append(NumberTrait(0.65, f"палиндром {text}"))

    if _is_repeated_pair(text):
        candidates.append(NumberTrait(0.6, f"парный {text}"))

    if not candidates:
        return ORDINARY
    return max(candidates, key=lambda trait: trait.score)


def score(number: int | None) -> float:
    """Только числовой балл — для использования как признака регрессии."""
    return classify(number).score
