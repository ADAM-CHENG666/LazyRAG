package codexruntime

// Native operations are short-lived CLI calls, not a new resident service.
import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

type ProcessInfo struct {
	PID        int    `json:"pid"`
	PPID       int    `json:"ppid"`
	Executable string `json:"executable"`
	Identity   string `json:"identity"`
}

type environmentSnapshot struct {
	Values map[string]*string `json:"values"`
	Types  map[string]uint32  `json:"types"`
}

type nativeRequest struct {
	Path   string             `json:"path"`
	PID    int                `json:"pid"`
	Values map[string]*string `json:"values"`
	Types  map[string]uint32  `json:"types"`
}

func Supported() bool { return runtime.GOOS == "darwin" || runtime.GOOS == "windows" }

func RunNative(ctx context.Context, args []string, input io.Reader, output io.Writer) error {
	if len(args) != 1 {
		return errors.New("expected one runtime operation")
	}
	var req nativeRequest
	if err := json.NewDecoder(io.LimitReader(input, 1<<20)).Decode(&req); err != nil {
		return err
	}
	var result any = map[string]bool{"ok": true}
	var err error
	switch args[0] {
	case "process":
		result, err = platformProcess(req.PID)
	case "running":
		result, err = platformRunning(req.Path)
	case "check-private":
		err = checkPrivate(req.Path)
	case "make-private":
		err = makePrivate(req.Path)
	case "environment-read":
		result, err = platformEnvironment(nil, nil)
	case "environment-write":
		result, err = platformEnvironment(req.Values, req.Types)
	case "ensure":
		result, err = ensureBackend(ctx, req.Path)
	default:
		return errors.New("unknown runtime operation")
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(result)
}

// Windows needs an executable entry point. The installed copy resolves its own
// sibling config, forwards every original argument and leaves protocol stdout clean.
func IsLauncher() bool {
	exe, _ := os.Executable()
	return runtime.GOOS == "windows" && strings.EqualFold(filepath.Base(exe), "launch-adapter.exe")
}
func RunLauncher(ctx context.Context, args []string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	file := filepath.Join(filepath.Dir(exe), "runtime.json")
	config, err := readRuntime(file)
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, config.Node, append([]string{filepath.Join(config.BundleDir, "adapter.mjs")}, args...)...)
	cmd.Env = append(os.Environ(), "LAZYMIND_CODEX_DESKTOP_CONFIG="+file, "LAZYMIND_RUNTIME_HELPER="+exe)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func privateDirectory(dir string) error {
	_, err := os.Lstat(dir)
	if os.IsNotExist(err) {
		if err = os.MkdirAll(dir, 0700); err != nil {
			return err
		}
		if err = makePrivate(dir); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	return checkPrivate(dir)
}
