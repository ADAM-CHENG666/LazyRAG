package executor

import (
	"context"
	"gorm.io/gorm"
	"lazymind/core/common/orm"
	"lazymind/core/workflow/controlstore"
	workflowstore "lazymind/core/workflow/store"
)

// AuthorizeExternalExecution is shared by tool discovery and calls. Call again
// after a long tool operation before publishing its result; admission does not
// give an expired or stopped worker continuing authority.
func AuthorizeExternalExecution(ctx context.Context, db *gorm.DB, owner, sessionID, attemptID, handle string) (AttemptContext, error) {
	if owner == "" {
		return AttemptContext{}, workflowstore.ErrPermissionDenied
	}
	if err := workflowstore.New(db).AuthorizeSession(ctx, sessionID, owner); err != nil {
		return AttemptContext{}, err
	}
	var value AttemptContext
	err := controlstore.Transaction(ctx, db, sessionID, func(tx *gorm.DB, session *orm.WorkflowSession) error {
		if !controlstore.Controlled(*session) {
			return controlstore.Reject("EXTERNAL_EXECUTION_REQUIRED", "tools require a controlled external execution")
		}
		if err := controlstore.ValidateExecution(tx, *session, attemptID, handle); err != nil {
			return err
		}
		var row orm.WorkflowSessionStep
		if err := tx.Where("id = ? AND session_id = ?", attemptID, sessionID).First(&row).Error; err != nil {
			return err
		}
		if row.ExecutorHost != "external-agent" {
			return controlstore.Reject("EXECUTOR_MISMATCH", "external execution does not own this step")
		}
		var err error
		value, err = (DBContextLoader{DB: tx}).LoadAttemptContext(ctx, attemptID)
		if err != nil {
			return err
		}
		value.Metadata = nil
		return nil
	})
	return value, err
}
