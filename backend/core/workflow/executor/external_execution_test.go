package executor

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"reflect"
	"testing"
	"time"

	"gorm.io/gorm"
	"lazymind/core/common/orm"
	"lazymind/core/workflow/controlpolicy"
	"lazymind/core/workflow/graphengine"
	workflowstore "lazymind/core/workflow/store"
)

func externalExecutionDB(t *testing.T) *gorm.DB {
	t.Helper()
	db := executorComponentDB(t, &orm.WorkflowSession{}, &orm.WorkflowSessionStep{}, &orm.WorkflowOutbox{},
		&orm.WorkflowRevision{}, &orm.WorkflowRevisionEntry{}, &orm.WorkflowBlob{}, &orm.WorkflowSlotRevision{})
	now := time.Now().UTC()
	expires := now.Add(time.Hour)
	graph := graphengine.CompiledStateGraph{Nodes: map[string]graphengine.CompiledNode{"write": {
		ID: "write", Prompt: "Write a report", ToolsOnly: true, TerminalToolsOnly: true,
		LegacyTools: []string{"native_generator"}, TerminalTools: []string{"native_generator"},
		Acceptance: []string{"At least two pages"}, Outputs: []string{"result", "native_only"}, RequiredOutputs: []string{"native_only"},
	}}}
	for _, model := range []any{
		&orm.WorkflowRevision{ID: "pinned", WorkflowResourceID: "resource", RevisionNo: 1, CompiledGraph: graph.JSON(), CreatedAt: now},
		&orm.WorkflowSession{ID: "run", ConversationID: "chat", WorkflowRevisionID: "pinned", CreateUserID: "owner", ControllerHost: "external-agent", ControlProtocol: controlpolicy.Protocol, Status: "active"},
		&orm.WorkflowSessionStep{ID: "attempt", TaskID: "attempt", SessionID: "run", StepID: "write", ExecutorHost: "external-agent", Status: "running", Validity: "effective", LeaseToken: "valid-handle", LeaseExpiresAt: &expires},
		&orm.WorkflowOutbox{ID: "outbox", AttemptID: "attempt", SessionID: "run", Status: "pending", PayloadJSON: json.RawMessage(`{"objective":"User objective","operation":"execute"}`)},
	} {
		if err := db.Create(model).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []struct{ revision, path, hash, content string }{
		{"pinned", "workflow.yaml", "manifest", "slots:\n  - {id: result, cardinality: single}\n  - {id: native_only, cardinality: single}\n"},
	} {
		file.hash = fmt.Sprintf("%x", sha256.Sum256([]byte(file.content)))
		if err := db.Create(&orm.WorkflowBlob{Hash: file.hash, Content: []byte(file.content), Size: int64(len(file.content))}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&orm.WorkflowRevisionEntry{RevisionID: file.revision, Path: file.path, EntryType: "file", BlobHash: &file.hash}).Error; err != nil {
			t.Fatal(err)
		}
	}
	return db
}

func TestBothExecutorsReadOnePinnedBusinessContract(t *testing.T) {
	db := externalExecutionDB(t)
	loader := DBContextLoader{DB: db}
	external, err := loader.LoadAttemptContext(t.Context(), "attempt")
	if err != nil {
		t.Fatal(err)
	}
	if external.Prompt != "Write a report" || external.ToolsOnly || external.TerminalToolsOnly || len(external.LegacyTools) != 0 {
		t.Fatalf("bad external contract: %+v", external)
	}
	if err := db.Model(&orm.WorkflowSessionStep{}).Where("id = ?", "attempt").Update("executor_host", "lazymind").Error; err != nil {
		t.Fatal(err)
	}
	native, err := loader.LoadAttemptContext(t.Context(), "attempt")
	if err != nil {
		t.Fatal(err)
	}
	if !native.ToolsOnly || native.Prompt != external.Prompt || !reflect.DeepEqual(native.Acceptance, external.Acceptance) || !reflect.DeepEqual(native.RequiredOutputs, external.RequiredOutputs) {
		t.Fatalf("different business contract: %+v / %+v", native, external)
	}
}

func TestExternalAuthorizationWithoutProfilePreservesFences(t *testing.T) {
	db := externalExecutionDB(t)
	value, err := AuthorizeExternalExecution(t.Context(), db, "owner", "run", "attempt", "valid-handle")
	if err != nil || value.Metadata != nil {
		t.Fatalf("authorized execution: %+v %v", value, err)
	}
	for _, test := range []struct {
		name, owner, handle, tool string
		ctx                       context.Context
	}{
		{"owner", "other", "valid-handle", "inspect_output", t.Context()},
		{"lease", "owner", "stale-handle", "inspect_output", t.Context()},
		{"conversation", "owner", "valid-handle", "inspect_output", workflowstore.WithConversationScope(t.Context(), "other-chat")},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := AuthorizeExternalExecution(test.ctx, db, test.owner, "run", "attempt", test.handle); err == nil {
				t.Fatal("unauthorized tool call accepted")
			}
		})
	}
	for _, state := range []string{"stopped", "active"} {
		if err := db.Model(&orm.WorkflowSession{}).Where("id = ?", "run").Update("status", state).Error; err != nil {
			t.Fatal(err)
		}
		if state == "active" {
			if err := db.Model(&orm.WorkflowSessionStep{}).Where("id = ?", "attempt").Update("validity", "stale").Error; err != nil {
				t.Fatal(err)
			}
		}
		if _, err := AuthorizeExternalExecution(t.Context(), db, "owner", "run", "attempt", "valid-handle"); err == nil {
			t.Fatalf("%s/stale execution retained tool authority", state)
		}
	}
}
