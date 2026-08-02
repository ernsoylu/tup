package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/telegram"
	"github.com/ernsoylu/tup/internal/vfs"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

// -- CP --
var cpCmd = &cobra.Command{
	Use:   "cp [source] [destination]",
	Short: "Copy files and directories",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		recursive, _ := cmd.Flags().GetBool("recursive")
		srcInfo := resolveRemotePath(args[0])
		// Local sources stay local if not a registered alias
		if !srcInfo.IsRemote {
			srcInfo = vfs.ParsePath(args[0])
		}
		dstInfo := resolveRemotePath(args[1])
		if !dstInfo.IsRemote {
			dstInfo = vfs.ParsePath(args[1])
		}

		ctx := cmd.Context()
		var err error
		switch {
		case !srcInfo.IsRemote && dstInfo.IsRemote:
			err = cpLocalToRemote(ctx, srcInfo.Path, dstInfo, recursive)
		case srcInfo.IsRemote && !dstInfo.IsRemote:
			err = cpRemoteToLocal(ctx, srcInfo, dstInfo.Path, recursive)
		case srcInfo.IsRemote && dstInfo.IsRemote:
			err = cpRemoteToRemote(ctx, srcInfo, dstInfo, recursive)
		default:
			err = fmt.Errorf("local to local copies are not supported by tup. Use standard 'cp'")
		}
		if err != nil {
			fail(err)
		}
	},
}

func fail(err error) {
	pterm.Error.Println(err)
	os.Exit(1)
}

func failf(format string, args ...interface{}) {
	pterm.Error.Printf(format, args...)
	os.Exit(1)
}

func cpLocalToRemote(ctx context.Context, localPath string, dst vfs.PathInfo, recursive bool) error {
	st, err := os.Stat(localPath)
	if err != nil {
		return fmt.Errorf("local path not found: %w", err)
	}

	if st.IsDir() {
		if !recursive {
			return fmt.Errorf("%s is a directory (use -r to copy recursively)", localPath)
		}
		return uploadDir(ctx, localPath, dst)
	}
	return uploadFile(ctx, localPath, dst)
}

func uploadFile(ctx context.Context, localPath string, dst vfs.PathInfo) error {
	st, err := os.Stat(localPath)
	if err != nil {
		return err
	}
	chatID, err := core.GetChatID(dst.Alias)
	if err != nil {
		return err
	}

	filename, parentPath := destFileNameAndParent(dst, filepath.Base(localPath))
	parentID, err := ensureParent(dst.Alias, parentPath)
	if err != nil {
		return err
	}

	pterm.Info.Printf("Uploading %s to %s\n", localPath, vfs.FormatRemote(dst.Alias, path.Join(parentPath, filename)))
	msgID, err := telegram.UploadFileMTProto(ctx, localPath, chatID)
	if err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}

	if err := core.InsertEntry(core.VfsEntry{
		Alias:     dst.Alias,
		ParentID:  parentID,
		Name:      filename,
		IsDir:     false,
		Size:      st.Size(),
		MessageID: msgID,
	}); err != nil {
		return fmt.Errorf("failed to record file in VFS: %w", err)
	}

	pterm.Success.Printf("Uploaded %s to %s (%d bytes)\n",
		localPath, vfs.FormatRemote(dst.Alias, path.Join(normalizeRemoteDir(parentPath), filename)), st.Size())
	return nil
}

func uploadDir(ctx context.Context, localDir string, dst vfs.PathInfo) error {
	// If destination exists as a directory, nest under basename(localDir).
	// Otherwise create the tree rooted at dst.Path.
	dirName := filepath.Base(localDir)
	targetPath := normalizeRemoteDir(dst.Path)
	if existing, err := core.GetEntryByPath(dst.Alias, targetPath); err == nil && existing != nil && existing.IsDir {
		targetPath = path.Join(targetPath, dirName)
	}

	if err := mkdirRemote(vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: targetPath}, true); err != nil {
		if !strings.Contains(err.Error(), "already exists") {
			return err
		}
	}

	return filepath.Walk(localDir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(localDir, p)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		remoteRel := filepath.ToSlash(rel)
		remoteFull := path.Join(targetPath, remoteRel)
		if info.IsDir() {
			return mkdirRemote(vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: remoteFull}, true)
		}
		return uploadFile(ctx, p, vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: remoteFull})
	})
}

func cpRemoteToLocal(ctx context.Context, src vfs.PathInfo, localPath string, recursive bool) error {
	entry, err := core.GetEntryByPath(src.Alias, src.Path)
	if err != nil || entry == nil {
		return fmt.Errorf("file not found on remote drive: %s", src.Format())
	}

	if entry.IsDir {
		if !recursive {
			return fmt.Errorf("%s is a directory (use -r to copy recursively)", src.Format())
		}
		return downloadDir(ctx, src.Alias, entry, src.Path, localPath)
	}

	chatID, err := core.GetChatID(src.Alias)
	if err != nil {
		return err
	}

	destPath := localPath
	if fi, err := os.Stat(destPath); err == nil && fi.IsDir() {
		destPath = filepath.Join(destPath, entry.Name)
	}

	outFile, err := os.Create(destPath)
	if err != nil {
		return fmt.Errorf("failed to create local file: %w", err)
	}
	pterm.Info.Printf("Downloading %s (%d bytes) to %s...\n", src.Format(), entry.Size, destPath)
	err = telegram.DownloadFileMTProto(ctx, chatID, entry.MessageID, outFile)
	_ = outFile.Close()
	if err != nil {
		return fmt.Errorf("download failed: %w", err)
	}
	pterm.Success.Printf("Downloaded %s to %s\n", src.Format(), destPath)
	return nil
}

func downloadDir(ctx context.Context, alias string, entry *core.VfsEntry, remotePath, localPath string) error {
	// If localPath exists as dir, nest under it; else create localPath as the dir
	destRoot := localPath
	if fi, err := os.Stat(localPath); err == nil && fi.IsDir() {
		destRoot = filepath.Join(localPath, entry.Name)
	}
	if err := os.MkdirAll(destRoot, 0755); err != nil {
		return err
	}

	chatID, err := core.GetChatID(alias)
	if err != nil {
		return err
	}

	return core.Walk(alias, entry.ID, remotePath, func(child core.VfsEntry, fullPath string) error {
		rel := strings.TrimPrefix(fullPath, strings.TrimSuffix(remotePath, "/"))
		rel = strings.TrimPrefix(rel, "/")
		localChild := filepath.Join(destRoot, filepath.FromSlash(rel))
		if child.IsDir {
			return os.MkdirAll(localChild, 0755)
		}
		if err := os.MkdirAll(filepath.Dir(localChild), 0755); err != nil {
			return err
		}
		f, err := os.Create(localChild)
		if err != nil {
			return err
		}
		pterm.Info.Printf("Downloading %s...\n", vfs.FormatRemote(alias, fullPath))
		err = telegram.DownloadFileMTProto(ctx, chatID, child.MessageID, f)
		_ = f.Close()
		return err
	})
}

func cpRemoteToRemote(ctx context.Context, src, dst vfs.PathInfo, recursive bool) error {
	srcEntry, err := core.GetEntryByPath(src.Alias, src.Path)
	if err != nil || srcEntry == nil {
		return fmt.Errorf("source not found: %s", src.Format())
	}

	if srcEntry.IsDir {
		if !recursive {
			return fmt.Errorf("%s is a directory (use -r to copy recursively)", src.Format())
		}
		return copyRemoteDir(ctx, src, srcEntry, dst)
	}
	return copyRemoteFile(ctx, src, srcEntry, dst)
}

func copyRemoteFile(ctx context.Context, src vfs.PathInfo, srcEntry *core.VfsEntry, dst vfs.PathInfo) error {
	filename, parentPath := destFileNameAndParent(dst, srcEntry.Name)
	parentID, err := ensureParent(dst.Alias, parentPath)
	if err != nil {
		return err
	}

	// Same drive: share Telegram message ID (no re-upload)
	if src.Alias == dst.Alias {
		if err := core.InsertEntry(core.VfsEntry{
			Alias:     dst.Alias,
			ParentID:  parentID,
			Name:      filename,
			IsDir:     false,
			Size:      srcEntry.Size,
			Sha256:    srcEntry.Sha256,
			MessageID: srcEntry.MessageID,
		}); err != nil {
			return err
		}
		pterm.Success.Printf("Copied %s to %s\n", src.Format(), vfs.FormatRemote(dst.Alias, path.Join(normalizeRemoteDir(parentPath), filename)))
		return nil
	}

	// Cross-drive: download to temp and re-upload
	chatSrc, err := core.GetChatID(src.Alias)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp("", "tup-cp-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()

	if err := telegram.DownloadFileMTProto(ctx, chatSrc, srcEntry.MessageID, tmp); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("download for remote copy failed: %w", err)
	}
	_ = tmp.Close()

	return uploadFile(ctx, tmpPath, vfs.PathInfo{
		IsRemote: true,
		Alias:    dst.Alias,
		Path:     path.Join(normalizeRemoteDir(parentPath), filename),
	})
}

func copyRemoteDir(ctx context.Context, src vfs.PathInfo, srcEntry *core.VfsEntry, dst vfs.PathInfo) error {
	dirName := srcEntry.Name
	targetPath := normalizeRemoteDir(dst.Path)
	if existing, err := core.GetEntryByPath(dst.Alias, targetPath); err == nil && existing != nil && existing.IsDir {
		targetPath = path.Join(targetPath, dirName)
	}
	if err := mkdirRemote(vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: targetPath}, true); err != nil {
		if !strings.Contains(err.Error(), "already exists") {
			return err
		}
	}

	return core.Walk(src.Alias, srcEntry.ID, src.Path, func(child core.VfsEntry, fullPath string) error {
		rel := strings.TrimPrefix(fullPath, strings.TrimSuffix(src.Path, "/"))
		rel = strings.TrimPrefix(rel, "/")
		remoteFull := path.Join(targetPath, rel)
		if child.IsDir {
			return mkdirRemote(vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: remoteFull}, true)
		}
		return copyRemoteFile(ctx,
			vfs.PathInfo{IsRemote: true, Alias: src.Alias, Path: fullPath},
			&child,
			vfs.PathInfo{IsRemote: true, Alias: dst.Alias, Path: remoteFull},
		)
	})
}

func destFileNameAndParent(dst vfs.PathInfo, defaultName string) (filename, parentPath string) {
	cleanDst := strings.TrimSuffix(dst.Path, "/")
	filename = path.Base(cleanDst)
	parentPath = path.Dir(cleanDst)
	if dst.Path == "/" || strings.HasSuffix(dst.Path, "/") || filename == "." || filename == "/" {
		filename = defaultName
		parentPath = normalizeRemoteDir(dst.Path)
	}
	if parentPath == "." {
		parentPath = "/"
	}
	return filename, parentPath
}

func normalizeRemoteDir(p string) string {
	if p == "" || p == "." {
		return "/"
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return path.Clean(p)
}

func ensureParent(alias, parentPath string) (int, error) {
	parentPath = normalizeRemoteDir(parentPath)
	if parentPath == "/" {
		return 0, nil
	}
	entry, err := core.GetEntryByPath(alias, parentPath)
	if err != nil || entry == nil {
		return 0, fmt.Errorf("destination parent does not exist: %s", vfs.FormatRemote(alias, parentPath))
	}
	if !entry.IsDir {
		return 0, fmt.Errorf("destination parent is not a directory: %s", vfs.FormatRemote(alias, parentPath))
	}
	return entry.ID, nil
}

// -- MV --
var mvCmd = &cobra.Command{
	Use:   "mv [source] [destination]",
	Short: "Move or rename files and directories",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		srcInfo := resolveRemotePath(args[0])
		if !srcInfo.IsRemote {
			srcInfo = vfs.ParsePath(args[0])
		}
		dstInfo := resolveRemotePath(args[1])
		if !dstInfo.IsRemote {
			dstInfo = vfs.ParsePath(args[1])
		}

		if !srcInfo.IsRemote || !dstInfo.IsRemote {
			fail(fmt.Errorf("tup mv only supports remote paths (e.g. VFS:/a VFS:/b)"))
		}

		srcEntry, err := core.GetEntryByPath(srcInfo.Alias, srcInfo.Path)
		if err != nil || srcEntry == nil {
			failf("source not found: %s\n", srcInfo.Format())
		}

		// If destination is an existing directory, move into it
		if destEntry, err := core.GetEntryByPath(dstInfo.Alias, dstInfo.Path); err == nil && destEntry != nil && destEntry.IsDir {
			dstInfo.Path = path.Join(dstInfo.Path, srcEntry.Name)
		}

		newName := path.Base(strings.TrimSuffix(dstInfo.Path, "/"))
		parentPath := path.Dir(strings.TrimSuffix(dstInfo.Path, "/"))
		if parentPath == "." {
			parentPath = "/"
		}

		// Same drive: reparent/rename in DB only
		if srcInfo.Alias == dstInfo.Alias {
			parentID, err := ensureParent(dstInfo.Alias, parentPath)
			if err != nil {
				fail(err)
			}
			if err := core.UpdateEntryParentAndName(srcEntry.ID, parentID, newName); err != nil {
				fail(fmt.Errorf("failed to move: %w", err))
			}
			pterm.Success.Printf("Moved %s to %s\n", srcInfo.Format(), vfs.FormatRemote(dstInfo.Alias, path.Join(normalizeRemoteDir(parentPath), newName)))
			return
		}

		// Cross-drive: copy then remove source
		if err := cpRemoteToRemote(cmd.Context(), srcInfo, dstInfo, true); err != nil {
			fail(err)
		}
		if err := removeRemote(cmd.Context(), srcInfo, true, false); err != nil {
			fail(fmt.Errorf("copied but failed to remove source: %w", err))
		}
		pterm.Success.Printf("Moved %s to %s\n", srcInfo.Format(), dstInfo.Format())
	},
}

// -- RM --
var rmCmd = &cobra.Command{
	Use:   "rm [target]",
	Short: "Remove files or directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		recursive, _ := cmd.Flags().GetBool("recursive")
		force, _ := cmd.Flags().GetBool("force")

		for _, arg := range args {
			targetInfo := resolveRemotePath(arg)
			if !targetInfo.IsRemote {
				targetInfo = vfs.ParsePath(arg)
			}
			if !targetInfo.IsRemote {
				fail(fmt.Errorf("use standard 'rm' for local files"))
			}
			if err := removeRemote(cmd.Context(), targetInfo, recursive, force); err != nil {
				if force {
					pterm.Warning.Println(err)
					continue
				}
				fail(err)
			}
			pterm.Success.Printf("Removed %s\n", targetInfo.Format())
		}
	},
}

func removeRemote(ctx context.Context, target vfs.PathInfo, recursive, force bool) error {
	entry, err := core.GetEntryByPath(target.Alias, target.Path)
	if err != nil || entry == nil {
		if force {
			return nil
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("path not found: %s", target.Format())
	}

	if entry.IsDir && !recursive {
		children, _ := core.ListDirectory(target.Alias, entry.ID)
		if len(children) > 0 {
			return fmt.Errorf("%s is a directory (use -r to remove recursively)", target.Format())
		}
		// empty dir: allow without -r (like rmdir behavior is separate; rm without -r fails on non-empty)
	}
	if entry.IsDir && !recursive {
		// Unix rm without -r fails on directories even if empty
		return fmt.Errorf("%s is a directory (use -r)", target.Format())
	}

	// Collect Telegram message IDs before deleting VFS rows
	msgIDs, err := core.CollectMessageIDs(entry.ID)
	if err != nil {
		return err
	}

	chatID, err := core.GetChatID(target.Alias)
	if err != nil {
		return err
	}

	if len(msgIDs) > 0 {
		if err := telegram.DeleteMessages(ctx, chatID, msgIDs); err != nil {
			// Still remove from VFS index so CLI stays consistent
			pterm.Warning.Printf("Telegram delete failed (index will still be cleared): %v\n", err)
		}
	}

	if err := core.DeleteEntryRecursive(entry.ID); err != nil {
		return fmt.Errorf("failed to delete VFS entry: %w", err)
	}
	return nil
}

// -- RMDIR --
var rmdirCmd = &cobra.Command{
	Use:   "rmdir [target]",
	Short: "Remove empty directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		for _, arg := range args {
			targetInfo := resolveRemotePath(arg)
			if !targetInfo.IsRemote {
				targetInfo = vfs.ParsePath(arg)
			}
			if !targetInfo.IsRemote {
				fail(fmt.Errorf("use standard 'rmdir' for local directories"))
			}

			entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
			if err != nil || entry == nil {
				failf("path not found: %s\n", targetInfo.Format())
			}
			if !entry.IsDir {
				failf("%s is not a directory\n", targetInfo.Format())
			}
			children, err := core.ListDirectory(targetInfo.Alias, entry.ID)
			if err != nil {
				fail(err)
			}
			if len(children) > 0 {
				failf("directory not empty: %s\n", targetInfo.Format())
			}
			if err := core.DeleteEntry(entry.ID); err != nil {
				fail(err)
			}
			pterm.Success.Printf("Removed directory %s\n", targetInfo.Format())
		}
	},
}

// -- MKDIR --
var mkdirCmd = &cobra.Command{
	Use:   "mkdir [directory]",
	Short: "Create directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		parents, _ := cmd.Flags().GetBool("parents")

		for _, arg := range args {
			targetInfo := resolveRemotePath(arg)
			if !targetInfo.IsRemote {
				targetInfo = vfs.ParsePath(arg)
			}
			if !targetInfo.IsRemote {
				fail(fmt.Errorf("use standard 'mkdir' for local directories"))
			}

			if err := mkdirRemote(targetInfo, parents); err != nil {
				fail(err)
			}
			pterm.Success.Printf("Created directory %s\n", targetInfo.Format())
		}
	},
}

// mkdirRemote creates a remote directory, optionally creating missing parents (-p).
func mkdirRemote(target vfs.PathInfo, parents bool) error {
	trimmed := strings.Trim(target.Path, "/")
	if trimmed == "" {
		return fmt.Errorf("cannot create root directory")
	}

	parts := strings.Split(trimmed, "/")
	parentID := 0
	built := ""

	for i, part := range parts {
		if part == "" || part == "." {
			continue
		}
		built += "/" + part
		isLast := i == len(parts)-1

		existing, err := core.GetEntryByPath(target.Alias, built)
		if err == nil && existing != nil {
			if !existing.IsDir {
				return fmt.Errorf("%s exists and is not a directory", vfs.FormatRemote(target.Alias, built))
			}
			if isLast && !parents {
				return fmt.Errorf("directory already exists: %s", vfs.FormatRemote(target.Alias, built))
			}
			parentID = existing.ID
			continue
		}

		if !isLast && !parents {
			return fmt.Errorf("parent path does not exist: %s", vfs.FormatRemote(target.Alias, built))
		}

		if err := core.InsertEntry(core.VfsEntry{
			Alias:    target.Alias,
			ParentID: parentID,
			Name:     part,
			IsDir:    true,
		}); err != nil {
			return fmt.Errorf("failed to create directory %s: %w", vfs.FormatRemote(target.Alias, built), err)
		}

		created, err := core.GetEntryByPath(target.Alias, built)
		if err != nil || created == nil {
			return fmt.Errorf("created directory but failed to resolve %s: %v", vfs.FormatRemote(target.Alias, built), err)
		}
		parentID = created.ID
	}

	return nil
}

// -- LS --
var lsCmd = &cobra.Command{
	Use:   "ls [directory]",
	Short: "List directory contents",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		showAll, _ := cmd.Flags().GetBool("all")
		long, _ := cmd.Flags().GetBool("long")
		human, _ := cmd.Flags().GetBool("human-readable")
		recursive, _ := cmd.Flags().GetBool("recursive")

		target := ""
		if len(args) > 0 {
			target = args[0]
		}
		targetInfo := resolveRemotePath(target)
		if !targetInfo.IsRemote {
			if target == "" || target == "." {
				pterm.Error.Println("Use standard 'ls' for local directories. Example: tup ls VFS:/")
				return
			}
			targetInfo = vfs.ParsePath(target)
		}
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'ls' for local directories.")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			pterm.Error.Println(err)
			return
		}

		if !entry.IsDir {
			printLsEntry(entry, long, human)
			return
		}

		if err := lsDir(targetInfo.Alias, entry.ID, targetInfo.Path, showAll, long, human, recursive); err != nil {
			pterm.Error.Println(err)
		}
	},
}

func lsDir(alias string, parentID int, dirPath string, showAll, long, human, recursive bool) error {
	children, err := core.ListDirectory(alias, parentID)
	if err != nil {
		return err
	}
	if recursive && dirPath != "" {
		pterm.DefaultBasicText.Printf("%s:\n", vfs.FormatRemote(alias, dirPath))
	}
	for _, child := range children {
		if !showAll && strings.HasPrefix(child.Name, ".") {
			continue
		}
		printLsEntry(&child, long, human)
	}
	if !recursive {
		return nil
	}
	for _, child := range children {
		if !child.IsDir {
			continue
		}
		if !showAll && strings.HasPrefix(child.Name, ".") {
			continue
		}
		childPath := path.Join(normalizeRemoteDir(dirPath), child.Name)
		pterm.DefaultBasicText.Println()
		if err := lsDir(alias, child.ID, childPath, showAll, long, human, true); err != nil {
			return err
		}
	}
	return nil
}

func printLsEntry(e *core.VfsEntry, long, human bool) {
	if !long {
		if e.IsDir {
			pterm.DefaultBasicText.Printf(pterm.Blue("%s/\n"), e.Name)
		} else {
			pterm.DefaultBasicText.Println(e.Name)
		}
		return
	}
	kind := "file"
	if e.IsDir {
		kind = "dir "
	}
	sizeStr := formatSize(e.Size, human)
	pterm.DefaultBasicText.Printf("%s  %10s  msg=%-8d  %s\n", kind, sizeStr, e.MessageID, e.Name)
}

func formatSize(n int64, human bool) string {
	if !human {
		return fmt.Sprintf("%d", n)
	}
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%dB", n)
	}
	div, exp := int64(unit), 0
	for v := n / unit; v >= unit; v /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f%ciB", float64(n)/float64(div), "KMGTPE"[exp])
}

// -- CAT --
var catCmd = &cobra.Command{
	Use:   "cat [file]",
	Short: "Print file contents to standard output",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		targetInfo := resolveRemotePath(args[0])
		if !targetInfo.IsRemote {
			targetInfo = vfs.ParsePath(args[0])
		}
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'cat' for local files.")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			pterm.Error.Printf("File not found on remote drive: %s\n", targetInfo.Path)
			return
		}
		if entry.IsDir {
			pterm.Error.Printf("%s is a directory\n", targetInfo.Path)
			return
		}
		if entry.MessageID == 0 {
			// Empty touch placeholder — print nothing
			return
		}

		chatID, err := core.GetChatID(targetInfo.Alias)
		if err != nil {
			pterm.Error.Println("Failed to resolve drive alias:", err)
			return
		}

		err = telegram.DownloadFileMTProto(cmd.Context(), chatID, entry.MessageID, os.Stdout)
		if err != nil {
			pterm.Error.Println("Streaming failed:", err)
			return
		}
	},
}

// -- FIND --
var findCmd = &cobra.Command{
	Use:   "find [path] [-name pattern]",
	Short: "Search for files in a directory hierarchy",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		namePat, _ := cmd.Flags().GetString("name")
		// Support: find path pattern  OR  find path -name pattern
		rootArg := args[0]
		if namePat == "" && len(args) >= 2 {
			// if second arg is not a flag-like leftover, treat as name pattern
			if !strings.HasPrefix(args[1], "-") {
				namePat = args[1]
			}
		}
		if namePat == "" {
			namePat = "*"
		}

		targetInfo := resolveRemotePath(rootArg)
		if !targetInfo.IsRemote {
			targetInfo = vfs.ParsePath(rootArg)
		}
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'find' for local paths.")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			pterm.Error.Println(err)
			return
		}
		if !entry.IsDir {
			if matchName(entry.Name, namePat) {
				pterm.DefaultBasicText.Println(targetInfo.Format())
			}
			return
		}

		// Include root if matches
		if matchName(entry.Name, namePat) && entry.Name != "/" {
			pterm.DefaultBasicText.Println(targetInfo.Format())
		}

		err = core.Walk(targetInfo.Alias, entry.ID, targetInfo.Path, func(e core.VfsEntry, fullPath string) error {
			if matchName(e.Name, namePat) {
				pterm.DefaultBasicText.Println(vfs.FormatRemote(targetInfo.Alias, fullPath))
			}
			return nil
		})
		if err != nil {
			pterm.Error.Println(err)
		}
	},
}

func matchName(name, pattern string) bool {
	ok, err := path.Match(pattern, name)
	return err == nil && ok
}

// -- STAT --
var statCmd = &cobra.Command{
	Use:   "stat [file]",
	Short: "Display file or file system status",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		for _, arg := range args {
			targetInfo := resolveRemotePath(arg)
			if !targetInfo.IsRemote {
				targetInfo = vfs.ParsePath(arg)
			}
			if !targetInfo.IsRemote {
				pterm.Error.Println("Use standard 'stat' for local files.")
				return
			}
			entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
			if err != nil || entry == nil {
				pterm.Error.Println(err)
				return
			}
			kind := "regular file"
			if entry.IsDir {
				kind = "directory"
			}
			pterm.DefaultBasicText.Printf("  File: %s\n", targetInfo.Format())
			pterm.DefaultBasicText.Printf("  Type: %s\n", kind)
			pterm.DefaultBasicText.Printf("  Size: %d\n", entry.Size)
			pterm.DefaultBasicText.Printf("    ID: %d\n", entry.ID)
			pterm.DefaultBasicText.Printf("Parent: %d\n", entry.ParentID)
			pterm.DefaultBasicText.Printf(" Alias: %s\n", entry.Alias)
			pterm.DefaultBasicText.Printf("Msg ID: %d\n", entry.MessageID)
			if entry.Sha256 != "" {
				pterm.DefaultBasicText.Printf("SHA256: %s\n", entry.Sha256)
			}
		}
	},
}

// treeNode is the JSON shape for `tup tree --json` (AI / machine-readable).
type treeNode struct {
	Name      string     `json:"name"`
	Path      string     `json:"path"`
	Type      string     `json:"type"` // "directory" | "file"
	Size      int64      `json:"size,omitempty"`
	MessageID int        `json:"message_id,omitempty"`
	Children  []treeNode `json:"children,omitempty"`
}

type treeJSON struct {
	Path    string   `json:"path"`
	Alias   string   `json:"alias"`
	Tree    treeNode `json:"tree"`
	Summary struct {
		Directories int `json:"directories"`
		Files       int `json:"files"`
	} `json:"summary"`
}

// -- TREE --
var treeCmd = &cobra.Command{
	Use:   "tree [directory]",
	Short: "List contents of directories in a tree-like format",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		asJSON, _ := cmd.Flags().GetBool("json")
		target := ""
		if len(args) > 0 {
			target = args[0]
		}

		targetInfo := resolveRemotePath(target)
		if !targetInfo.IsRemote {
			fail(fmt.Errorf("use standard 'tree' for local directories. Example: tup tree VFS:/"))
		}

		if _, err := core.GetChatID(targetInfo.Alias); err != nil {
			fail(fmt.Errorf("failed to resolve drive alias: %w", err))
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			failf("path not found: %s\n", targetInfo.Format())
		}

		if asJSON {
			if err := printTreeJSON(targetInfo, entry); err != nil {
				fail(err)
			}
			return
		}

		if !entry.IsDir {
			pterm.DefaultBasicText.Println(targetInfo.Format())
			return
		}

		pterm.DefaultBasicText.Println(pterm.Bold.Sprint(targetInfo.Format()))
		dirs, files, err := printTree(targetInfo.Alias, entry.ID, "")
		if err != nil {
			fail(fmt.Errorf("failed to build tree: %w", err))
		}
		pterm.DefaultBasicText.Printf("\n%d directories, %d files\n", dirs, files)
	},
}

func printTreeJSON(root vfs.PathInfo, entry *core.VfsEntry) error {
	name := entry.Name
	if entry.ID == 0 {
		name = "/"
	}
	node := treeNode{
		Name: name,
		Path: root.Format(),
		Type: "directory",
	}
	if !entry.IsDir {
		node.Type = "file"
		node.Size = entry.Size
		node.MessageID = entry.MessageID
	} else {
		children, dirs, files, err := buildTreeNodes(root.Alias, entry.ID, root.Path)
		if err != nil {
			return err
		}
		node.Children = children
		out := treeJSON{
			Path:  root.Format(),
			Alias: root.Alias,
			Tree:  node,
		}
		out.Summary.Directories = dirs
		out.Summary.Files = files
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(out)
	}

	// Single file root
	out := treeJSON{
		Path:  root.Format(),
		Alias: root.Alias,
		Tree:  node,
	}
	out.Summary.Files = 1
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

func buildTreeNodes(alias string, parentID int, parentPath string) (nodes []treeNode, dirs, files int, err error) {
	children, err := core.ListDirectory(alias, parentID)
	if err != nil {
		return nil, 0, 0, err
	}
	nodes = make([]treeNode, 0, len(children))
	for _, child := range children {
		fullPath := path.Join(normalizeRemoteDir(parentPath), child.Name)
		n := treeNode{
			Name: child.Name,
			Path: vfs.FormatRemote(alias, fullPath),
		}
		if child.IsDir {
			n.Type = "directory"
			dirs++
			sub, subDirs, subFiles, subErr := buildTreeNodes(alias, child.ID, fullPath)
			if subErr != nil {
				return nil, 0, 0, subErr
			}
			n.Children = sub
			dirs += subDirs
			files += subFiles
		} else {
			n.Type = "file"
			n.Size = child.Size
			n.MessageID = child.MessageID
			files++
		}
		nodes = append(nodes, n)
	}
	return nodes, dirs, files, nil
}

// resolveRemotePath parses a path and treats a bare drive alias (e.g. "VFS") as remote root.
func resolveRemotePath(input string) vfs.PathInfo {
	if input == "" || input == "." {
		return vfs.PathInfo{IsRemote: false, Path: input}
	}
	info := vfs.ParsePath(input)
	if info.IsRemote {
		return info
	}
	if !strings.ContainsAny(input, "/\\") {
		if _, err := core.GetChatID(input); err == nil {
			return vfs.PathInfo{IsRemote: true, Alias: input, Path: "/"}
		}
	}
	return info
}

func printTree(alias string, parentID int, prefix string) (dirs, files int, err error) {
	children, err := core.ListDirectory(alias, parentID)
	if err != nil {
		return 0, 0, err
	}

	for i, child := range children {
		isLast := i == len(children)-1
		branch := "├── "
		nextPrefix := prefix + "│   "
		if isLast {
			branch = "└── "
			nextPrefix = prefix + "    "
		}

		label := child.Name
		if child.IsDir {
			label = pterm.Blue(child.Name + "/")
			dirs++
		} else {
			files++
		}
		pterm.DefaultBasicText.Print(prefix + branch + label + "\n")

		if child.IsDir {
			subDirs, subFiles, subErr := printTree(alias, child.ID, nextPrefix)
			if subErr != nil {
				return dirs, files, subErr
			}
			dirs += subDirs
			files += subFiles
		}
	}
	return dirs, files, nil
}

// -- TOUCH --
var touchCmd = &cobra.Command{
	Use:   "touch [file]",
	Short: "Create empty file or update timestamps",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		for _, arg := range args {
			targetInfo := resolveRemotePath(arg)
			if !targetInfo.IsRemote {
				targetInfo = vfs.ParsePath(arg)
			}
			if !targetInfo.IsRemote {
				pterm.Error.Println("Use standard 'touch' for local files.")
				return
			}

			// Exists: success no-op (no mtime column)
			if core.EntryExists(targetInfo.Alias, targetInfo.Path) {
				pterm.Success.Printf("Touched %s\n", targetInfo.Format())
				continue
			}

			// Create empty file via temp upload
			tmp, err := os.CreateTemp("", "tup-touch-*")
			if err != nil {
				pterm.Error.Println(err)
				return
			}
			tmpPath := tmp.Name()
			_ = tmp.Close()

			if err := uploadFile(cmd.Context(), tmpPath, targetInfo); err != nil {
				_ = os.Remove(tmpPath)
				pterm.Error.Println(err)
				return
			}
			_ = os.Remove(tmpPath)
			pterm.Success.Printf("Created %s\n", targetInfo.Format())
		}
	},
}

// -- DU --
var duCmd = &cobra.Command{
	Use:   "du [directory]",
	Short: "Estimate file space usage",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		human, _ := cmd.Flags().GetBool("human-readable")
		summarize, _ := cmd.Flags().GetBool("summarize")

		target := ""
		if len(args) > 0 {
			target = args[0]
		}
		targetInfo := resolveRemotePath(target)
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'du' for local paths. Example: tup du VFS:/")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			pterm.Error.Println(err)
			return
		}

		if !entry.IsDir {
			size := entry.Size
			pterm.DefaultBasicText.Printf("%s\t%s\n", formatSize(size, human), targetInfo.Format())
			return
		}

		if summarize || entry.ID == 0 {
			var total int64
			if entry.ID == 0 {
				total, err = core.DuAlias(targetInfo.Alias, 0)
			} else {
				total, err = core.Du(entry.ID)
			}
			if err != nil {
				pterm.Error.Println(err)
				return
			}
			pterm.DefaultBasicText.Printf("%s\t%s\n", formatSize(total, human), targetInfo.Format())
			return
		}

		// Per-child sizes + total
		children, err := core.ListDirectory(targetInfo.Alias, entry.ID)
		if err != nil {
			pterm.Error.Println(err)
			return
		}
		var total int64
		for _, child := range children {
			var size int64
			if child.IsDir {
				size, err = core.Du(child.ID)
				if err != nil {
					pterm.Error.Println(err)
					return
				}
			} else {
				size = child.Size
			}
			total += size
			childPath := path.Join(normalizeRemoteDir(targetInfo.Path), child.Name)
			pterm.DefaultBasicText.Printf("%s\t%s\n", formatSize(size, human), vfs.FormatRemote(targetInfo.Alias, childPath))
		}
		pterm.DefaultBasicText.Printf("%s\ttotal\n", formatSize(total, human))
	},
}

func init() {
	for _, cmd := range []*cobra.Command{cpCmd, mvCmd, rmCmd, rmdirCmd, mkdirCmd, lsCmd, catCmd, findCmd, statCmd, treeCmd, touchCmd, duCmd} {
		cmd.Flags().SetInterspersed(false)
	}
	// Allow `tup tree PATH --json` as well as `tup tree --json PATH` for agents.
	treeCmd.Flags().SetInterspersed(true)

	cpCmd.Flags().BoolP("recursive", "r", false, "copy directories recursively")

	rmCmd.Flags().BoolP("recursive", "r", false, "remove directories and their contents recursively")
	rmCmd.Flags().BoolP("force", "f", false, "ignore nonexistent files and arguments, never prompt")

	mkdirCmd.Flags().BoolP("parents", "p", false, "no error if existing, make parent directories as needed")

	lsCmd.Flags().BoolP("all", "a", false, "do not ignore entries starting with .")
	lsCmd.Flags().BoolP("long", "l", false, "use a long listing format")
	lsCmd.Flags().BoolP("human-readable", "H", false, "with -l, print sizes in human readable format")
	lsCmd.Flags().BoolP("recursive", "R", false, "list subdirectories recursively")

	findCmd.Flags().String("name", "", "match basename against pattern (supports globs)")

	treeCmd.Flags().Bool("json", false, "emit machine-readable JSON tree on stdout (for AI tools / scripts)")

	duCmd.Flags().BoolP("human-readable", "H", false, "print sizes in human readable format")
	duCmd.Flags().BoolP("summarize", "s", false, "display only a total for each argument")

	RootCmd.AddCommand(cpCmd, mvCmd, rmCmd, rmdirCmd, mkdirCmd, lsCmd, catCmd, findCmd, statCmd, treeCmd, touchCmd, duCmd)
}
