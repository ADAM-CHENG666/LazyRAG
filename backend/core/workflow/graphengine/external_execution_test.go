package graphengine

import (
	"encoding/json"
	"testing"
)

const externalWorkflow = `
id: portable
runtime:
  external_execution:
    version: "1"
    skills: [scenario/external.md]
    constraints: [Validate artifacts before submitting]
    steps:
      write:
        prompt: Generate the output with the external agent
        skills: [scenario/write.md, scenario/external.md]
        acceptance_criteria: [One page is allowed]
        required_outputs: [result]
        tools:
          - {name: inspect_output, source: package}
          - {name: "builtin:image_generator", source: global}
slots:
  - {id: result, type: file}
steps:
  - {id: write}
`

const externalState = `
transitions:
  __start__: [{to: write}]
  write: [{to: __end__}]
steps:
  write:
    prompt: Use the native generator
    tools: [native_generator]
    tools_only: true
    terminal_tools: [native_generator]
    acceptance_criteria: [At least two pages]
    outputs: [{material: result, optional: true}]
`

func TestExternalOverlayRequiresPackageRefactor(t *testing.T) {
	result := Compile(externalWorkflow, externalState, "", ProfilePublish)
	if !result.Valid {
		t.Fatal("external overlay removal must not break native compilation")
	}
	found := false
	for _, problem := range result.Diagnostics {
		if problem.Code == "W_EXTERNAL_EXECUTION_REMOVED" {
			found = true
		}
	}
	if !found {
		t.Fatalf("missing actionable diagnostic: %+v", result.Diagnostics)
	}
	shared := "id: portable\nslots: [{id: result, type: file}]\nsteps: [{id: write}]\n"
	result = Compile(shared, externalState, "", ProfilePublish)
	if !result.Valid {
		t.Fatalf("shared step failed: %+v", result.Diagnostics)
	}
	var restored CompiledStateGraph
	if err := json.Unmarshal(result.Graph.JSON(), &restored); err != nil {
		t.Fatal(err)
	}
	if restored.Nodes["write"].Prompt != "Use the native generator" || len(restored.Nodes["write"].Acceptance) != 1 {
		t.Fatal("business contract lost")
	}
}
