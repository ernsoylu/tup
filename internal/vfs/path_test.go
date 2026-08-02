package vfs

import "testing"

func TestParsePath(t *testing.T) {
	tests := []struct {
		input   string
		want    PathInfo
		wantFmt string
	}{
		{
			input:   "VFS:/README.md",
			want:    PathInfo{IsRemote: true, Alias: "VFS", Path: "/README.md"},
			wantFmt: "VFS:/README.md",
		},
		{
			input:   "VFS://README.md",
			want:    PathInfo{IsRemote: true, Alias: "VFS", Path: "/README.md"},
			wantFmt: "VFS:/README.md",
		},
		{
			input:   "VFS:///docs/a.txt",
			want:    PathInfo{IsRemote: true, Alias: "VFS", Path: "/docs/a.txt"},
			wantFmt: "VFS:/docs/a.txt",
		},
		{
			input:   "work:/docs/file.txt",
			want:    PathInfo{IsRemote: true, Alias: "work", Path: "/docs/file.txt"},
			wantFmt: "work:/docs/file.txt",
		},
		{
			input:   "work:/",
			want:    PathInfo{IsRemote: true, Alias: "work", Path: "/"},
			wantFmt: "work:/",
		},
		{
			input:   "README.md",
			want:    PathInfo{IsRemote: false, Path: "README.md"},
			wantFmt: "README.md",
		},
		{
			input:   "./local-report.pdf",
			want:    PathInfo{IsRemote: false, Path: "./local-report.pdf"},
			wantFmt: "./local-report.pdf",
		},
		{
			input:   "VFS:",
			want:    PathInfo{IsRemote: false, Path: "VFS:"},
			wantFmt: "VFS:",
		},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := ParsePath(tt.input)
			if got.IsRemote != tt.want.IsRemote || got.Alias != tt.want.Alias || got.Path != tt.want.Path {
				t.Fatalf("ParsePath(%q) = %+v, want %+v", tt.input, got, tt.want)
			}
			if fmt := got.Format(); fmt != tt.wantFmt {
				t.Fatalf("Format() = %q, want %q", fmt, tt.wantFmt)
			}
		})
	}
}

func TestFormatRemote(t *testing.T) {
	tests := []struct {
		alias, path, want string
	}{
		{"VFS", "/README.md", "VFS:/README.md"},
		{"VFS", "README.md", "VFS:/README.md"},
		{"work", "/docs/a", "work:/docs/a"},
		{"work", "", "work:/"},
		{"work", "///nested", "work:/nested"},
	}

	for _, tt := range tests {
		got := FormatRemote(tt.alias, tt.path)
		if got != tt.want {
			t.Errorf("FormatRemote(%q, %q) = %q, want %q", tt.alias, tt.path, got, tt.want)
		}
	}
}
