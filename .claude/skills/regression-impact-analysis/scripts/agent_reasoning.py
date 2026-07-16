"""
Agent-driven reasoning helpers (replaces the old request/response file mailbox).

RIA's reasoning stages (method understanding, test-keyword generation, test-case
judgment, semantic dedup) are answered by the GitHub Copilot agent DIRECTLY —
there is NO ``copilot_llm_bridge``, NO ``llm_io/requests``/``responses`` folder,
NO prompt hashing and NO ``CopilotResponsePending`` re-run mailbox.

How it works now
----------------
Each reasoning stage is idempotent and self-describing:

  1. If the stage's OUTPUT file already carries the agent's answer
     (top-level ``_reasoning_source == "copilot-agent"``), the stage keeps it,
     runs any deterministic post-processing, and returns. Re-runs never clobber
     an answer the agent already wrote.

  2. Otherwise the stage writes a *pending baseline* — its normal output file
     populated with the deterministic context the agent needs (diffs, call
     chains, formatted test cases, …) and EMPTY reasoning fields — then prints
     an ``AGENT ACTION REQUIRED`` banner naming exactly which artifact to read
     and which file to write.

The Copilot agent (see ``.github/agents/ria.agent.md``) reads the named
artifacts, reasons in-chat, writes the output JSON with the reasoning fields
filled and ``_reasoning_source: "copilot-agent"``, then re-runs the pipeline.
Because the stages are idempotent, the loop converges in the same 2-3 passes
the old mailbox needed — just without any handoff files.

This module intentionally has no network calls, no credentials and no SDK deps.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

# Sentinel written by the Copilot agent into a stage output file to mark that
# the reasoning fields were filled by the agent (and must be preserved on
# subsequent pipeline re-runs).
AGENT_SOURCE = "copilot-agent"
PENDING_SOURCE = "pending"

_SOURCE_KEY = "_reasoning_source"


class AgentActionRequired(BaseException):
    """Raised to PAUSE the pipeline when a reasoning stage needs the agent.

    Subclasses ``BaseException`` (not ``Exception``) on purpose so the many
    ``except Exception`` guards scattered through the pipeline never swallow
    it — it always unwinds cleanly up to ``main()``, which prints a friendly
    "paused" message and exits 0. The Copilot agent then fills the named
    output file and re-runs with ``--resume`` (which skips the per-run
    cleanup so the answer survives).
    """

    def __init__(self, stage: str, output_path: str | os.PathLike):
        self.stage = stage
        self.output_path = str(output_path)
        super().__init__(
            f"Agent reasoning required for {stage}: fill '{output_path}', "
            f"set _reasoning_source=copilot-agent, then re-run with --resume."
        )


def is_pending(result: Any) -> bool:
    """True when a stage returned a pending baseline awaiting agent reasoning."""
    if not isinstance(result, dict):
        return False
    return (result.get(_SOURCE_KEY) == PENDING_SOURCE
            or bool(result.get("_needs_agent_reasoning")))



def load_json(path: str | os.PathLike) -> Optional[Any]:
    """Best-effort JSON load; returns None if the file is missing/invalid."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def agent_provided(path: str | os.PathLike) -> bool:
    """True when the stage output at ``path`` was filled by the Copilot agent.

    Detection is deliberately simple: the agent sets the top-level
    ``_reasoning_source`` field to ``"copilot-agent"``. A pending baseline
    written by the pipeline uses ``"pending"`` (or omits the key), so this
    returns False for it.
    """
    data = load_json(path)
    if not isinstance(data, dict):
        return False
    return data.get(_SOURCE_KEY) == AGENT_SOURCE


def mark_pending(payload: dict) -> dict:
    """Stamp a payload as a pending baseline awaiting agent reasoning."""
    payload[_SOURCE_KEY] = PENDING_SOURCE
    payload["_needs_agent_reasoning"] = True
    return payload


def normalize_scenarios(raw: Any) -> list:
    """Coerce a ``test_scenarios`` list into the canonical dict form.

    The agent is asked to write each scenario as a dict
    ``{"id","description","priority","rationale"}`` (see
    ``.github/agents/ria.agent.md``), but it sometimes writes plain strings.
    Every consumer (Stage 5 keyword enrichment, Stage 7 judgment, the HTML
    report) calls ``.get()`` on each element, so a bare string raises
    ``'str' object has no attribute 'get'`` and crashes the stage. This
    normaliser guarantees a list of dicts regardless of what the agent wrote:

      * dict elements are preserved, with ``id``/``description``/``priority``
        back-filled if the agent omitted them;
      * string elements are wrapped as
        ``{"id": "S<n>", "description": <string>, "priority": "", "rationale": ""}``
        with a sequential id;
      * empty strings and any other type are dropped.

    It is a total function (never raises) and idempotent — feeding already
    normalised scenarios back through it yields the same list.
    """
    out: list = []
    if not isinstance(raw, list):
        return out
    for i, s in enumerate(raw):
        if isinstance(s, dict):
            d = dict(s)
            if not d.get("id"):
                d["id"] = f"S{i + 1}"
            d.setdefault("description", "")
            d.setdefault("priority", "")
            out.append(d)
        elif isinstance(s, str):
            text = s.strip()
            if not text:
                continue
            out.append({
                "id": f"S{i + 1}",
                "description": text,
                "priority": "",
                "rationale": "",
            })
        # any other type (None, numbers, nested lists) is silently ignored
    return out


def write_json(path: str | os.PathLike, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


# ---------------------------------------------------------------------------
# Live-agent mode ("chef stays in the kitchen")
# ---------------------------------------------------------------------------
# By DEFAULT the pipeline pauses at a reasoning stage by writing a pending
# baseline and exiting (0); the agent fills the file and re-runs with --resume
# (the "chef leaves the kitchen and comes back" model). That path is unchanged.
#
# In LIVE mode the process stays alive: when a reasoning stage is pending it
# emits a single greppable marker line and BLOCKS, polling the output file until
# the agent fills it (marks it copilot-agent) — then the same process continues
# in place, no restart ("chef stays in the kitchen and waits for the manager's
# answer"). On timeout it returns False so the caller falls back to the proven
# pause/exit path, guaranteeing the pipeline can never hang forever.
LIVE_MODE = False
LIVE_TIMEOUT_SEC = 1800  # 30 min hard ceiling before falling back to pause/resume
LIVE_POLL_SEC = 2.0

# Machine-readable marker a watching agent greps for. Kept stable — external
# orchestration (Monitor) keys on this exact prefix.
LIVE_MARKER = "RIA_LIVE_WAIT"


def set_live_mode(enabled: bool, *, timeout_sec: Optional[int] = None,
                  poll_sec: Optional[float] = None) -> None:
    """Enable/disable live in-process agent reasoning (opt-in)."""
    global LIVE_MODE, LIVE_TIMEOUT_SEC, LIVE_POLL_SEC
    LIVE_MODE = bool(enabled)
    if timeout_sec and timeout_sec > 0:
        LIVE_TIMEOUT_SEC = int(timeout_sec)
    if poll_sec and poll_sec > 0:
        LIVE_POLL_SEC = float(poll_sec)


def wait_for_agent_answer(out_path: str | os.PathLike, *, stage: str,
                          timeout_sec: Optional[int] = None,
                          poll_sec: Optional[float] = None) -> bool:
    """Block until the agent fills ``out_path`` (copilot-agent) or times out.

    Emits one ``RIA_LIVE_WAIT stage="..." file="..."`` marker line on stdout so
    a watching agent knows exactly which file to fill, then polls without ever
    re-writing the file itself (the pending baseline the caller already wrote is
    left intact for the agent to read).

    Returns True once the agent's answer is detected, or False on timeout. A
    False return is the caller's signal to fall back to the pause/exit path.
    """
    timeout = int(timeout_sec) if timeout_sec else LIVE_TIMEOUT_SEC
    poll = float(poll_sec) if poll_sec else LIVE_POLL_SEC
    out_path = str(out_path)
    # Single, stable, greppable signal for the watching agent (Monitor).
    print(f'\n{LIVE_MARKER} stage="{stage}" file="{out_path}"', flush=True)
    print(f"[live-agent] Kitchen stays open — waiting for the agent to fill "
          f"{out_path} (timeout {timeout}s, poll {poll}s).", flush=True)
    waited = 0.0
    while waited < timeout:
        # agent_provided() best-effort-loads JSON and returns False on a
        # missing/partial/invalid file, so a torn read during the agent's
        # write simply costs one more poll cycle — never a crash.
        if agent_provided(out_path):
            print(f"[live-agent] Answer received for {stage}; "
                  f"resuming in-process (no restart).", flush=True)
            return True
        time.sleep(poll)
        waited += poll
    print(f"[live-agent] TIMEOUT after {timeout}s waiting for {stage}; "
          f"falling back to pause/resume (write the file and re-run "
          f"--resume).", flush=True)
    return False


def print_action_required(*, stage: str, reads: list[str], writes: str,
                          instructions: str) -> None:
    """Print a clear, machine-and-human readable ACTION-REQUIRED banner.

    The Copilot agent watches for this banner in pipeline output. It names the
    input artifacts to read and the single output file to write.
    """
    bar = "=" * 72
    print("\n" + bar)
    print(f"AGENT ACTION REQUIRED — {stage}")
    print(bar)
    print("READ (existing artifacts):")
    for r in reads:
        print(f"  - {r}")
    print(f"WRITE (fill reasoning fields, set \"_reasoning_source\": \"{AGENT_SOURCE}\"):")
    print(f"  - {writes}")
    print("INSTRUCTIONS:")
    for line in instructions.strip().splitlines():
        print(f"  {line.strip()}")
    print(bar + "\n")
