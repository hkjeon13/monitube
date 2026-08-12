//! Framework-independent Monitube domain values shared by Rust binaries.

use serde::{Deserialize, Serialize};
use std::fmt::{Display, Formatter};
use std::str::FromStr;
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceType {
    Channel,
    Keyword,
    Video,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuotaBucket {
    Core,
    SearchQueries,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Queued,
    Running,
    WaitingQuota,
    WaitingRetry,
    Completed,
    CompletedWithWarnings,
    Failed,
    Cancelled,
}

impl JobState {
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Completed | Self::CompletedWithWarnings | Self::Failed | Self::Cancelled
        )
    }

    #[must_use]
    pub const fn can_transition_to(self, next: Self) -> bool {
        use JobState::{
            Cancelled, Completed, CompletedWithWarnings, Failed, Queued, Running, WaitingQuota,
            WaitingRetry,
        };

        matches!(
            (self, next),
            (Queued, Running | WaitingQuota | WaitingRetry | Cancelled)
                | (
                    Running,
                    WaitingQuota
                        | WaitingRetry
                        | Completed
                        | CompletedWithWarnings
                        | Failed
                        | Cancelled
                )
                | (
                    WaitingQuota | WaitingRetry,
                    Queued | Running | Failed | Cancelled
                )
                | (Failed, Queued | Cancelled)
                | (Cancelled, Queued)
        )
    }
}

impl Display for JobState {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::WaitingQuota => "waiting_quota",
            Self::WaitingRetry => "waiting_retry",
            Self::Completed => "completed",
            Self::CompletedWithWarnings => "completed_with_warnings",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        })
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
#[error("unknown job state: {0}")]
pub struct ParseJobStateError(String);

impl FromStr for JobState {
    type Err = ParseJobStateError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "queued" => Ok(Self::Queued),
            "running" => Ok(Self::Running),
            "waiting_quota" => Ok(Self::WaitingQuota),
            "waiting_retry" => Ok(Self::WaitingRetry),
            "completed" => Ok(Self::Completed),
            "completed_with_warnings" => Ok(Self::CompletedWithWarnings),
            "failed" => Ok(Self::Failed),
            "cancelled" => Ok(Self::Cancelled),
            other => Err(ParseJobStateError(other.to_owned())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::JobState;

    #[test]
    fn terminal_states_do_not_transition() {
        for terminal in [
            JobState::Completed,
            JobState::CompletedWithWarnings,
            JobState::Failed,
            JobState::Cancelled,
        ] {
            assert!(terminal.is_terminal());
            assert!(!terminal.can_transition_to(JobState::Running));
        }
    }

    #[test]
    fn waiting_jobs_can_resume_without_losing_state_semantics() {
        assert!(JobState::WaitingQuota.can_transition_to(JobState::Queued));
        assert!(JobState::WaitingRetry.can_transition_to(JobState::Running));
        assert!(JobState::WaitingRetry.can_transition_to(JobState::Failed));
        assert!(JobState::Failed.can_transition_to(JobState::Queued));
        assert!(JobState::Cancelled.can_transition_to(JobState::Queued));
    }
}
