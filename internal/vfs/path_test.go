package vfs

import "testing"

func TestParsePath(t *testing.T) {
	// Compact rows: input, wantAlias, wantPath, wantFormat, isRemote
	rows := []struct {
		in, alias, path, format string
		remote                  bool
	}{
		{"VFS:/README.md", "VFS", "/README.md", "VFS:/README.md", true},
		{"VFS://README.md", "VFS", "/README.md", "VFS:/README.md", true},
		{"VFS:///docs/a.txt", "VFS", "/docs/a.txt", "VFS:/docs/a.txt", true},
		{"work:/docs/file.txt", "work", "/docs/file.txt", "work:/docs/file.txt", true},
		{"work:/", "work", "/", "work:/", true},
		{"VFS:", "VFS", "/", "VFS:/", true},
		{"VFS://", "VFS", "/", "VFS:/", true},
		{"README.md", "", "README.md", "README.md", false},
		{"./local-report.pdf", "", "./local-report.pdf", "./local-report.pdf", false},
	}

	for _, row := range rows {
		t.Run(row.in, func(t *testing.T) {
			got := ParsePath(row.in)
			if got.IsRemote != row.remote || got.Alias != row.alias || got.Path != row.path {
				t.Fatalf("ParsePath(%q) = {remote:%v alias:%q path:%q}, want {remote:%v alias:%q path:%q}",
					row.in, got.IsRemote, got.Alias, got.Path, row.remote, row.alias, row.path)
			}
			if got.Format() != row.format {
				t.Fatalf("Format() = %q, want %q", got.Format(), row.format)
			}
		})
	}
}

func TestFormatRemote(t *testing.T) {
	// alias, path, want
	cases := [][3]string{
		{"VFS", "/README.md", "VFS:/README.md"},
		{"VFS", "README.md", "VFS:/README.md"},
		{"work", "/docs/a", "work:/docs/a"},
		{"work", "", "work:/"},
		{"work", "///nested", "work:/nested"},
	}
	for _, c := range cases {
		if got := FormatRemote(c[0], c[1]); got != c[2] {
			t.Errorf("FormatRemote(%q, %q) = %q, want %q", c[0], c[1], got, c[2])
		}
	}
}
