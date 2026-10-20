//go:build !darwin

package workbuddyhost

import "context"

// Persistent GUI launch configuration is currently implemented for macOS.
// Other platforms retain their explicit launchDesktop behavior.
func configureDesktopStartup(context.Context, string, int, bool) error { return nil }
