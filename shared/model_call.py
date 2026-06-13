"""The seam for one bounded, structured-output model call made outside any
planner loop (issue #65).

The master's routing / synthesis / follow-up decisions and the offline
experiment judge each make a single structured-output model call against a
tools-less Strands ``Agent``. Before this module they each re-implemented the
same scaffolding: a per-site ``_StructuredAgent`` Protocol, a ``_truthy``
helper, ``from_env`` gate + knob parsing, a lazy ``_resolve_model`` agent build,
and ``asyncio.wait_for`` + swallow-to-default. Only the prompt builder and
response validation genuinely differed.

:class:`StructuredModelCall` owns the shared burden. Each former call site keeps
only its prompt builder, response validation, and domain types and composes one
instance.

Two error modes:

* :meth:`call` is **fail-open** — any error, timeout, or agent-build failure
  returns ``None`` and the caller proceeds without the result (routing /
  synthesis / follow-up).
* :meth:`call_or_raise` propagates, for callers that own their own failure
  policy (the offline judge fails per-pair at the CLI level).

Every call records its token usage into a request-scoped accumulator — the same
``reset_usage`` / ``record_usage`` / ``drain_usage`` pattern the log summarizer
uses — which the :class:`InvestigationOrchestrator` drains into the experiment
scorecard so master-side decision cost is counted alongside agent cost.

Not for the raw-boto3 tier (``BedrockLlmClassifier``, ``BedrockLogSummarizer``,
``EmbeddingClient``): those run in synchronous Lambda / threadpool contexts where
a Strands ``Agent`` does not fit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from shared.agent_telemetry import compute_cost_usd
from shared.env import truthy

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Request-scoped token accumulator for master-side decision calls. Mirrors
# ``shared.log_summarizer``: the orchestrator zeroes it at the start of an
# investigation and drains it when storing the experiment result. Lock-guarded
# for parity with the summarizer; the master's decision calls run on the loop
# thread, so contention is not expected, but the lock keeps the contract safe.
# ---------------------------------------------------------------------------

_usage_lock = threading.Lock()
_usage_input = 0
_usage_output = 0
_usage_cost = 0.0
_usage_calls = 0
_usage_cost_seen = False


def reset_usage() -> None:
    """Zero the request-scoped decision-call token/cost accumulator."""
    global _usage_input, _usage_output, _usage_cost, _usage_calls, _usage_cost_seen
    with _usage_lock:
        _usage_input = 0
        _usage_output = 0
        _usage_cost = 0.0
        _usage_calls = 0
        _usage_cost_seen = False


def record_usage(
    input_tokens: int | None, output_tokens: int | None, cost_usd: float | None
) -> None:
    """Add one decision call's token usage and cost to the accumulator.

    ``cost_usd`` is ``None`` when the call's model is unpriced; tokens still
    accumulate so the token total stays honest even when cost cannot.
    """
    global _usage_input, _usage_output, _usage_cost, _usage_calls, _usage_cost_seen
    with _usage_lock:
        _usage_input += int(input_tokens or 0)
        _usage_output += int(output_tokens or 0)
        if cost_usd is not None:
            _usage_cost += cost_usd
            _usage_cost_seen = True
        _usage_calls += 1


def drain_usage() -> tuple[int | None, int | None, float | None]:
    """Return ``(input_tokens, output_tokens, cost_usd)`` accumulated this request.

    All ``None`` when no decision call recorded usage, so a non-deciding
    investigation leaves the scorecard blank rather than reporting a misleading
    zero. ``cost_usd`` is ``None`` when calls happened but none were priced.
    """
    with _usage_lock:
        if _usage_calls == 0:
            return None, None, None
        return _usage_input, _usage_output, (_usage_cost if _usage_cost_seen else None)


class _StructuredAgent(Protocol):
    """Minimal seam over a Strands ``Agent`` for structured output.

    Returns an object carrying ``.structured_output`` and ``.metrics`` (a Strands
    ``AgentResult`` in production), typed ``Any`` to stay assignable from the
    concrete ``Agent.invoke_async`` whose ``prompt`` is a wider union.
    """

    async def invoke_async(
        self, prompt: str, *, structured_output_model: type[BaseModel], **kwargs: Any
    ) -> Any: ...  # pragma: no cover - structural type


class StructuredModelCall:
    """One bounded, fail-open-capable structured-output model call.

    The agent is injectable for tests; in production it is built lazily from a
    tools-less Strands ``Agent`` bound to the resolved model (so the
    ``BEDROCK_GUARDRAIL_ID`` binding in :func:`_resolve_model` applies). Build it
    via :meth:`from_env`, which owns the gate flag, timeout/model knob parsing,
    and the optional pinned temperature.
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        agent: _StructuredAgent | None = None,
        model_id: str | None = None,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._agent = agent
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def model_id(self) -> str | None:
        """The model-id override resolution starts from (env value or default).

        ``None`` means "fall through to ``MODEL_ID`` / config / bundled default"
        — the master's routing/synthesis/follow-up case. The judge sets a
        concrete default so this is always populated for it.
        """
        return self._model_id

    @classmethod
    def from_env(
        cls,
        *,
        system_prompt: str,
        model_env: str,
        timeout_env: str,
        default_timeout: float,
        gate_env: str | None = None,
        default_model_id: str | None = None,
        temperature: float | None = None,
    ) -> "StructuredModelCall | None":
        """Build a call from the environment, or ``None`` when the gate is off.

        ``gate_env`` is the on/off flag; when ``None`` the call is always built
        (the judge has no gate). The model id resolves to ``model_env`` →
        ``default_model_id`` (then, inside :func:`_resolve_model` at build time,
        → ``MODEL_ID`` → config/default when still ``None``). The timeout
        resolves to ``timeout_env`` → ``default_timeout``.
        """
        if gate_env is not None and not truthy(os.environ.get(gate_env)):
            return None
        timeout_raw = os.environ.get(timeout_env)
        try:
            timeout = float(timeout_raw) if timeout_raw else default_timeout
        except ValueError:
            timeout = default_timeout
        return cls(
            system_prompt=system_prompt,
            model_id=os.environ.get(model_env) or default_model_id or None,
            timeout_seconds=timeout,
            temperature=temperature,
        )

    def _effective_model_id(self) -> str:
        """The model id actually invoked, mirroring :func:`_resolve_model`'s order
        (override → ``MODEL_ID`` → bundled default), for cost attribution."""
        from shared.a2a_factory import DEFAULT_MODEL_ID

        return self._model_id or os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID

    def _build_agent(self) -> _StructuredAgent:
        """Build the tools-less Strands agent bound to the resolved model."""
        from strands import Agent

        from shared.a2a_factory import _resolve_model

        model = _resolve_model(model_id_override=self._model_id)
        if self._temperature is not None:
            try:
                model.update_config(temperature=self._temperature)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - depends on Strands model surface
                logger.debug("Could not pin temperature=%s on the model.", self._temperature)
        return Agent(model=model, system_prompt=self._system_prompt)

    def _get_agent(self, *, fail_open: bool) -> _StructuredAgent | None:
        """Return the agent, building it lazily.

        ``fail_open=True`` swallows a build failure and returns ``None``;
        ``fail_open=False`` lets it propagate (for :meth:`call_or_raise`).
        """
        if self._agent is not None:
            return self._agent
        if not fail_open:
            self._agent = self._build_agent()
            return self._agent
        try:
            self._agent = self._build_agent()
        except Exception:
            logger.exception("Failed to build the structured-output agent.")
            return None
        return self._agent

    async def _invoke(self, agent: _StructuredAgent, output_model: type[T], prompt: str) -> T:
        result = await asyncio.wait_for(
            agent.invoke_async(prompt, structured_output_model=output_model),
            timeout=self._timeout_seconds,
        )
        self._record(result)
        return result.structured_output

    def _record(self, result) -> None:
        """Record one call's token usage + cost into the request accumulator."""
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None) if metrics else None
        input_tokens = usage.get("inputTokens") if usage else None
        output_tokens = usage.get("outputTokens") if usage else None
        record_usage(
            input_tokens,
            output_tokens,
            compute_cost_usd(self._effective_model_id(), input_tokens, output_tokens),
        )

    async def call(self, output_model: type[T], prompt: str) -> T | None:
        """Run the call, **fail-open**: any error/timeout/build failure → ``None``."""
        agent = self._get_agent(fail_open=True)
        if agent is None:
            return None
        try:
            return await self._invoke(agent, output_model, prompt)
        except Exception:
            logger.warning(
                "Structured-output call failed; proceeding without it.", exc_info=True
            )
            return None

    async def call_or_raise(self, output_model: type[T], prompt: str) -> T:
        """Run the call, propagating any error/timeout/build failure to the caller."""
        agent = self._get_agent(fail_open=False)
        assert agent is not None  # fail_open=False either returns an agent or raised
        return await self._invoke(agent, output_model, prompt)
