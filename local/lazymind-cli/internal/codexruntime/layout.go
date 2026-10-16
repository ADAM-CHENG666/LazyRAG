package codexruntime

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// Resolve only an actual bundled CLI, never an unrelated PATH installation.
// Unknown Store/package layouts fail before startup configuration is changed.
func windowsDesktopLayout(binary string) (string, string, error) {
	cli, err := filepath.EvalSymlinks(binary)
	if err != nil {
		return "", "", err
	}
	for dir := filepath.Dir(cli); dir != filepath.Dir(dir); dir = filepath.Dir(dir) {
		resources := filepath.Join(dir, "resources")
		candidates := []string{filepath.Join(resources, "codex.exe"), filepath.Join(resources, "codex-cli", "codex.exe"), filepath.Join(resources, "codex-cli", "bin", "codex.exe")}
		match := false
		for _, p := range candidates {
			if strings.EqualFold(filepath.Clean(p), filepath.Clean(cli)) {
				match = true
			}
		}
		if !match {
			continue
		}
		desktops := 0
		for _, name := range []string{"Codex.exe", "ChatGPT.exe"} {
			if info, e := os.Stat(filepath.Join(dir, name)); e == nil && info.Mode().IsRegular() {
				desktops++
			}
		}
		nodes := []string{}
		for _, p := range []string{"node.exe", filepath.Join("bin", "node.exe")} {
			p = filepath.Join(resources, "cua_node", p)
			if info, e := os.Stat(p); e == nil && info.Mode().IsRegular() {
				nodes = append(nodes, p)
			}
		}
		if desktops == 1 && len(nodes) == 1 {
			return dir, nodes[0], nil
		}
		return "", "", errors.New("unsupported or ambiguous Windows Desktop layout")
	}
	return "", "", errors.New("shared mode requires the Windows Desktop bundled CLI")
}
