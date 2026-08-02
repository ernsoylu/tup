package vfs

import (
	"path"
	"strings"
)

type PathInfo struct {
	IsRemote bool
	Alias    string
	Path     string
}

// ParsePath determines if a path is a remote Telegram drive or a local filesystem path.
// Examples:
//
//	"work:/docs/file.txt" -> remote work, /docs/file.txt
//	"VFS:/" or "VFS://"   -> remote VFS, /
//	"VFS:"                -> remote VFS, /
//
// Bare names without a colon remain local (e.g. "./file", "README.md").
func ParsePath(input string) PathInfo {
	if idx := strings.Index(input, ":"); idx > 0 {
		alias := input[:idx]
		rest := input[idx+1:]
		// "alias:/" "alias://path" "alias:" (root) — require empty rest or leading slash
		if rest == "" || rest[0] == '/' {
			cleanPath := "/"
			if rest != "" {
				cleanPath = path.Clean("/" + strings.TrimLeft(rest, "/"))
			}
			return PathInfo{
				IsRemote: true,
				Alias:    alias,
				Path:     cleanPath,
			}
		}
	}
	return PathInfo{
		IsRemote: false,
		Path:     input,
	}
}

// Format returns a Unix-style remote path "alias:/path", or the local path as-is.
// Path is always absolute for remote entries, so this never emits "alias://...".
func (p PathInfo) Format() string {
	if !p.IsRemote {
		return p.Path
	}
	return FormatRemote(p.Alias, p.Path)
}

// FormatRemote joins a drive alias and path into "alias:/path".
// Bare names and multi-slash prefixes are normalized (e.g. "nested" -> "/nested").
func FormatRemote(alias, remotePath string) string {
	remotePath = "/" + strings.TrimLeft(remotePath, "/")
	if remotePath != "/" {
		remotePath = path.Clean(remotePath)
	}
	return alias + ":" + remotePath
}
