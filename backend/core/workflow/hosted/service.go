package hosted

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"

	"gorm.io/gorm"

	"lazymind/core/common/orm"
	"lazymind/core/workflow/attempt"
	"lazymind/core/workflow/controlstore"
	"lazymind/core/workflow/execution"
	"lazymind/core/workflow/executor"
	workflowstore "lazymind/core/workflow/store"
)

const HostName = "external-agent"

type Service struct {
	Completion *execution.Service
	DB         *gorm.DB
	Store      *workflowstore.Repository
	Attempts   *attempt.Service
	Contexts   executor.ContextLoader
	Artifacts  executor.ArtifactSink
}

type Execution struct {
	ExecutorHost        string                  `json:"executor_host,omitempty"`
	AttemptStatus       string                  `json:"attempt_status,omitempty"`
	ReviewAfterComplete bool                    `json:"review_after_complete"`
	ExecutionHandle     string                  `json:"execution_handle,omitempty"`
	ExecutionID         string                  `json:"execution_id"`
	LeaseExpires        time.Time               `json:"lease_expires_at"`
	StepContract        executor.AttemptContext `json:"step_contract"`
}

type ProtocolError struct {
	Code    string
	Message string
	Cause   error
}

func (e *ProtocolError) Error() string { return e.Message }
func (e *ProtocolError) Unwrap() error { return e.Cause }

func executorID(owner string) string {
	sum := sha256.Sum256([]byte(owner))
	return "hosted:" + hex.EncodeToString(sum[:16])
}

func (s *Service) Begin(ctx context.Context, owner, sessionID, attemptID string) (Execution, error) {
	return s.begin(ctx, owner, sessionID, attemptID, false)
}

func (s *Service) begin(ctx context.Context, owner, sessionID, attemptID string, resume bool) (Execution, error) {
	if strings.TrimSpace(owner) == "" || strings.TrimSpace(sessionID) == "" || strings.TrimSpace(attemptID) == "" {
		return Execution{}, &ProtocolError{Code: "INVALID_EXECUTION", Message: "owner, session_id and execution_id are required"}
	}
	if err := s.Store.AuthorizeSession(ctx, sessionID, owner); err != nil {
		return Execution{}, err
	}
	var session orm.WorkflowSession
	if err := s.DB.WithContext(ctx).Where("id = ?", sessionID).First(&session).Error; err != nil {
		return Execution{}, err
	}
	if !controlstore.Controlled(session) {
		return Execution{}, controlstore.Reject("CONTROL_PROTOCOL_REQUIRED", "This session does not use the current external Workflow protocol; start a new run.")
	}
	return s.beginControlled(ctx, owner, sessionID, attemptID, resume)
}

func (s *Service) Resume(ctx context.Context, owner, sessionID, attemptID string) (Execution, error) {
	return s.begin(ctx, owner, sessionID, attemptID, true)
}

func (s *Service) authorizeExecution(ctx context.Context, owner, sessionID, attemptID string) error {
	if err := s.Store.AuthorizeSession(ctx, sessionID, owner); err != nil {
		return err
	}
	var session orm.WorkflowSession
	if err := s.DB.WithContext(ctx).Where("id = ?", sessionID).First(&session).Error; err != nil {
		return err
	}
	if !controlstore.Controlled(session) {
		return controlstore.Reject("CONTROL_PROTOCOL_REQUIRED", "Start a run using the current external Workflow protocol")
	}
	row, err := s.Attempts.Attempt(ctx, attemptID)
	if err != nil || row.SessionID != sessionID {
		return controlstore.Reject("EXECUTION_NOT_FOUND", "execution was not found")
	}
	if row.ExecutorHost != HostName {
		return controlstore.Reject("EXECUTOR_MISMATCH", "this execution is not owned by the external executor")
	}
	return nil
}

func (s *Service) Complete(ctx context.Context, owner, sessionID, attemptID string, input executor.Completion) (execution.CompletionResult, error) {
	if err := s.authorizeExecution(ctx, owner, sessionID, attemptID); err != nil {
		return execution.CompletionResult{}, err
	}
	return s.Completion.Complete(ctx, owner, sessionID, attemptID, input)
}

type Publication struct {
	ExecutionHandle string            `json:"execution_handle"`
	Artifact        executor.Artifact `json:"artifact"`
}

func (s *Service) Publish(ctx context.Context, owner, sessionID, attemptID string, input Publication) error {
	if input.ExecutionHandle == "" {
		return controlstore.Reject("EXECUTION_FENCED", "execution_handle is required")
	}
	if err := s.authorizeExecution(ctx, owner, sessionID, attemptID); err != nil {
		return err
	}
	contract, err := s.Contexts.LoadAttemptContext(ctx, attemptID)
	if err != nil {
		return err
	}
	contract.ExecutionHandle = input.ExecutionHandle
	input.Artifact, err = executor.NormalizeArtifact(contract, input.Artifact)
	if err != nil {
		return err
	}
	return s.Artifacts.Save(ctx, contract, input.Artifact)
}

// Renew reuses the worker lease service under the same session fence as writes.
// It never reclaims expired authority or changes Workflow progress.
func (s *Service) Renew(ctx context.Context, owner, sessionID, attemptID, handle string) (time.Time, error) {
	if err := s.authorizeExecution(ctx, owner, sessionID, attemptID); err != nil {
		return time.Time{}, err
	}
	var expires time.Time
	err := controlstore.Transaction(ctx, s.DB, sessionID, func(tx *gorm.DB, session *orm.WorkflowSession) error {
		if !controlstore.Controlled(*session) {
			return controlstore.Reject("CONTROL_PROTOCOL_REQUIRED", "The current external Workflow protocol is required")
		}
		if err := controlstore.ValidateExecution(tx, *session, attemptID, handle); err != nil {
			return err
		}
		var err error
		expires, err = s.Attempts.WithDB(tx).Heartbeat(ctx, attemptID, handle)
		return err
	})
	return expires, err
}
