use crate::compact::content_items_to_text;
use crate::compact::is_summary_message;
use crate::context_manager::is_user_turn_boundary;
use crate::event_mapping::is_contextual_dev_message_content;
use crate::event_mapping::is_contextual_user_message_content;
use codex_protocol::models::ContentItem;
use codex_protocol::models::FunctionCallOutputPayload;
use codex_protocol::models::ResponseItem;
use codex_utils_output_truncation::TruncationPolicy;
use codex_utils_output_truncation::approx_token_count;
use codex_utils_output_truncation::truncate_text;

#[derive(Clone, Copy, Debug)]
pub(crate) struct StructuredFrontierConfig {
    pub preserve_turns: usize,
    pub max_active_turns: usize,
    pub max_total_tokens: usize,
    pub tool_call_input_max_tokens: usize,
    pub tool_output_max_tokens: usize,
    pub tool_call_input_min_tokens: usize,
    pub tool_output_min_tokens: usize,
    pub message_max_tokens: usize,
    pub message_min_tokens: usize,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct StructuredFrontierPartition {
    pub historical_merge_region: Vec<ResponseItem>,
    pub recent_frontier_region: Vec<ResponseItem>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct TurnRange {
    start: usize,
    end: usize,
}

const ACTIVE_WORK_SURFACE_MESSAGE_HINTS: &[&str] = &[
    "todo",
    "next",
    "continue",
    "remaining",
    "follow up",
    "follow-up",
    "pending",
    "unfinished",
    "blocker",
    "blocked",
    "error",
    "failed",
    "failing",
    "debug",
    "fix",
    "retry",
    "patch",
    "test",
    "command",
    "shell",
    "apply_patch",
    ".rs",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".md",
    ".toml",
    ".json",
    "cargo ",
    "pytest",
    "uv ",
    "npm ",
    "pnpm ",
    "git ",
];

pub(crate) fn partition_history_for_staged_compact(
    items: &[ResponseItem],
    preserve_turns: usize,
    max_active_turns: usize,
) -> StructuredFrontierPartition {
    let Some(split_index) = preserved_frontier_split_index(items, preserve_turns, max_active_turns)
    else {
        return StructuredFrontierPartition {
            historical_merge_region: items.to_vec(),
            recent_frontier_region: Vec::new(),
        };
    };

    StructuredFrontierPartition {
        historical_merge_region: items[..split_index].to_vec(),
        recent_frontier_region: items[split_index..].to_vec(),
    }
}

pub(crate) fn select_recent_structured_frontier(
    items: &[ResponseItem],
    config: StructuredFrontierConfig,
) -> Vec<ResponseItem> {
    let partition =
        partition_history_for_staged_compact(items, config.preserve_turns, config.max_active_turns);
    trim_frontier_to_budget(
        sanitize_structured_frontier(&partition.recent_frontier_region),
        config,
    )
}

pub(crate) fn frontier_token_estimate(items: &[ResponseItem]) -> usize {
    items.iter().map(estimate_frontier_item_tokens).sum()
}

fn preserved_frontier_split_index(
    items: &[ResponseItem],
    preserve_turns: usize,
    max_active_turns: usize,
) -> Option<usize> {
    let turn_ranges = collect_preservable_turn_ranges(items);
    if turn_ranges.is_empty() {
        return None;
    }

    let mut start_turn_index = if preserve_turns == 0 || preserve_turns >= turn_ranges.len() {
        0
    } else {
        turn_ranges.len() - preserve_turns
    };

    let max_active_turns = max_active_turns.max(preserve_turns.max(1));
    while start_turn_index > 0 && turn_ranges.len() - start_turn_index < max_active_turns {
        let previous_turn = turn_ranges[start_turn_index - 1];
        if !is_active_work_surface_turn(&items[previous_turn.start..previous_turn.end]) {
            break;
        }
        start_turn_index -= 1;
    }

    Some(turn_ranges[start_turn_index].start)
}

fn collect_preservable_turn_ranges(items: &[ResponseItem]) -> Vec<TurnRange> {
    let boundary_positions: Vec<usize> = items
        .iter()
        .enumerate()
        .filter_map(|(idx, item)| is_preservable_turn_boundary(item).then_some(idx))
        .collect();
    if boundary_positions.is_empty() {
        return Vec::new();
    }

    let mut starts = Vec::with_capacity(boundary_positions.len());
    for boundary in boundary_positions {
        let mut start = boundary;
        while start > 0 && is_pre_turn_context_update(&items[start - 1]) {
            start -= 1;
        }
        starts.push(start);
    }
    starts.dedup();

    starts
        .iter()
        .enumerate()
        .filter_map(|(idx, start)| {
            let end = starts.get(idx + 1).copied().unwrap_or(items.len());
            (*start < end).then_some(TurnRange { start: *start, end })
        })
        .collect()
}

fn is_active_work_surface_turn(items: &[ResponseItem]) -> bool {
    items.iter().any(is_active_work_surface_item)
}

fn is_active_work_surface_item(item: &ResponseItem) -> bool {
    match item {
        ResponseItem::Reasoning { .. }
        | ResponseItem::FunctionCall { .. }
        | ResponseItem::FunctionCallOutput { .. }
        | ResponseItem::CustomToolCall { .. }
        | ResponseItem::CustomToolCallOutput { .. }
        | ResponseItem::WebSearchCall { .. }
        | ResponseItem::ImageGenerationCall { .. } => true,
        ResponseItem::Message { role, content, .. } => {
            if role == "developer" && is_contextual_dev_message_content(content) {
                return false;
            }
            if role == "user" && is_contextual_user_message_content(content) {
                return false;
            }
            let Some(text) = content_items_to_text(content) else {
                return false;
            };
            let normalized = text.to_ascii_lowercase();
            ACTIVE_WORK_SURFACE_MESSAGE_HINTS
                .iter()
                .any(|hint| normalized.contains(hint))
        }
        _ => false,
    }
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

fn sanitize_structured_frontier(items: &[ResponseItem]) -> Vec<ResponseItem> {
    items
        .iter()
        .filter_map(sanitize_structured_frontier_item)
        .collect()
}

fn sanitize_structured_frontier_item(item: &ResponseItem) -> Option<ResponseItem> {
    match item {
        ResponseItem::GhostSnapshot { .. } | ResponseItem::Compaction { .. } => None,
        ResponseItem::Message {
            id,
            role,
            content,
            end_turn,
            phase,
        } => {
            if role == "developer" && is_contextual_dev_message_content(content) {
                return None;
            }
            if role == "user" && is_contextual_user_message_content(content) {
                return None;
            }
            let text = content_items_to_text(content)?;
            let content = vec![if role == "assistant" {
                ContentItem::OutputText { text }
            } else {
                ContentItem::InputText { text }
            }];
            Some(ResponseItem::Message {
                id: id.clone(),
                role: role.clone(),
                content,
                end_turn: *end_turn,
                phase: phase.clone(),
            })
        }
        _ => Some(item.clone()),
    }
}

fn trim_frontier_to_budget(
    mut items: Vec<ResponseItem>,
    config: StructuredFrontierConfig,
) -> Vec<ResponseItem> {
    if items.is_empty() {
        return items;
    }

    microcompact_structured_frontier(
        &mut items,
        config.max_total_tokens,
        config.tool_call_input_max_tokens,
        config.tool_output_max_tokens,
        config.message_max_tokens,
    );
    microcompact_structured_frontier(
        &mut items,
        config.max_total_tokens,
        config.tool_call_input_min_tokens,
        config.tool_output_min_tokens,
        config.message_min_tokens,
    );

    while frontier_token_estimate(&items) > config.max_total_tokens && items.len() > 1 {
        items.remove(0);
    }

    if frontier_token_estimate(&items) > config.max_total_tokens
        && let Some(item) = items.first_mut()
    {
        force_fit_frontier_item(item, config.max_total_tokens);
    }

    items
}

fn microcompact_structured_frontier(
    items: &mut [ResponseItem],
    max_total_tokens: usize,
    tool_call_input_max_tokens: usize,
    tool_output_max_tokens: usize,
    message_max_tokens: usize,
) {
    for idx in 0..items.len() {
        if frontier_token_estimate(items) <= max_total_tokens {
            break;
        }
        microcompact_frontier_item(
            &mut items[idx],
            tool_call_input_max_tokens,
            tool_output_max_tokens,
            message_max_tokens,
        );
    }
}

fn microcompact_frontier_item(
    item: &mut ResponseItem,
    tool_call_input_max_tokens: usize,
    tool_output_max_tokens: usize,
    message_max_tokens: usize,
) {
    match item {
        ResponseItem::Message { role, content, .. } => {
            truncate_message_content_in_place(role, content, message_max_tokens);
        }
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

fn truncate_message_content_in_place(
    role: &str,
    content: &mut Vec<ContentItem>,
    max_tokens: usize,
) {
    let Some(text) = content_items_to_text(content) else {
        return;
    };
    if approx_token_count(&text) <= max_tokens {
        return;
    }

    let truncated = truncate_text(&text, TruncationPolicy::Tokens(max_tokens));
    *content = vec![if role == "assistant" {
        ContentItem::OutputText { text: truncated }
    } else {
        ContentItem::InputText { text: truncated }
    }];
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

    let Some(text) = output.body.to_text() else {
        return;
    };
    if approx_token_count(&text) <= max_tokens {
        return;
    }

    let success = output.success;
    *output = FunctionCallOutputPayload::from_text(truncate_text(
        &text,
        TruncationPolicy::Tokens(max_tokens),
    ));
    output.success = success;
}

fn force_fit_frontier_item(item: &mut ResponseItem, max_tokens: usize) {
    match item {
        ResponseItem::Message { role, content, .. } => {
            truncate_message_content_in_place(role, content, max_tokens);
        }
        ResponseItem::FunctionCall { arguments, .. } => {
            truncate_string_in_place(arguments, max_tokens)
        }
        ResponseItem::CustomToolCall { input, .. } => truncate_string_in_place(input, max_tokens),
        ResponseItem::FunctionCallOutput { output, .. }
        | ResponseItem::CustomToolCallOutput { output, .. } => {
            truncate_function_call_output_payload(output, max_tokens);
        }
        _ => {}
    }
}

fn estimate_frontier_item_tokens(item: &ResponseItem) -> usize {
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
        _ => serde_json::to_string(item)
            .map(|serialized| approx_token_count(&serialized))
            .unwrap_or(0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_protocol::models::ContentItem;

    fn user_message(id: &str, text: &str) -> ResponseItem {
        ResponseItem::Message {
            id: Some(id.to_string()),
            role: "user".to_string(),
            content: vec![ContentItem::InputText {
                text: text.to_string(),
            }],
            end_turn: None,
            phase: None,
        }
    }

    fn assistant_message(id: &str, text: &str) -> ResponseItem {
        ResponseItem::Message {
            id: Some(id.to_string()),
            role: "assistant".to_string(),
            content: vec![ContentItem::OutputText {
                text: text.to_string(),
            }],
            end_turn: None,
            phase: None,
        }
    }

    fn contextual_developer_message(id: &str, text: &str) -> ResponseItem {
        ResponseItem::Message {
            id: Some(id.to_string()),
            role: "developer".to_string(),
            content: vec![ContentItem::InputText {
                text: format!("<permissions instructions>{text}</permissions instructions>"),
            }],
            end_turn: None,
            phase: None,
        }
    }

    #[test]
    fn staged_partition_attaches_pre_turn_context_to_frontier_region() {
        let items = vec![
            user_message("u1", "turn 1"),
            assistant_message("a1", "done 1"),
            contextual_developer_message("ctx2", "context for turn 2"),
            user_message("u2", "turn 2"),
            assistant_message("a2", "done 2"),
        ];

        let partition = partition_history_for_staged_compact(&items, 1, 1);

        assert_eq!(
            partition.historical_merge_region,
            vec![
                user_message("u1", "turn 1"),
                assistant_message("a1", "done 1")
            ]
        );
        assert_eq!(
            partition.recent_frontier_region,
            vec![
                contextual_developer_message("ctx2", "context for turn 2"),
                user_message("u2", "turn 2"),
                assistant_message("a2", "done 2"),
            ]
        );
    }

    #[test]
    fn staged_partition_keeps_requested_number_of_turns_in_recent_region() {
        let items = vec![
            user_message("u1", "turn 1"),
            assistant_message("a1", "done 1"),
            user_message("u2", "turn 2"),
            assistant_message("a2", "done 2"),
            user_message("u3", "turn 3"),
            assistant_message("a3", "done 3"),
        ];

        let partition = partition_history_for_staged_compact(&items, 2, 2);

        assert_eq!(
            partition.historical_merge_region,
            vec![
                user_message("u1", "turn 1"),
                assistant_message("a1", "done 1")
            ]
        );
        assert_eq!(
            partition.recent_frontier_region,
            vec![
                user_message("u2", "turn 2"),
                assistant_message("a2", "done 2"),
                user_message("u3", "turn 3"),
                assistant_message("a3", "done 3"),
            ]
        );
    }

    #[test]
    fn staged_partition_falls_back_to_historical_region_without_user_turns() {
        let items = vec![
            assistant_message("a1", "assistant only"),
            contextual_developer_message("ctx", "context only"),
        ];

        let partition = partition_history_for_staged_compact(&items, 2, 2);

        assert_eq!(partition.historical_merge_region, items);
        assert!(partition.recent_frontier_region.is_empty());
    }

    #[test]
    fn staged_partition_expands_to_include_recent_active_work_surface_turns() {
        let items = vec![
            user_message("u1", "turn 1"),
            assistant_message("a1", "done 1"),
            user_message("u2", "run the frontier test next"),
            ResponseItem::FunctionCall {
                id: Some("f2".to_string()),
                name: "shell".to_string(),
                namespace: None,
                arguments: "{\"cmd\":\"cargo test frontier\"}".to_string(),
                call_id: "call-2".to_string(),
            },
            ResponseItem::FunctionCallOutput {
                call_id: "call-2".to_string(),
                output: FunctionCallOutputPayload::from_text("frontier test failed".to_string()),
            },
            assistant_message("a2", "turn 2 active"),
            user_message("u3", "fix the failing compact frontier"),
            assistant_message("a3", "turn 3 active"),
            user_message("u4", "small follow up"),
            assistant_message("a4", "turn 4"),
        ];

        let partition = partition_history_for_staged_compact(&items, 2, 5);

        assert_eq!(
            partition.historical_merge_region,
            vec![
                user_message("u1", "turn 1"),
                assistant_message("a1", "done 1")
            ]
        );
        assert_eq!(partition.recent_frontier_region, items[2..].to_vec());
    }

    #[test]
    fn select_recent_structured_frontier_respects_active_turn_expansion() {
        let config = StructuredFrontierConfig {
            preserve_turns: 2,
            max_active_turns: 5,
            max_total_tokens: 8_000,
            tool_call_input_max_tokens: 256,
            tool_output_max_tokens: 128,
            tool_call_input_min_tokens: 48,
            tool_output_min_tokens: 24,
            message_max_tokens: 256,
            message_min_tokens: 64,
        };
        let items = vec![
            user_message("u1", "turn 1"),
            assistant_message("a1", "done 1"),
            user_message("u2", "run the frontier test next"),
            assistant_message("a2", "turn 2 active"),
            user_message("u3", "fix the failing compact frontier"),
            assistant_message("a3", "turn 3 active"),
            user_message("u4", "small follow up"),
            assistant_message("a4", "turn 4"),
        ];

        let frontier = select_recent_structured_frontier(&items, config);
        let frontier_text = frontier
            .iter()
            .filter_map(|item| match item {
                ResponseItem::Message { content, .. } => content_items_to_text(content),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("\n");

        assert!(frontier_text.contains("run the frontier test next"));
        assert!(frontier_text.contains("fix the failing compact frontier"));
        assert!(frontier_text.contains("small follow up"));
        assert!(!frontier_text.contains("turn 1"));
    }
}
