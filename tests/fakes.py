"""Shared test fakes.

``FakeModelCall`` stands in for :class:`shared.model_call.StructuredModelCall`
at the four call sites (routing / synthesis / follow-up / judge), replacing the
per-site ``agent=`` Strands fakes those tests used before issue #65.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeModelCall:
    """A drop-in fake for :class:`StructuredModelCall`.

    Configure exactly one response behaviour:

    * ``returns`` — a fixed structured-output object, or
    * ``response_fn`` — a ``prompt -> object`` callable (the judge needs this to
      vary its verdict by presentation order).

    ``raises`` simulates a failing model: :meth:`call` swallows it to ``None``
    (fail-open), :meth:`call_or_raise` re-raises it (caller-owned policy).
    """

    def __init__(
        self,
        *,
        returns: Any | None = None,
        response_fn: Callable[[str], Any] | None = None,
        raises: Exception | None = None,
        model_id: str | None = "fake-model",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._returns = returns
        self._response_fn = response_fn
        self._raises = raises
        self.model_id = model_id
        self._timeout_seconds = timeout_seconds
        self.prompts: list[str] = []

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def _resolve(self, prompt: str) -> Any:
        if self._response_fn is not None:
            return self._response_fn(prompt)
        return self._returns

    async def call(self, output_model: type, prompt: str) -> Any | None:
        self.prompts.append(prompt)
        if self._raises is not None:
            return None
        return self._resolve(prompt)

    async def call_or_raise(self, output_model: type, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._resolve(prompt)
