// Package codexruntime reads the shared Desktop registration used by both
// Workflow binding and the Node dispatcher. Missing registration means queue;
// a malformed registration must never silently advertise legacy capabilities.
package codexruntime

import (
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const RegistrationName = "lazymind-shared-server.json"

type Registration struct {
	Version  int    `json:"version"`
	Profile  string `json:"profile"`
	Endpoint string `json:"app_server_url"`
}

func Read(profile string) (*Registration, error) {
	file := filepath.Join(profile, RegistrationName)
	info, err := os.Lstat(file)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || runtime.GOOS != "windows" && (info.Mode().Perm()&0077 != 0 || !ownedByCurrentUser(info)) {
		return nil, errors.New("shared server registration must be a private owned file")
	}
	body, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}
	var value Registration
	if json.Unmarshal(body, &value) != nil || value.Version != 1 || value.Profile != profile {
		return nil, errors.New("shared server registration does not match this Codex profile")
	}
	const prefix = "ws+unix://localhost"
	if !strings.HasPrefix(value.Endpoint, prefix+"/") || !strings.HasSuffix(value.Endpoint, ":/rpc") {
		return nil, errors.New("shared server endpoint must be a local Unix WebSocket")
	}
	encoded := strings.TrimSuffix(strings.TrimPrefix(value.Endpoint, prefix), ":/rpc")
	if strings.ContainsAny(encoded, "?#") {
		return nil, errors.New("invalid shared server endpoint")
	}
	socket, err := url.PathUnescape(encoded)
	if err != nil || !filepath.IsAbs(socket) {
		return nil, errors.New("invalid shared server socket path")
	}
	for _, char := range socket {
		if char < 32 || char == ':' {
			return nil, errors.New("invalid shared server socket path")
		}
	}
	return &value, nil
}

func Cancellation(profile string) (string, error) {
	registration, err := Read(profile)
	if err != nil {
		return "", err
	}
	if registration != nil {
		return "session", nil
	}
	return "none", nil
}
