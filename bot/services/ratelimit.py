"""Прості обмежувачі частоти в пам'яті (скидаються рестартом, це нормально)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Не більше `limit` подій за `window` секунд на ключ (DESIGN §13: 5 спроб коду на хвилину)."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> bool:
        """Реєструє подію. `False`, якщо ліміт вичерпано (подію не рахуємо)."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def reset(self, key: int) -> None:
        self._hits.pop(key, None)
