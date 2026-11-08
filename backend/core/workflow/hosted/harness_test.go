package hosted

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gorilla/mux"
	"lazymind/core/common/orm"
	"lazymind/core/workflow/executor"
	"lazymind/core/workflow/graphengine"
)

func TestExternalRenewalKeepsHandleAndRejectsLostAuthority(t *testing.T) {
	for _, state := range []string{"active", "expired", "replaced", "stopped", "wrong-owner"} {
		t.Run(state, func(t *testing.T) {
			service, db, grant := controlledService(t)
			owner, handle := "owner", grant.ExecutionHandle
			past := time.Now().UTC().Add(-time.Minute)
			switch state {
			case "expired":
				if err := db.Model(&orm.WorkflowSessionStep{}).Where("id = ?", "attempt-1").Update("lease_expires_at", past).Error; err != nil {
					t.Fatal(err)
				}
			case "replaced":
				handle = "old-handle"
			case "stopped":
				if err := db.Model(&orm.WorkflowSession{}).Where("id = ?", "session-1").Update("status", "stopped").Error; err != nil {
					t.Fatal(err)
				}
			case "wrong-owner":
				owner = "other"
			}
			expires, err := service.Renew(t.Context(), owner, "session-1", "attempt-1", handle)
			if state != "active" {
				if err == nil {
					t.Fatal("renewed lost authority")
				}
				return
			}
			if err != nil || !expires.After(grant.LeaseExpires) {
				t.Fatalf("renewal failed: %v %v", expires, err)
			}
			var row orm.WorkflowSessionStep
			if err := db.First(&row, "id = ?", "attempt-1").Error; err != nil {
				t.Fatal(err)
			}
			if row.LeaseToken != handle || row.Status != "claimed" || row.ProgressJSON != "{}" {
				t.Fatalf("renewal changed identity/progress: %+v", row)
			}
		})
	}
}

func TestHTTPDiagnosticsAllowOutputRepairWithoutAnotherAttempt(t *testing.T) {
	service, db, grant := controlledService(t)
	call := func(handler http.HandlerFunc, body any) (int, map[string]any) {
		raw, _ := json.Marshal(body)
		request := mux.SetURLVars(httptest.NewRequest("POST", "/", bytes.NewReader(raw)), map[string]string{"session_id": "session-1", "attempt_id": "attempt-1"})
		request.Header.Set("X-User-Id", "owner")
		response := httptest.NewRecorder()
		handler(response, request)
		var value map[string]any
		if err := json.Unmarshal(response.Body.Bytes(), &value); err != nil {
			t.Fatal(err)
		}
		return response.Code, value
	}
	h := Handler{Service: service}
	completion := executor.Completion{ExecutionHandle: grant.ExecutionHandle, Outcome: "succeeded"}
	status, value := call(h.Complete, completion)
	problem := value["error"].(map[string]any)
	if status != 422 || problem["code"] != "REQUIRED_OUTPUT_MISSING" || problem["retryable"] != false || problem["recovery"] == "" {
		t.Fatalf("bad diagnostic: %v", value)
	}
	slots := problem["details"].(map[string]any)["missing_slots"].([]any)
	if len(slots) != 1 || slots[0] != "report" {
		t.Fatalf("missing slots: %v", slots)
	}
	status, value = call(h.Publish, Publication{ExecutionHandle: grant.ExecutionHandle, Artifact: executor.Artifact{Slot: "report", ContentType: "file", Value: json.RawMessage(`{"storage":"inline_base64","name":"report.txt","content_base64":"invalid!"}`)}})
	if status != 422 || value["error"].(map[string]any)["code"] != "INVALID_ARTIFACT_FILE" {
		t.Fatalf("invalid file became service failure: %d %v", status, value)
	}
	for _, artifact := range successfulSubmission(grant.ExecutionHandle).Artifacts {
		status, value = call(h.Publish, Publication{ExecutionHandle: grant.ExecutionHandle, Artifact: artifact})
		if status != 200 {
			t.Fatalf("repair failed: %v", value)
		}
	}
	status, value = call(h.Complete, completion)
	if status != 200 {
		t.Fatalf("complete after repair failed: %v", value)
	}
	var count int64
	if err := db.Model(&orm.WorkflowSessionStep{}).Count(&count).Error; err != nil || count != 1 {
		t.Fatalf("repair created another attempt: %d %v", count, err)
	}
}

func TestUnadaptedRevisionRejectsExternalClaimWithoutIssuingLease(t *testing.T) {
	service, db := hostedTestService(t)
	var revision orm.WorkflowRevision
	if err := db.First(&revision, "id = ?", "revision-1").Error; err != nil {
		t.Fatal(err)
	}
	var graph graphengine.CompiledStateGraph
	if err := json.Unmarshal(revision.CompiledGraph, &graph); err != nil {
		t.Fatal(err)
	}
	graph.Runtime.ExternalExecution = map[string]any{"version": "1"}
	if err := db.Model(&revision).Update("compiled_graph", graph.JSON()).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := service.Begin(t.Context(), "owner", "session-1", "attempt-1"); err == nil {
		t.Fatal("old overlay executed")
	}
	row, err := service.Attempts.Attempt(t.Context(), "attempt-1")
	if err != nil || row.LeaseToken != "" || row.Status != "queued" {
		t.Fatalf("rejection issued authority: %+v %v", row, err)
	}
}
