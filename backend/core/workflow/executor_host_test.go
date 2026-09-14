package workflow

import (
	"testing"

	"lazymind/core/workflow/graphengine"
)

func TestStepUsesInternalTools(t *testing.T) {
	packageFuncs := []string{"build_test_metadata", "select_image_route"}
	cases := []struct {
		name       string
		declared   []string
		wantNative bool
	}{
		{name: "prompt only", declared: nil},
		{name: "package scripts", declared: []string{"build_test_metadata", "select_image_route"}},
		{name: "platform tool", declared: []string{"image_generator"}, wantNative: true},
		{name: "mixed", declared: []string{"select_image_route", "kb"}, wantNative: true},
		{name: "unknown name", declared: []string{"not_in_package"}, wantNative: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := stepUsesInternalTools(tc.declared, packageFuncs)
			if got != tc.wantNative {
				t.Fatalf("declared=%v native=%t, want %t", tc.declared, got, tc.wantNative)
			}
		})
	}
}

func TestExecutorHostForStepKeepsPackageStepsOnTheHost(t *testing.T) {
	packageFuncs := []string{"package_tool"}
	host := executorHostForStep("external-agent", graphengine.CompiledNode{LegacyTools: []string{"package_tool"}}, packageFuncs)
	if host != "external-agent" {
		t.Fatalf("package step host=%q", host)
	}
	host = executorHostForStep("external-agent", graphengine.CompiledNode{LegacyTools: []string{"image_generator"}}, packageFuncs)
	if host != "lazymind" {
		t.Fatalf("internal-tool step host=%q", host)
	}
	host = executorHostForStep("lazymind", graphengine.CompiledNode{LegacyTools: []string{"image_generator"}}, packageFuncs)
	if host != "lazymind" {
		t.Fatalf("native session host=%q", host)
	}
}

func TestPackageScriptFunctionsReadsEveryScriptFile(t *testing.T) {
	names := packageScriptFunctions([]byte("tool_scripts:\n  - path: scripts/tools.py\n    functions: [a]\n  - path: scripts/pipeline_tools.py\n    functions: [b, c]\n"))
	if len(names) != 3 || names[0] != "a" || names[2] != "c" {
		t.Fatalf("names=%v", names)
	}
}
