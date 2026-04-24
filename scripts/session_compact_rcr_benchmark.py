#!/usr/bin/env python3

"""Session-compact retention benchmark tools for latest-upstream Codex.

This script now covers three layers:

1. deterministic corpus validation, probe rendering, and scoring
2. isolated copied-session lab setup
3. single-hop prepare/probe execution for `source` vs `session`
4. minimal chained continued-auto execution for `chain_control` vs `session`
5. per-hop working-state continuity probes layered onto chained execution
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_SRC = REPO_ROOT / "sdk" / "python" / "src"
LEGACY_REPO_ROOT = REPO_ROOT.parent / "codex"
DEFAULT_CASE_FILE = REPO_ROOT / "benchmarks" / "session_compact_rcr_cases.json"
DEFAULT_LAB_ROOT = REPO_ROOT / ".codex-session-compact-lab"
DEFAULT_COPY_FILES = ("config.toml", "auth.json", "hooks.json", "AGENTS.md")
DEFAULT_VARIANTS = ("source", "session")
DEFAULT_CHAIN_VARIANTS = ("chain_control", "session")
CHAIN_VARIANT_CHOICES = (*DEFAULT_CHAIN_VARIANTS, "legacy_collapse")
DEFAULT_MODEL_CONTEXT_WINDOW = 1_050_000
DEFAULT_MODEL_AUTO_COMPACT_TOKEN_LIMIT = 970_000
DEFAULT_CHAIN_HOPS = 1
DEFAULT_CHAIN_MODEL_CONTEXT_WINDOW = 45_000
DEFAULT_CHAIN_MODEL_AUTO_COMPACT_TOKEN_LIMIT = 24_000
DEFAULT_LEGACY_COLLAPSE_PRESERVE_TURNS = 5
DEFAULT_CHAIN_PROMPT_PAYLOAD_LINES = 900
CHAIN_PROBE_READY_STATUSES = {
    "midturn_compaction_observed",
    "task_completed_without_target_compaction",
    "completed_without_target_compaction_tracking",
}
CHAIN_PROMPT_PAYLOAD_LINE = (
    "CHAIN_PAYLOAD keep the continuation deterministic, plain-text-only, and large enough "
    "to stress repeated compaction without tool-runtime noise."
)
COMPACT_SETTLE_SECONDS = 4.0
SUMMARY_PREFIX_PATH = (
    REPO_ROOT / "codex-rs" / "core" / "templates" / "compact" / "summary_prefix.md"
)
FALLBACK_SUMMARY_PREFIXES = (
    "The following is a summary of the previous conversation:",
)

ABSTAIN_ANSWERS = {
    "",
    "unknown",
    "not sure",
    "uncertain",
    "cannot determine",
    "can't determine",
    "unable to determine",
    "不知道",
    "不确定",
    "无法确定",
}


def build_default_chain_prompt() -> str:
    payload = "\n".join([CHAIN_PROMPT_PAYLOAD_LINE] * DEFAULT_CHAIN_PROMPT_PAYLOAD_LINES)
    return (
        "Do not call any tools. Read the payload below and reply with JSON only: "
        '{"status":"done","mode":"plain_text_payload"}.\n\n'
        "Payload follows:\n"
        f"{payload}"
    )


DEFAULT_CHAIN_PROMPT = build_default_chain_prompt()

WORKING_STATE_BUNDLES = (
    {
        "file": "codex-rs/core/src/compact_session.rs",
        "next_step": "tune the active work-surface frontier selector",
        "blocker": "context window pressure near auto compact",
        "command": "cargo test -p codex-core compact_session -- --nocapture",
    },
    {
        "file": "codex-rs/core/src/compact_frontier.rs",
        "next_step": "verify the follow-up frontier request shape",
        "blocker": "oldest history trimming before compact request",
        "command": "cargo test -p codex-core session_strategy_manual_compact_follow_up_keeps_structured_recent_frontier -- --nocapture",
    },
    {
        "file": "codex-rs/core/tests/suite/compact_remote.rs",
        "next_step": "check the route-level compact request markers",
        "blocker": "recent frontier losing tool outputs",
        "command": "cargo test -p codex-core session_strategy_manual_compact_sends_full_precompact_history_until_context_limit -- --nocapture",
    },
    {
        "file": "docs/session-compact-rcr-benchmark.md",
        "next_step": "review the working-state probe summary",
        "blocker": "working-state parity drift after compaction",
        "command": "python3 scripts/session_compact_rcr_benchmark.py run-chain --variant session",
    },
)


def build_working_state_bundle(hop_index: int) -> dict[str, Any]:
    bundle = dict(WORKING_STATE_BUNDLES[(hop_index - 1) % len(WORKING_STATE_BUNDLES)])
    bundle["hop_index"] = hop_index
    return bundle


def render_working_state_chain_preamble(bundle: dict[str, Any]) -> str:
    return (
        "Most recent active work bundle for continuity testing:\n"
        f"WORKING_STATE_HOP: {bundle['hop_index']}\n"
        f"WORKING_STATE_FILE: {bundle['file']}\n"
        f"WORKING_STATE_NEXT_STEP: {bundle['next_step']}\n"
        f"WORKING_STATE_BLOCKER: {bundle['blocker']}\n"
        f"WORKING_STATE_COMMAND: {bundle['command']}\n"
    )


def build_chain_prompt_for_hop(base_prompt: str, hop_index: int) -> tuple[str, dict[str, Any]]:
    bundle = build_working_state_bundle(hop_index)
    prompt = (
        render_working_state_chain_preamble(bundle)
        + "\n"
        + "Keep the bundle above recoverable from the recent active work surface.\n\n"
        + base_prompt
    )
    return prompt, bundle


def build_working_state_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file": {"type": "string"},
            "next_step": {"type": "string"},
            "blocker": {"type": "string"},
            "command": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["file", "next_step", "blocker", "command", "reason"],
    }


def render_working_state_probe(bundle: dict[str, Any]) -> str:
    return (
        "You are resuming immediately after the most recent continuation hop.\n"
        "Answer only from the most recent active work bundle carried by this thread.\n"
        "Do not answer from generic coding habits.\n"
        f"The most recent hop index should be {bundle['hop_index']}.\n"
        "Return JSON only with the exact file, next_step, blocker, and command.\n"
    )


def score_working_state_probe(bundle: dict[str, Any], response_text: str) -> ScoreResult:
    parsed, parse_error = parse_json_response(response_text)
    if parse_error or parsed is None:
        return ScoreResult(
            score=0.0,
            verdict="parse_error",
            hard_fail=False,
            details={"parse_error": parse_error or "unknown"},
        )

    expected_fields = ("file", "next_step", "blocker", "command")
    matched_fields = 0
    field_results: dict[str, Any] = {}
    hard_fail = False
    for field in expected_fields:
        expected = normalize_text(bundle.get(field))
        actual = normalize_text(parsed.get(field))
        matched = actual == expected
        matched_fields += int(matched)
        field_results[field] = {
            "expected": bundle.get(field),
            "actual": parsed.get(field),
            "matched": matched,
        }
        if field in {"file", "next_step"} and actual and not matched:
            hard_fail = True

    score = matched_fields / len(expected_fields)
    verdict = "pass" if matched_fields == len(expected_fields) else "fail"
    return ScoreResult(
        score=score,
        verdict=verdict,
        hard_fail=hard_fail,
        details=field_results,
    )


@dataclass
class ScoreResult:
    score: float
    verdict: str
    hard_fail: bool
    details: dict[str, Any]


@dataclass
class CopiedRollout:
    source: str
    destination: str
    size_bytes: int


@dataclass
class RolloutScan:
    total_lines: int
    compacted_items: int
    context_compacted_items: int
    task_started_items: int
    task_complete_items: int
    latest_compacted_timestamp: str | None
    latest_task_started_turn_id: str | None
    latest_task_complete_turn_id: str | None


@dataclass
class CompactFollowResult:
    status: str
    elapsed_seconds: float
    new_lines: int
    new_compacted_items: int
    new_context_compacted_items: int
    new_task_started_items: int
    new_task_complete_items: int
    compact_turn_id: str | None
    task_complete_turn_id: str | None
    saw_new_compacted: bool
    saw_new_context_compacted: bool


@dataclass
class RolloutMetrics:
    path: str
    file_size_bytes: int
    total_lines: int
    response_items: int
    event_msgs: int
    turn_contexts: int
    compacted_items: int
    latest_compacted_timestamp: str | None
    latest_replacement_history_items: int
    latest_replacement_history_est_tokens: int
    latest_replacement_history_total_est_tokens: int
    latest_compacted_message_est_tokens: int
    latest_replacement_history_type_counts: dict[str, int]
    latest_replacement_history_message_role_counts: dict[str, int]
    latest_replacement_history_summary_messages: int
    latest_replacement_history_tail_types: list[str]
    latest_replacement_history_tail_roles: list[str]
    latest_compacted_signature: dict[str, Any] | None


@dataclass
class PreparedVariant:
    variant: str
    source_thread_id: str
    source_rollout_path: str | None
    source_metrics: dict[str, Any] | None
    prepared_thread_id: str
    prepared_rollout_path: str
    initial_scan: dict[str, Any]
    final_scan: dict[str, Any]
    initial_metrics: dict[str, Any]
    final_metrics: dict[str, Any]
    compact_follow_result: dict[str, Any] | None
    compaction_delta: dict[str, Any] | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summary_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    if SUMMARY_PREFIX_PATH.exists():
        text = SUMMARY_PREFIX_PATH.read_text(encoding="utf-8").strip()
        if text:
            prefixes.append(text)
    prefixes.extend(prefix for prefix in FALLBACK_SUMMARY_PREFIXES if prefix not in prefixes)
    return tuple(prefixes)


SUMMARY_PREFIXES = summary_prefixes()


def resolve_default_codex_bin(repo_root: Path = REPO_ROOT) -> Path:
    candidates = [
        repo_root / "codex-rs" / "target" / "debug" / "codex",
        repo_root / "codex-rs" / "target" / "release" / "codex",
        repo_root / "target" / "debug" / "codex",
        repo_root / "target" / "release" / "codex",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    on_path = shutil.which("codex")
    if on_path:
        return Path(on_path).resolve()
    return candidates[0]


def resolve_requested_codex_bin(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.exists():
        return candidate.resolve()
    on_path = shutil.which(value)
    if on_path:
        return Path(on_path).resolve()
    return candidate.resolve()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return True


def find_rollouts(source_home: Path, min_size_bytes: int) -> list[Path]:
    sessions_root = source_home / "sessions"
    if not sessions_root.exists():
        return []
    return sorted(
        (
            path
            for path in sessions_root.rglob("rollout-*.jsonl")
            if path.is_file() and path.stat().st_size >= min_size_bytes
        ),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )


def write_env_script(lab_root: Path, home_dir: Path, user_home: Path) -> Path:
    env_script = lab_root / "env.sh"
    contents = "\n".join(
        [
            "#!/usr/bin/env bash",
            "# Source this file before running the lab benchmark commands.",
            f'export CODEX_HOME="{home_dir}"',
            f'export HOME="{user_home}"',
            'export XDG_CONFIG_HOME="$HOME/.config"',
            'export XDG_CACHE_HOME="$HOME/.cache"',
            'export XDG_STATE_HOME="$HOME/.local/state"',
            'export XDG_DATA_HOME="$HOME/.local/share"',
            "",
        ]
    )
    env_script.write_text(contents, encoding="utf-8")
    env_script.chmod(0o755)
    return env_script


def build_lab_env(lab_root: Path) -> dict[str, str]:
    lab_home = lab_root / "home"
    lab_user_home = lab_root / "user-home"
    return {
        "CODEX_HOME": str(lab_home),
        "HOME": str(lab_user_home),
        "XDG_CONFIG_HOME": str(lab_user_home / ".config"),
        "XDG_CACHE_HOME": str(lab_user_home / ".cache"),
        "XDG_STATE_HOME": str(lab_user_home / ".local" / "state"),
        "XDG_DATA_HOME": str(lab_user_home / ".local" / "share"),
    }


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // 4)


def estimate_tokens_from_json(value: object) -> int:
    if value is None:
        return 0
    return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def extract_text_from_content_items(content: list[dict[str, Any]]) -> int:
    total = 0
    for item in content:
        text = item.get("text")
        if isinstance(text, str):
            total += estimate_tokens(text)
        thinking = item.get("thinking")
        if isinstance(thinking, str):
            total += estimate_tokens(thinking)
        data = item.get("data")
        if isinstance(data, str):
            total += estimate_tokens(data)
    return total


def extract_text_from_message(payload: dict[str, Any]) -> int:
    return extract_text_from_content_items(payload.get("content") or [])


def extract_text_from_reasoning(payload: dict[str, Any]) -> int:
    total = 0
    for item in payload.get("summary") or []:
        text = item.get("text")
        if isinstance(text, str):
            total += estimate_tokens(text)
    content = payload.get("content")
    if isinstance(content, list):
        total += extract_text_from_content_items(content)
    return total


def extract_text_from_output_payload(payload: object) -> int:
    if isinstance(payload, str):
        return estimate_tokens(payload)
    if isinstance(payload, list):
        return extract_text_from_content_items(
            [item for item in payload if isinstance(item, dict)]
        )
    return 0


def estimate_replacement_history_item_tokens(item: dict[str, Any]) -> int:
    item_type = item.get("type")
    if item_type == "message":
        return extract_text_from_message(item)
    if item_type == "reasoning":
        return extract_text_from_reasoning(item)
    if item_type == "function_call":
        total = estimate_tokens(item.get("name") or "")
        namespace = item.get("namespace")
        if isinstance(namespace, str):
            total += estimate_tokens(namespace)
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            total += estimate_tokens(arguments)
        return total
    if item_type == "custom_tool_call":
        return estimate_tokens(item.get("name") or "") + estimate_tokens(item.get("input") or "")
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        total = 0
        name = item.get("name")
        if isinstance(name, str):
            total += estimate_tokens(name)
        total += extract_text_from_output_payload(item.get("output"))
        return total
    if item_type == "tool_search_call":
        return estimate_tokens(item.get("execution") or "") + estimate_tokens_from_json(
            item.get("arguments")
        )
    if item_type == "tool_search_output":
        return estimate_tokens(item.get("execution") or "") + estimate_tokens_from_json(
            item.get("tools")
        )
    if item_type == "web_search_call":
        return estimate_tokens_from_json(item.get("action"))
    if item_type == "local_shell_call":
        return estimate_tokens_from_json(item.get("action"))
    if item_type == "image_generation_call":
        return estimate_tokens(item.get("revised_prompt") or "") + estimate_tokens(
            item.get("result") or ""
        )
    if item_type == "compaction":
        return 0
    return 0


def extract_replacement_history_tokens(payload: dict[str, Any]) -> int:
    history = payload.get("replacement_history") or []
    total = 0
    for item in history:
        if item.get("type") == "message":
            total += extract_text_from_message(item)
    return total


def extract_replacement_history_total_tokens(payload: dict[str, Any]) -> int:
    history = payload.get("replacement_history") or []
    total = 0
    for item in history:
        if isinstance(item, dict):
            total += estimate_replacement_history_item_tokens(item)
    return total


def extract_replacement_history_type_counts(history: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for item in history:
        counts[item.get("type") or "unknown"] += 1
    return dict(sorted(counts.items()))


def extract_replacement_history_message_role_counts(
    history: list[dict[str, Any]],
) -> dict[str, int]:
    counts = Counter()
    for item in history:
        if item.get("type") == "message":
            counts[item.get("role") or "unknown"] += 1
    return dict(sorted(counts.items()))


def extract_summary_message_count(history: list[dict[str, Any]]) -> int:
    count = 0
    for item in history:
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        for content_item in item.get("content") or []:
            text = content_item.get("text")
            if isinstance(text, str) and any(text.startswith(prefix) for prefix in SUMMARY_PREFIXES):
                count += 1
                break
    return count


def is_summary_message_item(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    for content_item in item.get("content") or []:
        text = content_item.get("text")
        if isinstance(text, str) and any(text.startswith(prefix) for prefix in SUMMARY_PREFIXES):
            return True
    return False


def summarize_compacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    history = payload.get("replacement_history") or []
    type_counts = Counter()
    role_counts = Counter()
    summary_positions: list[int] = []
    tool_item_count = 0

    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or "unknown"
        type_counts[item_type] += 1
        if item_type == "message":
            role_counts[item.get("role") or "unknown"] += 1
        if item_type not in {"message", "reasoning"}:
            tool_item_count += 1
        if is_summary_message_item(item):
            summary_positions.append(index)

    last_item = history[-1] if history else {}
    last_item_type = last_item.get("type") if isinstance(last_item, dict) else None
    last_item_role = (
        last_item.get("role")
        if isinstance(last_item, dict) and last_item.get("type") == "message"
        else None
    )

    return {
        "replacement_history_items": len(history),
        "replacement_history_type_counts": dict(sorted(type_counts.items())),
        "replacement_history_message_role_counts": dict(sorted(role_counts.items())),
        "summary_message_count": len(summary_positions),
        "summary_positions": summary_positions,
        "last_item_type": last_item_type,
        "last_item_role": last_item_role,
        "last_item_is_summary": isinstance(last_item, dict) and is_summary_message_item(last_item),
        "tail_types": [
            item.get("type") or "unknown"
            for item in history[-12:]
            if isinstance(item, dict)
        ],
        "tail_roles": [
            item.get("role") or "unknown"
            for item in history[-12:]
            if isinstance(item, dict) and item.get("type") == "message"
        ],
        "has_tool_items": tool_item_count > 0,
        "tool_item_count": tool_item_count,
    }


def analyze_rollout(path: Path) -> RolloutMetrics:
    response_items = 0
    event_msgs = 0
    turn_contexts = 0
    compacted_items = 0
    latest_compacted_timestamp = None
    latest_replacement_history_items = 0
    latest_replacement_history_est_tokens = 0
    latest_replacement_history_total_est_tokens = 0
    latest_compacted_message_est_tokens = 0
    latest_replacement_history_type_counts: dict[str, int] = {}
    latest_replacement_history_message_role_counts: dict[str, int] = {}
    latest_replacement_history_summary_messages = 0
    latest_replacement_history_tail_types: list[str] = []
    latest_replacement_history_tail_roles: list[str] = []
    latest_compacted_signature: dict[str, Any] | None = None
    total_lines = 0

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "response_item":
                response_items += 1
            elif kind == "event_msg":
                event_msgs += 1
            elif kind == "turn_context":
                turn_contexts += 1
            elif kind == "compacted":
                compacted_items += 1
                latest_compacted_timestamp = record.get("timestamp")
                history = payload.get("replacement_history") or []
                latest_replacement_history_items = len(history)
                latest_replacement_history_est_tokens = extract_replacement_history_tokens(payload)
                latest_replacement_history_total_est_tokens = (
                    extract_replacement_history_total_tokens(payload)
                )
                latest_compacted_message_est_tokens = estimate_tokens(payload.get("message") or "")
                latest_replacement_history_type_counts = (
                    extract_replacement_history_type_counts(history)
                )
                latest_replacement_history_message_role_counts = (
                    extract_replacement_history_message_role_counts(history)
                )
                latest_replacement_history_summary_messages = extract_summary_message_count(history)
                latest_replacement_history_tail_types = [
                    item.get("type") or "unknown"
                    for item in history[-12:]
                    if isinstance(item, dict)
                ]
                latest_replacement_history_tail_roles = [
                    item.get("role") or "unknown"
                    for item in history[-12:]
                    if isinstance(item, dict) and item.get("type") == "message"
                ]
                latest_compacted_signature = summarize_compacted_payload(payload)

    return RolloutMetrics(
        path=str(path),
        file_size_bytes=path.stat().st_size,
        total_lines=total_lines,
        response_items=response_items,
        event_msgs=event_msgs,
        turn_contexts=turn_contexts,
        compacted_items=compacted_items,
        latest_compacted_timestamp=latest_compacted_timestamp,
        latest_replacement_history_items=latest_replacement_history_items,
        latest_replacement_history_est_tokens=latest_replacement_history_est_tokens,
        latest_replacement_history_total_est_tokens=latest_replacement_history_total_est_tokens,
        latest_compacted_message_est_tokens=latest_compacted_message_est_tokens,
        latest_replacement_history_type_counts=latest_replacement_history_type_counts,
        latest_replacement_history_message_role_counts=latest_replacement_history_message_role_counts,
        latest_replacement_history_summary_messages=latest_replacement_history_summary_messages,
        latest_replacement_history_tail_types=latest_replacement_history_tail_types,
        latest_replacement_history_tail_roles=latest_replacement_history_tail_roles,
        latest_compacted_signature=latest_compacted_signature,
    )


def summarize_compaction_delta(
    initial_metrics: dict[str, Any],
    final_metrics: dict[str, Any],
) -> dict[str, Any]:
    before_items = int(initial_metrics.get("latest_replacement_history_items", 0) or 0)
    after_items = int(final_metrics.get("latest_replacement_history_items", 0) or 0)
    before_total_tokens = int(
        initial_metrics.get("latest_replacement_history_total_est_tokens", 0) or 0
    )
    after_total_tokens = int(
        final_metrics.get("latest_replacement_history_total_est_tokens", 0) or 0
    )
    before_visible_tokens = int(
        initial_metrics.get("latest_replacement_history_est_tokens", 0) or 0
    )
    after_visible_tokens = int(
        final_metrics.get("latest_replacement_history_est_tokens", 0) or 0
    )

    return {
        "replacement_history_items_before": before_items,
        "replacement_history_items_after": after_items,
        "replacement_history_items_delta": after_items - before_items,
        "replacement_history_items_ratio": (
            after_items / before_items if before_items > 0 else None
        ),
        "replacement_history_total_est_tokens_before": before_total_tokens,
        "replacement_history_total_est_tokens_after": after_total_tokens,
        "replacement_history_total_est_tokens_delta": after_total_tokens - before_total_tokens,
        "replacement_history_total_est_tokens_ratio": (
            after_total_tokens / before_total_tokens if before_total_tokens > 0 else None
        ),
        "replacement_history_visible_est_tokens_before": before_visible_tokens,
        "replacement_history_visible_est_tokens_after": after_visible_tokens,
        "replacement_history_visible_est_tokens_delta": (
            after_visible_tokens - before_visible_tokens
        ),
        "replacement_history_visible_est_tokens_ratio": (
            after_visible_tokens / before_visible_tokens if before_visible_tokens > 0 else None
        ),
        "signature_before": initial_metrics.get("latest_compacted_signature"),
        "signature_after": final_metrics.get("latest_compacted_signature"),
    }


def scan_rollout(path: Path) -> RolloutScan:
    total_lines = 0
    compacted_items = 0
    context_compacted_items = 0
    task_started_items = 0
    task_complete_items = 0
    latest_compacted_timestamp = None
    latest_task_started_turn_id = None
    latest_task_complete_turn_id = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            record_type = record.get("type")
            if record_type == "compacted":
                compacted_items += 1
                latest_compacted_timestamp = record.get("timestamp")
                continue
            if record_type == "context_compacted":
                context_compacted_items += 1
                continue
            if record_type != "event_msg":
                continue
            payload = record.get("payload") or {}
            payload_type = payload.get("type")
            if payload_type == "task_started":
                task_started_items += 1
                latest_task_started_turn_id = payload.get("turn_id")
            elif payload_type == "task_complete":
                task_complete_items += 1
                latest_task_complete_turn_id = payload.get("turn_id")

    return RolloutScan(
        total_lines=total_lines,
        compacted_items=compacted_items,
        context_compacted_items=context_compacted_items,
        task_started_items=task_started_items,
        task_complete_items=task_complete_items,
        latest_compacted_timestamp=latest_compacted_timestamp,
        latest_task_started_turn_id=latest_task_started_turn_id,
        latest_task_complete_turn_id=latest_task_complete_turn_id,
    )


def follow_compact(
    path: Path,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> CompactFollowResult:
    start = time.monotonic()
    last_size = path.stat().st_size
    new_lines = 0
    new_compacted_items = 0
    new_context_compacted_items = 0
    new_task_started_items = 0
    new_task_complete_items = 0
    compact_turn_id = None
    task_complete_turn_id = None
    saw_new_compacted = False
    saw_new_context_compacted = False

    while True:
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(last_size)
            while True:
                line = handle.readline()
                if not line:
                    break
                last_size = handle.tell()
                new_lines += 1
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                record_type = record.get("type")
                if record_type == "compacted":
                    saw_new_compacted = True
                    new_compacted_items += 1
                    continue
                if record_type == "context_compacted":
                    saw_new_context_compacted = True
                    new_context_compacted_items += 1
                    continue
                if record_type != "event_msg":
                    continue
                payload = record.get("payload") or {}
                payload_type = payload.get("type")
                if payload_type == "task_started":
                    new_task_started_items += 1
                    compact_turn_id = payload.get("turn_id")
                elif payload_type == "task_complete":
                    new_task_complete_items += 1
                    task_complete_turn_id = payload.get("turn_id")
                    if compact_turn_id is None or task_complete_turn_id == compact_turn_id:
                        status = (
                            "completed"
                            if saw_new_compacted
                            else "completed_without_new_compacted"
                        )
                        return CompactFollowResult(
                            status=status,
                            elapsed_seconds=time.monotonic() - start,
                            new_lines=new_lines,
                            new_compacted_items=new_compacted_items,
                            new_context_compacted_items=new_context_compacted_items,
                            new_task_started_items=new_task_started_items,
                            new_task_complete_items=new_task_complete_items,
                            compact_turn_id=compact_turn_id,
                            task_complete_turn_id=task_complete_turn_id,
                            saw_new_compacted=saw_new_compacted,
                            saw_new_context_compacted=saw_new_context_compacted,
                        )

        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            status = (
                "timeout_after_task_started"
                if new_task_started_items > 0
                else "timeout_without_task_started"
            )
            return CompactFollowResult(
                status=status,
                elapsed_seconds=elapsed,
                new_lines=new_lines,
                new_compacted_items=new_compacted_items,
                new_context_compacted_items=new_context_compacted_items,
                new_task_started_items=new_task_started_items,
                new_task_complete_items=new_task_complete_items,
                compact_turn_id=compact_turn_id,
                task_complete_turn_id=task_complete_turn_id,
                saw_new_compacted=saw_new_compacted,
                saw_new_context_compacted=saw_new_context_compacted,
            )

        time.sleep(poll_interval_seconds)


def scan_new_records(path: Path, start_line: int) -> dict[str, Any]:
    appended_lines = 0
    new_compacted_items = 0
    new_context_compacted_items = 0
    new_task_started_items = 0
    new_task_complete_items = 0
    new_response_items = 0
    latest_new_compacted: dict[str, Any] | None = None
    latest_task_started_turn_id = None
    latest_task_complete_turn_id = None

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number <= start_line:
                continue
            appended_lines += 1
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            record_type = record.get("type")
            payload = record.get("payload") or {}
            if record_type == "compacted":
                new_compacted_items += 1
                latest_new_compacted = {
                    "timestamp": record.get("timestamp"),
                    "message_est_tokens": estimate_tokens(payload.get("message") or ""),
                    "replacement_history_est_tokens": extract_replacement_history_tokens(payload),
                    "replacement_history_total_est_tokens": extract_replacement_history_total_tokens(
                        payload
                    ),
                    "signature": summarize_compacted_payload(payload),
                }
                continue
            if record_type == "context_compacted":
                new_context_compacted_items += 1
                continue
            if record_type == "response_item":
                new_response_items += 1
                continue
            if record_type != "event_msg":
                continue
            payload_type = payload.get("type")
            if payload_type == "task_started":
                new_task_started_items += 1
                latest_task_started_turn_id = payload.get("turn_id")
            elif payload_type == "task_complete":
                new_task_complete_items += 1
                latest_task_complete_turn_id = payload.get("turn_id")

    return {
        "start_line": start_line,
        "appended_lines": appended_lines,
        "new_compacted_items": new_compacted_items,
        "new_context_compacted_items": new_context_compacted_items,
        "new_task_started_items": new_task_started_items,
        "new_task_complete_items": new_task_complete_items,
        "new_response_items": new_response_items,
        "latest_new_compacted": latest_new_compacted,
        "latest_task_started_turn_id": latest_task_started_turn_id,
        "latest_task_complete_turn_id": latest_task_complete_turn_id,
    }


def wait_for_target_auto_compaction(
    path: Path,
    *,
    start_line: int,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        appended_records = scan_new_records(path, start_line)
        if appended_records["new_compacted_items"] > 0:
            return {
                "status": "midturn_compaction_observed",
                "elapsed_seconds": time.monotonic() - start,
                "appended_records": appended_records,
            }
        if appended_records["new_task_complete_items"] > 0:
            return {
                "status": "task_completed_without_target_compaction",
                "elapsed_seconds": time.monotonic() - start,
                "appended_records": appended_records,
            }
        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            status = (
                "timeout_after_task_started"
                if appended_records["new_task_started_items"] > 0
                else "timeout_without_task_started"
            )
            return {
                "status": status,
                "elapsed_seconds": elapsed,
                "appended_records": appended_records,
            }
        time.sleep(poll_interval_seconds)


def load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} does not contain a top-level object")
    if not isinstance(payload.get("cases"), list):
        raise SystemExit(f"{path} does not contain a top-level 'cases' array")
    return payload


def load_cases(path: Path, selected_case_ids: set[str] | None = None) -> list[dict[str, Any]]:
    payload = load_payload(path)
    cases = payload["cases"]
    filtered: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise SystemExit(f"{path} contains a non-object case entry")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise SystemExit(f"{path} contains a case without a valid id")
        if selected_case_ids and case_id not in selected_case_ids:
            continue
        filtered.append(case)

    if selected_case_ids:
        seen = {case["id"] for case in filtered}
        missing = sorted(selected_case_ids - seen)
        if missing:
            raise SystemExit(f"case ids not found in {path}: {', '.join(missing)}")
    if not filtered:
        raise SystemExit("no benchmark cases selected")
    return filtered


def load_source_thread_profiles(path: Path) -> dict[str, dict[str, Any]]:
    payload = load_payload(path)
    profiles = payload.get("source_thread_profiles") or {}
    if not isinstance(profiles, dict):
        raise SystemExit(f"{path} has invalid source_thread_profiles metadata")
    normalized: dict[str, dict[str, Any]] = {}
    for source_thread_id, profile in profiles.items():
        if not isinstance(source_thread_id, str) or not source_thread_id.strip():
            raise SystemExit(f"{path} has an invalid source_thread_profiles key")
        if not isinstance(profile, dict):
            raise SystemExit(f"{path} has a non-object source_thread profile for {source_thread_id}")
        normalized[source_thread_id] = profile
    return normalized


def validate_case(case: dict[str, Any]) -> None:
    required_string_fields = [
        "id",
        "source_thread_id",
        "category",
        "priority",
        "evidence_summary",
        "response_kind",
        "probe_question",
    ]
    for field in required_string_fields:
        value = case.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"case {case.get('id', '<unknown>')} is missing string field {field}")

    response_kind = case["response_kind"]
    if response_kind == "single_choice":
        choices = case.get("choices")
        expected_choice = case.get("expected_choice")
        if not isinstance(choices, dict) or not choices:
            raise SystemExit(f"case {case['id']} is missing choices")
        if not isinstance(expected_choice, str) or expected_choice not in choices:
            raise SystemExit(f"case {case['id']} has invalid expected_choice")
    elif response_kind == "short_answer":
        accepted_answers = case.get("accepted_answers")
        if not isinstance(accepted_answers, list) or not accepted_answers:
            raise SystemExit(f"case {case['id']} is missing accepted_answers")
    else:
        raise SystemExit(f"case {case['id']} has unsupported response_kind {response_kind}")


def validate_payload(path: Path) -> dict[str, Any]:
    payload = load_payload(path)
    for case in payload["cases"]:
        validate_case(case)
    return payload


def aggregate_case_stats(cases: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts = Counter()
    priority_counts = Counter()
    source_counts = Counter()
    for case in cases:
        category_counts[case["category"]] += 1
        priority_counts[case["priority"]] += 1
        source_counts[case["source_thread_id"]] += 1
    return {
        "case_count": len(cases),
        "category_counts": dict(sorted(category_counts.items())),
        "priority_counts": dict(sorted(priority_counts.items())),
        "source_thread_counts": dict(sorted(source_counts.items())),
    }


def summarize_source_thread_profiles(
    source_thread_profiles: dict[str, dict[str, Any]],
    selected_source_thread_ids: set[str],
) -> dict[str, Any]:
    selected_profiles = {
        source_thread_id: source_thread_profiles.get(source_thread_id, {})
        for source_thread_id in sorted(selected_source_thread_ids)
    }
    acceptance_source_threads = [
        source_thread_id
        for source_thread_id, profile in selected_profiles.items()
        if profile.get("classification") != "shape_harness_outlier"
    ]
    outlier_source_threads = [
        source_thread_id
        for source_thread_id, profile in selected_profiles.items()
        if profile.get("classification") == "shape_harness_outlier"
    ]
    return {
        "profiles": selected_profiles,
        "acceptance_source_threads": acceptance_source_threads,
        "outlier_source_threads": outlier_source_threads,
    }


def build_output_schema(case: dict[str, Any]) -> dict[str, Any]:
    response_kind = case["response_kind"]
    if response_kind == "single_choice":
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "selected_option": {
                    "type": "string",
                    "enum": sorted(str(key) for key in case["choices"].keys()),
                },
                "reason": {"type": "string"},
            },
            "required": ["selected_option", "reason"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "known": {"type": "boolean"},
            "answer": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["known", "answer", "reason"],
    }


def render_probe(case: dict[str, Any]) -> str:
    evidence_summary = case["evidence_summary"]
    probe_question = case["probe_question"]
    if case["response_kind"] == "single_choice":
        choices = "\n".join(
            f"{key}. {value}" for key, value in sorted(case["choices"].items())
        )
        return (
            "You are resuming the same project after a long session history.\n"
            "Use the prior thread requirements only; do not answer from generic preference.\n"
            f"Historical requirement under test: {evidence_summary}\n"
            f"Question: {probe_question}\n"
            "Choose exactly one option and return JSON only.\n"
            f"{choices}\n"
        )
    return (
        "You are resuming the same project after a long session history.\n"
        "Use the prior thread requirements only; do not answer from generic preference.\n"
        f"Historical requirement under test: {evidence_summary}\n"
        f"Question: {probe_question}\n"
        "Return JSON only. If the exact answer is unavailable, set known=false and answer=\"\".\n"
    )


def parse_json_response(text: str) -> tuple[dict[str, Any] | None, str | None]:
    stripped = text.strip()
    if not stripped:
        return None, "empty_response"
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and start < end:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data, None
    return None, "json_parse_failed"


def score_single_choice(case: dict[str, Any], parsed: dict[str, Any]) -> ScoreResult:
    choice = str(parsed.get("selected_option", "")).strip()
    expected = str(case["expected_choice"]).strip()
    hard_fail = choice in {str(value) for value in case.get("hard_fail_choices", [])}
    score = 1.0 if choice == expected else 0.0
    verdict = "pass" if score == 1.0 else "fail"
    return ScoreResult(
        score=score,
        verdict=verdict,
        hard_fail=hard_fail,
        details={"selected_option": choice, "expected_choice": expected},
    )


def is_short_answer_abstention(parsed: dict[str, Any]) -> bool:
    if parsed.get("known") is False:
        return True
    return normalize_text(parsed.get("answer")) in ABSTAIN_ANSWERS


def matches_short_answer(answer: str, accepted_answer: str) -> bool:
    return normalize_text(answer) == normalize_text(accepted_answer)


def score_short_answer(case: dict[str, Any], parsed: dict[str, Any]) -> ScoreResult:
    answer = str(parsed.get("answer", "") or "").strip()
    accepted_answers = [str(value) for value in case.get("accepted_answers", [])]
    if any(matches_short_answer(answer, accepted) for accepted in accepted_answers):
        return ScoreResult(
            score=1.0,
            verdict="pass",
            hard_fail=False,
            details={"answer": answer, "accepted_answers": accepted_answers},
        )

    if is_short_answer_abstention(parsed):
        score = float(case.get("abstain_score", 0.0))
        return ScoreResult(
            score=score,
            verdict="abstain",
            hard_fail=False,
            details={"answer": answer, "accepted_answers": accepted_answers},
        )

    return ScoreResult(
        score=0.0,
        verdict="fail",
        hard_fail=bool(case.get("hard_fail_on_non_abstain", False)),
        details={"answer": answer, "accepted_answers": accepted_answers},
    )


def score_case(case: dict[str, Any], response_text: str) -> ScoreResult:
    parsed, parse_error = parse_json_response(response_text)
    if parse_error or parsed is None:
        return ScoreResult(
            score=0.0,
            verdict="parse_error",
            hard_fail=False,
            details={"parse_error": parse_error or "unknown"},
        )

    if case["response_kind"] == "single_choice":
        return score_single_choice(case, parsed)
    return score_short_answer(case, parsed)


def serialize_usage(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return {"repr": repr(value)}


def classify_runtime_error(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    if (
        "402 payment required" in lowered
        or "usage limit exceeded" in lowered
        or "usagelimitexceeded" in lowered
        or "usage_limit_exceeded" in lowered
        or "总消费上限已达到" in text
    ):
        return "provider_capacity_402"
    if "429" in lowered or "rate limit" in lowered:
        return "provider_rate_limit"
    return "other"


def group_records(
    records: list[dict[str, Any]],
    *,
    key: Callable[[dict[str, Any]], Any],
) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(key(record), []).append(record)
    return grouped


def summarize_case_records(case_records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(case_records)
    verdict_counts = Counter(record["score"]["verdict"] for record in case_records)
    run_error_kinds = Counter(
        error_kind
        for error_kind in (
            classify_runtime_error(record.get("run_error")) for record in case_records
        )
        if error_kind
    )
    by_category: dict[str, dict[str, Any]] = {}
    for category, records in group_records(
        case_records, key=lambda record: record["category"]
    ).items():
        by_category[category] = {
            "count": len(records),
            "pass_rate": sum(record["score"]["score"] == 1.0 for record in records) / len(records),
            "mean_score": sum(record["score"]["score"] for record in records) / len(records),
            "hard_fail_count": sum(bool(record["score"]["hard_fail"]) for record in records),
        }
    by_priority: dict[str, dict[str, Any]] = {}
    for priority, records in group_records(
        case_records, key=lambda record: record["priority"]
    ).items():
        by_priority[priority] = {
            "count": len(records),
            "pass_rate": sum(record["score"]["score"] == 1.0 for record in records) / len(records),
            "mean_score": sum(record["score"]["score"] for record in records) / len(records),
            "hard_fail_count": sum(bool(record["score"]["hard_fail"]) for record in records),
        }
    return {
        "case_count": total,
        "exact_pass_rate": (
            sum(record["score"]["score"] == 1.0 for record in case_records) / total if total else 0.0
        ),
        "mean_score": (
            sum(record["score"]["score"] for record in case_records) / total if total else 0.0
        ),
        "hard_fail_count": sum(bool(record["score"]["hard_fail"]) for record in case_records),
        "hard_fail_case_ids": [
            record["case_id"] for record in case_records if record["score"]["hard_fail"]
        ],
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "run_error_kinds": dict(sorted(run_error_kinds.items())),
        "by_category": dict(sorted(by_category.items())),
        "by_priority": dict(sorted(by_priority.items())),
    }


def summarize_retention(case_records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_records(case_records, key=lambda record: record["case_id"])
    comparisons: list[dict[str, Any]] = []
    for case_id, records in grouped.items():
        by_variant = {record["variant"]: record for record in records}
        source = by_variant.get("source")
        if source is None:
            continue
        source_passed = source["score"]["score"] == 1.0
        for variant, record in sorted(by_variant.items()):
            if variant == "source":
                continue
            comparisons.append(
                {
                    "case_id": case_id,
                    "variant": variant,
                    "source_passed": source_passed,
                    "variant_passed": record["score"]["score"] == 1.0,
                    "variant_score": record["score"]["score"],
                    "retained_vs_source": source_passed and record["score"]["score"] == 1.0,
                }
            )
    grouped_comparisons = group_records(comparisons, key=lambda record: record["variant"])
    summary: dict[str, Any] = {}
    for variant, records in grouped_comparisons.items():
        comparable = [record for record in records if record["source_passed"]]
        summary[variant] = {
            "comparable_cases": len(comparable),
            "retained_vs_source_rate": (
                sum(record["retained_vs_source"] for record in comparable) / len(comparable)
                if comparable
                else 0.0
            ),
            "source_reference_pass_rate": (
                sum(record["source_passed"] for record in records) / len(records) if records else 0.0
            ),
        }
    return dict(sorted(summary.items()))


def summarize_against_baseline(
    case_records: list[dict[str, Any]],
    *,
    baseline_variant: str,
) -> dict[str, Any]:
    grouped = group_records(case_records, key=lambda record: record["case_id"])
    comparisons: list[dict[str, Any]] = []
    for case_id, records in grouped.items():
        by_variant = {record["variant"]: record for record in records}
        baseline = by_variant.get(baseline_variant)
        if baseline is None:
            continue
        baseline_passed = baseline["score"]["score"] == 1.0
        for variant, record in sorted(by_variant.items()):
            if variant == baseline_variant:
                continue
            comparisons.append(
                {
                    "case_id": case_id,
                    "variant": variant,
                    "baseline_passed": baseline_passed,
                    "variant_passed": record["score"]["score"] == 1.0,
                    "variant_score": record["score"]["score"],
                    "retained_vs_baseline": baseline_passed
                    and record["score"]["score"] == 1.0,
                }
            )

    grouped_comparisons = group_records(comparisons, key=lambda record: record["variant"])
    summary: dict[str, Any] = {}
    for variant, records in grouped_comparisons.items():
        comparable = [record for record in records if record["baseline_passed"]]
        summary[variant] = {
            "baseline_variant": baseline_variant,
            "comparable_cases": len(comparable),
            "retained_vs_baseline_rate": (
                sum(record["retained_vs_baseline"] for record in comparable) / len(comparable)
                if comparable
                else 0.0
            ),
            "baseline_reference_pass_rate": (
                sum(record["baseline_passed"] for record in records) / len(records) if records else 0.0
            ),
            "variant_pass_rate": (
                sum(record["variant_passed"] for record in records) / len(records) if records else 0.0
            ),
        }
    return dict(sorted(summary.items()))


def summarize_case_records_by_hop(case_records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_records(
        [record for record in case_records if record.get("hop_index") is not None],
        key=lambda record: int(record["hop_index"]),
    )
    return {
        str(hop_index): summarize_case_records(records)
        for hop_index, records in sorted(grouped.items())
    }


def summarize_chain_retention(case_records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped = group_records(
        [record for record in case_records if record.get("hop_index") is not None],
        key=lambda record: int(record["hop_index"]),
    )
    return {
        str(hop_index): summarize_against_baseline(
            records,
            baseline_variant="chain_control",
        )
        for hop_index, records in sorted(grouped.items())
    }


def chain_hop_status(hop_record: dict[str, Any]) -> str:
    turn_observation = hop_record.get("turn_observation") or {}
    status = turn_observation.get("status")
    if isinstance(status, str) and status:
        return status
    if hop_record.get("continuation_error"):
        return "continuation_error"
    if hop_record.get("continuation_response") or hop_record.get("continuation_usage"):
        return "completed_without_target_compaction_tracking"
    return "unknown"


def count_chain_hop_statuses(hop_records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hop_record in hop_records:
        status = chain_hop_status(hop_record)
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def collect_chain_hop_indexes(
    hop_records: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> list[int]:
    return [
        int(hop_record["hop_index"])
        for hop_record in hop_records
        if predicate(hop_record)
    ]


def summarize_deferred_compaction_pairs(
    source_thread_id: str,
    hop_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for current_hop, next_hop in zip(hop_records, hop_records[1:]):
        current_status = chain_hop_status(current_hop)
        next_compacted_items = (
            ((next_hop.get("compact_observation") or {}).get("new_compacted_items", 0) or 0)
        )
        if (
            current_status == "task_completed_without_target_compaction"
            and next_compacted_items > 0
        ):
            pairs.append(
                {
                    "source_thread_id": source_thread_id,
                    "completed_without_target_compaction_hop": int(
                        current_hop["hop_index"]
                    ),
                    "next_compaction_observed_hop": int(next_hop["hop_index"]),
                    "completed_hop_last_total_tokens": (
                        ((current_hop.get("continuation_usage") or {}).get("last") or {}).get(
                            "total_tokens"
                        )
                    ),
                    "next_hop_new_compacted_items": next_compacted_items,
                }
            )
    return pairs


def summarize_chain_prepared_variants(
    prepared_variants: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_source: dict[str, Any] = {}
    total_hops = 0
    compact_observed_hops = 0
    shape_valid_hops = 0
    continuation_error_hops = 0
    hop_status_counts: dict[str, int] = {}
    deferred_compaction_pairs: list[dict[str, Any]] = []
    for source_thread_id, prepared in sorted(prepared_variants.items()):
        hop_records = prepared.get("hop_records") or []
        total_hops += len(hop_records)
        source_status_counts = count_chain_hop_statuses(hop_records)
        for status, count in source_status_counts.items():
            hop_status_counts[status] = hop_status_counts.get(status, 0) + count
        compact_observed = sum(
            1
            for hop in hop_records
            if ((hop.get("compact_observation") or {}).get("new_compacted_items", 0) or 0) > 0
        )
        shape_valid = sum(1 for hop in hop_records if hop.get("shape_valid") is True)
        continuation_errors = sum(1 for hop in hop_records if hop.get("continuation_error"))
        compact_observed_hop_indexes = collect_chain_hop_indexes(
            hop_records,
            lambda hop: ((hop.get("compact_observation") or {}).get("new_compacted_items", 0) or 0)
            > 0,
        )
        completed_without_target_compaction_hop_indexes = collect_chain_hop_indexes(
            hop_records,
            lambda hop: chain_hop_status(hop) == "task_completed_without_target_compaction",
        )
        shape_valid_hop_indexes = collect_chain_hop_indexes(
            hop_records,
            lambda hop: hop.get("shape_valid") is True,
        )
        continuation_error_hop_indexes = collect_chain_hop_indexes(
            hop_records,
            lambda hop: bool(hop.get("continuation_error")),
        )
        source_deferred_pairs = summarize_deferred_compaction_pairs(
            source_thread_id,
            hop_records,
        )
        deferred_compaction_pairs.extend(source_deferred_pairs)
        compact_observed_hops += compact_observed
        shape_valid_hops += shape_valid
        continuation_error_hops += continuation_errors
        by_source[source_thread_id] = {
            "hop_count": len(hop_records),
            "hop_status_counts": source_status_counts,
            "compact_observed_hops": compact_observed,
            "compact_observed_hop_indexes": compact_observed_hop_indexes,
            "completed_without_target_compaction_hop_indexes": (
                completed_without_target_compaction_hop_indexes
            ),
            "shape_valid_hops": shape_valid,
            "shape_valid_hop_indexes": shape_valid_hop_indexes,
            "continuation_error_hops": continuation_errors,
            "continuation_error_hop_indexes": continuation_error_hop_indexes,
            "deferred_compaction_pairs": source_deferred_pairs,
            "latest_after_metrics": hop_records[-1]["after_metrics"] if hop_records else None,
        }
    return {
        "source_thread_count": len(prepared_variants),
        "total_hops": total_hops,
        "hop_status_counts": dict(sorted(hop_status_counts.items())),
        "compact_observed_hops": compact_observed_hops,
        "shape_valid_hops": shape_valid_hops,
        "continuation_error_hops": continuation_error_hops,
        "deferred_compaction_pairs": deferred_compaction_pairs,
        "by_source_thread": by_source,
    }


def write_result_document(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def load_sdk(sdk_src: Path = SDK_SRC) -> tuple[Any, Any, Any]:
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    from codex_app_server import AskForApproval
    from codex_app_server import Codex
    from codex_app_server.client import AppServerConfig

    return Codex, AskForApproval, AppServerConfig


def load_chain_sdk(sdk_src: Path = SDK_SRC) -> tuple[Any, Any, Any, Any, Any]:
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    from codex_app_server import AskForApproval
    from codex_app_server import Codex
    from codex_app_server import TextInput
    from codex_app_server._run import _collect_run_result
    from codex_app_server.client import AppServerConfig

    return Codex, AskForApproval, AppServerConfig, TextInput, _collect_run_result


def build_variant_config_overrides(
    variant: str,
    *,
    model_context_window: int,
    model_auto_compact_token_limit: int,
) -> tuple[str, ...]:
    overrides = [
        f"model_context_window={model_context_window}",
        f"model_auto_compact_token_limit={model_auto_compact_token_limit}",
    ]
    if variant == "session":
        overrides.insert(0, 'compact_strategy="session"')
    elif variant != "source":
        raise ValueError(f"unsupported benchmark variant: {variant}")
    return tuple(overrides)


def build_chain_variant_config_overrides(
    variant: str,
    *,
    chain_model_context_window: int,
    chain_model_auto_compact_token_limit: int,
    legacy_collapse_preserve_turns: int,
) -> tuple[str, ...]:
    if variant == "chain_control":
        return (
            f"model_context_window={DEFAULT_MODEL_CONTEXT_WINDOW}",
            f"model_auto_compact_token_limit={DEFAULT_MODEL_AUTO_COMPACT_TOKEN_LIMIT}",
        )
    if variant == "session":
        return (
            'compact_strategy="session"',
            f"model_context_window={chain_model_context_window}",
            f"model_auto_compact_token_limit={chain_model_auto_compact_token_limit}",
        )
    if variant == "legacy_collapse":
        return (
            'compact_mode="collapse"',
            f"compact_preserve_turns={legacy_collapse_preserve_turns}",
            f"model_context_window={chain_model_context_window}",
            f"model_auto_compact_token_limit={chain_model_auto_compact_token_limit}",
        )
    raise ValueError(f"unsupported chain benchmark variant: {variant}")


def prepare_variant_thread(
    codex: Any,
    approval_policy: Any,
    *,
    source_thread_id: str,
    variant: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> PreparedVariant:
    source_read = codex._client.thread_read(source_thread_id, include_turns=False)
    source_path_value = getattr(source_read.thread, "path", None)
    source_rollout_path = (
        str(Path(source_path_value).resolve()) if isinstance(source_path_value, str) else None
    )
    source_metrics = (
        asdict(analyze_rollout(Path(source_rollout_path)))
        if source_rollout_path is not None
        else None
    )

    prepared = codex.thread_fork(source_thread_id, approval_policy=approval_policy)
    prepared_read = prepared.read(include_turns=False)
    rollout_path = Path(prepared_read.thread.path).resolve()
    initial_scan = asdict(scan_rollout(rollout_path))
    initial_metrics = asdict(analyze_rollout(rollout_path))
    compact_follow_result = None
    if variant != "source":
        prepared.compact()
        compact_follow_result = asdict(
            follow_compact(
                rollout_path,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        )
        if not compact_follow_result["status"].startswith("completed"):
            raise RuntimeError(
                f"variant {variant} did not finish compaction for {source_thread_id}: "
                f"{compact_follow_result['status']}"
            )
    final_scan = asdict(scan_rollout(rollout_path))
    final_metrics = asdict(analyze_rollout(rollout_path))
    compaction_delta = (
        summarize_compaction_delta(initial_metrics, final_metrics)
        if compact_follow_result is not None
        else None
    )
    return PreparedVariant(
        variant=variant,
        source_thread_id=source_thread_id,
        source_rollout_path=source_rollout_path,
        source_metrics=source_metrics,
        prepared_thread_id=prepared.id,
        prepared_rollout_path=str(rollout_path),
        initial_scan=initial_scan,
        final_scan=final_scan,
        initial_metrics=initial_metrics,
        final_metrics=final_metrics,
        compact_follow_result=compact_follow_result,
        compaction_delta=compaction_delta,
    )


def run_case_probe(
    codex: Any,
    approval_policy: Any,
    *,
    case: dict[str, Any],
    variant: str,
    prepared_thread_id: str,
    effort: str,
    hop_index: int | None = None,
) -> dict[str, Any]:
    case_thread = codex.thread_fork(prepared_thread_id, approval_policy=approval_policy)
    case_read = case_thread.read(include_turns=False)
    rollout_path = Path(case_read.thread.path).resolve()
    before_metrics = asdict(analyze_rollout(rollout_path))
    prompt = render_probe(case)
    output_schema = build_output_schema(case)
    run_error = None
    raw_response = ""
    parsed_response = None
    parse_error = None
    score_result = ScoreResult(score=0.0, verdict="run_error", hard_fail=False, details={})
    usage = None

    try:
        run_result = case_thread.run(
            prompt,
            approval_policy=approval_policy,
            output_schema=output_schema,
            effort=effort,
        )
        raw_response = run_result.final_response or ""
        usage = serialize_usage(getattr(run_result, "usage", None))
        parsed_response, parse_error = parse_json_response(raw_response)
        score_result = score_case(case, raw_response)
    except Exception as exc:  # noqa: BLE001
        run_error = str(exc)
        score_result = ScoreResult(
            score=0.0,
            verdict="run_error",
            hard_fail=False,
            details={"error": run_error},
        )

    after_metrics = asdict(analyze_rollout(rollout_path))
    return {
        "case_id": case["id"],
        "source_thread_id": case["source_thread_id"],
        "variant": variant,
        "hop_index": hop_index,
        "category": case["category"],
        "priority": case["priority"],
        "prepared_thread_id": prepared_thread_id,
        "case_thread_id": case_thread.id,
        "case_rollout_path": str(rollout_path),
        "prompt": prompt,
        "output_schema": output_schema,
        "raw_response": raw_response,
        "parsed_response": parsed_response,
        "parse_error": parse_error,
        "run_error": run_error,
        "usage": usage,
        "score": asdict(score_result),
        "before_metrics": before_metrics,
        "after_metrics": after_metrics,
    }


def working_state_case_id(source_thread_id: str, hop_index: int) -> str:
    return f"working_state::{source_thread_id}::hop{hop_index}"


def run_working_state_probe(
    codex: Any,
    approval_policy: Any,
    *,
    source_thread_id: str,
    variant: str,
    prepared_thread_id: str,
    effort: str,
    hop_index: int,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    case_thread = codex.thread_fork(prepared_thread_id, approval_policy=approval_policy)
    case_read = case_thread.read(include_turns=False)
    rollout_path = Path(case_read.thread.path).resolve()
    before_metrics = asdict(analyze_rollout(rollout_path))
    prompt = render_working_state_probe(bundle)
    output_schema = build_working_state_output_schema()
    run_error = None
    raw_response = ""
    parsed_response = None
    parse_error = None
    score_result = ScoreResult(score=0.0, verdict="run_error", hard_fail=False, details={})
    usage = None

    try:
        run_result = case_thread.run(
            prompt,
            approval_policy=approval_policy,
            output_schema=output_schema,
            effort=effort,
        )
        raw_response = run_result.final_response or ""
        usage = serialize_usage(getattr(run_result, "usage", None))
        parsed_response, parse_error = parse_json_response(raw_response)
        score_result = score_working_state_probe(bundle, raw_response)
    except Exception as exc:  # noqa: BLE001
        run_error = str(exc)
        score_result = ScoreResult(
            score=0.0,
            verdict="run_error",
            hard_fail=False,
            details={"error": run_error},
        )

    after_metrics = asdict(analyze_rollout(rollout_path))
    return {
        "case_id": working_state_case_id(source_thread_id, hop_index),
        "source_thread_id": source_thread_id,
        "variant": variant,
        "hop_index": hop_index,
        "category": "working_state",
        "priority": "critical",
        "prepared_thread_id": prepared_thread_id,
        "case_thread_id": case_thread.id,
        "case_rollout_path": str(rollout_path),
        "prompt": prompt,
        "output_schema": output_schema,
        "raw_response": raw_response,
        "parsed_response": parsed_response,
        "parse_error": parse_error,
        "run_error": run_error,
        "usage": usage,
        "score": asdict(score_result),
        "before_metrics": before_metrics,
        "after_metrics": after_metrics,
        "expected_bundle": bundle,
    }


def build_skipped_probe_record(
    *,
    case: dict[str, Any],
    variant: str,
    prepared_thread_id: str,
    hop_index: int,
    reason: str,
) -> dict[str, Any]:
    score_result = ScoreResult(
        score=0.0,
        verdict="run_error",
        hard_fail=False,
        details={"error": reason},
    )
    return {
        "case_id": case["id"],
        "source_thread_id": case["source_thread_id"],
        "variant": variant,
        "hop_index": hop_index,
        "category": case["category"],
        "priority": case["priority"],
        "prepared_thread_id": prepared_thread_id,
        "case_thread_id": None,
        "case_rollout_path": None,
        "prompt": render_probe(case),
        "output_schema": build_output_schema(case),
        "raw_response": "",
        "parsed_response": None,
        "parse_error": None,
        "run_error": reason,
        "usage": None,
        "score": asdict(score_result),
        "before_metrics": None,
        "after_metrics": None,
    }


def build_skipped_working_state_probe_record(
    *,
    source_thread_id: str,
    variant: str,
    prepared_thread_id: str,
    hop_index: int,
    bundle: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    score_result = ScoreResult(
        score=0.0,
        verdict="run_error",
        hard_fail=False,
        details={"error": reason},
    )
    return {
        "case_id": working_state_case_id(source_thread_id, hop_index),
        "source_thread_id": source_thread_id,
        "variant": variant,
        "hop_index": hop_index,
        "category": "working_state",
        "priority": "critical",
        "prepared_thread_id": prepared_thread_id,
        "case_thread_id": None,
        "case_rollout_path": None,
        "prompt": render_working_state_probe(bundle),
        "output_schema": build_working_state_output_schema(),
        "raw_response": "",
        "parsed_response": None,
        "parse_error": None,
        "run_error": reason,
        "usage": None,
        "score": asdict(score_result),
        "before_metrics": None,
        "after_metrics": None,
        "expected_bundle": bundle,
    }


def run_variant(
    *,
    variant: str,
    cases: list[dict[str, Any]],
    lab_root: Path,
    codex_bin: Path,
    timeout_seconds: int,
    poll_interval_seconds: float,
    model_context_window: int,
    model_auto_compact_token_limit: int,
    effort: str,
) -> dict[str, Any]:
    Codex, AskForApproval, AppServerConfig = load_sdk()
    approval_policy = AskForApproval("never")
    config_overrides = build_variant_config_overrides(
        variant,
        model_context_window=model_context_window,
        model_auto_compact_token_limit=model_auto_compact_token_limit,
    )
    app_server_config = AppServerConfig(
        codex_bin=str(codex_bin),
        config_overrides=config_overrides,
        env=build_lab_env(lab_root),
        client_name="session_compact_rcr_benchmark",
        client_title="Session Compact RCR Benchmark",
        client_version="0.0.2",
    )

    prepared_variants: dict[str, PreparedVariant] = {}
    case_records: list[dict[str, Any]] = []
    source_thread_ids = sorted({case["source_thread_id"] for case in cases})

    with Codex(config=app_server_config) as codex:
        for source_thread_id in source_thread_ids:
            prepared_variants[source_thread_id] = prepare_variant_thread(
                codex,
                approval_policy,
                source_thread_id=source_thread_id,
                variant=variant,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            prepared = prepared_variants[source_thread_id]
            source_cases = [case for case in cases if case["source_thread_id"] == source_thread_id]
            for case in source_cases:
                case_records.append(
                    run_case_probe(
                        codex,
                        approval_policy,
                        case=case,
                        variant=variant,
                        prepared_thread_id=prepared.prepared_thread_id,
                        effort=effort,
                    )
                )

    return {
        "variant": variant,
        "config_overrides": list(config_overrides),
        "prepared_variants": {
            source_thread_id: asdict(prepared)
            for source_thread_id, prepared in sorted(prepared_variants.items())
        },
        "case_records": case_records,
        "summary": summarize_case_records(case_records),
    }


def compute_chain_shape_validity(compact_signature: dict[str, Any] | None) -> bool | None:
    if compact_signature is None:
        return None
    return bool(compact_signature.get("summary_message_count", 0) >= 1)


def run_chain_hop(
    *,
    prepared_thread: Any,
    approval_policy: Any,
    rollout_path: Path,
    variant: str,
    hop_index: int,
    chain_prompt: str,
    effort: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
    TextInput: Any,
    collect_run_result: Any,
) -> dict[str, Any]:
    before_scan = asdict(scan_rollout(rollout_path))
    before_metrics = asdict(analyze_rollout(rollout_path))

    continuation_error = None
    continuation_response = ""
    continuation_usage = None
    turn_observation = None
    turn_interrupted_after_detection = None
    interrupt_error = None

    try:
        if variant == "session":
            turn = prepared_thread.turn(
                TextInput(chain_prompt),
                approval_policy=approval_policy,
                effort=effort,
            )
            turn_observation = wait_for_target_auto_compaction(
                rollout_path,
                start_line=before_scan["total_lines"],
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            status = turn_observation["status"]
            if status == "midturn_compaction_observed":
                try:
                    turn.interrupt()
                    turn_interrupted_after_detection = True
                except Exception as exc:  # noqa: BLE001
                    turn_interrupted_after_detection = False
                    interrupt_error = str(exc)
                try:
                    turn.run()
                except Exception as exc:  # noqa: BLE001
                    if continuation_error is None:
                        continuation_error = str(exc)
                time.sleep(COMPACT_SETTLE_SECONDS)
            elif status == "task_completed_without_target_compaction":
                stream = turn.stream()
                try:
                    run_result = collect_run_result(stream, turn_id=turn.id)
                finally:
                    stream.close()
                continuation_response = run_result.final_response or ""
                continuation_usage = serialize_usage(getattr(run_result, "usage", None))
            else:
                continuation_error = status
                try:
                    turn.interrupt()
                    turn.run()
                except Exception as exc:  # noqa: BLE001
                    interrupt_error = str(exc)
        else:
            run_result = prepared_thread.run(
                chain_prompt,
                approval_policy=approval_policy,
                effort=effort,
            )
            continuation_response = run_result.final_response or ""
            continuation_usage = serialize_usage(getattr(run_result, "usage", None))
    except Exception as exc:  # noqa: BLE001
        continuation_error = str(exc)

    after_scan = asdict(scan_rollout(rollout_path))
    after_metrics = asdict(analyze_rollout(rollout_path))
    compact_observation = turn_observation["appended_records"] if turn_observation else None
    latest_new_compacted = (
        compact_observation.get("latest_new_compacted") if compact_observation else None
    )
    compact_signature = latest_new_compacted["signature"] if latest_new_compacted else None

    return {
        "hop_index": hop_index,
        "chain_prompt": chain_prompt,
        "before_scan": before_scan,
        "before_metrics": before_metrics,
        "after_scan": after_scan,
        "after_metrics": after_metrics,
        "continuation_error": continuation_error,
        "continuation_response": continuation_response,
        "continuation_usage": continuation_usage,
        "turn_observation": turn_observation,
        "turn_interrupted_after_detection": turn_interrupted_after_detection,
        "interrupt_error": interrupt_error,
        "compact_observation": compact_observation,
        "compact_signature": compact_signature,
        "shape_valid": compute_chain_shape_validity(compact_signature),
    }


def run_chain_variant(
    *,
    variant: str,
    cases: list[dict[str, Any]],
    lab_root: Path,
    codex_bin: Path,
    timeout_seconds: int,
    poll_interval_seconds: float,
    chain_model_context_window: int,
    chain_model_auto_compact_token_limit: int,
    legacy_collapse_preserve_turns: int,
    effort: str,
    chain_hops: int,
    chain_prompt: str,
    sdk_src: Path = SDK_SRC,
) -> dict[str, Any]:
    Codex, AskForApproval, AppServerConfig, TextInput, collect_run_result = load_chain_sdk(
        sdk_src
    )
    approval_policy = AskForApproval("never")
    config_overrides = build_chain_variant_config_overrides(
        variant,
        chain_model_context_window=chain_model_context_window,
        chain_model_auto_compact_token_limit=chain_model_auto_compact_token_limit,
        legacy_collapse_preserve_turns=legacy_collapse_preserve_turns,
    )
    app_server_config = AppServerConfig(
        codex_bin=str(codex_bin),
        config_overrides=config_overrides,
        env=build_lab_env(lab_root),
        client_name="session_compact_chain_benchmark",
        client_title="Session Compact Chain Benchmark",
        client_version="0.0.1",
    )

    prepared_variants: dict[str, dict[str, Any]] = {}
    case_records: list[dict[str, Any]] = []
    working_state_records: list[dict[str, Any]] = []
    source_thread_ids = sorted({case["source_thread_id"] for case in cases})

    with Codex(config=app_server_config) as codex:
        for source_thread_id in source_thread_ids:
            prepared_thread = codex.thread_fork(source_thread_id, approval_policy=approval_policy)
            prepared_read = prepared_thread.read(include_turns=False)
            rollout_path = Path(prepared_read.thread.path).resolve()
            prepared_state = {
                "variant": variant,
                "source_thread_id": source_thread_id,
                "prepared_thread_id": prepared_thread.id,
                "prepared_rollout_path": str(rollout_path),
                "initial_scan": asdict(scan_rollout(rollout_path)),
                "initial_metrics": asdict(analyze_rollout(rollout_path)),
                "hop_records": [],
            }
            prepared_variants[source_thread_id] = prepared_state

            source_cases = [case for case in cases if case["source_thread_id"] == source_thread_id]
            for hop_index in range(1, chain_hops + 1):
                hop_prompt, working_state_bundle = build_chain_prompt_for_hop(
                    chain_prompt,
                    hop_index,
                )
                hop_record = run_chain_hop(
                    prepared_thread=prepared_thread,
                    approval_policy=approval_policy,
                    rollout_path=rollout_path,
                    variant=variant,
                    hop_index=hop_index,
                    chain_prompt=hop_prompt,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    TextInput=TextInput,
                    collect_run_result=collect_run_result,
                )
                hop_record["working_state_bundle"] = working_state_bundle
                prepared_state["hop_records"].append(hop_record)
                hop_status = chain_hop_status(hop_record)
                probe_skip_reason = hop_record.get("continuation_error")
                if probe_skip_reason is None and hop_status not in CHAIN_PROBE_READY_STATUSES:
                    probe_skip_reason = f"prepare_{hop_status}"
                if probe_skip_reason is not None:
                    working_state_records.append(
                        build_skipped_working_state_probe_record(
                            source_thread_id=source_thread_id,
                            variant=variant,
                            prepared_thread_id=prepared_thread.id,
                            hop_index=hop_index,
                            bundle=working_state_bundle,
                            reason=probe_skip_reason,
                        )
                    )
                else:
                    working_state_records.append(
                        run_working_state_probe(
                            codex,
                            approval_policy,
                            source_thread_id=source_thread_id,
                            variant=variant,
                            prepared_thread_id=prepared_thread.id,
                            effort=effort,
                            hop_index=hop_index,
                            bundle=working_state_bundle,
                        )
                    )
                for case in source_cases:
                    if probe_skip_reason is not None:
                        case_records.append(
                            build_skipped_probe_record(
                                case=case,
                                variant=variant,
                                prepared_thread_id=prepared_thread.id,
                                hop_index=hop_index,
                                reason=probe_skip_reason,
                            )
                        )
                        continue
                    case_records.append(
                        run_case_probe(
                            codex,
                            approval_policy,
                            case=case,
                            variant=variant,
                            prepared_thread_id=prepared_thread.id,
                            effort=effort,
                            hop_index=hop_index,
                        )
                    )

            prepared_state["final_scan"] = asdict(scan_rollout(rollout_path))
            prepared_state["final_metrics"] = asdict(analyze_rollout(rollout_path))

    return {
        "variant": variant,
        "config_overrides": list(config_overrides),
        "prepared_variants": prepared_variants,
        "case_records": case_records,
        "summary": summarize_case_records(case_records),
        "per_hop_summary": summarize_case_records_by_hop(case_records),
        "working_state_records": working_state_records,
        "working_state_summary": summarize_case_records(working_state_records),
        "working_state_per_hop_summary": summarize_case_records_by_hop(
            working_state_records
        ),
        "chain_diagnostics": summarize_chain_prepared_variants(prepared_variants),
    }


def run_external_chain_variant_subprocess(
    *,
    cases_path: Path,
    variant: str,
    source_threads: list[str],
    case_ids: list[str],
    lab_root: Path,
    codex_bin: Path,
    sdk_src: Path,
    timeout_seconds: int,
    poll_interval_seconds: float,
    chain_model_context_window: int,
    chain_model_auto_compact_token_limit: int,
    legacy_collapse_preserve_turns: int,
    effort: str,
    chain_hops: int,
    chain_prompt: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--cases",
        str(cases_path),
        "run-chain-variant",
        "--variant",
        variant,
        "--lab-root",
        str(lab_root),
        "--codex-bin",
        str(codex_bin),
        "--sdk-src",
        str(sdk_src),
        "--timeout-seconds",
        str(timeout_seconds),
        "--poll-interval-seconds",
        str(poll_interval_seconds),
        "--chain-model-context-window",
        str(chain_model_context_window),
        "--chain-model-auto-compact-token-limit",
        str(chain_model_auto_compact_token_limit),
        "--legacy-collapse-preserve-turns",
        str(legacy_collapse_preserve_turns),
        "--effort",
        effort,
        "--chain-hops",
        str(chain_hops),
        "--chain-prompt",
        chain_prompt,
    ]
    for source_thread in source_threads:
        command.extend(["--source-thread", source_thread])
    for case_id in case_ids:
        command.extend(["--case-id", case_id])

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"external variant {variant} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )

    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(f"external variant {variant} produced no JSON output")
    return json.loads(stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Session-compact RCR benchmark tools for latest-upstream Codex.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASE_FILE,
        help="Path to the benchmark case manifest.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate the manifest and print a compact summary.",
    )
    validate_parser.add_argument(
        "--list-case-ids",
        action="store_true",
        help="Also print all case ids in manifest order.",
    )

    summary_parser = subparsers.add_parser(
        "summary",
        help="Print high-level corpus summary and current variant families.",
    )
    summary_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the summary as JSON.",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        help="Print the deterministic probe prompt and output schema for one case.",
    )
    probe_parser.add_argument("--case-id", required=True)

    score_parser = subparsers.add_parser(
        "score",
        help="Score one model response against one benchmark case.",
    )
    score_parser.add_argument("--case-id", required=True)
    response_group = score_parser.add_mutually_exclusive_group(required=True)
    response_group.add_argument("--response")
    response_group.add_argument("--response-file", type=Path)

    setup_lab_parser = subparsers.add_parser(
        "setup-lab",
        help="Create an isolated copied-session lab without touching live ~/.codex.",
    )
    setup_lab_parser.add_argument(
        "--source-home",
        default="~/.codex",
        help="Live Codex home to copy from.",
    )
    setup_lab_parser.add_argument(
        "--lab-root",
        type=Path,
        default=DEFAULT_LAB_ROOT,
        help="Lab root directory. home/ and user-home/ are created underneath it.",
    )
    setup_lab_parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Copy the N largest rollout files when --rollout is not specified.",
    )
    setup_lab_parser.add_argument(
        "--min-rollout-size-kb",
        type=int,
        default=512,
        help="Only consider rollout files at or above this size.",
    )
    setup_lab_parser.add_argument(
        "--rollout",
        action="append",
        default=[],
        help="Explicit rollout file to copy. May be passed multiple times.",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run single-hop copied-session source/session benchmark execution.",
    )
    run_parser.add_argument(
        "--lab-root",
        type=Path,
        default=DEFAULT_LAB_ROOT,
        help="Isolated lab root with home/ and user-home/.",
    )
    run_parser.add_argument(
        "--codex-bin",
        default=str(resolve_default_codex_bin()),
        help="Codex binary to launch for the benchmark runner.",
    )
    run_parser.add_argument(
        "--variant",
        action="append",
        choices=DEFAULT_VARIANTS,
        help="Variant(s) to execute. Defaults to source + session.",
    )
    run_parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Only run the selected case id. May be passed multiple times.",
    )
    run_parser.add_argument(
        "--source-thread",
        action="append",
        default=[],
        help="Only run cases from the selected source thread id. May be passed multiple times.",
    )
    run_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Stop waiting if manual session compaction does not finish in time.",
    )
    run_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
        help="Rollout polling interval while waiting for compact completion.",
    )
    run_parser.add_argument(
        "--model-context-window",
        type=int,
        default=DEFAULT_MODEL_CONTEXT_WINDOW,
        help="Large probe-time context window to avoid unrelated auto-compaction.",
    )
    run_parser.add_argument(
        "--model-auto-compact-token-limit",
        type=int,
        default=DEFAULT_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
        help="Large probe-time auto-compact limit to avoid contaminating probe turns.",
    )
    run_parser.add_argument(
        "--effort",
        default="medium",
        help="Reasoning effort for probe turns.",
    )
    run_parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file to write the benchmark result to.",
    )

    run_chain_parser = subparsers.add_parser(
        "run-chain",
        help="Run chained continued-auto copied-session benchmark execution.",
    )
    run_chain_parser.add_argument(
        "--lab-root",
        type=Path,
        default=DEFAULT_LAB_ROOT,
        help="Isolated lab root with home/ and user-home/.",
    )
    run_chain_parser.add_argument(
        "--codex-bin",
        default=str(resolve_default_codex_bin()),
        help="Codex binary to launch for the benchmark runner.",
    )
    run_chain_parser.add_argument(
        "--variant",
        action="append",
        choices=CHAIN_VARIANT_CHOICES,
        help="Variant(s) to execute. Defaults to chain_control + session.",
    )
    run_chain_parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Only run the selected case id. May be passed multiple times.",
    )
    run_chain_parser.add_argument(
        "--source-thread",
        action="append",
        default=[],
        help="Only run cases from the selected source thread id. May be passed multiple times.",
    )
    run_chain_parser.add_argument(
        "--chain-hops",
        type=int,
        default=DEFAULT_CHAIN_HOPS,
        help="Number of chained continuation hops to execute on each prepared thread.",
    )
    run_chain_parser.add_argument(
        "--chain-prompt",
        default=DEFAULT_CHAIN_PROMPT,
        help="Deterministic continuation prompt used before each hop's probe forks.",
    )
    run_chain_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Stop waiting if mid-turn auto compaction does not appear in time.",
    )
    run_chain_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
        help="Rollout polling interval while waiting for auto-compaction.",
    )
    run_chain_parser.add_argument(
        "--chain-model-context-window",
        type=int,
        default=DEFAULT_CHAIN_MODEL_CONTEXT_WINDOW,
        help="Low context window used for the session chained variant to trigger auto compact.",
    )
    run_chain_parser.add_argument(
        "--chain-model-auto-compact-token-limit",
        type=int,
        default=DEFAULT_CHAIN_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
        help="Low auto-compact threshold used for the session chained variant.",
    )
    run_chain_parser.add_argument(
        "--effort",
        default="medium",
        help="Reasoning effort for continuation and probe turns.",
    )
    run_chain_parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file to write the benchmark result to.",
    )
    run_chain_parser.add_argument(
        "--legacy-codex-bin",
        default=str(resolve_default_codex_bin(LEGACY_REPO_ROOT)),
        help="Old compat-repo Codex binary used for the optional legacy_collapse baseline.",
    )
    run_chain_parser.add_argument(
        "--legacy-sdk-src",
        type=Path,
        default=LEGACY_REPO_ROOT / "sdk" / "python" / "src",
        help="SDK source path for the old compat repo when running legacy_collapse.",
    )
    run_chain_parser.add_argument(
        "--legacy-collapse-preserve-turns",
        type=int,
        default=DEFAULT_LEGACY_COLLAPSE_PRESERVE_TURNS,
        help="Preserve-turn setting used when the legacy_collapse baseline is enabled.",
    )

    run_chain_variant_parser = subparsers.add_parser(
        "run-chain-variant",
        help=argparse.SUPPRESS,
    )
    run_chain_variant_parser.add_argument("--variant", required=True, choices=CHAIN_VARIANT_CHOICES)
    run_chain_variant_parser.add_argument(
        "--lab-root",
        type=Path,
        required=True,
    )
    run_chain_variant_parser.add_argument(
        "--codex-bin",
        required=True,
    )
    run_chain_variant_parser.add_argument(
        "--sdk-src",
        type=Path,
        default=SDK_SRC,
    )
    run_chain_variant_parser.add_argument(
        "--case-id",
        action="append",
        default=[],
    )
    run_chain_variant_parser.add_argument(
        "--source-thread",
        action="append",
        default=[],
    )
    run_chain_variant_parser.add_argument(
        "--chain-hops",
        type=int,
        default=DEFAULT_CHAIN_HOPS,
    )
    run_chain_variant_parser.add_argument(
        "--chain-prompt",
        default=DEFAULT_CHAIN_PROMPT,
    )
    run_chain_variant_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
    )
    run_chain_variant_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
    )
    run_chain_variant_parser.add_argument(
        "--chain-model-context-window",
        type=int,
        default=DEFAULT_CHAIN_MODEL_CONTEXT_WINDOW,
    )
    run_chain_variant_parser.add_argument(
        "--chain-model-auto-compact-token-limit",
        type=int,
        default=DEFAULT_CHAIN_MODEL_AUTO_COMPACT_TOKEN_LIMIT,
    )
    run_chain_variant_parser.add_argument(
        "--legacy-collapse-preserve-turns",
        type=int,
        default=DEFAULT_LEGACY_COLLAPSE_PRESERVE_TURNS,
    )
    run_chain_variant_parser.add_argument(
        "--effort",
        default="medium",
    )

    return parser


def cmd_validate(args: argparse.Namespace) -> int:
    payload = validate_payload(args.cases)
    cases = payload["cases"]
    source_thread_profiles = load_source_thread_profiles(args.cases)
    stats = aggregate_case_stats(cases)
    print(
        json.dumps(
            {
                "benchmark": payload.get("benchmark"),
                "version": payload.get("version"),
                **stats,
                "source_thread_profiles": summarize_source_thread_profiles(
                    source_thread_profiles,
                    {case["source_thread_id"] for case in cases},
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.list_case_ids:
        for case in cases:
            print(case["id"])
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    payload = validate_payload(args.cases)
    cases = payload["cases"]
    source_thread_profiles = load_source_thread_profiles(args.cases)
    summary = {
        "benchmark": payload.get("benchmark"),
        "version": payload.get("version"),
        **aggregate_case_stats(cases),
        "source_thread_profiles": summarize_source_thread_profiles(
            source_thread_profiles,
            {case["source_thread_id"] for case in cases},
        ),
        "primary_variants": list(DEFAULT_VARIANTS),
        "chain_variants": list(CHAIN_VARIANT_CHOICES),
        "notes": [
            "This tool now covers deterministic corpus helpers, isolated lab setup, single-hop source/session execution, and chained continued-auto execution.",
            "run-chain may also include the optional legacy_collapse donor baseline through the old compat repo.",
            "Use setup-lab before run so copied-session evaluation stays isolated from live ~/.codex.",
            "Primary acceptance remains behavior-based retention, not summary-string similarity.",
        ],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"benchmark: {summary['benchmark']} v{summary['version']}")
    print(f"cases: {summary['case_count']}")
    print(f"primary variants: {', '.join(summary['primary_variants'])}")
    print(f"chain variants: {', '.join(summary['chain_variants'])}")
    print("categories:")
    for key, value in summary["category_counts"].items():
        print(f"  {key}: {value}")
    print("priorities:")
    for key, value in summary["priority_counts"].items():
        print(f"  {key}: {value}")
    print("source threads:")
    for key, value in summary["source_thread_counts"].items():
        print(f"  {key}: {value}")
    print("source thread roles:")
    for key, value in summary["source_thread_profiles"]["profiles"].items():
        print(f"  {key}: {value.get('classification', 'acceptance_oracle')}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    case = load_cases(args.cases, {args.case_id})[0]
    print(render_probe(case))
    print("output_schema:")
    print(json.dumps(build_output_schema(case), ensure_ascii=False, indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    case = load_cases(args.cases, {args.case_id})[0]
    response_text = (
        args.response_file.read_text(encoding="utf-8")
        if args.response_file is not None
        else args.response
    )
    result = score_case(case, response_text)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def cmd_setup_lab(args: argparse.Namespace) -> int:
    source_home = Path(args.source_home).expanduser().resolve()
    lab_root = args.lab_root.expanduser().resolve()
    home_dir = lab_root / "home"
    user_home = lab_root / "user-home"

    ensure_dir(home_dir)
    ensure_dir(user_home)
    ensure_dir(user_home / ".config")
    ensure_dir(user_home / ".cache")
    ensure_dir(user_home / ".local" / "state")
    ensure_dir(user_home / ".local" / "share")

    copied_files: list[str] = []
    skipped_files: list[str] = []
    for name in DEFAULT_COPY_FILES:
        src = source_home / name
        dst = home_dir / name
        if copy_file(src, dst):
            copied_files.append(name)
        else:
            skipped_files.append(name)

    explicit_rollouts = [Path(item).expanduser().resolve() for item in args.rollout]
    if explicit_rollouts:
        selected_rollouts = explicit_rollouts
    else:
        min_size_bytes = args.min_rollout_size_kb * 1024
        selected_rollouts = find_rollouts(source_home, min_size_bytes)[: args.top_n]

    copied_rollouts: list[CopiedRollout] = []
    for rollout in selected_rollouts:
        rel = rollout.relative_to(source_home)
        dst = home_dir / rel
        copy_file(rollout, dst)
        copied_rollouts.append(
            CopiedRollout(
                source=str(rollout),
                destination=str(dst),
                size_bytes=rollout.stat().st_size,
            )
        )

    env_script = write_env_script(lab_root, home_dir, user_home)
    manifest = {
        "created_at": utc_now(),
        "source_home": str(source_home),
        "lab_root": str(lab_root),
        "isolated_codex_home": str(home_dir),
        "isolated_user_home": str(user_home),
        "env_script": str(env_script),
        "copied_files": copied_files,
        "skipped_files": skipped_files,
        "copied_rollouts": [asdict(item) for item in copied_rollouts],
    }
    manifest_path = lab_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    payload = validate_payload(args.cases)
    source_thread_profiles = load_source_thread_profiles(args.cases)
    selected_case_ids = set(args.case_id) if args.case_id else None
    cases = load_cases(args.cases, selected_case_ids)
    if args.source_thread:
        allowed_source_threads = set(args.source_thread)
        cases = [case for case in cases if case["source_thread_id"] in allowed_source_threads]
        if not cases:
            raise SystemExit("no benchmark cases remain after --source-thread filtering")
    variants = tuple(args.variant) if args.variant else DEFAULT_VARIANTS
    lab_root = args.lab_root.expanduser().resolve()
    lab_home = lab_root / "home"
    if not lab_home.exists():
        raise SystemExit(
            f"{lab_home} does not exist. Run `setup-lab` first to create an isolated copied-session lab."
        )
    codex_bin = resolve_requested_codex_bin(args.codex_bin)

    benchmark_started_at = utc_now()
    variant_results: list[dict[str, Any]] = []

    try:
        for variant in variants:
            variant_results.append(
                run_variant(
                    variant=variant,
                    cases=cases,
                    lab_root=lab_root,
                    codex_bin=codex_bin,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval_seconds=args.poll_interval_seconds,
                    model_context_window=args.model_context_window,
                    model_auto_compact_token_limit=args.model_auto_compact_token_limit,
                    effort=args.effort,
                )
            )
        status = "completed"
        failure = None
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        failure = {"error": str(exc)}

    all_case_records = [
        case_record
        for variant_result in variant_results
        for case_record in variant_result.get("case_records", [])
    ]
    result = {
        "benchmark": payload.get("benchmark"),
        "version": payload.get("version"),
        "status": status,
        "failure": failure,
        "benchmark_started_at": benchmark_started_at,
        "benchmark_finished_at": utc_now(),
        "lab_root": str(lab_root),
        "codex_bin": str(codex_bin),
        "variants": list(variants),
        "selected_case_ids": sorted(selected_case_ids) if selected_case_ids else None,
        "selected_source_thread_ids": sorted(
            {case["source_thread_id"] for case in cases}
        ),
        "source_thread_profiles": summarize_source_thread_profiles(
            source_thread_profiles,
            {case["source_thread_id"] for case in cases},
        ),
        "case_count": len(cases),
        "model_context_window": args.model_context_window,
        "model_auto_compact_token_limit": args.model_auto_compact_token_limit,
        "effort": args.effort,
        "variant_results": variant_results,
        "cross_variant_summary": summarize_retention(all_case_records),
    }

    if args.output is not None:
        write_result_document(args.output.expanduser().resolve(), result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "completed" else 1


def cmd_run_chain(args: argparse.Namespace) -> int:
    payload = validate_payload(args.cases)
    source_thread_profiles = load_source_thread_profiles(args.cases)
    selected_case_ids = set(args.case_id) if args.case_id else None
    cases = load_cases(args.cases, selected_case_ids)
    if args.source_thread:
        allowed_source_threads = set(args.source_thread)
        cases = [case for case in cases if case["source_thread_id"] in allowed_source_threads]
        if not cases:
            raise SystemExit("no benchmark cases remain after --source-thread filtering")
    variants = tuple(args.variant) if args.variant else DEFAULT_CHAIN_VARIANTS
    lab_root = args.lab_root.expanduser().resolve()
    lab_home = lab_root / "home"
    if not lab_home.exists():
        raise SystemExit(
            f"{lab_home} does not exist. Run `setup-lab` first to create an isolated copied-session lab."
        )
    codex_bin = resolve_requested_codex_bin(args.codex_bin)
    legacy_codex_bin = resolve_requested_codex_bin(args.legacy_codex_bin)
    legacy_sdk_src = args.legacy_sdk_src.expanduser().resolve()

    benchmark_started_at = utc_now()
    variant_results: list[dict[str, Any]] = []

    try:
        for variant in variants:
            if variant == "legacy_collapse":
                variant_results.append(
                    run_external_chain_variant_subprocess(
                        cases_path=args.cases.expanduser().resolve(),
                        variant=variant,
                        source_threads=args.source_thread,
                        case_ids=args.case_id,
                        lab_root=lab_root,
                        codex_bin=legacy_codex_bin,
                        sdk_src=legacy_sdk_src,
                        timeout_seconds=args.timeout_seconds,
                        poll_interval_seconds=args.poll_interval_seconds,
                        chain_model_context_window=args.chain_model_context_window,
                        chain_model_auto_compact_token_limit=(
                            args.chain_model_auto_compact_token_limit
                        ),
                        legacy_collapse_preserve_turns=args.legacy_collapse_preserve_turns,
                        effort=args.effort,
                        chain_hops=args.chain_hops,
                        chain_prompt=args.chain_prompt,
                    )
                )
            else:
                variant_results.append(
                    run_chain_variant(
                        variant=variant,
                        cases=cases,
                        lab_root=lab_root,
                        codex_bin=codex_bin,
                        timeout_seconds=args.timeout_seconds,
                        poll_interval_seconds=args.poll_interval_seconds,
                        chain_model_context_window=args.chain_model_context_window,
                        chain_model_auto_compact_token_limit=(
                            args.chain_model_auto_compact_token_limit
                        ),
                        legacy_collapse_preserve_turns=args.legacy_collapse_preserve_turns,
                        effort=args.effort,
                        chain_hops=args.chain_hops,
                        chain_prompt=args.chain_prompt,
                    )
                )
        status = "completed"
        failure = None
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        failure = {"error": str(exc)}

    all_case_records = [
        case_record
        for variant_result in variant_results
        for case_record in variant_result.get("case_records", [])
    ]
    all_working_state_records = [
        record
        for variant_result in variant_results
        for record in variant_result.get("working_state_records", [])
    ]
    result = {
        "benchmark": payload.get("benchmark"),
        "version": payload.get("version"),
        "status": status,
        "failure": failure,
        "benchmark_started_at": benchmark_started_at,
        "benchmark_finished_at": utc_now(),
        "lab_root": str(lab_root),
        "codex_bin": str(codex_bin),
        "variants": list(variants),
        "selected_case_ids": sorted(selected_case_ids) if selected_case_ids else None,
        "selected_source_thread_ids": sorted(
            {case["source_thread_id"] for case in cases}
        ),
        "source_thread_profiles": summarize_source_thread_profiles(
            source_thread_profiles,
            {case["source_thread_id"] for case in cases},
        ),
        "case_count": len(cases),
        "chain_hops": args.chain_hops,
        "chain_prompt": args.chain_prompt,
        "chain_model_context_window": args.chain_model_context_window,
        "chain_model_auto_compact_token_limit": args.chain_model_auto_compact_token_limit,
        "effort": args.effort,
        "variant_results": variant_results,
        "cross_variant_summary": summarize_chain_retention(all_case_records),
        "per_hop_summary": summarize_case_records_by_hop(all_case_records),
        "working_state_summary": summarize_case_records(all_working_state_records),
        "working_state_per_hop_summary": summarize_case_records_by_hop(
            all_working_state_records
        ),
        "working_state_cross_variant_summary": summarize_chain_retention(
            all_working_state_records
        ),
    }

    if args.output is not None:
        write_result_document(args.output.expanduser().resolve(), result)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "completed" else 1


def cmd_run_chain_variant(args: argparse.Namespace) -> int:
    selected_case_ids = set(args.case_id) if args.case_id else None
    cases = load_cases(args.cases, selected_case_ids)
    if args.source_thread:
        allowed_source_threads = set(args.source_thread)
        cases = [case for case in cases if case["source_thread_id"] in allowed_source_threads]
        if not cases:
            raise SystemExit("no benchmark cases remain after --source-thread filtering")

    lab_root = args.lab_root.expanduser().resolve()
    lab_home = lab_root / "home"
    if not lab_home.exists():
        raise SystemExit(
            f"{lab_home} does not exist. Run `setup-lab` first to create an isolated copied-session lab."
        )

    variant_result = run_chain_variant(
        variant=args.variant,
        cases=cases,
        lab_root=lab_root,
        codex_bin=resolve_requested_codex_bin(args.codex_bin),
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        chain_model_context_window=args.chain_model_context_window,
        chain_model_auto_compact_token_limit=args.chain_model_auto_compact_token_limit,
        legacy_collapse_preserve_turns=args.legacy_collapse_preserve_turns,
        effort=args.effort,
        chain_hops=args.chain_hops,
        chain_prompt=args.chain_prompt,
        sdk_src=args.sdk_src.expanduser().resolve(),
    )
    print(json.dumps(variant_result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "summary":
        return cmd_summary(args)
    if args.command == "probe":
        return cmd_probe(args)
    if args.command == "score":
        return cmd_score(args)
    if args.command == "setup-lab":
        return cmd_setup_lab(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "run-chain":
        return cmd_run_chain(args)
    if args.command == "run-chain-variant":
        return cmd_run_chain_variant(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
