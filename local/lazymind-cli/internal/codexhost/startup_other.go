//go:build !darwin && !windows

package codexhost

import (
	"context"
	"errors"
)

func configureStartup(context.Context, string, *startupRecord, string) error {
	return errors.New("Codex desktop startup is unsupported on this platform")
}
func disableStartup(context.Context, string, startupRecord) error { return nil }
func checkStartup(context.Context, string, startupRecord) error {
	return errors.New("Codex desktop startup is unsupported on this platform")
}
