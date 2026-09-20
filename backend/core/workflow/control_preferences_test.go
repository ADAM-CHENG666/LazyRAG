package workflow

import (
	"lazymind/core/common/orm"
	"lazymind/core/workflow/controlpolicy"
	"lazymind/core/workflow/graphengine"
	"testing"
)

func TestApprovalPreferencesAreIsolatedInBothDirections(t *testing.T) {
	for _, scope := range []string{"step", "following"} {
		t.Run(scope, func(t *testing.T) {
			db := newTestDB(t)
			if err := db.AutoMigrate(&orm.WorkflowApprovalPreference{}, &orm.ExternalWorkflowApprovalPreference{}); err != nil {
				t.Fatal(err)
			}
			native := orm.WorkflowSession{CreateUserID: "owner", WorkflowID: "workflow", ControllerHost: "lazymind"}
			external := native
			external.ControllerHost, external.ControlProtocol = "external-agent", controlpolicy.Protocol
			check := func(session orm.WorkflowSession, required bool) {
				t.Helper()
				projection := graphengine.Projection{Nodes: map[string]graphengine.NodeProjection{"review": {ID: "review", RequiresApproval: true}}}
				got := applyApprovalPreferences(approvalPreferenceDB(db.DB, session), session.CreateUserID, session.WorkflowID, projection)
				if got.Nodes["review"].RequiresApproval != required {
					t.Fatalf("controller=%s required=%v projection=%+v", session.ControllerHost, required, got)
				}
			}
			if _, err := saveWorkflowApprovalPreference(approvalPreferenceDB(db.DB, external), "owner", "workflow", "review", scope); err != nil {
				t.Fatal(err)
			}
			check(external, false)
			check(native, true)
			// A future external session shares only the external opt-out.
			external.ID = "future-external"
			check(external, false)
			if err := db.Where("user_id = ?", "owner").Delete(&orm.ExternalWorkflowApprovalPreference{}).Error; err != nil {
				t.Fatal(err)
			}
			if _, err := saveWorkflowApprovalPreference(db.DB, "owner", "workflow", "review", scope); err != nil {
				t.Fatal(err)
			}
			check(native, false)
			check(external, true)
		})
	}
}
