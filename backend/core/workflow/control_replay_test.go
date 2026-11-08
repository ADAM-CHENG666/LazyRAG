package workflow

import (
	"encoding/json"
	"testing"

	"lazymind/core/common/orm"
	"lazymind/core/workflow/controlstore"
	"lazymind/core/workflow/graphengine"
)

func TestLegacyControlReceiptReplaysAndRetainsConflictChecks(t *testing.T) {
	for _, kind := range []string{"begin", "stop"} {
		t.Run(kind, func(t *testing.T) {
			graph := graphengine.CompiledStateGraph{StartRoute: "all",
				Nodes:        map[string]graphengine.CompiledNode{"draft": {ID: "draft", Mode: "auto"}},
				ControlEdges: []graphengine.CompiledEdge{{ID: "start", From: "__start__", To: "draft"}, {ID: "end", From: "draft", To: "__end__"}}}
			db, session := newControllerTestSession(t, "external-agent", graph)
			svc := WorkflowControlService{DB: db.DB}
			command := WorkflowControlCommand{CommandID: "old-command", Kind: kind, StepID: "draft", Instruction: "original input", StateVersion: session.StateVersion}
			if command.StateVersion == 0 {
				t.Fatal("fixture must use a nonzero state version")
			}
			first, err := svc.Execute(t.Context(), session.CreateUserID, session.ID, command)
			if err != nil {
				t.Fatal(err)
			}
			// Persist the exact receipt format used before version-independent hashes.
			legacyBody, err := json.Marshal(command)
			if err != nil {
				t.Fatal(err)
			}
			if err := db.Model(&orm.WorkflowCommand{}).Where("command_id = ?", command.CommandID).Update("request_hash", controlstore.Hash(legacyBody)).Error; err != nil {
				t.Fatal(err)
			}
			var eventsBefore, attemptsBefore int64
			if err := db.Model(&orm.WorkflowEvent{}).Count(&eventsBefore).Error; err != nil {
				t.Fatal(err)
			}
			if err := db.Model(&orm.WorkflowSessionStep{}).Count(&attemptsBefore).Error; err != nil {
				t.Fatal(err)
			}
			changed := command
			changed.Instruction = "different input"
			_, err = svc.Execute(t.Context(), session.CreateUserID, session.ID, changed)
			expectControlCode(t, err, "COMMAND_CONFLICT")
			_, err = svc.Execute(t.Context(), "another-owner", session.ID, command)
			expectControlCode(t, err, "PERMISSION_DENIED")
			other := session
			other.ID = "another-session"
			if err := db.Create(&other).Error; err != nil {
				t.Fatal(err)
			}
			_, err = svc.Execute(t.Context(), other.CreateUserID, other.ID, command)
			expectControlCode(t, err, "COMMAND_CONFLICT")
			for _, version := range []int64{command.StateVersion, first.Control.StateVersion} {
				command.StateVersion = version
				replay, err := svc.Execute(t.Context(), session.CreateUserID, session.ID, command)
				if err != nil || replay.Receipt != first.Receipt || replay.Control.StateVersion != first.Control.StateVersion {
					t.Fatalf("accepted command did not replay: %+v %v", replay, err)
				}
			}
			changed.StateVersion = first.Control.StateVersion
			_, err = svc.Execute(t.Context(), session.CreateUserID, session.ID, changed)
			expectControlCode(t, err, "COMMAND_CONFLICT")
			var eventsAfter, attemptsAfter int64
			if err := db.Model(&orm.WorkflowEvent{}).Count(&eventsAfter).Error; err != nil {
				t.Fatal(err)
			}
			if err := db.Model(&orm.WorkflowSessionStep{}).Count(&attemptsAfter).Error; err != nil {
				t.Fatal(err)
			}
			if eventsBefore != eventsAfter || attemptsBefore != attemptsAfter {
				t.Fatal("receipt replay repeated command side effects")
			}
		})
	}
}
