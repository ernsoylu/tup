package vfs

import (
	"strings"
)

type PathInfo struct {
	IsRemote bool
	Alias    string
	Path     string
}

// ParsePath determines if a path is a remote Telegram drive or a local filesystem path.
// Example: "work:/docs/file.txt" -> IsRemote: true, Alias: "work", Path: "/docs/file.txt"
func ParsePath(input string) PathInfo {
	if strings.Contains(input, ":/") {
		parts := strings.SplitN(input, ":/", 2)
		return PathInfo{
			IsRemote: true,
			Alias:    parts[0],
			Path:     "/" + parts[1],
		}
	}
	return PathInfo{
		IsRemote: false,
		Path:     input,
	}
}
