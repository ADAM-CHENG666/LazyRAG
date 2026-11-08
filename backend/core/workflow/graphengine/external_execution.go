package graphengine

// Mark the removed overlay without breaking native packages. External claim
// rejects these revisions explicitly; package authors own their refactoring.
func compileExternalExecution(graph *CompiledStateGraph) []Diagnostic {
	if graph.Runtime.ExternalExecution == nil {
		return nil
	}
	return []Diagnostic{diag("W_EXTERNAL_EXECUTION_REMOVED", "warning", "workflow.yaml.runtime.external_execution",
		"Use the shared step prompt, acceptance_criteria and required_outputs; external_execution is no longer supported. Refactor and publish the workflow package.")}
}
