package workflowtool

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gorilla/mux"
	"lazymind/core/common/orm"
)

func catalogFixture(t *testing.T, transport transportFunc) *Service {
	t.Helper()
	service, _ := workflowToolFixture(t, transport)
	if err := service.DB.AutoMigrate(&orm.MCPServer{}, &orm.MCPServerTool{}, &orm.ExternalCapabilityGrant{},
		&orm.UserSelectedProvider{}, &orm.UserModelProviderGroup{}, &orm.UserModelProvider{}); err != nil {
		t.Fatal(err)
	}
	if err := service.DB.Model(&orm.WorkflowSession{}).Where("id = ?", "run").Update("control_binding_json", `{"provider":"codex"}`).Error; err != nil {
		t.Fatal(err)
	}
	if err := service.DB.Model(&orm.WorkflowRevisionEntry{}).Where("revision_id = ? AND path = ?", "pinned-revision", "scripts/tools.py").Update("path", "scripts/external_tools.py").Error; err != nil {
		t.Fatal(err)
	}
	hash := fmt.Sprintf("%x", sha256.Sum256([]byte(knowledgeBinding)))
	for _, row := range []any{
		&orm.WorkflowBlob{Hash: hash, Content: []byte(knowledgeBinding), Size: int64(len(knowledgeBinding))},
		&orm.WorkflowRevisionEntry{RevisionID: "pinned-revision", Path: knowledgeBindingPath, EntryType: "file", BlobHash: &hash},
	} {
		if err := service.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	return service
}

func TestCatalogHTTPPreservesHealthyToolsAndSourceWarnings(t *testing.T) {
	for _, broken := range []string{"package", "global", "both"} {
		t.Run(broken, func(t *testing.T) {
			service := catalogFixture(t, func(r *http.Request) (*http.Response, error) {
				switch r.URL.Path {
				case "/api/workflow/external-tools/list":
					if broken == "package" || broken == "both" {
						return nil, errors.New("package discovery offline")
					}
					return toolResponse(`{"tools":[{"name":"ppt_inspect_pptx","available":true,"input_schema":{"type":"object"}}]}`), nil
				case "/api/chat/tools/external-catalog":
					if broken == "global" || broken == "both" {
						return nil, errors.New("global discovery offline")
					}
					return toolResponse(`{"items":[{"name":"url_fetch","available":true,"input_schema":{"type":"object"}}]}`), nil
				default:
					t.Fatalf("unexpected external call: %s", r.URL.Path)
					return nil, nil
				}
			})
			r := mux.SetURLVars(httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"execution_handle":"lease"}`)), map[string]string{"session_id": "run", "attempt_id": "attempt"})
			r.Header.Set("X-User-Id", "owner")
			w := httptest.NewRecorder()
			(Handler{Service: service}).List(w, r)
			var response struct {
				OK     bool       `json:"ok"`
				Result ListResult `json:"result"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if w.Code != http.StatusOK || !response.OK {
				t.Fatalf("partial catalog rejected: %d %s", w.Code, w.Body.String())
			}
			names := map[string]bool{}
			for _, tool := range response.Result.Tools {
				names[tool.Name] = tool.Available
			}
			if !names["knowledge_list_documents"] || !names["knowledge_read_document"] || names["ppt_inspect_pptx"] != (broken == "global") || names["builtin:url_fetch"] != (broken == "package") {
				t.Fatalf("healthy tools lost or unavailable tools advertised: %+v", response.Result)
			}
			wantWarnings := 1
			if broken == "both" {
				wantWarnings = 2
			}
			if len(response.Result.Warnings) != wantWarnings {
				t.Fatalf("missing source warnings: %+v", response.Result)
			}
			for _, warning := range response.Result.Warnings {
				if (broken != "both" && warning.Source != broken) || warning.Error == nil || warning.Error.Code == "" || warning.Error.Message == "" {
					t.Fatalf("unusable diagnostic: %+v", warning)
				}
			}
			result, err := service.Call(t.Context(), "owner", "run", "attempt", CallInput{ExecutionHandle: "lease", ToolName: "knowledge_list_documents", CommandID: "still-usable", Arguments: map[string]any{}})
			if err != nil || result.Error != nil {
				t.Fatalf("healthy adapter no longer callable: %+v %v", result, err)
			}
		})
	}
}

func TestPartialCatalogStillRejectsRevokedExecution(t *testing.T) {
	for _, action := range []string{"stop", "replace"} {
		t.Run(action, func(t *testing.T) {
			var service *Service
			service = catalogFixture(t, func(r *http.Request) (*http.Response, error) {
				if strings.HasSuffix(r.URL.Path, "/external-tools/list") {
					var err error
					if action == "stop" {
						err = service.DB.Model(&orm.WorkflowSession{}).Where("id = ?", "run").Update("status", "stopped").Error
					} else {
						err = service.DB.Model(&orm.WorkflowSessionStep{}).Where("id = ?", "attempt").Update("lease_token", "replacement").Error
					}
					if err != nil {
						t.Fatal(err)
					}
				}
				return nil, errors.New("discovery offline")
			})
			catalog, err := service.List(t.Context(), "owner", "run", "attempt", "lease")
			wantCode := "EXECUTION_FENCED"
			if action == "stop" {
				wantCode = "SESSION_STOPPED"
			}
			requireControlError(t, err, wantCode)
			if len(catalog.Tools) != 0 || len(catalog.Warnings) != 0 {
				t.Fatal("revoked execution received a partial catalog")
			}
		})
	}
}
