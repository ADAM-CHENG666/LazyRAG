package workflow

import (
	"os"
	"path/filepath"
	"testing"

	"lazymind/core/workflow/graphengine"
)

func TestExecutorHostForStep(t *testing.T) {
	cases := []struct {
		name         string
		node         graphengine.CompiledNode
		checks       []graphengine.PostStepCheck
		wantExternal bool
	}{
		{name: "prompt only", wantExternal: true},
		{name: "package tool", node: graphengine.CompiledNode{LegacyTools: []string{"package_tool"}}},
		{name: "platform tool", node: graphengine.CompiledNode{LegacyTools: []string{"image_generator"}}},
		{name: "generic tool", node: graphengine.CompiledNode{LegacyTools: []string{"web_search"}}},
		{name: "terminal tool", node: graphengine.CompiledNode{TerminalTools: []string{"package_tool"}}},
		{name: "post-step check", checks: []graphengine.PostStepCheck{{StepID: "step", Tool: "check_ready"}}},
		{name: "other step check", checks: []graphengine.PostStepCheck{{StepID: "other", Tool: "check_ready"}}, wantExternal: true},
		{name: "mode flags only", node: graphengine.CompiledNode{ToolsOnly: true, TerminalToolsOnly: true}, wantExternal: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tc.node.ID = "step"
			for _, controller := range []string{"external-agent", "lazymind"} {
				want := "lazymind"
				if controller == "external-agent" && tc.wantExternal {
					want = controller
				}
				got := executorHostForStep(controller, tc.node, graphengine.RuntimePolicy{PostStepChecks: tc.checks})
				if got != want {
					t.Fatalf("controller=%q host=%q, want %q", controller, got, want)
				}
			}
		})
	}
}

func TestScriptlessControlWorkflowAlternatesExecutorHosts(t *testing.T) {
	root := filepath.Join("..", "..", "..", "workflows", "scriptless-test-workflow")
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
		t.Fatalf("scriptless control Workflow must compile: %#v", compiled.Diagnostics)
	}
	want := map[string]string{
		"external_draft":    "external-agent",
		"native_check":      "lazymind",
		"external_finalize": "external-agent",
	}
	for stepID, host := range want {
		node := compiled.Graph.Nodes[stepID]
		if node.Mode != "human" {
			t.Fatalf("step %s mode=%q, want human review", stepID, node.Mode)
		}
		if got := executorHostForStep("external-agent", node, compiled.Graph.Runtime); got != host {
			t.Fatalf("step %s executor host=%q, want %q", stepID, got, host)
		}
	}
}
