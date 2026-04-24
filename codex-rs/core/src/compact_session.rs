use std::sync::Arc;

use crate::Prompt;
use crate::compact::CompactionAnalyticsAttempt;
use crate::compact::InitialContextInjection;
use crate::compact::SUMMARY_PREFIX;
use crate::compact::compaction_status_from_result;
use crate::compact::drain_to_completed;
use crate::compact::insert_initial_context_before_last_real_user_or_summary;
use crate::compact_frontier::StructuredFrontierConfig;
use crate::session::session::Session;
use crate::session::turn::get_last_assistant_message_from_turn;
use crate::session::turn_context::TurnContext;
use crate::util::backoff;
use codex_analytics::CompactionImplementation;
use codex_analytics::CompactionPhase;
use codex_analytics::CompactionReason;
use codex_analytics::CompactionStrategy;
use codex_analytics::CompactionTrigger;
use codex_model_provider_info::ModelProviderInfo;
use codex_protocol::config_types::CompactStrategy;
use codex_protocol::error::CodexErr;
use codex_protocol::error::Result as CodexResult;
use codex_protocol::items::ContextCompactionItem;
use codex_protocol::items::TurnItem;
use codex_protocol::models::ContentItem;
use codex_protocol::models::ResponseInputItem;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::CompactedImageReference;
use codex_protocol::protocol::CompactedImageSidecar;
use codex_protocol::protocol::CompactedItem;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::TurnStartedEvent;
use codex_protocol::protocol::WarningEvent;
use codex_protocol::user_input::UserInput;
use codex_utils_output_truncation::TruncationPolicy;
use codex_utils_output_truncation::truncate_text;
use tracing::error;

const SESSION_COMPACT_PROMPT: &str = include_str!("../templates/session_compact/prompt.md");
const SESSION_COMPACT_OPEN_MARKER: &str = "<session_compact_state>";
const SESSION_COMPACT_CLOSE_MARKER: &str = "</session_compact_state>";
const LEGACY_SESSION_COMPACT_OPEN_MARKER: &str = "<session_compact_state_v1>";
const LEGACY_SESSION_COMPACT_CLOSE_MARKER: &str = "</session_compact_state_v1>";
const SESSION_COMPACT_FRONTIER_BASE_TURNS: usize = 2;
const SESSION_COMPACT_FRONTIER_MAX_ACTIVE_TURNS: usize = 5;
const SESSION_COMPACT_SECTION_HEADERS: [&str; 5] = [
    "Objective:",
    "Active Memory:",
    "Inactive Changes:",
    "Current Handoff:",
    "Evidence Pointers:",
];
const SESSION_COMPACT_TAIL_MIN_TOKENS: usize = 4_000;
const SESSION_COMPACT_TAIL_FRACTION_DENOMINATOR: usize = 10;
const SESSION_COMPACT_TOOL_CALL_INPUT_MAX_TOKENS: usize = 256;
const SESSION_COMPACT_TOOL_OUTPUT_MAX_TOKENS: usize = 128;
const SESSION_COMPACT_TOOL_CALL_INPUT_MIN_TOKENS: usize = 48;
const SESSION_COMPACT_TOOL_OUTPUT_MIN_TOKENS: usize = 24;
const SESSION_COMPACT_MESSAGE_MAX_TOKENS: usize = 256;
const SESSION_COMPACT_MESSAGE_MIN_TOKENS: usize = 64;
const SESSION_COMPACT_IMAGE_SIDECAR_MAX_IMAGES: usize = 4;
const SESSION_COMPACT_IMAGE_TEXT_MAX_TOKENS: usize = 300;
const CURRENT_HANDOFF_INDEX: usize = 3;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum CompactRoute {
    Session,
    Remote,
    Local,
}

pub(crate) fn select_compact_route(
    compact_strategy: CompactStrategy,
    provider: &ModelProviderInfo,
) -> CompactRoute {
    if matches!(compact_strategy, CompactStrategy::Session) {
        CompactRoute::Session
    } else if crate::compact::should_use_remote_compact_task(provider) {
        CompactRoute::Remote
    } else {
        CompactRoute::Local
    }
}

pub(crate) async fn run_inline_session_auto_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    initial_context_injection: InitialContextInjection,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    run_session_compact_task_inner(
        sess,
        turn_context,
        build_session_compact_input,
        initial_context_injection,
        CompactionTrigger::Auto,
        reason,
        phase,
    )
    .await
}

pub(crate) async fn run_session_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    _input: Vec<UserInput>,
) -> CodexResult<()> {
    let start_event = EventMsg::TurnStarted(TurnStartedEvent {
        turn_id: turn_context.sub_id.clone(),
        started_at: turn_context.turn_timing_state.started_at_unix_secs().await,
        model_context_window: turn_context.model_context_window(),
        collaboration_mode_kind: turn_context.collaboration_mode.mode,
    });
    sess.send_event(&turn_context, start_event).await;
    run_session_compact_task_inner(
        sess,
        turn_context,
        build_session_compact_input,
        InitialContextInjection::DoNotInject,
        CompactionTrigger::Manual,
        CompactionReason::UserRequested,
        CompactionPhase::StandaloneTurn,
    )
    .await
}

async fn run_session_compact_task_inner(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    input_builder: fn(&TurnContext) -> Vec<UserInput>,
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
        CompactionStrategy::SessionCompact,
        CompactionImplementation::Responses,
        phase,
    )
    .await;
    let result = run_session_compact_task_inner_impl(
        Arc::clone(&sess),
        Arc::clone(&turn_context),
        input_builder(turn_context.as_ref()),
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

async fn run_session_compact_task_inner_impl(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    input: Vec<UserInput>,
    initial_context_injection: InitialContextInjection,
) -> CodexResult<()> {
    let compaction_item = TurnItem::ContextCompaction(ContextCompactionItem::new());
    sess.emit_turn_item_started(&turn_context, &compaction_item)
        .await;
    let initial_input_for_turn: ResponseInputItem = ResponseInputItem::from(input);

    let mut history = sess.clone_history().await;
    let full_context_tokens = history.estimate_token_count(turn_context.as_ref());
    let frontier_source_items = history.raw_items().to_vec();
    let frontier_config =
        session_frontier_config(full_context_tokens, turn_context.model_context_window());
    history.record_items(
        &[initial_input_for_turn.into()],
        turn_context.truncation_policy,
    );

    let mut truncated_count = 0usize;
    let max_retries = turn_context.provider.info().stream_max_retries();
    let mut retries = 0;
    let mut client_session = sess.services.model_client.new_session();

    loop {
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
            Err(CodexErr::Interrupted) => return Err(CodexErr::Interrupted),
            Err(e @ CodexErr::ContextWindowExceeded) => {
                if turn_input_len > 1 {
                    error!(
                        "Context window exceeded while session compacting; removing oldest history item. Error: {e}"
                    );
                    history.remove_first_item();
                    truncated_count += 1;
                    retries = 0;
                    continue;
                }
                sess.set_total_tokens_full(turn_context.as_ref()).await;
                let event = EventMsg::Error(e.to_error_event(None));
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
                }
                let event = EventMsg::Error(e.to_error_event(None));
                sess.send_event(&turn_context, event).await;
                return Err(e);
            }
        }
    }

    let history_snapshot = sess.clone_history().await;
    let history_items = history_snapshot.raw_items();
    let summary_suffix = get_last_assistant_message_from_turn(history_items).unwrap_or_default();
    let normalized_state = normalize_session_compact_state(&summary_suffix);
    let summary_text = format!("{SUMMARY_PREFIX}\n{normalized_state}");

    let mut new_history = build_session_compacted_history(
        Vec::new(),
        &frontier_source_items,
        &summary_text,
        frontier_config,
    );

    if matches!(
        initial_context_injection,
        InitialContextInjection::BeforeLastUserMessage
    ) {
        let initial_context = sess.build_initial_context(turn_context.as_ref()).await;
        new_history =
            insert_initial_context_before_last_real_user_or_summary(new_history, initial_context);
    }

    let ghost_snapshots: Vec<ResponseItem> = history_items
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
        image_sidecar: build_session_image_sidecar(&frontier_source_items),
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

fn build_session_compact_input(turn_context: &TurnContext) -> Vec<UserInput> {
    vec![UserInput::Text {
        text: session_compact_prompt(turn_context),
        text_elements: Vec::new(),
    }]
}

fn session_compact_prompt(turn_context: &TurnContext) -> String {
    let mut prompt = SESSION_COMPACT_PROMPT.to_string();
    if let Some(custom_prompt) = turn_context.compact_prompt.as_deref() {
        prompt.push_str(
            "\n\nAdditional custom compact guidance (follow this only when it does not conflict with the required output block):\n",
        );
        prompt.push_str(custom_prompt);
    }
    prompt
}

fn build_session_compacted_history(
    mut history: Vec<ResponseItem>,
    history_items: &[ResponseItem],
    summary_text: &str,
    frontier_config: StructuredFrontierConfig,
) -> Vec<ResponseItem> {
    history.extend(select_recent_structured_frontier(
        history_items,
        frontier_config,
    ));
    history.push(user_text_message(summary_text.to_string()));
    history
}

fn build_session_image_sidecar(history_items: &[ResponseItem]) -> Option<CompactedImageSidecar> {
    let mut recent_user_images = Vec::new();

    for item in history_items.iter().rev() {
        let Some(TurnItem::UserMessage(user)) = crate::event_mapping::parse_turn_item(item) else {
            continue;
        };

        let user_text = user.message();
        let user_text = if user_text.trim().is_empty() {
            None
        } else {
            Some(truncate_text(
                user_text.trim(),
                TruncationPolicy::Tokens(SESSION_COMPACT_IMAGE_TEXT_MAX_TOKENS),
            ))
        };

        for input in user.content.iter().rev() {
            let UserInput::Image { image_url } = input else {
                continue;
            };
            recent_user_images.push(CompactedImageReference {
                image_url: image_url.clone(),
                user_text: user_text.clone(),
            });
            if recent_user_images.len() >= SESSION_COMPACT_IMAGE_SIDECAR_MAX_IMAGES {
                recent_user_images.reverse();
                return Some(CompactedImageSidecar { recent_user_images });
            }
        }
    }

    if recent_user_images.is_empty() {
        None
    } else {
        recent_user_images.reverse();
        Some(CompactedImageSidecar { recent_user_images })
    }
}

fn select_recent_structured_frontier(
    items: &[ResponseItem],
    config: StructuredFrontierConfig,
) -> Vec<ResponseItem> {
    crate::compact_frontier::select_recent_structured_frontier(items, config)
}

#[cfg(test)]
fn frontier_token_estimate(items: &[ResponseItem]) -> usize {
    crate::compact_frontier::frontier_token_estimate(items)
}

fn session_frontier_config(
    full_context_tokens: Option<i64>,
    model_context_window: Option<i64>,
) -> StructuredFrontierConfig {
    StructuredFrontierConfig {
        preserve_turns: SESSION_COMPACT_FRONTIER_BASE_TURNS,
        max_active_turns: SESSION_COMPACT_FRONTIER_MAX_ACTIVE_TURNS,
        max_total_tokens: session_frontier_token_budget(full_context_tokens, model_context_window),
        tool_call_input_max_tokens: SESSION_COMPACT_TOOL_CALL_INPUT_MAX_TOKENS,
        tool_output_max_tokens: SESSION_COMPACT_TOOL_OUTPUT_MAX_TOKENS,
        tool_call_input_min_tokens: SESSION_COMPACT_TOOL_CALL_INPUT_MIN_TOKENS,
        tool_output_min_tokens: SESSION_COMPACT_TOOL_OUTPUT_MIN_TOKENS,
        message_max_tokens: SESSION_COMPACT_MESSAGE_MAX_TOKENS,
        message_min_tokens: SESSION_COMPACT_MESSAGE_MIN_TOKENS,
    }
}

fn session_frontier_token_budget(
    full_context_tokens: Option<i64>,
    model_context_window: Option<i64>,
) -> usize {
    let cap = model_context_window
        .and_then(|value| usize::try_from(value.max(0)).ok())
        .map(|value| value / SESSION_COMPACT_TAIL_FRACTION_DENOMINATOR)
        .filter(|value| *value > 0)
        .unwrap_or(SESSION_COMPACT_TAIL_MIN_TOKENS);

    let dynamic = full_context_tokens
        .and_then(|value| usize::try_from(value.max(0)).ok())
        .map(|value| value / SESSION_COMPACT_TAIL_FRACTION_DENOMINATOR)
        .unwrap_or(cap);

    let floor = cap.min(SESSION_COMPACT_TAIL_MIN_TOKENS);
    dynamic.clamp(floor, cap.max(floor))
}

fn user_text_message(text: String) -> ResponseItem {
    ResponseItem::Message {
        id: None,
        role: "user".to_string(),
        content: vec![ContentItem::InputText { text }],
        end_turn: None,
        phase: None,
    }
}

pub(crate) fn normalize_session_compact_state(raw: &str) -> String {
    let mut sections: [Vec<String>; SESSION_COMPACT_SECTION_HEADERS.len()] =
        std::array::from_fn(|_| Vec::new());
    let body = extract_session_compact_state_body(raw);
    let mut current_section: Option<usize> = None;
    let mut saw_header = false;

    for line in body.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty()
            || is_session_compact_open_marker(trimmed)
            || is_session_compact_close_marker(trimmed)
        {
            continue;
        }

        if let Some(index) = SESSION_COMPACT_SECTION_HEADERS
            .iter()
            .position(|header| trimmed.eq_ignore_ascii_case(header))
        {
            current_section = Some(index);
            saw_header = true;
            continue;
        }

        let target_section = current_section.unwrap_or(CURRENT_HANDOFF_INDEX);
        sections[target_section].push(normalize_state_line(trimmed));
    }

    if !saw_header && sections[CURRENT_HANDOFF_INDEX].is_empty() && !body.trim().is_empty() {
        sections[CURRENT_HANDOFF_INDEX].push("- none".to_string());
    }

    for entries in &mut sections {
        if entries.is_empty() {
            entries.push("- none".to_string());
        }
    }

    let mut normalized = String::new();
    normalized.push_str(SESSION_COMPACT_OPEN_MARKER);
    normalized.push('\n');
    for (index, header) in SESSION_COMPACT_SECTION_HEADERS.iter().enumerate() {
        if index > 0 {
            normalized.push('\n');
        }
        normalized.push_str(header);
        normalized.push('\n');
        for entry in &sections[index] {
            normalized.push_str(entry);
            normalized.push('\n');
        }
    }
    normalized.push_str(SESSION_COMPACT_CLOSE_MARKER);
    normalized
}

fn extract_session_compact_state_body(raw: &str) -> &str {
    let trimmed = raw.trim();
    for (open_marker, close_marker) in [
        (SESSION_COMPACT_OPEN_MARKER, SESSION_COMPACT_CLOSE_MARKER),
        (
            LEGACY_SESSION_COMPACT_OPEN_MARKER,
            LEGACY_SESSION_COMPACT_CLOSE_MARKER,
        ),
    ] {
        if let Some(start) = trimmed.find(open_marker) {
            let after_open = &trimmed[start + open_marker.len()..];
            if let Some(end) = after_open.find(close_marker) {
                return after_open[..end].trim();
            }
        }
    }
    trimmed
}

fn is_session_compact_open_marker(text: &str) -> bool {
    text == SESSION_COMPACT_OPEN_MARKER || text == LEGACY_SESSION_COMPACT_OPEN_MARKER
}

fn is_session_compact_close_marker(text: &str) -> bool {
    text == SESSION_COMPACT_CLOSE_MARKER || text == LEGACY_SESSION_COMPACT_CLOSE_MARKER
}

fn normalize_state_line(line: &str) -> String {
    let trimmed = line
        .trim()
        .trim_start_matches(|c: char| c == '-' || c == '*' || c.is_whitespace())
        .trim();
    if trimmed.is_empty() {
        "- none".to_string()
    } else {
        format!("- {trimmed}")
    }
}

#[cfg(test)]
mod tests {
    use super::CompactRoute;
    use super::LEGACY_SESSION_COMPACT_CLOSE_MARKER;
    use super::LEGACY_SESSION_COMPACT_OPEN_MARKER;
    use super::SESSION_COMPACT_CLOSE_MARKER;
    use super::SESSION_COMPACT_OPEN_MARKER;
    use super::SESSION_COMPACT_TAIL_MIN_TOKENS;
    use super::build_session_compacted_history;
    use super::build_session_image_sidecar;
    use super::frontier_token_estimate;
    use super::normalize_session_compact_state;
    use super::select_compact_route;
    use super::select_recent_structured_frontier;
    use super::session_frontier_token_budget;
    use crate::compact_frontier::StructuredFrontierConfig;
    use codex_model_provider_info::ModelProviderInfo;
    use codex_model_provider_info::WireApi;
    use codex_protocol::config_types::CompactStrategy;
    use codex_protocol::models::ContentItem;
    use codex_protocol::models::ResponseItem;

    fn test_frontier_config(max_total_tokens: usize) -> StructuredFrontierConfig {
        StructuredFrontierConfig {
            preserve_turns: 2,
            max_active_turns: 5,
            max_total_tokens,
            tool_call_input_max_tokens: 256,
            tool_output_max_tokens: 128,
            tool_call_input_min_tokens: 48,
            tool_output_min_tokens: 24,
            message_max_tokens: 256,
            message_min_tokens: 64,
        }
    }

    #[test]
    fn select_compact_route_prefers_session_over_remote_capable_provider() {
        let provider = ModelProviderInfo {
            name: "Azure".into(),
            base_url: Some("https://example.com/openai".into()),
            env_key: Some("AZURE_OPENAI_API_KEY".into()),
            env_key_instructions: None,
            experimental_bearer_token: None,
            auth: None,
            aws: None,
            wire_api: WireApi::Responses,
            query_params: None,
            http_headers: None,
            env_http_headers: None,
            request_max_retries: None,
            stream_max_retries: None,
            stream_idle_timeout_ms: None,
            websocket_connect_timeout_ms: None,
            requires_openai_auth: false,
            supports_websockets: false,
        };

        assert_eq!(
            CompactRoute::Session,
            select_compact_route(CompactStrategy::Session, &provider),
        );
        assert_eq!(
            CompactRoute::Remote,
            select_compact_route(CompactStrategy::Default, &provider),
        );
    }

    #[test]
    fn normalize_session_compact_state_wraps_unstructured_output() {
        let normalized =
            normalize_session_compact_state("Need to finish the route gating and move tests.");

        assert!(normalized.starts_with(SESSION_COMPACT_OPEN_MARKER));
        assert!(
            normalized
                .contains("Current Handoff:\n- Need to finish the route gating and move tests.")
        );
        assert!(normalized.ends_with(SESSION_COMPACT_CLOSE_MARKER));
    }

    #[test]
    fn normalize_session_compact_state_accepts_legacy_markers() {
        let normalized = normalize_session_compact_state(
            "<session_compact_state_v1>\nObjective:\n- ship\n\nCurrent Handoff:\n- finish parity\n</session_compact_state_v1>",
        );

        assert!(normalized.starts_with(SESSION_COMPACT_OPEN_MARKER));
        assert!(normalized.ends_with(SESSION_COMPACT_CLOSE_MARKER));
        assert!(!normalized.contains(LEGACY_SESSION_COMPACT_OPEN_MARKER));
        assert!(!normalized.contains(LEGACY_SESSION_COMPACT_CLOSE_MARKER));
        assert!(normalized.contains("Current Handoff:\n- finish parity"));
    }

    #[test]
    fn build_session_compacted_history_keeps_small_tail_and_state_last() {
        let history = build_session_compacted_history(
            Vec::new(),
            &[
                ResponseItem::Message {
                    id: None,
                    role: "user".to_string(),
                    content: vec![ContentItem::InputText {
                        text: "first user message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "assistant".to_string(),
                    content: vec![ContentItem::OutputText {
                        text: "first assistant message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "user".to_string(),
                    content: vec![ContentItem::InputText {
                        text: "second user message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::FunctionCall {
                    id: None,
                    name: "shell".to_string(),
                    namespace: None,
                    arguments: "{\"cmd\":\"echo hi\"}".to_string(),
                    call_id: "call-1".to_string(),
                },
                ResponseItem::FunctionCallOutput {
                    call_id: "call-1".to_string(),
                    output: codex_protocol::models::FunctionCallOutputPayload::from_text(
                        "shell output".to_string(),
                    ),
                },
                ResponseItem::Message {
                    id: None,
                    role: "assistant".to_string(),
                    content: vec![ContentItem::OutputText {
                        text: "second assistant message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "user".to_string(),
                    content: vec![ContentItem::InputText {
                        text: "third user message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "assistant".to_string(),
                    content: vec![ContentItem::OutputText {
                        text: "third assistant message".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
            ],
            "<session_compact_state>\nObjective:\n- ship\n\nActive Memory:\n- retain | route gate\n\nInactive Changes:\n- none\n\nCurrent Handoff:\n- implement builder\n\nEvidence Pointers:\n- src/compact_session.rs\n</session_compact_state>",
            test_frontier_config(SESSION_COMPACT_TAIL_MIN_TOKENS),
        );

        assert_eq!(7, history.len());
        assert!(matches!(
            &history[0],
            ResponseItem::Message { role, content, .. }
                if role == "user"
                    && matches!(
                        content.first(),
                        Some(ContentItem::InputText { text }) if text == "second user message"
                    )
        ));
        assert!(matches!(&history[1], ResponseItem::FunctionCall { .. }));
        assert!(matches!(
            &history[2],
            ResponseItem::FunctionCallOutput { .. }
        ));
        assert!(matches!(
            &history[3],
            ResponseItem::Message { role, content, .. }
                if role == "assistant"
                    && matches!(
                        content.first(),
                        Some(ContentItem::OutputText { text }) if text == "second assistant message"
                    )
        ));
        assert!(matches!(
            &history[4],
            ResponseItem::Message { role, content, .. }
                if role == "user"
                    && matches!(
                        content.first(),
                        Some(ContentItem::InputText { text }) if text == "third user message"
                    )
        ));
        assert!(matches!(
            &history[5],
            ResponseItem::Message { role, content, .. }
                if role == "assistant"
                    && matches!(
                        content.first(),
                        Some(ContentItem::OutputText { text }) if text == "third assistant message"
                    )
        ));
        assert!(matches!(
            &history[6],
            ResponseItem::Message { content, .. }
                if matches!(
                    content.first(),
                    Some(ContentItem::InputText { text }) if text.contains(SESSION_COMPACT_OPEN_MARKER)
                )
        ));
    }

    #[test]
    fn select_recent_structured_frontier_drops_images_and_contextual_updates() {
        let frontier = select_recent_structured_frontier(
            &[
                ResponseItem::Message {
                    id: None,
                    role: "developer".to_string(),
                    content: vec![ContentItem::InputText {
                        text: "<permissions instructions>\nkeep".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "user".to_string(),
                    content: vec![
                        ContentItem::InputText {
                            text: "recent image context".to_string(),
                        },
                        ContentItem::InputImage {
                            image_url: "data:image/png;base64,AAA".to_string(),
                            detail: None,
                        },
                    ],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::Message {
                    id: None,
                    role: "assistant".to_string(),
                    content: vec![ContentItem::OutputText {
                        text: "assistant follow-up".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
            ],
            test_frontier_config(SESSION_COMPACT_TAIL_MIN_TOKENS),
        );

        assert_eq!(2, frontier.len());
        assert!(matches!(
            &frontier[0],
            ResponseItem::Message { role, content, .. }
                if role == "user"
                    && matches!(
                        content.first(),
                        Some(ContentItem::InputText { text }) if text == "recent image context"
                    )
                    && !content.iter().any(|item| matches!(item, ContentItem::InputImage { .. }))
        ));
        assert!(matches!(
            &frontier[1],
            ResponseItem::Message { role, .. } if role == "assistant"
        ));
    }

    #[test]
    fn select_recent_structured_frontier_microcompacts_tool_payloads_to_budget() {
        let oversized = "token ".repeat(4_000);
        let frontier = select_recent_structured_frontier(
            &[
                ResponseItem::Message {
                    id: None,
                    role: "user".to_string(),
                    content: vec![ContentItem::InputText {
                        text: "trigger frontier".to_string(),
                    }],
                    end_turn: None,
                    phase: None,
                },
                ResponseItem::FunctionCall {
                    id: None,
                    name: "shell".to_string(),
                    namespace: None,
                    arguments: oversized.clone(),
                    call_id: "call-1".to_string(),
                },
                ResponseItem::FunctionCallOutput {
                    call_id: "call-1".to_string(),
                    output: codex_protocol::models::FunctionCallOutputPayload::from_text(oversized),
                },
            ],
            test_frontier_config(SESSION_COMPACT_TAIL_MIN_TOKENS),
        );

        assert!(
            frontier_token_estimate(&frontier) <= SESSION_COMPACT_TAIL_MIN_TOKENS,
            "frontier should fit within the session compact tail budget"
        );
        assert!(matches!(
            frontier.get(0),
            Some(ResponseItem::Message { role, .. }) if role == "user"
        ));
    }

    #[test]
    fn session_frontier_token_budget_scales_with_full_context_and_caps_at_ten_percent() {
        assert_eq!(
            20_000,
            session_frontier_token_budget(Some(200_000), Some(258_400))
        );
        assert_eq!(
            25_840,
            session_frontier_token_budget(Some(500_000), Some(258_400))
        );
        assert_eq!(
            SESSION_COMPACT_TAIL_MIN_TOKENS,
            session_frontier_token_budget(Some(20_000), Some(258_400))
        );
        assert_eq!(
            3_000,
            session_frontier_token_budget(Some(20_000), Some(30_000))
        );
    }

    #[test]
    fn build_session_image_sidecar_keeps_bounded_recent_images() {
        let history = vec![
            ResponseItem::Message {
                id: None,
                role: "user".to_string(),
                content: vec![
                    ContentItem::InputText {
                        text: "older image".to_string(),
                    },
                    ContentItem::InputImage {
                        image_url: "data:image/png;base64,OLD".to_string(),
                        detail: None,
                    },
                ],
                end_turn: None,
                phase: None,
            },
            ResponseItem::Message {
                id: None,
                role: "user".to_string(),
                content: vec![
                    ContentItem::InputText {
                        text: "newer image".to_string(),
                    },
                    ContentItem::InputImage {
                        image_url: "data:image/png;base64,NEW".to_string(),
                        detail: None,
                    },
                ],
                end_turn: None,
                phase: None,
            },
        ];

        let sidecar = build_session_image_sidecar(&history).expect("image sidecar");
        assert_eq!(2, sidecar.recent_user_images.len());
        assert_eq!(
            "data:image/png;base64,OLD",
            sidecar.recent_user_images[0].image_url
        );
        assert_eq!(
            Some("newer image"),
            sidecar.recent_user_images[1].user_text.as_deref()
        );
    }
}
