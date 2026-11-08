package externalcapability

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"

	"lazymind/core/capability"
	"lazymind/core/common/orm"
)

const maxSharedToolBytes = 36 << 20 // Bounded base64 files, including JSON overhead.

// Resolve only the FileInput fields of shared built-ins. User-supplied identity,
// local paths and unrelated nested objects never become filesystem authority.
func (s *Service) resolveSharedFiles(ctx context.Context, owner, name string, arguments map[string]any) (map[string]any, error) {
	fields := map[string][]string{
		"resource.read": {"source"}, "resource.search": {"source"}, "document.parse": {"source"},
		"resource.images.extract": {"source"}, "github.pr.read": {"source"},
		"image.inspect": {"source"}, "image.caption": {"source"}, "image.compose": {"sources"},
		"video_to_gif": {"source"}, "presentation.inspect": {"source"}, "presentation.fill": {"source"},
		"presentation.create": {"template"}, "presentation.edit": {"source"},
		"presentation.render": {"source"}, "academic.read": {"source"},
	}[name]
	result := make(map[string]any, len(arguments))
	for key, value := range arguments {
		result[key] = value
	}
	total := 0
	resolve := func(value any) (any, error) {
		input, ok := value.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("file input must be an object")
		}
		out := make(map[string]any, len(input))
		for key, value := range input {
			out[key] = value
		}
		id, _ := input["resource_id"].(string)
		encoded, _ := input["content_base64"].(string)
		var content []byte
		if id != "" {
			if encoded != "" {
				return nil, fmt.Errorf("choose resource_id or content_base64, not both")
			}
			var resource orm.WorkflowInputResource
			if err := s.db.WithContext(ctx).Where("id = ? AND owner_user_id = ?", id, owner).First(&resource).Error; err != nil {
				return nil, fmt.Errorf("resource is unavailable to this user")
			}
			content = resource.Content
			if resource.Size != int64(len(content)) || resource.ContentHash != fmt.Sprintf("sha256:%x", sha256.Sum256(content)) {
				return nil, fmt.Errorf("resource integrity check failed; import it again")
			}
			delete(out, "resource_id")
			out["content_base64"] = base64.StdEncoding.EncodeToString(content)
		} else {
			if len(encoded) > (24<<20)*4/3+4 {
				return nil, fmt.Errorf("file exceeds 24 MiB")
			}
			var err error
			content, err = base64.StdEncoding.Strict().DecodeString(encoded)
			if err != nil || len(content) == 0 {
				return nil, fmt.Errorf("file content is empty or is not valid base64")
			}
		}
		total += len(content)
		if total > 24<<20 {
			return nil, fmt.Errorf("combined file inputs exceed 24 MiB")
		}
		actual := fmt.Sprintf("%x", sha256.Sum256(content))
		if expected, _ := input["sha256"].(string); expected != "" && expected != actual {
			failure := capability.NewError(capability.Conflict, "tool.call", "File SHA-256 does not match the supplied bytes.", false, nil)
			failure.Details = map[string]any{"tool_code": "VERSION_CONFLICT", "actual_sha256": actual}
			failure.Reason = "Read the current file and recompute the edit."
			return nil, failure
		}
		out["sha256"] = actual
		return out, nil
	}
	for _, field := range fields {
		value := arguments[field]
		if value == nil {
			continue
		}
		if field == "sources" {
			values, ok := value.([]any)
			if !ok || len(values) > 16 {
				return nil, fmt.Errorf("sources must be an array of at most 16 files")
			}
			resolved := make([]any, 0, len(values))
			for _, item := range values {
				file, err := resolve(item)
				if err != nil {
					return nil, err
				}
				resolved = append(resolved, file)
			}
			result[field] = resolved
		} else {
			file, err := resolve(value)
			if err != nil {
				return nil, err
			}
			result[field] = file
		}
	}
	return result, nil
}

// Only the controlled shared-tool error envelope is public. Raw Python/provider
// exceptions still use the existing generic failure path.
func sharedToolError(raw []byte) error {
	var response struct {
		Detail struct {
			Code      string         `json:"code"`
			Message   string         `json:"message"`
			Retryable bool           `json:"retryable"`
			Details   map[string]any `json:"details"`
			Recovery  string         `json:"recovery"`
		} `json:"detail"`
	}
	if json.Unmarshal(raw, &response) != nil || response.Detail.Code == "" || response.Detail.Message == "" {
		return nil
	}
	detail := response.Detail
	code := capability.InvalidArgument
	switch detail.Code {
	case "VERSION_CONFLICT":
		code = capability.Conflict
	case "RESOURCE_LIMIT":
		code = capability.ResultTooLarge
	case "DEPENDENCY_UNAVAILABLE", "TOOL_EXECUTION_FAILED", "OCR_REQUIRED":
		code = capability.Unavailable
	case "INVALID_ARGUMENT", "RESOURCE_NOT_RESOLVED", "UNDEFINED_STATISTIC":
	default:
		return nil
	}
	err := capability.NewError(code, "tool.call", detail.Message, detail.Retryable, nil)
	err.Details = map[string]any{}
	for _, key := range []string{"actual_sha256", "fields", "allowed"} {
		if value, ok := detail.Details[key]; ok {
			err.Details[key] = value
		}
	}
	err.Details["tool_code"] = detail.Code
	err.Reason = strings.TrimSpace(detail.Recovery)
	return err
}
