package hosted

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"lazymind/core/common/orm"
	corestore "lazymind/core/store"
	workflowcore "lazymind/core/workflow"
	"lazymind/core/workflow/controlpolicy"
	"lazymind/core/workflow/controlstore"
	"lazymind/core/workflow/execution"
	"lazymind/core/workflow/executor"
	"lazymind/core/workflow/graphengine"
)

// This is the deterministic counterpart of the manual DSH smoke workflow. It
// exercises the real transition, claim, artifact, review and completion paths;
// only the model output is replaced with fixed artifacts.
func TestScriptlessWorkflowCompletesExternalNativeExternalWithReviews(t *testing.T) {
	service, db := hostedTestService(t)
	service.Completion = &execution.Service{DB: db, Store: service.Store, Attempts: service.Attempts, Contexts: service.Contexts}
	if err := db.AutoMigrate(
		&orm.WorkflowReviewCheckpoint{}, &orm.WorkflowHostAction{}, &orm.WorkflowApprovalPreference{},
		&orm.ExternalWorkflowApprovalPreference{}, &orm.WorkflowTransitionCommand{}, &orm.SubAgentTask{},
		&orm.WorkflowResource{}, &orm.WorkflowStepIntent{}, &orm.TaskCenterTask{}, &orm.ChatHistory{},
	); err != nil {
		t.Fatal(err)
	}

	root := filepath.Join("..", "..", "..", "..", "workflows", "scriptless-test-workflow")
	workflowYAML, err := os.ReadFile(filepath.Join(root, "workflow.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	stateYAML, err := os.ReadFile(filepath.Join(root, "scenario", "state.yml"))
	if err != nil {
		t.Fatal(err)
	}
	compiled := graphengine.Compile(string(workflowYAML), string(stateYAML), "", graphengine.ProfileRuntimeLoad)
	if !compiled.Valid {
		t.Fatalf("scriptless workflow failed to compile: %#v", compiled.Diagnostics)
	}
	if err := db.Model(&orm.WorkflowRevision{}).Where("id = ?", "revision-1").Updates(map[string]any{
		"compiled_graph": compiled.Graph.JSON(), "graph_hash": compiled.Graph.GraphHash,
		"graph_schema_version": compiled.Graph.SchemaVersion,
	}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Where("session_id = ?", "session-1").Delete(&orm.WorkflowOutbox{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Where("session_id = ?", "session-1").Delete(&orm.WorkflowSessionStep{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(&orm.WorkflowSession{}).Where("id = ?", "session-1").Updates(map[string]any{
		"plugin_id": "scriptless-test-workflow", "graph_hash": compiled.Graph.GraphHash,
		"graph_schema_version": compiled.Graph.SchemaVersion, "control_protocol": controlpolicy.Protocol,
		"origin_host": HostName, "controller_host": HostName, "status": "active", "state_version": 1,
	}).Error; err != nil {
		t.Fatal(err)
	}

	oldDB, oldState := corestore.DB(), corestore.State()
	corestore.Init(db, db, nil)
	t.Cleanup(func() { corestore.Init(oldDB, oldDB, oldState) })
	ctx := context.Background()
	controller := workflowcore.WorkflowControlService{DB: db}
	identity := workflowcore.WorkflowHostIdentity{
		ConnectorID: "connector", Credential: strings.Repeat("x", 64), InstanceID: "driver-process",
	}
	if _, err := controller.Bind(ctx, "owner", "session-1", workflowcore.WorkflowHostBindingRequest{
		ConnectorID: identity.ConnectorID, Credential: identity.Credential, Provider: "deepseek-harness", DriverSessionID: "driver",
	}); err != nil {
		t.Fatal(err)
	}

	begin := func(step, command string) orm.WorkflowSessionStep {
		t.Helper()
		var session orm.WorkflowSession
		if err := db.First(&session, "id = ?", "session-1").Error; err != nil {
			t.Fatal(err)
		}
		result, err := controller.Execute(ctx, "owner", session.ID, workflowcore.WorkflowControlCommand{
			CommandID: command, Kind: "begin", StepID: step, StateVersion: session.StateVersion,
		})
		if err != nil {
			t.Fatal(err)
		}
		var attempt orm.WorkflowSessionStep
		if err := db.First(&attempt, "id = ?", result.Receipt.ExecutionID).Error; err != nil {
			t.Fatal(err)
		}
		return attempt
	}
	var nativeEventSeq int64
	confirmAndContinue := func(snapshot *controlstore.Snapshot, command string) {
		t.Helper()
		if snapshot == nil || snapshot.Continuation != "awaiting_user" {
			t.Fatalf("expected one pending review: %+v", snapshot)
		}
		var pending []orm.WorkflowReviewCheckpoint
		for _, review := range snapshot.Reviews {
			if review.Status == "pending" {
				pending = append(pending, review)
			}
		}
		if len(pending) != 1 {
			t.Fatalf("expected one pending review: %+v", snapshot.Reviews)
		}
		review := pending[0]
		result, err := controller.Execute(ctx, "owner", "session-1", workflowcore.WorkflowControlCommand{
			CommandID: command, Kind: "confirm_and_continue", ReviewID: review.ID,
			ReviewVersion: review.Version, ManifestHash: review.ManifestHash,
		})
		if err != nil {
			t.Fatal(err)
		}
		if result.Receipt.ActionID == "" {
			return
		}
		claim, err := controller.ClaimHostAction(ctx, "owner", result.Receipt.ActionID, identity)
		if err != nil {
			t.Fatal(err)
		}
		nativeEventSeq++
		if _, err := controller.SettleHostAction(ctx, "owner", claim.Action.ID, workflowcore.WorkflowHostReceipt{
			ConnectorID: identity.ConnectorID, Credential: identity.Credential, InstanceID: identity.InstanceID,
			DispatchToken: claim.DispatchToken, Status: "accepted", NativeEventSeq: nativeEventSeq,
		}); err != nil {
			t.Fatal(err)
		}
	}
	external := func(step, command, slot, contentType string, value json.RawMessage) *controlstore.Snapshot {
		t.Helper()
		attempt := begin(step, command)
		if attempt.ExecutorHost != HostName {
			t.Fatalf("%s executor=%q, want %q", step, attempt.ExecutorHost, HostName)
		}
		claimed, err := service.Begin(ctx, "owner", attempt.SessionID, attempt.ID)
		if err != nil {
			t.Fatal(err)
		}
		result, err := publishAndComplete(service, ctx, "owner", attempt.SessionID, attempt.ID, testCompletion{
			ExecutionHandle: claimed.ExecutionHandle, Outcome: "succeeded", Summary: step,
			Artifacts: []executor.Artifact{{Slot: slot, ContentType: contentType, Seq: 1, Value: value}},
		})
		if err != nil {
			t.Fatal(err)
		}
		return result.Control
	}

	first := external("external_draft", "begin-external-draft", "external_draft", "text", json.RawMessage(`{"text":"request"}`))
	confirmAndContinue(first, "confirm-external-draft")

	nativeAttempt := begin("native_check", "begin-native-check")
	if nativeAttempt.ExecutorHost != "lazymind" {
		t.Fatalf("native_check executor=%q, want lazymind", nativeAttempt.ExecutorHost)
	}
	nativeClaim, err := service.Attempts.ClaimForHost(ctx, "native-test-executor", "lazymind")
	if err != nil {
		t.Fatal(err)
	}
	contract, err := service.Contexts.LoadAttemptContext(ctx, nativeClaim.AttemptID)
	if err != nil {
		t.Fatal(err)
	}
	contract.ExecutionHandle = nativeClaim.LeaseToken
	if err := service.Artifacts.Save(ctx, contract, executor.Artifact{Slot: "native_check", ContentType: "json", Seq: 1,
		Value: json.RawMessage(`{"scriptless":true,"executor":"lazymind","calculation":42,"status":"ok"}`)}); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(executor.Result{Summary: "native_check"})
	if err := finishNative(service, ctx, nativeClaim.AttemptID, nativeClaim.LeaseToken, "succeeded", "", raw); err != nil {
		t.Fatal(err)
	}
	var session orm.WorkflowSession
	if err := db.First(&session, "id = ?", "session-1").Error; err != nil {
		t.Fatal(err)
	}
	nativeControl, err := controlstore.Read(db, session)
	if err != nil {
		t.Fatal(err)
	}
	confirmAndContinue(nativeControl, "confirm-native-check")

	last := external("external_finalize", "begin-external-finalize", "final_result", "text",
		json.RawMessage(`{"text":"Full workflow control test passed"}`))
	confirmAndContinue(last, "confirm-external-finalize")
	if err := db.First(&session, "id = ?", "session-1").Error; err != nil {
		t.Fatal(err)
	}
	finalControl, err := controlstore.Read(db, session)
	if err != nil {
		t.Fatal(err)
	}
	if session.Status != "completed" || finalControl.Continuation != "completed" {
		t.Fatalf("workflow did not complete: session=%s control=%+v", session.Status, finalControl)
	}
}
