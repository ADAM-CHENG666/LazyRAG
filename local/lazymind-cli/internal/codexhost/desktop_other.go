//go:build !darwin && !windows

package codexhost

import (
	"context"
	"errors"
)

func launchDesktop(context.Context, string, int) error {
	return errors.New("Codex Desktop CDP launch is not supported on this platform")
}
