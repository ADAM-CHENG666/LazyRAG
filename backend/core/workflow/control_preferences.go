package workflow

import (
	"gorm.io/gorm"
	"lazymind/core/common/orm"
	"lazymind/core/workflow/controlstore"
	"lazymind/core/workflow/graphengine"
)

// Only the opt-in external controller reads/writes this namespace.
func approvalPreferenceDB(db *gorm.DB, session orm.WorkflowSession) *gorm.DB {
	if controlstore.Controlled(session) {
		return db.Table((orm.ExternalWorkflowApprovalPreference{}).TableName())
	}
	return db
}

func projectSessionWithApprovalPreferences(db *gorm.DB, session orm.WorkflowSession, graph *graphengine.CompiledStateGraph, snapshot graphengine.RuntimeSnapshot) graphengine.Projection {
	return projectWithApprovalPreferences(approvalPreferenceDB(db, session), session.CreateUserID, session.WorkflowID, graph, snapshot)
}
