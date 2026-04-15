use std::sync::Arc;
use std::time::Instant;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use crate::Prompt;
use crate::client::ModelClientSession;
use crate::client_common::ResponseEvent;
#[cfg(test)]
use crate::codex::PreviousTurnSettings;
use crate::codex::Session;
use crate::codex::TurnContext;
use crate::codex::get_last_assistant_message_from_turn;
use crate::context_manager::is_user_turn_boundary;
use crate::event_mapping::is_contextual_dev_message_content;
use crate::event_mapping::is_contextual_user_message_content;
use crate::util::backoff;
use codex_analytics::CodexCompactionEvent;
use codex_analytics::CompactionImplementation;
use codex_analytics::CompactionPhase;
use codex_analytics::CompactionReason;
use codex_analytics::CompactionStatus;
use codex_analytics::CompactionStrategy;
use codex_analytics::CompactionTrigger;
use codex_features::Feature;
use codex_protocol::config_types::CompactMode;
use codex_protocol::error::CodexErr;
use codex_protocol::error::Result as CodexResult;
use codex_protocol::items::ContextCompactionItem;
use codex_protocol::items::TurnItem;
use codex_protocol::models::ContentItem;
use codex_protocol::models::FunctionCallOutputContentItem;
use codex_protocol::models::FunctionCallOutputPayload;
use codex_protocol::models::ResponseInputItem;
use codex_protocol::models::ResponseItem;
use codex_protocol::models::function_call_output_content_items_to_text;
use codex_protocol::protocol::CompactedItem;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::TurnStartedEvent;
use codex_protocol::protocol::WarningEvent;
use codex_protocol::user_input::UserInput;
use codex_utils_output_truncation::TruncationPolicy;
use codex_utils_output_truncation::approx_token_count;
use codex_utils_output_truncation::truncate_text;
use futures::prelude::*;
use tracing::error;

pub const SUMMARIZATION_PROMPT: &str = include_str!("../templates/compact/prompt.md");
pub const SUMMARY_PREFIX: &str = include_str!("../templates/compact/summary_prefix.md");
const SUMMARY_PREFIX_COMPAT_ALIASES: &[&str] = &[
    "The following is a summary of the previous conversation:",
    "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:",
];
const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;
const COLLAPSE_PRESERVED_TAIL_MAX_TOKENS: usize = 24_000;
const COLLAPSE_TOOL_CALL_INPUT_MAX_TOKENS: usize = 256;
const COLLAPSE_TOOL_OUTPUT_MAX_TOKENS: usize = 128;
const COLLAPSE_TOOL_CALL_INPUT_MIN_TOKENS: usize = 48;
const COLLAPSE_TOOL_OUTPUT_MIN_TOKENS: usize = 24;

/// Controls whether compaction replacement history must include initial context.
///
/// Pre-turn/manual compaction variants use `DoNotInject`: they replace history with a summary and
/// clear `reference_context_item`, so the next regular turn will fully reinject initial context
/// after compaction.
///
/// Mid-turn compaction must use `BeforeLastUserMessage` because the model is trained to see the
/// compaction summary as the last item in history after mid-turn compaction; we therefore inject
/// initial context into the replacement history just above the last real user message.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum InitialContextInjection {
    BeforeLastUserMessage,
    DoNotInject,
}

#[derive(Clone, Debug, PartialEq)]
struct CollapsePlan {
    compact_prefix: Vec<ResponseItem>,
    preserved_tail: Vec<ResponseItem>,
}

pub(crate) fn should_use_remote_compact_task(
    turn_context: &TurnContext,
    initial_context_injection: InitialContextInjection,
) -> bool {
    if should_use_collapse_compaction(turn_context, initial_context_injection) {
        return false;
    }

    turn_context.provider.is_openai()
}

pub(crate) async fn run_inline_auto_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    initial_context_injection: InitialContextInjection,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    let prompt = turn_context.compact_prompt().to_string();
    let input = vec![UserInput::Text {
        text: prompt,
        // Compaction prompt is synthesized; no UI element ranges to preserve.
        text_elements: Vec::new(),
    }];

    run_compact_task_inner(
        sess,
        turn_context,
        input,
        initial_context_injection,
        CompactionTrigger::Auto,
        reason,
        phase,
    )
    .await?;
    Ok(())
}

pub(crate) async fn run_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    input: Vec<UserInput>,
) -> CodexResult<()> {
    let start_event = EventMsg::TurnStarted(TurnStartedEvent {
        turn_id: turn_context.sub_id.clone(),
        started_at: turn_context.turn_timing_state.started_at_unix_secs().await,
        model_context_window: turn_context.model_context_window(),
        collaboration_mode_kind: turn_context.collaboration_mode.mode,
    });
    sess.send_event(&turn_context, start_event).await;
    run_compact_task_inner(
        sess.clone(),
        turn_context,
        input,
        InitialContextInjection::DoNotInject,
        CompactionTrigger::Manual,
        CompactionReason::UserRequested,
        CompactionPhase::StandaloneTurn,
    )
    .await
}

async fn run_compact_task_inner(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    input: Vec<UserInput>,
    initial_context_injection: InitialContextInjection,
    trigger: CompactionTrigger,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    let attempt = CompactionAnalyticsAttempt::begin(
        sess.as_ref(),
        turn_context.as_ref(),
        trigger,
        reason,
        CompactionImplementation::Responses,
        phase,
    )
    .await;
    let result = run_compact_task_inner_impl(
        Arc::clone(&sess),
        Arc::clone(&turn_context),
        input,
        initial_context_injection,
    )
    .await;
    attempt
        .track(
            sess.as_ref(),
            compaction_status_from_result(&result),
            result.as_ref().err().map(ToString::to_string),
        )
        .await;
    result
}

async fn run_compact_task_inner_impl(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    input: Vec<UserInput>,
    initial_context_injection: InitialContextInjection,
) -> CodexResult<()> {
    let compaction_item = TurnItem::ContextCompaction(ContextCompactionItem::new());
    sess.emit_turn_item_started(&turn_context, &compaction_item)
        .await;
    let initial_input_for_turn: ResponseInputItem = ResponseInputItem::from(input);

    let history_snapshot = sess.clone_history().await;
    let history_items_before_compaction = history_snapshot.raw_items().to_vec();
    let collapse_plan = build_collapse_plan(
        &history_items_before_compaction,
        turn_context.as_ref(),
        initial_context_injection,
    );
    let mut history = history_snapshot;
    if let Some(plan) = &collapse_plan {
        history.replace(plan.compact_prefix.clone());
    }
    history.record_items(
        &[initial_input_for_turn.into()],
        turn_context.truncation_policy,
    );

    let mut truncated_count = 0usize;

    let max_retries = turn_context.provider.stream_max_retries();
    let mut retries = 0;
    let mut client_session = sess.services.model_client.new_session();
    // Reuse one client session so turn-scoped state (sticky routing, websocket incremental
    // request tracking)
    // survives retries within this compact turn.

    loop {
        // Clone is required because of the loop
        let turn_input = history
            .clone()
            .for_prompt(&turn_context.model_info.input_modalities);
        let turn_input_len = turn_input.len();
        let prompt = Prompt {
            input: turn_input,
            base_instructions: sess.get_base_instructions().await,
            personality: turn_context.personality,
            ..Default::default()
        };
        let turn_metadata_header = turn_context.turn_metadata_state.current_header_value();
        let attempt_result = drain_to_completed(
            &sess,
            turn_context.as_ref(),
            &mut client_session,
            turn_metadata_header.as_deref(),
            &prompt,
        )
        .await;

        match attempt_result {
            Ok(()) => {
                if truncated_count > 0 {
                    sess.notify_background_event(
                        turn_context.as_ref(),
                        format!(
                            "Trimmed {truncated_count} older thread item(s) before compacting so the prompt fits the model context window."
                        ),
                    )
                    .await;
                }
                break;
            }
            Err(CodexErr::Interrupted) => {
                return Err(CodexErr::Interrupted);
            }
            Err(e @ CodexErr::ContextWindowExceeded) => {
                if turn_input_len > 1 {
                    // Trim from the beginning to preserve cache (prefix-based) and keep recent messages intact.
                    error!(
                        "Context window exceeded while compacting; removing oldest history item. Error: {e}"
                    );
                    history.remove_first_item();
                    truncated_count += 1;
                    retries = 0;
                    continue;
                }
                sess.set_total_tokens_full(turn_context.as_ref()).await;
                let event = EventMsg::Error(e.to_error_event(/*message_prefix*/ None));
                sess.send_event(&turn_context, event).await;
                return Err(e);
            }
            Err(e) => {
                if retries < max_retries {
                    retries += 1;
                    let delay = backoff(retries);
                    sess.notify_stream_error(
                        turn_context.as_ref(),
                        format!("Reconnecting... {retries}/{max_retries}"),
                        e,
                    )
                    .await;
                    tokio::time::sleep(delay).await;
                    continue;
                } else {
                    let event = EventMsg::Error(e.to_error_event(/*message_prefix*/ None));
                    sess.send_event(&turn_context, event).await;
                    return Err(e);
                }
            }
        }
    }

    let history_snapshot = sess.clone_history().await;
    let history_items = history_snapshot.raw_items();
    let summary_suffix = get_last_assistant_message_from_turn(history_items).unwrap_or_default();
    let summary_text = format!("{SUMMARY_PREFIX}\n{summary_suffix}");
    let mut new_history = if let Some(plan) = collapse_plan {
        build_collapse_compacted_history(&summary_text, &plan.preserved_tail)
    } else {
        let user_messages = collect_user_messages(&history_items_before_compaction);
        build_compacted_history(Vec::new(), &user_messages, &summary_text)
    };

    if matches!(
        initial_context_injection,
        InitialContextInjection::BeforeLastUserMessage
    ) {
        let initial_context = sess.build_initial_context(turn_context.as_ref()).await;
        new_history =
            insert_initial_context_before_last_real_user_or_summary(new_history, initial_context);
    }
    let ghost_snapshots: Vec<ResponseItem> = history_items_before_compaction
        .iter()
        .filter(|item| matches!(item, ResponseItem::GhostSnapshot { .. }))
        .cloned()
        .collect();
    new_history.extend(ghost_snapshots);
    let reference_context_item = match initial_context_injection {
        InitialContextInjection::DoNotInject => None,
        InitialContextInjection::BeforeLastUserMessage => Some(turn_context.to_turn_context_item()),
    };
    let compacted_item = CompactedItem {
        message: summary_text.clone(),
        replacement_history: Some(new_history.clone()),
    };
    sess.replace_compacted_history(new_history, reference_context_item, compacted_item)
        .await;
    client_session.reset_websocket_session();
    sess.recompute_token_usage(&turn_context).await;

    sess.emit_turn_item_completed(&turn_context, compaction_item)
        .await;
    let warning = EventMsg::Warning(WarningEvent {
        message: "Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible to keep threads small and targeted.".to_string(),
    });
    sess.send_event(&turn_context, warning).await;
    Ok(())
}

pub(crate) struct CompactionAnalyticsAttempt {
    enabled: bool,
    thread_id: String,
    turn_id: String,
    trigger: CompactionTrigger,
    reason: CompactionReason,
    implementation: CompactionImplementation,
    phase: CompactionPhase,
    active_context_tokens_before: i64,
    started_at: u64,
    start_instant: Instant,
}

impl CompactionAnalyticsAttempt {
    pub(crate) async fn begin(
        sess: &Session,
        turn_context: &TurnContext,
        trigger: CompactionTrigger,
        reason: CompactionReason,
        implementation: CompactionImplementation,
        phase: CompactionPhase,
    ) -> Self {
        let enabled = sess.enabled(Feature::GeneralAnalytics);
        let active_context_tokens_before = sess.get_total_token_usage().await;
        Self {
            enabled,
            thread_id: sess.conversation_id.to_string(),
            turn_id: turn_context.sub_id.clone(),
            trigger,
            reason,
            implementation,
            phase,
            active_context_tokens_before,
            started_at: now_unix_seconds(),
            start_instant: Instant::now(),
        }
    }

    pub(crate) async fn track(
        self,
        sess: &Session,
        status: CompactionStatus,
        error: Option<String>,
    ) {
        if !self.enabled {
            return;
        }
        let active_context_tokens_after = sess.get_total_token_usage().await;
        sess.services
            .analytics_events_client
            .track_compaction(CodexCompactionEvent {
                thread_id: self.thread_id,
                turn_id: self.turn_id,
                trigger: self.trigger,
                reason: self.reason,
                implementation: self.implementation,
                phase: self.phase,
                strategy: CompactionStrategy::Memento,
                status,
                error,
                active_context_tokens_before: self.active_context_tokens_before,
                active_context_tokens_after,
                started_at: self.started_at,
                completed_at: now_unix_seconds(),
                duration_ms: Some(
                    u64::try_from(self.start_instant.elapsed().as_millis()).unwrap_or(u64::MAX),
                ),
            });
    }
}

pub(crate) fn compaction_status_from_result<T>(result: &CodexResult<T>) -> CompactionStatus {
    match result {
        Ok(_) => CompactionStatus::Completed,
        Err(CodexErr::Interrupted | CodexErr::TurnAborted) => CompactionStatus::Interrupted,
        Err(_) => CompactionStatus::Failed,
    }
}

fn now_unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

pub fn content_items_to_text(content: &[ContentItem]) -> Option<String> {
    let mut pieces = Vec::new();
    for item in content {
        match item {
            ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                if !text.is_empty() {
                    pieces.push(text.as_str());
                }
            }
            ContentItem::InputImage { .. } => {}
        }
    }
    if pieces.is_empty() {
        None
    } else {
        Some(pieces.join("\n"))
    }
}

pub(crate) fn collect_user_messages(items: &[ResponseItem]) -> Vec<String> {
    items
        .iter()
        .filter_map(|item| match crate::event_mapping::parse_turn_item(item) {
            Some(TurnItem::UserMessage(user)) => {
                if is_summary_message(&user.message()) {
                    None
                } else {
                    Some(user.message())
                }
            }
            _ => None,
        })
        .collect()
}

pub(crate) fn is_summary_message(message: &str) -> bool {
    summary_prefixes()
        .iter()
        .any(|prefix| message.starts_with(format!("{prefix}\n").as_str()))
}

fn summary_prefixes() -> Vec<&'static str> {
    let mut prefixes = vec![SUMMARY_PREFIX];
    for alias in SUMMARY_PREFIX_COMPAT_ALIASES {
        if !prefixes.contains(alias) {
            prefixes.push(alias);
        }
    }
    prefixes
}

fn should_use_collapse_compaction(
    turn_context: &TurnContext,
    initial_context_injection: InitialContextInjection,
) -> bool {
    matches!(turn_context.config.compact_mode, CompactMode::Collapse)
        && matches!(
            initial_context_injection,
            InitialContextInjection::DoNotInject
        )
}

fn build_collapse_plan(
    items: &[ResponseItem],
    turn_context: &TurnContext,
    initial_context_injection: InitialContextInjection,
) -> Option<CollapsePlan> {
    if !should_use_collapse_compaction(turn_context, initial_context_injection) {
        return None;
    }

    let split_index = preserved_tail_split_index(items, turn_context.config.compact_preserve_turns);
    if split_index == 0 {
        return None;
    }

    Some(CollapsePlan {
        compact_prefix: items[..split_index].to_vec(),
        preserved_tail: sanitize_preserved_tail(&items[split_index..]),
    })
}

fn preserved_tail_split_index(items: &[ResponseItem], preserve_turns: u32) -> usize {
    let boundary_positions: Vec<usize> = items
        .iter()
        .enumerate()
        .filter_map(|(idx, item)| is_preservable_turn_boundary(item).then_some(idx))
        .collect();

    let mut split_index = if preserve_turns == 0 || boundary_positions.is_empty() {
        items.len()
    } else {
        let preserve_turns = usize::try_from(preserve_turns).unwrap_or(usize::MAX);
        if preserve_turns >= boundary_positions.len() {
            boundary_positions[0]
        } else {
            boundary_positions[boundary_positions.len() - preserve_turns]
        }
    };

    while split_index > 0 && is_pre_turn_context_update(&items[split_index - 1]) {
        split_index -= 1;
    }

    split_index
}

fn is_preservable_turn_boundary(item: &ResponseItem) -> bool {
    is_user_turn_boundary(item) && !is_summary_response_item(item)
}

fn is_summary_response_item(item: &ResponseItem) -> bool {
    let ResponseItem::Message { role, content, .. } = item else {
        return false;
    };
    if role != "user" {
        return false;
    }

    content_items_to_text(content)
        .map(|text| is_summary_message(&text))
        .unwrap_or(false)
}

fn is_pre_turn_context_update(item: &ResponseItem) -> bool {
    match item {
        ResponseItem::Message { role, content, .. }
            if role == "developer" && is_contextual_dev_message_content(content) =>
        {
            true
        }
        ResponseItem::Message { role, content, .. }
            if role == "user" && is_contextual_user_message_content(content) =>
        {
            true
        }
        _ => false,
    }
}

fn sanitize_preserved_tail(items: &[ResponseItem]) -> Vec<ResponseItem> {
    let sanitized: Vec<ResponseItem> = items
        .iter()
        .filter(|item| {
            !matches!(item, ResponseItem::GhostSnapshot { .. }) && !is_compaction_artifact(item)
        })
        .cloned()
        .collect();
    microcompact_preserved_tail(sanitized)
}

fn is_compaction_artifact(item: &ResponseItem) -> bool {
    matches!(item, ResponseItem::Compaction { .. }) || is_summary_response_item(item)
}

fn microcompact_preserved_tail(mut items: Vec<ResponseItem>) -> Vec<ResponseItem> {
    if preserved_tail_token_estimate(&items) <= COLLAPSE_PRESERVED_TAIL_MAX_TOKENS {
        return items;
    }

    microcompact_preserved_tail_pass(
        &mut items,
        COLLAPSE_TOOL_CALL_INPUT_MAX_TOKENS,
        COLLAPSE_TOOL_OUTPUT_MAX_TOKENS,
    );
    if preserved_tail_token_estimate(&items) <= COLLAPSE_PRESERVED_TAIL_MAX_TOKENS {
        return items;
    }

    microcompact_preserved_tail_pass(
        &mut items,
        COLLAPSE_TOOL_CALL_INPUT_MIN_TOKENS,
        COLLAPSE_TOOL_OUTPUT_MIN_TOKENS,
    );
    items
}

fn microcompact_preserved_tail_pass(
    items: &mut [ResponseItem],
    tool_call_input_max_tokens: usize,
    tool_output_max_tokens: usize,
) {
    for idx in 0..items.len() {
        if preserved_tail_token_estimate(items) <= COLLAPSE_PRESERVED_TAIL_MAX_TOKENS {
            break;
        }
        microcompact_preserved_item(
            &mut items[idx],
            tool_call_input_max_tokens,
            tool_output_max_tokens,
        );
    }
}

fn microcompact_preserved_item(
    item: &mut ResponseItem,
    tool_call_input_max_tokens: usize,
    tool_output_max_tokens: usize,
) {
    match item {
        ResponseItem::FunctionCall { arguments, .. } => {
            truncate_string_in_place(arguments, tool_call_input_max_tokens);
        }
        ResponseItem::CustomToolCall { input, .. } => {
            truncate_string_in_place(input, tool_call_input_max_tokens);
        }
        ResponseItem::FunctionCallOutput { output, .. }
        | ResponseItem::CustomToolCallOutput { output, .. } => {
            truncate_function_call_output_payload(output, tool_output_max_tokens);
        }
        _ => {}
    }
}

fn truncate_string_in_place(text: &mut String, max_tokens: usize) {
    if approx_token_count(text) > max_tokens {
        *text = truncate_text(text, TruncationPolicy::Tokens(max_tokens));
    }
}

fn truncate_function_call_output_payload(
    output: &mut FunctionCallOutputPayload,
    max_tokens: usize,
) {
    if let Some(text) = output.text_content_mut() {
        truncate_string_in_place(text, max_tokens);
        return;
    }

    let Some(content_items) = output.content_items() else {
        return;
    };
    let Some(text) = function_call_output_content_items_to_text(content_items) else {
        return;
    };
    if approx_token_count(&text) <= max_tokens {
        return;
    }

    let success = output.success;
    *output = FunctionCallOutputPayload::from_content_items(vec![
        FunctionCallOutputContentItem::InputText {
            text: truncate_text(&text, TruncationPolicy::Tokens(max_tokens)),
        },
    ]);
    output.success = success;
}

fn preserved_tail_token_estimate(items: &[ResponseItem]) -> usize {
    items.iter().map(estimate_response_item_tokens).sum()
}

fn estimate_response_item_tokens(item: &ResponseItem) -> usize {
    match item {
        ResponseItem::Message { content, .. } => content_items_to_text(content)
            .map(|text| approx_token_count(&text))
            .unwrap_or(0),
        ResponseItem::FunctionCall {
            name,
            namespace,
            arguments,
            ..
        } => {
            approx_token_count(name)
                + namespace
                    .as_ref()
                    .map(|value| approx_token_count(value))
                    .unwrap_or(0)
                + approx_token_count(arguments)
        }
        ResponseItem::CustomToolCall { name, input, .. } => {
            approx_token_count(name) + approx_token_count(input)
        }
        ResponseItem::FunctionCallOutput { output, .. }
        | ResponseItem::CustomToolCallOutput { output, .. } => output
            .body
            .to_text()
            .map(|text| approx_token_count(&text))
            .unwrap_or(0),
        _ => 0,
    }
}

fn build_collapse_compacted_history(
    summary_text: &str,
    preserved_tail: &[ResponseItem],
) -> Vec<ResponseItem> {
    let mut history = Vec::with_capacity(preserved_tail.len().saturating_add(1));
    history.push(summary_message_item(summary_text));
    history.extend_from_slice(preserved_tail);
    history
}

/// Inserts canonical initial context into compacted replacement history at the
/// model-expected boundary.
///
/// Placement rules:
/// - Prefer immediately before the last real user message.
/// - If no real user messages remain, insert before the compaction summary so
///   the summary stays last.
/// - If there are no user messages, insert before the last compaction item so
///   that item remains last (remote compaction may return only compaction items).
/// - If there are no user messages or compaction items, append the context.
pub(crate) fn insert_initial_context_before_last_real_user_or_summary(
    mut compacted_history: Vec<ResponseItem>,
    initial_context: Vec<ResponseItem>,
) -> Vec<ResponseItem> {
    let mut last_user_or_summary_index = None;
    let mut last_real_user_index = None;
    for (i, item) in compacted_history.iter().enumerate().rev() {
        let Some(TurnItem::UserMessage(user)) = crate::event_mapping::parse_turn_item(item) else {
            continue;
        };
        // Compaction summaries are encoded as user messages, so track both:
        // the last real user message (preferred insertion point) and the last
        // user-message-like item (fallback summary insertion point).
        last_user_or_summary_index.get_or_insert(i);
        if !is_summary_message(&user.message()) {
            last_real_user_index = Some(i);
            break;
        }
    }
    let last_compaction_index = compacted_history
        .iter()
        .enumerate()
        .rev()
        .find_map(|(i, item)| matches!(item, ResponseItem::Compaction { .. }).then_some(i));
    let insertion_index = last_real_user_index
        .or(last_user_or_summary_index)
        .or(last_compaction_index);

    // Re-inject canonical context from the current session since we stripped it
    // from the pre-compaction history. Prefer placing it before the last real
    // user message; if there is no real user message left, place it before the
    // summary or compaction item so the compaction item remains last.
    if let Some(insertion_index) = insertion_index {
        compacted_history.splice(insertion_index..insertion_index, initial_context);
    } else {
        compacted_history.extend(initial_context);
    }

    compacted_history
}

pub(crate) fn build_compacted_history(
    initial_context: Vec<ResponseItem>,
    user_messages: &[String],
    summary_text: &str,
) -> Vec<ResponseItem> {
    build_compacted_history_with_limit(
        initial_context,
        user_messages,
        summary_text,
        COMPACT_USER_MESSAGE_MAX_TOKENS,
    )
}

fn build_compacted_history_with_limit(
    mut history: Vec<ResponseItem>,
    user_messages: &[String],
    summary_text: &str,
    max_tokens: usize,
) -> Vec<ResponseItem> {
    let mut selected_messages: Vec<String> = Vec::new();
    if max_tokens > 0 {
        let mut remaining = max_tokens;
        for message in user_messages.iter().rev() {
            if remaining == 0 {
                break;
            }
            let tokens = approx_token_count(message);
            if tokens <= remaining {
                selected_messages.push(message.clone());
                remaining = remaining.saturating_sub(tokens);
            } else {
                let truncated = truncate_text(message, TruncationPolicy::Tokens(remaining));
                selected_messages.push(truncated);
                break;
            }
        }
        selected_messages.reverse();
    }

    for message in &selected_messages {
        history.push(ResponseItem::Message {
            id: None,
            role: "user".to_string(),
            content: vec![ContentItem::InputText {
                text: message.clone(),
            }],
            end_turn: None,
            phase: None,
        });
    }

    history.push(summary_message_item(summary_text));

    history
}

fn summary_message_item(summary_text: &str) -> ResponseItem {
    let summary_text = if summary_text.is_empty() {
        "(no summary available)".to_string()
    } else {
        summary_text.to_string()
    };

    ResponseItem::Message {
        id: None,
        role: "user".to_string(),
        content: vec![ContentItem::InputText { text: summary_text }],
        end_turn: None,
        phase: None,
    }
}

async fn drain_to_completed(
    sess: &Session,
    turn_context: &TurnContext,
    client_session: &mut ModelClientSession,
    turn_metadata_header: Option<&str>,
    prompt: &Prompt,
) -> CodexResult<()> {
    let mut stream = client_session
        .stream(
            prompt,
            &turn_context.model_info,
            &turn_context.session_telemetry,
            turn_context.reasoning_effort,
            turn_context.reasoning_summary,
            turn_context.config.service_tier,
            turn_metadata_header,
        )
        .await?;
    loop {
        let maybe_event = stream.next().await;
        let Some(event) = maybe_event else {
            return Err(CodexErr::Stream(
                "stream closed before response.completed".into(),
                None,
            ));
        };
        match event {
            Ok(ResponseEvent::OutputItemDone(item)) => {
                sess.record_into_history(std::slice::from_ref(&item), turn_context)
                    .await;
            }
            Ok(ResponseEvent::ServerReasoningIncluded(included)) => {
                sess.set_server_reasoning_included(included).await;
            }
            Ok(ResponseEvent::RateLimits(snapshot)) => {
                sess.update_rate_limits(turn_context, snapshot).await;
            }
            Ok(ResponseEvent::Completed { token_usage, .. }) => {
                sess.update_token_usage_info(turn_context, token_usage.as_ref())
                    .await;
                return Ok(());
            }
            Ok(_) => continue,
            Err(e) => return Err(e),
        }
    }
}

#[cfg(test)]
#[path = "compact_tests.rs"]
mod tests;
