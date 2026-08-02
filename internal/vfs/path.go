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
// Example: "work:/docs/file.txt" -> IsRemote: true, Alias: "work", Path: "/docs/file.txt"
// Safely handles variations like "VFS:/", "VFS://", "VFS:///".
func ParsePath(input string) PathInfo {
	if idx := strings.Index(input, ":"); idx != -1 && idx < len(input)-1 && input[idx+1] == '/' {
		alias := input[:idx]
		rest := "/" + strings.TrimLeft(input[idx+1:], "/")
		cleanPath := path.Clean(rest)
		return PathInfo{
			IsRemote: true,
			Alias:    alias,
			Path:     cleanPath,
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
