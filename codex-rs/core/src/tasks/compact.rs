use std::sync::Arc;

use super::SessionTask;
use super::SessionTaskContext;
use crate::session::turn_context::TurnContext;
use crate::state::TaskKind;
use codex_protocol::user_input::UserInput;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Copy, Default)]
pub(crate) struct CompactTask;

impl SessionTask for CompactTask {
    fn kind(&self) -> TaskKind {
        TaskKind::Compact
    }

    fn span_name(&self) -> &'static str {
        "session_task.compact"
    }

    async fn run(
        self: Arc<Self>,
        session: Arc<SessionTaskContext>,
        ctx: Arc<TurnContext>,
        input: Vec<UserInput>,
        _cancellation_token: CancellationToken,
    ) -> Option<String> {
        let session = session.clone_session();
        let _ = match crate::compact_session::select_compact_route(
            ctx.compact_strategy,
            ctx.provider.info(),
        ) {
            crate::compact_session::CompactRoute::Session => {
                session.services.session_telemetry.counter(
                    "codex.task.compact",
                    /*inc*/ 1,
                    &[("type", "session")],
                );
                crate::compact_session::run_session_compact_task(session.clone(), ctx, input).await
            }
            crate::compact_session::CompactRoute::Remote => {
                session.services.session_telemetry.counter(
                    "codex.task.compact",
                    /*inc*/ 1,
                    &[("type", "remote")],
                );
                crate::compact_remote::run_remote_compact_task(session.clone(), ctx).await
            }
            crate::compact_session::CompactRoute::Local => {
                session.services.session_telemetry.counter(
                    "codex.task.compact",
                    /*inc*/ 1,
                    &[("type", "local")],
                );
                crate::compact::run_compact_task(session.clone(), ctx, input).await
            }
        };
        None
    }
}
