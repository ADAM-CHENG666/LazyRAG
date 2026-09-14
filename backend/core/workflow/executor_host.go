package workflow

import (
	"context"

	"gopkg.in/yaml.v3"
	"gorm.io/gorm"
	"lazymind/core/common/orm"
	"lazymind/core/workflow/graphengine"
)

// stepUsesInternalTools is true when a step declares a tool that is not a
// package script function. The whole step then runs inside LazyMind.
func stepUsesInternalTools(declared, packageFuncs []string) bool {
	if len(declared) == 0 {
		return false
	}
	allowed := make(map[string]struct{}, len(packageFuncs))
	for _, name := range packageFuncs {
		if name == "" {
			continue
		}
		allowed[name] = struct{}{}
	}
	for _, name := range declared {
		if name == "" {
			continue
		}
		if _, ok := allowed[name]; !ok {
			return true
		}
	}
	return false
}

func declaredStepTools(node graphengine.CompiledNode) []string {
	declared := make([]string, 0, len(node.LegacyTools)+len(node.TerminalTools))
	declared = append(declared, node.LegacyTools...)
	declared = append(declared, node.TerminalTools...)
	return declared
}

func executorHostForStep(controllerHost string, node graphengine.CompiledNode, packageFuncs []string) string {
	if controllerHost != "external-agent" {
		return controllerHost
	}
	if stepUsesInternalTools(declaredStepTools(node), packageFuncs) {
		return "lazymind"
	}
	return "external-agent"
}

func packageScriptFunctions(workflowYAML []byte) []string {
	var doc struct {
		ToolScripts []struct {
			Functions []string `yaml:"functions"`
		} `yaml:"tool_scripts"`
	}
	if yaml.Unmarshal(workflowYAML, &doc) != nil {
		return nil
	}
	var names []string
	for _, script := range doc.ToolScripts {
		names = append(names, script.Functions...)
	}
	return names
}

func loadPackageScriptFunctions(ctx context.Context, db *gorm.DB, revisionID string) []string {
	if revisionID == "" {
		return nil
	}
	var entry orm.WorkflowRevisionEntry
	if err := db.WithContext(ctx).Where("revision_id = ? AND path = ?", revisionID, "workflow.yaml").First(&entry).Error; err != nil {
		return nil
	}
	if entry.BlobHash == nil {
		return nil
	}
	var blob orm.WorkflowBlob
	if err := db.WithContext(ctx).Where("hash = ?", *entry.BlobHash).First(&blob).Error; err != nil {
		return nil
	}
	return packageScriptFunctions(blob.Content)
}
